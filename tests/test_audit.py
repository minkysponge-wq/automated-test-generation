"""Hash-chained audit log: append-only and tamper-evident."""
import sqlite3

import pytest

from app import audit, db


def test_chain_verifies(seeded, conn):
    audit.log(conn, "test.event", "test", 1, {"a": 1}, {"a": 2},
              user={"id": None, "username": "pytest", "role": "admin"})
    res = audit.verify_chain(conn)
    assert res["ok"] and res["rows"] > 10      # the seeded workflow logged plenty of rows


def test_audit_rows_cannot_be_changed(seeded, conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE audit_log SET action='x' WHERE id=1")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM audit_log WHERE id=1")


def test_tampering_breaks_the_chain(tmp_path):
    """Even if someone bypasses the triggers (drops them), the hash chain exposes the edit."""
    path = str(tmp_path / "chain.db")
    db.init_db(path)
    c = db.connect(path)
    u = {"id": 1, "username": "x", "role": "admin"}
    for i in range(5):
        audit.log(c, f"evt{i}", "t", i, None, {"i": i}, user=u)
    assert audit.verify_chain(c)["ok"]
    c.execute("DROP TRIGGER trg_audit_no_upd")
    c.execute("UPDATE audit_log SET after_json='{\"i\": 99}' WHERE id=3")
    res = audit.verify_chain(c)
    assert not res["ok"] and res["bad_id"] == 3
    c.close()


def test_logins_are_logged(client, conn):
    client.post("/admin/login", data={"username": "admin", "password": "wrong"})
    client.post("/admin/login", data={"username": "admin", "password": "admin12345"})
    acts = [r["action"] for r in conn.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 2")]
    assert acts == ["login.success", "login.fail"]
