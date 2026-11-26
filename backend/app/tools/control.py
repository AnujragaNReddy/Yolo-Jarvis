"""Full control of the PC: keyboard, windows, settings, processes, clipboard, commands, and the screen itself.

Keyboard and screen actions aim at the window the user was working in. When they talk to JARVIS from this PC the
HUD's browser tab is in front, so "type hello" must not land in JARVIS's own command box: the HUD is skipped and
the next window down is used, unless they name an app ("type hello in Notepad").

Running a command is the one thing here that can do anything at all, so it only PLANS (like tools/files.py): the
owner approves the exact command with their admin voice before execute() runs it.
"""

import base64
import ctypes
import json
import os
import re
import subprocess
import time
import winreg
from ctypes import wintypes

import psutil

from ..admin import Refused
from . import tool
from .system import NO_WINDOW, SETTINGS_PAGES, _match, _ps, open_settings, show_desktop

user32 = ctypes.windll.user32

# ------------------------------------------------------------------ finding the right window

HUD = re.compile(r"J\.A\.R\.V\.I\.S", re.I)
SHELL_CLASSES = {"Shell_TrayWnd", "Shell_SecondaryTrayWnd", "Progman", "WorkerW", "Windows.UI.Core.CoreWindow"}


def _title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _process(hwnd: int) -> str:
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name()
    except psutil.Error:
        return ""


def _cloaked(hwnd: int) -> bool:
    # Store apps that are suspended, and windows on other virtual desktops, are "visible" but cloaked.
    val = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(val), ctypes.sizeof(val))
    return bool(val.value)


def windows() -> list[dict]:
    """Real app windows, front to back (the order EnumWindows gives is the Z-order)."""
    found: list[dict] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if (user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4)  # GW_OWNER: skip dialogs' owners
                and _class(hwnd) not in SHELL_CLASSES and not _cloaked(hwnd)):
            title = _title(hwnd)
            if title.strip():
                found.append({"hwnd": hwnd, "title": title, "process": _process(hwnd),
                              "minimized": bool(user32.IsIconic(hwnd))})
        return True

    user32.EnumWindows(each, 0)
    return found


def _is_hud(w: dict) -> bool:
    return bool(HUD.search(w["title"]))


def pick_window(app: str = "") -> dict:
    """The window to act on: the named app's, or the one the user was in before JARVIS."""
    wins = windows()
    if app.strip():
        key = app.lower().strip().removesuffix(" app").removesuffix(" window")
        if key in ("this", "it", "the", "current", "that"):
            app = ""
        else:
            names = {f"{w['title']}": w for w in wins}
            names.update({w["process"].lower().removesuffix(".exe"): w for w in reversed(wins) if w["process"]})
            hit = _match(key, list(names))
            if not hit:
                hit = next((n for n in names if key in n.lower()), None)
            if not hit:
                raise Refused(f"I can't see a {app} window open.")
            return names[hit]
    fg = user32.GetForegroundWindow()
    rest = [w for w in wins if not _is_hud(w)]
    front = next((w for w in rest if w["hwnd"] == fg), None)
    if front:
        return front
    if not rest:
        raise Refused("There's no window open to act on.")
    return rest[0]


def focus(w: dict) -> None:
    hwnd = w["hwnd"]
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    if user32.GetForegroundWindow() != hwnd:
        # Windows only lets the foreground app hand focus over; a tap of Alt makes us count as "the user".
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    time.sleep(0.25)


def _name(w: dict) -> str:
    t = w["title"]
    return t if len(t) <= 60 else t[:57] + "..."


# ------------------------------------------------------------------ keyboard

class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT)]

    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def _send(*inputs: _INPUT) -> None:
    arr = (_INPUT * len(inputs))(*inputs)
    user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT))


def _unicode(ch: str) -> list[_INPUT]:
    out = []
    data = ch.encode("utf-16-le")
    for i in range(0, len(data), 2):  # characters outside the BMP are two UTF-16 units
        unit = int.from_bytes(data[i:i + 2], "little")
        for flags in (0x0004, 0x0004 | 0x0002):  # KEYEVENTF_UNICODE, then key up
            inp = _INPUT(type=1)
            inp.ki = _KEYBDINPUT(0, unit, flags, 0, 0)
            out.append(inp)
    return out


def type_into(text: str) -> None:
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            _tap(0x0D)
        elif ch == "\t":
            _tap(0x09)
        else:
            _send(*_unicode(ch))
        time.sleep(0.004)


VKEYS = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B, "cmd": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20, "spacebar": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "page up": 0x21, "pgup": 0x21, "pagedown": 0x22, "page down": 0x22, "pgdn": 0x22,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "printscreen": 0x2C, "print screen": 0x2C,
    "capslock": 0x14, "menu": 0x5D, "plus": 0xBB, "minus": 0xBD, "comma": 0xBC, "period": 0xBE, "dot": 0xBE,
    "slash": 0xBF, "backslash": 0xDC, "semicolon": 0xBA, "quote": 0xDE, "bracketleft": 0xDB, "bracketright": 0xDD,
    "=": 0xBB, "-": 0xBD, ",": 0xBC, ".": 0xBE, "/": 0xBF, "\\": 0xDC, ";": 0xBA, "'": 0xDE, "[": 0xDB, "]": 0xDD,
    "`": 0xC0, "volumeup": 0xAF, "volumedown": 0xAE, "mute": 0xAD,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}
EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D}
SPOKEN = {"control": "ctrl", "windows key": "win", "window key": "win", "the windows key": "win", "option": "alt",
          "page up": "pageup", "page down": "pagedown", "print screen": "printscreen", "arrow up": "up",
          "arrow down": "down", "arrow left": "left", "arrow right": "right", "up arrow": "up", "down arrow": "down",
          "left arrow": "left", "right arrow": "right", "escape key": "esc", "enter key": "enter"}


def _vk(name: str) -> int:
    n = name.strip().lower()
    n = SPOKEN.get(n, n)
    if n in VKEYS:
        return VKEYS[n]
    if len(n) == 1 and n.isalnum():
        return ord(n.upper())
    raise Refused(f"I don't know the key '{name}'.")


def _key(vk: int, up: bool) -> None:
    flags = (0x0001 if vk in EXTENDED else 0) | (0x0002 if up else 0)
    user32.keybd_event(vk, 0, flags, 0)


def _tap(vk: int) -> None:
    _key(vk, False)
    _key(vk, True)


def parse_combo(keys: str) -> list[int]:
    k = keys.lower().strip()
    for spoken, short in SPOKEN.items():
        k = k.replace(spoken, short)
    parts = [p for p in re.split(r"\s*\+\s*|\s+plus\s+|\s+and\s+|\s+", k) if p]
    if not parts:
        raise Refused("Which keys?")
    return [_vk(p) for p in parts]


def press_combo(vks: list[int]) -> None:
    for vk in vks:
        _key(vk, False)
        time.sleep(0.02)
    for vk in reversed(vks):
        _key(vk, True)
        time.sleep(0.01)


@tool("Type text on this PC, as if on the keyboard, into the window the user was using (or the named app). "
      "Use for 'type ...', 'write ... in Notepad', filling a search box.",
      text="string: exactly what to type", app="string?: app to type into, e.g. Notepad; empty = the current window")
def type_text(text: str, app: str = "") -> str:
    if not text:
        return "What should I type?"
    w = pick_window(app)
    focus(w)
    type_into(text)
    return f"Typed {len(text)} characters into {_name(w)}."


@tool("Press keys or a keyboard shortcut on this PC, e.g. ctrl+c, ctrl+s, alt+tab, enter, f5, win+d, ctrl+shift+t.",
      keys="string: the keys joined with +", app="string?: app to send them to; empty = the current window",
      times="integer?: how many times to press, default 1")
def press_keys(keys: str, app: str = "", times: int = 1) -> str:
    vks = parse_combo(keys)
    # alt+tab and win+... act on the desktop; everything else goes to the user's window, not the HUD.
    if not (vks[:2] == [0x12, 0x09] or vks[0] == 0x5B):
        focus(pick_window(app))
    for _ in range(max(1, min(50, int(times or 1)))):
        press_combo(vks)
        time.sleep(0.05)
    return f"Pressed {keys}" + (f" {times} times." if times and int(times) > 1 else ".")


# ------------------------------------------------------------------ windows

@tool("Manage windows on this PC: switch to an app, minimise, maximise, restore, close, snap left or right, scroll, "
      "list open windows, or show the desktop.",
      action="string: switch, minimize, maximize, restore, close, snap_left, snap_right, scroll_up, scroll_down, "
             "list, show_desktop or minimize_all",
      app="string?: which app's window; empty = the current window")
def window_control(action: str, app: str = "") -> str | list:
    a = action.lower().strip().replace(" ", "_").replace("minimise", "minimize").replace("maximise", "maximize")
    a = {"focus": "switch", "go_to": "switch", "show": "switch", "activate": "switch", "open": "switch",
         "list_windows": "list", "desktop": "show_desktop", "hide_all": "minimize_all", "min": "minimize",
         "max": "maximize", "full_screen": "maximize", "fullscreen": "maximize", "left": "snap_left",
         "right": "snap_right", "scroll": "scroll_down", "up": "scroll_up", "down": "scroll_down"}.get(a, a)
    if a == "list":
        wins = [w for w in windows() if not _is_hud(w)]
        return [f"{_name(w)}{' (minimised)' if w['minimized'] else ''}" for w in wins[:15]] or "No windows are open."
    if a in ("show_desktop", "minimize_all"):
        return show_desktop()
    w = pick_window(app)
    hwnd = w["hwnd"]
    if a == "switch":
        focus(w)
        return f"Switched to {_name(w)}."
    if a in ("minimize", "maximize", "restore"):
        user32.ShowWindow(hwnd, {"minimize": 6, "maximize": 3, "restore": 9}[a])
        if a != "minimize":
            focus(w)
        return f"{a.capitalize()}d {_name(w)}."
    if a == "close":
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE: the app can still ask to save
        return f"Closing {_name(w)}."
    if a in ("snap_left", "snap_right"):
        focus(w)
        press_combo([0x5B, 0x25 if a == "snap_left" else 0x27])
        return f"Snapped {_name(w)} to the {a.split('_')[1]}."
    if a in ("scroll_up", "scroll_down"):
        focus(w)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        user32.SetCursorPos((rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2)
        for _ in range(5):
            user32.mouse_event(0x0800, 0, 0, 120 if a == "scroll_up" else -120, 0)  # MOUSEEVENTF_WHEEL
            time.sleep(0.03)
        return f"Scrolled {a.split('_')[1]}."
    return "Say switch, minimise, maximise, restore, close, snap left or right, scroll, or list."


# ------------------------------------------------------------------ settings and hardware

_RADIO = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTask = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and
  $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($op, [Type]$t) { $task = $asTask.MakeGenericMethod($t).Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
[Windows.Devices.Radios.Radio,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Radios.RadioAccessStatus,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
[Windows.Devices.Radios.RadioState,Windows.System.Devices,ContentType=WindowsRuntime] | Out-Null
Await ([Windows.Devices.Radios.Radio]::RequestAccessAsync()) ([Windows.Devices.Radios.RadioAccessStatus]) | Out-Null
$radios = Await ([Windows.Devices.Radios.Radio]::GetRadiosAsync()) ([System.Collections.Generic.IReadOnlyList[Windows.Devices.Radios.Radio]])
$r = $radios | Where-Object { $_.Kind -eq '__KIND__' } | Select-Object -First 1
if (-not $r) { 'none'; exit }
if ('__STATE__' -eq 'Query') { [string]$r.State; exit }
[string](Await ($r.SetStateAsync('__STATE__')) ([Windows.Devices.Radios.RadioAccessStatus]))
"""


def radio(kind: str, state: str) -> str:
    """kind WiFi or Bluetooth; state On, Off or Query. Needs no admin rights (Windows.Devices.Radios)."""
    out = _ps(_RADIO.replace("__KIND__", kind).replace("__STATE__", state), timeout=25).stdout.strip().splitlines()
    return out[-1].strip() if out else ""


PERSONALIZE = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"

MORE_PAGES = {
    "airplane mode": "ms-settings:network-airplanemode", "airplane": "ms-settings:network-airplanemode",
    "night light": "ms-settings:nightlight", "focus": "ms-settings:quiethours", "do not disturb": "ms-settings:quiethours",
    "notifications": "ms-settings:notifications", "battery saver": "ms-settings:batterysaver",
    "power": "ms-settings:powersleep", "sleep": "ms-settings:powersleep", "mouse": "ms-settings:mousetouchpad",
    "touchpad": "ms-settings:devices-touchpad", "keyboard": "ms-settings:keyboard", "printers": "ms-settings:printers",
    "personalization": "ms-settings:personalization", "background": "ms-settings:personalization-background",
    "colors": "ms-settings:colors", "themes": "ms-settings:themes", "lock screen": "ms-settings:lockscreen",
    "hotspot": "ms-settings:network-mobilehotspot", "vpn": "ms-settings:network-vpn",
    "default apps": "ms-settings:defaultapps", "startup apps": "ms-settings:startupapps",
    "about": "ms-settings:about", "privacy": "ms-settings:privacy", "camera": "ms-settings:privacy-webcam",
    "microphone": "ms-settings:privacy-microphone", "date": "ms-settings:dateandtime", "time": "ms-settings:dateandtime",
    "language": "ms-settings:regionlanguage", "accounts": "ms-settings:yourinfo", "multitasking": "ms-settings:multitasking",
    "projector": "ms-settings-displays-topology:", "second screen": "ms-settings-displays-topology:",
}


@tool("Change this PC's settings and hardware: Wi-Fi on/off/connect/list, Bluetooth on/off, dark or light mode, "
      "or open any Settings page (airplane mode, night light, do not disturb, battery saver, display, sound...).",
      setting="string: wifi, bluetooth, dark_mode, or a Settings page name",
      value="string?: on, off, status, list (wifi networks), a Wi-Fi network name to connect to, or open")
def pc_setting(setting: str, value: str = "") -> str:
    s = setting.lower().strip().replace("_", " ").replace("-", "")
    v = (value or "").strip()
    vl = v.lower()
    if s in ("wifi", "wireless", "wlan"):
        if vl in ("on", "off", "enable", "disable", "true", "false", "status", ""):
            if vl in ("status", ""):
                st = radio("WiFi", "Query")
                if st == "none":
                    return "This PC has no Wi-Fi adapter."
                conn = _ps("(netsh wlan show interfaces) -match '^\\s+SSID' | Select-Object -First 1").stdout.strip()
                ssid = conn.split(":", 1)[1].strip() if ":" in conn else ""
                return f"Wi-Fi is {st.lower()}" + (f", connected to {ssid}." if ssid else ".")
            on = vl in ("on", "enable", "true")
            res = radio("WiFi", "On" if on else "Off")
            if res == "none":
                return "This PC has no Wi-Fi adapter."
            return f"Wi-Fi turned {'on' if on else 'off'}." if res == "Allowed" else f"Windows refused ({res})."
        if vl in ("list", "networks", "scan"):
            out = subprocess.run(["netsh", "wlan", "show", "networks"], capture_output=True, text=True,
                                 creationflags=NO_WINDOW).stdout
            names = [m.strip() for m in re.findall(r"^SSID \d+ : (.*)$", out, re.M) if m.strip()]
            return ("Wi-Fi networks in range: " + ", ".join(names[:12]) + ".") if names else "I can't see any Wi-Fi networks."
        r = subprocess.run(["netsh", "wlan", "connect", f"name={v}"], capture_output=True, text=True, creationflags=NO_WINDOW)
        if r.returncode == 0:
            return f"Connecting to {v}."
        return f"I couldn't connect to {v}. It has to be a network this PC has joined before."
    if s in ("bluetooth", "bt"):
        st = "Query" if vl in ("status", "") else ("On" if vl in ("on", "enable", "true") else "Off")
        res = radio("Bluetooth", st)
        if res == "none":
            return "This PC has no Bluetooth radio."
        if st == "Query":
            return f"Bluetooth is {res.lower()}."
        return f"Bluetooth turned {st.lower()}." if res == "Allowed" else f"Windows refused ({res})."
    if s in ("dark mode", "darkmode", "dark", "light mode", "lightmode", "light", "theme", "dark theme", "light theme"):
        if s.startswith("light"):
            dark = vl not in ("off", "disable", "false")
            dark = not dark
        else:
            dark = vl not in ("off", "disable", "false")
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE, 0, winreg.KEY_SET_VALUE) as k:
            for name in ("AppsUseLightTheme", "SystemUsesLightTheme"):
                winreg.SetValueEx(k, name, 0, winreg.REG_DWORD, 0 if dark else 1)
        return f"{'Dark' if dark else 'Light'} mode on."
    page = MORE_PAGES.get(s) or SETTINGS_PAGES.get(s)
    if page:
        os.startfile(page)
        extra = "" if vl in ("", "open") else f" Windows doesn't let apps switch {setting} {v} directly, so flip it there."
        return f"Opened {setting} settings.{extra}"
    return open_settings(setting)


# ------------------------------------------------------------------ processes

NEVER_KILL = {"system", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
              "lsass.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "explorer.exe", "python.exe", "pythonw.exe",
              "ollama.exe", "ollama app.exe", "msmpeng.exe", "conhost.exe", "sihost.exe", "ctfmon.exe"}


@tool("See or stop running programs on this PC: list what's running and using the most memory or CPU, force-kill "
      "a frozen app, or restart Windows Explorer (fixes a stuck taskbar).",
      action="string: list, kill or restart_explorer", name="string?: the program to kill, e.g. chrome")
def processes(action: str, name: str = "") -> str | list:
    a = action.lower().strip().replace(" ", "_")
    if a in ("list", "top", "running", "show"):
        for p in psutil.process_iter(["name"]):
            try:
                p.cpu_percent(None)
            except psutil.Error:
                pass
        time.sleep(0.6)
        apps: dict[str, dict] = {}
        for p in psutil.process_iter(["name", "memory_info"]):
            try:
                n = p.info["name"] or "?"
                row = apps.setdefault(n, {"name": n, "mb": 0, "cpu": 0.0, "count": 0})
                row["mb"] += round((p.info["memory_info"].rss if p.info["memory_info"] else 0) / 1e6)
                row["cpu"] += p.cpu_percent(None)
                row["count"] += 1
            except psutil.Error:
                continue
        top = sorted(apps.values(), key=lambda r: r["mb"], reverse=True)[:10]
        return [f"{r['name']}: {r['mb']} MB, CPU {round(r['cpu'] / psutil.cpu_count())}%"
                + (f" ({r['count']} processes)" if r["count"] > 1 else "") for r in top]
    if a in ("restart_explorer", "explorer"):
        subprocess.run(["taskkill", "/f", "/im", "explorer.exe"], capture_output=True, creationflags=NO_WINDOW)
        time.sleep(1)
        subprocess.Popen(["explorer.exe"], creationflags=NO_WINDOW)
        return "Restarted Windows Explorer."
    if a in ("kill", "stop", "end", "force_close", "terminate"):
        key = name.lower().strip().replace(" ", "")
        if not key:
            return "Which program should I stop?"
        me = {os.getpid(), *[c.pid for c in psutil.Process().parents()]}
        victims = [p for p in psutil.process_iter(["name"]) if p.info["name"]
                   and key in p.info["name"].lower().replace(" ", "") and p.info["name"].lower() not in NEVER_KILL
                   and p.pid not in me]
        if not victims:
            return f"Nothing called {name} is running that I'm allowed to stop."
        names = sorted({p.info["name"] for p in victims})
        for p in victims:
            try:
                p.kill()
            except psutil.Error:
                pass
        return f"Force-stopped {', '.join(names)}."
    return "Say list, kill or restart explorer."


# ------------------------------------------------------------------ clipboard

@tool("Read, set or clear the clipboard (what copy and paste use) on this PC.",
      action="string: read, set or clear", text="string?: the text to put on the clipboard, for set")
def clipboard(action: str, text: str = "") -> str:
    a = action.lower().strip()
    if a in ("read", "get", "show", "what"):
        out = _ps("$t = Get-Clipboard -Raw; if ($t) { [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($t)) }").stdout.strip()
        if not out:
            return "The clipboard is empty (or holds a picture or files)."
        value = base64.b64decode(out).decode("utf-8", "replace")
        return f"The clipboard holds: {value[:1500]}"
    if a in ("set", "copy", "put", "write"):
        b64 = base64.b64encode(text.encode("utf-8")).decode()
        _ps(f"Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}')))")
        return "Copied to the clipboard."
    if a in ("clear", "empty"):
        _ps("Set-Clipboard -Value $null")
        return "Clipboard cleared."
    return "Say read, set or clear."


# ------------------------------------------------------------------ running commands (admin voice)

# The admin voice is the real gate; this only stops a misheard request from becoming a disaster.
CATASTROPHIC = re.compile(
    r"\b(format(-volume)?\s+[a-z]:|format-volume|clear-disk|initialize-disk|remove-partition|diskpart|bcdedit|"
    r"cipher\s+/w|vssadmin\s+delete|wbadmin\s+delete|reg(\.exe)?\s+delete\s+hklm|remove-item\s+.*hklm:|"
    r"takeown\s+/f\s+[a-z]:\\windows|icacls\s+[a-z]:\\windows|stop-computer|restart-computer|"
    r"remove-item\s+(-\w+\s+)*['\"]?[a-z]:\\?['\"]?(\s|$)|rd\s+/s\s+/q\s+[a-z]:\\?(\s|$)|del\s+/[fsq].*[a-z]:\\windows|"
    r"set-mppreference\s+.*-disable|disable-computerrestore|net\s+user\s+\S+\s+/delete|"
    r"invoke-expression\s*\(?\s*\(?\s*(new-object|iwr|invoke-webrequest)|iex\s*\(?\s*\(?\s*(new-object|iwr|irm))",
    re.I)


@tool("Run a PowerShell command on this PC and report its output, for anything no other tool covers (e.g. "
      "'what's my IP address', 'list my installed drivers', 'flush DNS', 'how big is my Downloads folder'). "
      "Write one safe command. Needs the owner's admin voice; never say it has run.",
      command="string: the PowerShell command", reason="string?: one short sentence on what it does")
def run_command(command: str, reason: str = "") -> dict:
    cmd = (command or "").strip().strip("`").strip()
    cmd = re.sub(r"^(powershell|pwsh)(\.exe)?\s+(-command\s+)?", "", cmd, flags=re.I).strip()
    if not cmd:
        raise Refused("What command should I run?")
    if len(cmd) > 1500:
        raise Refused("That command is too long to approve by voice. Make it a script instead.")
    if CATASTROPHIC.search(cmd):
        raise Refused("That command could wreck Windows or wipe a drive, so I won't run it even with your approval.")
    why = f" to {reason.strip().rstrip('.').removeprefix('to ').lower()}" if reason.strip() else ""
    return {"kind": "run_command", "summary": f"Run this PowerShell command{why}: {cmd}", "args": {"command": cmd}}


def execute_command(command: str) -> str:
    """Only reached after the owner approved this exact command by voice (tools/files.py execute())."""
    if CATASTROPHIC.search(command):
        return "I won't run that."
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-Command", f"[Console]::OutputEncoding=[Text.Encoding]::UTF8; {command}"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                           cwd=os.path.expanduser("~"), creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return "The command ran for two minutes without finishing, so I stopped waiting."
    out = re.sub(r"\n{3,}", "\n\n", (r.stdout or "").strip())
    err = (r.stderr or "").strip()
    if r.returncode != 0 and not out:
        first = err.splitlines()[0] if err else f"exit code {r.returncode}"
        return f"The command failed: {first[:300]}"
    if not out:
        return "Done. The command finished with no output."
    return f"Done. Output: {out[:1200]}" + (" (cut short)" if len(out) > 1200 else "")


# ------------------------------------------------------------------ the screen (UI Automation)

INTERACTIVE = {"ButtonControl", "MenuItemControl", "HyperlinkControl", "TabItemControl", "ListItemControl",
               "CheckBoxControl", "RadioButtonControl", "EditControl", "ComboBoxControl", "TreeItemControl",
               "SplitButtonControl", "MenuBarControl", "DataItemControl"}
READABLE = INTERACTIVE | {"TextControl", "DocumentControl", "HeaderItemControl", "ToolBarControl"}
KIND = {"ButtonControl": "button", "MenuItemControl": "menu", "HyperlinkControl": "link", "TabItemControl": "tab",
        "ListItemControl": "item", "CheckBoxControl": "checkbox", "RadioButtonControl": "option",
        "EditControl": "text box", "ComboBoxControl": "dropdown", "TreeItemControl": "item",
        "SplitButtonControl": "button", "TextControl": "text", "DocumentControl": "document",
        "DataItemControl": "row", "HeaderItemControl": "header", "ToolBarControl": "toolbar", "MenuBarControl": "menu bar"}


def _ui():
    import uiautomation as auto

    return auto


def _elements(w: dict, kinds: set[str], limit: int = 400, seconds: float = 5.0) -> list:
    auto = _ui()
    root = auto.ControlFromHandle(w["hwnd"])
    if not root:
        raise Refused("I can't read that window.")
    found, deadline = [], time.time() + seconds
    for ctrl, _depth in auto.WalkControl(root, includeTop=False, maxDepth=25):
        if time.time() > deadline or len(found) >= limit:
            break
        try:
            if ctrl.ControlTypeName in kinds and (ctrl.Name or "").strip() and not ctrl.IsOffscreen:
                found.append(ctrl)
        except Exception:
            continue
    return found


@tool("Look at what's on the screen: the buttons, links, menus, boxes and text in the window the user was using "
      "(or the named app). Use for 'what's on my screen', 'read this page', or before clicking something.",
      app="string?: which app's window; empty = the current window")
def read_screen(app: str = "") -> dict:
    auto = _ui()
    with auto.UIAutomationInitializerInThread():
        w = pick_window(app)
        items, seen, text = [], set(), []
        for c in _elements(w, READABLE, limit=300):
            label = re.sub(r"\s+", " ", c.Name).strip()[:90]
            kind = KIND.get(c.ControlTypeName, "")
            if (kind, label) in seen:
                continue
            seen.add((kind, label))
            if kind in ("text", "document"):
                if len(label) > 3:
                    text.append(label)
            else:
                value = ""
                if kind == "text box":
                    try:
                        value = c.GetValuePattern().Value[:60]
                    except Exception:
                        pass
                items.append(f"{kind}: {label}" + (f" = {value}" if value else ""))
        return {"window": _name(w), "app": w["process"], "controls": items[:70], "text": text[:40]}


def _score(name: str, want: str) -> float:
    n, q = name.lower().strip(), want.lower().strip()
    if n == q:
        return 100
    if n.startswith(q) or n.endswith(q):
        return 80 - min(30, len(n) - len(q)) / 2
    if re.search(rf"\b{re.escape(q)}\b", n):
        return 60 - min(30, len(n) - len(q)) / 3
    if q in n:
        return 40
    import difflib

    r = difflib.SequenceMatcher(None, n, q).ratio()
    return r * 50 if r > 0.75 else 0


@tool("Click a button, link, tab, menu item or checkbox ON THE SCREEN by its visible name, in the window the user "
      "was using (or the named app), e.g. 'click Save', 'press the Sign in button', 'open the Settings tab'. "
      "Also types into a named text box.",
      target="string: the visible label of the thing to click, e.g. Save, Sign in, Search",
      app="string?: which app's window; empty = the current window",
      action="string?: click (default), double_click, right_click, or type",
      text="string?: what to type, when action is type")
def click_on(target: str, app: str = "", action: str = "click", text: str = "") -> str:
    auto = _ui()
    want = re.sub(r"\b(the|button|link|tab|menu|icon|option|checkbox|box)\b", " ", target, flags=re.I)
    want = re.sub(r"\s+", " ", want).strip() or target.strip()
    a = (action or "click").lower().replace(" ", "_")
    with auto.UIAutomationInitializerInThread():
        w = pick_window(app)
        focus(w)
        kinds = {"EditControl", "ComboBoxControl"} if a == "type" else READABLE
        scored = []
        for c in _elements(w, kinds):
            s = _score(c.Name, want) + (5 if c.ControlTypeName in INTERACTIVE else 0)
            if s > 5:
                scored.append((s, c))
        if not scored:
            raise Refused(f"I can't see anything called {target} in {_name(w)}.")
        scored.sort(key=lambda x: -x[0])
        best = scored[0][1]
        label = f"{KIND.get(best.ControlTypeName, '')} {best.Name.strip()[:60]}".strip()
        if a == "type":
            try:
                best.GetValuePattern().SetValue(text)
            except Exception:
                best.Click(simulateMove=False, waitTime=0.2)
                type_into(text)
            return f"Typed into {label}."
        if a == "double_click":
            best.DoubleClick(simulateMove=False, waitTime=0.2)
            return f"Double-clicked {label}."
        if a == "right_click":
            best.RightClick(simulateMove=False, waitTime=0.2)
            return f"Right-clicked {label}."
        # Patterns work even when the control is partly covered; a real click is the fallback.
        for get, call in (("GetInvokePattern", "Invoke"), ("GetTogglePattern", "Toggle"),
                          ("GetSelectionItemPattern", "Select"), ("GetExpandCollapsePattern", "Expand")):
            try:
                pattern = getattr(best, get)()
                if pattern:
                    getattr(pattern, call)()
                    return f"Clicked {label}."
            except Exception:
                continue
        best.Click(simulateMove=False, waitTime=0.2)
        return f"Clicked {label}."


def warm() -> None:
    try:
        _ui()
    except Exception:
        pass
