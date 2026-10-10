"""
Engineer portal: my jobs, per-station Excel template download / upload,
editable readings grid with mandatory correction reasons, verification, lock.
"""
import io
import json
import os
import tempfile

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for
from openpyxl import load_workbook

import config
from .. import audit, excel_io, fields as F, jobs, workflow
from ..auth import role_required
from ..db import get_db, tx
from .common import expected_version, register_auth_views

bp = Blueprint("engineer", __name__)
register_auth_views(bp, "engineer")


def _my_job(job_id):
    conn = get_db()
    job = conn.execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()
    if job is None or not jobs.is_assigned(conn, job_id, g.user["id"]):
        abort(404)
    return job


@bp.route("/")
@role_required("engineer")
def dashboard():
    conn = get_db()
    rows = conn.execute(
        "SELECT r.*, je.role AS my_role, je.station AS my_station FROM requests r "
        "JOIN job_engineers je ON je.job_id=r.id WHERE je.user_id=? ORDER BY r.is_sealed, r.id DESC", (g.user["id"],)).fetchall()
    return render_template("engineer/dashboard.html", rows=rows)


@bp.route("/jobs/<int:job_id>")
@role_required("engineer")
def job(job_id):
    conn = get_db()
    job = _my_job(job_id)
    stations = jobs.station_status(conn, job)
    active = request.args.get("station") or (stations[0]["station"] if stations else None)
    readings = {r["key"]: r for r in conn.execute(
        "SELECT rd.*, f.original_name AS file_name FROM readings rd LEFT JOIN files f ON f.id=rd.source_file_id "
        "WHERE rd.job_id=?", (job_id,))}
    results = conn.execute("SELECT * FROM test_results WHERE job_id=? ORDER BY passed IS NULL, passed, id", (job_id,)).fetchall()
    corr_counts = {r["key"]: r["n"] for r in conn.execute(
        "SELECT key, COUNT(*) n FROM corrections WHERE job_id=? GROUP BY key", (job_id,))}
    uploads = conn.execute("SELECT f.*, u.username FROM files f LEFT JOIN users u ON u.id=f.uploaded_by "
                           "WHERE job_id=? AND kind='logsheet' ORDER BY id DESC", (job_id,)).fetchall()
    import_summary = None
    if request.args.get("imported"):
        row = conn.execute("SELECT after_json FROM audit_log WHERE action='readings.import' AND entity_id=? "
                           "ORDER BY id DESC LIMIT 1", (str(job_id),)).fetchone()
        import_summary = json.loads(row["after_json"]) if row else None
    tests = workflow.tests_of(job)
    return render_template("engineer/job.html", job=job, stations=stations, active=active, readings=readings,
                           results=results, corr_counts=corr_counts, uploads=uploads, tests=tests,
                           form=json.loads(job["form_json"]), editable=job["status"] in workflow.EDITABLE,
                           engineers=jobs.engineers_of(conn, job_id), import_summary=import_summary,
                           station_fields=F.station_fields, is_required=F.is_required)


@bp.route("/jobs/<int:job_id>/template/<station>.xlsx")
@role_required("engineer")
def template(job_id, station):
    job = _my_job(job_id)
    if station not in jobs.job_stations(job):
        abort(404)
    form = json.loads(job["form_json"])
    wb = load_workbook(os.path.join(config.EXCEL_TEMPLATE_DIR, F.stations()[station]["file"]))
    from datetime import datetime
    from ..db import IST
    excel_io.fill_header(wb, station, {
        "series_no": job["report_no"], "sample_code": job["sample_code"],
        "customer": form.get("customer_name", ""), "rating": form.get("rating", ""),
        "serial_no": form.get("serial_numbers", ""), "date": datetime.now(IST).strftime("%d-%m-%Y")})
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    audit.log(get_db(), "template.download", "job", job_id, None, {"station": station})
    return send_file(buf, as_attachment=True, download_name=f"{job['sample_code']}_{F.stations()[station]['file']}",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/jobs/<int:job_id>/upload/<station>", methods=["POST"])
@role_required("engineer")
def upload(job_id, station):
    conn = get_db()
    job = _my_job(job_id)
    if station not in jobs.job_stations(job):
        abort(404)
    f = request.files.get("file")
    if not f or not f.filename:
        flash("Choose the filled Excel logsheet first.", "warning")
        return redirect(url_for("engineer.job", job_id=job_id, station=station))
    # parse first (outside the write lock), then store file + readings in one transaction
    data = f.read()
    tmp = tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False)
    tmp.write(data)
    tmp.close()
    try:
        result = excel_io.import_workbook(tmp.name, station, workflow.tests_of(job), job["sample_code"])
    finally:
        os.unlink(tmp.name)
    if not result.ok:
        for e in result.errors:
            flash(e, "danger")
        return redirect(url_for("engineer.job", job_id=job_id, station=station))
    from werkzeug.datastructures import FileStorage
    fs = FileStorage(stream=io.BytesIO(data), filename=f.filename, content_type=f.content_type)
    with tx(conn):
        file_id = jobs.save_upload(conn, fs, job_id, "logsheet", g.user, config.ALLOWED_EXCEL_EXT, station)
        s = jobs.store_import(conn, job_id, station, file_id, result, g.user)
    msg = f"Imported {s['inserted'] + s['updated'] + s['unchanged']} values ({s['inserted']} new, {s['updated']} changed)."
    if result.unfilled or result.missing:
        msg += f" {len(result.unfilled) + len(result.missing)} required placeholder(s) still empty."
    if result.non_numeric:
        msg += f" {len(result.non_numeric)} non-numeric value(s) - check the red rows."
    flash(msg, "success" if not (result.unfilled or result.missing or result.non_numeric) else "warning")
    return redirect(url_for("engineer.job", job_id=job_id, station=station, imported=1))


@bp.route("/jobs/<int:job_id>/readings/<int:reading_id>", methods=["POST"])
@role_required("engineer")
def correct(job_id, reading_id):
    conn = get_db()
    _my_job(job_id)
    body = request.get_json(silent=True) or request.form
    with tx(conn):
        row = jobs.correct_reading(conn, job_id, reading_id, body.get("value"), body.get("reason"),
                                   body.get("version"), g.user)
        job = jobs.get_job(conn, job_id)
        results = conn.execute("SELECT rule_code, label, value, limit_text, passed, station FROM test_results "
                               "WHERE job_id=?", (job_id,)).fetchall()
    return jsonify(ok=True, reading={"id": row["id"], "value": row["current_value"], "version": row["version"],
                                     "imported": row["imported_value"]},
                   job_version=job["version"], results=[dict(r) for r in results])


@bp.route("/jobs/<int:job_id>/readings/<int:reading_id>/history")
@role_required("engineer")
def history(job_id, reading_id):
    _my_job(job_id)
    rows = get_db().execute("SELECT c.*, u.full_name FROM corrections c LEFT JOIN users u ON u.id=c.user_id "
                            "WHERE c.reading_id=? AND c.job_id=? ORDER BY c.id", (reading_id, job_id)).fetchall()
    return jsonify(ok=True, history=[dict(r) for r in rows])


@bp.route("/jobs/<int:job_id>/verify/<station>", methods=["POST"])
@role_required("engineer")
def verify(job_id, station):
    conn = get_db()
    _my_job(job_id)
    with tx(conn):
        jobs.set_verified(conn, job_id, station, g.user)
    flash(f"{F.stations()[station]['title']} marked as verified.", "success")
    return redirect(url_for("engineer.job", job_id=job_id, station=station))


@bp.route("/jobs/<int:job_id>/lock", methods=["POST"])
@role_required("engineer")
def lock(job_id):
    conn = get_db()
    _my_job(job_id)
    with tx(conn):
        jobs.lock(conn, job_id, g.user, expected_version())
    flash("Job locked and sent for admin review. It can no longer be edited unless the admin returns it.", "success")
    return redirect(url_for("engineer.job", job_id=job_id))
