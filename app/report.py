"""
Report generation.

    build_snapshot(conn, job_id)  -> canonical dict of everything the report depends on
    build_context(snapshot)       -> Jinja context for templates/report_template.docx  (pure function)
    render_docx(snapshot, path)   -> filled DOCX
    docx_to_pdf(docx, pdf)        -> PDF via LibreOffice, else MS Word (COM), else None

Because the context is computed only from the frozen snapshot, an issued report
can always be reproduced and its hash re-verified.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

import qrcode
from docx.shared import Mm
from docxtpl import DocxTemplate, InlineImage, Listing

import config
from . import fields as F
from . import numbering, rules
from .db import get_settings

WORDS = ["Nil", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven",
         "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen", "Twenty"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
# original template sheet -> what decides whether it is printed
SHEET_RULES = {
    1: lambda t: True, 2: lambda t: True, 3: lambda t: True, 4: lambda t: True,
    5: lambda t: bool(t & {"winding_res", "ratio", "impedance", "nll"}),
    6: lambda t: bool(t & {"impedance", "nll", "ir", "induced", "sep_source"}),
    7: lambda t: "sc_withstand" in t,
    8: lambda t: bool(t & {"sc_withstand", "oil_leak", "pressure_routine"}),
    9: lambda t: "temp_rise" in t,
    10: lambda t: True,          # type pressure / NLC 112.5 % + conclusion
    11: lambda t: True,          # notes
}
TEST_SHEETS = {"sc_withstand": [7, 8], "nlc_112": [10], "temp_rise": [9], "pressure_type": [10], "winding_res": [5],
               "ratio": [5], "impedance": [5], "nll": [5], "ir": [6], "induced": [6], "sep_source": [6],
               "pressure_routine": [8], "oil_leak": [8]}
ROUTINE = ["winding_res", "ratio", "impedance", "nll", "ir", "induced", "sep_source", "pressure_routine", "oil_leak"]


def words(n):
    return WORDS[n] if 0 <= n < len(WORDS) else str(n)


def ddmmyyyy(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso[:19]).strftime("%d-%m-%Y")
    except ValueError:
        return iso


def join_and(items):
    items = [i for i in items if i]
    if not items:
        return "Nil"
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " & " + items[-1]


# =============================================================================
# Snapshot
# =============================================================================
def build_snapshot(conn, job_id, for_verify=False):
    """Everything the report depends on, as plain JSON-able data (deterministic order)."""
    job = conn.execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()
    cust = conn.execute("SELECT full_name, company, username FROM users WHERE id=?", (job["customer_id"],)).fetchone()
    readings = conn.execute("SELECT key, station, imported_value, current_value, unit, source_cell, source_file_id "
                            "FROM readings WHERE job_id=? ORDER BY key", (job_id,)).fetchall()
    results = conn.execute("SELECT station, rule_code, label, value, limit_text, formula, passed FROM test_results "
                           "WHERE job_id=? ORDER BY station, rule_code", (job_id,)).fetchall()
    corrections = conn.execute("SELECT c.key, c.old_value, c.new_value, c.reason, c.ts, u.username FROM corrections c "
                               "LEFT JOIN users u ON u.id=c.user_id WHERE c.job_id=? ORDER BY c.id", (job_id,)).fetchall()
    engineers = conn.execute("SELECT u.full_name, u.username, je.role, je.station FROM job_engineers je "
                             "JOIN users u ON u.id=je.user_id WHERE je.job_id=? ORDER BY je.role DESC, u.full_name",
                             (job_id,)).fetchall()
    files = conn.execute("SELECT id, kind, station, original_name, sha256 FROM files WHERE job_id=? ORDER BY id",
                         (job_id,)).fetchall()
    settings = get_settings(conn)
    data = {
        "job": {"id": job["id"], "sample_code": job["sample_code"], "report_no": job["report_no"],
                "amends_id": job["amends_id"], "amendment_no": job["amendment_no"],
                "accepted_at": job["accepted_at"], "created_at": job["created_at"],
                "customer": {"name": cust["full_name"], "company": cust["company"], "username": cust["username"]},
                "wi": json.loads(job["wi_json"] or "{}"), "return_comment": job["return_comment"]},
        "form": json.loads(job["form_json"]),
        "tests": sorted(json.loads(job["tests_json"])),
        "readings": {r["key"]: r["current_value"] for r in readings},
        "readings_meta": {r["key"]: {"imported": r["imported_value"], "cell": r["source_cell"],
                                     "file_id": r["source_file_id"], "unit": r["unit"]} for r in readings},
        "results": [dict(r) for r in results],
        "corrections": [dict(c) for c in corrections],
        "engineers": [dict(e) for e in engineers],
        "files": [dict(f) for f in files],
        "settings": settings,
    }
    return {"schema": 1, "data": data}


# =============================================================================
# Context
# =============================================================================
def build_context(snapshot, issue=None):
    """
    Jinja context for the report template. `issue` = {"report_date", "reviewer_name", "seal"} -
    for previews it is filled with "PREVIEW" values.
    """
    d = snapshot["data"]
    form, rd, tests = d["form"], d["readings"], set(d["tests"])
    st = d["settings"]
    issue = issue or {}
    results, calc = rules.evaluate(rd, form, tests, st)

    def v(key, dp="field"):
        f = F.field(key)
        val = rd.get(key)
        if val in (None, ""):
            return "—"
        return F.fmt(val, f["dp"] if (f and dp == "field") else (None if dp == "field" else dp))

    def num(x, dp):
        return "—" if x is None else f"{x:.{dp}f}"

    def ok(code):
        return rules.test_ok(results, code)

    def obs(code, good="Within limits", bad="Not within limits"):
        return good if ok(code) else bad

    def rule(code):
        return next((r for r in results if r["rule_code"] == code), None)

    report_no = d["job"]["report_no"] or "PREVIEW"
    base_no = numbering.base_report_no(report_no)
    engineers = d["engineers"]
    resp = next((e for e in engineers if e["role"] == "responsible"), engineers[0] if engineers else None)

    # ---- sheet numbering (only sheets with requested tests are printed)
    shown = [s for s in range(1, 12) if SHEET_RULES[s](tests)]
    sh = {f"s{s}": (shown.index(s) + 1 if s in shown else "-") for s in range(1, 12)}
    test_sheet_nums = sorted({sh[f"s{s}"] for c in tests for s in TEST_SHEETS.get(c, []) if s in shown})
    n_sheets = len(shown)

    # ---- declared data (sheet 2)
    kva, hv, lv = form.get("pf_kva", ""), form.get("pf_hv_v", ""), form.get("pf_lv_v", "")
    phases = "Three" if str(form.get("pf_phases", "3")) == "3" else "Single"
    samples = form.get("samples", "Distribution Transformer")
    sample_name = "Distribution Transformer" if "distribution" in samples.lower() else samples
    mfg = form.get("pf_mfg", "")
    try:
        mm, yy = mfg.split("/")
        mfg_text = f"{MONTHS[int(mm) - 1]} {yy}"
    except (ValueError, IndexError):
        mfg_text = mfg
    mat = form.get("pf_winding_material", "Copper")
    taps = rules.tap_percentages(form)
    n_taps = len(taps)
    principal = int(float(form.get("pf_principal_tap") or (taps.index(0.0) + 1 if 0.0 in taps else 1)))
    tap_txt = (f"+{float(form.get('pf_tap_plus') or 0):g} % to -{float(form.get('pf_tap_minus') or 0):g} % "
               f"in steps of {float(form.get('pf_tap_step') or 2.5):g} % ({n_taps} positions), off-circuit")
    sp = {
        "test_sample": f"{phases} Phase {sample_name}",
        "type": form.get("sample_type") or ", ".join(x for x in (form.get("pf_location"), form.get("pf_sealing"),
                                                                    form.get("pf_insulation")) if x),
        "serial": form.get("serial_numbers", ""),
        "rated_power": f"{kva} kVA",
        "rated_voltage": f"{hv} V / {lv} V",
        "um": f"{form.get('pf_um', '')} kV",
        "hv_ins": f"{form.get('pf_hv_li', '-')} kVp / {form.get('pf_hv_ac', '-')} kVrms",
        "lv_ins": f"{form.get('pf_lv_li', '-')} / {form.get('pf_lv_ac', '-')} kVrms",
        "impedance": f"{float(form.get('pf_impedance') or 0):.2f} %",
        "phases": phases,
        "freq": f"{form.get('pf_freq', '50')} Hz",
        "vector_group": form.get("pf_vector_group", ""),
        "tappings": tap_txt,
        "eel": f"Energy Efficiency Level - {form.get('pf_eel', '').replace('EEL-', '')}",
        "cooling": form.get("pf_cooling", ""),
        "max_tl50": f"{form.get('pf_max_tl50', '')} W",
        "max_tl100": f"{form.get('pf_max_tl100', '')} W",
        "coil": f"{mat}, {form.get('pf_coil_shape', '')}",
        "core": form.get("pf_core", ""),
        "mfg": mfg_text,
        "oil": f"{form.get('pf_oil_volume', '')} litres",
        "lv_winding": Listing(f"Type          : {form.get('pf_lv_winding', '')}\nConductor : {mat}"),
        "hv_winding": Listing(f"Type          : {form.get('pf_hv_winding', '')}\nConductor : {mat}"),
    }

    # ---- test-dates from the logsheet headers
    dates = []
    for key in ("losses.hdr.date", "losses.hdr.date_at", "routine.hdr.date", "sc.hdr.date", "temprise.hdr.date",
                "pressure.hdr.date"):
        val = rd.get(key)
        if val:
            try:
                dates.append(datetime.strptime(val, "%d-%m-%Y"))
            except ValueError:
                pass
    test_dates = ("—" if not dates else dates[0].strftime("%d-%m-%Y") if min(dates) == max(dates)
                  else f"{min(dates).strftime('%d-%m-%Y')} to {max(dates).strftime('%d-%m-%Y')}")

    # ---- particulars of tests
    groups = {"special": bool(tests & {"sc_withstand", "nlc_112"}), "type": bool(tests & {"temp_rise", "pressure_type"}),
              "routine": bool(tests & set(ROUTINE))}
    all_routine = set(ROUTINE) <= tests
    parts = []
    if groups["type"]:
        parts.append("Type Tests")
    if groups["routine"]:
        parts.append("All Routine Tests" if all_routine else "Routine Tests")
    if groups["special"]:
        parts.append("Special Test" + ("s" if len(tests & {"sc_withstand", "nlc_112"}) > 1 else ""))
    tests_particulars = join_and(parts)

    sc = "sc_withstand" in tests
    osc_nums = [numbering.oscillogram_no(report_no, n) for n in range(2, 12)] if sc else []
    graphs = [numbering.graph_no(report_no, n) for n in (1, 2)] if "temp_rise" in tests else []
    photos = [numbering.photo_no(report_no, n) for n in range(1, 5)] if sc else []
    drawings = [{"sl": i, "number": dr.get("number", ""), "sheet": dr.get("sheet", ""), "rev": dr.get("rev", "")}
                for i, dr in enumerate(form.get("drawings") or [], start=1)]

    # ---- sheet 5
    hv75, lv75 = calc.get("hv_r75", {}), calc.get("lv_r75", {})
    wr = {"hv": {s: {t: num(hv75.get(s, {}).get(t), 4) for t in ("n", "h", "l")} for s in ("bt", "at")},
          "lv": {s: num(lv75.get(s), 4) for s in ("bt", "at")}}
    ratio_rows = []
    for row in calc.get("ratio_rows", []):
        i = row["tap"]
        r = {"tap": i, "pct": row["pct"]}
        for side, pre in (("bt", "b"), ("at", "a")):
            for p in ("u", "v", "w"):
                r[pre + p] = v(f"ratio.t{i}.{side}.{p}")
        ratio_rows.append(r)
    imp = {"z": {}, "ll": {}, "ll50": {}}
    for side in ("bt", "at"):
        imp["z"][side] = {t: v(f"calc.{t}.{side}.z75") for t in ("nt", "ht", "lt")}
        imp["ll"][side] = {t: v(f"calc.{t}.{side}.ll100") for t in ("nt", "ht", "lt")}
        imp["ll50"][side] = v(f"calc.nt.{side}.ll50")
    nl = {s: {k: v(f"nll.{s}.{k}") for k in ("v", "f", "i", "p")} for s in ("bt", "at")}

    # ---- sheet 6
    eel = {"tl50": {s: v(f"calc.nt.{s}.tl50") for s in ("bt", "at")},
           "tl100": {s: v(f"calc.nt.{s}.tl100") for s in ("bt", "at")}}
    ir = {s: {k: (v(f"ir.{s}.{k}") + " GΩ") for k in ("hv_e", "lv_e", "hv_lv")} for s in ("bt", "at")}

    def withstood(code_prefix, side, fallback):
        r = rule(f"{code_prefix}_{side}")
        return "Withstood" if (r and r["passed"]) else (fallback or "—")

    iov = {s: Listing(f"{v(f'iov.{s}.v')} V, {v(f'iov.{s}.hz')} Hz for {v(f'iov.{s}.s')} s\n"
                      f"{withstood('induced', s, rd.get(f'iov.{s}.obs'))}") for s in ("bt", "at")}
    sep = {s: Listing(f"{v(f'hvpf.{s}.kv')} kV for {v(f'hvpf.{s}.s')} s\n{withstood('hvpf', s, rd.get(f'hvpf.{s}.obs'))}"
                      f"\n\n\n{v(f'lvpf.{s}.kv')} kV for {v(f'lvpf.{s}.s')} s\n{withstood('lvpf', s, rd.get(f'lvpf.{s}.obs'))}")
           for s in ("bt", "at")}

    # ---- sheets 7 / 8 short circuit
    sc_obs_ok = ok("sc_withstand")
    isc = {t: {"xr": v(f"calc.{t}.bt.xr"), "rms": v(f"calc.{t}.bt.iscrms"), "pk": v(f"calc.{t}.bt.iscpk")}
           for t in ("nt", "ht", "lt")}
    osc = {}
    for n in range(2, 12):
        sid = f"s{n:03d}"
        r = rule(f"sc_peak_{sid}") if n < 11 else rule("sc_thermal")
        osc[sid] = {"pk": v(f"sc.{sid}.pk"), "rms": v(f"sc.{sid}.rms_avg"), "dur": v(f"sc.{sid}.dur"),
                    "obs": "No abnormality" if (r is None or r["passed"]) and sc_obs_ok else "Refer remarks"}
    xr = {t: {"b": v(f"calc.{t}.bt.x50"), "a": v(f"calc.{t}.at.x50"), "chg": num(calc.get("xchg", {}).get(t), 3)}
          for t in ("nt", "ht", "lt")}
    xchg_ok = all(r["passed"] for r in results if r["rule_code"].startswith("xchg_") and r["passed"] is not None)
    sc_ctx = {"condition": "Transformer as after routine tests" if "routine" in (rd.get("sc.hdr.condition") or "routine").lower()
              else rd.get("sc.hdr.condition"),
              "insp_conductor": rd.get("sc.insp.conductor", "—"), "insp_spacers": rd.get("sc.insp.spacers", "—"),
              "insp_oil": rd.get("sc.insp.oil", "—")}

    def obs_text(key, code):
        txt = rd.get(key) or ""
        return txt if ok(code) and txt else (txt or "—") + ("" if ok(code) else " (not within limits)")

    ol = {"atm": v("pr.atm"), "amb": v("pr.amb"), "head": v("ol.head_kpa"), "test": v("ol.test_kpa"),
          "hours": v("ol.hours"), "obs": obs_text("ol.obs", "oil_leak")}
    prr = {"atm": v("pr.atm"), "amb": v("pr.amb"), "kpa": v("prr.kpa"), "min": v("prr.min"),
           "obs": obs_text("prr.obs", "pressure_routine")}

    # ---- sheet 9 temperature rise
    trc = calc.get("tr", {})
    tr = {"total_loss": v("tr.total_loss"), "rated_current": v("tr.rated_current"), "amb": v("tr.amb_shutdown", 1),
          "nll": v("tr.nll"), "fll": v("tr.fll")}
    for w, label_code in (("top", "tr_top_oil"), ("hv", "tr_hv"), ("lv", "tr_lv")):
        r = rule(label_code)
        tr[w] = {"rise": num(trc.get(w), 1),
                 "g": f"{(trc.get('g_oil') if w == 'top' else trc.get('g_w')) or 0:g}",
                 "std": f"{(trc.get('std_oil') if w == 'top' else trc.get('std_w')) or 0:g}",
                 "obs": "Within limits" if (r is None or r["passed"]) else "Not within limits"}

    # ---- sheet 10
    prt = {"atm": v("pr.atm"), "amb": v("pr.amb"), "val": v("prt.kpa"), "min": v("prt.min"), "defl": v("prt.perm_defl"),
           "obs": obs("pressure_type") if all(rule(c) is None or rule(c)["passed"] for c in ("prt_defl", "prt_obs")) else "Not within limits"}
    prv = {"val": v("prv.mmhg"), "min": v("prv.min"), "defl": v("prv.perm_defl"),
           "obs": "Within limits" if all(rule(c) is None or rule(c)["passed"] for c in ("prv_defl", "prv_obs")) else "Not within limits"}
    lv_v = float(lv or 0)
    nlc = {"v100": f"{lv_v:g}", "v112": f"{lv_v * 1.125:.0f}",
           "lim100": f"{float(st['nlc_limit_100_pct']):g} %", "lim112": f"{float(st['nlc_limit_112_pct']):g} %",
           "m100": num(calc.get("nlc100_pct_bt"), 2) + " %" if calc.get("nlc100_pct_bt") is not None else "—",
           "m112": num(calc.get("nlc112_pct"), 2) + " %" if calc.get("nlc112_pct") is not None else "—"}

    deviations = d["job"]["wi"].get("deviation") or "Nil"
    if d["job"]["amendment_no"]:
        deviations = (f"Amendment A{d['job']['amendment_no']} - supersedes Test Report No. {base_no}"
                      + (f" ({d['job']['wi'].get('amendment_reason')})" if d["job"]["wi"].get("amendment_reason") else ""))
    ulr = numbering.ulr_no(st, report_no) if d["job"]["report_no"] else "ULR-PREVIEW-F"
    return {
        "report_no": report_no, "base_no": base_no, "report_date": issue.get("report_date", ddmmyyyy(datetime.now().isoformat())),
        "ulr_base": ulr[:-1],
        "seal12": issue.get("seal", "PREVIEW")[:12],
        "engineer_name": resp["full_name"] if resp else "—",
        "reviewer_name": issue.get("reviewer_name", "—"),
        "customer_block": Listing(f"{form.get('customer_name', '')}\n{form.get('customer_address', '')}"),
        "manufacturer_block": Listing(form.get("manufacturer", "")),
        "particulars": Listing(f"{kva} kVA, {hv} / {lv} V, {phases} Phase\n{sample_name}"),
        "sp": sp,
        "n_samples_words": words(int(float(form.get("num_samples") or 1))),
        "test_dates": test_dates,
        "sample_code": d["job"]["sample_code"] or "—",
        "tests_particulars": tests_particulars,
        "standard": st["standard_text"],
        "sc_standard": st["sc_standard_text"],
        "customer_requirement": form.get("customer_requirement") or "Nil",
        "deviations": deviations,
        "witness_customer": form.get("witness_customer") or "None",
        "witness_other": form.get("witness_other") or "None",
        "n_sheets": n_sheets, "sh": sh,
        "first_test_sheet": test_sheet_nums[0] if test_sheet_nums else "-",
        "last_test_sheet": test_sheet_nums[-1] if test_sheet_nums else "-",
        "n_sheets_words": f"{words(n_sheets)} ({n_sheets})",
        "n_osc_words": words(len(osc_nums)), "n_graph_words": words(len(graphs)),
        "n_photo_words": words(len(photos)), "n_circuit_words": words(1 if sc else 0),
        "n_drawing_words": words(len(drawings)),
        "t": {c: (c in tests) for c in F.tests()}, "g": groups,
        "show": {**{f"s{s}": (s in shown) for s in range(1, 12)},
                 "eel": bool(tests & {"impedance", "nll"}),
                 "routine_s8": bool(tests & {"oil_leak", "pressure_routine"})},
        "routine_heading": "All Routine Tests" if all_routine else "Routine Tests",
        "osc_list": join_and(osc_nums), "graph_list": join_and(graphs), "photo_list": join_and(photos),
        "circuit_list": st["circuit_diagram_no"] if sc else "Nil", "circuit_no": st["circuit_diagram_no"],
        "drawings": drawings or [{"sl": 1, "number": "Nil", "sheet": "-", "rev": "-"}],
        "tapn": {"Principal": principal, "Highest": 1, "Lowest": n_taps},
        "wr": wr, "ratio_rows": ratio_rows, "vector_group": rd.get("vg.bt") or form.get("pf_vector_group", ""),
        "imp": imp, "nl": nl, "eel": eel, "ir": ir, "iov": iov, "sep": sep,
        "sc": sc_ctx, "isc": isc, "osc": osc, "xr": xr,
        "xr_conclusion": "Variation of reactance is within limits" if xchg_ok else "Variation of reactance is NOT within limits",
        "ol": ol, "prr": prr, "tr": tr, "prt": prt, "prv": prv, "nlc": nlc,
        "complies_text": "complies with" if not any(r["passed"] is False for r in results) else "does not comply with",
        "_results": results,
    }


# =============================================================================
# Rendering
# =============================================================================
def _qr_png(text):
    img = qrcode.make(text, box_size=4, border=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def render_docx(snapshot, out_path, issue=None, verify_url=None):
    ctx = build_context(snapshot, issue)
    ctx.pop("_results", None)
    tpl = DocxTemplate(config.REPORT_TEMPLATE)
    qr_text = (f"CPRI ATRG | Report {ctx['report_no']} | Seal {(issue or {}).get('seal', 'PREVIEW')[:16]}"
               + (f" | Verify: {verify_url}" if verify_url else ""))
    ctx["qr"] = InlineImage(tpl, _qr_png(qr_text), width=Mm(18))
    # the format's sheet 11 carries a static QR picture (word/media/image3.png) - swap in ours
    qr_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    qr_tmp.write(_qr_png(qr_text).getvalue())
    qr_tmp.close()
    tpl.replace_zipname("word/media/image3.png", qr_tmp.name)
    tpl.render(ctx, autoescape=True)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tpl.save(out_path)
    os.unlink(qr_tmp.name)
    return out_path


# =============================================================================
# PDF conversion (detected once at start-up)
# =============================================================================
_word_lock = threading.Lock()
_converter = None


def _find_soffice():
    for cand in ("soffice", "soffice.exe", r"C:\Program Files\LibreOffice\program\soffice.exe",
                 r"C:\Program Files (x86)\LibreOffice\program\soffice.exe", "/usr/bin/soffice",
                 "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        path = shutil.which(cand) or (cand if os.path.isfile(cand) else None)
        if path:
            return path
    return None


def _word_available():
    if sys.platform != "win32":
        return False
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application"))
        import win32com.client  # noqa: F401
        return True
    except Exception:
        return False


def detect_converter():
    """Return ('libreoffice', path) | ('word', None) | (None, None). Cached."""
    global _converter
    if _converter is None:
        so = _find_soffice()
        if so:
            _converter = ("libreoffice", so)
        elif _word_available():
            _converter = ("word", None)
        else:
            _converter = (None, None)
    return _converter


def _word_convert(docx_path, pdf_path):
    import pythoncom
    import win32com.client
    with _word_lock:           # Word automation is not thread-safe -> one conversion at a time
        pythoncom.CoInitialize()
        word = None
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            doc = word.Documents.Open(os.path.abspath(docx_path), ReadOnly=True, AddToRecentFiles=False,
                                      ConfirmConversions=False)
            doc.ExportAsFixedFormat(os.path.abspath(pdf_path), 17)   # 17 = wdExportFormatPDF
            doc.Close(0)
        finally:
            if word is not None:
                try:
                    word.Quit()
                except Exception:
                    pass
            pythoncom.CoUninitialize()


def docx_to_pdf(docx_path, pdf_path):
    """Convert; returns pdf_path or None if no converter works."""
    kind, path = detect_converter()
    try:
        if kind == "libreoffice":
            outdir = tempfile.mkdtemp()
            subprocess.run([path, "--headless", "--convert-to", "pdf", "--outdir", outdir, docx_path],
                           check=True, timeout=120, capture_output=True)
            produced = os.path.join(outdir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
            shutil.move(produced, pdf_path)
            return pdf_path
        if kind == "word":
            try:
                _word_convert(docx_path, pdf_path)
            except Exception:
                from docx2pdf import convert   # last resort
                convert(docx_path, pdf_path)
            return pdf_path if os.path.exists(pdf_path) else None
    except Exception as exc:  # pragma: no cover - environment dependent
        print("PDF conversion failed:", exc)
    return None
