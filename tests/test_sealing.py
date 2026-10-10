"""After issue, the DATABASE itself must refuse any change (triggers), not just the Python code."""
import sqlite3

import pytest

from app import integrity, jobs, workflow
from app.db import tx


def test_issued_job_is_sealed(seeded, conn):
    job = conn.execute("SELECT * FROM requests WHERE id=?", (seeded["job_id"],)).fetchone()
    assert job["status"] == workflow.ISSUED and job["is_sealed"] == 1
    rep = conn.execute("SELECT * FROM reports WHERE job_id=?", (job["id"],)).fetchone()
    assert len(rep["sha256_snapshot"]) == 64
    # the issued report records the template it was generated with, and the HOD who authorized it
    assert rep["template_id"] and rep["template_version"] == 1 and len(rep["template_sha"]) == 64
    assert conn.execute("SELECT role FROM users WHERE id=?", (rep["issued_by"],)).fetchone()["role"] == "hod"


@pytest.mark.parametrize("sql", [
    "UPDATE readings SET current_value='999' WHERE job_id={j}",
    "DELETE FROM readings WHERE job_id={j}",
    "INSERT INTO readings(job_id,station,key,current_value) VALUES ({j},'losses','x.y','1')",
    "UPDATE requests SET status='IN_TESTING' WHERE id={j}",
    "UPDATE requests SET is_sealed=0 WHERE id={j}",
    "DELETE FROM requests WHERE id={j}",
    "UPDATE test_results SET passed=1 WHERE job_id={j}",
    "DELETE FROM test_results WHERE job_id={j}",
    "UPDATE reports SET sha256_docx='0' WHERE job_id={j}",
    "DELETE FROM reports WHERE job_id={j}",
    "UPDATE files SET sha256='0' WHERE job_id={j}",
    "UPDATE job_tests SET verified=0 WHERE job_id={j}",
    "UPDATE report_templates SET mapping_json='{{}}' WHERE status='active'",
    "DELETE FROM report_templates",
    "DELETE FROM job_engineers WHERE job_id={j}",
])
def test_update_after_issue_fails_at_db_level(seeded, conn, sql):
    with pytest.raises(sqlite3.IntegrityError) as e:
        conn.execute(sql.format(j=seeded["job_id"]))
    assert any(w in str(e.value) for w in ("SEALED", "IMMUTABLE", "APPEND-ONLY")), str(e.value)


def test_workflow_refuses_edits_on_sealed_job(seeded, conn):
    eng = dict(conn.execute("SELECT * FROM users WHERE username='engineer1'").fetchone())
    rid = conn.execute("SELECT id FROM readings WHERE job_id=? LIMIT 1", (seeded["job_id"],)).fetchone()["id"]
    with pytest.raises(workflow.WorkflowError):
        with tx(conn):
            jobs.correct_reading(conn, seeded["job_id"], rid, "1.234", "test", None, eng)


def test_integrity_verification_passes(seeded, conn):
    rep = conn.execute("SELECT * FROM reports WHERE job_id=?", (seeded["job_id"],)).fetchone()
    res = integrity.verify_report(conn, rep)
    assert res["ok"], res


def test_tampered_file_is_detected(seeded, conn, tmp_path):
    rep = dict(conn.execute("SELECT * FROM reports WHERE job_id=?", (seeded["job_id"],)).fetchone())
    fake = tmp_path / "tampered.docx"
    fake.write_bytes(open(rep["docx_path"], "rb").read() + b"x")
    rep["docx_path"] = str(fake)
    res = integrity.verify_report(conn, rep)
    assert not res["ok"]


def test_amendment_creates_new_job_and_keeps_original(seeded, conn):
    admin = dict(conn.execute("SELECT * FROM users WHERE username='admin'").fetchone())
    with tx(conn):
        new_id = jobs.create_amendment(conn, seeded["job_id"], admin, "typo in ambient temperature")
    new = conn.execute("SELECT * FROM requests WHERE id=?", (new_id,)).fetchone()
    assert new["report_no"].startswith("CPRIBLRSCL25T1654/A") and new["amends_id"] == seeded["job_id"]
    assert new["status"] == workflow.ASSIGNED and new["is_sealed"] == 0
    n_orig = conn.execute("SELECT COUNT(*) FROM readings WHERE job_id=?", (seeded["job_id"],)).fetchone()[0]
    n_new = conn.execute("SELECT COUNT(*) FROM readings WHERE job_id=?", (new_id,)).fetchone()[0]
    assert n_orig == n_new > 0
