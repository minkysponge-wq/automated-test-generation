"""
Login / register / logout views shared by the three portals.

Each portal registers its own copy under its own URL prefix
(/customer/login, /engineer/login, /admin/login ...), with its own page title
and session key - so the portals stay separate.
"""
import re
import sqlite3

from flask import flash, g, redirect, render_template, request, url_for

import config
from .. import audit, auth
from ..db import get_db, tx

ROLE_META = {
    "customer": {"title": "Customer Portal", "icon": "🏭", "blurb": "Submit test requests, track progress and download sealed reports."},
    "engineer": {"title": "Test Engineer Portal", "icon": "⚡", "blurb": "Upload logsheets, verify readings and lock jobs for review."},
    "admin": {"title": "Test In-charge / HOD Portal", "icon": "🛡️", "blurb": "Accept requests, assign engineers, review and issue reports."},
}


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
                flash("Invalid username or password for the %s portal." % role, "danger")
            elif user["status"] == "pending":
                flash("Your account is waiting for approval by the Test In-charge.", "warning")
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

    @bp.route("/register", methods=["GET", "POST"])
    def register():
        form = request.form
        if request.method == "POST":
            errors = []
            username = (form.get("username") or "").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
                errors.append("Username: 3-32 letters, digits, . _ -")
            if len(form.get("password") or "") < 6:
                errors.append("Password must be at least 6 characters.")
            if form.get("password") != form.get("password2"):
                errors.append("Passwords do not match.")
            for k, lab in (("full_name", "Full name"), ("email", "E-mail"), ("phone", "Phone")):
                if not (form.get(k) or "").strip():
                    errors.append(f"{lab} is required.")
            if form.get("email") and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", form.get("email").strip()):
                errors.append("E-mail address looks invalid.")
            if role == "customer" and not ((form.get("company") or "").strip() and (form.get("address") or "").strip()):
                errors.append("Company name and address are required for customers.")
            if role == "admin" and form.get("invite_code") != config.ADMIN_INVITE_CODE:
                errors.append("Invalid admin invite code.")
            if not errors:
                conn = get_db()
                status = "pending" if role == "engineer" else "active"
                try:
                    with tx(conn):
                        uid = auth.register_user(conn, role, form, status)
                        audit.log(conn, "user.register", "user", uid, None, {"username": username, "role": role, "status": status},
                                  user={"id": uid, "username": username, "role": role})
                except sqlite3.IntegrityError:
                    errors.append("That username is already taken.")
                else:
                    if status == "pending":
                        flash("Registered. An administrator must approve your engineer account before you can log in.", "info")
                    else:
                        flash("Registration successful - please log in.", "success")
                    return redirect(url_for(f"{role}.login"))
            for e in errors:
                flash(e, "danger")
        return render_template("auth_register.html", role=role, meta=meta, form=form)

    @bp.route("/logout", methods=["POST", "GET"])
    def logout():
        auth.logout(role)
        flash("Logged out.", "info")
        return redirect(url_for(f"{role}.login"))


def expected_version():
    """Version the client read (form field, JSON body or header) for optimistic locking."""
    v = request.form.get("version") if request.form else None
    if v is None and request.is_json:
        v = (request.get_json(silent=True) or {}).get("version")
    return v or request.headers.get("X-Record-Version")


def current_user():
    return g.user
