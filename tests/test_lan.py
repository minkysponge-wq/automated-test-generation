"""LAN access: address discovery and no hardcoded hosts in the UI (the real server is not started)."""
import os

import config
from app.netinfo import get_lan_addresses

UI_DIRS = [os.path.join(config.BASE_DIR, "app", "templates"), os.path.join(config.BASE_DIR, "app", "static"),
           os.path.join(config.BASE_DIR, "templates")]


def test_lan_addresses_have_no_loopback():
    ips = get_lan_addresses()
    assert isinstance(ips, list)
    for ip in ips:
        assert not ip.startswith("127.") and not ip.startswith("169.254.")
        assert len(ip.split(".")) == 4


def test_no_hardcoded_hosts_in_templates_or_static():
    offenders = []
    for root_dir in UI_DIRS:
        for root, _dirs, files in os.walk(root_dir):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as fh:
                    data = fh.read()
                if b"localhost" in data or b"127.0.0.1" in data:
                    offenders.append(path)
    assert not offenders, offenders


def test_session_cookie_works_over_plain_http(app):
    assert app.config["SESSION_COOKIE_SECURE"] is False
    assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert app.config.get("SERVER_NAME") is None


def test_login_works_when_host_is_an_ip(app):
    c = app.test_client()
    r = c.post("/admin/login", data={"username": "admin", "password": "admin12345"},
               base_url="http://192.168.1.23:5000")
    assert r.status_code == 302
    html = c.get("/admin/", base_url="http://192.168.1.23:5000").get_data(as_text=True)
    assert "Network address for other computers" in html or not get_lan_addresses()
