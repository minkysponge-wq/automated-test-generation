"""
Definition of the customer request form.

Mirrors CPRI/QAF/01A (Customer request form, sheets 1-2) and the
"Proforma for transformers" (manufacturer-declared data). The HTML form,
the downloadable request Excel and the validation all come from this list,
so they can never drift apart. `demo` values are the A.P. Transformers
demo sample (used by seed.py and sample_data/customer_request_filled.xlsx).
"""

YN = ["Yes", "No"]

SECTIONS = [
    {"title": "1. Customer & sample (Request form - sheet 1)", "fields": [
        {"name": "customer_name", "label": "Name of the customer", "type": "text", "required": True,
         "demo": "M/s. A.P. Transformers"},
        {"name": "customer_address", "label": "Address of the customer", "type": "textarea", "required": True,
         "demo": "SP24 & 95H/1(SP), Ambattur Indl. Estate, Chennai-600058"},
        {"name": "samples", "label": "Sample(s) to be tested", "type": "text", "required": True,
         "demo": "Three Phase Distribution Transformer"},
        {"name": "rating", "label": "Rating", "type": "text", "required": True,
         "demo": "250 kVA, 11000 / 433 V"},
        {"name": "sample_description", "label": "Description of test sample", "type": "textarea",
         "demo": "250 kVA, 11 kV / 433 V, 3-phase, oil-immersed, ONAN, outdoor distribution transformer"},
        {"name": "sample_type", "label": "Type", "type": "text", "demo": "Outdoor, Non-sealed, Oil immersed"},
        {"name": "serial_numbers", "label": "Serial number(s)", "type": "text", "required": True, "demo": "1098"},
        {"name": "manufacturer", "label": "Manufacturer's name & address", "type": "textarea", "required": True,
         "demo": "M/s. A.P. Transformers, SP24 & 95H/1(SP), Ambattur Indl. Estate, Chennai-600058"},
        {"name": "drawings", "label": "Drawing number(s) with sheet no. and revision", "type": "drawings",
         "demo": [{"number": "APT/250-11/001", "sheet": "1 of 1", "rev": "0"},
                  {"number": "APT/250-11/001B", "sheet": "1 of 1", "rev": "0"}]},
        {"name": "customer_requirement", "label": "Customer's requirement", "type": "text", "demo": "Nil"},
        {"name": "criteria", "label": "Criteria for evaluation (standard / specification)", "type": "text",
         "required": True, "demo": "IS 1180 (Part 1): 2014 (with Amendment No.4: March 2021)"},
        {"name": "num_samples", "label": "Number of samples", "type": "number", "required": True, "demo": "1"},
        {"name": "take_back", "label": "Sample storage / handling / disposal - take back the sample?",
         "type": "radio", "options": YN, "demo": "Yes"},
        {"name": "tests_details", "label": "Details of test(s) requested", "type": "textarea",
         "demo": "Type tests, all routine tests and special tests as per IS 1180 (Part 1): 2014"},
        {"name": "mounting_instructions", "label": "Specific mounting / connection instructions", "type": "textarea",
         "demo": "Nil"},
        {"name": "witness_customer", "label": "Witnessing person - customer's representative", "type": "text",
         "demo": "Mr. Vivekanand Krishna"},
        {"name": "witness_other", "label": "Witnessing person - other than customer", "type": "text", "demo": "None"},
        {"name": "dispatch_to", "label": "Test report to be dispatched to", "type": "textarea",
         "demo": "M/s. A.P. Transformers, Ambattur Indl. Estate, Chennai-600058"},
        {"name": "dispatch_mode", "label": "Mode of dispatch", "type": "select",
         "options": ["By hand", "Speed post", "Courier", "E-mail (scanned copy)"], "demo": "Courier"},
        {"name": "additional_reports", "label": "Number of additional reports required", "type": "number", "demo": "0"},
    ]},
    {"title": "2. Request form - sheet 2", "fields": [
        {"name": "msme", "label": "MSME discount applicable?", "type": "radio", "options": YN, "demo": "No"},
        {"name": "soc_required", "label": "Statement of conformity required?", "type": "radio", "options": YN,
         "demo": "Yes"},
        {"name": "decision_rule", "label": "If yes, decision rule", "type": "radio",
         "options": [["(i)", "(i) Simple acceptance - measured value within limit"],
                     ["(ii)", "(ii) Guarded acceptance considering measurement uncertainty"],
                     ["(iii)", "(iii) As per customer specified rule"]], "demo": "(i)"},
    ]},
    {"title": "3. Proforma for transformers (manufacturer-declared data)", "fields": [
        {"name": "pf_sealing", "label": "Type - sealing", "type": "radio", "options": ["Sealed", "Non-sealed"],
         "demo": "Non-sealed"},
        {"name": "pf_insulation", "label": "Type - insulation", "type": "radio", "options": ["Oil immersed", "Dry type"],
         "demo": "Oil immersed"},
        {"name": "pf_location", "label": "Type - installation", "type": "radio", "options": ["Indoor", "Outdoor"],
         "demo": "Outdoor"},
        {"name": "pf_kva", "label": "Rated power (kVA)", "type": "number", "required": True, "demo": "250"},
        {"name": "pf_hv_v", "label": "Rated voltage HV (V)", "type": "number", "required": True, "demo": "11000"},
        {"name": "pf_lv_v", "label": "Rated voltage LV (V)", "type": "number", "required": True, "demo": "433"},
        {"name": "pf_um", "label": "Highest voltage for equipment Um (kV)", "type": "number", "demo": "12"},
        {"name": "pf_hv_li", "label": "HV insulation level LI (kVp)", "type": "text", "demo": "75"},
        {"name": "pf_hv_ac", "label": "HV insulation level AC (kVrms)", "type": "text", "demo": "28"},
        {"name": "pf_lv_li", "label": "LV insulation level LI (kVp)", "type": "text", "demo": "-"},
        {"name": "pf_lv_ac", "label": "LV insulation level AC (kVrms)", "type": "text", "demo": "3"},
        {"name": "pf_impedance", "label": "Impedance voltage at 75 °C (%)", "type": "number", "required": True,
         "demo": "4.50"},
        {"name": "pf_phases", "label": "Number of phases", "type": "select", "options": ["3", "1"], "demo": "3"},
        {"name": "pf_freq", "label": "Rated frequency (Hz)", "type": "number", "demo": "50"},
        {"name": "pf_vector_group", "label": "Vector group", "type": "text", "required": True, "demo": "Dyn11"},
        {"name": "pf_eel", "label": "Energy efficiency level", "type": "select",
         "options": ["EEL-1", "EEL-2", "EEL-3", "EEL-4", "EEL-5"], "demo": "EEL-1"},
        {"name": "pf_cooling", "label": "Type of cooling", "type": "text", "demo": "ONAN"},
        {"name": "pf_tap_plus", "label": "Tapping range + (%)", "type": "number", "demo": "5"},
        {"name": "pf_tap_minus", "label": "Tapping range - (%)", "type": "number", "demo": "10"},
        {"name": "pf_tap_step", "label": "Tap step (%)", "type": "number", "demo": "2.5"},
        {"name": "pf_num_taps", "label": "Number of taps", "type": "number", "demo": "7"},
        {"name": "pf_principal_tap", "label": "Principal tap position", "type": "number", "demo": "3"},
        {"name": "pf_nll", "label": "Declared no-load loss (W)", "type": "number", "demo": "385"},
        {"name": "pf_ll", "label": "Declared load loss at 75 °C (W)", "type": "number", "demo": "2250"},
        {"name": "pf_max_tl50", "label": "Maximum total loss at 50 % load (W)", "type": "number", "required": True,
         "demo": "980"},
        {"name": "pf_max_tl100", "label": "Maximum total loss at 100 % load (W)", "type": "number", "required": True,
         "demo": "2930"},
        {"name": "pf_noise", "label": "Noise level (dB)", "type": "text", "demo": "-"},
        {"name": "pf_oil_volume", "label": "Volume of oil (litres)", "type": "number", "demo": "460"},
        {"name": "pf_mfg", "label": "Month & year of manufacture (MM/YYYY)", "type": "text", "demo": "10/2025"},
        {"name": "pf_winding_material", "label": "Winding material", "type": "radio", "options": ["Copper", "Aluminium"],
         "demo": "Copper"},
        {"name": "pf_coil_shape", "label": "Coil shape", "type": "radio", "options": ["Circular", "Non-circular"],
         "demo": "Circular"},
        {"name": "pf_core", "label": "Core material", "type": "radio", "options": ["CRGO", "Amorphous"], "demo": "CRGO"},
        {"name": "pf_lv_winding", "label": "LV winding type", "type": "text", "demo": "Helical / Spiral"},
        {"name": "pf_hv_winding", "label": "HV winding type", "type": "text", "demo": "Crossover"},
        {"name": "pf_tank", "label": "Tank construction", "type": "radio", "options": ["Plain", "Corrugated"],
         "demo": "Plain"},
        {"name": "pf_gtr_top_oil", "label": "Guaranteed max. temperature rise - top oil (°C)", "type": "number",
         "demo": "35"},
        {"name": "pf_gtr_winding", "label": "Guaranteed max. temperature rise - winding (°C)", "type": "number",
         "demo": "40"},
    ]},
]

DECLARATION = ("We hereby declare that the information furnished above is true and correct. We agree to abide by "
               "the terms and conditions of CPRI for testing, accept the test methods and decision rule indicated "
               "above, and understand that the test results relate only to the sample(s) tested.")


def all_fields():
    for sec in SECTIONS:
        for fd in sec["fields"]:
            yield fd


def demo_form():
    """Demo request (A.P. Transformers) as the dict stored in requests.form_json."""
    return {fd["name"]: fd["demo"] for fd in all_fields() if "demo" in fd}


def parse_form(form, test_codes_all):
    """
    Turn a submitted HTML form (werkzeug MultiDict) into (data, tests, errors).
    """
    data, errors = {}, []
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
            errors.append(f"'{fd['label']}' is required.")
        if val and fd["type"] == "number":
            try:
                float(val)
            except ValueError:
                errors.append(f"'{fd['label']}' must be a number.")
        data[fd["name"]] = val
    tests = [c for c in test_codes_all if form.get(f"test_{c}")]
    if not tests:
        errors.append("Select at least one test.")
    if data.get("soc_required") == "Yes" and not data.get("decision_rule"):
        errors.append("Choose a decision rule for the statement of conformity.")
    if not form.get("declaration"):
        errors.append("You must accept the declaration before submitting.")
    return data, tests, errors


def from_excel(values, test_codes_all):
    """Map parse_request_workbook() output to {field: value} + test list for the JS auto-fill."""
    data = {k: v for k, v in values.items() if not k.startswith(("drawing", "test_"))}
    drawings = []
    for i in range(1, 6):
        n = values.get(f"drawing{i}_number")
        if n:
            drawings.append({"number": n, "sheet": values.get(f"drawing{i}_sheet", ""),
                             "rev": values.get(f"drawing{i}_rev", "")})
    tests = [c for c in test_codes_all if str(values.get(f"test_{c}", "")).strip().lower() in ("yes", "y", "1", "true", "x")]
    return {"fields": data, "drawings": drawings, "tests": tests}
