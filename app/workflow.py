"""
Server-side state machine for a test request / job.

    SUBMITTED -> ACCEPTED -> ASSIGNED -> IN_TESTING -> LOCKED_BY_ENGINEER -> UNDER_REVIEW -> ISSUED
         \-> REJECTED                        ^                |                  |
                                             +-- RETURNED <---+------------------+

The UI only hides buttons; every state change goes through `apply()` here,
which checks (1) the job is not sealed, (2) the optimistic-lock version,
(3) the transition is legal for the current state, (4) the actor's role.
"""
import json

from . import audit
from .db import now_ist

SUBMITTED, ACCEPTED, ASSIGNED, IN_TESTING = "SUBMITTED", "ACCEPTED", "ASSIGNED", "IN_TESTING"
LOCKED, UNDER_REVIEW, ISSUED = "LOCKED_BY_ENGINEER", "UNDER_REVIEW", "ISSUED"
RETURNED, REJECTED = "RETURNED", "REJECTED"

STATE_LABELS = {
    SUBMITTED: "Submitted",
    ACCEPTED: "Accepted - sample code issued",
    ASSIGNED: "Engineer assigned",
    IN_TESTING: "Testing",
    LOCKED: "Engineer verified & locked",
    UNDER_REVIEW: "Under admin review",
    ISSUED: "Report issued",
    RETURNED: "Returned to engineer",
    REJECTED: "Rejected",
}
# The happy path shown as a timeline to the customer.
TIMELINE = [SUBMITTED, ACCEPTED, ASSIGNED, IN_TESTING, LOCKED, UNDER_REVIEW, ISSUED]

# action -> (allowed from-states, to-state or None=unchanged, roles allowed)
TRANSITIONS = {
    "accept":        ({SUBMITTED}, ACCEPTED, {"admin"}),
    "reject":        ({SUBMITTED}, REJECTED, {"admin"}),
    "assign":        ({ACCEPTED, ASSIGNED, IN_TESTING, RETURNED}, None, {"admin"}),
    "start_testing": ({ASSIGNED, RETURNED}, IN_TESTING, {"engineer"}),
    "edit":          ({ASSIGNED, IN_TESTING, RETURNED}, None, {"engineer"}),
    "lock":          ({IN_TESTING}, LOCKED, {"engineer"}),
    "begin_review":  ({LOCKED}, UNDER_REVIEW, {"admin"}),
    "return":        ({LOCKED, UNDER_REVIEW}, RETURNED, {"admin"}),
    "issue":         ({UNDER_REVIEW}, ISSUED, {"admin"}),
}
# states in which engineers may change readings
EDITABLE = {ASSIGNED, IN_TESTING, RETURNED}


class WorkflowError(Exception):
    """Illegal transition / permission problem -> HTTP 400/403."""
    status = 400


class Conflict(Exception):
    """Optimistic-lock version mismatch -> HTTP 409."""
    status = 409


def can(action, state, role):
    if action not in TRANSITIONS:
        return False
    froms, _to, roles = TRANSITIONS[action]
    return state in froms and role in roles


def conflict_message(conn, job):
    who = "another user"
    if job["updated_by"]:
        u = conn.execute("SELECT full_name, username FROM users WHERE id=?", (job["updated_by"],)).fetchone()
        if u:
            who = f"{u['full_name']} ({u['username']})"
    return f"This record was changed by {who} at {job['updated_at'] or '?'}; reload."


def check_version(conn, job, expected_version):
    if expected_version is not None and str(job["version"]) != str(expected_version):
        raise Conflict(conflict_message(conn, job))


def apply(conn, job_id, action, user, expected_version=None, sets=None, audit_extra=None):
    """
    Perform `action` on job `job_id`. Must run inside db.tx().
    `sets` = extra columns to update in the same statement.
    Returns the updated row.
    """
    if not conn.in_transaction:
        raise RuntimeError("workflow.apply() must run inside db.tx()")
    job = conn.execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()
    if job is None:
        raise WorkflowError("Job not found")
    if job["is_sealed"]:
        raise WorkflowError("This job is sealed - the report has been issued. Create an amendment instead.")
    check_version(conn, job, expected_version)
    if action not in TRANSITIONS:
        raise WorkflowError(f"Unknown action {action}")
    froms, to, roles = TRANSITIONS[action]
    if user["role"] not in roles:
        err = WorkflowError(f"Role '{user['role']}' may not perform '{action}'")
        err.status = 403
        raise err
    if job["status"] not in froms:
        raise WorkflowError(f"Cannot {action.replace('_', ' ')} a job in state {STATE_LABELS.get(job['status'], job['status'])}")
    new_status = to or job["status"]
    if action == "assign" and job["status"] == ACCEPTED:
        new_status = ASSIGNED
    cols = {"status": new_status, "updated_by": user["id"], "updated_at": now_ist()}
    cols.update(sets or {})
    assignments = ", ".join(f"{c}=?" for c in cols) + ", version=version+1"
    conn.execute(f"UPDATE requests SET {assignments} WHERE id=?", (*cols.values(), job_id))
    if action != "edit":
        after = {"status": new_status}
        after.update(audit_extra or {})
        audit.log(conn, f"job.{action}", "job", job_id, {"status": job["status"]}, after, user=user)
    return conn.execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()


def touch(conn, job_id, user, expected_version=None):
    """Bump the job version after an engineer edit (keeps optimistic locking honest)."""
    return apply(conn, job_id, "edit", user, expected_version)


def tests_of(job):
    return json.loads(job["tests_json"] or "[]")
