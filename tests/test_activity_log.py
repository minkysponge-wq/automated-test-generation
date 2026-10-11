"""Activity log: Test In-charge and HOD only, filters, CSV export, chain verification."""
from conftest import login


def test_admin_and_hod_can_open_it(client):
    for role, u, pw in (("admin", "admin", "admin12345"), ("hod", "hod", "hod12345")):
        login(client, role, u, pw)
        r = client.get(f"/{role}/audit")
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        assert "Verify log integrity" in html and "Export CSV" in html
        assert client.get(f"/{role}/audit?all=1&q=HVD25S0847").status_code == 200
        assert client.get(f"/{role}/activity").status_code == 302


def test_customer_and_engineer_are_kept_out(client):
    for role, u, pw in (("customer", "customer1", "cust12345"), ("engineer", "engineer1", "eng12345")):
        c = client.application.test_client()
        login(c, role, u, pw)
        for url in ("/admin/audit", "/hod/audit", "/admin/audit.csv", "/hod/audit.csv"):
            assert c.get(url).status_code in (302, 403), (role, url)


def test_csv_export_and_verify(client):
    login(client, "admin", "admin", "admin12345")
    r = client.get("/admin/audit.csv?all=1")
    assert r.status_code == 200 and r.mimetype == "text/csv"
    res = client.post("/admin/audit/verify").get_json()
    assert res["ok"] and res["message"].startswith("Log verified:") and "chain intact" in res["message"]


def test_filters_and_reference(client, seeded):
    login(client, "admin", "admin", "admin12345")
    html = client.get("/admin/audit?all=1&type=job.issue").get_data(as_text=True)
    assert "Report issued" in html and "CPRIBLRSCL25T1654" in html
