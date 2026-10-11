"""
Customer portal: new test request (scanned PDF read by AI, or manual entry), tracking,
issued report download and integrity verification.
"""
import json
import os
import tempfile

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

import config
from .. import audit, excel_io, fields as F, integrity, jobs, request_form, workflow
from ..ai import client as ai, intake
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
    rows = get_db().execute("SELECT r.*, rp.id AS report_id FROM requests r LEFT JOIN reports rp ON rp.job_id=r.id "
                            "WHERE r.customer_id=? ORDER BY r.id DESC", (g.user["id"],)).fetchall()
    view = request.args.get("view", "")
    if view == "reports":                      # sidebar "Reports": requests with an issued report
        rows = [r for r in rows if r["report_id"]]
    return render_template("customer/dashboard.html", rows=rows, view=view)


def _form_page(prefill, prefill_tests, prefill_drawings, errors=None, source_file_id="", extraction_id=""):
    return render_template("customer/new_request.html", sections=request_form.sections(),
                           declaration=request_form.DECLARATION, prefill=prefill, prefill_tests=prefill_tests,
                           prefill_drawings=prefill_drawings, errors=errors or {}, ai_on=ai.available(),
                           ai_notice=ai.disabled_reason(), source_file_id=source_file_id, extraction_id=extraction_id)


@bp.route("/requests/new", methods=["GET", "POST"])
@role_required("customer")
def new_request():
    conn = get_db()
    if request.method == "POST":
        data, tests, errors = request_form.parse_form(request.form, list(F.tests()))
        files = [f for f in request.files.getlist("documents") if f and f.filename]
        src = request.form.get("source_file_id") or None
        if errors:
            flash("Please correct the highlighted fields.", "danger")
            return _form_page(request.form, tests, data.get("drawings", []), errors, src or "",
                              request.form.get("extraction_id", ""))
        with tx(conn):
            job_id, ref = jobs.create_request(conn, g.user, data, tests, int(src) if src else None,
                                              request.form.get("extraction_id") or None)
            for f in files:
                jobs.save_upload(conn, f, job_id, "request_document", g.user, config.ALLOWED_DOC_EXT)
        flash(f"Request submitted. Reference: {ref}.", "success")
        return redirect(url_for("customer.request_detail", job_id=job_id))
    prefill = {"customer_name": g.user.get("company") or "", "customer_address": g.user.get("address") or ""}
    return _form_page(prefill, [], [])


@bp.route("/requests/scan", methods=["POST"])
@role_required("customer")
def upload_scan():
    """Read the filled request form (PDF / photo / Excel in any layout) in the background.
    Either a new file, or file_id + force_pro=1 to re-read a stored file with the higher-accuracy model."""
    conn = get_db()
    force_pro = request.form.get("force_pro") == "1"
    with tx(conn):
        if request.form.get("file_id"):
            row = conn.execute("SELECT * FROM files WHERE id=? AND uploaded_by=?",
                               (int(request.form["file_id"]), g.user["id"])).fetchone()
            if row is None:
                return jsonify(ok=False, error="File not found."), 404
        else:
            f = request.files.get("file")
            if not f or not f.filename:
                return jsonify(ok=False, error="Choose the filled request form."), 400
            fid = jobs.save_upload(conn, f, None, "request_document", g.user, intake.ACCEPTED)
            row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        file_id, path = row["id"], row["stored_path"]
        task_id = ai.submit(conn, "request_form", {"file_id": file_id, "force_pro": force_pro},
                            lambda tid, c: intake.read_document(path, "request_form", c, tid, file_id, force_pro),
                            g.user)
        audit.log(conn, "ai.read_document", "file", file_id, None,
                  {"kind": "request_form", "task": task_id, "force_pro": force_pro})
    return jsonify(ok=True, task_id=task_id, file_id=file_id, ai=ai.available(), notice=ai.disabled_reason())


@bp.route("/requests/template.xlsx")
@role_required("customer")
def request_template():
    wb = excel_io.build_request_template(request_form.sections())
    path = os.path.join(tempfile.mkdtemp(), "customer_request_template.xlsx")
    wb.save(path)
    return send_file(path, as_attachment=True, download_name="customer_request_template.xlsx")


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
        return jsonify(ok=False, error=f"Could not read the workbook: {exc}"), 400
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
    return jsonify(ok=True, **request_form.from_excel(values, list(F.tests())))


@bp.route("/requests/<int:job_id>")
@role_required("customer")
def request_detail(job_id):
    conn = get_db()
    job = _own_job(job_id)
    rep = conn.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
    files = conn.execute("SELECT * FROM files WHERE job_id=? AND kind='request_document'", (job_id,)).fetchall()
    history = conn.execute("SELECT ts, action, after_json FROM audit_log WHERE entity_type='job' AND entity_id=? "
                           "AND (action LIKE 'job.%' OR action='request.submit') ORDER BY id", (str(job_id),)).fetchall()
    amendments = conn.execute("SELECT id, report_no, status FROM requests WHERE amends_id=?",
                              (job["amends_id"] or job_id,)).fetchall()
    messages = conn.execute("SELECT * FROM notifications WHERE user_id=? AND link LIKE ? ORDER BY id DESC",
                            (g.user["id"], f"%/requests/{job_id}")).fetchall()
    return render_template("customer/request_detail.html", job=job, form=json.loads(job["form_json"]),
                           tests=workflow.tests_of(job), rep=rep, files=files, history=history,
                           amendments=amendments, messages=messages)


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
    f = get_db().execute("SELECT * FROM files WHERE id=? AND job_id=? AND kind='request_document'",
                         (file_id, job_id)).fetchone()
    if f is None:
        abort(404)
    return send_file(f["stored_path"], as_attachment=True, download_name=f["original_name"])
