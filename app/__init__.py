"""
Flask application factory for CPRI ATRG.
"""
import json
import os
import sqlite3
from datetime import timedelta

from flask import Flask, flash, jsonify, redirect, render_template, request

import config
from . import approval_types  # noqa: F401  (registers the HOD approval types)
from . import auth, db, fields, notify, workflow
from .workflow import Conflict, WorkflowError


def _wants_json():
    return (request.path.startswith("/api/") or request.is_json
            or request.headers.get("X-Requested-With") == "fetch"
            or request.accept_mimetypes.best == "application/json")


def create_app(test_config=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        SECRET_KEY=config.secret_key(),
        MAX_CONTENT_LENGTH=(config.MAX_UPLOAD_MB * 3) * 1024 * 1024,   # several files per request, 20 MB each
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=config.HTTPS,        # plain HTTP on the lab network unless HTTPS is configured
        SESSION_COOKIE_NAME="atrg_session",
    )                                              # SERVER_NAME stays unset so any host / IP address works
    if test_config:
        app.config.update(test_config)
    for d in (config.UPLOAD_DIR, config.REPORT_DIR, config.SEALED_DIR, config.PREVIEW_DIR, config.INSTANCE_DIR,
              config.BACKUP_DIR):
        os.makedirs(d, exist_ok=True)
    db.init_db()
    app.teardown_appcontext(db.close_db)

    # ---- CSRF on every POST
    @app.before_request
    def _csrf():
        if not app.config.get("TESTING_DISABLE_CSRF"):
            auth.check_csrf()

    # ---- template helpers
    @app.context_processor
    def _inject():
        from flask import g
        from .routes.common import ROLE_META, nav_state
        unread = 0
        user = getattr(g, "user", None)
        if user:
            try:
                unread = notify.unread_count(db.get_db(), user)
            except Exception:
                unread = 0
        return {
            "csrf_token": auth.csrf_token,
            "STATE_LABELS": workflow.STATE_LABELS,
            "TIMELINE": workflow.TIMELINE,
            "STATIONS": fields.stations(),
            "TESTS": fields.tests(),
            "ROLE_META": ROLE_META,
            "unread_count": unread,
            "nav_state": nav_state,
            "signed_in_as": lambda r: auth.load_user(r),
        }

    @app.template_filter("fromjson")
    def _fromjson(s):
        return json.loads(s) if s else {}

    @app.template_filter("dt")
    def _dt(s):
        """2026-10-10T14:03:22+05:30 -> 10-10-2026 14:03"""
        if not s:
            return ""
        return f"{s[8:10]}-{s[5:7]}-{s[0:4]} {s[11:16]}".strip()

    @app.template_filter("d")
    def _d(s):
        return f"{s[8:10]}-{s[5:7]}-{s[0:4]}" if s else ""

    # ---- error handling: conflicts, workflow errors, DB-level seal violations
    @app.errorhandler(Conflict)
    def _conflict(e):
        if _wants_json():
            return jsonify(ok=False, error=str(e), conflict=True), 409
        flash(str(e), "danger")
        return redirect(request.referrer or "/"), 303

    @app.errorhandler(WorkflowError)
    def _wf(e):
        if _wants_json():
            return jsonify(ok=False, error=str(e)), getattr(e, "status", 400)
        flash(str(e), "danger")
        return redirect(request.referrer or "/"), 303

    @app.errorhandler(sqlite3.IntegrityError)
    def _integrity(e):
        msg = str(e)
        if "SEALED" in msg or "IMMUTABLE" in msg or "APPEND-ONLY" in msg:
            msg = "Blocked by the database: " + msg
        if _wants_json():
            return jsonify(ok=False, error=msg), 409
        flash(msg, "danger")
        return redirect(request.referrer or "/"), 303

    @app.errorhandler(413)
    def _too_big(e):
        msg = f"Upload too large - maximum {config.MAX_UPLOAD_MB} MB per file."
        if _wants_json():
            return jsonify(ok=False, error=msg), 413
        flash(msg, "danger")
        return redirect(request.referrer or "/"), 303

    @app.errorhandler(400)
    def _bad(e):
        if _wants_json():
            return jsonify(ok=False, error=getattr(e, "description", "Bad request")), 400
        return render_template("error.html", code=400, message=getattr(e, "description", "Bad request")), 400

    @app.errorhandler(403)
    def _forbidden(e):
        if _wants_json():
            return jsonify(ok=False, error="Forbidden"), 403
        return render_template("error.html", code=403, message="You do not have access to this page."), 403

    @app.errorhandler(404)
    def _nf(e):
        if _wants_json():
            return jsonify(ok=False, error="Not found"), 404
        return render_template("error.html", code=404, message="Page not found."), 404

    from .routes import admin, api, customer, engineer, hod, public
    app.register_blueprint(public.bp)
    app.register_blueprint(customer.bp, url_prefix="/customer")
    app.register_blueprint(engineer.bp, url_prefix="/engineer")
    app.register_blueprint(admin.bp, url_prefix="/admin")
    app.register_blueprint(hod.bp, url_prefix="/hod")
    app.register_blueprint(api.bp, url_prefix="/api")
    return app
