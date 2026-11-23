"""Access from other devices on the network: one secret key, given out as a link."""

import re
import secrets
import socket

from . import config

KEY_FILE = config.DB_PATH.parent / "access_key.txt"
COOKIE = "jarvis_key"
_key: str | None = None
_HOST = re.compile(rf"^(localhost|127\.0\.0\.1|\d{{1,3}}(\.\d{{1,3}}){{3}}|\[[0-9a-f:]+\]):({config.PORT}|{config.HTTPS_PORT})$", re.I)


def key() -> str:
    global _key
    if _key is None:
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        if KEY_FILE.exists() and KEY_FILE.read_text().strip():
            _key = KEY_FILE.read_text().strip()
        else:
            _key = secrets.token_urlsafe(18)
            KEY_FILE.write_text(_key)
    return _key


def check(given: str | None) -> bool:
    return bool(given) and secrets.compare_digest(given, key())


def lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return None


def link() -> str | None:
    ip = lan_ip()
    return f"https://{ip}:{config.HTTPS_PORT}/?key={key()}" if ip else None


def host_ok(host: str) -> bool:
    """Only IP addresses and localhost: a hostname here could be a DNS-rebinding attack."""
    if not config.LAN:
        return host in (f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}")
    return bool(_HOST.match(host))


def is_local(client_ip: str | None) -> bool:
    return client_ip in ("127.0.0.1", "::1")
