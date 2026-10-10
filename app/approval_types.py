"""
Approval types handled by the HOD (see app/approvals.py for the engine).

Always need the HOD:  issue_report, template_activation, registry_change, amendment, admin_account, settings_change
Optional (HOD settings, default off): accept_request, reject_request, assign_engineers, return_job
"""
import json
import shutil

from . import approvals, audit, fields as F, jobs, notify, workflow
from .db import now_ist, save_setting
from .workflow import WorkflowError


def _job_state(conn, job_id, *states):
    job = jobs.get_job(conn, job_id)
    if job["status"] not in states:
        raise WorkflowError(f"Job is {workflow.STATE_LABELS.get(job['status'], job['status'])}; the request no longer applies.")
    return job


def _requester(conn, approval, fallback):
    if approval is None:
        return fallback
    u = conn.execute("SELECT * FROM users WHERE id=?", (approval["requested_by"],)).fetchone()
    return dict(u) if u else fallback


# ---------------------------------------------------------------- final report authorization (always)
def _issue_submit(conn, p, requester):
    jobs.forward_to_hod(conn, p["job_id"], requester, p.get("version"))


def _issue_validate(conn, p):
    _job_state(conn, p["job_id"], workflow.PENDING_HOD)


def _issue_prepare(conn, p, approval, hod):
    return jobs.prepare_issue(conn, p["job_id"], hod, p.get("verify_base", ""))


def _issue_apply(conn, p, approval, hod, art):
    try:
        rid, folder = jobs.finalize_issue(conn, p["job_id"], hod, art, approval["requested_by"] if approval else None)
    finally:
        shutil.rmtree(art["tmp"], ignore_errors=True)
    rep = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    jobs.seal_files(folder, rep)
    return {"report_id": rid, "report_no": rep["report_no"], "sha256": rep["sha256_snapshot"]}


def _issue_reject(conn, p, approval, hod, comment):
    jobs.hod_reject(conn, p["job_id"], hod, comment)


approvals.register("issue_report", "Final report authorization and issue", _issue_apply, always=True,
                   validate=_issue_validate, prepare=_issue_prepare, on_submit=_issue_submit, on_reject=_issue_reject)


# ---------------------------------------------------------------- report template activation (always)
def _tpl_submit(conn, p, requester):
    from .reporting import templates as T
    rec = T.get(conn, p["template_id"])
    if rec is None or rec["status"] != "draft":
        raise WorkflowError("Only a draft template can be sent for activation")
    problems = T.blocking_problems(conn, rec)
    if problems:
        raise WorkflowError("Template is not ready: " + "; ".join(problems))
    conn.execute("UPDATE report_templates SET status='pending_hod', version_row=version_row+1 WHERE id=?", (rec["id"],))


def _tpl_validate(conn, p):
    from .reporting import templates as T
    rec = T.get(conn, p["template_id"])
    if rec is None or rec["status"] not in ("pending_hod", "draft"):
        raise WorkflowError("Template is no longer waiting for activation")


def _tpl_apply(conn, p, approval, hod, art):
    from .reporting import templates as T
    T.activate(conn, p["template_id"], hod["id"])
    rec = T.get(conn, p["template_id"])
    audit.log(conn, "template.activate", "template", rec["id"], None,
              {"name": rec["name"], "version": rec["version"], "sha256": rec["compiled_sha"]}, user=hod)
    return {"template_id": rec["id"], "version": rec["version"]}


def _tpl_reject(conn, p, approval, hod, comment):
    conn.execute("UPDATE report_templates SET status='draft', version_row=version_row+1 WHERE id=?", (p["template_id"],))


approvals.register("template_activation", "Activate report template", _tpl_apply, always=True, validate=_tpl_validate,
                   on_submit=_tpl_submit, on_reject=_tpl_reject, entity_type="template")


# ---------------------------------------------------------------- field registry change (always)
def _reg_apply(conn, p, approval, hod, art):
    n = F.apply_changes(conn, p["changes"])
    audit.log(conn, "registry.change", "registry", None, None, {"changes": p["changes"]}, user=hod)
    return {"changed": n}


approvals.register("registry_change", "Field registry / mapping change", _reg_apply, always=True, entity_type="registry")


# ---------------------------------------------------------------- amendment of an issued report (always)
def _amend_validate(conn, p):
    job = jobs.get_job(conn, p["job_id"])
    if not job["is_sealed"]:
        raise WorkflowError("Only issued reports can be amended")


def _amend_apply(conn, p, approval, hod, art):
    new_id = jobs.create_amendment(conn, p["job_id"], _requester(conn, approval, hod), p["reason"])
    return {"new_job_id": new_id}


approvals.register("amendment", "Amendment of an issued report", _amend_apply, always=True, validate=_amend_validate)


# ---------------------------------------------------------------- admin account (always)
def _admin_apply(conn, p, approval, hod, art):
    conn.execute("UPDATE users SET status='active', approved_by=?, approved_at=? WHERE id=? AND role='admin'",
                 (hod["id"], now_ist(), p["user_id"]))
    notify.send(conn, "Your Test In-charge account has been approved.", "/admin/", user_id=p["user_id"])
    return {"user_id": p["user_id"]}


def _admin_reject(conn, p, approval, hod, comment):
    conn.execute("UPDATE users SET status='disabled' WHERE id=? AND role='admin' AND status='pending'", (p["user_id"],))


approvals.register("admin_account", "Approve Test In-charge (admin) account", _admin_apply, always=True,
                   on_reject=_admin_reject, entity_type="user")


# ---------------------------------------------------------------- rule limits / numbering prefixes (always)
def _settings_apply(conn, p, approval, hod, art):
    for k, v in p["changes"].items():
        save_setting(conn, k, v, hod["id"])
    audit.log(conn, "config.update", "settings", None, None, p["changes"], user=hod)
    return {"changed": list(p["changes"])}


approvals.register("settings_change", "Change of rule limits / numbering", _settings_apply, always=True,
                   entity_type="settings")


# ---------------------------------------------------------------- optional approvals
def _accept_apply(conn, p, approval, hod, art):
    sc, rn = jobs.accept(conn, p["job_id"], _requester(conn, approval, hod), p["condition"], p["wi"], p.get("version"))
    return {"sample_code": sc, "report_no": rn}


approvals.register("accept_request", "Accept test request", _accept_apply,
                   validate=lambda c, p: _job_state(c, p["job_id"], workflow.SUBMITTED))


def _reject_apply(conn, p, approval, hod, art):
    jobs.reject(conn, p["job_id"], _requester(conn, approval, hod), p["reason"], p.get("version"), p.get("condition"))
    return {}


approvals.register("reject_request", "Reject test request", _reject_apply,
                   validate=lambda c, p: _job_state(c, p["job_id"], workflow.SUBMITTED))


def _assign_apply(conn, p, approval, hod, art):
    jobs.assign(conn, p["job_id"], _requester(conn, approval, hod), int(p["responsible"]),
                [tuple(x) for x in p.get("additional", [])], p.get("version"))
    return {}


approvals.register("assign_engineers", "Engineer assignment", _assign_apply,
                   validate=lambda c, p: _job_state(c, p["job_id"], workflow.ACCEPTED, workflow.ASSIGNED,
                                                    workflow.IN_TESTING, workflow.RETURNED))


def _return_apply(conn, p, approval, hod, art):
    jobs.return_to_engineer(conn, p["job_id"], _requester(conn, approval, hod), p["comment"], p.get("version"))
    return {}


approvals.register("return_job", "Return job to engineer after admin review", _return_apply,
                   validate=lambda c, p: _job_state(c, p["job_id"], workflow.LOCKED, workflow.ADMIN_REVIEW))
