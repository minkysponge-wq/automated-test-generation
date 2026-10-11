"""Landing page and the signed-in navigation."""
from conftest import login


def test_landing_has_four_portals_and_no_process_text(client):
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "The customer submits" not in html
    assert "calculations" not in html
    for role in ("customer", "engineer", "admin", "hod"):
        assert f'href="/{role}/login"' in html
    for role in ("customer", "engineer", "admin"):
        assert f'href="/{role}/register"' in html
    assert 'href="/hod/register"' not in html
    hod_card = html[html.index('id="portal-hod"'):]
    assert "Register" not in hod_card


def test_signed_in_card_says_continue(client):
    login(client, "customer", "customer1", "cust12345")
    html = client.get("/").get_data(as_text=True)
    card = html[html.index('id="portal-customer"'):html.index('id="portal-engineer"')]
    assert "Continue as" in card and 'href="/customer/"' in card


def test_every_sidebar_page_renders(client):
    for role, user, pw in (("customer", "customer1", "cust12345"), ("engineer", "engineer1", "eng12345"),
                           ("admin", "admin", "admin12345"), ("hod", "hod", "hod12345")):
        login(client, role, user, pw)
        home = client.get(f"/{role}/").get_data(as_text=True)
        assert "Sign out" in home and "CPRI ATRG" in home
        import re
        links = re.findall(r'<ul class="nav">(.*?)</ul>', home, re.S)[0]
        hrefs = re.findall(r'href="([^"]+)"', links)
        assert 1 <= len(hrefs) <= 6
        for h in hrefs:
            r = client.get(h.replace("&amp;", "&"))
            assert r.status_code == 200, (role, h)
            assert 'class="active"' in r.get_data(as_text=True), (role, h)
        assert client.get(f"/{role}/notifications").status_code == 200
    for url in ("/admin/engineers", "/admin/templates", "/admin/registry", "/admin/config",
                "/hod/settings", "/hod/accounts", "/hod/system", "/hod/approvals?show=all"):
        html = client.get(url).get_data(as_text=True)
        assert 'class="active"' in html, url


def test_detail_pages_have_breadcrumbs(client, seeded, conn):
    job = seeded["review_job_id"]
    ap = conn.execute("SELECT id FROM approval_requests ORDER BY id LIMIT 1").fetchone()
    pages = [("customer", "customer1", "cust12345", f"/customer/requests/{job}"),
             ("engineer", "engineer1", "eng12345", f"/engineer/jobs/{job}"),
             ("admin", "admin", "admin12345", f"/admin/requests/{job}")]
    if ap:
        pages.append(("hod", "hod", "hod12345", f"/hod/approvals/{ap['id']}"))
    for role, u, pw, url in pages:
        login(client, role, u, pw)
        r = client.get(url)
        assert r.status_code == 200, url
        assert 'class="crumbs"' in r.get_data(as_text=True), url


def test_sign_out_goes_to_landing(client):
    login(client, "admin", "admin", "admin12345")
    r = client.post("/admin/logout")
    assert r.status_code == 302 and r.headers["Location"].endswith("/")
