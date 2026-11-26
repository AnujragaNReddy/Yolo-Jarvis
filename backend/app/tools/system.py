import ctypes
import difflib
import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

import httpx
import psutil

from . import tool

IS_WINDOWS = os.name == "nt"
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

SITES = {
    "youtube": "https://www.youtube.com", "gmail": "https://mail.google.com", "google": "https://www.google.com",
    "github": "https://github.com", "maps": "https://maps.google.com", "google maps": "https://maps.google.com",
    "calendar": "https://calendar.google.com", "drive": "https://drive.google.com", "google drive": "https://drive.google.com",
    "whatsapp": "https://web.whatsapp.com", "netflix": "https://www.netflix.com", "amazon": "https://www.amazon.com",
    "linkedin": "https://www.linkedin.com", "instagram": "https://www.instagram.com", "twitter": "https://x.com",
    "x": "https://x.com", "reddit": "https://www.reddit.com", "facebook": "https://www.facebook.com",
    "chatgpt": "https://chatgpt.com", "claude": "https://claude.ai", "stack overflow": "https://stackoverflow.com",
    "spotify web": "https://open.spotify.com", "outlook": "https://outlook.live.com", "news": "https://news.google.com",
    "wikipedia": "https://en.wikipedia.org", "translate": "https://translate.google.com",
}

BUILTIN_APPS = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "paint": "mspaint.exe", "file explorer": "explorer.exe",
    "explorer": "explorer.exe", "task manager": "taskmgr.exe", "settings": "ms-settings:", "control panel": "control.exe",
    "command prompt": "cmd.exe", "terminal": "wt.exe", "powershell": "powershell.exe", "snipping tool": "ms-screenclip:",
    "camera": "microsoft.windows.camera:", "clock": "ms-clock:", "store": "ms-windows-store:",
    "downloads": str(Path.home() / "Downloads"), "documents": str(Path.home() / "Documents"),
    "desktop": str(Path.home() / "Desktop"), "pictures": str(Path.home() / "Pictures"),
}

# Things open_file refuses to launch: anything that runs code.
RUNNABLE = {".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".msi", ".msp",
            ".scr", ".lnk", ".reg", ".hta", ".cpl", ".jar", ".py", ".pyw"}

_apps: list[dict] = []
_apps_loaded = 0.0
_apps_lock = threading.Lock()


def start_apps() -> list[dict]:
    """Everything in the Start menu (desktop and Store apps), refreshed every 10 minutes."""
    global _apps, _apps_loaded
    with _apps_lock:
        if _apps and time.time() - _apps_loaded < 600:
            return _apps
        if not IS_WINDOWS:
            return []
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=30, creationflags=NO_WINDOW).stdout
        try:
            rows = json.loads(out or "[]")
            _apps = [{"name": r["Name"], "id": r["AppID"]} for r in (rows if isinstance(rows, list) else [rows])]
            _apps_loaded = time.time()
        except (json.JSONDecodeError, KeyError):
            pass
        return _apps


def _match(query: str, names: list[str]) -> str | None:
    q = query.lower().strip()
    lowered = {n.lower(): n for n in names}
    if q in lowered:
        return lowered[q]
    starts = [n for low, n in lowered.items() if low.startswith(q)]
    if starts:
        return min(starts, key=len)
    words = [n for low, n in lowered.items() if re.search(rf"\b{re.escape(q)}\b", low)]
    if words:
        return min(words, key=len)
    close = difflib.get_close_matches(q, list(lowered), n=1, cutoff=0.75)
    return lowered[close[0]] if close else None


@tool("Open an installed application or a common folder on this PC, e.g. Spotify, VS Code, Chrome, Notepad, Downloads.",
      name="string: the app's name")
def open_app(name: str) -> str:
    key = name.lower().strip().removesuffix(" app")
    if key in BUILTIN_APPS:
        os.startfile(BUILTIN_APPS[key])
        return f"Opened {name}."
    apps = start_apps()
    hit = _match(key, [a["name"] for a in apps])
    if hit:
        app = next(a for a in apps if a["name"] == hit)
        os.startfile(f"shell:AppsFolder\\{app['id']}")
        return f"Opened {hit}."
    if key in SITES:
        return open_website(key)
    return f"I couldn't find an app called {name} on this PC."


@tool("Close a running application (it can still ask to save work).", name="string: the app's name, e.g. notepad")
def close_app(name: str) -> str:
    key = name.lower().strip().replace(" ", "")
    protected = {"explorer.exe", "python.exe", "pythonw.exe", "winlogon.exe", "csrss.exe", "svchost.exe",
                 "lsass.exe", "services.exe", "dwm.exe", "system", "ollama.exe", "node.exe"}
    images = sorted({p.info["name"] for p in psutil.process_iter(["name"]) if p.info["name"]
                     and key in p.info["name"].lower().replace(" ", "") and p.info["name"].lower() not in protected})
    if not images:
        return f"{name} doesn't seem to be running."
    for image in images:
        subprocess.run(["taskkill", "/im", image], capture_output=True, creationflags=NO_WINDOW)
    return f"Asked {', '.join(images)} to close."


@tool("Open a website in the browser. Give a site name (YouTube, Gmail, GitHub...) or a full https address.",
      site="string: site name or URL")
def open_website(site: str) -> str:
    s = site.strip()
    url = SITES.get(s.lower())
    if not url:
        if not re.match(r"^https?://", s, re.I):
            if re.match(r"^[\w.-]+\.[a-z]{2,}(/\S*)?$", s, re.I):
                url = "https://" + s
            else:
                url = "https://www.google.com/search?q=" + urllib.parse.quote_plus(s)
        else:
            url = s
    if not re.match(r"^https?://", url, re.I):
        return "I can only open web addresses."
    webbrowser.open(url)
    return f"Opened {url}."


@tool("Search Google in the user's browser (use when they ask to 'google' something or see results themselves).",
      query="string: search terms")
def search_in_browser(query: str) -> str:
    webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote_plus(query))
    return f"Searching Google for {query}."


@tool("Play a song or video on YouTube in the browser.", query="string: song, artist or video to play")
def play_youtube(query: str) -> str:
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(query)
    try:
        page = httpx.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}).text
        vid = re.search(r'"videoId":"([\w-]{11})"', page)
        if vid:
            webbrowser.open(f"https://www.youtube.com/watch?v={vid.group(1)}")
            return f"Playing {query} on YouTube."
    except httpx.HTTPError:
        pass
    webbrowser.open(url)
    return f"Opened YouTube results for {query}."


VK = {"mute": 0xAD, "down": 0xAE, "up": 0xAF, "next": 0xB0, "previous": 0xB1, "stop": 0xB2, "play_pause": 0xB3}


def _press(vk: int, times: int = 1) -> None:
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.01)


def whatsapp_send(number: str, text: str) -> str:
    """Send a WhatsApp Web message from this PC. Not a model tool: only reachable through a spoken "confirm" (intents.py)."""
    digits = re.sub(r"\D", "", number)
    if len(digits) == 10:
        digits = "91" + digits  # 10-digit Indian mobile numbers need the country code
    if not 8 <= len(digits) <= 15:
        return "That doesn't look like a phone number."
    webbrowser.open(f"https://web.whatsapp.com/send?phone={digits}&text={urllib.parse.quote(text)}")

    def press_enter() -> None:
        time.sleep(18)  # WhatsApp Web needs a while to load the chat and fill in the message
        _press(0x0D)

    threading.Thread(target=press_enter, daemon=True).start()
    return f"Opening WhatsApp Web and sending your message to +{digits}. If WhatsApp Web asks you to scan a QR code, it can't send until you do."


@tool("Change the PC's volume.", action="string: up, down, mute, unmute or set",
      level="integer?: 0-100, only for set")
def volume(action: str, level: int | None = None) -> str:
    a = action.lower()
    if a == "set" and level is not None:
        level = max(0, min(100, int(level)))
        _press(VK["down"], 50)
        _press(VK["up"], round(level / 2))
        return f"Volume set to {level}%."
    if a in ("mute", "unmute"):
        _press(VK["mute"])
        return "Toggled mute."
    if a in ("up", "down"):
        _press(VK[a], 5)
        return f"Volume {a}."
    return "Say up, down, mute or set."


@tool("Control whatever music or video is playing (Spotify, YouTube, etc.).",
      action="string: play, pause, next, previous or stop")
def media(action: str) -> str:
    a = action.lower()
    key = {"play": "play_pause", "pause": "play_pause", "resume": "play_pause", "toggle": "play_pause",
           "next": "next", "skip": "next", "previous": "previous", "back": "previous", "stop": "stop"}.get(a)
    if not key:
        return "Say play, pause, next, previous or stop."
    _press(VK[key])
    return {"play_pause": "Toggled playback.", "next": "Next track.", "previous": "Previous track.", "stop": "Stopped."}[key]


def stats() -> dict:
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\" if IS_WINDOWS else "/")
    bat = psutil.sensors_battery()
    net = psutil.net_io_counters()
    return {
        "cpu": psutil.cpu_percent(interval=None),
        "cores": psutil.cpu_count(),
        "ram": vm.percent, "ram_used_gb": round(vm.used / 1e9, 1), "ram_total_gb": round(vm.total / 1e9, 1),
        "disk": disk.percent, "disk_free_gb": round(disk.free / 1e9, 1),
        "battery": round(bat.percent) if bat else None, "plugged": bat.power_plugged if bat else None,
        "uptime_h": round((time.time() - psutil.boot_time()) / 3600, 1),
        "net_sent_mb": round(net.bytes_sent / 1e6), "net_recv_mb": round(net.bytes_recv / 1e6),
    }


@tool("This PC's CPU, memory, disk, battery, uptime and the programs using the most memory.")
def system_status() -> dict:
    s = stats()
    s["cpu"] = psutil.cpu_percent(interval=0.5)
    procs = sorted(psutil.process_iter(["name", "memory_info"]),
                   key=lambda p: p.info["memory_info"].rss if p.info["memory_info"] else 0, reverse=True)
    s["top_memory"] = [f"{p.info['name']} {round(p.info['memory_info'].rss / 1e6)} MB" for p in procs[:5]]
    return s


@tool("Lock the PC screen.")
def lock_screen() -> str:
    ctypes.windll.user32.LockWorkStation()
    return "Screen locked."


def _search_roots() -> list[Path]:
    home = Path.home()
    return [p for p in (home / n for n in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos",
                                             "OneDrive")) if p.exists()]


@tool("Find files and folders on this PC by name (Desktop, Documents, Downloads, Pictures, Music, Videos).",
      name="string: part of the file name")
def find_files(name: str) -> list[dict] | str:
    needle = name.lower().strip()
    found, deadline = [], time.time() + 4
    for root in _search_roots():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "node_modules", "AppData", "$"))]
            for d in dirnames:
                if needle in d.lower():
                    p = Path(dirpath) / d
                    found.append({"name": d + "\\", "path": str(p), "folder": True,
                                  "modified": time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))})
            for f in filenames:
                if needle in f.lower():
                    p = Path(dirpath) / f
                    try:
                        found.append({"name": f, "path": str(p), "modified": time.strftime("%Y-%m-%d", time.localtime(p.stat().st_mtime))})
                    except OSError:
                        continue
            if len(found) >= 25 or time.time() > deadline:
                break
    found.sort(key=lambda r: r["modified"], reverse=True)
    return found[:8] or f"No files matching {name}."


@tool("Open a document, picture, video or folder by its full path (from find_files).", path="string: full path")
def open_file(path: str) -> str:
    p = Path(path).expanduser().resolve()
    home = Path.home().resolve()
    if home not in p.parents and p != home:
        return "I only open files in your own folders."
    if not p.exists():
        return "That file doesn't exist."
    if p.suffix.lower() in RUNNABLE:
        return "That's a program or script; I won't run it for you. Open it yourself if you trust it."
    os.startfile(str(p))
    return f"Opened {p.name}."


def _ps(script: str, timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)


@tool("Set or change the laptop screen brightness.", action="string: up, down or set", level="integer?: 0-100, only for set")
def brightness(action: str, level: int | None = None) -> str:
    cur = _ps("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness | Select-Object -First 1).CurrentBrightness").stdout.strip()
    if not cur.isdigit():
        return "This screen's brightness can't be changed from software (it may be an external monitor)."
    value = {"up": int(cur) + 20, "down": int(cur) - 20}.get(action.lower(), level if level is not None else int(cur))
    value = max(0, min(100, int(value)))
    _ps(f"Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods | "
        f"Invoke-CimMethod -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={value}}} | Out-Null")
    return f"Brightness {value}%."


@tool("Take a screenshot of the whole screen (saved to Pictures\\Screenshots).")
def screenshot() -> str:
    # Win + Print Screen: Windows saves the shot itself, to Pictures\Screenshots.
    u = ctypes.windll.user32
    u.keybd_event(0x5B, 0, 0, 0)
    u.keybd_event(0x2C, 0, 0, 0)
    u.keybd_event(0x2C, 0, 2, 0)
    u.keybd_event(0x5B, 0, 2, 0)
    return "Screenshot saved to Pictures, Screenshots."


def show_desktop() -> str:
    u = ctypes.windll.user32
    u.keybd_event(0x5B, 0, 0, 0)
    u.keybd_event(0x44, 0, 0, 0)
    u.keybd_event(0x44, 0, 2, 0)
    u.keybd_event(0x5B, 0, 2, 0)
    return "Showing the desktop."


SETTINGS_PAGES = {"wifi": "ms-settings:network-wifi", "wi-fi": "ms-settings:network-wifi", "bluetooth": "ms-settings:bluetooth",
                  "display": "ms-settings:display", "sound": "ms-settings:sound", "battery": "ms-settings:batterysaver",
                  "update": "ms-settings:windowsupdate", "windows update": "ms-settings:windowsupdate",
                  "network": "ms-settings:network", "apps": "ms-settings:appsfeatures", "storage": "ms-settings:storagesense"}


def open_settings(page: str) -> str:
    os.startfile(SETTINGS_PAGES.get(page.lower(), "ms-settings:"))
    return f"Opened {page} settings."


def recycle_bin_count() -> int:
    out = _ps("(New-Object -ComObject Shell.Application).NameSpace(10).Items().Count").stdout.strip()
    return int(out) if out.isdigit() else 0


def empty_recycle_bin() -> str:
    """Only reachable through an explicit spoken command plus "confirm" (see intents.py), never the model."""
    r = _ps("Clear-RecycleBin -Force -ErrorAction Stop", timeout=120)
    return "Recycle Bin emptied." if r.returncode == 0 else "I couldn't empty the Recycle Bin."


def sleep() -> str:
    """Only reachable through an explicit spoken command plus "confirm"."""
    threading.Timer(3, lambda: ctypes.windll.powrprof.SetSuspendState(False, True, False)).start()
    return "Going to sleep. Goodnight."


def power(action: str) -> str:
    """Only reachable through an explicit spoken command plus "confirm" (see intents.py), never the model."""
    if action == "cancel":
        r = subprocess.run(["shutdown", "/a"], capture_output=True, creationflags=NO_WINDOW)
        return "Cancelled." if r.returncode == 0 else "Nothing was scheduled."
    flag = {"shutdown": "/s", "restart": "/r"}[action]
    subprocess.run(["shutdown", flag, "/t", "30"], capture_output=True, creationflags=NO_WINDOW)
    return f"{'Shutting down' if action == 'shutdown' else 'Restarting'} in 30 seconds. Say 'cancel shutdown' to stop it."


def warm() -> None:
    psutil.cpu_percent(interval=None)
    try:
        start_apps()
    except Exception:
        pass
