"""Reads Yolo Network Scanner's API, when it is running on this PC."""

import httpx

from .. import config
from . import tool


def summary() -> dict | None:
    try:
        devices = httpx.get(f"{config.NETWORK_SCANNER_URL}/api/devices", timeout=4).json()
    except (httpx.HTTPError, ValueError):
        return None
    online = [d for d in devices if d.get("online")]
    return {
        "online": len(online),
        "known": len(devices),
        "new": [d.get("name") or d.get("ip") for d in devices if d.get("is_new")],
        "not_trusted_online": [d.get("name") or d.get("ip") for d in online if not d.get("trusted")],
        "risky": [d.get("name") or d.get("ip") for d in devices if d.get("risk") in ("high", "medium")],
    }


@tool("Who is on the home Wi-Fi network: devices online, new devices, untrusted and risky ones (from Yolo Network Scanner).")
def network_devices() -> dict | str:
    s = summary()
    if s is None:
        return "Yolo Network Scanner isn't running, so I can't see the network. Start it with its start.bat."
    s["not_trusted_online"] = s["not_trusted_online"][:10]
    return s
