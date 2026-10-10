"""
Test fixtures: every test session runs against a throw-away database and
storage folder (set through environment variables BEFORE config is imported).
PDF conversion is switched off so the suite runs without Word / LibreOffice.
"""
import os
import sys
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="atrg_test_")
os.environ["ATRG_DB"] = os.path.join(_TMP, "test.db")
os.environ["ATRG_STORAGE"] = os.path.join(_TMP, "storage")
os.environ["AI_ENABLED"] = os.environ.get("ATRG_TEST_AI", "false")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
from app import db, report  # noqa: E402

report.docx_to_pdf = lambda d, p: None      # keep tests fast and independent of MS Word / LibreOffice


@pytest.fixture(scope="session")
def seeded():
    """Demo accounts, template v1, one issued job and one job in technical review."""
    import seed
    db.init_db()
    conn = db.connect()
    seed.ensure_users(conn)
    seed.ensure_template(conn)
    seed.make_history(conn)
    issued = conn.execute("SELECT id FROM requests WHERE report_no='CPRIBLRSCL25T1654'").fetchone()["id"]
    review = conn.execute("SELECT id FROM requests WHERE report_no='CPRIBLRSCL25T1655'").fetchone()["id"]
    yield {"conn": conn, "job_id": issued, "review_job_id": review}
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


def user(conn, username):
    return dict(conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone())


def login(client, role, username, password):
    r = client.post(f"/{role}/login", data={"username": username, "password": password})
    assert r.status_code == 302, r.data[:300]
    return client
