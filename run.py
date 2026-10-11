"""
Start the ATRG server for the whole local network.

    python run.py            (port from config.py / ATRG_PORT, default 5000)
    python run.py --port 8080

The laptop running this acts as the server; other computers on the same
Wi-Fi / LAN open the printed http://<LAN-IP>:<port> address.
"""
import os

from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))  # before config is read

import argparse

from waitress import serve

import config
from app import create_app
from app.ai import client as ai
from app.netinfo import get_lan_addresses
from app.report import detect_converter


def startup_banner(port, ips):
    """The copyable address block printed when the server starts."""
    lines = ["CPRI ATRG is running.",
             f"On this computer:            http://localhost:{port}"]
    if ips:
        lines.append(f"From other computers (LAN):  http://{ips[0]}:{port}")
        lines += [f"                             http://{ip}:{port}" for ip in ips[1:]]
    else:
        lines.append("From other computers (LAN):  no network address found - connect to the network first")
    lines += ["If other computers cannot connect: run allow_firewall.bat once as administrator",
              "and set this computer's network profile to Private."]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="CPRI ATRG server")
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--threads", type=int, default=config.THREADS)
    args = ap.parse_args()

    app = create_app()
    kind, path = detect_converter()
    print("=" * 66)
    print(startup_banner(args.port, get_lan_addresses()))
    print("-" * 66)
    print(f"PDF export:   {kind or 'not available (reports served as DOCX)'} {path or ''}")
    print(f"Database:     {config.DB_PATH}")
    print(f"AI (Gemini):  {ai.status_text()}")
    print("Demo accounts (after `python seed.py --reset`): hod/hod12345  admin/admin12345  "
          "engineer1/eng12345  customer1/cust12345")
    print("=" * 66)
    serve(app, host=args.host, port=args.port, threads=args.threads, channel_timeout=120)


if __name__ == "__main__":
    main()
