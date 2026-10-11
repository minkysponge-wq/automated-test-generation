"""
Login / register / logout views shared by the four portals, plus small helpers.

Each portal registers its own copy under its own URL prefix (/customer/login,
/engineer/login, /admin/login, /hod/login) with its own session key.

Account approval chain
    customer  active immediately
    engineer  pending until an admin approves
    admin     pending until the HOD approves (approval type `admin_account`)
    hod       no public registration (manage.py create-hod, or an HOD creates another HOD)
"""
import os
import re
import sqlite3

from flask import flash, g, redirect, render_template, request, url_for

import config
from .. import approvals, audit, auth, notify
from ..db import get_db, tx

ROLE_META = {
    "customer": {"title": "Customer", "long": "Customer portal",
                 "blurb": "Submit test requests, track progress and download reports."},
    "engineer": {"title": "Test Engineer", "long": "Test engineer portal",
                 "blurb": "Enter readings, verify results and lock jobs for review."},
    "admin": {"title": "Test In-charge", "long": "Test In-charge portal",
              "blurb": "Accept requests, assign engineers and review results."},
    "hod": {"title": "Head of Division", "long": "HOD portal",
            "blurb": "Authorize reports and approve changes."},
}


# Sidebar per role (max 6 items): key, label, endpoint, query args, icon.
NAV = {
    "customer": [("home", "Home", "customer.dashboard", {}, "home"),
                 ("new", "New request", "customer.new_request", {}, "file-plus"),
                 ("requests", "My requests", "customer.dashboard", {"view": "requests"}, "list"),
                 ("reports", "Reports", "customer.dashboard", {"view": "reports"}, "layout")],
    "engineer": [("home", "Home", "engineer.dashboard", {}, "home"),
                 ("jobs", "My jobs", "engineer.dashboard", {"view": "jobs"}, "list")],
    "admin": [("home", "Home", "admin.dashboard", {}, "home"),
              ("requests", "Requests", "admin.dashboard", {"status": "SUBMITTED"}, "inbox"),
              ("jobs", "Jobs", "admin.dashboard", {"view": "jobs"}, "list"),
              ("activity", "Activity log", "admin.audit_log", {}, "activity"),
              ("config", "Configuration", "admin.settings", {}, "settings")],
    "hod": [("home", "Home", "hod.dashboard", {}, "home"),
            ("approvals", "Approvals", "hod.approval_list", {}, "inbox"),
            ("reports", "Reports", "hod.approval_list", {"show": "reports"}, "layout"),
            ("activity", "Activity log", "hod.audit_log", {}, "activity"),
            ("system", "System", "hod.system", {}, "server")],
}

# Pages that are not in the sidebar: which sidebar item they belong to, and the tabs shown on them.
NAV_SECTION = {
    "admin.engineers": "config", "admin.templates": "config", "admin.template_detail": "config",
    "admin.registry": "config", "admin.registry_edit": "config", "admin.request_detail": "jobs",
    "customer.request_detail": "requests", "engineer.job": "jobs",
    "hod.settings": "system", "hod.accounts": "system", "hod.approval_detail": "approvals",
}
SUBNAV = {
    ("admin", "config"): [("admin.settings", "Configuration"), ("admin.engineers", "Engineer accounts"),
                          ("admin.templates", "Report templates"), ("admin.registry", "Field registry")],
    ("hod", "system"): [("hod.system", "System"), ("hod.settings", "Settings"), ("hod.accounts", "Accounts")],
}


def nav_state(role, endpoint, args, override=None):
    """(sidebar items with an `active` flag, tabs for the current section) for the signed-in layout."""
    items = NAV.get(role, [])
    active = override
    if active is None:
        best = -1
        for key, _, ep, q, _ in items:
            if ep == endpoint and all(args.get(k) == v for k, v in q.items()) and len(q) > best:
                active, best = key, len(q)
    if active is None:
        active = NAV_SECTION.get(endpoint)
    nav = [{"key": k, "label": label, "endpoint": ep, "args": q, "icon": ic, "active": k == active}
           for k, label, ep, q, ic in items]
    tabs = [{"endpoint": ep, "label": label, "active": ep == endpoint}
            for ep, label in SUBNAV.get((role, active), [])]
    return nav, tabs


def register_auth_views(bp, role):
    meta = ROLE_META[role]

    @bp.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            conn = get_db()
            username = (request.form.get("username") or "").strip()
            pw = request.form.get("password") or ""
            user = conn.execute("SELECT * FROM users WHERE username=? AND role=?", (username, role)).fetchone()
            if user is None or not auth.check_password(user["password_hash"], pw):
                audit.log(conn, "login.fail", "user", username, None, {"role": role},
                          user={"id": user["id"] if user else None, "username": username, "role": role})
                flash("Invalid username or password.", "danger")
            elif user["status"] == "pending":
                who = "the HOD" if role == "admin" else "the Test In-charge"
                flash(f"Your account is waiting for approval by {who}.", "warning")
            elif user["status"] != "active":
                flash("This account is disabled.", "danger")
            else:
                auth.login(role, user)
                audit.log(conn, "login.success", "user", user["id"], None, {"role": role}, user=dict(user))
                nxt = request.args.get("next")
                if nxt and nxt.startswith(f"/{role}/"):
                    return redirect(nxt)
                return redirect(url_for(f"{role}.dashboard"))
        return render_template("auth_login.html", role=role, meta=meta)

    if role != "hod":
        @bp.route("/register", methods=["GET", "POST"])
        def register():
            form = request.form
            errors = {}
            if request.method == "POST":
                errors = validate_registration(form, role)
                if not errors:
                    conn = get_db()
                    status = "active" if role == "customer" else "pending"
                    try:
                        with tx(conn):
                            uid = auth.register_user(conn, role, form, status)
                            new_user = {"id": uid, "username": form["username"].strip(), "role": role,
                                        "full_name": form["full_name"].strip()}
                            audit.log(conn, "user.register", "user", uid, None,
                                      {"username": new_user["username"], "role": role, "status": status}, user=new_user)
                            if role == "admin":
                                approvals.submit(conn, "admin_account", {"user_id": uid}, new_user, entity_id=uid,
                                                 summary=f"New Test In-charge account: {new_user['full_name']} "
                                                         f"({new_user['username']})")
                            elif role == "engineer":
                                notify.send(conn, f"Engineer account waiting for approval: {new_user['full_name']}",
                                            "/admin/engineers", role="admin")
                    except sqlite3.IntegrityError:
                        errors["username"] = "This username is already taken."
                    else:
                        if role == "admin":
                            flash("Registration received. The HOD must approve your account before you can sign in.", "info")
                        elif role == "engineer":
                            flash("Registration received. The Test In-charge must approve your account before you can "
                                  "sign in.", "info")
                        else:
                            flash("Registration complete. Please sign in.", "success")
                        return redirect(url_for(f"{role}.login"))
            return render_template("auth_register.html", role=role, meta=meta, form=form, errors=errors)

    @bp.route("/logout", methods=["POST", "GET"])
    def logout():
        auth.logout(role)
        flash("Signed out.", "info")
        return redirect(url_for("public.landing"))

    @bp.route("/notifications")
    @auth.role_required(role)
    def notifications():
        conn = get_db()
        rows = notify.for_user(conn, g.user)
        notify.mark_read(conn, g.user)
        return render_template("notifications.html", rows=rows)


def validate_registration(form, role):
    errors = {}
    username = (form.get("username") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
        errors["username"] = "3 to 32 characters: letters, digits, dot, dash or underscore."
    if len(form.get("password") or "") < config.MIN_PASSWORD_LENGTH:
        errors["password"] = f"At least {config.MIN_PASSWORD_LENGTH} characters."
    if form.get("password") != form.get("password2"):
        errors["password2"] = "Passwords do not match."
    for k in ("full_name", "email", "phone"):
        if not (form.get(k) or "").strip():
            errors[k] = "Required."
    if form.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", form.get("email").strip()):
        errors["email"] = "Enter a valid e-mail address."
    if role == "customer":
        for k in ("company", "address"):
            if not (form.get(k) or "").strip():
                errors[k] = "Required."
    return errors


def expected_version():
    """Version the client read (form field, JSON body or header) for optimistic locking."""
    v = request.form.get("version") if request.form else None
    if v is None and request.is_json:
        v = (request.get_json(silent=True) or {}).get("version")
    return v or request.headers.get("X-Record-Version")


def outcome_flash(outcome, done_msg, pending_msg="Sent to the HOD for approval."):
    kind, _ = outcome
    flash(done_msg if kind == "applied" else pending_msg, "success" if kind == "applied" else "info")


def report_file_name(rep, job, path):
    """Name of a downloaded report: {report_no}_{sample_no}.docx/.pdf (the sealed file on disk keeps its name)."""
    ext = os.path.splitext(path)[1]
    return f"{rep['report_no'].replace('/', '-')}_{job['sample_code']}{ext}"
