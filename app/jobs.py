"""
Domain operations on a job (test request). Routes stay thin; everything that
changes data lives here so it can be unit-tested without HTTP.

Write functions expect to be called inside `db.tx(conn)` unless noted.
Actions that may need HOD sign-off are wired to the approval engine in
app/approval_types.py.
"""
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime

from werkzeug.utils import secure_filename

import config
from . import audit, fields as F, integrity, notify, numbering, report, rules, workflow
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


def unconfirmed_ai(conn, job_id, station=None):
    sql = "SELECT * FROM readings WHERE job_id=? AND source='ai' AND verified_by IS NULL"
    args = [job_id]
    if station:
        sql += " AND station=?"
        args.append(station)
    return conn.execute(sql, args).fetchall()


def station_status(conn, job):
    """One dict per logsheet station needed by the job (engineer tabs / admin review)."""
    out = []
    results = conn.execute("SELECT station, passed FROM test_results WHERE job_id=?", (job["id"],)).fetchall()
    vt = {r["station"]: r for r in conn.execute("SELECT * FROM job_tests WHERE job_id=?", (job["id"],))}
    counts = {r["station"]: r["n"] for r in conn.execute(
        "SELECT station, COUNT(*) n FROM readings WHERE job_id=? AND current_value IS NOT NULL AND current_value<>'' "
        "GROUP BY station", (job["id"],))}
    ai = {r["station"]: r["n"] for r in conn.execute(
        "SELECT station, COUNT(*) n FROM readings WHERE job_id=? AND source='ai' AND verified_by IS NULL GROUP BY station",
        (job["id"],))}
    for st in job_stations(job):
        rs = [r for r in results if r["station"] == st]
        out.append({
            "station": st, "title": F.stations()[st]["title"], "n_values": counts.get(st, 0),
            "missing": missing_required(conn, job, st), "ai_pending": ai.get(st, 0),
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


def attach_file(conn, file_id, job_id, user):
    """Files are immutable: attaching a pre-uploaded file to a job adds a new row pointing to the same stored file."""
    f = conn.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if f is None or f["uploaded_by"] != user["id"]:
        return None
    cur = conn.execute("INSERT INTO files(job_id,kind,station,original_name,stored_path,sha256,size,uploaded_by,uploaded_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (job_id, f["kind"], f["station"], f["original_name"], f["stored_path"],
                                                      f["sha256"], f["size"], user["id"], now_ist()))
    return cur.lastrowid


# =============================================================================
# customer request
# =============================================================================
def create_request(conn, customer, form_data, tests, source_file_id=None, ai_extraction_id=None):
    year = datetime.now(IST).year
    ref = f"REQ-{year}-{numbering.allocate(conn, 'request_ref', year):04d}"
    cur = conn.execute("INSERT INTO requests(customer_id,ref,status,form_json,tests_json,created_at,updated_by,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?)", (customer["id"], ref, workflow.SUBMITTED, json.dumps(form_data),
                                                    json.dumps(tests), now_ist(), customer["id"], now_ist()))
    job_id = cur.lastrowid
    if source_file_id:
        attach_file(conn, source_file_id, job_id, customer)
    audit.log(conn, "request.submit", "job", job_id, None,
              {"ref": ref, "tests": tests, "serial": form_data.get("serial_numbers"), "rating": form_data.get("rating"),
               "source_file": source_file_id, "ai_extraction": ai_extraction_id}, user=customer)
    notify.send(conn, f"New test request {ref} from {form_data.get('customer_name', '')}", f"/admin/requests/{job_id}",
                role="admin")
    return job_id, ref


# =============================================================================
# admin: accept / reject / assign  (wrapped by the approval engine)
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
        "format": "CPRI/QAF/02", "test_series_no": report_no, "sample_code": sample_code,
        "customer": form.get("customer_name", ""), "customer_ref": wi_extra.get("customer_ref") or job["ref"] or "-",
        "description": (form.get("rating", "") + " " + form.get("samples", "")).strip(),
        "mounting": form.get("mounting_instructions") or "-",
        "special_instructions": wi_extra.get("special_instructions") or "-",
        "date_of_test": wi_extra.get("date_of_test") or datetime.now(IST).strftime("%d-%m-%Y"),
        "customer_requirement": form.get("customer_requirement") or "Nil",
        "tests_required": [f"{F.tests()[c]['name']} [{F.tests()[c]['clause']}]" for c in tests if c in F.tests()],
        "reference_standard": form.get("criteria") or settings["standard_text"],
        "latest_standard": wi_extra.get("latest_standard") or "Yes", "deviation": wi_extra.get("deviation") or "Nil",
        "work_allotted_to": "", "issued_by": admin["full_name"], "issued_at": now_ist(),
    }
    workflow.apply(conn, job_id, "accept", admin, expected_version, sets={
        "sample_code": sample_code, "report_no": report_no, "accepted_at": now_ist(), "accepted_by": admin["id"],
        "condition_json": json.dumps(condition), "wi_json": json.dumps(wi)},
        audit_extra={"sample_code": sample_code, "report_no": report_no, "condition": condition})
    notify.send(conn, f"Request {job['ref']} accepted. Sample code {sample_code}, test report number {report_no}.",
                f"/customer/requests/{job_id}", user_id=job["customer_id"])
    return sample_code, report_no


def reject(conn, job_id, admin, reason, expected_version, condition=None):
    if not (reason or "").strip():
        raise WorkflowError("A reason is required to reject a request")
    job = get_job(conn, job_id)
    workflow.apply(conn, job_id, "reject", admin, expected_version,
                   sets={"reject_reason": reason.strip(), "condition_json": json.dumps(condition or {})},
                   audit_extra={"reason": reason.strip()})
    notify.send(conn, f"Request {job['ref']} was not accepted: {reason.strip()}", f"/customer/requests/{job_id}",
                user_id=job["customer_id"])


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
    notify.send(conn, f"Job {job['sample_code']} assigned to you (responsible engineer).", f"/engineer/jobs/{job_id}",
                user_id=eng["id"])
    for uid, station in additional:
        if int(uid) == int(responsible_id):
            continue
        u = conn.execute("SELECT * FROM users WHERE id=? AND role='engineer' AND status='active'", (uid,)).fetchone()
        if u is None:
            continue
        conn.execute("INSERT OR REPLACE INTO job_engineers(job_id,user_id,role,station,assigned_by,assigned_at) "
                     "VALUES (?,?,?,?,?,?)", (job_id, uid, "additional", station or "", admin["id"], ts))
        names.append(u["full_name"] + (f" ({F.stations()[station]['title']})" if station else ""))
        notify.send(conn, f"Job {job['sample_code']} assigned to you.", f"/engineer/jobs/{job_id}", user_id=u["id"])
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


def store_import(conn, job_id, station, file_id, values, user, source="excel"):
    """
    Write imported values into readings. `values` = {key: (value, source_ref[, confidence])}.
    Re-imports are logged as corrections (nothing is lost). AI values stay flagged
    "AI-extracted, needs verification" (verified_by NULL) until an engineer confirms them.
    """
    job = get_job(conn, job_id)
    _ensure_editable(conn, job, user)
    fname = conn.execute("SELECT original_name FROM files WHERE id=?", (file_id,)).fetchone()["original_name"]
    existing = {r["key"]: r for r in conn.execute("SELECT * FROM readings WHERE job_id=? AND station=?", (job_id, station))}
    ins = upd = same = 0
    ts = now_ist()
    for key, item in values.items():
        value, cell = item[0], item[1]
        conf = item[2] if len(item) > 2 else None
        f = F.field(key)
        if f is None:
            continue
        verified_by = None if source == "ai" else user["id"]
        row = existing.get(key)
        if row is None:
            conn.execute("INSERT INTO readings(job_id,station,key,imported_value,current_value,unit,source,source_file_id,"
                         "source_cell,ai_confidence,verified_by,verified_at,updated_by,updated_at) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (job_id, station, key, value, value, f["unit"], source, file_id, cell, conf, verified_by,
                          ts if verified_by else None, user["id"], ts))
            ins += 1
        elif row["current_value"] != value or row["imported_value"] != value:
            conn.execute("INSERT INTO corrections(job_id,reading_id,key,old_value,new_value,reason,user_id,ts) "
                         "VALUES (?,?,?,?,?,?,?,?)",
                         (job_id, row["id"], key, row["current_value"], value,
                          f"Re-imported from {fname} ({cell})" + (" by AI" if source == "ai" else ""), user["id"], ts))
            conn.execute("UPDATE readings SET imported_value=?, current_value=?, source=?, source_file_id=?, source_cell=?, "
                         "ai_confidence=?, verified_by=?, verified_at=?, version=version+1, updated_by=?, updated_at=? "
                         "WHERE id=?", (value, value, source, file_id, cell, conf, verified_by,
                                        ts if verified_by else None, user["id"], ts, row["id"]))
            upd += 1
        else:
            same += 1
    _unverify(conn, job_id, station, user, f"new logsheet imported ({source})")
    summary = {"station": station, "file_id": file_id, "source": source, "inserted": ins, "updated": upd, "unchanged": same}
    audit.log(conn, "readings.import", "job", job_id, None, summary, user=user)
    recompute(conn, job_id)
    return summary


def confirm_ai(conn, job_id, user, station=None, reading_id=None):
    """Engineer confirms AI-extracted values (clears the 'needs verification' flag)."""
    job = get_job(conn, job_id)
    _ensure_editable(conn, job, user)
    sql = ("UPDATE readings SET verified_by=?, verified_at=?, version=version+1 "
           "WHERE job_id=? AND source='ai' AND verified_by IS NULL")
    args = [user["id"], now_ist(), job_id]
    if reading_id:
        sql += " AND id=?"
        args.append(reading_id)
    elif station:
        sql += " AND station=?"
        args.append(station)
    n = conn.execute(sql, args).rowcount
    audit.log(conn, "readings.confirm_ai", "job", job_id, None, {"station": station, "reading": reading_id, "count": n},
              user=user)
    return n


def clear_ai(conn, job_id, user, station=None):
    """Remove automatically read values nobody confirmed yet (audited; confirmed or corrected values stay)."""
    job = get_job(conn, job_id)
    _ensure_editable(conn, job, user)
    sql = "SELECT * FROM readings WHERE job_id=? AND source='ai' AND verified_by IS NULL"
    rows = conn.execute(sql + (" AND station=?" if station else ""), [job_id] + ([station] if station else [])).fetchall()
    removed = {}
    for r in rows:
        if conn.execute("SELECT 1 FROM corrections WHERE reading_id=?", (r["id"],)).fetchone():
            continue
        conn.execute("DELETE FROM readings WHERE id=?", (r["id"],))
        removed[r["key"]] = r["current_value"]
    audit.log(conn, "readings.clear_ai", "job", job_id, {"values": removed}, {"station": station, "removed": len(removed)},
              user=user)
    recompute(conn, job_id)
    return len(removed)


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
        raise Conflict(f"This record was changed by {who['full_name'] if who else 'another user'} at "
                       f"{row['updated_at']}. Reload to continue.")
    f = F.field(row["key"])
    new_value = (new_value or "").strip()
    if f and f["dtype"] == "number" and new_value and F.to_number(new_value) is None:
        raise WorkflowError(f"{f['label']} must be a number")
    if new_value == (row["current_value"] or ""):
        raise WorkflowError("Value unchanged")
    _ensure_editable(conn, job, user)
    ts = now_ist()
    conn.execute("INSERT INTO corrections(job_id,reading_id,key,old_value,new_value,reason,user_id,ts) VALUES (?,?,?,?,?,?,?,?)",
                 (job_id, reading_id, row["key"], row["current_value"], new_value, reason.strip(), user["id"], ts))
    conn.execute("UPDATE readings SET current_value=?, verified_by=?, verified_at=?, version=version+1, updated_by=?, "
                 "updated_at=? WHERE id=?", (new_value, user["id"], ts, user["id"], ts, reading_id))
    audit.log(conn, "reading.correct", "reading", reading_id, {"key": row["key"], "value": row["current_value"]},
              {"key": row["key"], "value": new_value, "reason": reason.strip(), "job_id": job_id}, user=user)
    _unverify(conn, job_id, row["station"], user, f"reading {row['key']} corrected")
    recompute(conn, job_id)
    return conn.execute("SELECT * FROM readings WHERE id=?", (reading_id,)).fetchone()


def recompute(conn, job_id):
    """Re-run the rule checks and replace the job's test_results."""
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


def set_verified(conn, job_id, station, user, missing_reason=None):
    """Verify a test. Required values that are not in the logsheet can be left empty only with a stated reason."""
    job = get_job(conn, job_id)
    if station not in job_stations(job):
        raise WorkflowError("Station not part of this job")
    missing = missing_required(conn, job, station)
    missing_reason = (missing_reason or "").strip()
    if missing and len(missing_reason) < 5:
        labels = ", ".join(F.field(k)["label"] for k in missing[:6])
        raise WorkflowError(f"{len(missing)} required value(s) missing, for example: {labels}. "
                            "Fill them in, or verify with missing values and give the reason.")
    ai = unconfirmed_ai(conn, job_id, station)
    if ai:
        raise WorkflowError(f"{len(ai)} AI-extracted value(s) still need verification. Confirm or correct them first.")
    _ensure_editable(conn, job, user)
    conn.execute("INSERT INTO job_tests(job_id, station, verified, verified_by, verified_at) VALUES (?,?,1,?,?) "
                 "ON CONFLICT(job_id, station) DO UPDATE SET verified=1, verified_by=excluded.verified_by, "
                 "verified_at=excluded.verified_at, version=version+1", (job_id, station, user["id"], now_ist()))
    detail = {"station": station}
    if missing:
        detail.update(missing=missing, missing_reason=missing_reason)
    audit.log(conn, "station.verify", "job", job_id, None, detail, user=user)


def lock(conn, job_id, user, expected_version, missing_reason=None):
    """Lock for review. With a reason, tests not yet verified are verified as they are (missing values stay blank)."""
    job = get_job(conn, job_id)
    if not is_assigned(conn, job_id, user["id"]):
        raise WorkflowError("You are not assigned to this job")
    pending = [s for s in station_status(conn, job) if not s["verified"]]
    if pending and len((missing_reason or "").strip()) >= 5:
        for s in pending:
            set_verified(conn, job_id, s["station"], user, missing_reason)    # still refuses unconfirmed AI values
        job = get_job(conn, job_id)
        expected_version = job["version"]
    elif pending:
        raise WorkflowError("Verify every test first, or lock with missing values and give the reason: "
                            + ", ".join(s["title"] for s in pending))
    workflow.apply(conn, job_id, "lock", user, expected_version, sets={"locked_at": now_ist(), "return_comment": None})
    notify.send(conn, f"Job {job['sample_code']} locked by {user['full_name']} and ready for technical review.",
                f"/admin/requests/{job_id}", role="admin")


# =============================================================================
# admin review -> HOD authorization -> issue
# =============================================================================
def begin_review(conn, job_id, admin):
    job = get_job(conn, job_id)
    if job["status"] == workflow.LOCKED:
        workflow.apply(conn, job_id, "begin_review", admin)


def return_to_engineer(conn, job_id, admin, comment, expected_version):
    if not (comment or "").strip():
        raise WorkflowError("A comment is mandatory when returning a job")
    job = get_job(conn, job_id)
    workflow.apply(conn, job_id, "return", admin, expected_version, sets={"return_comment": comment.strip()},
                   audit_extra={"comment": comment.strip()})
    conn.execute("UPDATE job_tests SET verified=0, version=version+1 WHERE job_id=?", (job_id,))
    for e in engineers_of(conn, job_id):
        notify.send(conn, f"Job {job['sample_code']} returned for corrections: {comment.strip()}",
                    f"/engineer/jobs/{job_id}", user_id=e["user_id"])


def forward_to_hod(conn, job_id, admin, expected_version):
    """Technical review complete: job -> PENDING_HOD (on_submit of the `issue_report` approval)."""
    workflow.apply(conn, job_id, "forward", admin, expected_version)


def hod_reject(conn, job_id, hod, comment):
    workflow.apply(conn, job_id, "hod_reject", hod, None, sets={"return_comment": f"HOD: {comment}"},
                   audit_extra={"comment": comment})


def _sealed_folder(report_no):
    return os.path.join(config.SEALED_DIR, report_no.replace("/", "_"))


def preview(conn, job_id, user, want_pdf=True):
    """Live preview of the filled report with the active template (not sealed). Cached per job+template version."""
    from .reporting import templates as T
    job = get_job(conn, job_id)
    act = T.active(conn)
    snap = report.build_snapshot(conn, job_id, act)
    os.makedirs(config.PREVIEW_DIR, exist_ok=True)
    base = os.path.join(config.PREVIEW_DIR, f"job{job_id}_v{job['version']}_t{act['id'] if act else 0}")
    docx_path, pdf_path = base + ".docx", base + ".pdf"
    if not os.path.exists(docx_path):
        report.render_docx(conn, snap, docx_path, {"reviewer_name": "(HOD authorization pending)",
                                                   "seal": "PREVIEW-NOT-ISSUED",
                                                   "report_date": datetime.now(IST).strftime("%d-%m-%Y")})
    if want_pdf and not os.path.exists(pdf_path):
        report.docx_to_pdf(docx_path, pdf_path)
    return docx_path, (pdf_path if os.path.exists(pdf_path) else None)


def prepare_issue(conn, job_id, hod, verify_base_url=""):
    """
    Outside the write lock: freeze the canonical snapshot (request + readings + rule results + template
    id/version/hash), render DOCX + PDF with the snapshot hash in the footer and QR code.
    """
    from .reporting import templates as T
    job = get_job(conn, job_id)
    if job["status"] != workflow.PENDING_HOD:
        raise WorkflowError("Only jobs awaiting HOD authorization can be issued")
    tpl = T.active(conn)
    snap = report.build_snapshot(conn, job_id, tpl)
    issued_at = now_ist()
    fails = conn.execute("SELECT COUNT(*) FROM test_results WHERE job_id=? AND passed=0", (job_id,)).fetchone()[0]
    snap["issue"] = {"report_no": job["report_no"], "issued_at": issued_at, "authorized_by": hod["username"],
                     "reviewer_name": hod["full_name"], "failed_rules": fails}
    snapshot_json = integrity.canonical_json(snap)
    sha = integrity.sha256_text(snapshot_json)
    tmp = tempfile.mkdtemp(prefix="atrg_issue_")
    docx_tmp, pdf_tmp = os.path.join(tmp, "report.docx"), os.path.join(tmp, "report.pdf")
    report.render_docx(conn, snap, docx_tmp, {"report_date": report.ddmmyyyy(issued_at),
                                              "reviewer_name": hod["full_name"], "seal": sha},
                       verify_url=f"{verify_base_url.rstrip('/')}/verify/{job['report_no']}" if verify_base_url else None)
    pdf_ok = report.docx_to_pdf(docx_tmp, pdf_tmp)
    return {"tmp": tmp, "docx": docx_tmp, "pdf": pdf_tmp if pdf_ok else None, "snapshot": snap,
            "snapshot_json": snapshot_json, "sha": sha, "issued_at": issued_at, "version": job["version"]}


def finalize_issue(conn, job_id, hod, art, forwarded_by=None):
    """Inside BEGIN IMMEDIATE: re-check the data, store sealed files, insert `reports`, set is_sealed=1."""
    job = get_job(conn, job_id)
    workflow.check_version(conn, job, art["version"])
    again = report.build_snapshot(conn, job_id)
    if integrity.canonical_json(again["data"]) != integrity.canonical_json(art["snapshot"]["data"]):
        raise Conflict("Data changed while the report was being generated; reload and approve again.")
    folder = _sealed_folder(job["report_no"])
    os.makedirs(folder, exist_ok=True)
    stem = job["report_no"].replace("/", "_")
    docx_path = os.path.join(folder, f"{stem}.docx")
    pdf_path = os.path.join(folder, f"{stem}.pdf") if art["pdf"] else None
    try:
        shutil.copyfile(art["docx"], docx_path)
        if pdf_path:
            shutil.copyfile(art["pdf"], pdf_path)
        with open(os.path.join(folder, "snapshot.json"), "w", encoding="utf-8") as fh:
            fh.write(art["snapshot_json"])
        amends = None
        if job["amends_id"]:
            r = conn.execute("SELECT id FROM reports WHERE job_id=? ORDER BY id DESC LIMIT 1", (job["amends_id"],)).fetchone()
            amends = r["id"] if r else None
        t = art["snapshot"].get("template") or {}
        cur = conn.execute(
            "INSERT INTO reports(job_id,report_no,kind,amends_report_id,snapshot_json,sha256_snapshot,sha256_docx,sha256_pdf,"
            "docx_path,pdf_path,template_id,template_version,template_sha,issued_by,forwarded_by,issued_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, job["report_no"], "amended" if job["amends_id"] else "original", amends, art["snapshot_json"],
             art["sha"], integrity.sha256_file(docx_path), integrity.sha256_file(pdf_path) if pdf_path else None,
             docx_path, pdf_path, t.get("id"), t.get("version"), t.get("sha256"), hod["id"], forwarded_by,
             art["issued_at"]))
        workflow.apply(conn, job_id, "issue", hod, art["version"],
                       sets={"issued_at": art["issued_at"], "is_sealed": 1, "template_id": t.get("id")},
                       audit_extra={"report_no": job["report_no"], "sha256_snapshot": art["sha"], "template": t})
    except Exception:
        for p in (docx_path, pdf_path):
            if p and os.path.exists(p):
                os.chmod(p, 0o666)
                os.remove(p)
        raise
    notify.send(conn, f"Test report {job['report_no']} has been issued.", f"/customer/requests/{job_id}",
                user_id=job["customer_id"])
    return cur.lastrowid, folder


def seal_files(folder, rep):
    for p in (rep["docx_path"], rep["pdf_path"], os.path.join(folder, "snapshot.json")):
        if p and os.path.exists(p):
            integrity.make_read_only(p)


def issue(conn, job_id, hod, verify_base_url="", forwarded_by=None):
    """Convenience (seed / tests): prepare + finalize in one call. The HOD portal goes through approvals.decide()."""
    art = prepare_issue(conn, job_id, hod, verify_base_url)
    try:
        with tx(conn):
            rid, folder = finalize_issue(conn, job_id, hod, art, forwarded_by)
    finally:
        shutil.rmtree(art["tmp"], ignore_errors=True)
    rep = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    seal_files(folder, rep)
    return rep


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
        "INSERT INTO requests(customer_id,ref,status,sample_code,report_no,amends_id,amendment_no,form_json,tests_json,"
        "condition_json,wi_json,created_at,accepted_at,accepted_by,assigned_at,updated_by,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (orig["customer_id"], f"{root['ref']}/A{n}", workflow.ASSIGNED, orig["sample_code"], report_no, root_id, n,
         orig["form_json"], orig["tests_json"], orig["condition_json"], json.dumps(wi), ts, ts, admin["id"], ts,
         admin["id"], ts))
    new_id = cur.lastrowid
    for e in engineers_of(conn, job_id):
        conn.execute("INSERT INTO job_engineers(job_id,user_id,role,station,assigned_by,assigned_at) VALUES (?,?,?,?,?,?)",
                     (new_id, e["user_id"], e["role"], e["station"], admin["id"], ts))
    for r in conn.execute("SELECT * FROM readings WHERE job_id=?", (job_id,)).fetchall():
        conn.execute("INSERT INTO readings(job_id,station,key,imported_value,current_value,unit,source,source_file_id,"
                     "source_cell,ai_confidence,verified_by,verified_at,updated_by,updated_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (new_id, r["station"], r["key"], r["imported_value"], r["current_value"], r["unit"], r["source"],
                      r["source_file_id"], r["source_cell"], r["ai_confidence"], r["verified_by"], r["verified_at"],
                      admin["id"], ts))
    for st in job_stations(orig):
        conn.execute("INSERT INTO job_tests(job_id, station) VALUES (?,?)", (new_id, st))
    audit.log(conn, "job.amend", "job", new_id, {"original_job": job_id, "original_report": orig["report_no"]},
              {"report_no": report_no, "reason": reason.strip()}, user=admin)
    recompute(conn, new_id)
    return new_id
