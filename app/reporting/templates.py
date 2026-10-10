"""
Versioned report templates (table `report_templates`).

Life cycle:  draft  --(admin submits, HOD approves `template_activation`)-->  active  --(next version)-->  retired
A draft can be re-mapped and re-compiled freely; an active or retired template is frozen by a DB trigger,
so every issued report can be regenerated from exactly the template it recorded (id + version + SHA-256).
"""
import json
import os
import shutil

import yaml

import config
from .. import excel_io, integrity
from .. import fields as F
from ..db import now_ist
from . import automapper, compiler, docx_parser, generator

TEMPLATE_STORE = os.path.join(config.STORAGE_DIR, "templates")
REPO_TEMPLATES = os.path.join(config.BASE_DIR, "report_templates")
DEFAULT_EQUIPMENT = "Distribution Transformer, IS 1180"


def _abs(path):
    return path if os.path.isabs(path) else os.path.join(config.BASE_DIR, path)


def _rel(path):
    try:
        rel = os.path.relpath(path, config.BASE_DIR)
        return path if rel.startswith("..") else rel.replace("\\", "/")
    except ValueError:
        return path


def get(conn, template_id):
    return conn.execute("SELECT * FROM report_templates WHERE id=?", (template_id,)).fetchone()


def active(conn, equipment_type=DEFAULT_EQUIPMENT):
    return conn.execute("SELECT * FROM report_templates WHERE equipment_type=? AND status='active'",
                        (equipment_type,)).fetchone()


def mapping_of(rec):
    return json.loads(rec["mapping_json"])


# =============================================================================
# demo data for dry runs
# =============================================================================
def demo_snapshot(tests=None):
    """Snapshot of the demo transformer built from sample_data/ (used for dry runs and tests)."""
    from .. import request_form
    tests = list(tests or F.tests())
    rd = {}
    for st, meta in F.stations().items():
        path = os.path.join(config.SAMPLE_DATA_DIR, meta["file"])
        if os.path.exists(path):
            res = excel_io.import_workbook(path, st, tests)
            rd.update({k: v[0] for k, v in res.values.items()})
    for k, v in {"losses.hdr.date": "29-10-2025", "pressure.hdr.date": "14-11-2025"}.items():
        rd.setdefault(k, v)
    return {"schema": 2, "data": {
        "job": {"id": 0, "sample_code": "HVD25S0847", "report_no": "CPRIBLRSCL25T1654", "amends_id": None,
                "amendment_no": 0, "accepted_at": None, "created_at": None, "customer": {}, "wi": {}, "return_comment": None},
        "form": request_form.demo_form(), "tests": sorted(tests), "readings": rd, "readings_meta": {}, "results": [],
        "corrections": [], "engineers": [{"full_name": "P. Naveenkumar", "username": "engineer1", "role": "responsible",
                                          "station": ""}],
        "files": [], "settings": dict(config.DEFAULT_SETTINGS), "template": None}}


# =============================================================================
# building a mapping from a source DOCX
# =============================================================================
def apply_review(mapping, parsed, review):
    """Apply reviewer decisions (YAML / editor) on top of an auto-mapping."""
    ids = {s["id"]: s for s in parsed["slots"]}
    for sid, decision in (review.get("slots") or {}).items():
        if sid not in ids:
            continue
        if decision == "ignore":
            mapping["slots"][sid] = {"kind": "ignore", "method": "review", "confidence": 1.0, "fp": ids[sid]["fp"]}
        elif isinstance(decision, dict):
            mapping["slots"][sid] = dict(decision, method="review", confidence=1.0, fp=ids[sid]["fp"])
        else:
            mapping["slots"][sid] = {"kind": "field", "key": decision, "method": "review", "confidence": 1.0,
                                     "fp": ids[sid]["fp"]}
    for key in ("sections", "replacements", "keep_sheets_with", "qr_media", "sheet_marker"):
        if key in review:
            mapping[key] = review[key]
    mapping["stats"] = automapper.stats(parsed, mapping)
    return mapping


def build(source_path, previous_mapping=None, review=None, use_ai=False, ai_fn=None):
    parsed = docx_parser.parse(source_path, (previous_mapping or {}).get("sheet_marker", r"^TEST REPORT$")).to_json()
    mapping = automapper.automap(parsed, previous_mapping, use_ai=use_ai, ai_fn=ai_fn)
    auto_stats = dict(mapping["stats"])
    if review:
        mapping = apply_review(mapping, parsed, review)
    mapping["auto_stats"] = auto_stats
    return parsed, mapping


def compile_and_check(source_path, mapping, compiled_path, demo_tests=None):
    """Compile, then dry-run with the demo data. Returns (meta, unfilled_list, preview_docx)."""
    meta = compiler.compile_template(source_path, mapping, compiled_path)
    mapping["compiled_meta"] = meta
    preview = compiled_path.replace(".docx", "_dryrun.docx")
    generator.render(compiled_path, mapping, demo_snapshot(demo_tests), preview,
                     {"report_date": "18-11-2025", "reviewer_name": "HOD (dry run)", "seal": "DRYRUN" * 6})
    problems = generator.unfilled(preview)
    return meta, problems, preview


def unmapped_required(parsed, mapping):
    return [s for s in parsed["slots"] if s["required"] and s["id"] not in mapping["slots"]]


# =============================================================================
# DB records
# =============================================================================
def register(conn, name, equipment_type, source_path, compiled_path, mapping, status, user_id, notes=None):
    ver = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM report_templates WHERE equipment_type=?",
                       (equipment_type,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO report_templates(name,equipment_type,version,source_path,source_docx_sha,compiled_path,compiled_sha,"
        "mapping_json,status,notes,created_by,created_at,approved_by,approved_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (name, equipment_type, ver, _rel(source_path), integrity.sha256_file(source_path),
         _rel(compiled_path) if compiled_path else None,
         integrity.sha256_file(compiled_path) if compiled_path and os.path.exists(compiled_path) else None,
         json.dumps(mapping), status, notes, user_id, now_ist(),
         user_id if status == "active" else None, now_ist() if status == "active" else None))
    return cur.lastrowid


def register_repo_v1(conn):
    """Seed: register report_templates/v1 (built by tools/import_template.py) as the active template."""
    if conn.execute("SELECT 1 FROM report_templates").fetchone():
        return None
    folder = os.path.join(REPO_TEMPLATES, "v1")
    with open(os.path.join(folder, "mapping.json"), encoding="utf-8") as fh:
        mapping = json.load(fh)
    review = yaml.safe_load(open(os.path.join(folder, "review.yaml"), encoding="utf-8"))
    return register(conn, review.get("name", "Template v1"), review.get("equipment_type", DEFAULT_EQUIPMENT),
                    os.path.join(folder, "source.docx"), os.path.join(folder, "compiled.docx"), mapping, "active", None,
                    "Built from Transformer_Test_report_format_1.docx by tools/import_template.py")


def create_draft(conn, file_path, original_name, user, name, equipment_type=DEFAULT_EQUIPMENT, use_ai=False, ai_fn=None):
    """Upload a new DOCX: parse, auto-map (inheriting from the active version), compile and dry-run."""
    act = active(conn, equipment_type)
    prev = mapping_of(act) if act else None
    os.makedirs(TEMPLATE_STORE, exist_ok=True)
    tmp_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM report_templates").fetchone()[0]
    folder = os.path.join(TEMPLATE_STORE, f"t{tmp_id}_{integrity.sha256_file(file_path)[:8]}")
    os.makedirs(folder, exist_ok=True)
    source = os.path.join(folder, "source.docx")
    shutil.copyfile(file_path, source)
    parsed, mapping = build(source, prev, use_ai=use_ai, ai_fn=ai_fn)
    compiled = os.path.join(folder, "compiled.docx")
    meta, problems, _ = compile_and_check(source, mapping, compiled)
    mapping["dry_run"] = {"unfilled": problems, "warnings": meta["warnings"]}
    tid = register(conn, name, equipment_type, source, compiled, mapping, "draft", user["id"],
                   f"Uploaded {original_name}")
    return tid, mapping


def recompile(conn, rec, mapping):
    """After editing a draft mapping: compile again, dry-run, store."""
    if rec["status"] not in ("draft", "pending_hod"):
        raise ValueError("Only draft templates can be changed")
    source = _abs(rec["source_path"])
    compiled = _abs(rec["compiled_path"]) if rec["compiled_path"] else os.path.join(os.path.dirname(source), "compiled.docx")
    parsed = docx_parser.parse(source, mapping.get("sheet_marker", r"^TEST REPORT$")).to_json()
    mapping["stats"] = automapper.stats(parsed, mapping)
    meta, problems, _ = compile_and_check(source, mapping, compiled)
    mapping["dry_run"] = {"unfilled": problems, "warnings": meta["warnings"]}
    conn.execute("UPDATE report_templates SET mapping_json=?, compiled_path=?, compiled_sha=?, status='draft', "
                 "version_row=version_row+1 WHERE id=?",
                 (json.dumps(mapping), _rel(compiled), integrity.sha256_file(compiled), rec["id"]))
    return mapping


def activate(conn, template_id, hod_id):
    """Approval apply: retire the current active template of the same equipment type, activate this one."""
    rec = get(conn, template_id)
    conn.execute("UPDATE report_templates SET status='retired' WHERE equipment_type=? AND status='active'",
                 (rec["equipment_type"],))
    conn.execute("UPDATE report_templates SET status='active', approved_by=?, approved_at=? WHERE id=?",
                 (hod_id, now_ist(), template_id))


def blocking_problems(conn, rec):
    """Reasons a draft can not be sent for activation."""
    mapping = mapping_of(rec)
    out = []
    left = mapping.get("stats", {}).get("unmapped_required", [])
    if left:
        out.append(f"{len(left)} required slot(s) are not mapped")
    if mapping.get("dry_run", {}).get("unfilled"):
        out.append(f"Dry run left {len(mapping['dry_run']['unfilled'])} unfilled place(s)")
    return out


# =============================================================================
# mapping editor view model
# =============================================================================
def editor_view(rec):
    """Blocks of the source document with their slots and current bindings, for the HTML editor."""
    mapping = mapping_of(rec)
    parsed = docx_parser.parse(_abs(rec["source_path"]), mapping.get("sheet_marker", r"^TEST REPORT$")).to_json()
    registry = F.load()["by_key"]
    slots = {}
    for s in parsed["slots"]:
        b = mapping["slots"].get(s["id"])
        label = ""
        if b:
            if b.get("kind") == "field":
                f = registry.get(b["key"])
                label = f"{b['key']}" + (f" - {f['label']}" if f else " (unknown field)")
            elif b.get("kind") == "text":
                label = f"text: {b.get('text', '')}"
            elif b.get("kind") == "expr":
                label = f"expression: {b.get('expr', '')}"
            else:
                label = "ignored"
        slots[s["id"]] = dict(s, binding=b, binding_label=label)
    by_cell, by_block = {}, {}
    for s in slots.values():
        if s.get("t") is not None:
            by_cell.setdefault((s["t"], s["row"], s["col"]), []).append(s)
        elif s.get("block") is not None:
            by_block.setdefault(s["block"], []).append(s)
    loops = {lp["t"]: lp for lp in mapping.get("loops", [])}
    sched = {sc["t"]: sc for sc in mapping.get("schedule", [])}
    blocks = []
    for b in parsed["blocks"]:
        if b["type"] == "p":
            blocks.append({"type": "p", "i": b["i"], "text": b["text"], "sheet": b["sheet"], "slots": by_block.get(b["i"], [])})
        elif b["type"] == "t":
            t = parsed["tables"][b["t"]]
            rows = []
            for r, cells in enumerate(t["cells"]):
                rows.append([dict(c, slots=by_cell.get((b["t"], r, c["gi"]), [])) for c in cells])
            blocks.append({"type": "t", "i": b["i"], "t": b["t"], "sheet": b["sheet"], "rows": rows,
                           "loop": loops.get(b["t"]), "schedule": sched.get(b["t"])})
    footer = [s for s in slots.values() if s.get("part") in ("footer", "header")]
    return {"blocks": blocks, "footer": footer, "mapping": mapping, "parsed": parsed,
            "stats": automapper.stats(parsed, mapping)}
