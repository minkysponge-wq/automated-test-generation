"""
Generic HOD approval engine.

An admin action that needs HOD sign-off is stored as an `approval_requests` row
and takes effect only when the HOD approves it. Each action type registers:

    validate(conn, payload)                       -> raise WorkflowError if the action is no longer valid
    prepare(conn, payload, approval, hod)         -> artifacts   (optional, runs OUTSIDE the write lock -
                                                                   e.g. report generation takes seconds)
    apply(conn, payload, approval, hod, artifacts)-> result dict (runs inside BEGIN IMMEDIATE)
    on_submit(conn, payload, requester)           -> side effect when the request is filed (e.g. job -> PENDING_HOD)
    on_reject(conn, payload, approval, hod, comment)

`always=True` types always need the HOD. Optional types are switched on in the HOD settings
(settings key `approval_<type>`); when off, `request_or_apply()` applies the action directly.
"""
import json
from dataclasses import dataclass
from typing import Callable, Optional

from . import audit, notify
from .db import get_settings, now_ist, tx
from .workflow import Conflict, WorkflowError


@dataclass
class Spec:
    type: str
    label: str
    always: bool
    apply: Callable
    validate: Optional[Callable] = None
    prepare: Optional[Callable] = None
    on_submit: Optional[Callable] = None
    on_reject: Optional[Callable] = None
    entity_type: str = "job"


REGISTRY = {}


def register(type, label, apply, always=False, **kw):
    REGISTRY[type] = Spec(type=type, label=label, always=always, apply=apply, **kw)


def optional_types():
    return [s for s in REGISTRY.values() if not s.always]


def required(conn, type):
    spec = REGISTRY[type]
    return spec.always or bool(get_settings(conn).get(f"approval_{type}", False))


def pending_for(conn, entity_type, entity_id):
    return conn.execute("SELECT * FROM approval_requests WHERE status='pending' AND entity_type=? AND entity_id=? "
                        "ORDER BY id", (entity_type, str(entity_id))).fetchall()


def submit(conn, type, payload, requester, entity_id=None, summary=None):
    """File an approval request (caller holds the transaction)."""
    spec = REGISTRY[type]
    if entity_id is not None and conn.execute(
            "SELECT 1 FROM approval_requests WHERE status='pending' AND type=? AND entity_type=? AND entity_id=?",
            (type, spec.entity_type, str(entity_id))).fetchone():
        raise WorkflowError(f"A '{spec.label}' request for this record is already waiting for the HOD.")
    if spec.on_submit:                      # e.g. job -> PENDING_HOD; then the request must be valid
        spec.on_submit(conn, payload, requester)
    if spec.validate:
        spec.validate(conn, payload)
    cur = conn.execute(
        "INSERT INTO approval_requests(type, entity_type, entity_id, summary, payload_json, requested_by, requested_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (type, spec.entity_type, None if entity_id is None else str(entity_id), summary or spec.label,
         json.dumps(payload), requester["id"] if requester else None, now_ist()))
    aid = cur.lastrowid
    audit.log(conn, "approval.request", "approval", aid, None, {"type": type, "entity": entity_id, "summary": summary},
              user=requester)
    notify.send(conn, f"Approval needed: {summary or spec.label}", f"/hod/approvals/{aid}", role="hod")
    return aid


def request_or_apply(conn, type, payload, requester, entity_id=None, summary=None):
    """Admin entry point. Returns ('pending', approval_id) or ('applied', result). Manages its own transactions."""
    if required(conn, type):
        with tx(conn):
            return "pending", submit(conn, type, payload, requester, entity_id, summary)
    spec = REGISTRY[type]
    artifacts = spec.prepare(conn, payload, None, requester) if spec.prepare else None
    with tx(conn):
        if spec.validate:
            spec.validate(conn, payload)
        result = spec.apply(conn, payload, None, requester, artifacts)
    return "applied", result


def decide(conn, approval_id, hod, approve, comment=""):
    """HOD decision. Approve -> prepare (no lock) -> apply (BEGIN IMMEDIATE). Reject needs a comment."""
    ap = conn.execute("SELECT * FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
    if ap is None:
        raise WorkflowError("Approval request not found")
    if ap["status"] != "pending":
        raise WorkflowError(f"This request is already {ap['status']}.")
    spec = REGISTRY[ap["type"]]
    payload = json.loads(ap["payload_json"])
    requester = conn.execute("SELECT * FROM users WHERE id=?", (ap["requested_by"],)).fetchone()
    if not approve:
        if not (comment or "").strip():
            raise WorkflowError("A comment is required when rejecting a request.")
        with tx(conn):
            _set(conn, ap, "rejected", hod, comment)
            if spec.on_reject:
                spec.on_reject(conn, payload, ap, hod, comment.strip())
            audit.log(conn, "approval.reject", "approval", approval_id, {"status": "pending"},
                      {"status": "rejected", "type": ap["type"], "comment": comment.strip()}, user=hod)
            if requester:
                notify.send(conn, f"HOD rejected: {ap['summary']} - {comment.strip()}", None, user_id=requester["id"])
        return None
    try:
        if spec.validate:
            spec.validate(conn, payload)
        artifacts = spec.prepare(conn, payload, ap, hod) if spec.prepare else None
        with tx(conn):
            ap2 = conn.execute("SELECT status, version FROM approval_requests WHERE id=?", (approval_id,)).fetchone()
            if ap2["status"] != "pending" or ap2["version"] != ap["version"]:
                raise Conflict("This approval request was decided by someone else; reload.")
            result = spec.apply(conn, payload, ap, hod, artifacts) or {}
            _set(conn, ap, "approved", hod, comment, result)
            audit.log(conn, "approval.approve", "approval", approval_id, {"status": "pending"},
                      {"status": "approved", "type": ap["type"], "result": result}, user=hod)
            if requester:
                notify.send(conn, f"HOD approved: {ap['summary']}", None, user_id=requester["id"])
        return result
    except (WorkflowError, Conflict) as exc:
        with tx(conn):
            conn.execute("UPDATE approval_requests SET comment=?, version=version+1 WHERE id=? AND status='pending'",
                         (f"Could not apply: {exc}", approval_id))
            audit.log(conn, "approval.apply_failed", "approval", approval_id, None, {"error": str(exc)}, user=hod)
        raise


def _set(conn, ap, status, hod, comment, result=None):
    conn.execute("UPDATE approval_requests SET status=?, decided_by=?, decided_at=?, comment=?, result_json=?, "
                 "version=version+1 WHERE id=?",
                 (status, hod["id"], now_ist(), (comment or "").strip() or None,
                  json.dumps(result, default=str) if result is not None else None, ap["id"]))


def cancel(conn, approval_id, user):
    with tx(conn):
        conn.execute("UPDATE approval_requests SET status='cancelled', decided_by=?, decided_at=?, version=version+1 "
                     "WHERE id=? AND status='pending'", (user["id"], now_ist(), approval_id))
        audit.log(conn, "approval.cancel", "approval", approval_id, None, None, user=user)
