"""Controls the owner's Android phone over ADB wireless debugging (paired once with this PC).

`adb shell` joins its arguments into one command line for the phone's shell, so every value that comes from a
person or the model is quoted with shlex.quote, and package names and phone numbers are validated first.
Calls and messages only open the dialer or composer; the owner presses call or send on the phone.
"""

import os
import re
import shlex
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import db
from ..admin import Refused
from . import tool
from .system import NO_WINDOW

KNOWN = {
    "whatsapp": "com.whatsapp", "whatsapp business": "com.whatsapp.w4b", "youtube": "com.google.android.youtube",
    "youtube music": "com.google.android.apps.youtube.music", "instagram": "com.instagram.android",
    "facebook": "com.facebook.katana", "chrome": "com.android.chrome", "gmail": "com.google.android.gm",
    "maps": "com.google.android.apps.maps", "google maps": "com.google.android.apps.maps",
    "spotify": "com.spotify.music", "telegram": "org.telegram.messenger", "snapchat": "com.snapchat.android",
    "twitter": "com.twitter.android", "x": "com.twitter.android", "netflix": "com.netflix.mediaclient",
    "amazon": "in.amazon.mShop.android.shopping", "paytm": "net.one97.paytm", "phonepe": "com.phonepe.app",
    "gpay": "com.google.android.apps.nbu.paisa.user", "google pay": "com.google.android.apps.nbu.paisa.user",
    "linkedin": "com.linkedin.android", "drive": "com.google.android.apps.docs", "photos": "com.google.android.apps.photos",
    "google photos": "com.google.android.apps.photos", "calendar": "com.google.android.calendar",
    "play store": "com.android.vending", "settings": "com.android.settings", "camera": "com.android.camera",
    "chatgpt": "com.openai.chatgpt", "claude": "com.anthropic.claude", "gemini": "com.google.android.apps.bard",
    "swiggy": "in.swiggy.android", "zomato": "com.application.zomato", "uber": "com.ubercab", "ola": "com.olacabs.customer",
    "hotstar": "in.startv.hotstar", "jio hotstar": "in.startv.hotstar", "prime video": "com.amazon.avod.thirdpartyclient",
    "truecaller": "com.truecaller", "flipkart": "com.flipkart.android", "google": "com.google.android.googlequicksearchbox",
    "messages": "com.google.android.apps.messaging", "sms": "com.google.android.apps.messaging",
    "phone": "com.google.android.dialer", "dialer": "com.google.android.dialer", "contacts": "com.google.android.contacts",
    "keep": "com.google.android.keep", "notes": "com.google.android.keep", "google keep": "com.google.android.keep",
    "files": "com.google.android.apps.nbu.files", "file manager": "com.google.android.apps.nbu.files",
    "bgmi": "com.pubg.imobile", "pubg": "com.pubg.imobile", "battlegrounds": "com.pubg.imobile",
    "jio": "com.jio.myjio", "myjio": "com.jio.myjio",
    "lens": "com.google.ar.lens", "google lens": "com.google.ar.lens", "home": "com.google.android.apps.chromecast.app",
    "google home": "com.google.android.apps.chromecast.app", "docs": "com.google.android.apps.docs.editors.docs",
    "assistant": "com.google.android.apps.googleassistant", "duckduckgo": "com.duckduckgo.mobile.android",
    
}
KEYS = {"volume_up": 24, "volume_down": 25, "mute": 164, "play_pause": 85, "next": 87, "previous": 88,
        "stop": 86, "home": 3, "back": 4, "sleep": 223, "wake": 224, "recents": 187}
PKG = re.compile(r"^[A-Za-z][\w]*(\.[\w]+)+$")
_lock = threading.Lock()
_serial: str | None = None


def adb_path() -> str | None:
    found = os.getenv("ADB_PATH") or shutil.which("adb")
    if found:
        return found
    sdk = Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk" / "platform-tools" / "adb.exe"
    return str(sdk) if sdk.exists() else None


def _adb(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    exe = adb_path()
    if not exe:
        raise Refused("ADB isn't installed on this PC, so I can't reach your phone.")
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW,
                          encoding="utf-8", errors="replace")


def _remember(key: str, value: str) -> None:
    db.x("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)


def _connected() -> list[str]:
    out = _adb("devices").stdout
    return [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("device")]


def _scan(ip: str) -> int | None:
    """Wireless debugging listens on a random port that changes whenever it restarts; find it."""
    def probe(port: int) -> int | None:
        try:
            with socket.create_connection((ip, port), timeout=0.5):
                return port
        except OSError:
            return None
    with ThreadPoolExecutor(max_workers=400) as pool:
        for port in pool.map(probe, range(30000, 50000)):
            if port:
                return port
    return None


PAIR_HELP = ("On your phone open Settings, Developer options, Wireless debugging, Pair device with pairing code, "
             "then give me the code and the address it shows.")
PROBLEMS = {
    "not_set_up": "Your phone isn't linked yet. " + PAIR_HELP,
    "unreachable": "I can't see your phone on the network. Check it's on the same Wi-Fi as this PC.",
    "debugging_off": "Your phone is on the network but Wireless debugging is off. Turn it on in Developer options.",
    "not_paired": "Your phone has forgotten this PC (this happens when Developer options is switched off). "
                  "Pair it again: " + PAIR_HELP,
}
_problem: str | None = None
_last_scan = 0.0


class PhoneProblem(Refused):
    def __init__(self, code: str):
        global _problem
        _problem = code
        self.code = code
        super().__init__(PROBLEMS[code])


def _try(address: str) -> bool:
    out = _adb("connect", address, timeout=8).stdout
    return f"connected to {address}" in out


def _reachable(ip: str) -> bool:
    # Phones in power saving often skip a ping or two, so give it three chances.
    out = subprocess.run(["ping", "-n", "3", "-w", "1000", ip], capture_output=True, text=True, creationflags=NO_WINDOW)
    return "TTL=" in out.stdout.upper()


def connect(force: bool = False) -> str:
    """The phone's ADB serial, reconnecting (and finding its new port) when needed. Raises PhoneProblem saying
    what's wrong: not set up, not on the network, Wireless debugging off, or the pairing forgotten."""
    global _serial, _problem, _last_scan
    with _lock:
        live = _connected()
        if _serial in live and not force:
            return _serial
        wireless = [d for d in live if ":" in d]
        if wireless and not force:
            _serial, _problem = wireless[0], None
            _remember("phone_ip", _serial.split(":")[0])
            _remember("phone_port", _serial.split(":")[1])
            return _serial
        s = db.settings()
        ip, port = s.get("phone_ip"), s.get("phone_port")
        if not ip:
            raise PhoneProblem("not_set_up")
        if port and _try(f"{ip}:{port}"):
            _serial, _problem = f"{ip}:{port}", None
            return _serial
        if not _reachable(ip):
            raise PhoneProblem("unreachable")
        # A full port scan takes several seconds; don't repeat it more than once every 20 s.
        if time.time() - _last_scan < 20 and _problem in ("debugging_off", "not_paired"):
            raise PhoneProblem(_problem)
        _last_scan = time.time()
        found = _scan(ip)
        if not found:
            raise PhoneProblem("debugging_off")
        _remember("phone_port", str(found))
        if not _try(f"{ip}:{found}"):
            raise PhoneProblem("not_paired")
        _serial, _problem = f"{ip}:{found}", None
        return _serial


def pair(address: str, code: str) -> str:
    """Pairs this PC with the phone using the code from its "Pair device with pairing code" box, then connects."""
    address, code = address.strip(), re.sub(r"\D", "", code)
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}:\d{4,5}", address):
        raise Refused("The address should look like 192.168.1.20:40447, from the pairing box on your phone.")
    if len(code) != 6:
        raise Refused("The pairing code has 6 digits.")
    out = _adb("pair", address, code, timeout=25)
    text = (out.stdout or "") + (out.stderr or "")
    if "Successfully paired" not in text:
        if "protocol fault" in text or "Unable to start pairing" in text:
            raise Refused("That code didn't work; it may have expired or the pairing box closed. "
                          "Open Pair device with pairing code again and use the new code straight away.")
        raise Refused(f"Pairing failed: {text.strip()[:160]}")
    ip = address.split(":")[0]
    _remember("phone_ip", ip)
    try:
        connect(force=True)
    except PhoneProblem as e:
        return f"Paired, but I couldn't connect yet: {e}"
    return "Paired and connected. Your phone is linked to JARVIS."


def keep_alive() -> None:
    """Background check: reconnect quietly when the phone drops (restart, Wi-Fi change, debugging toggled)."""
    if not adb_path() or not db.setting("phone_ip"):
        return
    try:
        connect()
    except Refused:
        pass


def sh(*words: str, timeout: int = 15) -> str:
    """Runs a command on the phone. Every word is quoted for the phone's shell."""
    serial = connect()
    r = _adb("-s", serial, "shell", " ".join(shlex.quote(w) for w in words), timeout=timeout)
    if r.returncode and "device" in (r.stderr or "") and "not found" in (r.stderr or ""):
        serial = connect(force=True)
        r = _adb("-s", serial, "shell", " ".join(shlex.quote(w) for w in words), timeout=timeout)
    return (r.stdout or "").strip()


def key(name: str) -> None:
    sh("input", "keyevent", str(KEYS[name]))


def _launchable() -> list[str]:
    out = sh("cmd", "package", "query-activities", "--brief", "-a", "android.intent.action.MAIN",
             "-c", "android.intent.category.LAUNCHER", timeout=25)
    return sorted({line.strip().split("/")[0] for line in out.splitlines() if "/" in line and PKG.match(line.strip().split("/")[0])})


def find_package(name: str) -> str | None:
    n = name.lower().strip().removesuffix(" app")
    pkgs = _launchable()
    if n in KNOWN and KNOWN[n] in pkgs:
        return KNOWN[n]
    words = [w for w in re.split(r"\W+", n) if len(w) > 2]
    scored = []
    for p in pkgs:
        parts = p.lower().split(".")
        score = sum(3 if w in parts else 1 if w in p.lower() else 0 for w in words) if words else 0
        if n.replace(" ", "") in p.lower().replace(".", ""):
            score += 2
        if score:
            scored.append((score, -len(p), p))
    return max(scored)[2] if scored else None


def _number(who: str) -> str:
    w = who.strip()
    digits = re.sub(r"[\s()-]", "", w)
    if re.fullmatch(r"\+?\d{5,15}", digits):
        return digits
    out = sh("content", "query", "--uri", "content://com.android.contacts/data/phones",
             "--projection", "display_name:data1", timeout=20)
    rows = re.findall(r"display_name=(.*?), data1=([+\d][\d\s()+-]{4,})", out)
    if not rows:
        raise Refused(f"I couldn't read your phone's contacts to find {who}. Say the number instead.")
    lw = w.lower()
    exact = [(n, num) for n, num in rows if n.lower() == lw]
    part = exact or [(n, num) for n, num in rows if lw in n.lower()] or \
        [(n, num) for n, num in rows if any(t in n.lower().split() for t in lw.split())]
    if not part:
        raise Refused(f"There's no {who} in your phone's contacts.")
    names = sorted({n for n, _ in part})
    if len(names) > 1:
        raise Refused(f"Which one: {', '.join(names[:4])}?")
    return re.sub(r"[\s()-]", "", part[0][1])


# ------------------------------------------------------------------ tools

@tool("Open an app on the user's Android phone.", name="string: the app's name, e.g. WhatsApp, YouTube, Camera")
def phone_open_app(name: str) -> str:
    pkg = find_package(name)
    if not pkg:
        return f"I couldn't find {name} on your phone."
    key("wake")
    sh("monkey", "-p", pkg, "-c", "android.intent.category.LAUNCHER", "1")
    return f"Opened {name} on your phone."


@tool("Close (force-stop) an app on the user's phone.", name="string: the app's name")
def phone_close_app(name: str) -> str:
    pkg = find_package(name)
    if not pkg:
        return f"I couldn't find {name} on your phone."
    sh("am", "force-stop", pkg)
    return f"Closed {name} on your phone."


@tool("Change the phone's media volume.", action="string: up, down, mute or set", level="integer?: 0-100, only for set")
def phone_volume(action: str, level: int | None = None) -> str:
    a = action.lower()
    if a == "set" and level is not None:
        out = sh("cmd", "media_session", "volume", "--stream", "3", "--get")
        mx = re.search(r"range \[\d+\.\.(\d+)\]", out)
        top = int(mx.group(1)) if mx else 15
        value = round(max(0, min(100, int(level))) / 100 * top)
        sh("cmd", "media_session", "volume", "--show", "--stream", "3", "--set", str(value))
        return f"Phone volume set to {level}%."
    if a in ("up", "down"):
        for _ in range(3):
            key(f"volume_{a}")
        return f"Phone volume {a}."
    if a in ("mute", "unmute"):
        key("mute")
        return "Toggled the phone's mute."
    return "Say up, down, mute or set."


@tool("Control music or video playing on the phone.", action="string: play, pause, next, previous or stop")
def phone_media(action: str) -> str:
    k = {"play": "play_pause", "pause": "play_pause", "resume": "play_pause", "next": "next", "skip": "next",
         "previous": "previous", "back": "previous", "stop": "stop"}.get(action.lower())
    if not k:
        return "Say play, pause, next, previous or stop."
    key(k)
    return {"play_pause": "Toggled playback on your phone.", "next": "Next track on your phone.",
            "previous": "Previous track on your phone.", "stop": "Stopped playback on your phone."}[k]


@tool("The phone's battery level, charging state, model and Android version.")
def phone_status() -> dict:
    bat = sh("dumpsys", "battery")
    level = re.search(r"level: (\d+)", bat)
    charging = re.search(r"(AC|USB|Wireless) powered: true", bat)
    return {"model": sh("getprop", "ro.product.model"), "maker": sh("getprop", "ro.product.manufacturer"),
            "android": sh("getprop", "ro.build.version.release"), "battery": int(level.group(1)) if level else None,
            "charging": bool(charging)}


@tool("Take a screenshot of the phone's screen and copy it to this PC.")
def phone_screenshot() -> str:
    name = f"jarvis_{time.strftime('%Y%m%d_%H%M%S')}.png"
    remote = f"/sdcard/Pictures/Screenshots/{name}"
    sh("mkdir", "-p", "/sdcard/Pictures/Screenshots")
    sh("screencap", "-p", remote, timeout=30)
    local_dir = Path.home() / "Pictures" / "JARVIS Phone"
    local_dir.mkdir(parents=True, exist_ok=True)
    _adb("-s", connect(), "pull", remote, str(local_dir / name), timeout=60)
    return f"Screenshot saved on your phone and copied to Pictures, JARVIS Phone, on this PC."


@tool("Open the phone's dialer ready to call a contact or number (the user presses call).",
      who="string: contact name or phone number")
def phone_call(who: str) -> str:
    number = _number(who)
    key("wake")
    sh("am", "start", "-a", "android.intent.action.DIAL", "-d", f"tel:{number}")
    return f"The dialer is open on your phone with {who}. Press call."


@tool("Open a text message or WhatsApp chat to a contact with the message typed in (the user presses send).",
      who="string: contact name or phone number", text="string: the message",
      app="string?: sms or whatsapp; default sms")
def phone_message(who: str, text: str, app: str = "sms") -> str:
    whatsapp = "whats" in (app or "").lower()
    if whatsapp and not any(p in sh("pm", "list", "packages", "com.whatsapp") for p in ("com.whatsapp", "com.whatsapp.w4b")):
        return "WhatsApp isn't installed on your phone. Say 'message' instead and I'll use a text message."
    number = _number(who)
    key("wake")
    if whatsapp:
        intl = number.lstrip("+")
        if len(intl) == 10:
            intl = "91" + intl  # 10-digit Indian mobile numbers need the country code for WhatsApp links
        sh("am", "start", "-a", "android.intent.action.VIEW", "-d",
           f"https://wa.me/{intl}?text={urllib.parse.quote(text)}")
        return f"WhatsApp is open with your message to {who}. Press send."
    sh("am", "start", "-a", "android.intent.action.SENDTO", "-d", f"smsto:{number}", "--es", "sms_body", text)
    return f"Your message to {who} is typed in Messages. Press send."


@tool("Open a website, or search YouTube, on the phone.", url="string?: web address", youtube="string?: what to play on YouTube")
def phone_open(url: str = "", youtube: str = "") -> str:
    if youtube:
        target = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(youtube)
    elif url:
        target = url if re.match(r"^https?://", url, re.I) else "https://" + url
    else:
        return "What should I open?"
    if not re.match(r"^https?://[\w.-]+(/\S*)?$", target):
        return "That isn't a web address."
    key("wake")
    sh("am", "start", "-a", "android.intent.action.VIEW", "-d", target)
    return f"Opened {'YouTube' if youtube else target} on your phone."


@tool("Type text into whatever is focused on the phone.", text="string: what to type")
def phone_type(text: str) -> str:
    safe = re.sub(r"[^\w .,?!@#:/+=-]", "", text)[:300].replace(" ", "%s")
    if not safe:
        return "Nothing I can type."
    sh("input", "text", safe)
    return "Typed it on your phone."


@tool("Lock (switch off the screen of) or wake the phone.", action="string: lock or wake")
def phone_screen(action: str) -> str:
    key("sleep" if action.lower().startswith("l") or "off" in action.lower() else "wake")
    return "Phone locked." if action.lower().startswith("l") or "off" in action.lower() else "Phone screen on."


def plan_uninstall(name: str) -> dict:
    """Admin-voice action: only apps the owner installed, never system apps."""
    pkg = find_package(name)
    if not pkg:
        raise Refused(f"I couldn't find {name} on your phone.")
    user_apps = {line.removeprefix("package:").strip() for line in sh("pm", "list", "packages", "-3").splitlines()}
    if pkg not in user_apps:
        raise Refused(f"{name} came with the phone; I won't remove system apps.")
    return {"kind": "phone_uninstall", "args": {"package": pkg, "name": name},
            "summary": f"Uninstall {name} ({pkg}) from your phone"}


@tool("Uninstall an app from the user's phone. Needs the owner's admin voice.", name="string: the app's name")
def phone_uninstall_app(name: str) -> dict:
    return plan_uninstall(name)


def uninstall(args: dict) -> str:
    pkg = args["package"]
    if not PKG.match(pkg):
        return "That isn't a valid app."
    out = sh("pm", "uninstall", pkg, timeout=60)
    return f"Uninstalled {args['name']} from your phone." if "Success" in out else f"The phone wouldn't uninstall it: {out[:120]}"


def summary() -> dict:
    """For the HUD. Never scans (that takes seconds); keep_alive() does the reconnecting."""
    if not adb_path():
        return {"connected": False, "problem": "no_adb", "message": "ADB isn't installed on this PC."}
    try:
        if any(":" in d for d in _connected()):
            return {"connected": True, **phone_status()}
    except Exception:
        pass
    code = _problem or ("not_set_up" if not db.setting("phone_ip") else "unreachable")
    return {"connected": False, "problem": code, "message": PROBLEMS.get(code, ""), "ip": db.setting("phone_ip")}
