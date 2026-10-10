"""
Domain operations on a job (test request). Routes stay thin; everything that
changes data lives here so it can be unit-tested without HTTP.

All write functions expect to be called inside `db.tx(conn)` unless noted.
"""
import json
import os
import shutil
import tempfile
from datetime import datetime

from werkzeug.utils import secure_filename

import config
from . import audit, excel_io, fields as F, integrity, numbering, report, rules, workflow
from .db import IST, get_settings, now_ist, tx
from .workflow import Conflict, WorkflowError


# =============================================================================
# reading helpers
# =============================================================================
def get_job(conn, job_id):
    job = conn.execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise WorkflowError("Job not found")
    return job


def readings_values(conn, job_id):
    return {r["key"]: r["current_value"] for r in
            conn.execute("SELECT key, current_value FROM readings WHERE job_id=?", (job_id,))}


def job_stations(job):
    return F.stations_for_tests(workflow.tests_of(job))


def engineers_of(conn, job_id):
    return conn.execute("SELECT je.*, u.full_name, u.username FROM job_engineers je JOIN users u ON u.id=je.user_id "
                        "WHERE je.job_id=? ORDER BY je.role DESC, u.full_name", (job_id,)).fetchall()


def is_assigned(conn, job_id, user_id):
    return conn.execute("SELECT 1 FROM job_engineers WHERE job_id=? AND user_id=?", (job_id, user_id)).fetchone() is not None


def missing_required(conn, job, station):
    tests = workflow.tests_of(job)
    have = {k for k, v in readings_values(conn, job["id"]).items() if v not in (None, "")}
    return [f["key"] for f in F.station_fields(station) if F.is_required(f, tests) and f["key"] not in have]


def station_status(conn, job):
    """One dict per logsheet station needed by the job (for the engineer tabs / admin review)."""
    out = []
    results = conn.execute("SELECT station, passed FROM test_results WHERE job_id=?", (job["id"],)).fetchall()
    vt = {r["station"]: r for r in conn.execute("SELECT * FROM job_tests WHERE job_id=?", (job["id"],))}
    counts = {r["station"]: r["n"] for r in conn.execute(
        "SELECT station, COUNT(*) n FROM readings WHERE job_id=? AND current_value IS NOT NULL AND current_value<>'' "
        "GROUP BY station", (job["id"],))}
    for st in job_stations(job):
        rs = [r for r in results if r["station"] == st]
        out.append({
            "station": st, "title": F.stations()[st]["title"], "n_values": counts.get(st, 0),
            "missing": missing_required(conn, job, st),
            "verified": bool(vt.get(st) and vt[st]["verified"]),
            "verified_at": vt[st]["verified_at"] if vt.get(st) else None,
            "passed": sum(1 for r in rs if r["passed"] == 1), "failed": sum(1 for r in rs if r["passed"] == 0),
        })
    return out


# =============================================================================
# files
# =============================================================================
def save_upload(conn, file_storage, job_id, kind, user, allowed_ext, station=None):
    """Store an uploaded file immutably (content-hashed name, read-only) and register it."""
    name = secure_filename(file_storage.filename or "upload")
    ext = os.path.splitext(name)[1].lower()
    if ext not in allowed_ext:
        raise WorkflowError(f"File type {ext or '?'} not allowed (allowed: {', '.join(sorted(allowed_ext))})")
    data = file_storage.read()
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise WorkflowError(f"File larger than {config.MAX_UPLOAD_MB} MB")
    if not data:
        raise WorkflowError("Empty file")
    import hashlib
    digest = hashlib.sha256(data).hexdigest()
    folder = os.path.join(config.UPLOAD_DIR, f"job_{job_id}" if job_id else "unassigned")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{digest[:16]}_{name}")
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(data)
        integrity.make_read_only(path)
    cur = conn.execute("INSERT INTO files(job_id,kind,station,original_name,stored_path,sha256,size,uploaded_by,uploaded_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (job_id, kind, station, file_storage.filename or name, path, digest,
                                                      len(data), user["id"], now_ist()))
    audit.log(conn, "file.upload", "file", cur.lastrowid, None,
              {"job_id": job_id, "kind": kind, "station": station, "name": name, "sha256": digest}, user=user)
    return cur.lastrowid


# =============================================================================
# customer request
# =============================================================================
def create_request(conn, customer, form_data, tests):
    cur = conn.execute("INSERT INTO requests(customer_id,status,form_json,tests_json,created_at,updated_by,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)", (customer["id"], workflow.SUBMITTED, json.dumps(form_data),
                                                   json.dumps(tests), now_ist(), customer["id"], now_ist()))
    audit.log(conn, "request.submit", "job", cur.lastrowid, None,
              {"tests": tests, "serial": form_data.get("serial_numbers"), "rating": form_data.get("rating")}, user=customer)
    return cur.lastrowid


# =============================================================================
# admin: accept / reject / assign
# =============================================================================
def accept(conn, job_id, admin, condition, wi_extra, expected_version, year=None):
    """Allocate sample code + report number atomically and create the work instruction."""
    job = get_job(conn, job_id)
    workflow.check_version(conn, job, expected_version)
    if job["status"] != workflow.SUBMITTED:
        raise WorkflowError("Only submitted requests can be accepted")
    settings = get_settings(conn)
    year = year or datetime.now(IST).year
    sample_code = numbering.next_sample_code(conn, settings, year)
    report_no = numbering.next_report_no(conn, settings, year)
    form = json.loads(job["form_json"])
    tests = workflow.tests_of(job)
    wi = {
        "format": "CPRI/QAF/02",
        "test_series_no": report_no,
        "sample_code": sample_code,
        "customer": form.get("customer_name", ""),
        "customer_ref": wi_extra.get("customer_ref") or "-",
        "description": form.get("rating", "") + " " + form.get("samples", ""),
        "mounting": form.get("mounting_instructions") or "-",
        "special_instructions": wi_extra.get("special_instructions") or "-",
        "date_of_test": wi_extra.get("date_of_test") or datetime.now(IST).strftime("%d-%m-%Y"),
        "customer_requirement": form.get("customer_requirement") or "Nil",
        "tests_required": [f"{F.tests()[c]['label']} [{F.tests()[c]['clause']}]" for c in tests],
        "reference_standard": form.get("criteria") or settings["standard_text"],
        "latest_standard": wi_extra.get("latest_standard") or "Yes",
        "deviation": wi_extra.get("deviation") or "Nil",
        "work_allotted_to": "",
        "issued_by": admin["full_name"],
        "issued_at": now_ist(),
    }
    workflow.apply(conn, job_id, "accept", admin, expected_version, sets={
        "sample_code": sample_code, "report_no": report_no, "accepted_at": now_ist(), "accepted_by": admin["id"],
        "condition_json": json.dumps(condition), "wi_json": json.dumps(wi)},
        audit_extra={"sample_code": sample_code, "report_no": report_no, "condition": condition})
    return sample_code, report_no


def reject(conn, job_id, admin, reason, expected_version, condition=None):
    if not reason.strip():
        raise WorkflowError("A reason is required to reject a request")
    workflow.apply(conn, job_id, "reject", admin, expected_version,
                   sets={"reject_reason": reason.strip(), "condition_json": json.dumps(condition or {})},
                   audit_extra={"reason": reason.strip()})


def assign(conn, job_id, admin, responsible_id, additional, expected_version):
    """additional = [(user_id, station), ...]; station '' = all."""
    eng = conn.execute("SELECT * FROM users WHERE id=? AND role='engineer' AND status='active'", (responsible_id,)).fetchone()
    if eng is None:
        raise WorkflowError("Pick an approved engineer as responsible test engineer")
    job = get_job(conn, job_id)
    workflow.check_version(conn, job, expected_version)
    conn.execute("DELETE FROM job_engineers WHERE job_id=?", (job_id,))
    ts = now_ist()
    conn.execute("INSERT INTO job_engineers(job_id,user_id,role,station,assigned_by,assigned_at) VALUES (?,?,?,?,?,?)",
                 (job_id, responsible_id, "responsible", "", admin["id"], ts))
    names = [eng["full_name"]]
    for uid, station in additional:
        if int(uid) == int(responsible_id):
            continue
        u = conn.execute("SELECT * FROM users WHERE id=? AND role='engineer' AND status='active'", (uid,)).fetchone()
        if u is None:
            continue
        conn.execute("INSERT OR REPLACE INTO job_engineers(job_id,user_id,role,station,assigned_by,assigned_at) "
                     "VALUES (?,?,?,?,?,?)", (job_id, uid, "additional", station or "", admin["id"], ts))
        names.append(u["full_name"] + (f" ({F.stations()[station]['title']})" if station else ""))
    wi = json.loads(job["wi_json"] or "{}")
    wi["work_allotted_to"] = " / ".join(names)
    for st in job_stations(job):
        conn.execute("INSERT OR IGNORE INTO job_tests(job_id, station) VALUES (?,?)", (job_id, st))
    workflow.apply(conn, job_id, "assign", admin, expected_version,
                   sets={"assigned_at": ts, "wi_json": json.dumps(wi)},
                   audit_extra={"responsible": eng["username"], "team": names})


# =============================================================================
# engineer: import / correct / verify / lock
# =============================================================================
def _ensure_editable(conn, job, user):
    """Engineer edits move ASSIGNED/RETURNED -> IN_TESTING; refuse when locked/sealed."""
    if job["is_sealed"]:
        raise WorkflowError("Job is sealed - the report has been issued.")
    if job["status"] not in workflow.EDITABLE:
        raise WorkflowError(f"Job is {workflow.STATE_LABELS.get(job['status'])} - editing is not allowed.")
    if user["role"] == "engineer" and not is_assigned(conn, job["id"], user["id"]):
        err = WorkflowError("You are not assigned to this job")
        err.status = 403
        raise err
    if job["status"] in (workflow.ASSIGNED, workflow.RETURNED):
        workflow.apply(conn, job["id"], "start_testing", user)
    else:
        workflow.touch(conn, job["id"], user)


def _unverify(conn, job_id, station, user, why):
    row = conn.execute("SELECT verified FROM job_tests WHERE job_id=? AND station=?", (job_id, station)).fetchone()
    if row and row["verified"]:
        conn.execute("UPDATE job_tests SET verified=0, verified_by=NULL, verified_at=NULL, version=version+1 "
                     "WHERE job_id=? AND station=?", (job_id, station))
        audit.log(conn, "station.unverify", "job", job_id, {"station": station, "verified": 1},
                  {"verified": 0, "why": why}, user=user)


def store_import(conn, job_id, station, file_id, result, user):
    """Write an ImportResult into readings. Re-imports are logged as corrections (nothing is lost)."""
    job = get_job(conn, job_id)
    _ensure_editable(conn, job, user)
    fname = conn.execute("SELECT original_name FROM files WHERE id=?", (file_id,)).fetchone()["original_name"]
    existing = {r["key"]: r for r in conn.execute("SELECT * FROM readings WHERE job_id=? AND station=?", (job_id, station))}
    ins = upd = same = 0
    ts = now_ist()
    for key, (value, cell) in result.values.items():
        f = F.field(key)
        row = existing.get(key)
        if row is None:
            conn.execute("INSERT INTO readings(job_id,station,key,imported_value,current_value,unit,source_file_id,"
                         "source_cell,updated_by,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (job_id, station, key, value, value, f["unit"], file_id, cell, user["id"], ts))
            ins += 1
        elif row["current_value"] != value or row["imported_value"] != value:
            conn.execute("INSERT INTO corrections(job_id,reading_id,key,old_value,new_value,reason,user_id,ts) "
                         "VALUES (?,?,?,?,?,?,?,?)", (job_id, row["id"], key, row["current_value"], value,
                                                      f"Re-imported from {fname} ({cell})", user["id"], ts))
            conn.execute("UPDATE readings SET imported_value=?, current_value=?, source_file_id=?, source_cell=?, "
                         "version=version+1, updated_by=?, updated_at=? WHERE id=?",
                         (value, value, file_id, cell, user["id"], ts, row["id"]))
            upd += 1
        else:
            same += 1
    _unverify(conn, job_id, station, user, "new logsheet imported")
    summary = {"station": station, "file_id": file_id, "inserted": ins, "updated": upd, "unchanged": same,
               "missing": result.missing, "unfilled": [k for k, _ in result.unfilled],
               "non_numeric": [k for k, _, _ in result.non_numeric]}
    audit.log(conn, "readings.import", "job", job_id, None, summary, user=user)
    recompute(conn, job_id)
    return summary


def correct_reading(conn, job_id, reading_id, new_value, reason, expected_version, user):
    """Manual correction: requires a reason; keeps imported_value; optimistic lock on the reading."""
    if not (reason or "").strip():
        raise WorkflowError("A reason is required for every correction")
    job = get_job(conn, job_id)
    row = conn.execute("SELECT * FROM readings WHERE id=? AND job_id=?", (reading_id, job_id)).fetchone()
    if row is None:
        raise WorkflowError("Reading not found")
    if expected_version is not None and str(row["version"]) != str(expected_version):
        who = conn.execute("SELECT full_name FROM users WHERE id=?", (row["updated_by"],)).fetchone()
        raise Conflict(f"This record was changed by {who['full_name'] if who else 'another user'} at {row['updated_at']}; reload.")
    f = F.field(row["key"])
    new_value = (new_value or "").strip()
    if f and f["type"] == "number" and new_value and F.to_number(new_value) is None:
        raise WorkflowError(f"{f['label']} must be a number")
    if new_value == (row["current_value"] or ""):
        raise WorkflowError("Value unchanged")
    _ensure_editable(conn, job, user)
    ts = now_ist()
    conn.execute("INSERT INTO corrections(job_id,reading_id,key,old_value,new_value,reason,user_id,ts) VALUES (?,?,?,?,?,?,?,?)",
                 (job_id, reading_id, row["key"], row["current_value"], new_value, reason.strip(), user["id"], ts))
    conn.execute("UPDATE readings SET current_value=?, version=version+1, updated_by=?, updated_at=? WHERE id=?",
                 (new_value, user["id"], ts, reading_id))
    audit.log(conn, "reading.correct", "reading", reading_id, {"key": row["key"], "value": row["current_value"]},
              {"key": row["key"], "value": new_value, "reason": reason.strip(), "job_id": job_id}, user=user)
    _unverify(conn, job_id, row["station"], user, f"reading {row['key']} corrected")
    recompute(conn, job_id)
    return conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()


def recompute(conn, job_id):
    """Re-run rules.py and replace the job's test_results."""
    job = get_job(conn, job_id)
    results, _calc = rules.evaluate(readings_values(conn, job_id), json.loads(job["form_json"]),
                                    workflow.tests_of(job), get_settings(conn))
    conn.execute("DELETE FROM test_results WHERE job_id=?", (job_id,))
    ts = now_ist()
    stations = {c: F.tests()[c]["station"] for c in F.tests()}
    for r in results:
        conn.execute("INSERT INTO test_results(job_id,station,rule_code,label,value,limit_text,formula,passed,computed_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?)",
                     (job_id, r["station"] or stations.get(r["test"]), r["rule_code"], r["label"], r["value"], r["limit"],
                      r["formula"], None if r["passed"] is None else int(bool(r["passed"])), ts))
    return results


def set_verified(conn, job_id, station, user):
    job = get_job(conn, job_id)
    if station not in job_stations(job):
        raise WorkflowError("Station not part of this job")
    missing = missing_required(conn, job, station)
    if missing:
        labels = ", ".join(F.field(k)["label"] for k in missing[:6])
        raise WorkflowError(f"{len(missing)} required value(s) missing - e.g. {labels}")
    _ensure_editable(conn, job, user)
    conn.execute("INSERT INTO job_tests(job_id, station, verified, verified_by, verified_at) VALUES (?,?,1,?,?) "
                 "ON CONFLICT(job_id, station) DO UPDATE SET verified=1, verified_by=excluded.verified_by, "
                 "verified_at=excluded.verified_at, version=version+1", (job_id, station, user["id"], now_ist()))
    audit.log(conn, "station.verify", "job", job_id, None, {"station": station}, user=user)


def lock(conn, job_id, user, expected_version):
    job = get_job(conn, job_id)
    if not is_assigned(conn, job_id, user["id"]):
        raise WorkflowError("You are not assigned to this job")
    pending = [s["title"] for s in station_status(conn, job) if not s["verified"]]
    if pending:
        raise WorkflowError("Verify every test first: " + ", ".join(pending))
    workflow.apply(conn, job_id, "lock", user, expected_version, sets={"locked_at": now_ist(), "return_comment": None})


# =============================================================================
# admin: review / return / issue / amend
# =============================================================================
def begin_review(conn, job_id, admin):
    job = get_job(conn, job_id)
    if job["status"] == workflow.LOCKED:
        workflow.apply(conn, job_id, "begin_review", admin)


def return_to_engineer(conn, job_id, admin, comment, expected_version):
    if not (comment or "").strip():
        raise WorkflowError("A comment is mandatory when returning a job")
    workflow.apply(conn, job_id, "return", admin, expected_version, sets={"return_comment": comment.strip()},
                   audit_extra={"comment": comment.strip()})
    conn.execute("UPDATE job_tests SET verified=0, version=version+1 WHERE job_id=?", (job_id,))


def _sealed_folder(report_no):
    return os.path.join(config.SEALED_DIR, report_no.replace("/", "_"))


def preview(conn, job_id, admin, want_pdf=True):
    """Live preview of the filled report (not sealed). Cached per job version."""
    job = get_job(conn, job_id)
    snap = report.build_snapshot(conn, job_id)
    os.makedirs(config.PREVIEW_DIR, exist_ok=True)
    base = os.path.join(config.PREVIEW_DIR, f"job{job_id}_v{job['version']}")
    docx_path, pdf_path = base + ".docx", base + ".pdf"
    if not os.path.exists(docx_path):
        report.render_docx(snap, docx_path, {"reviewer_name": admin["full_name"], "seal": "PREVIEW-NOT-ISSUED",
                                            "report_date": datetime.now(IST).strftime("%d-%m-%Y")})
    if want_pdf and not os.path.exists(pdf_path):
        report.docx_to_pdf(docx_path, pdf_path)
    return docx_path, (pdf_path if os.path.exists(pdf_path) else None)


def issue(conn, job_id, admin, expected_version, verify_base_url=""):
    """
    Approve & Issue:
      1. freeze canonical snapshot (request + readings + rule results)
      2. render DOCX (+PDF) with the snapshot hash in the footer / QR
      3. hash files, store in `reports`, set requests.is_sealed = 1  (all in ONE transaction)
    The slow file generation happens before the write lock is taken; inside the
    transaction the snapshot is rebuilt and compared so nothing can slip in between.
    """
    job = get_job(conn, job_id)
    workflow.check_version(conn, job, expected_version)
    if job["status"] != workflow.UNDER_REVIEW:
        raise WorkflowError("Only jobs under review can be issued")
    fails = conn.execute("SELECT COUNT(*) FROM test_results WHERE job_id=? AND passed=0", (job_id,)).fetchone()[0]
    snap = report.build_snapshot(conn, job_id)
    issued_at = now_ist()
    snap["issue"] = {"report_no": job["report_no"], "issued_at": issued_at, "issued_by": admin["username"],
                     "reviewer_name": admin["full_name"], "failed_rules": fails}
    snapshot_json = integrity.canonical_json(snap)
    sha_snap = integrity.sha256_text(snapshot_json)
    tmp = tempfile.mkdtemp(prefix="atrg_issue_")
    docx_tmp, pdf_tmp = os.path.join(tmp, "report.docx"), os.path.join(tmp, "report.pdf")
    report.render_docx(snap, docx_tmp, {"report_date": report.ddmmyyyy(issued_at),
                                        "reviewer_name": admin["full_name"], "seal": sha_snap},
                       verify_url=f"{verify_base_url.rstrip('/')}/verify/{job['report_no']}" if verify_base_url else None)
    pdf_ok = report.docx_to_pdf(docx_tmp, pdf_tmp)
    folder = _sealed_folder(job["report_no"])
    os.makedirs(folder, exist_ok=True)
    docx_path = os.path.join(folder, f"{job['report_no'].replace('/', '_')}.docx")
    pdf_path = os.path.join(folder, f"{job['report_no'].replace('/', '_')}.pdf") if pdf_ok else None
    try:
        with tx(conn):
            job2 = get_job(conn, job_id)
            workflow.check_version(conn, job2, job["version"])
            again = report.build_snapshot(conn, job_id)
            if integrity.canonical_json(again["data"]) != integrity.canonical_json(snap["data"]):
                raise Conflict("Data changed while the report was being generated; reload and issue again.")
            shutil.copyfile(docx_tmp, docx_path)
            if pdf_path:
                shutil.copyfile(pdf_tmp, pdf_path)
            with open(os.path.join(folder, "snapshot.json"), "w", encoding="utf-8") as fh:
                fh.write(snapshot_json)
            amends = None
            if job["amends_id"]:
                r = conn.execute("SELECT id FROM reports WHERE job_id=? ORDER BY id DESC LIMIT 1", (job["amends_id"],)).fetchone()
                amends = r["id"] if r else None
            cur = conn.execute(
                "INSERT INTO reports(job_id,report_no,kind,amends_report_id,snapshot_json,sha256_snapshot,sha256_docx,"
                "sha256_pdf,docx_path,pdf_path,issued_by,issued_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, job["report_no"], "amended" if job["amends_id"] else "original", amends, snapshot_json, sha_snap,
                 integrity.sha256_file(docx_path), integrity.sha256_file(pdf_path) if pdf_path else None,
                 docx_path, pdf_path, admin["id"], issued_at))
            workflow.apply(conn, job_id, "issue", admin, job["version"], sets={"issued_at": issued_at, "is_sealed": 1},
                           audit_extra={"report_no": job["report_no"], "sha256_snapshot": sha_snap})
            report_id = cur.lastrowid
    except Exception:
        for p in (docx_path, pdf_path):
            if p and os.path.exists(p):
                os.remove(p)
        raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    for p in (docx_path, pdf_path, os.path.join(folder, "snapshot.json")):
        if p and os.path.exists(p):
            integrity.make_read_only(p)
    return conn.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()


def create_amendment(conn, job_id, admin, reason):
    """Corrections after issue -> a NEW job ('.../A1') that references the original; the original is untouched."""
    if not (reason or "").strip():
        raise WorkflowError("A reason is required for an amendment")
    orig = get_job(conn, job_id)
    if not orig["is_sealed"]:
        raise WorkflowError("Only issued reports can be amended")
    root_id = orig["amends_id"] or orig["id"]
    root = get_job(conn, root_id)
    n = conn.execute("SELECT COUNT(*) FROM requests WHERE amends_id=?", (root_id,)).fetchone()[0] + 1
    report_no = f"{numbering.base_report_no(root['report_no'])}/A{n}"
    wi = json.loads(orig["wi_json"] or "{}")
    wi["amendment_reason"] = reason.strip()
    ts = now_ist()
    cur = conn.execute(
        "INSERT INTO requests(customer_id,status,sample_code,report_no,amends_id,amendment_no,form_json,tests_json,"
        "condition_json,wi_json,created_at,accepted_at,accepted_by,assigned_at,updated_by,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (orig["customer_id"], workflow.ASSIGNED, orig["sample_code"], report_no, root_id, n, orig["form_json"],
         orig["tests_json"], orig["condition_json"], json.dumps(wi), ts, ts, admin["id"], ts, admin["id"], ts))
    new_id = cur.lastrowid
    for e in engineers_of(conn, job_id):
        conn.execute("INSERT INTO job_engineers(job_id,user_id,role,station,assigned_by,assigned_at) VALUES (?,?,?,?,?,?)",
                     (new_id, e["user_id"], e["role"], e["station"], admin["id"], ts))
    for r in conn.execute("SELECT * FROM readings WHERE job_id=?", (job_id,)).fetchall():
        conn.execute("INSERT INTO readings(job_id,station,key,imported_value,current_value,unit,source_file_id,source_cell,"
                     "updated_by,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (new_id, r["station"], r["key"], r["imported_value"], r["current_value"], r["unit"],
                      r["source_file_id"], r["source_cell"], admin["id"], ts))
    for st in job_stations(orig):
        conn.execute("INSERT INTO job_tests(job_id, station) VALUES (?,?)", (new_id, st))
    audit.log(conn, "job.amend", "job", new_id, {"original_job": job_id, "original_report": orig["report_no"]},
              {"report_no": report_no, "reason": reason.strip()}, user=admin)
    recompute(conn, new_id)
    return new_id
