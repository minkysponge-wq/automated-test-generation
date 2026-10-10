"""
Central configuration for the ATRG prototype.

Values can be overridden with environment variables of the same name
(e.g. `set ATRG_PORT=8080` before `python run.py`).
"""
import os
import secrets

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _env(name, default):
    return os.environ.get(name, default)


# --- server ---------------------------------------------------------------
HOST = _env("ATRG_HOST", "0.0.0.0")
PORT = int(_env("ATRG_PORT", "5000"))
THREADS = int(_env("ATRG_THREADS", "8"))

# --- paths ----------------------------------------------------------------
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = _env("ATRG_DB", os.path.join(INSTANCE_DIR, "atrg.db"))
STORAGE_DIR = _env("ATRG_STORAGE", os.path.join(BASE_DIR, "storage"))
UPLOAD_DIR = os.path.join(STORAGE_DIR, "uploads")      # customer documents + engineer Excel files
REPORT_DIR = os.path.join(STORAGE_DIR, "reports")      # work instructions, other generated docs
SEALED_DIR = os.path.join(STORAGE_DIR, "sealed")       # issued reports (read-only)
PREVIEW_DIR = os.path.join(STORAGE_DIR, "preview")     # admin live previews (throw-away)
FIELDS_YAML = os.path.join(BASE_DIR, "mappings", "fields.yaml")
REPORT_TEMPLATE = os.path.join(BASE_DIR, "templates", "report_template.docx")
EXCEL_TEMPLATE_DIR = os.path.join(BASE_DIR, "templates", "excel")
SAMPLE_DATA_DIR = os.path.join(BASE_DIR, "sample_data")

# --- security -------------------------------------------------------------
# Admin self-registration needs this code so random LAN users cannot become admin.
ADMIN_INVITE_CODE = _env("ATRG_ADMIN_INVITE_CODE", "CPRI-ADMIN-2026")
MAX_UPLOAD_MB = 20
ALLOWED_DOC_EXT = {".pdf", ".jpg", ".jpeg", ".png"}
ALLOWED_EXCEL_EXT = {".xlsx"}


def secret_key():
    """Persist a random secret key in instance/ so sessions survive restarts."""
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    path = os.path.join(INSTANCE_DIR, "secret_key")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write(secrets.token_hex(32))
    with open(path) as fh:
        return fh.read().strip()


# --- defaults for the admin "Config" page (stored in the config table) -----
DEFAULT_SETTINGS = {
    "lab_code": "CPRIBLRSCL",                  # report number prefix
    "sample_prefix": "HVD",                    # sample code prefix
    "ulr_prefix": "ULR-TC5452",                # NABL ULR prefix (footer)
    "standard_text": "IS 1180 (Part 1): 2014 (with Amendment No.4: March 2021)",
    "sc_standard_text": "IS: 2026 (Part-5):2011",
    "circuit_diagram_no": "CRTL/SC/TR-07B",
    # rule limits (see app/rules.py)
    "nlc_limit_100_pct": 2.0,                  # no-load current at 100 % V, % of rated
    "nlc_limit_112_pct": 5.0,                  # no-load current at 112.5 % V, % of rated
    "impedance_tolerance_pct": 10.0,           # +/- around declared impedance
    "reactance_change_limit_pct": 2.0,         # |% change in X| after SC test
    "ratio_tolerance_pct": 0.5,                # voltage ratio deviation
    "std_top_oil_rise": 35.0,                  # IS 1180 limit, deg C
    "std_winding_rise": 40.0,                  # IS 1180 limit, deg C
    "sc_peak_tolerance_pct": 5.0,              # applied peak vs required peak
    "sc_rms_tolerance_pct": 10.0,              # thermal shot rms vs required rms
    "sc_thermal_min_duration_s": 2.0,
    "pressure_perm_defl_max_mm": 5.0,          # flat plate <= 750 mm
    "loss_crosscheck_tolerance_pct": 0.5,      # typed totals vs recomputed
}
