"""
Authentication helpers for the four portals (customer, engineer, admin, HOD).

Each role keeps its own key in the (signed) Flask session cookie:
    session["customer_uid"], session["engineer_uid"], session["admin_uid"], session["hod_uid"]
so one browser can be logged in as customer in one tab and admin in another
during the demo, while `@role_required("admin")` only ever looks at the
admin key - a customer login can never open an admin page.
"""
import functools
import secrets

from flask import abort, flash, g, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, now_ist

ROLES = ("customer", "engineer", "admin", "hod")


def session_key(role):
    return f"{role}_uid"


def hash_password(pw):
    return generate_password_hash(pw)


def check_password(pw_hash, pw):
    return check_password_hash(pw_hash, pw)


def load_user(role):
    uid = session.get(session_key(role))
    if not uid:
        return None
    row = get_db().execute("SELECT * FROM users WHERE id=? AND role=?", (uid, role)).fetchone()
    if row is None or row["status"] != "active":
        session.pop(session_key(role), None)
        return None
    return dict(row)


def login(role, user_row):
    session[session_key(role)] = user_row["id"]
    session.permanent = True


def logout(role):
    session.pop(session_key(role), None)


def role_required(role):
    """Decorator: the current request must carry a valid login for `role`."""
    def deco(view):
        @functools.wraps(view)
        def wrapped(*args, **kwargs):
            user = load_user(role)
            if user is None:
                if request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json":
                    abort(401)
                flash(f"Please log in to the {role} portal.", "warning")
                return redirect(url_for(f"{role}.login", next=request.full_path))
            g.user = user
            g.role = role
            return view(*args, **kwargs)
        return wrapped
    return deco


# --- CSRF protection (simple synchroniser token) -------------------------------
def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return session["csrf"]


def check_csrf():
    """Called before every POST. Accepts form field or X-CSRF-Token header."""
    if request.method != "POST":
        return
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not sent or sent != session.get("csrf"):
        abort(400, description="Invalid or missing CSRF token - reload the page and try again.")


def register_user(conn, role, form, status="active"):
    """Insert a user row; returns new id. Caller validates the form."""
    cur = conn.execute(
        "INSERT INTO users(username,password_hash,role,full_name,email,phone,company,address,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (form["username"].strip(), hash_password(form["password"]), role, form["full_name"].strip(),
         form.get("email", "").strip(), form.get("phone", "").strip(), form.get("company", "").strip(),
         form.get("address", "").strip(), status, now_ist()),
    )
    return cur.lastrowid
