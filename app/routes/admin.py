"""
Admin portal (Test In-charge / HOD): dashboard + search, accept/reject, work
instruction, engineer approval and assignment, review (live preview), approve &
issue, return, amendments, audit log, configuration.
"""
import csv
import io
import json
import os
from datetime import datetime

from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

import config
from .. import audit, fields as F, integrity, jobs, report, workflow
from ..auth import role_required
from ..db import IST, get_db, get_settings, now_ist, save_setting, tx
from .common import expected_version, register_auth_views

bp = Blueprint("admin", __name__)
register_auth_views(bp, "admin")


def _job(job_id):
    job = get_db().execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()
    if job is None:
        abort(404)
    return job


# =============================================================================
# dashboard + search
# =============================================================================
@bp.route("/")
@role_required("admin")
def dashboard():
    conn = get_db()
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM requests GROUP BY status")}
    month = datetime.now(IST).strftime("%Y-%m")
    kpi = {
        "total": sum(counts.values()),
        "awaiting_accept": counts.get(workflow.SUBMITTED, 0),
        "pending_review": counts.get(workflow.LOCKED, 0) + counts.get(workflow.UNDER_REVIEW, 0),
        "in_testing": counts.get(workflow.IN_TESTING, 0) + counts.get(workflow.ASSIGNED, 0) + counts.get(workflow.RETURNED, 0),
        "issued_month": conn.execute("SELECT COUNT(*) FROM reports WHERE substr(issued_at,1,7)=?", (month,)).fetchone()[0],
        "issued_total": conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
        "pending_engineers": conn.execute("SELECT COUNT(*) FROM users WHERE role='engineer' AND status='pending'").fetchone()[0],
    }
    tat = conn.execute("SELECT AVG(julianday(issued_at) - julianday(created_at)) FROM requests WHERE issued_at IS NOT NULL").fetchone()[0]
    kpi["avg_tat_days"] = None if tat is None else round(tat, 1)

    # ---- search over all (historical) records
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or ""
    d_from, d_to = request.args.get("from") or "", request.args.get("to") or ""
    sql = ("SELECT r.*, u.full_name AS customer_user, u.company FROM requests r JOIN users u ON u.id=r.customer_id WHERE 1=1")
    args = []
    if q:
        like = f"%{q}%"
        sql += (" AND (r.sample_code LIKE ? OR r.report_no LIKE ? OR u.company LIKE ? OR u.full_name LIKE ? "
                "OR json_extract(r.form_json,'$.customer_name') LIKE ? OR json_extract(r.form_json,'$.serial_numbers') LIKE ? "
                "OR json_extract(r.form_json,'$.rating') LIKE ?)")
        args += [like] * 7
    if status:
        sql += " AND r.status=?"
        args.append(status)
    if d_from:
        sql += " AND substr(r.created_at,1,10) >= ?"
        args.append(d_from)
    if d_to:
        sql += " AND substr(r.created_at,1,10) <= ?"
        args.append(d_to)
    sql += " ORDER BY r.id DESC LIMIT 200"
    rows = conn.execute(sql, args).fetchall()
    return render_template("admin/dashboard.html", kpi=kpi, counts=counts, rows=rows, q=q, status=status,
                           d_from=d_from, d_to=d_to)


# =============================================================================
# request / job detail
# =============================================================================
@bp.route("/requests/<int:job_id>")
@role_required("admin")
def request_detail(job_id):
    conn = get_db()
    job = _job(job_id)
    if job["status"] == workflow.LOCKED:            # opening a locked job starts the review
        with tx(conn):
            jobs.begin_review(conn, job_id, g.user)
        job = _job(job_id)
    form = json.loads(job["form_json"])
    readings = conn.execute("SELECT rd.*, f.original_name AS file_name FROM readings rd LEFT JOIN files f "
                            "ON f.id=rd.source_file_id WHERE rd.job_id=? ORDER BY rd.station, rd.id", (job_id,)).fetchall()
    corrections = conn.execute("SELECT c.*, u.full_name FROM corrections c LEFT JOIN users u ON u.id=c.user_id "
                               "WHERE c.job_id=? ORDER BY c.id", (job_id,)).fetchall()
    results = conn.execute("SELECT * FROM test_results WHERE job_id=? ORDER BY station, passed IS NULL, passed, id",
                           (job_id,)).fetchall()
    rep = conn.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
    files = conn.execute("SELECT f.*, u.username FROM files f LEFT JOIN users u ON u.id=f.uploaded_by WHERE job_id=? "
                         "ORDER BY id", (job_id,)).fetchall()
    engineers = conn.execute("SELECT * FROM users WHERE role='engineer' AND status='active' ORDER BY full_name").fetchall()
    history = conn.execute("SELECT * FROM audit_log WHERE entity_type='job' AND entity_id=? ORDER BY id", (str(job_id),)).fetchall()
    amendments = conn.execute("SELECT id, report_no, status FROM requests WHERE amends_id=? ORDER BY id",
                              (job["amends_id"] or job_id,)).fetchall()
    original = conn.execute("SELECT id, report_no FROM requests WHERE id=?", (job["amends_id"],)).fetchone() if job["amends_id"] else None
    return render_template("admin/request_detail.html", job=job, form=form, tests=workflow.tests_of(job),
                           readings=readings, corrections=corrections, results=results, rep=rep, files=files,
                           engineers=engineers, assigned=jobs.engineers_of(conn, job_id), history=history,
                           stations=jobs.station_status(conn, job) if job["sample_code"] else [],
                           wi=json.loads(job["wi_json"] or "{}"), condition=json.loads(job["condition_json"] or "{}"),
                           amendments=amendments, original=original, converter=report.detect_converter()[0])


@bp.route("/requests/<int:job_id>/accept", methods=["POST"])
@role_required("admin")
def accept(job_id):
    conn = get_db()
    f = request.form
    condition = {"physical_condition": f.get("physical_condition", ""), "lab_capability": f.get("lab_capability", ""),
                 "remarks": f.get("condition_remarks", ""), "checked_by": g.user["full_name"], "checked_at": now_ist()}
    if condition["physical_condition"] != "Suitable" or condition["lab_capability"] != "Yes":
        flash("A sample can only be accepted when it is suitable and the lab has the capability - otherwise reject it.", "danger")
        return redirect(url_for("admin.request_detail", job_id=job_id))
    with tx(conn):
        sc, rn = jobs.accept(conn, job_id, g.user, condition, {
            "customer_ref": f.get("customer_ref"), "special_instructions": f.get("special_instructions"),
            "date_of_test": f.get("date_of_test"), "latest_standard": f.get("latest_standard"),
            "deviation": f.get("deviation")}, expected_version())
    flash(f"Accepted. Sample code {sc}, test report number {rn}. Work instruction created.", "success")
    return redirect(url_for("admin.request_detail", job_id=job_id))


@bp.route("/requests/<int:job_id>/reject", methods=["POST"])
@role_required("admin")
def reject(job_id):
    conn = get_db()
    with tx(conn):
        jobs.reject(conn, job_id, g.user, request.form.get("reason", ""), expected_version(),
                    {"physical_condition": request.form.get("physical_condition"),
                     "lab_capability": request.form.get("lab_capability")})
    flash("Request rejected - the customer can see the reason.", "info")
    return redirect(url_for("admin.request_detail", job_id=job_id))


@bp.route("/requests/<int:job_id>/assign", methods=["POST"])
@role_required("admin")
def assign(job_id):
    conn = get_db()
    resp = request.form.get("responsible")
    additional = []
    for uid in request.form.getlist("additional"):
        additional.append((int(uid), request.form.get(f"station_{uid}", "")))
    with tx(conn):
        jobs.assign(conn, job_id, g.user, int(resp or 0), additional, expected_version())
    flash("Engineer(s) assigned.", "success")
    return redirect(url_for("admin.request_detail", job_id=job_id))


@bp.route("/requests/<int:job_id>/return", methods=["POST"])
@role_required("admin")
def return_job(job_id):
    conn = get_db()
    with tx(conn):
        jobs.return_to_engineer(conn, job_id, g.user, request.form.get("comment", ""), expected_version())
    flash("Job returned to the engineer (unlocked).", "info")
    return redirect(url_for("admin.request_detail", job_id=job_id))


@bp.route("/requests/<int:job_id>/issue", methods=["POST"])
@role_required("admin")
def issue(job_id):
    conn = get_db()
    rep = jobs.issue(conn, job_id, g.user, expected_version(), request.host_url)
    flash(f"Report {rep['report_no']} issued and sealed (SHA-256 {rep['sha256_snapshot'][:12]}...).", "success")
    return redirect(url_for("admin.request_detail", job_id=job_id))


@bp.route("/requests/<int:job_id>/amend", methods=["POST"])
@role_required("admin")
def amend(job_id):
    conn = get_db()
    with tx(conn):
        new_id = jobs.create_amendment(conn, job_id, g.user, request.form.get("reason", ""))
    flash("Amendment job created. The original report is untouched; engineers can now correct the copy.", "success")
    return redirect(url_for("admin.request_detail", job_id=new_id))


@bp.route("/requests/<int:job_id>/preview.<fmt>")
@role_required("admin")
def preview(job_id, fmt):
    conn = get_db()
    _job(job_id)
    docx_path, pdf_path = jobs.preview(conn, job_id, g.user, want_pdf=(fmt == "pdf"))
    if fmt == "pdf":
        if not pdf_path:
            flash("No PDF converter available (install LibreOffice or MS Word) - downloading DOCX instead.", "warning")
            return send_file(docx_path, as_attachment=True, download_name=f"preview_job{job_id}.docx")
        return send_file(pdf_path, mimetype="application/pdf", download_name=f"preview_job{job_id}.pdf")
    return send_file(docx_path, as_attachment=True, download_name=f"preview_job{job_id}.docx")


@bp.route("/requests/<int:job_id>/wi.<fmt>")
@role_required("admin")
def work_instruction(job_id, fmt):
    job = _job(job_id)
    if not job["wi_json"]:
        abort(404)
    os.makedirs(config.REPORT_DIR, exist_ok=True)
    path = os.path.join(config.REPORT_DIR, f"WI_{job['sample_code']}_v{job['version']}.docx")
    report.render_work_instruction(job, path)
    audit.log(get_db(), "wi.download", "job", job_id, None, {"format": fmt})
    if fmt == "pdf":
        pdf = report.docx_to_pdf(path, path[:-5] + ".pdf")
        if pdf:
            return send_file(pdf, as_attachment=True, download_name=f"Work_Instruction_{job['sample_code']}.pdf")
        flash("No PDF converter available - downloading DOCX.", "warning")
    return send_file(path, as_attachment=True, download_name=f"Work_Instruction_{job['sample_code']}.docx")


@bp.route("/requests/<int:job_id>/report.<fmt>")
@role_required("admin")
def download_report(job_id, fmt):
    conn = get_db()
    rep = conn.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
    if rep is None:
        abort(404)
    path = rep["pdf_path"] if fmt == "pdf" else rep["docx_path"]
    if not path or not os.path.exists(path):
        abort(404)
    audit.log(conn, "report.download", "report", rep["id"], None, {"format": fmt})
    return send_file(path, as_attachment=fmt != "pdf", download_name=os.path.basename(path))


@bp.route("/requests/<int:job_id>/verify", methods=["POST"])
@role_required("admin")
def verify(job_id):
    conn = get_db()
    rep = conn.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
    if rep is None:
        abort(404)
    res = integrity.verify_report(conn, rep)
    audit.log(conn, "report.verify", "report", rep["id"], None, {"ok": res["ok"]})
    return jsonify(ok=True, valid=res["ok"], checks=[{"label": l, "ok": o} for l, o in res["checks"]],
                   sha256=rep["sha256_snapshot"])


@bp.route("/requests/<int:job_id>/files/<int:file_id>")
@role_required("admin")
def download_file(job_id, file_id):
    f = get_db().execute("SELECT * FROM files WHERE id=? AND job_id=?", (file_id, job_id)).fetchone()
    if f is None:
        abort(404)
    return send_file(f["stored_path"], as_attachment=True, download_name=f["original_name"])


# =============================================================================
# engineers
# =============================================================================
@bp.route("/engineers", methods=["GET", "POST"])
@role_required("admin")
def engineers():
    conn = get_db()
    if request.method == "POST":
        uid, action = int(request.form["user_id"]), request.form["action"]
        new = {"approve": "active", "disable": "disabled", "enable": "active"}.get(action)
        if new:
            with tx(conn):
                before = conn.execute("SELECT status FROM users WHERE id=? AND role='engineer'", (uid,)).fetchone()
                if before is None:
                    abort(404)
                conn.execute("UPDATE users SET status=?, approved_by=?, approved_at=? WHERE id=?",
                             (new, g.user["id"], now_ist(), uid))
                audit.log(conn, f"engineer.{action}", "user", uid, {"status": before["status"]}, {"status": new})
            flash(f"Engineer account {action}d.", "success")
        return redirect(url_for("admin.engineers"))
    rows = conn.execute("SELECT u.*, (SELECT COUNT(*) FROM job_engineers je JOIN requests r ON r.id=je.job_id "
                        "WHERE je.user_id=u.id AND r.is_sealed=0) AS open_jobs FROM users u WHERE role='engineer' "
                        "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, full_name").fetchall()
    return render_template("admin/engineers.html", rows=rows)


# =============================================================================
# audit log
# =============================================================================
def _audit_query():
    sql, args = "SELECT * FROM audit_log WHERE 1=1", []
    for key, col in (("user", "username"), ("role", "role"), ("action", "action"), ("entity", "entity_type")):
        v = (request.args.get(key) or "").strip()
        if v:
            sql += f" AND {col} LIKE ?"
            args.append(f"%{v}%")
    if request.args.get("entity_id"):
        sql += " AND entity_id=?"
        args.append(request.args["entity_id"])
    if request.args.get("from"):
        sql += " AND substr(ts,1,10) >= ?"
        args.append(request.args["from"])
    if request.args.get("to"):
        sql += " AND substr(ts,1,10) <= ?"
        args.append(request.args["to"])
    return sql + " ORDER BY id DESC", args


@bp.route("/audit")
@role_required("admin")
def audit_log():
    conn = get_db()
    sql, args = _audit_query()
    rows = conn.execute(sql + " LIMIT 500", args).fetchall()
    chain = audit.verify_chain(conn) if request.args.get("verify") else None
    return render_template("admin/audit.html", rows=rows, chain=chain, args=request.args)


@bp.route("/audit.csv")
@role_required("admin")
def audit_csv():
    conn = get_db()
    sql, args = _audit_query()
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "ts", "username", "role", "action", "entity_type", "entity_id", "before_json", "after_json", "ip",
            "user_agent", "prev_hash", "row_hash"]
    w.writerow(cols)
    for r in conn.execute(sql, args):
        w.writerow([r[c] for c in cols])
    audit.log(conn, "audit.export", "audit_log", None, None, {"filters": dict(request.args)})
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=atrg_audit_{datetime.now(IST):%Y%m%d_%H%M}.csv"})


@bp.route("/audit/verify", methods=["POST"])
@role_required("admin")
def audit_verify():
    conn = get_db()
    res = audit.verify_chain(conn)
    audit.log(conn, "audit.verify_chain", "audit_log", None, None, res)
    return jsonify(ok=True, **res)


# =============================================================================
# configuration
# =============================================================================
@bp.route("/config", methods=["GET", "POST"])
@role_required("admin")
def settings():
    conn = get_db()
    current = get_settings(conn)
    if request.method == "POST":
        changed = {}
        with tx(conn):
            for key, default in config.DEFAULT_SETTINGS.items():
                raw = request.form.get(key)
                if raw is None:
                    continue
                if isinstance(default, float):
                    try:
                        val = float(raw)
                    except ValueError:
                        flash(f"{key} must be a number", "danger")
                        continue
                else:
                    val = raw.strip()
                if val != current.get(key):
                    save_setting(conn, key, val, g.user["id"])
                    changed[key] = {"from": current.get(key), "to": val}
            if changed:
                audit.log(conn, "config.update", "settings", None, {k: v["from"] for k, v in changed.items()},
                          {k: v["to"] for k, v in changed.items()})
        flash(f"Saved {len(changed)} setting(s). Rule limits apply to the next recalculation.", "success")
        return redirect(url_for("admin.settings"))
    return render_template("admin/config.html", s=current, defaults=config.DEFAULT_SETTINGS,
                           converter=report.detect_converter())
