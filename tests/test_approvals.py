"""Generic approval engine: optional toggles, always-required types, settings and registry changes."""
import json

import pytest

from app import approvals, fields as F, jobs, request_form, workflow
from app.db import get_settings, save_setting, tx
from conftest import user


def _new_request(conn):
    with tx(conn):
        jid, _ = jobs.create_request(conn, user(conn, "customer1"), request_form.demo_form(), ["winding_res"])
    return jid


def test_optional_approval_toggle(seeded, conn):
    admin, hod = user(conn, "admin"), user(conn, "hod")
    jid = _new_request(conn)
    payload = {"job_id": jid, "condition": {"physical_condition": "Suitable", "lab_capability": "Yes"}, "wi": {}, "version": None}
    with tx(conn):
        save_setting(conn, "approval_accept_request", True, hod["id"])
    kind, aid = approvals.request_or_apply(conn, "accept_request", payload, admin, jid, "Accept")
    assert kind == "pending" and jobs.get_job(conn, jid)["status"] == "SUBMITTED"
    with pytest.raises(workflow.WorkflowError):                       # duplicate pending request refused
        with tx(conn):
            approvals.submit(conn, "accept_request", payload, admin, jid, "Accept again")
    result = approvals.decide(conn, aid, hod, True)
    assert result["sample_code"].startswith("HVD")
    assert jobs.get_job(conn, jid)["status"] == "ACCEPTED"
    with tx(conn):
        save_setting(conn, "approval_accept_request", False, hod["id"])
    jid2 = _new_request(conn)
    kind, result = approvals.request_or_apply(conn, "accept_request", dict(payload, job_id=jid2), admin, jid2, "Accept")
    assert kind == "applied" and jobs.get_job(conn, jid2)["status"] == "ACCEPTED"


def test_always_required_types_cannot_be_switched_off(seeded, conn):
    for t in ("issue_report", "template_activation", "registry_change", "amendment", "admin_account", "settings_change"):
        assert approvals.REGISTRY[t].always and approvals.required(conn, t)


def test_settings_change_applies_only_after_approval(seeded, conn):
    admin, hod = user(conn, "admin"), user(conn, "hod")
    with tx(conn):
        aid = approvals.submit(conn, "settings_change", {"changes": {"ratio_tolerance_pct": 0.4}}, admin, None, "Ratio 0.4")
    assert get_settings(conn)["ratio_tolerance_pct"] == 0.5
    approvals.decide(conn, aid, hod, True)
    assert get_settings(conn)["ratio_tolerance_pct"] == 0.4
    with tx(conn):
        save_setting(conn, "ratio_tolerance_pct", 0.5, hod["id"])


def test_registry_change_adds_field_to_excel_template(seeded, conn):
    """A new registry field appears in the generated Excel logsheet without code changes."""
    from app import excel_io
    admin, hod = user(conn, "admin"), user(conn, "hod")
    new = {"key": "ir.bt.core_e", "label": "IR core to earth BT", "unit": "GΩ", "dtype": "number", "dp": 2, "source": "reading",
           "test_code": "ir", "station": "routine", "section": "Insulation resistance (GΩ)", "sheet": "Logsheet",
           "row": "Core to earth", "col": "BT"}
    with tx(conn):
        aid = approvals.submit(conn, "registry_change", {"changes": [{"op": "upsert", "field": new}]}, admin, None, "Add field")
    assert F.field("ir.bt.core_e") is None
    approvals.decide(conn, aid, hod, True)
    F.invalidate()
    assert F.field("ir.bt.core_e")["label"] == "IR core to earth BT"
    wb = excel_io.build_station_template("routine")
    assert "ir_bt_core_e" in wb.defined_names


def test_amendment_needs_hod(seeded, conn):
    admin, hod = user(conn, "admin"), user(conn, "hod")
    jid = seeded["job_id"]
    with tx(conn):
        aid = approvals.submit(conn, "amendment", {"job_id": jid, "reason": "typo"}, admin, jid, "Amend")
    assert conn.execute("SELECT COUNT(*) FROM requests WHERE amends_id=?", (jid,)).fetchone()[0] == 0
    res = approvals.decide(conn, aid, hod, True)
    new = jobs.get_job(conn, res["new_job_id"])
    assert new["report_no"].endswith("/A1") and new["amends_id"] == jid
