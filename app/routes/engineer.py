"""
Engineer portal: my jobs, per-station Excel logsheet (template generated live from the registry),
optional AI reading of a scanned logsheet, editable readings with mandatory correction reasons,
confirmation of AI values, verification and lock.
"""
import io
import json
import os
import tempfile
from datetime import datetime

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for
from werkzeug.datastructures import FileStorage

import config
from .. import audit, excel_io, fields as F, jobs, workflow
from ..ai import client as ai, intake
from ..auth import role_required
from ..db import IST, get_db, tx
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
    rows = get_db().execute(
        "SELECT r.*, je.role AS my_role, je.station AS my_station FROM requests r "
        "JOIN job_engineers je ON je.job_id=r.id WHERE je.user_id=? ORDER BY r.is_sealed, r.id DESC",
        (g.user["id"],)).fetchall()
    view = request.args.get("view", "")
    if view == "jobs":                         # sidebar "My jobs": jobs still open (not issued)
        rows = [r for r in rows if not r["is_sealed"]]
    return render_template("engineer/dashboard.html", rows=rows, view=view)


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
    results = conn.execute("SELECT * FROM test_results WHERE job_id=? ORDER BY passed IS NULL, passed, id",
                           (job_id,)).fetchall()
    corr_counts = {r["key"]: r["n"] for r in conn.execute(
        "SELECT key, COUNT(*) n FROM corrections WHERE job_id=? GROUP BY key", (job_id,))}
    uploads = conn.execute("SELECT f.*, u.username FROM files f LEFT JOIN users u ON u.id=f.uploaded_by "
                           "WHERE job_id=? AND kind IN ('logsheet','logsheet_scan') ORDER BY id DESC", (job_id,)).fetchall()
    last_import = conn.execute("SELECT after_json FROM audit_log WHERE action='readings.import' AND entity_id=? "
                               "ORDER BY id DESC LIMIT 1", (str(job_id),)).fetchone()
    last_files = {r["station"]: r["id"] for r in conn.execute(
        "SELECT station, MAX(id) id FROM files WHERE job_id=? AND kind='logsheet_scan' GROUP BY station", (job_id,))}
    return render_template("engineer/job.html", job=job, stations=stations, active=active, readings=readings,
                           results=results, corr_counts=corr_counts, uploads=uploads, tests=workflow.tests_of(job),
                           form=json.loads(job["form_json"]), editable=job["status"] in workflow.EDITABLE,
                           engineers=jobs.engineers_of(conn, job_id),
                           last_import=json.loads(last_import["after_json"]) if last_import else None,
                           station_fields=F.station_fields, is_required=F.is_required, ai_on=ai.available(),
                           ai_notice=ai.disabled_reason(), last_files=last_files)


@bp.route("/jobs/<int:job_id>/template/<station>.xlsx")
@role_required("engineer")
def template(job_id, station):
    """Excel logsheet generated from the CURRENT registry (a new registry field appears automatically)."""
    job = _my_job(job_id)
    if station not in jobs.job_stations(job):
        abort(404)
    form = json.loads(job["form_json"])
    wb = excel_io.build_station_template(station)
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
    fs = FileStorage(stream=io.BytesIO(data), filename=f.filename, content_type=f.content_type)
    with tx(conn):
        file_id = jobs.save_upload(conn, fs, job_id, "logsheet", g.user, config.ALLOWED_EXCEL_EXT, station)
        s = jobs.store_import(conn, job_id, station, file_id, result.values, g.user)
    msg = f"Imported {s['inserted'] + s['updated'] + s['unchanged']} values ({s['inserted']} new, {s['updated']} changed)."
    problems = len(result.unfilled) + len(result.missing)
    if problems:
        msg += f" {problems} required value(s) are still empty."
    if result.non_numeric:
        msg += f" {len(result.non_numeric)} value(s) are not numeric."
    flash(msg, "success" if not (problems or result.non_numeric) else "warning")
    return redirect(url_for("engineer.job", job_id=job_id, station=station))


def _store_extraction(c, job_id, station, file_id, res, user):
    """Readings from intake -> readings table. Our own Excel template = source excel; otherwise source ai,
    flagged 'AI-extracted, needs verification' until an engineer confirms."""
    source = "excel" if res.get("method") == "template" else "ai"
    values = {k: (v["value"], v.get("source") or "", v["confidence"]) for k, v in res["fields"].items()
              if F.field(k) and F.field(k)["station"] == station}
    if not values:
        return None
    c.execute("BEGIN IMMEDIATE")
    try:
        summary = jobs.store_import(c, job_id, station, file_id, values, user, source=source)
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise
    return summary


def _read_task(job_id, doc_kind, file_id, path, user, force_pro, tests):
    def work(tid, c):
        res = intake.read_document(path, doc_kind, c, tid, file_id, force_pro, tests)
        if doc_kind == "logsheet:auto":
            res["stored"] = {st: _store_extraction(c, job_id, st, file_id, part, user)
                             for st, part in res["parts"].items()}
        else:
            st = intake.station_of(doc_kind.split(":", 1)[1])
            res["stored"] = _store_extraction(c, job_id, st, file_id, res, user)
        return res
    return work


@bp.route("/jobs/<int:job_id>/read", methods=["POST"])
@role_required("engineer")
def read_document(job_id):
    """Read a logsheet document (PDF / photo / Excel in any layout) for one station, or station=auto for a
    document holding several logsheets. A new file, or file_id + force_pro=1 to re-read with the Pro model."""
    conn = get_db()
    job = _my_job(job_id)
    station = request.form.get("station") or "auto"
    if station != "auto" and station not in jobs.job_stations(job):
        abort(404)
    if job["status"] not in workflow.EDITABLE:
        return jsonify(ok=False, error="The job is locked."), 400
    if station == "auto" and not ai.available():
        return jsonify(ok=False, error=ai.disabled_reason()), 400
    force_pro = request.form.get("force_pro") == "1"
    user = dict(g.user)
    with tx(conn):
        if request.form.get("file_id"):
            row = conn.execute("SELECT * FROM files WHERE id=? AND job_id=?",
                               (int(request.form["file_id"]), job_id)).fetchone()
            if row is None:
                return jsonify(ok=False, error="File not found."), 404
        else:
            f = request.files.get("file")
            if not f or not f.filename:
                return jsonify(ok=False, error="Choose the logsheet document."), 400
            fid = jobs.save_upload(conn, f, job_id, "logsheet_scan", user, intake.ACCEPTED,
                                   None if station == "auto" else station)
            row = conn.execute("SELECT * FROM files WHERE id=?", (fid,)).fetchone()
        doc_kind = f"logsheet:{station}"
        task_id = ai.submit(conn, doc_kind, {"job_id": job_id, "station": station, "file_id": row["id"]},
                            _read_task(job_id, doc_kind, row["id"], row["stored_path"], user, force_pro,
                                       workflow.tests_of(job)), user)
        audit.log(conn, "ai.read_document", "job", job_id, None, {"kind": doc_kind, "file": row["id"], "task": task_id})
    return jsonify(ok=True, task_id=task_id, file_id=row["id"], notice=ai.disabled_reason())


@bp.route("/jobs/<int:job_id>/clear-ai", methods=["POST"])
@role_required("engineer")
def clear_ai(job_id):
    conn = get_db()
    _my_job(job_id)
    station = request.form.get("station") or None
    with tx(conn):
        n = jobs.clear_ai(conn, job_id, g.user, station)
    flash(f"{n} unconfirmed automatically read value(s) removed.", "info")
    return redirect(url_for("engineer.job", job_id=job_id, station=station))


@bp.route("/jobs/<int:job_id>/files/<int:file_id>/view")
@role_required("engineer")
def file_view(job_id, file_id):
    """Source document for the review pane: page images, or an HTML render of the spreadsheet."""
    _my_job(job_id)
    row = get_db().execute("SELECT * FROM files WHERE id=? AND job_id=?", (file_id, job_id)).fetchone()
    if row is None:
        abort(404)
    return jsonify(ok=True, name=row["original_name"], **intake.viewer(row["stored_path"]))


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
    return jsonify(ok=True, reading={"id": row["id"], "value": row["current_value"], "version": row["version"],
                                     "imported": row["imported_value"]}, job_version=job["version"])


@bp.route("/jobs/<int:job_id>/confirm-ai", methods=["POST"])
@role_required("engineer")
def confirm_ai(job_id):
    conn = get_db()
    _my_job(job_id)
    station = request.form.get("station") or None
    rid = request.form.get("reading_id")
    with tx(conn):
        n = jobs.confirm_ai(conn, job_id, g.user, station=station, reading_id=int(rid) if rid else None)
    flash(f"{n} AI-extracted value(s) confirmed.", "success")
    return redirect(url_for("engineer.job", job_id=job_id, station=station or request.form.get("back_station")))


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
        jobs.set_verified(conn, job_id, station, g.user, request.form.get("missing_reason"))
    flash(f"{F.stations()[station]['title']}: marked as verified"
          + (" with missing values (reason recorded)." if request.form.get("missing_reason") else "."), "success")
    return redirect(url_for("engineer.job", job_id=job_id, station=station))


@bp.route("/jobs/<int:job_id>/lock", methods=["POST"])
@role_required("engineer")
def lock(job_id):
    conn = get_db()
    _my_job(job_id)
    with tx(conn):
        jobs.lock(conn, job_id, g.user, expected_version(), request.form.get("missing_reason"))
    flash("Job locked and sent for technical review. Readings can no longer be edited unless the job is returned.",
          "success")
    return redirect(url_for("engineer.job", job_id=job_id))
