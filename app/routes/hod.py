"""
HOD portal (Head of Division): approval inbox, final report authorization, dashboard with
turnaround per stage and rule-failure counts, full activity log with chain verification,
approval settings, accounts (create HOD) and system status (PDF converter, Gemini key, DB size, backups).
"""
import json
import os
import sqlite3
from datetime import datetime

from flask import Blueprint, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

import config
from .. import approvals, audit, auth, fields as F, jobs, report, workflow
from ..netinfo import network_url
from ..ai import client as ai
from ..auth import role_required
from ..db import IST, get_db, get_settings, now_ist, save_setting, tx
from ..reporting import templates as T
from .admin import audit_csv_response, audit_query, render_activity, turnaround_days, verify_chain_response
from .common import register_auth_views, validate_registration

bp = Blueprint("hod", __name__)
register_auth_views(bp, "hod")

STAGES = [("Submitted to accepted", "request.submit", "job.accept"),
          ("Accepted to assigned", "job.accept", "job.assign"),
          ("Assigned to locked by engineer", "job.assign", "job.lock"),
          ("Technical review", "job.lock", "job.forward"),
          ("HOD authorization", "job.forward", "job.issue")]


def stage_turnaround(conn):
    """Average days per workflow stage, from the audit trail."""
    rows = conn.execute("SELECT entity_id, action, ts FROM audit_log WHERE entity_type='job' AND "
                        "(action LIKE 'job.%' OR action='request.submit') ORDER BY id").fetchall()
    first = {}
    for r in rows:
        first.setdefault((r["entity_id"], r["action"]), r["ts"])
    out = []
    for label, a, b in STAGES:
        spans = []
        for (eid, act), ts in first.items():
            if act != a or (eid, b) not in first:
                continue
            try:
                d = (datetime.fromisoformat(first[(eid, b)]) - datetime.fromisoformat(ts)).total_seconds() / 86400
            except ValueError:
                continue
            spans.append(d)
        out.append({"label": label, "days": round(sum(spans) / len(spans), 2) if spans else None, "n": len(spans)})
    return out


def age_text(ts):
    try:
        delta = datetime.now(IST) - datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return ""
    h = delta.total_seconds() / 3600
    return f"{int(h * 60)} min" if h < 1 else (f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} days")


def system_status(conn, check_key=False):
    kind, path = report.detect_converter()
    db_size = os.path.getsize(config.DB_PATH) if os.path.exists(config.DB_PATH) else 0
    last_backup = get_settings(conn).get("last_backup")
    act = T.active(conn)
    return {
        "converter": {"LibreOffice": "libreoffice", "word": "Microsoft Word"}.get(kind, kind) if kind else None,
        "converter_path": path,
        "ai": ai.status_line(), "ai_key": ai.check_key() if check_key else None,
        "db_size_mb": round(db_size / 1024 / 1024, 2), "db_path": config.DB_PATH, "last_backup": last_backup,
        "template": f"{act['name']} v{act['version']}" if act else None,
        "registry_fields": len(F.all_fields()),
    }


def make_backup(conn, user):
    os.makedirs(config.BACKUP_DIR, exist_ok=True)
    path = os.path.join(config.BACKUP_DIR, f"atrg_{datetime.now(IST):%Y%m%d_%H%M%S}.db")
    dest = sqlite3.connect(path)
    with dest:
        conn.backup(dest)
    dest.close()
    with tx(conn):
        save_setting(conn, "last_backup", now_ist(), user["id"] if user else None)
        audit.log(conn, "system.backup", "system", None, None, {"path": path}, user=user)
    return path


# =============================================================================
# dashboard
# =============================================================================
@bp.route("/")
@role_required("hod")
def dashboard():
    conn = get_db()
    month = datetime.now(IST).strftime("%Y-%m")
    pending = conn.execute("SELECT a.*, u.full_name AS requester FROM approval_requests a LEFT JOIN users u "
                           "ON u.id=a.requested_by WHERE a.status='pending' ORDER BY a.id").fetchall()
    fails = conn.execute("SELECT label, rule_code, COUNT(*) n FROM test_results WHERE passed=0 GROUP BY rule_code "
                         "ORDER BY n DESC LIMIT 10").fetchall()
    counts = {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM requests GROUP BY status")}
    kpi = {
        "pending": len(pending),
        "issued_month": conn.execute("SELECT COUNT(*) FROM reports WHERE substr(issued_at,1,7)=?", (month,)).fetchone()[0],
        "issued_total": conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
        "avg_tat_days": turnaround_days(conn),
        "open_jobs": sum(n for s, n in counts.items() if s not in (workflow.ISSUED, workflow.REJECTED)),
    }
    return render_template("hod/dashboard.html", kpi=kpi, pending=pending, stages=stage_turnaround(conn), fails=fails,
                           status=system_status(conn), age=age_text, counts=counts,
                           lan_url=network_url(request, config.PORT))


# =============================================================================
# approvals
# =============================================================================
@bp.route("/approvals")
@role_required("hod")
def approval_list():
    conn = get_db()
    show = request.args.get("show", "pending")
    sql = ("SELECT a.*, u.full_name AS requester, d.full_name AS decider, r.sample_code, r.report_no AS job_report_no "
           "FROM approval_requests a LEFT JOIN users u ON u.id=a.requested_by LEFT JOIN users d ON d.id=a.decided_by "
           "LEFT JOIN requests r ON a.type='issue_report' AND r.id=CAST(a.entity_id AS INTEGER)")
    if show == "pending":
        where = " WHERE a.status='pending' ORDER BY a.id"
    elif show == "reports":                    # sidebar "Reports": final report authorizations
        where = " WHERE a.type='issue_report' ORDER BY a.id DESC LIMIT 300"
    else:
        where = " ORDER BY a.id DESC LIMIT 300"
    rows = conn.execute(sql + where).fetchall()
    return render_template("hod/approvals.html", rows=rows, show=show, age=age_text, labels={t: s.label for t, s in
                                                                                            approvals.REGISTRY.items()})


@bp.route("/approvals/<int:aid>")
@role_required("hod")
def approval_detail(aid):
    conn = get_db()
    ap = conn.execute("SELECT a.*, u.full_name AS requester, d.full_name AS decider FROM approval_requests a "
                      "LEFT JOIN users u ON u.id=a.requested_by LEFT JOIN users d ON d.id=a.decided_by WHERE a.id=?",
                      (aid,)).fetchone()
    if ap is None:
        abort(404)
    payload = json.loads(ap["payload_json"])
    ctx = {"ap": ap, "payload": payload, "spec": approvals.REGISTRY.get(ap["type"]), "age": age_text}
    if ap["entity_type"] == "job" and ap["entity_id"]:
        job = conn.execute("SELECT * FROM requests WHERE id=?", (int(ap["entity_id"]),)).fetchone()
        ctx["job"] = job
        ctx["form"] = json.loads(job["form_json"])
        ctx["results"] = conn.execute("SELECT * FROM test_results WHERE job_id=? ORDER BY passed IS NULL, passed, station, id",
                                      (job["id"],)).fetchall()
        ctx["corrections"] = conn.execute("SELECT c.*, u.full_name FROM corrections c LEFT JOIN users u ON u.id=c.user_id "
                                          "WHERE c.job_id=? ORDER BY c.id", (job["id"],)).fetchall()
        ctx["stations"] = jobs.station_status(conn, job) if job["sample_code"] else []
        ctx["engineers"] = jobs.engineers_of(conn, job["id"])
    if ap["type"] == "template_activation":
        rec = T.get(conn, payload["template_id"])
        ctx["template"] = rec
        ctx["tmapping"] = T.mapping_of(rec) if rec else None
    if ap["type"] == "admin_account":
        ctx["user"] = conn.execute("SELECT * FROM users WHERE id=?", (payload["user_id"],)).fetchone()
    if ap["type"] == "registry_change":
        ctx["current"] = {c["field"]["key"]: F.field(c["field"]["key"]) for c in payload["changes"] if c.get("field")}
    if ap["type"] == "settings_change":
        ctx["current"] = get_settings(conn)
    return render_template("hod/approval_detail.html", **ctx)


@bp.route("/approvals/<int:aid>/decide", methods=["POST"])
@role_required("hod")
def decide(aid):
    conn = get_db()
    approve = request.form.get("decision") == "approve"
    result = approvals.decide(conn, aid, g.user, approve, request.form.get("comment", ""))
    if approve:
        msg = "Approved and applied."
        if result and result.get("report_no"):
            msg = f"Test report {result['report_no']} authorized, issued and sealed."
        flash(msg, "success")
    else:
        flash("Request rejected. The Test In-charge has been notified.", "info")
    return redirect(url_for("hod.approval_list"))


@bp.route("/approvals/<int:aid>/preview")
@role_required("hod")
def approval_preview(aid):
    conn = get_db()
    ap = conn.execute("SELECT * FROM approval_requests WHERE id=?", (aid,)).fetchone()
    if ap is None or ap["entity_type"] != "job":
        abort(404)
    docx_path, pdf_path = jobs.preview(conn, int(ap["entity_id"]), g.user)
    if pdf_path:
        return send_file(pdf_path, mimetype="application/pdf")
    return send_file(docx_path, as_attachment=True, download_name="preview.docx")


@bp.route("/templates/<int:tid>/dryrun")
@role_required("hod")
def template_dryrun(tid):
    rec = T.get(get_db(), tid)
    if rec is None:
        abort(404)
    path = T._abs(rec["compiled_path"]).replace(".docx", "_dryrun.docx")
    if not os.path.exists(path):
        from ..reporting import generator
        generator.render(T._abs(rec["compiled_path"]), T.mapping_of(rec), T.demo_snapshot(), path,
                         {"report_date": "18-11-2025", "reviewer_name": "HOD (dry run)", "seal": "DRYRUN" * 6})
    pdf = path[:-5] + ".pdf"
    if not os.path.exists(pdf):
        report.docx_to_pdf(path, pdf)
    if os.path.exists(pdf):
        return send_file(pdf, mimetype="application/pdf")
    return send_file(path, as_attachment=True, download_name=f"template_{tid}_dryrun.docx")


# =============================================================================
# activity log (full) + chain verification
# =============================================================================
@bp.route("/audit")
@role_required("hod")
def audit_log():
    return render_activity("hod")


@bp.route("/activity")
@role_required("hod")
def activity_log():
    return redirect(url_for("hod.audit_log", **request.args))


@bp.route("/audit.csv")
@role_required("hod")
def audit_csv():
    return audit_csv_response(get_db(), request.args)


@bp.route("/audit/verify", methods=["POST"])
@role_required("hod")
def audit_verify():
    return verify_chain_response(get_db())


# =============================================================================
# settings: optional approvals + direct edits of limits by the HOD
# =============================================================================
@bp.route("/settings", methods=["GET", "POST"])
@role_required("hod")
def settings():
    conn = get_db()
    current = get_settings(conn)
    if request.method == "POST":
        changed = {}
        with tx(conn):
            for spec in approvals.optional_types():
                key = f"approval_{spec.type}"
                val = bool(request.form.get(key))
                if val != bool(current.get(key)):
                    save_setting(conn, key, val, g.user["id"])
                    changed[key] = val
            for key, default in config.DEFAULT_SETTINGS.items():
                if key.startswith("approval_") or request.form.get(key) is None:
                    continue
                raw = request.form.get(key)
                try:
                    val = float(raw) if isinstance(default, float) else raw.strip()
                except ValueError:
                    flash(f"{key} must be a number", "danger")
                    continue
                if val != current.get(key):
                    save_setting(conn, key, val, g.user["id"])
                    changed[key] = val
            if changed:
                audit.log(conn, "config.update", "settings", None, {k: current.get(k) for k in changed}, changed)
        flash(f"{len(changed)} setting(s) saved." if changed else "Nothing changed.", "success" if changed else "info")
        return redirect(url_for("hod.settings"))
    return render_template("hod/settings.html", s=current, optional=approvals.optional_types(),
                           always=[s for s in approvals.REGISTRY.values() if s.always], defaults=config.DEFAULT_SETTINGS)


# =============================================================================
# accounts
# =============================================================================
@bp.route("/accounts", methods=["GET", "POST"])
@role_required("hod")
def accounts():
    conn = get_db()
    errors = {}
    if request.method == "POST":
        errors = validate_registration(request.form, "hod")
        if not errors:
            try:
                with tx(conn):
                    uid = auth.register_user(conn, "hod", request.form, "active")
                    audit.log(conn, "user.create_hod", "user", uid, None, {"username": request.form["username"]})
            except sqlite3.IntegrityError:
                errors["username"] = "This username is already taken."
            else:
                flash("HOD account created.", "success")
                return redirect(url_for("hod.accounts"))
    rows = conn.execute("SELECT * FROM users WHERE role IN ('admin', 'hod') ORDER BY role, full_name").fetchall()
    return render_template("hod/accounts.html", rows=rows, errors=errors, form=request.form)


# =============================================================================
# system status
# =============================================================================
@bp.route("/system", methods=["GET", "POST"])
@role_required("hod")
def system():
    conn = get_db()
    if request.method == "POST":
        path = make_backup(conn, g.user)
        flash(f"Backup written to {path}.", "success")
        return redirect(url_for("hod.system"))
    return render_template("hod/system.html", status=system_status(conn, check_key=request.args.get("check") == "1"))
