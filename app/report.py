"""
Report data and file conversion.

    build_snapshot(conn, job_id)        -> canonical dict of everything the report depends on
    render_docx(snapshot, out_path)     -> filled DOCX, using the report template recorded in the snapshot
                                           (app/reporting: versioned templates + generator)
    docx_to_pdf(docx, pdf)              -> PDF via LibreOffice, else MS Word (COM, as docx2pdf), else None
    render_work_instruction(job, path)  -> Work Instruction CPRI/QAF/02

Because the generator works only from the frozen snapshot and a frozen template version,
an issued report can always be reproduced and its hash re-verified.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime

from docx.shared import Mm

import config
from .db import get_settings


def ddmmyyyy(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso[:19]).strftime("%d-%m-%Y")
    except ValueError:
        return iso


# =============================================================================
# Snapshot
# =============================================================================
def build_snapshot(conn, job_id, template=None):
    """
    Everything the report depends on, as plain JSON-able data (deterministic order).
    `data` holds the job's own data (re-built and compared by "Verify integrity");
    `template` records the template id / version / SHA-256 used to render it.
    """
    from .reporting import templates as T
    job = conn.execute("SELECT * FROM requests WHERE id=?", (job_id,)).fetchone()
    cust = conn.execute("SELECT full_name, company, username FROM users WHERE id=?", (job["customer_id"],)).fetchone()
    readings = conn.execute("SELECT key, station, imported_value, current_value, unit, source, source_cell, source_file_id, "
                            "ai_confidence FROM readings WHERE job_id=? ORDER BY key", (job_id,)).fetchall()
    results = conn.execute("SELECT station, rule_code, label, value, limit_text, formula, passed FROM test_results "
                           "WHERE job_id=? ORDER BY station, rule_code", (job_id,)).fetchall()
    corrections = conn.execute("SELECT c.key, c.old_value, c.new_value, c.reason, c.ts, u.username FROM corrections c "
                               "LEFT JOIN users u ON u.id=c.user_id WHERE c.job_id=? ORDER BY c.id", (job_id,)).fetchall()
    engineers = conn.execute("SELECT u.full_name, u.username, je.role, je.station FROM job_engineers je "
                             "JOIN users u ON u.id=je.user_id WHERE je.job_id=? ORDER BY je.role DESC, u.full_name",
                             (job_id,)).fetchall()
    files = conn.execute("SELECT id, kind, station, original_name, sha256 FROM files WHERE job_id=? ORDER BY id",
                         (job_id,)).fetchall()
    # only settings that influence the report (limits, numbering, texts) - internal counters such as
    # registry_rev or last_backup must not affect the sealed data
    settings = {k: v for k, v in get_settings(conn).items()
                if k in config.DEFAULT_SETTINGS and not k.startswith("approval_")}
    data = {
        "job": {"id": job["id"], "ref": job["ref"], "sample_code": job["sample_code"], "report_no": job["report_no"],
                "amends_id": job["amends_id"], "amendment_no": job["amendment_no"],
                "accepted_at": job["accepted_at"], "created_at": job["created_at"],
                "customer": {"name": cust["full_name"], "company": cust["company"], "username": cust["username"]},
                "wi": json.loads(job["wi_json"] or "{}"), "return_comment": job["return_comment"]},
        "form": json.loads(job["form_json"]),
        "tests": sorted(json.loads(job["tests_json"])),
        "readings": {r["key"]: r["current_value"] for r in readings},
        "readings_meta": {r["key"]: {"imported": r["imported_value"], "cell": r["source_cell"], "source": r["source"],
                                     "file_id": r["source_file_id"], "unit": r["unit"], "ai_confidence": r["ai_confidence"]}
                          for r in readings},
        "results": [dict(r) for r in results],
        "corrections": [dict(c) for c in corrections],
        "engineers": [dict(e) for e in engineers],
        "files": [dict(f) for f in files],
        "settings": settings,
    }
    if template is None:
        template = (T.get(conn, job["template_id"]) if job["template_id"] else None) or T.active(conn)
    tinfo = None
    if template is not None:
        tinfo = {"id": template["id"], "version": template["version"], "name": template["name"],
                 "equipment_type": template["equipment_type"], "sha256": template["compiled_sha"]}
    return {"schema": 2, "data": data, "template": tinfo}


def render_docx(conn, snapshot, out_path, issue=None, verify_url=None):
    """Fill the template recorded in the snapshot (falls back to the active template)."""
    from .reporting import generator, templates as T
    rec = T.get(conn, snapshot["template"]["id"]) if snapshot.get("template") else T.active(conn)
    if rec is None:
        raise RuntimeError("No active report template - import one first (tools/import_template.py / seed.py).")
    mapping = T.mapping_of(rec)
    generator.render(T._abs(rec["compiled_path"]), mapping, snapshot, out_path, issue, verify_url)
    return out_path


# =============================================================================
# PDF conversion (detected once at start-up)
# =============================================================================
_word_lock = threading.Lock()
_converter = None


def _find_soffice():
    for cand in ("soffice", "soffice.exe", r"C:\Program Files\LibreOffice\program\soffice.exe",
                 r"C:\Program Files (x86)\LibreOffice\program\soffice.exe", "/usr/bin/soffice",
                 "/Applications/LibreOffice.app/Contents/MacOS/soffice"):
        path = shutil.which(cand) or (cand if os.path.isfile(cand) else None)
        if path:
            return path
    return None


def _word_available():
    if sys.platform != "win32":
        return False
    try:
        import winreg
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application"))
        import win32com.client  # noqa: F401
        return True
    except Exception:
        return False


def detect_converter():
    """Return ('libreoffice', path) | ('word', None) | (None, None). Cached."""
    global _converter
    if _converter is None:
        so = _find_soffice()
        if so:
            _converter = ("libreoffice", so)
        elif _word_available():
            _converter = ("word", None)
        else:
            _converter = (None, None)
    return _converter


def _word_convert(docx_path, pdf_path):
    import pythoncom
    import win32com.client
    with _word_lock:           # Word automation is not thread-safe -> one conversion at a time
        pythoncom.CoInitialize()
        word = None
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            doc = word.Documents.Open(os.path.abspath(docx_path), ReadOnly=True, AddToRecentFiles=False,
                                      ConfirmConversions=False)
            doc.ExportAsFixedFormat(os.path.abspath(pdf_path), 17)   # 17 = wdExportFormatPDF
            doc.Close(0)
        finally:
            if word is not None:
                try:
                    word.Quit()
                except Exception:
                    pass
            pythoncom.CoUninitialize()


def docx_to_pdf(docx_path, pdf_path):
    """Convert; returns pdf_path or None if no converter works."""
    kind, path = detect_converter()
    try:
        if kind == "libreoffice":
            outdir = tempfile.mkdtemp()
            subprocess.run([path, "--headless", "--convert-to", "pdf", "--outdir", outdir, docx_path],
                           check=True, timeout=120, capture_output=True)
            produced = os.path.join(outdir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
            shutil.move(produced, pdf_path)
            return pdf_path
        if kind == "word":
            try:
                _word_convert(docx_path, pdf_path)
            except Exception:
                from docx2pdf import convert   # last resort
                convert(docx_path, pdf_path)
            return pdf_path if os.path.exists(pdf_path) else None
    except Exception as exc:  # pragma: no cover - environment dependent
        print("PDF conversion failed:", exc)
    return None


# =============================================================================
# Work instruction (format CPRI/QAF/02)
# =============================================================================
def render_work_instruction(job, out_path):
    """Generate the Work Instruction as a DOCX laid out like the CPRI/QAF/02 paper form."""
    from docx import Document
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    wi = json.loads(job["wi_json"] or "{}")
    doc = Document()
    for s in doc.sections:
        s.left_margin = s.right_margin = Mm(20)
        s.top_margin = s.bottom_margin = Mm(15)
    style = doc.styles["Normal"]
    style.font.name = "Arial"
    style.font.size = Pt(10.5)

    def para(text, size=10.5, bold=False, align=None, space=4):
        p = doc.add_paragraph()
        r = p.add_run(text)
        r.bold = bold
        r.font.size = Pt(size)
        if align:
            p.alignment = align
        p.paragraph_format.space_after = Pt(space)
        return p

    para("CENTRAL POWER RESEARCH INSTITUTE", 15, True, WD_ALIGN_PARAGRAPH.CENTER, 2)
    para("Name of the Unit / Division : SHORT CIRCUIT LABORATORY", 12, False, WD_ALIGN_PARAGRAPH.CENTER, 10)
    hdr = doc.add_table(rows=3, cols=2)
    for i, (a, b) in enumerate((("Format No. : CPRI/QAF/02", "Issue No. 01"),
                                ("Revision No. 02", "Date of Issue : 15-11-2005"),
                                ("Revision Date : 28/10/2019", "Sheet 1 of 1"))):
        hdr.cell(i, 0).text = a
        hdr.cell(i, 1).text = b
        hdr.cell(i, 1).paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.RIGHT
        for c in (0, 1):
            for r in hdr.cell(i, c).paragraphs[0].runs:
                r.font.size = Pt(9)
    p = para("Work Instruction", 12, True, WD_ALIGN_PARAGRAPH.CENTER, 8)
    p.runs[0].underline = True
    rows = [
        ("Test series Number (where applicable)", wi.get("test_series_no", "")),
        ("Sample Code Number", wi.get("sample_code", "")),
        ("Name of Customer", wi.get("customer", "")),
        ("Customer Reference Number", wi.get("customer_ref", "-")),
        ("Description of Test Sample", wi.get("description", "")),
        ("Specific Instructions for mounting and Connections, if any", wi.get("mounting", "-")),
        ("Special Instructions", wi.get("special_instructions", "-")),
        ("Date of Test", wi.get("date_of_test", "")),
        ("Customer Requirement", wi.get("customer_requirement", "")),
        ("Name of Test or Tests Required", "\n".join(wi.get("tests_required", []))),
        ("Reference Standard for the Test", wi.get("reference_standard", "")),
        ("Whether Standard used is latest", wi.get("latest_standard", "Yes")),
        ("Deviation / Change from Standard", wi.get("deviation", "Nil")),
        ("Work Allotted to", wi.get("work_allotted_to") or "(to be assigned)"),
    ]
    t = doc.add_table(rows=len(rows), cols=3)
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, (k, v) in enumerate(rows):
        t.cell(i, 0).text = k
        t.cell(i, 1).text = ":"
        t.cell(i, 2).text = str(v)
        t.cell(i, 0).width, t.cell(i, 1).width, t.cell(i, 2).width = Mm(70), Mm(5), Mm(95)
    para("")
    para(f"Signature Test In-charge / HOD with date :  {wi.get('issued_by', '')}   {ddmmyyyy(wi.get('issued_at'))}",
         10, False, WD_ALIGN_PARAGRAPH.RIGHT, 14)
    para("_" * 86, 9)
    for k in ("Work Completed on", "Name & Signature of the Test Engineer who performed the work",
              "Test Engineer Responsible for Report", "Deviations Noticed during the Testing / Calibration",
              "Recording of sorting out the discrepancies"):
        para(f"{k} :", 10, False, None, 10)
    para("Generated by CPRI ATRG", 7, False, WD_ALIGN_PARAGRAPH.RIGHT)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    doc.save(out_path)
    return out_path
