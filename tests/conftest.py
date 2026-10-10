"""
Test fixtures: every test session runs against a throw-away database and
storage folder (set through environment variables BEFORE config is imported).
"""
import os
import sys
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="atrg_test_")
os.environ["ATRG_DB"] = os.path.join(_TMP, "test.db")
os.environ["ATRG_STORAGE"] = os.path.join(_TMP, "storage")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from app import db, report  # noqa: E402


@pytest.fixture(scope="session")
def tmpdir_session():
    return _TMP


@pytest.fixture(scope="session")
def seeded():
    """Demo users + one fully issued demo job (PDF conversion skipped to keep tests fast)."""
    import seed
    db.init_db()
    conn = db.connect()
    seed.ensure_users(conn)
    original = report.docx_to_pdf
    report.docx_to_pdf = lambda d, p: None
    try:
        seed.make_history(conn)
    finally:
        report.docx_to_pdf = original
    job = conn.execute("SELECT * FROM requests WHERE report_no='CPRIBLRSCL25T1654'").fetchone()
    yield {"conn": conn, "job_id": job["id"]}
    conn.close()


@pytest.fixture()
def conn(seeded):
    c = db.connect()
    yield c
    c.close()


@pytest.fixture()
def app(seeded):
    from app import create_app
    return create_app({"TESTING_DISABLE_CSRF": True, "TESTING": True})


@pytest.fixture()
def client(app):
    return app.test_client()


def login(client, role, username, password):
    r = client.post(f"/{role}/login", data={"username": username, "password": password})
    assert r.status_code == 302, r.data[:200]
    return client
