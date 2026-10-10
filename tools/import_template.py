"""
Build a report template version through the same pipeline the Admin portal uses.

    python tools/import_template.py                         # v1 from reference/Transformer_Test_report_format_1.docx
    python tools/import_template.py --source my.docx --out report_templates/v2 --previous report_templates/v1

Steps: parse the DOCX -> auto-map slots to the field registry -> apply the reviewer's decisions
(review.yaml in the output folder) -> compile to a docxtpl template -> dry run with the demo
transformer -> write source.docx, mapping.json, compiled.docx and dry_run.docx.
"""
import argparse
import json
import os
import shutil
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import db  # noqa: E402
from app.reporting import templates  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=os.path.join(ROOT, "reference", "Transformer_Test_report_format_1.docx"))
    ap.add_argument("--out", default=os.path.join(ROOT, "report_templates", "v1"))
    ap.add_argument("--previous", help="folder of the template version this one replaces (for fingerprint transfer)")
    a = ap.parse_args()
    db.init_db()
    os.makedirs(a.out, exist_ok=True)
    source = os.path.join(a.out, "source.docx")
    if os.path.abspath(a.source) != os.path.abspath(source):
        shutil.copyfile(a.source, source)
    review_path = os.path.join(a.out, "review.yaml")
    review = yaml.safe_load(open(review_path, encoding="utf-8")) if os.path.exists(review_path) else None
    previous = None
    if a.previous:
        with open(os.path.join(a.previous, "mapping.json"), encoding="utf-8") as fh:
            previous = json.load(fh)
    parsed, mapping = templates.build(source, previous, review)
    auto = mapping["auto_stats"]
    print(f"slots found: {auto['slots']}  required: {auto['required']}")
    print(f"auto-mapped (no review): {auto['mapped_required']}/{auto['required']} required = {auto['coverage']:.1%}  "
          f"methods {auto['methods']}")
    st = mapping["stats"]
    print(f"after review:            {st['mapped_required']}/{st['required']} required = {st['coverage']:.1%}")
    compiled = os.path.join(a.out, "compiled.docx")
    meta, problems, preview = templates.compile_and_check(source, mapping, compiled)
    shutil.move(preview, os.path.join(a.out, "dry_run.docx"))
    mapping["dry_run"] = {"unfilled": problems, "warnings": meta["warnings"]}
    with open(os.path.join(a.out, "mapping.json"), "w", encoding="utf-8") as fh:
        json.dump(mapping, fh, indent=1, ensure_ascii=False)
    print(f"sections: {len(meta['sections'])}  sheets: {meta['source_sheet_count']}  loops: {meta['loops']}")
    for w in meta["warnings"]:
        print("warning:", w)
    print("dry run unfilled:", problems or "none")
    left = templates.unmapped_required(parsed, mapping)
    if left:
        print("unmapped required slots:")
        for s in left:
            print("  ", s["id"], s["kind"], "|", s["context"].get("row", "")[:50], "|", s["context"].get("col", "")[:40])
    print("written", a.out)


if __name__ == "__main__":
    main()
