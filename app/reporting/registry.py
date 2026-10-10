"""
Value namespace used to fill a report template.

    field_values(snapshot)  -> F: {registry key: display string}  (readings, request, proforma, computed)
    system_values(...)      -> sys.* values (report no, dates, sheet counts, lists of numbers, names)
    lists(snapshot)         -> repeating-row data (voltage ratio taps, drawings)

Display strings use the registry decimals; missing values print as an em dash.
"""
from datetime import datetime

from .. import fields as F
from .. import numbering, rules

DASH = "—"
WORDS = rules.WORDS
ROUTINE = ["winding_res", "ratio", "impedance", "nll", "ir", "induced", "sep_source", "pressure_routine", "oil_leak"]

# Repeating-row lists the compiler can bind a table row to. `aliases` help the auto-mapper
# match table headers to item attributes.
LISTS = {
    "ratio_rows": {
        "label": "Voltage ratio per tap position",
        "columns": {
            "tap": ["Tap switch position", "Tap position", "Tap"],
            "pct": ["Tapping %", "Tapping percent", "Tap %"],
            "bu": ["Before SCW test U", "Before U"], "bv": ["Before SCW test V", "Before V"],
            "bw": ["Before SCW test W", "Before W"], "au": ["After SCW test U", "After U"],
            "av": ["After SCW test V", "After V"], "aw": ["After SCW test W", "After W"],
        },
    },
    "drawings": {
        "label": "Drawings submitted by the manufacturer",
        "columns": {
            "sl": ["Sl. No.", "Serial No", "S. No."], "number": ["Drawing Number", "Drawing No"],
            "sheet": ["Sheet Number", "Sheet No"], "rev": ["Revision Number", "Revision", "Rev"],
        },
    },
}


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


def words(n):
    return WORDS[n] if 0 <= n < len(WORDS) else str(n)


def evaluate(snapshot):
    d = snapshot["data"]
    return rules.evaluate(d["readings"], d["form"], d["tests"], d["settings"])


def field_values(snapshot, results=None, calc=None):
    d = snapshot["data"]
    form, rd = d["form"], d["readings"]
    if results is None:
        results, calc = evaluate(snapshot)
    out = {}
    for f in F.all_fields():
        k, src = f["key"], f["source"]
        if src == "reading":
            v = rd.get(k)
            out[k] = DASH if v in (None, "") else F.fmt(v, f["dp"])
        elif src in ("request", "proforma"):
            v = form.get(k)
            if isinstance(v, list):
                continue
            out[k] = DASH if v in (None, "") else str(v)
        elif src == "computed":
            v = calc.get(k)
            if v is None or v == "":
                out[k] = DASH
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = F.fmt(v, f["dp"]) if f["dp"] is not None else f"{v:g}"
            else:
                out[k] = str(v)
    return out, results, calc


def lists(snapshot):
    d = snapshot["data"]
    form, rd = d["form"], d["readings"]
    rows = []
    for i, pct in enumerate(rules.tap_percentages(form), start=1):
        r = {"tap": str(i), "pct": f"{pct:+.1f}".replace("+0.0", "0.0")}
        for side, pre in (("bt", "b"), ("at", "a")):
            for p in ("u", "v", "w"):
                v = rd.get(f"ratio.t{i}.{side}.{p}")
                r[pre + p] = DASH if v in (None, "") else F.fmt(v, 2)
        rows.append(r)
    drawings = [{"sl": str(i), "number": dr.get("number", ""), "sheet": dr.get("sheet", ""), "rev": dr.get("rev", "")}
                for i, dr in enumerate(form.get("drawings") or [], start=1)]
    return {"ratio_rows": rows, "drawings": drawings or [{"sl": "1", "number": "Nil", "sheet": "-", "rev": "-"}]}


def system_values(snapshot, issue, n_sheets, test_sheets):
    """sys.* values. `test_sheets` = sorted printed sheet numbers holding test results."""
    d = snapshot["data"]
    form, rd, tests, st = d["form"], d["readings"], set(d["tests"]), d["settings"]
    issue = issue or {}
    report_no = d["job"]["report_no"] or "PREVIEW"
    base = numbering.base_report_no(report_no)
    engineers = d["engineers"]
    resp = next((e for e in engineers if e["role"] == "responsible"), engineers[0] if engineers else None)
    dates = []
    for key in ("losses.hdr.date", "losses.hdr.date_at", "routine.hdr.date", "sc.hdr.date", "temprise.hdr.date",
                "pressure.hdr.date"):
        try:
            dates.append(datetime.strptime(rd.get(key) or "", "%d-%m-%Y"))
        except ValueError:
            pass
    test_dates = (DASH if not dates else dates[0].strftime("%d-%m-%Y") if min(dates) == max(dates)
                  else f"{min(dates).strftime('%d-%m-%Y')} to {max(dates).strftime('%d-%m-%Y')}")
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
    sc = "sc_withstand" in tests
    osc = [numbering.oscillogram_no(report_no, n) for n in range(2, 12)] if sc else []
    graphs = [numbering.graph_no(report_no, n) for n in (1, 2)] if "temp_rise" in tests else []
    photos = [numbering.photo_no(report_no, n) for n in range(1, 5)] if sc else []
    n_draw = len(form.get("drawings") or [])
    taps = rules.tap_percentages(form)
    principal = form.get("pf_principal_tap") or (taps.index(0.0) + 1 if 0.0 in taps else 1)
    wi = d["job"].get("wi") or {}
    deviations = wi.get("deviation") or "Nil"
    if d["job"].get("amendment_no"):
        deviations = (f"Amendment A{d['job']['amendment_no']} - supersedes Test Report No. {base}"
                      + (f" ({wi.get('amendment_reason')})" if wi.get("amendment_reason") else ""))
    ulr = numbering.ulr_no(st, report_no) if d["job"]["report_no"] else "ULR-PREVIEW-F"
    return {
        "sys.report_no": report_no, "sys.base_no": base,
        "sys.report_date": issue.get("report_date") or datetime.now().strftime("%d-%m-%Y"),
        "sys.ulr_base": ulr[:-1], "sys.seal12": (issue.get("seal") or "PREVIEW")[:12],
        "sys.engineer_name": resp["full_name"] if resp else DASH,
        "sys.reviewer_name": issue.get("reviewer_name") or DASH,
        "sys.sample_code": d["job"]["sample_code"] or DASH, "sys.test_dates": test_dates,
        "sys.tests_particulars": join_and(parts), "sys.standard": st["standard_text"],
        "sys.sc_standard": st["sc_standard_text"], "sys.deviations": deviations,
        "sys.n_sheets": str(n_sheets), "sys.n_sheets_words": f"{words(n_sheets)} ({n_sheets})",
        "sys.n_osc_words": words(len(osc)), "sys.n_graph_words": words(len(graphs)),
        "sys.n_photo_words": words(len(photos)), "sys.n_circuit_words": words(1 if sc else 0),
        "sys.n_drawing_words": words(n_draw), "sys.osc_list": join_and(osc), "sys.graph_list": join_and(graphs),
        "sys.photo_list": join_and(photos), "sys.circuit_list": st["circuit_diagram_no"] if sc else "Nil",
        "sys.circuit_no": st["circuit_diagram_no"],
        "sys.routine_heading": "All Routine Tests" if all_routine else "Routine Tests",
        "sys.tap_principal": str(int(float(principal))), "sys.tap_highest": "1", "sys.tap_lowest": str(len(taps)),
        "sys.first_test_sheet": str(test_sheets[0]) if test_sheets else "-",
        "sys.last_test_sheet": str(test_sheets[-1]) if test_sheets else "-",
        "_groups": groups,
    }
