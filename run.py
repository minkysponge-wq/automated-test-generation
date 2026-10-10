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
import socket

from waitress import serve

import config
from app import create_app
from app.ai import client as ai
from app.report import detect_converter


def lan_ips():
    """Best-effort list of this machine's IPv4 LAN addresses."""
    ips = set()
    try:
        # no packet is sent - this only asks the OS which interface would route outward
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    return sorted(ips)


def main():
    ap = argparse.ArgumentParser(description="CPRI ATRG server")
    ap.add_argument("--port", type=int, default=config.PORT)
    ap.add_argument("--host", default=config.HOST)
    ap.add_argument("--threads", type=int, default=config.THREADS)
    args = ap.parse_args()

    app = create_app()
    kind, path = detect_converter()
    print("=" * 66)
    print(" CPRI - Automated Test Report Generation (ATRG)")
    print("=" * 66)
    print(f" Local:   http://localhost:{args.port}")
    for ip in lan_ips():
        print(f" LAN:     http://{ip}:{args.port}   <- open this on other computers")
    print(f" PDF export: {kind or 'not available (reports served as DOCX)'} {path or ''}")
    print(f" Database:   {config.DB_PATH}")
    print(f" AI (Gemini):  {ai.status_text()}")
    print(" Demo accounts (after `python seed.py --reset`): hod/hod12345  admin/admin12345  "
          "engineer1/eng12345  customer1/cust12345")
    print(" If other PCs cannot connect, allow the port in Windows Firewall (see README).")
    print("=" * 66)
    serve(app, host=args.host, port=args.port, threads=args.threads, channel_timeout=120)


if __name__ == "__main__":
    main()
