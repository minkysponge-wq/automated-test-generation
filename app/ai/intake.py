"""
Unified document intake: `read_document(path, doc_kind)` is used by every upload screen.

Accepted files: PDF, JPG, PNG, XLSX, XLS, CSV.
doc_kind: request_form | logsheet:<test or station code> | logsheet:auto | work_instruction

PDF / images
  1. AcroForm fields and a real text layer are read deterministically (method=deterministic)
  2. every page is rendered with PyMuPDF (170 dpi) and sent to Gemini in one call with a schema built
     from the field registry (handwritten scans - the text layer is not relied upon)
Excel / CSV
  1. our own template (defined names / {{placeholders}}) is read deterministically - no AI call
  2. any other layout is turned into a compact text grid with cell addresses and merged ranges and Gemini
     maps cells to fields, returning the source cell for each value
  3. mixed: deterministic values are kept, only the remaining fields go to Gemini
Post-processing: number/unit normalisation, regex validation, failing values get confidence 0.5.
Low-confidence retry: if more than 20 % of the required fields are below 0.8, only those fields are asked
again on the Pro model with the relevant pages; the higher-confidence answer is kept.

Every field: {value, confidence, source, evidence_text, method, check}.
"""
import csv
import hashlib
import os
import re
from typing import List, Optional

from pydantic import BaseModel, Field

import config
from .. import excel_io, fields as F
from . import client

PROMPT_DIR = os.path.join(os.path.dirname(__file__), "prompts")
PAGES_DIR = os.path.join(config.UPLOAD_DIR, "pages")
CHECK_BELOW = 0.8
IMAGE_EXT = {".pdf", ".jpg", ".jpeg", ".png"}
SHEET_EXT = {".xlsx", ".xls", ".csv"}
ACCEPTED = IMAGE_EXT | SHEET_EXT
VG_RE = re.compile(r"^([DYZ])([dyz])(n?)(\d{1,2})$", re.I)
CODE_RES = {"sample_code": re.compile(r"^HVD\d{2}S\d{4}$"), "report_no": re.compile(r"^[A-Z]{4,}\d{2}T\d{4}(/A\d+)?$")}

WORK_INSTRUCTION_FIELDS = [
    ("test_series_no", "Test series number"), ("sample_code", "Sample code number"), ("customer", "Name of customer"),
    ("customer_ref", "Customer reference number"), ("description", "Description of test sample"),
    ("mounting", "Specific instructions for mounting and connections"), ("special_instructions", "Special instructions"),
    ("date_of_test", "Date of test"), ("customer_requirement", "Customer requirement"),
    ("tests_required", "Name of test or tests required"), ("reference_standard", "Reference standard for the test"),
    ("latest_standard", "Whether standard used is latest"), ("deviation", "Deviation / change from standard"),
    ("work_allotted_to", "Work allotted to"), ("work_completed_on", "Work completed on"),
    ("responsible_engineer", "Test engineer responsible for report"),
]


# =============================================================================
# schema (list form keeps the JSON schema small even for 200 fields)
# =============================================================================
class Entry(BaseModel):
    key: str = Field(description="field key from the list")
    value: Optional[str] = Field(None, description="value as written, null if blank")
    confidence: float = Field(0.0, description="0..1")
    source: Optional[str] = Field(None, description="pdf_page N or Sheet!B4")
    evidence_text: Optional[str] = None


class Drawing(BaseModel):
    number: str
    sheet: Optional[str] = None
    rev: Optional[str] = None
    confidence: float = 0.0


class TestPick(BaseModel):
    line_text: str = Field(description="the test line exactly as written")
    struck_out: bool = Field(False, description="true if the line is crossed out")
    code: Optional[str] = Field(None, description="catalogue code, null if no code fits")
    confidence: float = 0.0


class Extraction(BaseModel):
    fields: List[Entry]


class RequestExtraction(BaseModel):
    fields: List[Entry]
    drawings: List[Drawing] = []
    tests: List[TestPick] = []


class TestsOnly(BaseModel):
    tests: List[TestPick] = []


class Part(BaseModel):
    part: int = Field(description="1-based page or sheet number")
    logsheet: str = Field(description="logsheet code or none")


class Classification(BaseModel):
    parts: List[Part]


# =============================================================================
# field specs per doc_kind (from the registry)
# =============================================================================
def station_of(code):
    tests = F.tests()
    if code in F.stations():
        return code
    if code in tests:
        return tests[code]["station"]
    raise ValueError(f"Unknown logsheet code {code}")


def field_specs(doc_kind):
    """[{key, label, unit, dtype, options, where, required}] for the doc kind."""
    if doc_kind == "request_form":
        return [{"key": f["key"], "label": f["label"], "unit": f.get("unit") or "", "dtype": f["dtype"],
                 "options": f.get("options"), "where": f.get("section") or "", "required": f.get("required", False)}
                for f in F.request_fields() if f["dtype"] != "list"]
    if doc_kind == "work_instruction":
        return [{"key": k, "label": l, "unit": "", "dtype": "text", "options": None, "where": "", "required": False}
                for k, l in WORK_INSTRUCTION_FIELDS]
    if doc_kind.startswith("logsheet:"):
        st = station_of(doc_kind.split(":", 1)[1])
        out = []
        for f in F.station_fields(st):
            if ".hdr." in f["key"]:
                continue
            where = " / ".join(x for x in (f.get("section"), ("row " + f["row"]) if f.get("row") else None,
                                           ("column " + f["col"]) if f.get("col") else None) if x)
            out.append({"key": f["key"], "label": f["label"], "unit": f.get("unit") or "", "dtype": f["dtype"],
                        "options": None, "where": where, "required": f.get("required", False), "dp": f.get("dp")})
        return out
    raise ValueError(f"Unknown document kind {doc_kind}")


def _example(spec):
    if spec.get("options"):
        return " / ".join(spec["options"])
    if spec["dtype"] == "number":
        dp = spec.get("dp")
        return "number" + (f" with up to {dp} decimals" if dp else "")
    if spec["dtype"] == "date":
        return "date DD/MM/YYYY"
    return "text"


def prompt(doc_kind, specs):
    """System prompt and its version."""
    base = "logsheet" if doc_kind.startswith("logsheet:") else doc_kind
    with open(os.path.join(PROMPT_DIR, f"{base}.md"), encoding="utf-8") as fh:
        text = fh.read()
    version = re.search(r"version:\s*(\d+)", text).group(1)
    with open(os.path.join(PROMPT_DIR, "_common.md"), encoding="utf-8") as fh:
        common = fh.read()
    lines = [text.split("\n", 1)[1].strip(), "", common.strip(), ""]
    if doc_kind == "request_form":
        lines.append("Test catalogue (code: name [clause]):")
        lines += [f"  {c}: {t['name']} [{t['clause']}]" for c, t in F.tests().items()]
        lines.append("")
    if doc_kind.startswith("logsheet:"):
        st = station_of(doc_kind.split(":", 1)[1])
        lines.append(f"Logsheet: {F.stations()[st]['logsheet'] or F.stations()[st]['title']}")
    lines.append("Fields (key | label | unit | where on the form | expected format):")
    for s in specs:
        lines.append(f"  {s['key']} | {s['label']} | {s['unit'] or '-'} | {s['where'] or '-'} | {_example(s)}")
    return "\n".join(lines), f"{base}_v{version}"


# =============================================================================
# files -> parts
# =============================================================================
def render_pages(path, dpi=170):
    """PNG bytes per page (cached on disk for the review viewer) and their URLs."""
    import pymupdf
    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()[:16]
    out_dir = os.path.join(PAGES_DIR, sha)
    os.makedirs(out_dir, exist_ok=True)
    ext = os.path.splitext(path)[1].lower()
    pages, urls = [], []
    if ext == ".pdf":
        for i, page in enumerate(pymupdf.open(path)):
            png = os.path.join(out_dir, f"p{i + 1}.png")
            if not os.path.exists(png):
                page.get_pixmap(dpi=dpi).save(png)
            pages.append((open(png, "rb").read(), "image/png"))
            urls.append(f"/files/pages/{sha}/p{i + 1}.png")
    else:
        name = "p1" + (".png" if ext == ".png" else ".jpg")
        dst = os.path.join(out_dir, name)
        if not os.path.exists(dst):
            open(dst, "wb").write(open(path, "rb").read())
        pages.append((open(dst, "rb").read(), "image/png" if ext == ".png" else "image/jpeg"))
        urls.append(f"/files/pages/{sha}/{name}")
    return pages, urls


def load_sheets(path):
    """[(sheet_name, {(row, col): value}, merged_ranges)] for xlsx / xls / csv."""
    ext = os.path.splitext(path)[1].lower()
    sheets = []
    if ext == ".xlsx":
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True)
        for ws in wb.worksheets:
            cells = {(c.row, c.column): c.value for row in ws.iter_rows() for c in row if c.value not in (None, "")}
            sheets.append((ws.title, cells, [str(m) for m in ws.merged_cells.ranges]))
    elif ext == ".xls":
        import xlrd
        book = xlrd.open_workbook(path)
        for sh in book.sheets():
            cells = {(r + 1, c + 1): sh.cell_value(r, c) for r in range(sh.nrows) for c in range(sh.ncols)
                     if sh.cell_value(r, c) not in (None, "")}
            sheets.append((sh.name, cells, []))
    else:
        with open(path, newline="", encoding="utf-8-sig", errors="replace") as fh:
            rows = list(csv.reader(fh))
        cells = {(r + 1, c + 1): v for r, row in enumerate(rows) for c, v in enumerate(row) if v.strip()}
        sheets.append((os.path.splitext(os.path.basename(path))[0], cells, []))
    return sheets


def _addr(row, col):
    from openpyxl.utils import get_column_letter
    return f"{get_column_letter(col)}{row}"


def grid_text(sheets):
    """Compact text grid keeping cell addresses: 'B4: "No load current" | C4: 1.689'."""
    out = []
    for name, cells, merged in sheets:
        out.append(f"=== Sheet '{name}'" + (f" (merged ranges: {', '.join(merged[:40])})" if merged else ""))
        rows = sorted({r for r, _ in cells})
        for r in rows:
            items = []
            for (rr, c) in sorted(k for k in cells if k[0] == r):
                v = cells[(rr, c)]
                v = f'"{v}"' if isinstance(v, str) else (str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
                items.append(f"{_addr(r, c)}: {v[:80]}")
            out.append(" | ".join(items))
    return "\n".join(out)


def sheets_html(sheets, limit_rows=200):
    """HTML tables of the sheets for the review viewer (each cell carries data-cell='Sheet!B4')."""
    from html import escape
    parts = []
    for name, cells, _ in sheets:
        if not cells:
            continue
        maxr = min(max(r for r, _ in cells), limit_rows)
        maxc = min(max(c for _, c in cells), 30)
        rows = []
        for r in range(1, maxr + 1):
            tds = "".join(f'<td data-cell="{escape(name)}!{_addr(r, c)}">{escape(str(cells.get((r, c), "")))[:60]}</td>'
                          for c in range(1, maxc + 1))
            rows.append(f"<tr><th>{r}</th>{tds}</tr>")
        parts.append(f'<div class="sheet-view"><div class="sheet-name">{escape(name)}</div><table>{"".join(rows)}</table></div>')
    return "".join(parts)


def viewer(path):
    """{pages: [urls], html} for the review pane."""
    ext = os.path.splitext(path)[1].lower()
    if ext in IMAGE_EXT:
        return {"pages": render_pages(path)[1], "html": ""}
    return {"pages": [], "html": sheets_html(load_sheets(path))}


# =============================================================================
# deterministic paths
# =============================================================================
def deterministic_pdf(path, specs):
    """AcroForm fields and 'Label: value' lines of a real text layer."""
    import pymupdf
    keys = {s["key"]: s for s in specs}
    labels = {re.sub(r"\W+", " ", s["label"]).strip().lower(): s["key"] for s in specs}
    found = {}
    for pno, page in enumerate(pymupdf.open(path), start=1):
        for w in page.widgets() or []:
            name = (w.field_name or "").strip()
            key = name if name in keys else labels.get(re.sub(r"\W+", " ", name).strip().lower())
            if key and w.field_value not in (None, "", "Off"):
                found[key] = _item(str(w.field_value), 1.0, f"pdf_page {pno}", f"form field {name}", "deterministic")
        for line in page.get_text().splitlines():
            lab, sep, val = line.partition(":")
            key = labels.get(re.sub(r"\W+", " ", lab).strip().lower()) if sep else None
            if key and val.strip() and key not in found:
                found[key] = _item(val.strip(), 0.95, f"pdf_page {pno}", line.strip()[:80], "deterministic")
    return found


def deterministic_excel(path, doc_kind, test_codes):
    """Our own templates: defined names / tokens. Returns ({key: item}, recognised: bool)."""
    if not path.lower().endswith(".xlsx"):
        return {}, False
    if doc_kind == "request_form":
        values = excel_io.parse_request_workbook(path)
        if not values:
            from openpyxl import load_workbook
            names = [n for n in load_workbook(path).defined_names if n.startswith("req_")]
            return {}, bool(names)
        return {k: _item(v, 1.0, "placeholder", f"req_{k}", "deterministic") for k, v in values.items()}, True
    if doc_kind.startswith("logsheet:"):
        st = station_of(doc_kind.split(":", 1)[1])
        res = excel_io.import_workbook(path, st, test_codes)
        if not res.values and not res.unfilled:
            return {}, False
        return {k: _item(v, 1.0, cell, f"placeholder {k}", "deterministic") for k, (v, cell) in res.values.items()
                if ".hdr." not in k}, True
    return {}, False


# =============================================================================
# normalisation / validation
# =============================================================================
def _item(value, confidence, source, evidence, method):
    return {"value": value, "confidence": round(float(confidence or 0), 2), "source": source,
            "evidence_text": evidence, "method": method, "check": float(confidence or 0) < CHECK_BELOW}


def _num(text):
    m = re.search(r"[-+]?\d+(?:[.,]\d+)?", str(text or ""))
    return m.group(0).replace(",", ".") if m else None


def normalise(key, raw, spec=None):
    """Return (value, ok). ok=False -> failed validation (confidence capped at 0.5)."""
    spec = spec or {}
    f = F.field(key) or {}
    dtype = spec.get("dtype") or f.get("dtype")
    unit = spec.get("unit") if spec.get("unit") is not None else f.get("unit", "")
    options = spec.get("options") or f.get("options")
    if raw is None:
        return None, True
    if isinstance(raw, bool) or str(raw).strip().lower() in ("true", "false"):
        b = raw if isinstance(raw, bool) else str(raw).strip().lower() == "true"
        if options and sorted(options) == ["No", "Yes"]:
            return ("Yes" if b else "No"), True
        return ("Yes" if b else "No"), True
    v = str(raw).strip()
    if v.lower() in ("", "null", "none", "n/a"):
        return None, True
    if dtype == "number":
        n = _num(v)
        if n is None:
            return v, False
        volts = (unit or "").startswith("V") or "(V)" in (f.get("label") or "")
        if volts and re.search(r"kv", v, re.I) and float(n) < 100:     # "11 kV" in a volts field
            n = str(int(round(float(n) * 1000)))
        return n, True
    if key == "pf_vector_group":
        m = VG_RE.match(v.replace(" ", ""))
        if not m:
            return v, False
        return f"{m.group(1).upper()}{m.group(2).lower()}{m.group(3).lower()}{m.group(4)}", True
    if key == "pf_mfg":
        m = re.search(r"(\d{1,2})\s*[/\-.]\s*(\d{2,4})", v)
        if m:
            yy = m.group(2)
            return f"{int(m.group(1)):02d}/{'20' + yy if len(yy) == 2 else yy}", True
        return v, False
    if key in CODE_RES:
        v2 = v.replace(" ", "").upper()
        return v2, bool(CODE_RES[key].match(v2))
    if dtype == "date":
        m = re.search(r"(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{2,4})", v)
        if m:
            yy = m.group(3)
            return f"{int(m.group(1)):02d}-{int(m.group(2)):02d}-{'20' + yy if len(yy) == 2 else yy}", True
        return v, False
    if options:
        low = v.lower().replace("-", "").replace(" ", "")
        for o in options:
            if o.lower().replace("-", "").replace(" ", "") == low:
                return o, True
        for o in options:
            if low in o.lower().replace("-", "").replace(" ", "") or o.lower().replace("-", "").replace(" ", "") in low:
                return o, True
        return v, False
    return v, True


def split_rating(fields_out):
    """'250 kVA, 11 kV / 433 V' in `rating` fills empty kVA / HV / LV proforma fields."""
    rating = str((fields_out.get("rating") or {}).get("value") or "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*kva", rating, re.I)
    volts = re.findall(r"(\d+(?:\.\d+)?)\s*(kv|v)\b", rating, re.I)
    fill = {}
    if m:
        fill["pf_kva"] = m.group(1)
    if len(volts) >= 2:
        fill["pf_hv_v"], fill["pf_lv_v"] = [str(int(round(float(n) * (1000 if u.lower() == "kv" else 1)))) for n, u in volts[:2]]
    for k, v in fill.items():
        if not (fields_out.get(k) or {}).get("value"):
            fields_out[k] = _item(v, 0.7, (fields_out.get("rating") or {}).get("source"), f"from rating '{rating[:40]}'",
                                  "deterministic")


def _apply(entries, specs, out, prefer_higher=True):
    by_key = {s["key"]: s for s in specs}
    for e in entries or []:
        key = e.get("key")
        if key not in by_key:
            continue
        val, ok = normalise(key, e.get("value"), by_key[key])
        if val in (None, ""):
            continue
        conf = float(e.get("confidence") or 0)
        if not ok:
            conf = min(conf, 0.5)
        new = _item(val, conf, e.get("source"), e.get("evidence_text"), "ai")
        old = out.get(key)
        if old is None or not prefer_higher or new["confidence"] > old["confidence"]:
            out[key] = new


DITTO = re.compile(r'^\s*(["“”„]+|-+\s*"+\s*-*|do|-do-|ditto)\s*$', re.I)


def resolve_ditto(out, specs):
    """A ditto mark copies the value of the same column in the row above (same table)."""
    rows = {}
    for f in (F.field(s["key"]) for s in specs):
        if f and f.get("row") and f.get("col"):
            rows.setdefault((f["section"], f["col"]), []).append(f["key"])
    for keys in rows.values():
        prev = None
        for k in keys:
            item = out.get(k)
            if item and DITTO.match(str(item["value"])) and prev:
                out[k] = dict(item, value=prev["value"], evidence_text=f'ditto ({item["value"]})')
            elif item and item.get("value") not in (None, ""):
                prev = out[k]


def _pages_for(fields_out, keys, n_pages):
    pages = set()
    for k in keys:
        m = re.search(r"(\d+)", str((fields_out.get(k) or {}).get("source") or ""))
        if m and 1 <= int(m.group(1)) <= n_pages:
            pages.add(int(m.group(1)))
    return sorted(pages) or list(range(1, n_pages + 1))


# =============================================================================
# entry point
# =============================================================================
def read_document(path, doc_kind, conn=None, task_id=None, file_id=None, force_pro=False, test_codes=None,
                  only_keys=None):
    ext = os.path.splitext(path)[1].lower()
    if ext not in ACCEPTED:
        raise client.AIError(f"File type {ext} is not supported (PDF, JPG, PNG, XLSX, XLS or CSV).")
    if doc_kind == "logsheet:auto":
        return read_multi_logsheet(path, conn, task_id, file_id, force_pro, test_codes)
    specs = field_specs(doc_kind)
    if only_keys:
        specs = [s for s in specs if s["key"] in only_keys]
    test_codes = test_codes or list(F.tests())
    out, drawings, tests, viewer = {}, [], [], {"pages": [], "html": ""}
    meta = {"model": None, "calls": 0, "prompt_version": None, "method": "deterministic", "ai_skipped": None}

    def step(text):
        if conn is not None:
            client.progress(conn, task_id, text)

    parts, n_pages, sheets = [], 0, None
    if ext in IMAGE_EXT:
        step("Reading document - preparing pages")
        pages, viewer["pages"] = render_pages(path)
        n_pages = len(pages)
        if ext == ".pdf":
            out.update(deterministic_pdf(path, specs))
        parts = pages
    else:
        sheets = load_sheets(path)
        viewer["html"] = sheets_html(sheets)
        det, recognised = deterministic_excel(path, doc_kind, test_codes)
        out.update(det)
        if recognised:
            meta["method"] = "template"
        parts = None

    remaining = [s for s in specs if s["key"] not in out]
    # our own template is read deterministically; whatever it leaves empty stays empty (no AI call)
    need_ai = bool(remaining) and meta["method"] != "template"
    if need_ai and not client.available():
        meta["ai_skipped"] = client.disabled_reason()
        need_ai = False
    if need_ai:
        system, meta["prompt_version"] = prompt(doc_kind, remaining)
        schema = RequestExtraction if doc_kind == "request_form" else Extraction
        model = "pro" if force_pro else "fast"
        if parts is not None:
            for i in range(n_pages):
                step(f"Reading document - page {i + 1} of {n_pages}")
            payload = [f"The document has {n_pages} page(s). The pages follow in order (pdf_page 1, 2, ...)."] + parts
        else:
            step("Reading document - analysing the spreadsheet")
            payload = ["Spreadsheet content (cell address: value):\n" + grid_text(sheets)]
        step("Reading document - extracting fields")
        data, m = client.extract_json(payload, schema, system, model, meta["prompt_version"], conn, task_id,
                                      doc_kind, file_id)
        meta.update(model=m["model"], calls=1, method="ai" if not out else "mixed")
        _apply(data.get("fields"), remaining, out)
        if doc_kind == "request_form":
            drawings = [{"number": d.get("number", ""), "sheet": d.get("sheet") or "", "rev": d.get("rev") or "",
                         "check": float(d.get("confidence") or 0) < CHECK_BELOW} for d in data.get("drawings") or []
                        if d.get("number")]
            valid = set(F.tests())
            tests = sorted({t["code"] for t in data.get("tests") or []
                            if t.get("code") in valid and not t.get("struck_out")})
            meta["test_lines"] = [{"text": t.get("line_text"), "struck_out": bool(t.get("struck_out")),
                                   "code": t.get("code")} for t in data.get("tests") or []]
        # the requested-test list decides what is tested: it is always confirmed with the Pro model, which
        # separates real strike-throughs from the form's ruled lines far more reliably than Flash
        if doc_kind == "request_form" and parts is not None and not force_pro:
            step("Reading document - confirming the requested tests")
            sys_t, _ = prompt(doc_kind, [])
            data_t, _m = client.extract_json(payload + ["Return only the `tests` list (every numbered test line)."],
                                             TestsOnly, sys_t, "pro", meta["prompt_version"], conn, task_id,
                                             "request_form_tests", file_id)
            meta["calls"] += 1
            if data_t.get("tests"):
                valid = set(F.tests())
                tests = sorted({t["code"] for t in data_t["tests"] if t.get("code") in valid and not t.get("struck_out")})
                meta["test_lines"] = [{"text": t.get("line_text"), "struck_out": bool(t.get("struck_out")),
                                       "code": t.get("code")} for t in data_t["tests"]]
        # low-confidence retry on the Pro model with the relevant pages only
        req = [s["key"] for s in specs if s["required"]] or [s["key"] for s in specs]
        # only fields that were read with low confidence (fields absent from this document are not retried)
        weak = [k for k in req if k in out and out[k]["method"] == "ai" and out[k]["confidence"] < CHECK_BELOW]
        read_req = [k for k in req if k in out] or req
        if not force_pro and weak and len(weak) / len(read_req) > 0.2:
            step("Reading document - re-reading uncertain fields with the higher-accuracy model")
            wspecs = [s for s in specs if s["key"] in weak]
            system2, _ = prompt(doc_kind, wspecs)
            if parts is not None:
                pages = _pages_for(out, weak, n_pages)
                payload2 = [f"Pages {', '.join(map(str, pages))} of the document (numbered as pdf_page)."] + \
                           [parts[p - 1] for p in pages]
            else:
                payload2 = payload
            data2, m2 = client.extract_json(payload2, Extraction, system2, "pro", meta["prompt_version"], conn,
                                            task_id, doc_kind, file_id)
            _apply(data2.get("fields"), wspecs, out)
            meta["calls"] += 1
            meta["retry_model"] = m2["model"]
    if doc_kind.startswith("logsheet:"):
        resolve_ditto(out, specs)
    if doc_kind == "request_form":
        split_rating(out)
        if sheets is not None and meta["method"] == "template":
            dets = excel_io.parse_request_workbook(path)
            drawings = [{"number": dets[f"drawing{i}_number"], "sheet": dets.get(f"drawing{i}_sheet", ""),
                         "rev": dets.get(f"drawing{i}_rev", ""), "check": False}
                        for i in range(1, 6) if dets.get(f"drawing{i}_number")]
            tests = [c for c in F.tests() if str(dets.get(f"test_{c}", "")).lower() in ("yes", "y", "1", "true", "x")]
            for k in list(out):
                if k.startswith(("drawing", "test_")):
                    out.pop(k)
    keys = [s["key"] for s in specs]
    required = [s["key"] for s in specs if s["required"]] or keys
    summary = {"read": sum(1 for k in keys if k in out), "check": sum(1 for k in keys if out.get(k, {}).get("check")),
               "not_found": sum(1 for k in required if k not in out), "total": len(keys)}
    return {"kind": doc_kind, "doc_kind": doc_kind, "fields": out, "drawings": drawings, "tests": tests,
            "pages": viewer["pages"], "html": viewer["html"], "summary": summary, "file_id": file_id, **meta}


def read_multi_logsheet(path, conn=None, task_id=None, file_id=None, force_pro=False, test_codes=None):
    """One PDF / workbook holding several logsheets: classify each part, then extract each with its schema."""
    ext = os.path.splitext(path)[1].lower()
    if not client.available():
        raise client.AIError(client.disabled_reason())
    with open(os.path.join(PROMPT_DIR, "logsheet_classify.md"), encoding="utf-8") as fh:
        text = fh.read()
    lines = [text.split("\n", 1)[1].strip(), ""]
    lines += [f"  {code}: {s['logsheet'] or s['title']}" for code, s in F.stations().items()]
    system = "\n".join(lines)
    if ext in IMAGE_EXT:
        pages, urls = render_pages(path)
        payload = [f"{len(pages)} page(s) follow, part 1 = first page."] + pages
    else:
        sheets = load_sheets(path)
        payload = ["Sheets in order, part 1 = first sheet:\n" + "\n".join(
            f"part {i + 1}: sheet '{n}'\n" + grid_text([(n, c, m)])[:4000] for i, (n, c, m) in enumerate(sheets))]
    client.progress(conn, task_id, "Reading document - identifying the logsheets")
    data, _ = client.extract_json(payload, Classification, system, "fast", "logsheet_classify", conn, task_id,
                                  "logsheet_classify", file_id)
    groups = {}
    for p in data.get("parts") or []:
        if p.get("logsheet") in F.stations():
            groups.setdefault(p["logsheet"], []).append(int(p["part"]))
    results = {}
    for st, idx in groups.items():
        if ext in IMAGE_EXT:
            import pymupdf
            src = pymupdf.open(path) if ext == ".pdf" else None
            sub = os.path.join(PAGES_DIR, f"part_{hashlib.sha1((path + st).encode()).hexdigest()[:10]}.pdf")
            if src is not None:
                doc = pymupdf.open()
                for i in idx:
                    doc.insert_pdf(src, from_page=i - 1, to_page=i - 1)
                doc.save(sub)
                results[st] = read_document(sub, f"logsheet:{st}", conn, task_id, file_id, force_pro, test_codes)
        else:
            results[st] = read_document(path, f"logsheet:{st}", conn, task_id, file_id, force_pro, test_codes)
    return {"kind": "logsheet:auto", "doc_kind": "logsheet:auto", "parts": results,
            "classification": groups, "file_id": file_id,
            "summary": {st: r["summary"] for st, r in results.items()}}
