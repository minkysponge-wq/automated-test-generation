"""
Create demo accounts, register report template v1 and (optionally) demo jobs.

    python seed.py --reset       # wipe database and storage first (recommended before a demo)
    python seed.py               # add missing accounts / template / demo jobs
    python seed.py --no-history  # accounts + template only

Demo accounts
    hod / hod12345            Head of Division (approves admin actions, authorizes reports)
    admin / admin12345        Test In-charge
    admin2 / admin23456       Test In-charge waiting for HOD approval (demo of the approval chain)
    engineer1 / eng12345      approved test engineer
    engineer2 / eng23456      engineer waiting for admin approval
    customer1 / cust12345     A.P. Transformers

Demo jobs
    CPRIBLRSCL25T1654 / HVD25S0847   issued and sealed (history, search, verification)
    CPRIBLRSCL25T1655 / HVD25S0848   locked by the engineer and in technical review
"""
import argparse
import os
import shutil
import stat
import sys

import config
from app import approval_types  # noqa: F401
from app import approvals, auth, db, excel_io, fields as F, jobs, request_form, workflow
from app.db import tx
from app.reporting import templates as T

USERS = [
    ("hod", "hod12345", "hod", "Dr. R. Krishnamurthy", "hod.scl@cpri.example", "080-22072365", "", "", "active"),
    ("admin", "admin12345", "admin", "S. Prakash (Test In-charge)", "incharge.scl@cpri.example", "080-22072300", "", "",
     "active"),
    ("admin2", "admin23456", "admin", "M. Lakshmi", "lakshmi@cpri.example", "080-22072303", "", "", "pending"),
    ("engineer1", "eng12345", "engineer", "P. Naveenkumar", "naveen@cpri.example", "080-22072301", "", "", "active"),
    ("engineer2", "eng23456", "engineer", "S. Uttej", "uttej@cpri.example", "080-22072302", "", "", "pending"),
    ("customer1", "cust12345", "customer", "Vivekanand Krishna", "qa@aptransformers.example", "044-26250000",
     "M/s. A.P. Transformers", "SP24 & 95H/1(SP), Ambattur Indl. Estate, Chennai-600058", "active"),
]


def _rm(path):
    def onerr(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=onerr)


def reset():
    for p in (config.DB_PATH, config.DB_PATH + "-wal", config.DB_PATH + "-shm"):
        if os.path.exists(p):
            os.remove(p)
    for d in (config.UPLOAD_DIR, config.REPORT_DIR, config.SEALED_DIR, config.PREVIEW_DIR,
              os.path.join(config.STORAGE_DIR, "templates")):
        _rm(d)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, ".gitkeep"), "w").close()
    F.invalidate()
    print("database and storage wiped")


def ensure_users(conn):
    with tx(conn):
        for u, pw, role, name, email, phone, company, addr, status in USERS:
            if conn.execute("SELECT 1 FROM users WHERE username=?", (u,)).fetchone():
                continue
            uid = auth.register_user(conn, role, {"username": u, "password": pw, "full_name": name, "email": email,
                                                  "phone": phone, "company": company, "address": addr}, status)
            if role == "admin" and status == "pending":
                approvals.submit(conn, "admin_account", {"user_id": uid}, {"id": uid, "username": u, "role": role},
                                 entity_id=uid, summary=f"New Test In-charge account: {name} ({u})")
        # the next live numbers look like the real register (...S0847 / ...T1654)
        for name, year, value in (("sample_code", 2026, 846), ("report_no", 2026, 1653)):
            conn.execute("INSERT OR IGNORE INTO counters(name, year, value) VALUES (?,?,?)", (name, year, value))


def users(conn):
    return {r["username"]: dict(r) for r in conn.execute("SELECT * FROM users")}


def ensure_template(conn):
    with tx(conn):
        tid = T.register_repo_v1(conn)
    if tid:
        print("report template v1 registered and active")


def _job_to_review(conn, year_counter_start):
    """Run the real workflow for the demo transformer up to the technical review. Returns job id."""
    U = users(conn)
    cust, admin, eng = U["customer1"], U["admin"], U["engineer1"]
    with tx(conn):
        job_id, _ref = jobs.create_request(conn, cust, request_form.demo_form(), list(F.tests()))
    with tx(conn):
        jobs.accept(conn, job_id, admin, {"physical_condition": "Suitable", "lab_capability": "Yes",
                                          "remarks": "Received in good condition", "checked_by": admin["full_name"]},
                    {"date_of_test": "28-10-2025", "deviation": "Nil"}, None, year=2025)
    with tx(conn):
        jobs.assign(conn, job_id, admin, eng["id"], [], None)
    job = jobs.get_job(conn, job_id)
    from werkzeug.datastructures import FileStorage
    for st in jobs.job_stations(job):
        path = os.path.join(config.SAMPLE_DATA_DIR, F.stations()[st]["file"])
        res = excel_io.import_workbook(path, st, workflow.tests_of(job), job["sample_code"])
        assert res.ok, res.errors
        with open(path, "rb") as fh, tx(conn):
            fid = jobs.save_upload(conn, FileStorage(stream=fh, filename=os.path.basename(path)), job_id, "logsheet",
                                   eng, config.ALLOWED_EXCEL_EXT, st)
            jobs.store_import(conn, job_id, st, fid, res.values, eng)
    with tx(conn):
        for st in jobs.job_stations(job):
            jobs.set_verified(conn, job_id, st, eng)
        jobs.lock(conn, job_id, eng, None)
    with tx(conn):
        jobs.begin_review(conn, job_id, admin)
    return job_id


def make_history(conn, issue_pdf=True):
    if conn.execute("SELECT 1 FROM requests WHERE report_no='CPRIBLRSCL25T1654'").fetchone():
        print("demo jobs already present")
        return
    with tx(conn):
        conn.execute("INSERT OR REPLACE INTO counters(name, year, value) VALUES ('sample_code', 2025, 846)")
        conn.execute("INSERT OR REPLACE INTO counters(name, year, value) VALUES ('report_no', 2025, 1653)")
    U = users(conn)
    job_id = _job_to_review(conn, 846)
    with tx(conn):
        aid = approvals.submit(conn, "issue_report", {"job_id": job_id, "version": None,
                                                      "verify_base": f"http://localhost:{config.PORT}"},
                               U["admin"], job_id, "Authorize and issue test report CPRIBLRSCL25T1654")
    approvals.decide(conn, aid, U["hod"], True, "Authorized")
    rep = conn.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
    print(f"issued demo report {rep['report_no']} (seal {rep['sha256_snapshot'][:12]}, PDF: {bool(rep['pdf_path'])})")
    job2 = _job_to_review(conn, 847)
    print(f"second demo job {jobs.get_job(conn, job2)['report_no']} is in technical review")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="delete the database and stored files first")
    ap.add_argument("--no-history", action="store_true", help="do not create the demo jobs")
    a = ap.parse_args()
    if a.reset:
        reset()
    db.init_db()
    conn = db.connect()
    ensure_users(conn)
    ensure_template(conn)
    print("accounts: hod/hod12345, admin/admin12345, engineer1/eng12345, customer1/cust12345 "
          "(pending: admin2/admin23456, engineer2/eng23456)")
    if not a.no_history:
        make_history(conn)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
