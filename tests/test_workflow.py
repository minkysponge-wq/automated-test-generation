"""State machine, role separation and optimistic locking over HTTP."""
import io
import json
import os

import pytest

import config
from app import fields as F, jobs, request_form, workflow
from app.db import tx
from conftest import login


def test_transition_table():
    assert workflow.can("accept", "SUBMITTED", "admin")
    assert not workflow.can("accept", "SUBMITTED", "engineer")
    assert not workflow.can("issue", "IN_TESTING", "admin")
    assert not workflow.can("lock", "ASSIGNED", "engineer")      # must be IN_TESTING first
    assert workflow.can("return", "UNDER_REVIEW", "admin")


def test_roles_cannot_cross_portals(client):
    login(client, "customer", "customer1", "cust123")
    assert client.get("/admin/").status_code == 302              # redirected to admin login
    assert client.get("/engineer/").status_code == 302
    assert client.get("/customer/").status_code == 200
    # a customer password does not open the admin portal
    r = client.post("/admin/login", data={"username": "customer1", "password": "cust123"})
    assert r.status_code == 200 and b"Invalid" in r.data


def test_full_http_flow_with_conflict(client, conn):
    # ---- customer submits
    login(client, "customer", "customer1", "cust123")
    form = {k: v for k, v in request_form.demo_form().items() if k != "drawings"}
    form.update({f"test_{c}": "1" for c in ("winding_res", "oil_leak")})
    form.update({"declaration": "1", "drawing_number": "APT/1", "drawing_sheet": "1", "drawing_rev": "0"})
    r = client.post("/customer/requests/new", data=form)
    assert r.status_code == 302
    job_id = int(r.headers["Location"].rstrip("/").split("/")[-1])
    job = jobs.get_job(conn, job_id)
    assert job["status"] == "SUBMITTED"

    # ---- admin accepts (stale version -> 409-style conflict), then accepts properly
    login(client, "admin", "admin", "admin123")
    acc = {"physical_condition": "Suitable", "lab_capability": "Yes"}
    r = client.post(f"/admin/requests/{job_id}/accept", data=dict(acc, version=job["version"] + 5),
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 409 and "reload" in r.get_json()["error"]
    r = client.post(f"/admin/requests/{job_id}/accept", data=dict(acc, version=job["version"]))
    assert r.status_code == 302
    job = jobs.get_job(conn, job_id)
    assert job["sample_code"].startswith("HVD") and job["report_no"].startswith("CPRIBLRSCL")
    eng = conn.execute("SELECT id FROM users WHERE username='engineer1'").fetchone()["id"]
    r = client.post(f"/admin/requests/{job_id}/assign", data={"responsible": eng, "version": job["version"]})
    assert r.status_code == 302

    # ---- engineer uploads sample data for both stations
    login(client, "engineer", "engineer1", "eng123")
    for st in ("losses", "pressure"):
        with open(os.path.join(config.SAMPLE_DATA_DIR, F.stations()[st]["file"]), "rb") as fh:
            r = client.post(f"/engineer/jobs/{job_id}/upload/{st}", data={"file": (io.BytesIO(fh.read()), f"{st}.xlsx")},
                            content_type="multipart/form-data")
        assert r.status_code == 302
    rd = conn.execute("SELECT * FROM readings WHERE job_id=? AND key='wr.hv.n.bt.uv'", (job_id,)).fetchone()
    # correction without reason is refused
    r = client.post(f"/engineer/jobs/{job_id}/readings/{rd['id']}", json={"value": "3.9740", "reason": "", "version": rd["version"]})
    assert r.status_code == 400
    r = client.post(f"/engineer/jobs/{job_id}/readings/{rd['id']}",
                    json={"value": "3.9740", "reason": "re-read from meter", "version": rd["version"]})
    assert r.status_code == 200 and r.get_json()["reading"]["imported"] == "3.9736"
    # a second engineer working from the old version gets HTTP 409
    r = client.post(f"/engineer/jobs/{job_id}/readings/{rd['id']}",
                    json={"value": "3.9750", "reason": "x", "version": rd["version"]})
    assert r.status_code == 409 and "changed by" in r.get_json()["error"]
    # lock before verification is refused
    job = jobs.get_job(conn, job_id)
    r = client.post(f"/engineer/jobs/{job_id}/lock", data={"version": job["version"]}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 400
    for st in ("losses", "pressure"):
        assert client.post(f"/engineer/jobs/{job_id}/verify/{st}").status_code == 302
    job = jobs.get_job(conn, job_id)
    assert client.post(f"/engineer/jobs/{job_id}/lock", data={"version": job["version"]}).status_code == 302
    assert jobs.get_job(conn, job_id)["status"] == "LOCKED_BY_ENGINEER"
    # locked -> engineer edits refused
    r = client.post(f"/engineer/jobs/{job_id}/readings/{rd['id']}", json={"value": "1", "reason": "x", "version": rd["version"] + 1})
    assert r.status_code == 400

    # ---- admin opens review (starts it), returns, status poll works
    login(client, "admin", "admin", "admin123")
    assert client.get(f"/admin/requests/{job_id}").status_code == 200
    assert jobs.get_job(conn, job_id)["status"] == "UNDER_REVIEW"
    s = client.get(f"/api/jobs/{job_id}/status").get_json()
    assert s["locked"] and s["status"] == "UNDER_REVIEW"
    job = jobs.get_job(conn, job_id)
    r = client.post(f"/admin/requests/{job_id}/return", data={"comment": "", "version": job["version"]},
                    headers={"X-Requested-With": "fetch"})
    assert r.status_code == 400                                    # comment mandatory
    r = client.post(f"/admin/requests/{job_id}/return", data={"comment": "check LV temp", "version": job["version"]})
    assert jobs.get_job(conn, job_id)["status"] == "RETURNED"
    corr = conn.execute("SELECT * FROM corrections WHERE job_id=? AND reason='re-read from meter'", (job_id,)).fetchone()
    assert corr["old_value"] == "3.9736" and corr["new_value"] == "3.9740"


def test_engineer_registration_needs_approval(client, conn):
    r = client.post("/engineer/register", data={"username": "newEng", "password": "secret1", "password2": "secret1",
                                                "full_name": "New Eng", "email": "n@e.com", "phone": "1"})
    assert r.status_code == 302
    r = client.post("/engineer/login", data={"username": "newEng", "password": "secret1"})
    assert b"waiting for approval" in r.data


def test_admin_registration_needs_invite_code(client):
    data = {"username": "boss2", "password": "secret1", "password2": "secret1", "full_name": "B", "email": "b@b.com",
            "phone": "1", "invite_code": "wrong"}
    assert client.post("/admin/register", data=data).status_code == 200      # re-rendered with error
    data["invite_code"] = config.ADMIN_INVITE_CODE
    assert client.post("/admin/register", data=data).status_code == 302
