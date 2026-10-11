"""
Regression guards after the navigation/layout change: request-form auto-fill mapping, test auto-select
from the extraction, and the engineer's per-test Verify + Lock. No AI calls: the extraction comes from
tests/fixtures/request_form_extraction.json (a saved real read of the request form plus the proforma).
"""
import json
import os
from html.parser import HTMLParser

import config
from app import fields as F, jobs
from app.ai import intake
from app.db import tx
from conftest import login, user

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "request_form_extraction.json")


class _Inputs(HTMLParser):
    """name -> list of attribute dicts for every input/select/textarea."""

    def __init__(self):
        super().__init__()
        self.by_name = {}

    def handle_starttag(self, tag, attrs):
        if tag in ("input", "select", "textarea"):
            a = dict(attrs)
            if a.get("name"):
                self.by_name.setdefault(a["name"], []).append(a)


def _form_html(app, prefill, tests, drawings):
    from flask import g
    from app.routes.customer import _form_page
    with app.test_request_context("/customer/requests/new"):
        g.user, g.role = user(__import__("app").db.connect(), "customer1"), "customer"
        return _form_page(prefill, tests, drawings)


def _parse(html):
    p = _Inputs()
    p.feed(html)
    return p.by_name


def _fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def test_every_extracted_key_has_an_input(app, seeded):
    names = _parse(_form_html(app, {}, [], []))
    schema_keys = [s["key"] for s in intake.field_specs("request_form")]
    assert schema_keys
    assert [k for k in schema_keys if k not in names] == []          # every key the reader can return
    assert [k for k in _fixture()["fields"] if k not in names] == []  # every key in the saved extraction


def test_prefilled_form_contains_the_read_values(app, seeded):
    fx = _fixture()
    prefill = {k: v["value"] for k, v in fx["fields"].items() if v["value"] not in (None, "")}
    names = _parse(_form_html(app, prefill, fx["tests"], fx["drawings"]))

    def value(key):
        els = names[key]
        if els[0].get("type") == "radio":
            return next((e["value"] for e in els if "checked" in e), None)
        return els[0].get("value")

    expected = {"customer_name": "A.P.TRANSFORMERS", "serial_numbers": "1098", "pf_vector_group": "Dyn11",
                "pf_impedance": "4.50", "pf_max_tl50": "980", "pf_max_tl100": "2930"}
    for k, v in expected.items():
        assert value(k) == v, k
    assert value("rating").startswith("250 KVA")


def test_tests_are_ticked_from_the_extraction(app, seeded):
    fx = _fixture()
    names = _parse(_form_html(app, {}, fx["tests"], []))
    boxes = {n[len("test_"):]: els[0] for n, els in names.items() if n.startswith("test_")}
    assert set(boxes) == set(F.tests())                      # one checkbox per catalogue test, named test_<code>
    for code, box in boxes.items():
        assert "js-test" in box.get("class", "")             # the class the auto-fill JS ticks
        assert ("checked" in box) == (code in {"sc_withstand", "temp_rise", "pressure_type", "nlc_112", "oil_leak"}), code
    struck = [t for t in fx["test_lines"] if t["struck_out"]]
    assert struck and all(t["code"] is None for t in struck)   # unbalance current is struck out -> not requested
    assert not any("unbalance" in c for c in fx["tests"])


def test_engineer_verify_each_test_then_lock(app, seeded, conn):
    cust, admin, eng = user(conn, "customer1"), user(conn, "admin"), user(conn, "engineer1")
    with tx(conn):
        job_id, _ = jobs.create_request(conn, cust, {"customer_name": "Verify check", "rating": "250 kVA"},
                                        ["nll", "impedance", "winding_res", "nlc_112", "pressure_type", "oil_leak"])
    job = jobs.get_job(conn, job_id)
    with tx(conn):
        jobs.accept(conn, job_id, admin, {"physical_condition": "Suitable", "lab_capability": "Yes"}, {}, job["version"])
    job = jobs.get_job(conn, job_id)
    with tx(conn):
        jobs.assign(conn, job_id, admin, eng["id"], [], job["version"])

    c = app.test_client()
    login(c, "engineer", "engineer1", "eng12345")
    stations = {"losses": "losses.xlsx", "pressure": "pressure_oil_leakage.xlsx"}
    for st, fn in stations.items():
        with open(os.path.join(config.SAMPLE_DATA_DIR, fn), "rb") as fh:
            r = c.post(f"/engineer/jobs/{job_id}/upload/{st}", data={"file": (fh, fn)}, content_type="multipart/form-data")
        assert r.status_code in (200, 302)

    for st in stations:
        html = c.get(f"/engineer/jobs/{job_id}?station={st}").get_data(as_text=True)
        assert f'action="/engineer/jobs/{job_id}/verify/{st}"' in html, st
        assert "Mark verified" in html, st
        data = {}
        if 'name="missing_reason"' in html:                       # required values not in the logsheet
            data["missing_reason"] = "not measured in this test series"
        assert c.post(f"/engineer/jobs/{job_id}/verify/{st}", data=data).status_code == 302
    html = c.get(f"/engineer/jobs/{job_id}").get_data(as_text=True)
    lock = html[html.index('id="lock-job"') - 120:html.index('id="lock-job"') + 120]
    assert "disabled" not in lock
    st = {s["station"]: s for s in jobs.station_status(conn, jobs.get_job(conn, job_id))}
    assert all(s["verified"] for s in st.values())


def test_verify_all_and_panel_always_shown(app, seeded, conn):
    cust, admin, eng = user(conn, "customer1"), user(conn, "admin"), user(conn, "engineer1")
    with tx(conn):
        job_id, _ = jobs.create_request(conn, cust, {"customer_name": "Verify all check"}, ["nll", "impedance",
                                        "winding_res", "nlc_112", "pressure_type", "oil_leak"])
    job = jobs.get_job(conn, job_id)
    with tx(conn):
        jobs.accept(conn, job_id, admin, {"physical_condition": "Suitable", "lab_capability": "Yes"}, {}, job["version"])
    job = jobs.get_job(conn, job_id)
    with tx(conn):
        jobs.assign(conn, job_id, admin, eng["id"], [], job["version"])
    c = app.test_client()
    login(c, "engineer", "engineer1", "eng12345")
    for st, fn in (("losses", "losses.xlsx"), ("pressure", "pressure_oil_leakage.xlsx")):
        with open(os.path.join(config.SAMPLE_DATA_DIR, fn), "rb") as fh:
            c.post(f"/engineer/jobs/{job_id}/upload/{st}", data={"file": (fh, fn)}, content_type="multipart/form-data")
    html = c.get(f"/engineer/jobs/{job_id}").get_data(as_text=True)
    assert 'id="verify-lock"' in html and 'id="lock-job-top"' in html
    before = {s["station"]: s for s in jobs.station_status(conn, jobs.get_job(conn, job_id))}
    ready = [k for k, s in before.items() if not s["verified"] and s["n_values"] and not s["missing"] and not s["ai_pending"]]
    assert ready
    assert c.post(f"/engineer/jobs/{job_id}/verify-all").status_code == 302
    after = {s["station"]: s for s in jobs.station_status(conn, jobs.get_job(conn, job_id))}
    assert all(after[k]["verified"] for k in ready)
    if all(s["verified"] for s in after.values()):
        html = c.get(f"/engineer/jobs/{job_id}").get_data(as_text=True)
        top = html[html.index('id="lock-job-top"'):html.index('id="lock-job-top"') + 40]
        assert "disabled" not in top
    # a job already in technical review still shows the panel, with the buttons disabled and the reason
    html = c.get(f"/engineer/jobs/{seeded['review_job_id']}").get_data(as_text=True)
    assert 'id="verify-lock"' in html and "readings can no longer be changed" in html
