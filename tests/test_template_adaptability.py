"""
The report format can change without code changes.

A modified copy of template v1 is made (two sections reordered, two labels renamed, one new
field row added, one table deleted). It is imported through the same pipeline as an uploaded
format: the auto-mapper must map at least 90 % of the required value places, and after the
remaining places are mapped the generated report must have no unfilled places.
"""
import copy

from docx import Document
from docx.table import _Row

from app.reporting import automapper, generator, templates as T


def make_modified(src, out):
    doc = Document(src)
    P, Tb = doc.paragraphs, doc.tables
    # 1. reorder: move the insulation-resistance block (heading + table) after the induced over-voltage block
    ir_head, ir_tbl, ind_tbl = P[83]._p, Tb[18]._tbl, Tb[19]._tbl
    ind_tbl.addnext(ir_head)
    ir_head.addnext(ir_tbl)
    # ... and swap the oil-leakage and routine-pressure blocks' order on sheet 8 is covered by text anchors as well
    # 2. rename two labels
    for cell, old, new in ((Tb[14].rows[4].cells[0], "Load loss at 75", "Load losses at 75"),
                           (Tb[17].rows[1].cells[0], "Total loss at 50% load", "Total losses at 50 % load")):
        for p in cell.paragraphs:
            for r in p.runs:
                if old in r.text:
                    r.text = r.text.replace(old, new)
    # 3. one new field row in the insulation-resistance table
    tr = copy.deepcopy(Tb[18].rows[3]._tr)
    Tb[18]._tbl.append(tr)
    for i, c in enumerate(_Row(tr, Tb[18]).cells):
        for p in c.paragraphs[1:]:
            p._p.getparent().remove(p._p)
        runs = c.paragraphs[0].runs
        for r in runs[1:]:
            r._r.getparent().remove(r._r)
        if runs:
            runs[0].text = "Ambient temperature during test (°C)" if i == 0 else ""
    # 4. delete one table (test-procedure box of the no-load current at 112.5 % test)
    Tb[35]._tbl.getparent().remove(Tb[35]._tbl)
    doc.save(out)


def test_modified_template_maps_and_generates(seeded, tmp_path):
    conn = seeded["conn"]
    act = T.active(conn)
    src = T._abs(act["source_path"])
    modified = str(tmp_path / "modified.docx")
    make_modified(src, modified)
    parsed, mapping = T.build(modified, T.mapping_of(act))
    st = mapping["stats"]
    print(f"auto-mapped {st['mapped_required']}/{st['required']} = {st['coverage']:.1%} {st['methods']}")
    assert st["coverage"] >= 0.90
    # the new row was mapped by its label to the registry field rt.amb.*
    new_row = [b for b in mapping["slots"].values() if b.get("key") in ("rt.amb.bt", "rt.amb.at")]
    assert len(new_row) == 2
    # map whatever is left (here: a reviewer would do this in the editor) and generate
    for s in automapper.stats(parsed, mapping)["unmapped_required"]:
        mapping["slots"][s] = {"kind": "ignore", "method": "manual"}
    compiled = str(tmp_path / "compiled.docx")
    meta, problems, preview = T.compile_and_check(modified, mapping, compiled)
    assert problems == [], problems
    assert "Load losses at 75" in "".join(t.text for t in Document(preview).tables[14].rows[4].cells)
    assert len(Document(preview).tables) == 38           # one table fewer


def test_upload_creates_draft_and_activation_needs_hod(seeded, tmp_path):
    from app import approvals
    from app.db import tx
    from conftest import user
    conn = seeded["conn"]
    admin, hod = user(conn, "admin"), user(conn, "hod")
    act = T.active(conn)
    modified = str(tmp_path / "m.docx")
    make_modified(T._abs(act["source_path"]), modified)
    with tx(conn):
        tid, mapping = T.create_draft(conn, modified, "m.docx", admin, "Revised format")
    rec = T.get(conn, tid)
    assert rec["status"] == "draft" and rec["version"] == act["version"] + 1
    mapping = T.mapping_of(rec)
    for s in mapping["stats"]["unmapped_required"]:
        mapping["slots"][s] = {"kind": "ignore", "method": "manual"}
    with tx(conn):
        T.recompile(conn, rec, mapping)
        aid = approvals.submit(conn, "template_activation", {"template_id": tid}, admin, tid, "Activate")
    assert T.active(conn)["id"] == act["id"]              # not active before the HOD approves
    approvals.decide(conn, aid, hod, True)
    assert T.active(conn)["id"] == tid
    assert T.get(conn, act["id"])["status"] == "retired"
    # restore v1 as active for the other tests
    with tx(conn):
        conn.execute("UPDATE report_templates SET status='retired' WHERE id=?", (tid,))
        conn.execute("UPDATE report_templates SET status='active' WHERE id=?", (act["id"],))
