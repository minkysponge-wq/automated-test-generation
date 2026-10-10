"""
Excel logsheet templates and the placeholder-based importer.

Template convention (generated from mappings/fields.yaml by tools/make_excel_templates.py):
  * every input cell initially holds a token  {{key}}   e.g. {{nll.bt.v12}}
  * every input cell is also a workbook *defined name*  key_with_underscores -> 'Sheet'!$C$12
  * engineers simply type over the token

Importer strategy (independent of fixed coordinates, so moving rows/columns is safe):
  1. look the key up by defined name
  2. otherwise scan every sheet for the leftover token {{key}}  (=> reported as unfilled)
  3. otherwise report the placeholder as missing
It also reports non-numeric values where a number is expected and a header
sample-code mismatch (protects against uploading another job's logsheet).
"""
import datetime as _dt
import re
from dataclasses import dataclass, field as dc_field

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.workbook.defined_name import DefinedName

from . import fields as F

TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")

NAVY = "1F3864"
_thin = Side(style="thin", color="7F7F7F")
BORDER = Border(left=_thin, right=_thin, top=_thin, bottom=_thin)
FILL_INPUT = PatternFill("solid", fgColor="FFF9DB")    # pale yellow = type here
FILL_HEADER = PatternFill("solid", fgColor="DDEBF7")   # pale blue  = pre-filled by system
FILL_TITLE = PatternFill("solid", fgColor=NAVY)
FILL_COLHEAD = PatternFill("solid", fgColor="D9D9D9")
WRAP = Alignment(wrap_text=True, vertical="center")
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)

STATION_TITLES = {
    "losses": ("LOG-SHEET FOR LOSS MEASUREMENT ON TRANSFORMER", "DATASHEET FOR LOSSES CALCULATION (CPRI SCL Transformer Loss Measurement)"),
    "routine": ("LOG SHEET FOR ROUTINE TESTS ON TRANSFORMERS", None),
    "sc": ("LOG-SHEET FOR SHORT CIRCUIT TEST ON TRANSFORMERS", None),
    "temprise": ("LOG SHEET FOR TEMPERATURE-RISE TEST ON OIL IMMERSED / DRY TYPE TRANSFORMERS", None),
    "pressure": ("LOG SHEET FOR AIR PRESSURE TESTS AND OIL LEAKAGE TEST", None),
}


# ============================================================================
# Template generation
# ============================================================================
def _ordered_unique(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _add_name(wb, ws, key, cell):
    name = F.defined_name(key)
    ref = f"'{ws.title}'!${cell.column_letter}${cell.row}"
    wb.defined_names[name] = DefinedName(name, attr_text=ref)


def _input_cell(wb, ws, row, col, f, fill=FILL_INPUT):
    c = ws.cell(row=row, column=col, value="{{%s}}" % f["key"])
    c.fill = fill
    c.border = BORDER
    c.alignment = CENTER
    c.font = Font(size=9, color="7F6000")
    _add_name(wb, ws, f["key"], c)
    return c


def build_station_template(station):
    """Return an openpyxl Workbook with tokens + defined names for one station."""
    flds = F.station_fields(station)
    wb = Workbook()
    wb.remove(wb.active)
    sheets = _ordered_unique(f["sheet"] for f in flds)
    titles = STATION_TITLES.get(station, (station.upper(), None))
    for si, sheet in enumerate(sheets):
        ws = wb.create_sheet(sheet)
        sflds = [f for f in flds if f["sheet"] == sheet]
        title = titles[si] if si < len(titles) and titles[si] else titles[0]
        # ---- title block
        ws.column_dimensions["A"].width = 2
        ws.column_dimensions["B"].width = 34
        for col in range(3, 20):
            ws.column_dimensions[get_column_letter(col)].width = 12
        ws.merge_cells("B1:N1")
        c = ws["B1"]
        c.value = "CENTRAL POWER RESEARCH INSTITUTE - SHORT CIRCUIT LABORATORY, BENGALURU"
        c.font = Font(bold=True, color="FFFFFF", size=12)
        c.fill = FILL_TITLE
        c.alignment = CENTER
        ws.merge_cells("B2:N2")
        ws["B2"].value = title
        ws["B2"].font = Font(bold=True, size=12, underline="single")
        ws["B2"].alignment = CENTER
        ws.merge_cells("B3:N3")
        ws["B3"].value = ("Type your readings over the yellow {{placeholders}}. Blue cells are pre-filled by ATRG. "
                          "Do not rename sheets' defined names. Station: " + F.stations()[station]["title"])
        ws["B3"].font = Font(italic=True, size=8, color="595959")
        row = 5
        sections = _ordered_unique(f["section"] for f in sflds)
        # header fields only on the first sheet
        for sec in sections:
            sec_fields = [f for f in sflds if f["section"] == sec]
            ws.cell(row=row, column=2, value=sec).font = Font(bold=True, color=NAVY, size=10)
            row += 1
            grid = [f for f in sec_fields if f.get("row") is not None and f.get("col") is not None]
            if grid:
                rows = _ordered_unique(f["row"] for f in grid)
                cols = _ordered_unique(f["col"] for f in grid)
                unit = _ordered_unique(f["unit"] for f in grid)
                hdr = ws.cell(row=row, column=2, value=("Unit: " + ", ".join(u for u in unit if u)) if any(unit) else "")
                hdr.fill = FILL_COLHEAD
                hdr.border = BORDER
                hdr.font = Font(size=8, italic=True)
                for ci, col_label in enumerate(cols):
                    c = ws.cell(row=row, column=3 + ci, value=col_label)
                    c.fill = FILL_COLHEAD
                    c.border = BORDER
                    c.font = Font(bold=True, size=9)
                    c.alignment = CENTER
                row += 1
                lookup = {(f["row"], f["col"]): f for f in grid}
                for r_label in rows:
                    c = ws.cell(row=row, column=2, value=r_label)
                    c.border = BORDER
                    c.font = Font(bold=True, size=9)
                    for ci, col_label in enumerate(cols):
                        f = lookup.get((r_label, col_label))
                        if f:
                            _input_cell(wb, ws, row, 3 + ci, f)
                        else:
                            ws.cell(row=row, column=3 + ci).fill = PatternFill("solid", fgColor="F2F2F2")
                            ws.cell(row=row, column=3 + ci).border = BORDER
                    row += 1
            for f in [f for f in sec_fields if f not in grid]:
                lab = ws.cell(row=row, column=2, value=f["label"] + (f" ({f['unit']})" if f["unit"] else ""))
                lab.border = BORDER
                lab.alignment = WRAP
                lab.font = Font(size=9)
                ws.merge_cells(start_row=row, start_column=3, end_row=row, end_column=6)
                is_hdr = ".hdr." in f["key"]
                _input_cell(wb, ws, row, 3, f, FILL_HEADER if is_hdr else FILL_INPUT)
                for col in range(4, 7):
                    ws.cell(row=row, column=col).border = BORDER
                row += 1
            row += 1
        # signature block like the paper logsheets
        ws.cell(row=row + 1, column=2, value="Customer's Signature").font = Font(bold=True, size=9)
        ws.cell(row=row + 1, column=9, value="Test Engineer").font = Font(bold=True, size=9)
        ws.cell(row=row + 3, column=2, value="DOCUMENT : FORMS & FORMATS  -  generated by CPRI ATRG").font = Font(size=7, color="7F7F7F")
        ws.sheet_view.showGridLines = False
        ws.page_setup.orientation = "landscape"
        ws.page_setup.fitToWidth = 1
        ws.freeze_panes = "C5"
    return wb


def fill_header(wb, station, header_values):
    """Pre-fill the {station}.hdr.* cells of a template for a specific job."""
    for k, v in header_values.items():
        key = f"{station}.hdr.{k}"
        dn = wb.defined_names.get(F.defined_name(key))
        if dn is None or v in (None, ""):
            continue
        for title, coord in dn.destinations:
            wb[title][coord.replace("$", "")].value = v


def fill_values(wb, values):
    """Write {key: value} into the cells of their defined names (used for sample data)."""
    for key, v in values.items():
        dn = wb.defined_names.get(F.defined_name(key))
        if dn is None:
            continue
        for title, coord in dn.destinations:
            cell = wb[title][coord.replace("$", "")]
            cell.value = v
            cell.font = Font(size=10, color="000000")


# ============================================================================
# Import
# ============================================================================
@dataclass
class ImportResult:
    values: dict = dc_field(default_factory=dict)      # key -> (value_str, "Sheet!C12")
    missing: list = dc_field(default_factory=list)     # placeholder not found at all
    unfilled: list = dc_field(default_factory=list)    # (key, cell) token left untouched / blank required
    non_numeric: list = dc_field(default_factory=list) # (key, cell, value)
    errors: list = dc_field(default_factory=list)      # fatal problems (e.g. wrong sample code)
    warnings: list = dc_field(default_factory=list)

    @property
    def ok(self):
        return not self.errors

    def summary(self):
        return {"values": len(self.values), "missing": self.missing, "unfilled": self.unfilled,
                "non_numeric": self.non_numeric, "errors": self.errors, "warnings": self.warnings}


def _cell_to_str(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, _dt.datetime):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, _dt.date):
        return v.strftime("%d-%m-%Y")
    if isinstance(v, _dt.time):
        return v.strftime("%H:%M")
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    s = str(v).strip()
    return s or None


def _scan_tokens(wb):
    """All leftover {{key}} tokens -> {key: 'Sheet!A1'}."""
    found = {}
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for c in row:
                if isinstance(c.value, str) and "{{" in c.value:
                    for m in TOKEN_RE.finditer(c.value):
                        found.setdefault(m.group(1), f"{ws.title}!{c.coordinate}")
    return found


def import_workbook(path, station, test_codes, expected_sample_code=None):
    """Read one uploaded logsheet. Pure function - does not touch the database."""
    res = ImportResult()
    try:
        wb = load_workbook(path, data_only=True)
    except Exception as exc:  # corrupt / not an xlsx
        res.errors.append(f"Could not read the Excel file: {exc}")
        return res
    tokens = _scan_tokens(wb)
    for f in F.station_fields(station):
        key = f["key"]
        required = F.is_required(f, test_codes)
        dn = wb.defined_names.get(F.defined_name(key))
        cell_ref, raw = None, None
        if dn is not None:
            try:
                dests = list(dn.destinations)
            except Exception:
                dests = []
            if dests:
                title, coord = dests[0]
                coord = coord.replace("$", "")
                if title in wb.sheetnames:
                    raw = wb[title][coord].value
                    cell_ref = f"{title}!{coord}"
        if cell_ref is None:
            # defined name lost (e.g. sheet copied) - fall back to token scan
            if key in tokens:
                if required:
                    res.unfilled.append((key, tokens[key]))
                continue
            if required:
                res.missing.append(key)
            continue
        if isinstance(raw, str) and TOKEN_RE.fullmatch(raw.strip()):
            if required:
                res.unfilled.append((key, cell_ref))
            continue
        val = _cell_to_str(raw)
        if val is None:
            if required:
                res.unfilled.append((key, cell_ref))
            continue
        if f["type"] == "number" and F.to_number(val) is None:
            res.non_numeric.append((key, cell_ref, val))
        res.values[key] = (val, cell_ref)
    # header must belong to this job
    sc_key = f"{station}.hdr.sample_code"
    if expected_sample_code and sc_key in res.values:
        got = res.values[sc_key][0].replace(" ", "").upper()
        if got != expected_sample_code.upper():
            res.errors.append(f"This logsheet is for sample code {res.values[sc_key][0]}, "
                              f"but the job is {expected_sample_code}. Upload the right file.")
    if not res.values:
        if tokens:
            res.errors.append("The workbook still contains only empty {{placeholders}} - type the readings over them first.")
        else:
            res.errors.append("No ATRG placeholders were found in this workbook - "
                              "download the template from ATRG and fill that file.")
    return res


# ============================================================================
# Customer request Excel (two-column form)
# ============================================================================
def build_request_template(request_fields):
    """Field | Value sheet with {{req.name}} tokens and defined names req_<name>."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Request"
    ws.column_dimensions["A"].width = 2
    ws.column_dimensions["B"].width = 55
    ws.column_dimensions["C"].width = 60
    ws.column_dimensions["D"].width = 40
    ws.merge_cells("B1:D1")
    ws["B1"].value = "CPRI - CUSTOMER REQUEST FORM (CPRI/QAF/01A) + PROFORMA FOR TRANSFORMERS"
    ws["B1"].font = Font(bold=True, color="FFFFFF", size=12)
    ws["B1"].fill = FILL_TITLE
    ws.merge_cells("B2:D2")
    ws["B2"].value = ("Type over the yellow {{placeholders}} and upload this file on the New Test Request page "
                      "to auto-fill the form. Allowed values are listed in column D.")
    ws["B2"].font = Font(italic=True, size=9)
    row = 4
    for sec in request_fields:
        ws.cell(row=row, column=2, value=sec["title"]).font = Font(bold=True, color=NAVY)
        row += 1
        for fd in sec["fields"]:
            if fd["type"] in ("drawings", "tests"):
                continue
            lab = ws.cell(row=row, column=2, value=fd["label"] + (" *" if fd.get("required") else ""))
            lab.border = BORDER
            lab.alignment = WRAP
            c = ws.cell(row=row, column=3, value="{{req.%s}}" % fd["name"])
            c.fill = FILL_INPUT
            c.border = BORDER
            c.alignment = WRAP
            name = "req_" + fd["name"]
            wb.defined_names[name] = DefinedName(name, attr_text=f"'Request'!$C${row}")
            if fd.get("options"):
                ws.cell(row=row, column=4, value=" / ".join(o[0] if isinstance(o, (list, tuple)) else o
                                                          for o in fd["options"])).font = Font(size=8, color="595959")
            row += 1
        row += 1
    # drawings and tests as small tables
    ws.cell(row=row, column=2, value="Drawing numbers (one per row: number | sheet no. | revision)").font = Font(bold=True, color=NAVY)
    row += 1
    for i in range(1, 6):
        for j, part in enumerate(("number", "sheet", "rev")):
            c = ws.cell(row=row, column=2 + j, value="{{req.drawing%d_%s}}" % (i, part))
            c.fill = FILL_INPUT
            c.border = BORDER
            name = f"req_drawing{i}_{part}"
            wb.defined_names[name] = DefinedName(name, attr_text=f"'Request'!${get_column_letter(2 + j)}${row}")
        row += 1
    row += 1
    ws.cell(row=row, column=2, value="Tests requested (type Yes / No)").font = Font(bold=True, color=NAVY)
    row += 1
    for code, t in F.tests().items():
        ws.cell(row=row, column=2, value=f"{t['label']} [{t['clause']}]").border = BORDER
        c = ws.cell(row=row, column=3, value="{{req.test_%s}}" % code)
        c.fill = FILL_INPUT
        c.border = BORDER
        name = f"req_test_{code}"
        wb.defined_names[name] = DefinedName(name, attr_text=f"'Request'!$C${row}")
        row += 1
    return wb


def parse_request_workbook(path):
    """Return {name: value} for every filled req_* defined name."""
    wb = load_workbook(path, data_only=True)
    out = {}
    for name, dn in wb.defined_names.items():
        if not name.startswith("req_"):
            continue
        try:
            title, coord = next(iter(dn.destinations))
        except StopIteration:
            continue
        v = wb[title][coord.replace("$", "")].value
        s = _cell_to_str(v)
        if s is None or TOKEN_RE.fullmatch(s):
            continue
        out[name[4:]] = s
    return out
