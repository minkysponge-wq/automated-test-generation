"""
Landing page and public report verification (the QR code on each report points here).
"""
from flask import Blueprint, render_template

from .. import audit, integrity
from ..db import get_db

bp = Blueprint("public", __name__)


@bp.route("/")
def landing():
    return render_template("landing.html")


@bp.route("/verify/<path:report_no>")
def verify(report_no):
    """Anyone holding a report can check it is genuine - shows only non-confidential data."""
    conn = get_db()
    rep = conn.execute("SELECT * FROM reports WHERE report_no=?", (report_no,)).fetchone()
    res = integrity.verify_report(conn, rep) if rep else None
    if rep:
        audit.log(conn, "report.public_verify", "report", rep["id"], None, {"ok": res["ok"]})
    return render_template("verify.html", rep=rep, res=res, report_no=report_no)


@bp.route("/healthz")
def healthz():
    get_db().execute("SELECT 1")
    return {"ok": True}
