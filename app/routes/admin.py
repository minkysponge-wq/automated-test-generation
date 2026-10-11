"""
Admin portal (Test In-charge): dashboard and search, accept / reject, work instruction,
engineer accounts and assignment, technical review with live report preview, forward to
the HOD, return to engineer, report templates (upload, auto-map, mapping editor, dry run),
field registry, activity log and configuration. Actions that need HOD sign-off go through
the approval engine (app/approvals.py).
"""
import csv
import io
import json
import os
import tempfile
from datetime import datetime

from flask import Blueprint, Response, abort, flash, g, jsonify, redirect, render_template, request, send_file, url_for

import config
from .. import activity, approvals, audit, fields as F, integrity, jobs, report, workflow
from ..netinfo import network_url
from ..auth import role_required
from ..db import IST, get_db, get_settings, now_ist, tx
from ..reporting import templates as T
from .common import expected_version, outcome_flash, register_auth_views, report_file_name

bp = Blueprint("admin", __name__)
register_auth_views(bp, "admin")


def _job(job_id):
    job = get_db().execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()
    if job is None:
        abort(404)
    return job


def turnaround_days(conn):
    v = conn.execute("SELECT AVG(julianday(issued_at) - julianday(created_at)) FROM requests "
                     "WHERE issued_at IS NOT NULL").fetchone()[0]
    return None if v is None else round(v, 1)


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
        "awaiting_accept": counts.get(workflow.SUBMITTED, 0),
        "in_testing": sum(counts.get(s, 0) for s in (workflow.ACCEPTED, workflow.ASSIGNED, workflow.IN_TESTING,
                                                      workflow.RETURNED)),
        "to_review": counts.get(workflow.LOCKED, 0) + counts.get(workflow.ADMIN_REVIEW, 0),
        "with_hod": conn.execute("SELECT COUNT(*) FROM approval_requests WHERE status='pending'").fetchone()[0],
        "issued_month": conn.execute("SELECT COUNT(*) FROM reports WHERE substr(issued_at,1,7)=?", (month,)).fetchone()[0],
        "avg_tat_days": turnaround_days(conn),
        "pending_engineers": conn.execute("SELECT COUNT(*) FROM users WHERE role='engineer' AND status='pending'").fetchone()[0],
    }
    q = (request.args.get("q") or "").strip()
    status = request.args.get("status") or ""
    d_from, d_to = request.args.get("from") or "", request.args.get("to") or ""
    sql = "SELECT r.*, u.full_name AS customer_user FROM requests r JOIN users u ON u.id=r.customer_id WHERE 1=1"
    args = []
    if q:
        like = f"%{q}%"
        sql += (" AND (r.ref LIKE ? OR r.sample_code LIKE ? OR r.report_no LIKE ? OR u.company LIKE ? OR u.full_name LIKE ? "
                "OR json_extract(r.form_json,'$.customer_name') LIKE ? OR json_extract(r.form_json,'$.serial_numbers') LIKE ? "
                "OR json_extract(r.form_json,'$.rating') LIKE ?)")
        args += [like] * 8
    if status:
        sql += " AND r.status=?"
        args.append(status)
    view = request.args.get("view") or ""
    if view == "jobs":                         # sidebar "Jobs": accepted and not yet issued
        sql += " AND r.status NOT IN (?,?,?)"
        args += [workflow.SUBMITTED, workflow.REJECTED, workflow.ISSUED]
    if d_from:
        sql += " AND substr(r.created_at,1,10) >= ?"
        args.append(d_from)
    if d_to:
        sql += " AND substr(r.created_at,1,10) <= ?"
        args.append(d_to)
    rows = conn.execute(sql + " ORDER BY r.id DESC LIMIT 200", args).fetchall()
    return render_template("admin/dashboard.html", kpi=kpi, counts=counts, rows=rows, q=q, status=status,
                           d_from=d_from, d_to=d_to, view=view, lan_url=network_url(request, config.PORT))


# =============================================================================
# request / job
# =============================================================================
@bp.route("/requests/<int:job_id>")
@role_required("admin")
def request_detail(job_id):
    conn = get_db()
    job = _job(job_id)
    if job["status"] == workflow.LOCKED:            # opening a locked job starts the technical review
        with tx(conn):
            jobs.begin_review(conn, job_id, g.user)
        job = _job(job_id)
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
    history = conn.execute("SELECT * FROM audit_log WHERE entity_type='job' AND entity_id=? ORDER BY id",
                           (str(job_id),)).fetchall()
    amendments = conn.execute("SELECT id, report_no, status FROM requests WHERE amends_id=? ORDER BY id",
                              (job["amends_id"] or job_id,)).fetchall()
    original = conn.execute("SELECT id, report_no FROM requests WHERE id=?", (job["amends_id"],)).fetchone() \
        if job["amends_id"] else None
    pending = approvals.pending_for(conn, "job", job_id)
    return render_template("admin/request_detail.html", job=job, form=json.loads(job["form_json"]),
                           tests=workflow.tests_of(job), readings=readings, corrections=corrections, results=results,
                           rep=rep, files=files, engineers=engineers, assigned=jobs.engineers_of(conn, job_id),
                           history=history, stations=jobs.station_status(conn, job) if job["sample_code"] else [],
                           wi=json.loads(job["wi_json"] or "{}"), condition=json.loads(job["condition_json"] or "{}"),
                           amendments=amendments, original=original, converter=report.detect_converter()[0],
                           pending=pending, needs={t: approvals.required(conn, t) for t in approvals.REGISTRY},
                           template=T.active(conn))


@bp.route("/requests/<int:job_id>/accept", methods=["POST"])
@role_required("admin")
def accept(job_id):
    conn = get_db()
    f = request.form
    condition = {"physical_condition": f.get("physical_condition", ""), "lab_capability": f.get("lab_capability", ""),
                 "remarks": f.get("condition_remarks", ""), "checked_by": g.user["full_name"], "checked_at": now_ist()}
    if condition["physical_condition"] != "Suitable" or condition["lab_capability"] != "Yes":
        flash("A sample can only be accepted when it is suitable and the laboratory has the capability. "
              "Otherwise reject the request.", "danger")
        return redirect(url_for("admin.request_detail", job_id=job_id))
    wi = {k: f.get(k) for k in ("customer_ref", "special_instructions", "date_of_test", "latest_standard", "deviation")}
    out = approvals.request_or_apply(conn, "accept_request", {"job_id": job_id, "condition": condition, "wi": wi,
                                                              "version": expected_version()}, g.user, job_id,
                                     f"Accept request {_job(job_id)['ref']}")
    if out[0] == "applied":
        flash(f"Request accepted. Sample code {out[1]['sample_code']}, test report number {out[1]['report_no']}.", "success")
    else:
        flash("Acceptance sent to the HOD for approval.", "info")
    return redirect(url_for("admin.request_detail", job_id=job_id))


@bp.route("/requests/<int:job_id>/reject", methods=["POST"])
@role_required("admin")
def reject(job_id):
    conn = get_db()
    if not (request.form.get("reason") or "").strip():
        flash("A reason is required to reject a request.", "danger")
        return redirect(url_for("admin.request_detail", job_id=job_id))
    out = approvals.request_or_apply(conn, "reject_request", {
        "job_id": job_id, "reason": request.form.get("reason", ""), "version": expected_version(),
        "condition": {"physical_condition": request.form.get("physical_condition"),
                      "lab_capability": request.form.get("lab_capability")}}, g.user, job_id,
        f"Reject request {_job(job_id)['ref']}")
    outcome_flash(out, "Request rejected. The customer can see the reason.")
    return redirect(url_for("admin.request_detail", job_id=job_id))


@bp.route("/requests/<int:job_id>/assign", methods=["POST"])
@role_required("admin")
def assign(job_id):
    conn = get_db()
    additional = [(int(uid), request.form.get(f"station_{uid}", "")) for uid in request.form.getlist("additional")]
    out = approvals.request_or_apply(conn, "assign_engineers", {
        "job_id": job_id, "responsible": int(request.form.get("responsible") or 0), "additional": additional,
        "version": expected_version()}, g.user, job_id, f"Assign engineers to {_job(job_id)['sample_code']}")
    outcome_flash(out, "Engineers assigned.")
    return redirect(url_for("admin.request_detail", job_id=job_id))


@bp.route("/requests/<int:job_id>/return", methods=["POST"])
@role_required("admin")
def return_job(job_id):
    conn = get_db()
    if not (request.form.get("comment") or "").strip():
        flash("A comment is mandatory when returning a job.", "danger")
        return redirect(url_for("admin.request_detail", job_id=job_id))
    out = approvals.request_or_apply(conn, "return_job", {"job_id": job_id, "comment": request.form.get("comment", ""),
                                                          "version": expected_version()}, g.user, job_id,
                                     f"Return {_job(job_id)['sample_code']} to the engineer")
    outcome_flash(out, "Job returned to the engineer and unlocked.")
    return redirect(url_for("admin.request_detail", job_id=job_id))


@bp.route("/requests/<int:job_id>/forward", methods=["POST"])
@role_required("admin")
def forward(job_id):
    """Technical review complete -> HOD authorization request (the only path to issue and seal)."""
    conn = get_db()
    job = _job(job_id)
    with tx(conn):
        approvals.submit(conn, "issue_report", {"job_id": job_id, "version": expected_version(),
                                                "verify_base": request.host_url}, g.user, job_id,
                         f"Authorize and issue test report {job['report_no']}")
    flash("Forwarded to the HOD for authorization.", "success")
    return redirect(url_for("admin.request_detail", job_id=job_id))


@bp.route("/requests/<int:job_id>/amend", methods=["POST"])
@role_required("admin")
def amend(job_id):
    conn = get_db()
    reason = (request.form.get("reason") or "").strip()
    if not reason:
        flash("A reason is required for an amendment.", "danger")
        return redirect(url_for("admin.request_detail", job_id=job_id))
    job = _job(job_id)
    with tx(conn):
        approvals.submit(conn, "amendment", {"job_id": job_id, "reason": reason}, g.user, job_id,
                         f"Amend issued report {job['report_no']}: {reason}")
    flash("Amendment request sent to the HOD. The issued report stays unchanged.", "info")
    return redirect(url_for("admin.request_detail", job_id=job_id))


@bp.route("/requests/<int:job_id>/preview.<fmt>")
@role_required("admin")
def preview(job_id, fmt):
    conn = get_db()
    _job(job_id)
    docx_path, pdf_path = jobs.preview(conn, job_id, g.user, want_pdf=(fmt == "pdf"))
    if fmt == "pdf" and pdf_path:
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
        flash("No PDF converter is available; the DOCX was downloaded instead.", "warning")
    return send_file(path, as_attachment=True, download_name=f"Work_Instruction_{job['sample_code']}.docx")


@bp.route("/requests/<int:job_id>/wi-scan", methods=["POST"])
@role_required("admin")
def wi_scan(job_id):
    """Optional: read a scanned, hand-filled work instruction to cross-check it against the generated one."""
    from ..ai import client as ai, intake
    conn = get_db()
    _job(job_id)
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify(ok=False, error="Choose the scanned work instruction."), 400
    if not ai.available():
        return jsonify(ok=False, error=ai.disabled_reason()), 400
    with tx(conn):
        fid = jobs.save_upload(conn, f, job_id, "work_instruction_scan", g.user, intake.ACCEPTED)
        path = conn.execute("SELECT stored_path FROM files WHERE id=?", (fid,)).fetchone()["stored_path"]
        task_id = ai.submit(conn, "work_instruction", {"job_id": job_id, "file_id": fid},
                            lambda tid, c: intake.read_document(path, "work_instruction", c, tid, fid), g.user)
    return jsonify(ok=True, task_id=task_id, file_id=fid)


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
    return send_file(path, as_attachment=fmt != "pdf", download_name=report_file_name(rep, _job(job_id), path))


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
# engineer accounts
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
            flash({"approve": "Engineer account approved.", "disable": "Engineer account disabled.",
                   "enable": "Engineer account enabled."}[action], "success")
        return redirect(url_for("admin.engineers"))
    rows = conn.execute("SELECT u.*, (SELECT COUNT(*) FROM job_engineers je JOIN requests r ON r.id=je.job_id "
                        "WHERE je.user_id=u.id AND r.is_sealed=0) AS open_jobs FROM users u WHERE role='engineer' "
                        "ORDER BY CASE status WHEN 'pending' THEN 0 ELSE 1 END, full_name").fetchall()
    return render_template("admin/engineers.html", rows=rows)


# =============================================================================
# report templates
# =============================================================================
@bp.route("/templates")
@role_required("admin")
def templates():
    conn = get_db()
    rows = conn.execute("SELECT t.*, u.full_name AS creator, a.full_name AS approver FROM report_templates t "
                        "LEFT JOIN users u ON u.id=t.created_by LEFT JOIN users a ON a.id=t.approved_by "
                        "ORDER BY t.equipment_type, t.version DESC").fetchall()
    return render_template("admin/templates.html", rows=rows, mappings={r["id"]: T.mapping_of(r) for r in rows})


@bp.route("/templates/upload", methods=["POST"])
@role_required("admin")
def template_upload():
    conn = get_db()
    f = request.files.get("file")
    if not f or not f.filename.lower().endswith(".docx"):
        flash("Choose a .docx report format.", "danger")
        return redirect(url_for("admin.templates"))
    tmp = tempfile.NamedTemporaryFile(suffix=".docx", delete=False)
    f.save(tmp.name)
    tmp.close()
    from ..ai import client as ai, template_mapper
    use_ai = bool(request.form.get("use_ai")) and ai.available()
    try:
        with tx(conn):
            tid, mapping = T.create_draft(conn, tmp.name, f.filename, g.user, request.form.get("name") or f.filename,
                                          request.form.get("equipment_type") or T.DEFAULT_EQUIPMENT, use_ai=use_ai,
                                          ai_fn=lambda slots, cands: template_mapper.propose(slots, cands, conn))
            audit.log(conn, "template.upload", "template", tid, None,
                      {"name": f.filename, "coverage": mapping["stats"]["coverage"],
                       "auto_coverage": mapping["auto_stats"]["coverage"]})
    finally:
        os.unlink(tmp.name)
    st = mapping["stats"]
    flash(f"Template parsed: {st['mapped_required']} of {st['required']} required places mapped automatically "
          f"({st['coverage']:.0%}). Review the remaining ones below.", "success")
    return redirect(url_for("admin.template_detail", tid=tid))


@bp.route("/templates/<int:tid>")
@role_required("admin")
def template_detail(tid):
    conn = get_db()
    rec = T.get(conn, tid)
    if rec is None:
        abort(404)
    view = T.editor_view(rec)
    fields_list = [f for f in F.all_fields() if f["source"] in ("reading", "computed", "system", "request", "proforma")]
    return render_template("admin/template_detail.html", rec=rec, view=view, fields_list=fields_list,
                           tests=F.tests(), problems=T.blocking_problems(conn, rec),
                           pending=approvals.pending_for(conn, "template", tid),
                           advanced=json.dumps({k: view["mapping"].get(k) for k in
                                                ("sections", "replacements", "loops", "keep_sheets_with", "qr_media",
                                                 "sheet_marker")}, indent=1, ensure_ascii=False))


@bp.route("/templates/<int:tid>/slot", methods=["POST"])
@role_required("admin")
def template_slot(tid):
    """Set the binding of one slot (field / text / expression / ignore). Draft templates only."""
    conn = get_db()
    rec = T.get(conn, tid)
    if rec is None or rec["status"] != "draft":
        return jsonify(ok=False, error="Only draft templates can be edited."), 400
    body = request.get_json(silent=True) or {}
    mapping = T.mapping_of(rec)
    kind = body.get("kind")
    sid = body.get("slot_id")
    old = mapping["slots"].get(sid)
    if kind == "clear":
        mapping["slots"].pop(sid, None)
    else:
        b = {"kind": kind, "method": "manual", "confidence": 1.0, "fp": body.get("fp") or (old or {}).get("fp")}
        if kind == "field":
            if not F.field(body.get("key", "")):
                return jsonify(ok=False, error="Unknown field key."), 400
            b["key"] = body["key"]
        elif kind == "text":
            b["text"] = body.get("text", "")
        elif kind == "expr":
            from simpleeval import SimpleEval
            try:
                SimpleEval().parse(body.get("expr", ""))
            except Exception as exc:
                return jsonify(ok=False, error=f"Expression is not valid: {exc}"), 400
            b["expr"] = body.get("expr", "")
        elif kind != "ignore":
            return jsonify(ok=False, error="Unknown binding type."), 400
        mapping["slots"][sid] = b
    with tx(conn):
        conn.execute("UPDATE report_templates SET mapping_json=?, version_row=version_row+1 WHERE id=?",
                     (json.dumps(mapping), tid))
        audit.log(conn, "template.map_slot", "template", tid, {"slot": sid, "binding": old}, {"slot": sid, **body})
    return jsonify(ok=True)


@bp.route("/templates/<int:tid>/advanced", methods=["POST"])
@role_required("admin")
def template_advanced(tid):
    """Sections, manual rules, loops and sheet settings as JSON."""
    conn = get_db()
    rec = T.get(conn, tid)
    if rec is None or rec["status"] != "draft":
        abort(400)
    try:
        data = json.loads(request.form.get("advanced") or "{}")
    except ValueError as exc:
        flash(f"Not valid JSON: {exc}", "danger")
        return redirect(url_for("admin.template_detail", tid=tid))
    mapping = T.mapping_of(rec)
    for k in ("sections", "replacements", "loops", "keep_sheets_with", "qr_media", "sheet_marker"):
        if k in data:
            mapping[k] = data[k]
    with tx(conn):
        T.recompile(conn, rec, mapping)
        audit.log(conn, "template.settings", "template", tid, None, {"keys": list(data)})
    flash("Settings saved and the template compiled again.", "success")
    return redirect(url_for("admin.template_detail", tid=tid))


@bp.route("/templates/<int:tid>/section", methods=["POST"])
@role_required("admin")
def template_section(tid):
    conn = get_db()
    rec = T.get(conn, tid)
    if rec is None or rec["status"] != "draft":
        abort(400)
    mapping = T.mapping_of(rec)
    f = request.form
    sec = {"id": f.get("id") or f"sec{len(mapping.get('sections', [])) + 1}", "tests": f.getlist("tests"),
           "start": {"type": f.get("start_type") or None, "text": f.get("start_text", "")},
           "end": {"type": f.get("end_type") or None, "text": f.get("end_text") or f.get("start_text", "")}}
    if not sec["tests"] or not sec["start"]["text"]:
        flash("A section needs at least one test and a start text.", "danger")
        return redirect(url_for("admin.template_detail", tid=tid))
    mapping.setdefault("sections", []).append(sec)
    with tx(conn):
        T.recompile(conn, rec, mapping)
        audit.log(conn, "template.section", "template", tid, None, sec)
    flash(f"Section '{sec['id']}' added.", "success")
    return redirect(url_for("admin.template_detail", tid=tid))


@bp.route("/templates/<int:tid>/compile", methods=["POST"])
@role_required("admin")
def template_compile(tid):
    conn = get_db()
    rec = T.get(conn, tid)
    if rec is None or rec["status"] != "draft":
        abort(400)
    with tx(conn):
        m = T.recompile(conn, rec, T.mapping_of(rec))
    left = m["dry_run"]["unfilled"]
    flash("Compiled. Dry run complete: " + ("no unfilled places." if not left else f"{len(left)} unfilled place(s)."),
          "success" if not left else "warning")
    return redirect(url_for("admin.template_detail", tid=tid))


@bp.route("/templates/<int:tid>/submit", methods=["POST"])
@role_required("admin")
def template_submit(tid):
    conn = get_db()
    rec = T.get(conn, tid)
    with tx(conn):
        approvals.submit(conn, "template_activation", {"template_id": tid}, g.user, tid,
                         f"Activate report template '{rec['name']}' v{rec['version']}")
    flash("Template sent to the HOD for activation.", "success")
    return redirect(url_for("admin.template_detail", tid=tid))


@bp.route("/templates/<int:tid>/file/<which>")
@role_required("admin")
def template_file(tid, which):
    rec = T.get(get_db(), tid)
    if rec is None:
        abort(404)
    path = {"source": T._abs(rec["source_path"]), "compiled": T._abs(rec["compiled_path"] or "")}.get(which)
    if which == "dryrun":
        path = T._abs(rec["compiled_path"]).replace(".docx", "_dryrun.docx")
        if not os.path.exists(path):
            m = T.mapping_of(rec)
            from ..reporting import generator
            generator.render(T._abs(rec["compiled_path"]), m, T.demo_snapshot(), path,
                             {"report_date": "18-11-2025", "reviewer_name": "HOD (dry run)", "seal": "DRYRUN" * 6})
    if not path or not os.path.exists(path):
        abort(404)
    return send_file(path, as_attachment=True, download_name=f"template{tid}_v{rec['version']}_{which}.docx")


# =============================================================================
# field registry
# =============================================================================
@bp.route("/registry")
@role_required("admin")
def registry():
    q = (request.args.get("q") or "").strip().lower()
    source = request.args.get("source") or ""
    station = request.args.get("station") or ""
    rows = [f for f in F.all_fields() if (not source or f["source"] == source) and (not station or f["station"] == station)
            and (not q or q in f["key"].lower() or q in f["label"].lower() or q in (f.get("report_label") or "").lower())]
    conn = get_db()
    pending = conn.execute("SELECT * FROM approval_requests WHERE type='registry_change' ORDER BY id DESC LIMIT 20").fetchall()
    return render_template("admin/registry.html", rows=rows[:400], total=len(rows), q=q, source=source, station=station,
                           pending=pending)


@bp.route("/registry/edit", methods=["GET", "POST"])
@role_required("admin")
def registry_edit():
    conn = get_db()
    key = request.values.get("key", "")
    f = F.field(key) if key else None
    if request.method == "POST":
        form = request.form
        new = F.field_as_change(f or {})
        for k in ("key", "label", "unit", "dtype", "source", "test_code", "station", "formula", "stage", "section",
                  "sheet", "row", "col", "report_label", "ui"):
            if k in form:
                new[k] = form.get(k).strip() or None
        new["dp"] = int(form["dp"]) if (form.get("dp") or "").strip() else None
        new["required"] = bool(form.get("required"))
        new["multi"] = bool(form.get("multi"))
        if form.get("options"):
            new["options"] = [o.strip() for o in form["options"].split("|") if o.strip()]
        errors = []
        if not new.get("key") or not new.get("label") or not new.get("source"):
            errors.append("Key, label and source are required.")
        if new.get("formula"):
            from simpleeval import SimpleEval
            try:
                SimpleEval().parse(new["formula"])
            except Exception as exc:
                errors.append(f"Formula is not valid: {exc}")
        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("admin/registry_edit.html", f=new, is_new=f is None)
        with tx(conn):
            approvals.submit(conn, "registry_change", {"changes": [{"op": "upsert", "field": new}]}, g.user, None,
                             f"{'Add' if f is None else 'Change'} registry field {new['key']}")
        flash("Change sent to the HOD for approval. It takes effect once approved.", "info")
        return redirect(url_for("admin.registry", q=new["key"]))
    return render_template("admin/registry_edit.html", f=f or {"source": "reading", "dtype": "number"}, is_new=f is None)


# =============================================================================
# activity log (operational view)
# =============================================================================
def audit_query(args):
    return activity.build_query(args)


def audit_csv_response(conn, args):
    sql, params = audit_query(args)
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "ts", "username", "role", "action", "entity_type", "entity_id", "before_json", "after_json", "ip",
            "user_agent", "prev_hash", "row_hash"]
    w.writerow(cols)
    for r in conn.execute(sql, params):
        w.writerow([r[c] for c in cols])
    audit.log(conn, "audit.export", "audit_log", None, None, {"filters": dict(args)})
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=atrg_audit_{datetime.now(IST):%Y%m%d_%H%M}.csv"})


def render_activity(portal_role):
    """Shared Activity log page (Test In-charge and HOD). Read-only."""
    data = activity.page(get_db(), request.args)
    return render_template("audit.html", portal_role=portal_role, can_verify=True, fmt_ts=activity.fmt_ts, **data)


def verify_chain_response(conn):
    res = audit.verify_chain(conn)
    audit.log(conn, "audit.verify_chain", "audit_log", None, None, res)
    msg = (f"Log verified: {res['rows']} entries, chain intact" if res["ok"]
           else f"Integrity problem at entry #{res['bad_id']}")
    return jsonify(message=msg, **res)


@bp.route("/audit")
@role_required("admin")
def audit_log():
    return render_activity("admin")


@bp.route("/activity")
@role_required("admin")
def activity_log():
    return redirect(url_for("admin.audit_log", **request.args))


@bp.route("/audit/verify", methods=["POST"])
@role_required("admin")
def audit_verify():
    return verify_chain_response(get_db())


@bp.route("/audit.csv")
@role_required("admin")
def audit_csv():
    return audit_csv_response(get_db(), request.args)


# =============================================================================
# configuration (changes need HOD approval)
# =============================================================================
CONFIG_KEYS = [k for k, v in config.DEFAULT_SETTINGS.items() if not k.startswith("approval_")]


@bp.route("/config", methods=["GET", "POST"])
@role_required("admin")
def settings():
    conn = get_db()
    current = get_settings(conn)
    if request.method == "POST":
        changes, errors = {}, []
        for key in CONFIG_KEYS:
            raw = request.form.get(key)
            if raw is None:
                continue
            default = config.DEFAULT_SETTINGS[key]
            if isinstance(default, float):
                try:
                    val = float(raw)
                except ValueError:
                    errors.append(f"{key} must be a number")
                    continue
            else:
                val = raw.strip()
            if val != current.get(key):
                changes[key] = val
        for e in errors:
            flash(e, "danger")
        if changes and not errors:
            with tx(conn):
                approvals.submit(conn, "settings_change", {"changes": changes}, g.user, None,
                                 "Change " + ", ".join(changes))
            flash(f"{len(changes)} change(s) sent to the HOD for approval.", "info")
        elif not changes and not errors:
            flash("Nothing changed.", "info")
        return redirect(url_for("admin.settings"))
    pending = conn.execute("SELECT * FROM approval_requests WHERE type='settings_change' AND status='pending'").fetchall()
    return render_template("admin/config.html", s=current, keys=CONFIG_KEYS, defaults=config.DEFAULT_SETTINGS,
                           pending=pending, converter=report.detect_converter())
