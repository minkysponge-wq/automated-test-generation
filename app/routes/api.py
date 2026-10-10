"""
Small JSON API used by the pages (status polling every 10 s).
"""
from flask import Blueprint, abort, jsonify

from .. import auth, jobs, workflow
from ..db import get_db

bp = Blueprint("api", __name__)


@bp.route("/jobs/<int:job_id>/status")
def job_status(job_id):
    """Who can see it: the owning customer, an assigned engineer, or any admin (whichever session is present)."""
    conn = get_db()
    job = conn.execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()
    if job is None:
        abort(404)
    allowed = False
    if auth.load_user("admin"):
        allowed = True
    eng = auth.load_user("engineer")
    if eng and jobs.is_assigned(conn, job_id, eng["id"]):
        allowed = True
    cust = auth.load_user("customer")
    if cust and cust["id"] == job["customer_id"]:
        allowed = True
    if not allowed:
        abort(403)
    who = conn.execute("SELECT full_name, username FROM users WHERE id=?", (job["updated_by"],)).fetchone()
    return jsonify(
        ok=True, id=job_id, status=job["status"], label=workflow.STATE_LABELS.get(job["status"]),
        version=job["version"], sealed=bool(job["is_sealed"]),
        locked=job["status"] not in workflow.EDITABLE,
        updated_by=(who["full_name"] if who else None), updated_at=job["updated_at"],
    )
