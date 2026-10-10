"""
Append-only, hash-chained activity log.

    row_hash = sha256(prev_hash + canonical_json(row_content))

Because every row includes the hash of the previous one, editing or deleting
any historic row breaks the chain from that point on; `verify_chain()` finds it.
SQLite triggers additionally refuse UPDATE/DELETE on the table.
"""
import hashlib
import json

from .db import now_ist, tx

GENESIS = "0" * 64
_FIELDS = ("ts", "user_id", "username", "role", "action", "entity_type", "entity_id",
           "before_json", "after_json", "ip", "user_agent")


def _dumps(obj):
    if obj is None:
        return None
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _row_hash(prev_hash, content):
    payload = prev_hash + json.dumps([content[f] for f in _FIELDS], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _request_context():
    """Who/where from the current Flask request, if there is one."""
    try:
        from flask import g, has_request_context, request
        if not has_request_context():
            return None, None, None
        user = getattr(g, "user", None)
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        return user, ip, (request.user_agent.string or "")[:200]
    except Exception:  # pragma: no cover - outside Flask
        return None, None, None


def log(conn, action, entity_type=None, entity_id=None, before=None, after=None, user=None):
    """Append one audit row. Joins the caller's transaction if one is open."""
    ctx_user, ip, ua = _request_context()
    user = user or ctx_user
    content = {
        "ts": now_ist(),
        "user_id": user["id"] if user else None,
        "username": user["username"] if user else None,
        "role": user["role"] if user else None,
        "action": action,
        "entity_type": entity_type,
        "entity_id": None if entity_id is None else str(entity_id),
        "before_json": _dumps(before),
        "after_json": _dumps(after),
        "ip": ip,
        "user_agent": ua,
    }

    def _insert():
        row = conn.execute("SELECT row_hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
        prev = row["row_hash"] if row else GENESIS
        conn.execute(
            "INSERT INTO audit_log(ts,user_id,username,role,action,entity_type,entity_id,"
            "before_json,after_json,ip,user_agent,prev_hash,row_hash) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(content[f] for f in _FIELDS) + (prev, _row_hash(prev, content)),
        )

    if conn.in_transaction:
        _insert()            # caller holds BEGIN IMMEDIATE -> chain order is safe
    else:
        with tx(conn):
            _insert()


def verify_chain(conn):
    """Walk the whole log. Returns dict(ok, rows, bad_id)."""
    prev = GENESIS
    n = 0
    for row in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        n += 1
        content = {f: row[f] for f in _FIELDS}
        if row["prev_hash"] != prev or _row_hash(prev, content) != row["row_hash"]:
            return {"ok": False, "rows": n, "bad_id": row["id"]}
        prev = row["row_hash"]
    return {"ok": True, "rows": n, "bad_id": None}
