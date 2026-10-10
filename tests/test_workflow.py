"""State machine, role separation, optimistic locking and the HOD authorization step over HTTP."""
import io
import os

import pytest

import config
from app import approvals, fields as F, jobs, request_form, workflow
from app.db import tx
from conftest import login, user


def test_transition_table():
    assert workflow.can("accept", "SUBMITTED", "admin")
    assert not workflow.can("accept", "SUBMITTED", "engineer")
    assert not workflow.can("lock", "ASSIGNED", "engineer")          # must be IN_TESTING first
    assert workflow.can("forward", "ADMIN_REVIEW", "admin")
    assert not workflow.can("issue", "ADMIN_REVIEW", "admin")        # an admin can never issue
    assert not workflow.can("issue", "PENDING_HOD", "admin")
    assert workflow.can("issue", "PENDING_HOD", "hod")


def test_admin_cannot_issue_without_hod(seeded, conn):
    admin = user(conn, "admin")
    with pytest.raises(workflow.WorkflowError):
        with tx(conn):
            workflow.apply(conn, seeded["review_job_id"], "issue", admin)
    assert approvals.required(conn, "issue_report")                  # not switchable


def test_roles_cannot_cross_portals(client):
    login(client, "customer", "customer1", "cust12345")
    assert client.get("/admin/").status_code == 302
    assert client.get("/hod/").status_code == 302
    assert client.get("/engineer/").status_code == 302
    assert client.get("/customer/").status_code == 200
    r = client.post("/hod/login", data={"username": "customer1", "password": "cust12345"})
    assert r.status_code == 200 and b"Invalid" in r.data


def test_full_http_flow(client, conn):
    # ---- customer submits
    login(client, "customer", "customer1", "cust12345")
    form = {k: v for k, v in request_form.demo_form().items() if k != "drawings"}
    form.update({f"test_{c}": "1" for c in ("winding_res", "oil_leak")})
    form.update({"declaration": "1", "drawing_number": "APT/1", "drawing_sheet": "1", "drawing_rev": "0"})
    r = client.post("/customer/requests/new", data=form)
    assert r.status_code == 302
    job_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    job = jobs.get_job(conn, job_id)
    assert job["status"] == "SUBMITTED" and job["ref"].startswith("REQ-")

    # ---- admin accepts (stale version -> 409), then properly; assigns
    login(client, "admin", "admin", "admin12345")
    acc = {"physical_condition": "Suitable", "lab_capability": "Yes"}
    r = client.post(f"/admin/requests/{job_id}/accept", data=dict(acc, version=job["version"] + 5),
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 409 and "Reload" in r.get_json()["error"]
    assert client.post(f"/admin/requests/{job_id}/accept", data=dict(acc, version=job["version"])).status_code == 302
    job = jobs.get_job(conn, job_id)
    assert job["sample_code"].startswith("HVD") and job["report_no"].startswith("CPRIBLRSCL")
    eng = user(conn, "engineer1")
    assert client.post(f"/admin/requests/{job_id}/assign", data={"responsible": eng["id"], "version": job["version"]}).status_code == 302

    # ---- engineer uploads, corrects (reason required, conflict on stale version), verifies, locks
    login(client, "engineer", "engineer1", "eng12345")
    assert client.get(f"/engineer/jobs/{job_id}/template/losses.xlsx").status_code == 200
    for st in ("losses", "pressure"):
        with open(os.path.join(config.SAMPLE_DATA_DIR, F.stations()[st]["file"]), "rb") as fh:
            r = client.post(f"/engineer/jobs/{job_id}/upload/{st}", data={"file": (io.BytesIO(fh.read()), f"{st}.xlsx")},
                            content_type="multipart/form-data")
        assert r.status_code == 302
    rd = conn.execute("SELECT * FROM readings WHERE job_id=? AND key='wr.hv.n.bt.uv'", (job_id,)).fetchone()
    r = client.post(f"/engineer/jobs/{job_id}/readings/{rd['id']}", json={"value": "3.9740", "reason": "", "version": rd["version"]})
    assert r.status_code == 400
    r = client.post(f"/engineer/jobs/{job_id}/readings/{rd['id']}",
                    json={"value": "3.9740", "reason": "re-read from meter", "version": rd["version"]})
    assert r.status_code == 200 and r.get_json()["reading"]["imported"] == "3.9736"
    r = client.post(f"/engineer/jobs/{job_id}/readings/{rd['id']}", json={"value": "3.9750", "reason": "x", "version": rd["version"]})
    assert r.status_code == 409 and "changed by" in r.get_json()["error"]
    job = jobs.get_job(conn, job_id)
    r = client.post(f"/engineer/jobs/{job_id}/lock", data={"version": job["version"]}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 400                                         # not verified yet
    for st in ("losses", "pressure"):
        assert client.post(f"/engineer/jobs/{job_id}/verify/{st}").status_code == 302
    job = jobs.get_job(conn, job_id)
    assert client.post(f"/engineer/jobs/{job_id}/lock", data={"version": job["version"]}).status_code == 302
    r = client.post(f"/engineer/jobs/{job_id}/readings/{rd['id']}", json={"value": "1", "reason": "x", "version": rd["version"] + 1})
    assert r.status_code == 400                                         # engineer can't edit after lock

    # ---- admin review -> forward; HOD approves -> issued and sealed
    login(client, "admin", "admin", "admin12345")
    assert client.get(f"/admin/requests/{job_id}").status_code == 200
    job = jobs.get_job(conn, job_id)
    assert job["status"] == "ADMIN_REVIEW"
    assert client.post(f"/admin/requests/{job_id}/forward", data={"version": job["version"]}).status_code == 302
    assert jobs.get_job(conn, job_id)["status"] == "PENDING_HOD"
    ap = conn.execute("SELECT * FROM approval_requests WHERE type='issue_report' AND entity_id=? AND status='pending'",
                      (str(job_id),)).fetchone()
    assert ap is not None
    assert client.post(f"/hod/approvals/{ap['id']}/decide", data={"decision": "approve"}).status_code == 302  # admin session only
    assert jobs.get_job(conn, job_id)["status"] == "PENDING_HOD"
    login(client, "hod", "hod", "hod12345")
    r = client.post(f"/hod/approvals/{ap['id']}/decide", data={"decision": "approve", "comment": "ok"})
    assert r.status_code == 302
    job = jobs.get_job(conn, job_id)
    assert job["status"] == "ISSUED" and job["is_sealed"] == 1
    rep = conn.execute("SELECT * FROM reports WHERE job_id=?", (job_id,)).fetchone()
    assert rep["issued_by"] == user(conn, "hod")["id"] and rep["forwarded_by"] == user(conn, "admin")["id"]


def test_hod_rejection_returns_to_admin_review(seeded, conn):
    admin, hod = user(conn, "admin"), user(conn, "hod")
    jid = seeded["review_job_id"]
    with tx(conn):
        aid = approvals.submit(conn, "issue_report", {"job_id": jid, "version": None}, admin, jid, "Authorize")
    assert jobs.get_job(conn, jid)["status"] == "PENDING_HOD"
    with pytest.raises(workflow.WorkflowError):
        approvals.decide(conn, aid, hod, False, "")                    # comment mandatory
    approvals.decide(conn, aid, hod, False, "Check the LV resistance values")
    job = jobs.get_job(conn, jid)
    assert job["status"] == "ADMIN_REVIEW" and "LV resistance" in job["return_comment"]


def test_registration_rules(client, conn):
    data = {"username": "newEng", "password": "short", "password2": "short", "full_name": "N", "email": "n@e.com", "phone": "1"}
    assert client.post("/engineer/register", data=data).status_code == 200          # password < 8 rejected
    data.update(password="secret123", password2="secret123")
    assert client.post("/engineer/register", data=data).status_code == 302
    assert b"waiting for approval" in client.post("/engineer/login", data={"username": "newEng", "password": "secret123"}).data
    # admin registration -> pending + approval request for the HOD
    data = {"username": "boss2", "password": "secret123", "password2": "secret123", "full_name": "B", "email": "b@b.com", "phone": "1"}
    assert client.post("/admin/register", data=data).status_code == 302
    u = user(conn, "boss2")
    assert u["status"] == "pending"
    ap = conn.execute("SELECT * FROM approval_requests WHERE type='admin_account' AND entity_id=?", (str(u["id"]),)).fetchone()
    approvals.decide(conn, ap["id"], user(conn, "hod"), True)
    assert user(conn, "boss2")["status"] == "active"
    assert client.get("/hod/register").status_code == 404                           # no public HOD registration


def test_verify_with_missing_values_needs_reason_and_still_issues(client, conn):
    login(client, "customer", "customer1", "cust12345")
    form = {k: v for k, v in request_form.demo_form().items() if k != "drawings"}
    form.update({"test_oil_leak": "1", "declaration": "1", "drawing_number": "APT/1", "drawing_sheet": "1", "drawing_rev": "0"})
    job_id = int(client.post("/customer/requests/new", data=form).headers["Location"].rstrip("/").split("/")[-1])
    login(client, "admin", "admin", "admin12345")
    job = jobs.get_job(conn, job_id)
    client.post(f"/admin/requests/{job_id}/accept", data={"physical_condition": "Suitable", "lab_capability": "Yes",
                                                          "version": job["version"]})
    job = jobs.get_job(conn, job_id)
    client.post(f"/admin/requests/{job_id}/assign", data={"responsible": user(conn, "engineer1")["id"], "version": job["version"]})
    login(client, "engineer", "engineer1", "eng12345")
    hdr = {"X-Requested-With": "fetch"}
    assert client.post(f"/engineer/jobs/{job_id}/verify/pressure", headers=hdr).status_code == 400       # nothing entered
    assert client.post(f"/engineer/jobs/{job_id}/verify/pressure", data={"missing_reason": "no"}, headers=hdr).status_code == 400
    r = client.post(f"/engineer/jobs/{job_id}/verify/pressure", data={"missing_reason": "not measured in this test series"})
    assert r.status_code == 302
    log = conn.execute("SELECT * FROM audit_log WHERE action='station.verify' AND entity_id=? ORDER BY id DESC",
                       (str(job_id),)).fetchone()
    assert "not measured in this test series" in str(dict(log))
    job = jobs.get_job(conn, job_id)
    assert client.post(f"/engineer/jobs/{job_id}/lock", data={"version": job["version"]}).status_code == 302
    login(client, "admin", "admin", "admin12345")
    assert client.get(f"/admin/requests/{job_id}").status_code == 200       # opens the review
    job = jobs.get_job(conn, job_id)
    r = client.post(f"/admin/requests/{job_id}/forward", data={"version": job["version"]}, headers=hdr)
    assert r.status_code in (200, 302), r.get_data(as_text=True)[:400]
    ap = conn.execute("SELECT id FROM approval_requests WHERE type='issue_report' AND entity_id=? AND status='pending'",
                      (str(job_id),)).fetchone()
    login(client, "hod", "hod", "hod12345")
    client.post(f"/hod/approvals/{ap['id']}/decide", data={"decision": "approve", "comment": "ok"})
    assert jobs.get_job(conn, job_id)["status"] == "ISSUED"                   # report generated with blanks


def test_lock_with_missing_values(client, conn):
    login(client, "customer", "customer1", "cust12345")
    form = {k: v for k, v in request_form.demo_form().items() if k != "drawings"}
    form.update({"test_oil_leak": "1", "test_winding_res": "1", "declaration": "1", "drawing_number": "APT/1",
                 "drawing_sheet": "1", "drawing_rev": "0"})
    job_id = int(client.post("/customer/requests/new", data=form).headers["Location"].rstrip("/").split("/")[-1])
    login(client, "admin", "admin", "admin12345")
    job = jobs.get_job(conn, job_id)
    client.post(f"/admin/requests/{job_id}/accept", data={"physical_condition": "Suitable", "lab_capability": "Yes",
                                                          "version": job["version"]})
    job = jobs.get_job(conn, job_id)
    client.post(f"/admin/requests/{job_id}/assign", data={"responsible": user(conn, "engineer1")["id"], "version": job["version"]})
    login(client, "engineer", "engineer1", "eng12345")
    with open(os.path.join(config.SAMPLE_DATA_DIR, F.stations()["pressure"]["file"]), "rb") as fh:   # starts testing
        client.post(f"/engineer/jobs/{job_id}/upload/pressure", data={"file": (io.BytesIO(fh.read()), "p.xlsx")},
                    content_type="multipart/form-data")
    assert b"Lock with missing values" in client.get(f"/engineer/jobs/{job_id}").data
    job = jobs.get_job(conn, job_id)
    hdr = {"X-Requested-With": "fetch"}
    assert client.post(f"/engineer/jobs/{job_id}/lock", data={"version": job["version"]}, headers=hdr).status_code == 400
    r = client.post(f"/engineer/jobs/{job_id}/lock", data={"version": job["version"], "missing_reason": "not in the logsheets"})
    assert r.status_code == 302 and jobs.get_job(conn, job_id)["status"] != "IN_TESTING"
