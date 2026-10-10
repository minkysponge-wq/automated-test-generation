"""
End-to-end browser run with Playwright against a live server (after `python seed.py --reset`):

    python run.py                       # terminal 1
    python tests/e2e/e2e_flow.py        # terminal 2   [--base http://localhost:5000] [--headed]

    set ATRG_E2E_AI=true first to include the live Gemini review checks (screenshots check1/3/5_*.png)

 1. HOD switches on the optional "accept request" approval
 2. customer reads the scanned request PDF (check 1), reviews it beside the scan, clears it, reads the
    ATRG Excel request template (no AI), submits
 3. admin accepts -> HOD approves -> admin assigns an engineer
 4. engineer reads the scanned routine logsheet (check 3) and a foreign-layout losses workbook (check 5),
    reviews them with the source drawer, clears them, then uploads every sample_data logsheet, corrects one value with a reason, verifies, locks
 5. admin reviews (live preview) and forwards -> HOD authorizes -> report issued and sealed
 6. customer downloads the PDF, integrity verified; an edit after issue is blocked (HTTP and database)
 7. admin uploads a modified report format -> HOD activates it -> the next job's report uses the new layout
 8. screenshots of every main screen at 1440x900 and 1280x720 in e2e_output/screens/
"""
import argparse
import os
import random
import re
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(ROOT, ".pw-browsers"))

from playwright.sync_api import expect, sync_playwright  # noqa: E402

import config  # noqa: E402

OUT = os.path.join(ROOT, "e2e_output")
AI = os.environ.get("ATRG_E2E_AI", "false").lower() == "true"     # run the live Gemini checks 1, 3 and 5
STATIONS = [("losses", "losses.xlsx"), ("routine", "routine.xlsx"), ("sc", "short_circuit.xlsx"),
            ("temprise", "temperature_rise.xlsx"), ("pressure", "pressure_oil_leakage.xlsx")]
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass


def step(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def login(page, base, role, user, pw):
    page.goto(f"{base}/{role}/login")
    page.fill("input[name=username]", user)
    page.fill("input[name=password]", pw)
    page.click("button[type=submit]")
    page.wait_for_url(f"{base}/{role}/**")


def confirm(page):
    """Click Confirm in the confirmation modal."""
    page.locator("#confirm-dialog").wait_for(state="visible")
    page.click("#confirm-ok")


def combined_request_pdf():
    import pymupdf
    path = os.path.join(OUT, "scanned_request.pdf")
    doc = pymupdf.open()
    for name in ("Customer_request_form.pdf", "Proforma_for_transformers.pdf"):
        doc.insert_pdf(pymupdf.open(os.path.join(ROOT, "reference", name)))
    doc.save(path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=f"http://localhost:{config.PORT}")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    os.makedirs(os.path.join(OUT, "screens"), exist_ok=True)
    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not a.headed)
        ctx = browser.new_context(accept_downloads=True, viewport={"width": 1440, "height": 900})
        ctx.set_default_timeout(90000)
        cust, admin, eng, hod = ctx.new_page(), ctx.new_page(), ctx.new_page(), ctx.new_page()

        # ---------------------------------------------------------------- 1. HOD enables the optional approval
        login(hod, base, "hod", "hod", "hod12345")
        hod.goto(f"{base}/hod/settings")
        hod.check("input[name=approval_accept_request]")
        hod.click("form button.btn-primary")
        expect(hod.locator(".flash")).to_be_visible()
        step("HOD: request acceptance now needs HOD approval")

        # ---------------------------------------------------------------- 2. customer
        uname = f"e2ecust{random.randint(1000, 9999)}"
        cust.goto(f"{base}/customer/register")
        for k, v in {"username": uname, "full_name": "E2E Customer", "password": "secret123", "password2": "secret123",
                     "email": "e2e@example.com", "phone": "9999", "company": "M/s. A.P. Transformers",
                     "address": "Ambattur Indl. Estate, Chennai-600058"}.items():
            cust.fill(f"[name={k}]", v)
        cust.click("button[type=submit]")
        login(cust, base, "customer", uname, "secret123")
        step(f"customer {uname} registered")
        cust.goto(f"{base}/customer/requests/new")
        # check 1: scanned request form + proforma, read automatically, shown side by side for review
        cust.set_input_files("#read-file", combined_request_pdf())
        cust.click("#read-btn")
        expect(cust.locator("#read-status")).to_contain_text(re.compile("Review every value|turned off|not configured|Please fill"),
                                                             timeout=300000)
        step(f"scanned request read: {cust.locator('#read-status').inner_text()[:110]}")
        if AI:
            summary = cust.locator("#read-summary").inner_text()
            step(f"review summary: {summary}")
            assert cust.locator("[name=customer_name]").input_value().strip(), "customer name not read"
            assert cust.locator(".ai-mark").count() > 10 and cust.locator("#source-view img").count() >= 1
            cust.locator("[name=customer_name]").focus()
            cust.wait_for_timeout(600)
            cust.screenshot(path=os.path.join(OUT, "screens", "check1_request_form_review.png"), full_page=False)
            cust.click("#clear-btn")
            assert cust.locator(".ai-mark").count() == 0 and not cust.locator("[name=customer_name]").input_value()
            step("auto-filled values cleared")
        # the ATRG Excel request template goes through the same reader without AI
        cust.set_input_files("#read-file", os.path.join(config.SAMPLE_DATA_DIR, "customer_request_filled.xlsx"))
        cust.click("#read-btn")
        expect(cust.locator("#read-status")).to_contain_text("Excel request template", timeout=60000)
        cust.uncheck("#select-all")
        cust.check("#select-all")
        assert cust.locator(".js-test:checked").count() == 13
        cust.check("[name=declaration]")
        cust.screenshot(path=os.path.join(OUT, "screens", "customer_new_request_filled.png"), full_page=True)
        cust.click("#submit-request")
        expect(cust.locator(".flash-success")).to_contain_text("Reference")
        job_id = int(cust.url.rstrip("/").split("/")[-1])
        step(f"request submitted, job #{job_id}")

        # ---------------------------------------------------------------- 3. admin accepts -> HOD approves -> admin assigns
        login(admin, base, "admin", "admin", "admin12345")
        admin.goto(f"{base}/admin/requests/{job_id}")
        admin.click("#accept-btn")
        expect(admin.locator(".flash-info")).to_contain_text("HOD")
        hod.goto(f"{base}/hod/approvals")
        hod.click("text=Accept request")
        hod.click("#approve-btn")
        expect(hod.locator(".flash-success")).to_be_visible()
        step("HOD approved the acceptance")
        admin.goto(f"{base}/admin/requests/{job_id}")
        admin.select_option("select[name=responsible]", label="P. Naveenkumar (engineer1)")
        admin.click("#assign-btn")
        expect(admin.locator(".flash-success")).to_contain_text("assigned")
        step("engineer assigned")

        # ---------------------------------------------------------------- 4. engineer
        login(eng, base, "engineer", "engineer1", "eng12345")
        if AI:
            # check 3: scanned routine logsheet; check 5: losses workbook in a layout ATRG has never seen
            for st, path, shot in [("routine", os.path.join(ROOT, "reference", "Logsheet_for_routine_test_no_load_current_at_112.5_%.pdf"),
                                    "check3_routine_logsheet_review"),
                                   ("losses", os.path.join(config.SAMPLE_DATA_DIR, "foreign_layout_losses.xlsx"),
                                    "check5_foreign_excel_review")]:
                eng.goto(f"{base}/engineer/jobs/{job_id}?station={st}")
                box = eng.locator(f'.reader[data-station="{st}"]')
                box.locator(".read-file").set_input_files(path)
                with eng.expect_navigation(timeout=400000):
                    box.locator(".read-btn").click()
                n_ai = eng.locator("text=AI-extracted, needs verification").count()
                step(f"{st}: {n_ai} values read automatically, awaiting verification")
                assert n_ai > 20
                eng.locator(".js-goto").first.click()
                expect(eng.locator("#source-drawer")).to_be_visible()
                eng.wait_for_timeout(1200)
                eng.screenshot(path=os.path.join(OUT, "screens", f"{shot}.png"), full_page=False)
                eng.click("#drawer-close")
                eng.click("text=Clear auto-filled values")
                confirm(eng)
                expect(eng.locator(".flash-success, .flash-info")).to_be_visible()
            step("automatic readings reviewed and cleared; continuing with the Excel logsheets")
        for st, fname in STATIONS:
            eng.goto(f"{base}/engineer/jobs/{job_id}?station={st}")
            eng.set_input_files(".upload-form input[name=file]", os.path.join(config.SAMPLE_DATA_DIR, fname))
            eng.click(".upload-form button")
            expect(eng.locator(".flash-success, .flash-warning")).to_contain_text("Imported")
        step("all five logsheets imported")
        eng.goto(f"{base}/engineer/jobs/{job_id}?station=losses")
        eng.click('tr[data-key="wr.oil_top.at"] .js-edit')
        eng.fill("#edit-value", "27.1")
        eng.fill("#edit-reason", "Transcription error - re-read from the thermometer log")
        with eng.expect_navigation():
            eng.click("#edit-save")
        expect(eng.locator('tr[data-key="wr.oil_top.at"] td.val').first).to_have_text("27.1")
        step("corrected top-oil temperature 27.0 -> 27.1 with a reason")
        for st, _ in STATIONS:
            eng.goto(f"{base}/engineer/jobs/{job_id}?station={st}")
            eng.click(f"form[action$='/verify/{st}'] button")
            expect(eng.locator(".flash-success")).to_contain_text("verified")
        eng.click("#lock-job")
        confirm(eng)
        expect(eng.locator(".flash-success")).to_contain_text("locked")
        step("all tests verified, job locked")

        # ---------------------------------------------------------------- 5. admin review -> forward -> HOD authorizes
        admin.goto(f"{base}/admin/requests/{job_id}")
        expect(admin.locator(".page-title")).to_be_visible()
        admin.click("#load-preview")
        admin.wait_for_function("document.querySelector('#load-preview').textContent.includes('Refresh')", timeout=180000)
        step("admin: live preview rendered")
        admin.click("#forward-btn")
        confirm(admin)
        expect(admin.locator(".flash-success")).to_contain_text("Forwarded")
        report_no = admin.locator(".page-title").inner_text().split()[0]
        hod.goto(f"{base}/hod/approvals")
        hod.click(f"text=Authorize and issue test report {report_no}")
        hod.screenshot(path=os.path.join(OUT, "screens", "hod_authorization.png"), full_page=True)
        hod.click("#approve-btn")
        confirm(hod)
        expect(hod.locator(".flash-success")).to_contain_text("issued", timeout=240000)
        step(f"HOD authorized {report_no}: issued and sealed")

        # ---------------------------------------------------------------- 6. customer download + verify; edit blocked
        cust.goto(f"{base}/customer/requests/{job_id}")
        with cust.expect_download() as dl:
            cust.click("#dl-pdf")
        pdf_path = os.path.join(OUT, f"{report_no}.pdf")
        dl.value.save_as(pdf_path)
        cust.click(".js-verify")
        expect(cust.locator(".verify-result .callout")).to_contain_text("Verified")
        step(f"customer downloaded the PDF ({os.path.getsize(pdf_path)} bytes); integrity verified")
        token = eng.locator("meta[name=csrf-token]").get_attribute("content")
        r = eng.request.post(f"{base}/engineer/jobs/{job_id}/readings/1", data='{"value":"1","reason":"x"}',
                             headers={"Content-Type": "application/json", "X-Requested-With": "fetch", "X-CSRF-Token": token})
        assert r.status in (400, 404), r.status
        conn = sqlite3.connect(config.DB_PATH)
        try:
            conn.execute("UPDATE readings SET current_value='0' WHERE job_id=?", (job_id,))
            raise AssertionError("the database accepted an update after issue")
        except sqlite3.IntegrityError as e:
            step(f"edit after issue blocked by the database: {e}")
        finally:
            conn.close()

        # ---------------------------------------------------------------- 7. new report format
        from test_template_adaptability import make_modified
        modified = os.path.join(OUT, "Transformer_Test_report_format_revised.docx")
        make_modified(os.path.join(ROOT, "report_templates", "v1", "source.docx"), modified)
        admin.goto(f"{base}/admin/templates")
        admin.set_input_files("input[name=file]", modified)
        admin.fill("input[name=name]", "CPRI Transformer Test Report, revision 2")
        admin.click("#template-upload-btn")
        expect(admin.locator(".flash-success")).to_contain_text("mapped automatically")
        step("admin: " + admin.locator(".flash-success").inner_text()[:120])
        tid = int(admin.url.rstrip("/").split("/")[-1])
        # map any remaining required places (a reviewer would pick fields in the editor)
        while admin.locator(".slot.unmapped").count():
            admin.locator(".slot.unmapped").first.click()
            admin.check("input[name=slot_kind][value=ignore]")
            with admin.expect_navigation():
                admin.click("#slot-form button[type=submit]")
        admin.screenshot(path=os.path.join(OUT, "screens", "admin_template_editor.png"), full_page=False)
        admin.click("form[action$='/compile'] button")
        expect(admin.locator(".callout.good")).to_contain_text("no unfilled")
        admin.click("#template-submit")
        confirm(admin)
        expect(admin.locator(".flash-success")).to_contain_text("activation")
        hod.goto(f"{base}/hod/approvals")
        hod.click("text=Activate report template")
        hod.click("#approve-btn")
        expect(hod.locator(".flash-success")).to_be_visible()
        step("HOD activated template revision 2")
        # the seeded job in technical review is now forwarded and issued with the new layout
        conn = sqlite3.connect(config.DB_PATH)
        job2 = conn.execute("SELECT id, report_no FROM requests WHERE report_no='CPRIBLRSCL25T1655'").fetchone()
        conn.close()
        admin.goto(f"{base}/admin/requests/{job2[0]}")
        admin.click("#forward-btn")
        confirm(admin)
        hod.goto(f"{base}/hod/approvals")
        hod.click(f"text=Authorize and issue test report {job2[1]}")
        hod.click("#approve-btn")
        confirm(hod)
        expect(hod.locator(".flash-success")).to_contain_text("issued", timeout=240000)
        conn = sqlite3.connect(config.DB_PATH)
        rep2 = conn.execute("SELECT docx_path, template_id, template_version FROM reports WHERE job_id=?", (job2[0],)).fetchone()
        conn.close()
        from docx import Document
        text = " ".join(c.text for t in Document(rep2[0]).tables for r in t.rows for c in r.cells)
        assert rep2[1] == tid and "Load losses at 75" in text, (rep2, tid)
        step(f"{job2[1]} issued with template v{rep2[2]}: new layout confirmed")

        # ---------------------------------------------------------------- 8. screenshots
        conn = sqlite3.connect(config.DB_PATH)
        ap_id = conn.execute("SELECT MAX(id) FROM approval_requests").fetchone()[0]
        conn.close()
        screens = [
            (cust, "customer_requests", f"{base}/customer/"),
            (cust, "customer_request_detail", f"{base}/customer/requests/{job_id}"),
            (cust, "customer_new_request", f"{base}/customer/requests/new"),
            (eng, "engineer_jobs", f"{base}/engineer/"),
            (eng, "engineer_job", f"{base}/engineer/jobs/{job_id}?station=losses"),
            (admin, "admin_requests", f"{base}/admin/"),
            (admin, "admin_request_detail", f"{base}/admin/requests/{job_id}"),
            (admin, "admin_templates", f"{base}/admin/templates"),
            (admin, "admin_template_editor", f"{base}/admin/templates/{tid}"),
            (admin, "admin_registry", f"{base}/admin/registry"),
            (admin, "admin_engineers", f"{base}/admin/engineers"),
            (hod, "hod_dashboard", f"{base}/hod/"),
            (hod, "hod_approvals", f"{base}/hod/approvals?show=all"),
            (hod, "hod_approval_detail", f"{base}/hod/approvals/{ap_id}"),
            (hod, "hod_audit", f"{base}/hod/audit"),
            (hod, "hod_system", f"{base}/hod/system"),
            (cust, "landing", f"{base}/"),
            (cust, "verify", f"{base}/verify/{report_no}"),
        ]
        for w, h in ((1440, 900), (1280, 720)):
            for page, name, url in screens:
                page.set_viewport_size({"width": w, "height": h})
                page.goto(url)
                page.screenshot(path=os.path.join(OUT, "screens", f"{name}_{w}.png"))
        step(f"{len(screens) * 2} screenshots written to e2e_output/screens")
        browser.close()

    import pymupdf
    doc = pymupdf.open(pdf_path)
    for i, pg in enumerate(doc):
        pg.get_pixmap(dpi=80).save(os.path.join(OUT, f"page_{i + 1:02d}.png"))
    step(f"rendered {doc.page_count} PDF pages to e2e_output")
    step(f"END-TO-END OK in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
