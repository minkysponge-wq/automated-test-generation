"""
Unified document intake (app/ai/intake.py).

Normalisation, schema and own-template tests always run and never call Gemini (the client is
replaced by one that raises). The accuracy test sends the scanned reference forms to Gemini and
runs only when GEMINI_API_KEY is set and the suite is started with ATRG_TEST_AI=true.
"""
import os

import pytest

import config
from app.ai import client, intake

SAMPLES = os.path.join(config.BASE_DIR, "sample_data")


@pytest.fixture()
def no_ai(monkeypatch):
    """Any call to Gemini fails the test."""
    def boom(*a, **k):
        raise AssertionError("Gemini must not be called")
    monkeypatch.setattr(client, "extract_json", boom)
    monkeypatch.setattr(client, "connect", boom)


def test_normalisation():
    assert intake.normalise("pf_kva", "250 KVA") == ("250", True)
    assert intake.normalise("pf_hv_v", "11 kV") == ("11000", True)
    assert intake.normalise("pf_vector_group", "DYN 11") == ("Dyn11", True)
    assert intake.normalise("pf_vector_group", "Delta") == ("Delta", False)
    assert intake.normalise("pf_mfg", "10/25") == ("10/2025", True)
    assert intake.normalise("msme", True) == ("Yes", True)
    assert intake.normalise("pf_sealing", "non sealed")[0] == "Non-sealed"


def test_rating_split():
    out = {"rating": {"value": "250 kVA, 11kV/433V"}}
    intake.split_rating(out)
    assert out["pf_kva"]["value"] == "250" and out["pf_hv_v"]["value"] == "11000" and out["pf_lv_v"]["value"] == "433"
    assert out["pf_kva"]["confidence"] < intake.CHECK_BELOW          # derived values are always flagged for checking


def test_schema_comes_from_registry(seeded):
    keys = {s["key"] for s in intake.field_specs("request_form")}
    assert {"customer_name", "pf_vector_group", "pf_kva"} <= keys
    assert any(k.startswith("ir.") for k in (s["key"] for s in intake.field_specs("logsheet:ratio")))
    text, version = intake.prompt("request_form", intake.field_specs("request_form"))
    assert version == "request_form_v6" and "pf_vector_group" in text


def test_unsupported_file_type(tmp_path):
    p = tmp_path / "x.docx"
    p.write_bytes(b"x")
    with pytest.raises(client.AIError):
        intake.read_document(str(p), "request_form")


@pytest.mark.parametrize("fname, kind", [("customer_request_filled.xlsx", "request_form"),
                                         ("routine.xlsx", "logsheet:ratio"),
                                         ("short_circuit.xlsx", "logsheet:sc_withstand")])
def test_own_templates_import_without_ai(seeded, no_ai, fname, kind):
    res = intake.read_document(os.path.join(SAMPLES, fname), kind)
    assert res["method"] == "template" and res["calls"] == 0
    assert res["summary"]["read"] > 0 and res["html"]


def test_ai_disabled_falls_back(seeded, no_ai, monkeypatch):
    monkeypatch.setattr(config, "AI_ENABLED", False)
    monkeypatch.setattr(config, "GEMINI_API_KEY", "dummy-key-1234")
    assert client.disabled_reason() == "Automatic reading is turned off by the administrator"
    res = intake.read_document(os.path.join(config.BASE_DIR, "reference", "Customer_request_form.pdf"), "request_form")
    assert res["ai_skipped"] == "Automatic reading is turned off by the administrator"
    assert res["pages"]                                        # the scan is still shown for manual entry


def test_masked_key(monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "abcdefghijklWXYZ")
    assert client.masked_key().endswith("WXYZ") and "abcd" not in client.masked_key()


@pytest.mark.skipif(not client.available(), reason="Gemini not configured (set GEMINI_API_KEY and ATRG_TEST_AI=true)")
def test_accuracy_on_reference_documents(seeded):
    import sys
    sys.path.insert(0, os.path.join(config.BASE_DIR, "tools"))
    import ai_accuracy as A
    for name, fname, kind, expected in A.CASES + [A.foreign_case()]:
        path = fname if os.path.isabs(fname) else os.path.join(A.REF, fname)
        ok, _rows = A.evaluate(intake.read_document(path, kind, seeded["conn"]), expected)
        assert ok / len(expected) >= 0.8, f"{name}: {ok}/{len(expected)}"


def test_handwritten_v_read_as_u_is_corrected():
    """Saved read of the request form (11-10-2026): '250 KUA / 11 KV / 433V' must give kVA and fill the proforma."""
    assert intake.fix_unit_ocr("250 KUA / 11 KU / 433V") == "250 kVA / 11 kV / 433V"
    assert intake.fix_unit_ocr("25kua") == "25 kVA"
    assert intake.fix_unit_ocr("KUALA 25 units") == "KUALA 25 units"      # only directly after a number
    specs = intake.field_specs("request_form")
    out = {}
    intake._apply([{"key": "rating", "value": "250 KUA / 11 KV / 433V, 3PH, 50 Hz Copper wound", "confidence": 0.9,
                    "source": "pdf_page 1"},
                   {"key": "manufacturer", "value": "250 KUA / 11 KV / 433V EEL1", "confidence": 0.9,
                    "source": "pdf_page 1"}], specs, out)
    intake.split_rating(out)
    assert out["rating"]["value"].startswith("250 kVA / 11 KV")
    assert out["manufacturer"]["value"].startswith("250 kVA")
    assert (out["pf_kva"]["value"], out["pf_hv_v"]["value"], out["pf_lv_v"]["value"]) == ("250", "11000", "433")
