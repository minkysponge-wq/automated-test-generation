"""
Landing page, public report verification (the QR code on each report points here) and
the page images of uploaded scans (shown next to AI-filled forms).
"""
import os
import re

from flask import Blueprint, abort, render_template, send_from_directory

from .. import audit, auth, integrity
from ..ai.intake import PAGES_DIR
from ..db import get_db

bp = Blueprint("public", __name__)


@bp.route("/")
def landing():
    return render_template("landing.html")


@bp.route("/verify/<path:report_no>")
def verify(report_no):
    """Anyone holding a report can check it is genuine - only non-confidential data is shown."""
    conn = get_db()
    rep = conn.execute("SELECT * FROM reports WHERE report_no=?", (report_no,)).fetchone()
    res = integrity.verify_report(conn, rep) if rep else None
    if rep:
        audit.log(conn, "report.public_verify", "report", rep["id"], None, {"ok": res["ok"]})
    return render_template("verify.html", rep=rep, res=res, report_no=report_no)


@bp.route("/files/pages/<folder>/<name>")
def page_image(folder, name):
    """Rendered page of an uploaded scan (any signed-in user; names are content hashes)."""
    if not any(auth.load_user(r) for r in auth.ROLES):
        abort(403)
    if not re.fullmatch(r"[0-9a-f]{16}", folder) or not re.fullmatch(r"p\d+\.(png|jpg|jpeg)", name):
        abort(404)
    return send_from_directory(os.path.join(PAGES_DIR, folder), name)


@bp.route("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return {"ok": True}
