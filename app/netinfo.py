"""
This computer's addresses on the local network, so other computers know where to connect.
No packet is sent: the UDP "connect" only asks the OS which interface would route outward.
"""
import socket


def _usable(ip):
    return bool(ip) and not ip.startswith("127.") and not ip.startswith("169.254.") and ip != "0.0.0.0"


def get_lan_addresses():
    """Every non-loopback, non-link-local IPv4 address of this machine (sorted, no duplicates)."""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if _usable(ip))


def network_url(request, default_port):
    """`http://<first LAN IP>:<port>` for the info line on the Home pages (port as the browser used it)."""
    ips = get_lan_addresses()
    if not ips:
        return None
    host = request.host or ""
    port = host.rsplit(":", 1)[1] if ":" in host and not host.endswith("]") else str(default_port)
    return f"http://{ips[0]}:{port}"
