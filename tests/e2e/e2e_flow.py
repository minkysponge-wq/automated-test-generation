"""
End-to-end browser check with Playwright (run against a live server):

    python run.py                       # terminal 1 (after python seed.py)
    python tests/e2e/e2e_flow.py        # terminal 2   [--base http://localhost:5000] [--headed]

register -> submit request (all tests, auto-filled from Excel) -> admin accept & assign ->
engineer uploads all sample_data logsheets, corrects one value with a reason, verifies, locks ->
admin reviews (live preview) and issues -> customer downloads PDF -> integrity ✅ ->
edit after issue is blocked (HTTP and database). The issued PDF pages are rendered to PNG
in e2e_output/ for visual inspection.
"""
import argparse
import os
import random
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", os.path.join(ROOT, ".pw-browsers"))

from playwright.sync_api import expect, sync_playwright  # noqa: E402

import config  # noqa: E402

OUT = os.path.join(ROOT, "e2e_output")
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except AttributeError:
    pass
STATIONS = [("losses", "losses.xlsx"), ("routine", "routine.xlsx"), ("sc", "short_circuit.xlsx"),
            ("temprise", "temperature_rise.xlsx"), ("pressure", "pressure_oil_leakage.xlsx")]


def step(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def login(page, base, role, user, pw):
    page.goto(f"{base}/{role}/login")
    page.fill("input[name=username]", user)
    page.fill("input[name=password]", pw)
    page.click("button[type=submit]")
    expect(page).to_have_url(f"{base}/{role}/")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=f"http://localhost:{config.PORT}")
    ap.add_argument("--headed", action="store_true")
    a = ap.parse_args()
    base = a.base.rstrip("/")
    os.makedirs(OUT, exist_ok=True)
    t0 = time.time()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not a.headed)
        ctx = browser.new_context(accept_downloads=True, viewport={"width": 1366, "height": 900})
        ctx.set_default_timeout(60000)
        cust, admin, eng = ctx.new_page(), ctx.new_page(), ctx.new_page()   # three portals, one browser
        for pg in (cust, admin, eng):
            pg.on("dialog", lambda d: d.accept())

        # ---------------------------------------------------------------- customer registers & submits
        uname = f"e2ecust{random.randint(1000, 9999)}"
        step(f"customer registers as {uname}")
        cust.goto(f"{base}/customer/register")
        for k, v in {"username": uname, "full_name": "E2E Customer", "password": "secret12", "password2": "secret12",
                     "email": "e2e@example.com", "phone": "9999", "company": "M/s. A.P. Transformers",
                     "address": "Ambattur Indl. Estate, Chennai-600058"}.items():
            cust.fill(f"[name={k}]", v)
        cust.click("button[type=submit]")
        login(cust, base, "customer", uname, "secret12")
        step("new test request - auto-fill from Excel, select all tests")
        cust.goto(f"{base}/customer/requests/new")
        cust.set_input_files("#request-excel", os.path.join(config.SAMPLE_DATA_DIR, "customer_request_filled.xlsx"))
        expect(cust.locator("#excel-status")).to_contain_text("Filled")
        cust.uncheck("#select-all")
        cust.check("#select-all")
        assert cust.locator(".js-test:checked").count() == 13
        cust.check("[name=declaration]")
        cust.click("#submit-request")
        expect(cust.locator(".flash-success")).to_contain_text("submitted")
        job_id = int(cust.url.rstrip("/").split("/")[-1])
        step(f"request #{job_id} submitted")

        # ---------------------------------------------------------------- admin accepts & assigns
        login(admin, base, "admin", "admin", "admin123")
        admin.goto(f"{base}/admin/requests/{job_id}")
        admin.click("#accept-btn")
        expect(admin.locator(".flash-success")).to_contain_text("Sample code")
        admin.select_option("select[name=responsible]", label="P. Naveenkumar (engineer1)")
        admin.click("#assign-btn")
        expect(admin.locator(".flash-success")).to_contain_text("assigned")
        step("accepted and assigned to engineer1")

        # ---------------------------------------------------------------- engineer uploads, corrects, verifies, locks
        login(eng, base, "engineer", "engineer1", "eng123")
        for st, fname in STATIONS:
            eng.goto(f"{base}/engineer/jobs/{job_id}?station={st}")
            eng.set_input_files(".upload-form input[name=file]", os.path.join(config.SAMPLE_DATA_DIR, fname))
            eng.click(".upload-form button")
            expect(eng.locator(".flash-success")).to_contain_text("Imported")
            step(f"uploaded {fname}")
        eng.goto(f"{base}/engineer/jobs/{job_id}?station=losses")
        eng.click('tr[data-key="wr.oil_top.at"] .js-edit')
        eng.fill("#edit-value", "27.1")
        eng.fill("#edit-reason", "Transcription error - re-read from thermometer log")
        with eng.expect_navigation():
            eng.click("#edit-save")
        expect(eng.locator('tr[data-key="wr.oil_top.at"] td.val')).to_have_text("27.1")
        step("corrected wr.oil_top.at 27.0 -> 27.1 with reason")
        for st, _ in STATIONS:
            eng.goto(f"{base}/engineer/jobs/{job_id}?station={st}")
            eng.click("form[action$='/verify/%s'] button" % st)
            expect(eng.locator(".flash-success")).to_contain_text("verified")
        eng.click("#lock-job")
        expect(eng.locator(".flash-success")).to_contain_text("locked")
        step("all stations verified, job locked")

        # ---------------------------------------------------------------- admin reviews and issues
        admin.goto(f"{base}/admin/requests/{job_id}")
        expect(admin.locator("h1 .badge")).to_contain_text("Under admin review")
        expect(admin.locator(".review")).to_contain_text("PASS")
        admin.click("#load-preview")
        admin.wait_for_function("document.querySelector('#load-preview').textContent.includes('Refresh')", timeout=180000)
        step("live preview rendered")
        admin.click("#issue-btn")
        expect(admin.locator(".flash-success")).to_contain_text("issued and sealed", timeout=180000)
        report_no = admin.locator(".report-card h2").inner_text().replace("Issued report ", "").strip()
        step(f"issued {report_no}")

        # ---------------------------------------------------------------- customer downloads + verifies
        cust.goto(f"{base}/customer/requests/{job_id}")
        with cust.expect_download() as dl:
            cust.click("#dl-pdf")
        pdf_path = os.path.join(OUT, f"{report_no}.pdf")
        dl.value.save_as(pdf_path)
        cust.click(".js-verify")
        expect(cust.locator(".verify-result .verdict")).to_contain_text("intact")
        step(f"customer downloaded PDF ({os.path.getsize(pdf_path)} bytes); integrity ✅")

        # ---------------------------------------------------------------- edits after issue are blocked
        r = eng.request.post(f"{base}/engineer/jobs/{job_id}/readings/1",
                             data='{"value":"1","reason":"x"}',
                             headers={"Content-Type": "application/json", "X-Requested-With": "fetch",
                                      "X-CSRF-Token": eng.locator("meta[name=csrf-token]").get_attribute("content")})
        assert r.status in (400, 404), r.status
        conn = sqlite3.connect(config.DB_PATH)
        try:
            conn.execute("UPDATE readings SET current_value='0' WHERE job_id=?", (job_id,))
            raise AssertionError("database accepted an update after issue!")
        except sqlite3.IntegrityError as e:
            step(f"edit after issue blocked by DB trigger: {e}")
        finally:
            conn.close()
        cust.screenshot(path=os.path.join(OUT, "customer_issued.png"), full_page=True)
        admin.screenshot(path=os.path.join(OUT, "admin_issued.png"), full_page=True)
        browser.close()

    # ---------------------------------------------------------------- render PDF pages for inspection
    try:
        import pymupdf
        doc = pymupdf.open(pdf_path)
        for i, pg in enumerate(doc):
            pg.get_pixmap(dpi=80).save(os.path.join(OUT, f"page_{i + 1:02d}.png"))
        step(f"rendered {doc.page_count} PDF pages to {OUT}")
    except ImportError:
        pass
    step(f"END-TO-END OK in {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
