"""
Accuracy of automatic document reading on the reference documents.

    python tools/ai_accuracy.py          (needs GEMINI_API_KEY in .env)

Runs read_document() on the scanned CPRI request form, proforma, routine logsheet, short-circuit logsheet
and the foreign-layout Excel workbook, compares with the expected values, prints a table and writes
docs/ai_accuracy.md.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(ROOT, ".env"))

import yaml  # noqa: E402

from app import db  # noqa: E402
from app.ai import client, intake  # noqa: E402

DEMO = yaml.safe_load(open(os.path.join(ROOT, "sample_data", "demo_values.yaml"), encoding="utf-8"))
REF = os.path.join(ROOT, "reference")


def contains(*words):
    return lambda v: v is not None and all(w.lower().replace(" ", "") in str(v).lower().replace(" ", "") for w in words)


def number(x):
    def chk(v):
        try:
            return v is not None and abs(float(str(v)) - float(x)) < 1e-9
        except ValueError:
            return False
    return chk


def exact(x):
    return lambda v: v is not None and str(v).strip().lower() == str(x).lower()


CASES = [
    ("Customer request form (scanned)", "Customer_request_form.pdf", "request_form", {
        "customer_name": contains("A.P. Transformers"), "customer_address": contains("Chennai"),
        "samples": contains("distribution transformer"), "rating": contains("250", "433"),
        "pf_kva": number(250), "pf_hv_v": number(11000), "pf_lv_v": number(433), "serial_numbers": exact("1098"),
        "num_samples": number(1), "witness_customer": contains("Vivekanand Krishna"), "criteria": contains("as per standard"),
        "take_back": exact("Yes"), "soc_required": exact("Yes"), "decision_rule": exact("(i)"), "msme": exact("Yes"),
        "@drawings": {"APT/250-11/001", "APT/250-11/001B"},
    }),
    ("Proforma for transformers (scanned)", "Proforma_for_transformers.pdf", "request_form", {
        "pf_vector_group": exact("Dyn11"), "pf_cooling": exact("ONAN"), "pf_freq": number(50), "pf_impedance": number(4.5),
        "pf_um": number(12), "pf_max_tl50": number(980), "pf_max_tl100": number(2930), "pf_oil_volume": number(460),
        "pf_mfg": exact("10/2025"), "pf_sealing": exact("Non-sealed"), "pf_insulation": exact("Oil immersed"),
        "pf_location": exact("Outdoor"), "pf_winding_material": exact("Copper"), "pf_coil_shape": exact("Circular"),
        "pf_core": exact("CRGO"), "@tests": {"sc_withstand", "temp_rise", "pressure_type", "nlc_112", "oil_leak"},
    }),
    ("Routine test logsheet (scanned)", "Logsheet_for_routine_test_no_load_current_at_112.5_%.pdf", "logsheet:ratio",
     {k: number(v) for k, v in DEMO.items() if k.startswith(("ir.", "ratio.")) or k in ("iov.bt.v", "iov.at.v", "iov.bt.a",
                                                                                            "iov.at.a", "iov.bt.hz", "iov.bt.s")}),
    ("Short-circuit logsheet (scanned)", "Logsheet_for_short_circuit.pdf", "logsheet:sc_withstand",
     {k: number(v) for k, v in DEMO.items() if k.startswith("sc.") and k.rsplit(".", 1)[1] in ("pk", "rms_avg", "dur")
      or k.startswith("sc.req.")}),
]


def foreign_case():
    exp = yaml.safe_load(open(os.path.join(ROOT, "sample_data", "foreign_layout_losses.expected.yaml"), encoding="utf-8"))
    return ("Losses workbook, foreign layout (Excel)", os.path.join(ROOT, "sample_data", "foreign_layout_losses.xlsx"),
            "logsheet:impedance", {k: ("cell", e["cell"], number(e["value"])) for k, e in exp.items()})


def evaluate(res, expected):
    rows, ok = [], 0
    for key, chk in expected.items():
        if key == "@drawings":
            got = {d["number"] for d in res.get("drawings", [])}
            good = chk <= got
            rows.append((key, ", ".join(sorted(chk)), ", ".join(sorted(got)), "", good))
        elif key == "@tests":
            got = set(res.get("tests", []))
            good = got == chk
            rows.append((key, ", ".join(sorted(chk)), ", ".join(sorted(got)), "", good))
        else:
            item = res["fields"].get(key) or {}
            if isinstance(chk, tuple):                     # value + source cell
                good = chk[2](item.get("value")) and item.get("source") == chk[1]
                rows.append((key, chk[1], f"{item.get('value')} @ {item.get('source')}", item.get("confidence"), good))
            else:
                good = chk(item.get("value"))
                rows.append((key, "", item.get("value"), item.get("confidence"), good))
        ok += good
    return ok, rows


def main():
    print(client.status_line())
    if not client.available() or client.connect()["client"] is None:
        return 1
    conn = db.connect()
    db.init_db_conn(conn)
    lines = ["# Automatic reading - accuracy on the reference documents", "",
             f"Generated {time.strftime('%d-%m-%Y %H:%M')} with {client.status_line()[8:]}.", "",
             "| Document | Correct | Total | Accuracy | Time |", "|---|---|---|---|---|"]
    details = []
    total_ok = total_n = 0
    for name, fname, kind, expected in CASES + [foreign_case()]:
        path = fname if os.path.isabs(fname) else os.path.join(REF, fname)
        t = time.time()
        res = intake.read_document(path, kind, conn)
        secs = time.time() - t
        ok, rows = evaluate(res, expected)
        total_ok += ok
        total_n += len(expected)
        print(f"\n{name}: {ok}/{len(expected)} correct ({ok / len(expected):.0%}) in {secs:.0f} s, method {res['method']}")
        for key, exp, got, conf, good in rows:
            if not good:
                print(f"   MISS {key:22} expected {exp!s:30} got {got!s:40} conf {conf}")
        lines.append(f"| {name} | {ok} | {len(expected)} | {ok / len(expected):.0%} | {secs:.0f} s |")
        details += ["", f"## {name}", "", "| Field | Read | Confidence | Result |", "|---|---|---|---|"]
        details += [f"| {k} | {str(g).replace('|', '/')} | {c if c is not None else ''} | {'correct' if gd else 'wrong'} |"
                    for k, _e, g, c, gd in rows]
    lines.append(f"| **All documents** | **{total_ok}** | **{total_n}** | **{total_ok / total_n:.0%}** | |")
    print(f"\nOverall: {total_ok}/{total_n} = {total_ok / total_n:.0%}")
    with open(os.path.join(ROOT, "docs", "ai_accuracy.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines + details) + "\n")
    print("written docs/ai_accuracy.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
