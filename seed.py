"""
Create demo accounts and (optionally) a fully issued historical record.

    python seed.py              # accounts + counters + issued demo report (HVD25S0847 / CPRIBLRSCL25T1654)
    python seed.py --no-history # accounts + counters only
    python seed.py --reset      # wipe database and storage first

Demo accounts
    admin / admin123        Test In-charge (HOD)
    engineer1 / eng123      approved test engineer
    engineer2 / eng234      engineer awaiting approval (to demo the approval step)
    customer1 / cust123     A.P. Transformers
"""
import argparse
import os
import shutil
import stat
import sys

import config
from app import auth, db, excel_io, fields as F, jobs, request_form, workflow
from app.db import tx

USERS = [
    ("admin", "admin123", "admin", "Dr. R. Krishnamurthy (HOD, SCL)", "hod.scl@cpri.example", "080-22072365", "", "", "active"),
    ("engineer1", "eng123", "engineer", "P. Naveenkumar", "naveen@cpri.example", "080-22072301", "", "", "active"),
    ("engineer2", "eng234", "engineer", "S. Uttej", "uttej@cpri.example", "080-22072302", "", "", "pending"),
    ("customer1", "cust123", "customer", "Vivekanand Krishna", "qa@aptransformers.example", "044-26250000",
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
    for d in (config.UPLOAD_DIR, config.REPORT_DIR, config.SEALED_DIR, config.PREVIEW_DIR):
        _rm(d)
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, ".gitkeep"), "w").close()
    print("database and storage wiped")


def ensure_users(conn):
    ids = {}
    with tx(conn):
        for u, pw, role, name, email, phone, company, addr, status in USERS:
            row = conn.execute("SELECT id FROM users WHERE username=?", (u,)).fetchone()
            if row:
                ids[u] = row["id"]
                continue
            ids[u] = auth.register_user(conn, role, {"username": u, "password": pw, "full_name": name, "email": email,
                                                     "phone": phone, "company": company, "address": addr}, status)
        # counters: next numbers look like the real register (…S0847 / …T1654)
        for name, year, value in (("sample_code", 2026, 846), ("report_no", 2026, 1653)):
            conn.execute("INSERT OR IGNORE INTO counters(name, year, value) VALUES (?,?,?)", (name, year, value))
    return ids


def users(conn):
    return {r["username"]: dict(r) for r in conn.execute("SELECT * FROM users")}


def make_history(conn):
    """Run the real workflow once for the 2025 demo transformer so the dashboard and search have an issued report."""
    if conn.execute("SELECT 1 FROM requests WHERE report_no='CPRIBLRSCL25T1654'").fetchone():
        print("historical demo report already present")
        return
    U = users(conn)
    cust, admin, eng = U["customer1"], U["admin"], U["engineer1"]
    with tx(conn):
        conn.execute("INSERT OR REPLACE INTO counters(name, year, value) VALUES ('sample_code', 2025, 846)")
        conn.execute("INSERT OR REPLACE INTO counters(name, year, value) VALUES ('report_no', 2025, 1653)")
        job_id = jobs.create_request(conn, cust, request_form.demo_form(), list(F.tests()))
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
            jobs.store_import(conn, job_id, st, fid, res, eng)
    with tx(conn):
        for st in jobs.job_stations(job):
            jobs.set_verified(conn, job_id, st, eng)
        jobs.lock(conn, job_id, eng, None)
    with tx(conn):
        jobs.begin_review(conn, job_id, admin)
    rep = jobs.issue(conn, job_id, admin, None, f"http://localhost:{config.PORT}")
    print(f"issued historical demo report {rep['report_no']} (seal {rep['sha256_snapshot'][:12]}), PDF: {bool(rep['pdf_path'])}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="delete the database and stored files first")
    ap.add_argument("--no-history", action="store_true", help="do not create the issued demo report")
    a = ap.parse_args()
    if a.reset:
        reset()
    db.init_db()
    conn = db.connect()
    ensure_users(conn)
    print("demo accounts ready: admin/admin123, engineer1/eng123, engineer2/eng234 (pending), customer1/cust123")
    if not a.no_history:
        make_history(conn)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())
