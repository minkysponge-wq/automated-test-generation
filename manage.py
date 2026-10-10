"""
Administrative commands.

    python manage.py create-hod                 create the first HOD account (prompts for the details)
    python manage.py create-hod --username hod --password hod12345 --name "Dr. R. Krishnamurthy" --email hod@cpri.in
    python manage.py backup                     consistent copy of the database into storage/backups/
    python manage.py import-template            rebuild report_templates/v1 from the reference format
    python manage.py verify-audit               verify the hash chain of the activity log
    python manage.py check-ai                   Gemini connection, models, a test call, and the reference request form
    python manage.py check-ai --file <path> --kind <request_form | logsheet:<code> | logsheet:auto | work_instruction>
"""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))  # before config is read

import argparse
import getpass
import sys

import config
from app import approval_types  # noqa: F401
from app import audit, auth, db
from app.db import tx


def create_hod(a):
    db.init_db()
    conn = db.connect()
    username = a.username or input("Username: ").strip()
    name = a.name or input("Full name: ").strip()
    email = a.email or input("E-mail: ").strip()
    phone = a.phone or input("Phone: ").strip() or "-"
    pw = a.password or getpass.getpass(f"Password (min {config.MIN_PASSWORD_LENGTH}): ")
    if len(pw) < config.MIN_PASSWORD_LENGTH:
        print(f"Password must have at least {config.MIN_PASSWORD_LENGTH} characters.")
        return 1
    with tx(conn):
        uid = auth.register_user(conn, "hod", {"username": username, "password": pw, "full_name": name, "email": email,
                                               "phone": phone}, "active")
        audit.log(conn, "user.create_hod", "user", uid, None, {"username": username, "via": "manage.py"},
                  user={"id": None, "username": "manage.py", "role": "system"})
    print(f"HOD account '{username}' created.")
    return 0


def backup(_a):
    db.init_db()
    conn = db.connect()
    from app.routes.hod import make_backup
    print("written", make_backup(conn, None))
    return 0


def import_template(_a):
    import runpy
    sys.argv = ["import_template.py"]
    runpy.run_path("tools/import_template.py", run_name="__main__")
    return 0


def verify_audit(_a):
    conn = db.connect()
    res = audit.verify_chain(conn)
    print("audit chain", "intact" if res["ok"] else f"BROKEN at row {res['bad_id']}", f"({res['rows']} rows)")
    return 0 if res["ok"] else 1


def check_ai(a):
    """Diagnostics: client mode, models, a 1-token test call, then read_document on a file."""
    db.init_db()
    from app.ai import client, intake
    from google.genai import types
    print(f"API key: {client.masked_key()}  AI_ENABLED={config.AI_ENABLED}")
    print(client.status_line())
    st = client.connect()
    if st["client"] is None:
        return 1
    r = st["client"].models.generate_content(model=st["fast"], contents="Reply with the single word OK",
                                             config=types.GenerateContentConfig(max_output_tokens=200, temperature=0, thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)))
    print(f"test call ({st['fast']}): {(r.text or '').strip()!r}")
    path = a.file or "reference/Customer_request_form.pdf"
    kind = a.kind or "request_form"
    print(f"\nreading {path} as {kind} ...")
    res = intake.read_document(path, kind, db.connect())
    parts = res["parts"].items() if kind == "logsheet:auto" else [(kind, res)]
    for name, part in parts:
        print(f"\n[{name}] method={part['method']} model={part.get('model')} calls={part.get('calls')} {part['summary']}")
        print(f"{'field':26} {'value':42} {'conf':>5}  source")
        for k, v in part["fields"].items():
            print(f"{k:26} {str(v['value'])[:42]:42} {v['confidence']:5.2f}  {v.get('source') or ''}")
        if part.get("drawings"):
            print("drawings:", ", ".join(d["number"] for d in part["drawings"]))
        if part.get("tests") is not None and kind == "request_form":
            print("tests requested:", ", ".join(part["tests"]) or "-")
    return 0


def main():
    ap = argparse.ArgumentParser(description="ATRG administration")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("create-hod")
    for opt in ("--username", "--password", "--name", "--email", "--phone"):
        h.add_argument(opt)
    sub.add_parser("backup")
    sub.add_parser("import-template")
    sub.add_parser("verify-audit")
    c = sub.add_parser("check-ai")
    c.add_argument("--file")
    c.add_argument("--kind")
    a = ap.parse_args()
    return {"create-hod": create_hod, "backup": backup, "import-template": import_template,
            "verify-audit": verify_audit, "check-ai": check_ai}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
