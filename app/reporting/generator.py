"""
Generator: fills a compiled template with a frozen snapshot.

The Jinja context contains only plain values - every calculation happens in
Python (rules.py / registry.py) before rendering, so an admin-edited template
can never execute code.
"""
import io
import os
import tempfile

import qrcode
from docx.shared import Mm
from docxtpl import DocxTemplate, InlineImage, Listing
from simpleeval import EvalWithCompoundTypes

from .. import rules
from . import registry, sections


def _qr_png(text):
    img = qrcode.make(text, box_size=4, border=1)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def build_context(snapshot, mapping, issue=None):
    """Everything the compiled template needs, as plain dicts. Also returns rule results."""
    meta = mapping["compiled_meta"]
    data = snapshot["data"]
    tests = set(data["tests"])
    F, results, calc = registry.field_values(snapshot)
    show, sheet_flags, numbers, n_sheets, test_sheets = sections.visibility(meta, tests)
    all_test_sheets = sorted({n for v in test_sheets.values() for n in v})
    sysv = registry.system_values(snapshot, issue, n_sheets, all_test_sheets)
    groups = sysv.pop("_groups")
    F.update(sysv)
    sched = {code: "\n&\n".join(f"{n} of {n_sheets}" for n in test_sheets.get(code, [])) or "-"
             for code in meta.get("schedule_tests", [])}
    # expression slots: evaluated with the same safe evaluator and helpers as the rules
    X = {}
    if meta.get("expr_slots"):
        values = dict(data["readings"])
        values.update({k: v for k, v in calc.items() if isinstance(v, (int, float, str))})
        ctx = {"form": data["form"], "values": rules._typed_values(values), "K": rules.material_constant(data["form"]),
               "results": results, "results_by_id": {r["rule_code"]: r for r in results}}
        funcs = rules.make_functions(ctx)
        tree = rules._tree(ctx["values"])
        names = {k: (rules.Node(v, k + ".") if isinstance(v, dict) else v) for k, v in tree.items()}
        names.update({"form": rules.Node(data["form"], "form.", strict=False), "F": F})
        for sid, expr in meta["expr_slots"].items():
            try:
                X[sid] = str(EvalWithCompoundTypes(functions=funcs, names=names).eval(expr))
            except Exception:
                X[sid] = registry.DASH
    return {
        "F": F, "X": X, "L": registry.lists(snapshot), "T": {c: (c in tests) for c in meta.get("schedule_tests", [])},
        "G": groups, "SHOW": show, "SHEET": sheet_flags, "SH": numbers, "NSHEETS": n_sheets, "SCHED": sched,
    }, results


def _listing(ctx):
    """Multi-line strings become Listing objects so line breaks survive in Word."""
    for d in (ctx["F"], ctx["X"], ctx["SCHED"]):
        for k, v in list(d.items()):
            if isinstance(v, str) and "\n" in v:
                d[k] = Listing(v)
    for rows in ctx["L"].values():
        for row in rows:
            for k, v in list(row.items()):
                if isinstance(v, str) and "\n" in v:
                    row[k] = Listing(v)


def render(compiled_path, mapping, snapshot, out_path, issue=None, verify_url=None):
    ctx, _results = build_context(snapshot, mapping, issue)
    tpl = DocxTemplate(compiled_path)
    qr_text = (f"CPRI ATRG | Report {ctx['F']['sys.report_no']} | Seal {((issue or {}).get('seal') or 'PREVIEW')[:16]}"
               + (f" | Verify: {verify_url}" if verify_url else ""))
    ctx["F"]["sys.qr"] = InlineImage(tpl, _qr_png(qr_text), width=Mm(18))
    _listing(ctx)
    qr_tmp = None
    if mapping.get("qr_media"):
        qr_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        qr_tmp.write(_qr_png(qr_text).getvalue())
        qr_tmp.close()
        tpl.replace_zipname(mapping["qr_media"], qr_tmp.name)
    tpl.render(ctx, autoescape=True)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tpl.save(out_path)
    if qr_tmp:
        os.unlink(qr_tmp.name)
    return out_path


def unfilled(docx_path):
    """List leftover template syntax / dummy text in a generated DOCX (dry-run check)."""
    import re
    from docx import Document
    from docx.oxml.ns import qn
    doc = Document(docx_path)
    problems = []
    roots = [doc.element.body] + [s.footer._element for s in doc.sections] + [s.header._element for s in doc.sections]
    for root in roots:
        for p in root.iter(qn("w:p")):
            txt = "".join(t.text or "" for t in p.iter(qn("w:t")) if next(t.iterancestors(qn("w:p")), None) is p)
            if "{{" in txt or "{%" in txt or re.search(r"X{4,}", txt):
                problems.append(txt.strip()[:120])
    return problems
