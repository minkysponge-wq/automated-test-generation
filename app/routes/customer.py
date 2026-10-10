"""
Customer portal: new test request, tracking, sealed report download.
"""
import io
import json
import os
import tempfile

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

import config
from .. import audit, excel_io, fields as F, integrity, jobs, request_form, workflow
from ..auth import role_required
from ..db import get_db, tx
from .common import register_auth_views

bp = Blueprint("customer", __name__)
register_auth_views(bp, "customer")


def _own_job(job_id):
    job = get_db().execute("SELECT * FROM requests WHERE id=? AND customer_id=?", (job_id, g.user["id"])).fetchone()
    if job is None:
        abort(404)
    return job


@bp.route("/")
@role_required("customer")
def dashboard():
    conn = get_db()
    rows = conn.execute("SELECT r.*, rp.id AS report_id FROM requests r LEFT JOIN reports rp ON rp.job_id=r.id "
                        "WHERE r.customer_id=? ORDER BY r.id DESC", (g.user["id"],)).fetchall()
    return render_template("customer/dashboard.html", rows=rows)


@bp.route("/requests/new", methods=["GET", "POST"])
@role_required("customer")
def new_request():
    conn = get_db()
    if request.method == "POST":
        data, tests, errors = request_form.parse_form(request.form, list(F.tests()))
        files = [f for f in request.files.getlist("documents") if f and f.filename]
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("customer/new_request.html", sections=request_form.SECTIONS,
                                   declaration=request_form.DECLARATION, prefill=request.form, prefill_tests=tests,
                                   prefill_drawings=data.get("drawings", []))
        with tx(conn):
            job_id = jobs.create_request(conn, g.user, data, tests)
            for f in files:
                jobs.save_upload(conn, f, job_id, "request_document", g.user, config.ALLOWED_DOC_EXT)
        flash("Test request submitted. CPRI will review it and issue a sample code.", "success")
        return redirect(url_for("customer.request_detail", job_id=job_id))
    prefill = {"customer_name": g.user.get("company") or "", "customer_address": g.user.get("address") or ""}
    return render_template("customer/new_request.html", sections=request_form.SECTIONS,
                           declaration=request_form.DECLARATION, prefill=prefill, prefill_tests=[], prefill_drawings=[])


@bp.route("/requests/template.xlsx")
@role_required("customer")
def request_template():
    return send_file(os.path.join(config.EXCEL_TEMPLATE_DIR, "customer_request_template.xlsx"), as_attachment=True,
                     download_name="customer_request_template.xlsx")


@bp.route("/requests/parse-excel", methods=["POST"])
@role_required("customer")
def parse_excel():
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".xlsx"):
        return jsonify(ok=False, error="Choose the filled customer_request_template.xlsx"), 400
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    try:
        f.save(tmp.name)
        tmp.close()
        values = excel_io.parse_request_workbook(tmp.name)
    except Exception as exc:
        return jsonify(ok=False, error=f"Could not read workbook: {exc}"), 400
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    out = request_form.from_excel(values, list(F.tests()))
    return jsonify(ok=True, **out)


@bp.route("/requests/<int:job_id>")
@role_required("customer")
def request_detail(job_id):
    conn = get_db()
    job = _own_job(job_id)
    rep = conn.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
    files = conn.execute("SELECT * FROM files WHERE job_id=? AND kind='request_document'", (job_id,)).fetchall()
    history = conn.execute("SELECT ts, action, after_json FROM audit_log WHERE entity_type='job' AND entity_id=? "
                           "AND (action LIKE 'job.%' OR action='request.submit') ORDER BY id", (str(job_id),)).fetchall()
    amendments = conn.execute("SELECT id, report_no, status FROM requests WHERE amends_id=?", (job["amends_id"] or job_id,)).fetchall()
    return render_template("customer/request_detail.html", job=job, form=json.loads(job["form_json"]),
                           tests=workflow.tests_of(job), rep=rep, files=files, history=history, amendments=amendments)


@bp.route("/requests/<int:job_id>/report.<fmt>")
@role_required("customer")
def download_report(job_id, fmt):
    conn = get_db()
    job = _own_job(job_id)
    if job["status"] != workflow.ISSUED:
        abort(403)
    rep = conn.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
    path = rep["pdf_path"] if fmt == "pdf" else rep["docx_path"] if fmt == "docx" else None
    if not path or not os.path.exists(path):
        abort(404)
    audit.log(conn, "report.download", "report", rep["id"], None, {"format": fmt, "report_no": rep["report_no"]})
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@bp.route("/requests/<int:job_id>/verify", methods=["POST"])
@role_required("customer")
def verify(job_id):
    conn = get_db()
    _own_job(job_id)
    rep = conn.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
    if rep is None:
        abort(404)
    res = integrity.verify_report(conn, rep)
    audit.log(conn, "report.verify", "report", rep["id"], None, {"ok": res["ok"]})
    return jsonify(ok=True, valid=res["ok"], checks=[{"label": l, "ok": o} for l, o in res["checks"]],
                   sha256=rep["sha256_snapshot"])


@bp.route("/requests/<int:job_id>/files/<int:file_id>")
@role_required("customer")
def download_file(job_id, file_id):
    _own_job(job_id)
    f = get_db().execute("SELECT * FROM files WHERE id=? AND job_id=? AND kind='request_document'", (file_id, job_id)).fetchone()
    if f is None:
        abort(404)
    return send_file(f["stored_path"], as_attachment=True, download_name=f["original_name"])
