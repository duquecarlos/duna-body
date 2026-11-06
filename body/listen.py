"""Which network address the web server binds. Exposing every health screen to the LAN is a Tier 1
decision; the screens themselves are not, so that one choice lives here instead of inside body/web.py.
A new way of listening -- another interface, a tunnel, TLS -- is added HERE, not in the web module.
"""
from __future__ import annotations

LOCAL = "127.0.0.1"
LAN = "0.0.0.0"


def listen_host(lan: bool) -> str:
    return LAN if lan else LOCAL


def describe(lan: bool, port: int) -> str:
    if lan:
        return f"Body app is OPEN TO EVERY DEVICE ON THIS NETWORK at http://<this-PC-IP>:{port}"
    return f"Body app on http://{LOCAL}:{port}"
