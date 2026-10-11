"""
Read-only view of the audit log for the Test In-charge and the HOD: plain-English action labels,
filters, references (sample code / report number) and pagination. Nothing here writes to audit_log
except through audit.log() (export and chain verification are themselves logged).
"""
import json
from datetime import datetime, timedelta

from .db import IST

PAGE_SIZE = 50

ACTION_LABELS = {
    "request.submit": "Request submitted",
    "job.accept": "Request accepted",
    "job.reject": "Request rejected",
    "job.assign": "Engineers assigned",
    "job.start_testing": "Testing started",
    "job.lock": "Job locked",
    "job.begin_review": "Review started",
    "job.return": "Job returned to engineer",
    "job.forward": "Forwarded to HOD",
    "job.hod_reject": "Returned by HOD",
    "job.issue": "Report issued",
    "job.amend": "Amendment created",
    "readings.import": "Readings imported",
    "readings.confirm_ai": "AI readings confirmed",
    "readings.clear_ai": "AI readings cleared",
    "reading.correct": "Value corrected",
    "station.verify": "Station verified",
    "station.unverify": "Station verification cleared",
    "file.upload": "File uploaded",
    "ai.read_document": "Document read by AI",
    "ai.task_done": "AI reading finished",
    "ai.task_failed": "AI reading failed",
    "approval.request": "Approval requested",
    "approval.approve": "Approval granted",
    "approval.reject": "Approval rejected",
    "approval.cancel": "Approval cancelled",
    "approval.apply_failed": "Approval could not be applied",
    "report.download": "Report downloaded",
    "report.verify": "Report integrity checked",
    "report.public_verify": "Report verified via QR link",
    "wi.download": "Work instruction downloaded",
    "template.upload": "Report template uploaded",
    "template.map_slot": "Template field mapped",
    "template.settings": "Template settings changed",
    "template.section": "Template section changed",
    "template.activate": "Report template activated",
    "registry.change": "Field registry changed",
    "config.update": "Settings changed",
    "engineer.approve": "Engineer account approved",
    "engineer.reject": "Engineer account rejected",
    "engineer.disable": "Engineer account disabled",
    "engineer.enable": "Engineer account enabled",
    "user.register": "Account registered",
    "user.create_hod": "HOD account created",
    "login.success": "Signed in",
    "login.fail": "Sign-in failed",
    "system.backup": "Database backup",
    "audit.export": "Activity log exported",
    "audit.verify_chain": "Activity log verified",
}


def action_label(action):
    if action in ACTION_LABELS:
        return ACTION_LABELS[action]
    return action.replace(".", ": ").replace("_", " ").capitalize()


def default_from():
    return (datetime.now(IST) - timedelta(days=6)).strftime("%Y-%m-%d")


def effective_args(args):
    """Request args with the default date range (last 7 days) filled in, unless `all=1` or a date is given."""
    out = {k: v for k, v in args.items() if v not in (None, "") and k != "page"}
    if "from" not in out and "to" not in out and out.get("all") != "1":
        out["from"] = default_from()
    return out


def build_query(args):
    """SELECT over audit_log for the filters in `args` (newest first)."""
    sql, params = "SELECT * FROM audit_log WHERE 1=1", []
    for key, col in (("user", "username"), ("action", "action"), ("entity", "entity_type")):
        v = (args.get(key) or "").strip()
        if v:
            sql += f" AND {col} LIKE ?"
            params.append(f"%{v}%")
    if (args.get("role") or "").strip():
        sql += " AND role=?"
        params.append(args["role"].strip())
    if args.get("type"):                        # exact action code from the "Action" select
        sql += " AND action=?"
        params.append(args["type"])
    if args.get("entity_id"):
        sql += " AND entity_id=?"
        params.append(args["entity_id"])
    if args.get("from"):
        sql += " AND ts >= ?"
        params.append(args["from"])
    if args.get("to"):
        sql += " AND ts < ?"                    # whole "to" day included
        params.append(args["to"] + "T99")
    q = (args.get("q") or "").strip()
    if q:                                       # sample code / report number / request reference
        like = f"%{q}%"
        sql += (" AND ((entity_type='job' AND entity_id IN (SELECT CAST(id AS TEXT) FROM requests WHERE sample_code LIKE ? "
                "OR report_no LIKE ? OR ref LIKE ?)) "
                "OR (entity_type='report' AND entity_id IN (SELECT CAST(id AS TEXT) FROM reports WHERE report_no LIKE ?)) "
                "OR (entity_type='approval' AND entity_id IN (SELECT CAST(a.id AS TEXT) FROM approval_requests a "
                "JOIN requests r ON a.entity_type='job' AND CAST(r.id AS TEXT)=a.entity_id "
                "WHERE r.sample_code LIKE ? OR r.report_no LIKE ?)) "
                "OR after_json LIKE ?)")
        params += [like] * 7
    return sql + " ORDER BY id DESC", params


def _short(js, limit=120):
    if not js:
        return ""
    try:
        data = json.loads(js)
    except ValueError:
        return js[:limit]
    if isinstance(data, dict):
        parts = [f"{k}: {v}" for k, v in data.items() if not isinstance(v, (dict, list)) and v not in (None, "")]
        text = ", ".join(parts)
    else:
        text = str(data)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def references(conn, rows):
    """{audit row id: (label, job_id)} - sample code / report number of the record each row is about."""
    job_ids, report_ids, approval_ids = set(), set(), set()
    for r in rows:
        if r["entity_id"] is None:
            continue
        {"job": job_ids, "report": report_ids, "approval": approval_ids}.get(r["entity_type"], set()).add(r["entity_id"])

    def _in(ids):
        return ",".join("?" * len(ids))

    approvals = {}
    if approval_ids:
        for a in conn.execute(f"SELECT id, entity_id FROM approval_requests WHERE entity_type='job' AND "
                              f"CAST(id AS TEXT) IN ({_in(approval_ids)})", list(approval_ids)):
            approvals[str(a["id"])] = a["entity_id"]
            job_ids.add(a["entity_id"])
    reports = {}
    if report_ids:
        for rp in conn.execute(f"SELECT id, job_id, report_no FROM reports WHERE CAST(id AS TEXT) IN ({_in(report_ids)})",
                               list(report_ids)):
            reports[str(rp["id"])] = (rp["report_no"], rp["job_id"])
    jobs = {}
    if job_ids:
        for j in conn.execute(f"SELECT id, sample_code, report_no, ref FROM requests WHERE CAST(id AS TEXT) "
                              f"IN ({_in(job_ids)})", list(job_ids)):
            label = " / ".join(x for x in (j["sample_code"], j["report_no"]) if x) or j["ref"]
            jobs[str(j["id"])] = (label, j["id"])
    out = {}
    for r in rows:
        eid = r["entity_id"]
        if r["entity_type"] == "job" and eid in jobs:
            out[r["id"]] = jobs[eid]
        elif r["entity_type"] == "report" and eid in reports:
            out[r["id"]] = reports[eid]
        elif r["entity_type"] == "approval" and approvals.get(eid) in jobs:
            out[r["id"]] = jobs[approvals[eid]]
    return out


def page(conn, args):
    """Rows for one page of the activity log plus paging info."""
    eff = effective_args(args)
    try:
        n = max(1, int(args.get("page") or 1))
    except ValueError:
        n = 1
    sql, params = build_query(eff)
    rows = conn.execute(sql + " LIMIT ? OFFSET ?", params + [PAGE_SIZE + 1, (n - 1) * PAGE_SIZE]).fetchall()
    has_next = len(rows) > PAGE_SIZE
    rows = rows[:PAGE_SIZE]
    refs = references(conn, rows)
    items = [{"row": r, "label": action_label(r["action"]), "detail": _short(r["after_json"]),
              "ref": refs.get(r["id"])} for r in rows]
    used = [a for (a,) in conn.execute("SELECT DISTINCT action FROM audit_log")]
    types = sorted(((a, action_label(a)) for a in used), key=lambda t: t[1])
    return {"items": items, "page": n, "has_next": has_next, "args": eff, "types": types}


def fmt_ts(ts):
    """2026-10-10T14:03:22+05:30 -> 10-10-2026 14:03:22 (stored in IST)."""
    if not ts:
        return ""
    return f"{ts[8:10]}-{ts[5:7]}-{ts[0:4]} {ts[11:19]}"
