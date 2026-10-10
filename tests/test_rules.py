"""Calculations (registry formulas + rules.yaml, simpleeval) reproduce the values on the scanned CPRI logsheets."""
import os

import pytest

import config
from app import excel_io, fields as F, request_form, rules

ALL = list(F.tests())


@pytest.fixture(scope="module")
def evaluated():
    rd = {}
    for st, meta in F.stations().items():
        res = excel_io.import_workbook(os.path.join(config.SAMPLE_DATA_DIR, meta["file"]), st, ALL)
        rd.update({k: v[0] for k, v in res.values.items()})
    return rules.evaluate(rd, request_form.demo_form(), ALL, config.DEFAULT_SETTINGS), rd


def _r(results, code):
    return next(r for r in results if r["rule_code"] == code)


def test_winding_temperature_rise(evaluated):
    (results, calc), _ = evaluated
    assert round(calc["calc.tr.hv"], 1) == 39.7       # 4.0464/3.5540 x (235+25.9) - 235 - 22.85 + 0.475
    assert round(calc["calc.tr.lv"], 1) == 34.3
    assert _r(results, "tr_hv")["passed"] is True


def test_no_load_current_percent(evaluated):
    (results, calc), _ = evaluated
    assert round(calc["calc.i_rated"], 2) == 333.34
    assert round(calc["calc.nlc100.bt"], 2) == 0.51
    assert round(calc["calc.nlc112"], 2) == 1.28
    assert "6.667 A" in _r(results, "nlc100_bt")["limit"] and "16.667 A" in _r(results, "nlc112")["limit"]


def test_reactance_change(evaluated):
    (results, calc), _ = evaluated
    assert [round(calc[f"calc.xchg.{t}"], 3) for t in ("nt", "ht", "lt")] == [-0.163, -0.092, -0.205]


def test_resistance_correction_to_75(evaluated):
    (_, calc), rd = evaluated
    avg = (3.9736 + 3.9558 + 3.9690) / 3
    assert calc["calc.r75.hv.bt.n"] == pytest.approx(avg * 310 / (235 + 24.7))


def test_all_demo_rules_pass(evaluated):
    (results, _), _ = evaluated
    fails = [r for r in results if r["passed"] is False]
    assert not fails, fails


def test_failures_are_detected():
    form = request_form.demo_form()
    rd = {"nll.bt.i": "8.0", "calc.nt.bt.tl50": "1000", "calc.nt.bt.z75": "5.2", "ol.obs": "Leakage at drain valve",
          "ol.head_kpa": "14", "ol.test_kpa": "20"}
    results, _ = rules.evaluate(rd, form, ["nll", "impedance", "oil_leak"], config.DEFAULT_SETTINGS)
    failed = {r["rule_code"] for r in results if r["passed"] is False}
    assert {"nlc100_bt", "eel_tl50_bt", "impedance_bt", "ol_obs", "ol_pressure"} <= failed


def test_ratio_nominal_uses_vector_group():
    form = request_form.demo_form()
    assert rules.nominal_ratio(form, 0.0) == pytest.approx(11000 / (433 / 3 ** 0.5))
    form["pf_vector_group"] = "Yyn0"
    assert rules.nominal_ratio(form, 0.0) == pytest.approx(11000 / 433)


def test_rules_never_use_python_eval():
    import inspect
    from app import rules
    src = inspect.getsource(rules)
    assert " eval(" not in src.replace("simple_eval", "") and "exec(" not in src
