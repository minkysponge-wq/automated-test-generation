"""
Small JSON API used by the pages: job status polling (every 10 s) and AI task polling.
"""
from flask import Blueprint, abort, jsonify

from .. import auth, jobs, workflow
from ..ai import client as ai
from ..db import get_db

bp = Blueprint("api", __name__)


def _any_user():
    for role in auth.ROLES:
        u = auth.load_user(role)
        if u:
            yield u


@bp.route("/jobs/<int:job_id>/status")
def job_status(job_id):
    """Visible to the owning customer, an assigned engineer, any admin or HOD."""
    conn = get_db()
    job = conn.execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()
    if job is None:
        abort(404)
    allowed = False
    for u in _any_user():
        if u["role"] in ("admin", "hod"):
            allowed = True
        elif u["role"] == "engineer" and jobs.is_assigned(conn, job_id, u["id"]):
            allowed = True
        elif u["role"] == "customer" and u["id"] == job["customer_id"]:
            allowed = True
    if not allowed:
        abort(403)
    who = conn.execute("SELECT full_name, username FROM users WHERE id=?", (job["updated_by"],)).fetchone()
    pending = conn.execute("SELECT type FROM approval_requests WHERE status='pending' AND entity_type='job' AND entity_id=?",
                           (str(job_id),)).fetchall()
    return jsonify(ok=True, id=job_id, status=job["status"], label=workflow.STATE_LABELS.get(job["status"]),
                   version=job["version"], sealed=bool(job["is_sealed"]), locked=job["status"] not in workflow.EDITABLE,
                   updated_by=(who["full_name"] if who else None), updated_at=job["updated_at"],
                   pending_approvals=[p["type"] for p in pending])


@bp.route("/ai/tasks/<int:task_id>")
def ai_task(task_id):
    conn = get_db()
    t = ai.task(conn, task_id)
    if t is None:
        abort(404)
    if not any(u["id"] == t["created_by"] or u["role"] in ("admin", "hod") for u in _any_user()):
        abort(403)
    return jsonify(ok=True, id=task_id, status=t["status"], error=t["error"], result=t["result"])
