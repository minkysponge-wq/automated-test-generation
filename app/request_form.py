"""
Customer request form (CPRI/QAF/01A sheets 1-2 + Proforma for transformers).

The fields come from the field registry (source = request / proforma), grouped
by their `section`. The HTML form, the request Excel, the AI extraction schema
and the validation are all generated from it, so they can never drift apart.
"""
import os
from functools import lru_cache

import yaml

import config
from . import fields as F

DECLARATION = ("We hereby declare that the information furnished above is true and correct. We agree to abide by "
               "the terms and conditions of CPRI for testing, accept the test methods and decision rule indicated "
               "above, and understand that the test results relate only to the sample(s) tested.")
DEMO_YAML = os.path.join(config.SAMPLE_DATA_DIR, "demo_request.yaml")


def sections():
    """[{title, fields:[{name,label,type,options,required}]}] in registry order."""
    out, index = [], {}
    for f in F.request_fields():
        title = f.get("section") or "Request"
        if title not in index:
            index[title] = {"title": title, "fields": []}
            out.append(index[title])
        opts = f.get("options")
        if opts and f.get("option_labels"):
            opts = [[o, l] for o, l in zip(opts, f["option_labels"])]
        index[title]["fields"].append({"name": f["key"], "label": f["label"], "type": f.get("ui") or "text",
                                       "options": opts, "required": f.get("required", False), "unit": f.get("unit")})
    return out


# kept for modules that iterate the form definition
class _Sections:
    def __iter__(self):
        return iter(sections())


SECTIONS = _Sections()


def all_fields():
    for sec in sections():
        for fd in sec["fields"]:
            yield fd


@lru_cache(maxsize=1)
def _demo():
    with open(DEMO_YAML, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def demo_form():
    """Demo request (A.P. Transformers) as stored in requests.form_json."""
    return dict(_demo())


def parse_form(form, test_codes_all):
    """Turn a submitted HTML form (werkzeug MultiDict) into (data, tests, errors{field: message})."""
    data, errors = {}, {}
    for fd in all_fields():
        if fd["type"] == "drawings":
            nums = form.getlist("drawing_number")
            sheets = form.getlist("drawing_sheet")
            revs = form.getlist("drawing_rev")
            data["drawings"] = [{"number": n.strip(), "sheet": s.strip(), "rev": r.strip()}
                                for n, s, r in zip(nums, sheets, revs) if n.strip()]
            continue
        val = (form.get(fd["name"]) or "").strip()
        if fd.get("required") and not val:
            errors[fd["name"]] = "Required."
        if val and fd["type"] == "number":
            try:
                float(val)
            except ValueError:
                errors[fd["name"]] = "Enter a number."
        data[fd["name"]] = val
    tests = [c for c in test_codes_all if form.get(f"test_{c}")]
    if not tests:
        errors["tests"] = "Select at least one test."
    if data.get("soc_required") == "Yes" and not data.get("decision_rule"):
        errors["decision_rule"] = "Choose a decision rule for the statement of conformity."
    if not form.get("declaration"):
        errors["declaration"] = "The declaration must be accepted before submitting."
    return data, tests, errors


def from_excel(values, test_codes_all):
    """Map parse_request_workbook() output to {field: value} + test list for the form auto-fill."""
    data = {k: v for k, v in values.items() if not k.startswith(("drawing", "test_"))}
    drawings = []
    for i in range(1, 6):
        n = values.get(f"drawing{i}_number")
        if n:
            drawings.append({"number": n, "sheet": values.get(f"drawing{i}_sheet", ""),
                             "rev": values.get(f"drawing{i}_rev", "")})
    tests = [c for c in test_codes_all if str(values.get(f"test_{c}", "")).strip().lower() in ("yes", "y", "1", "true", "x")]
    return {"fields": data, "drawings": drawings, "tests": tests}
