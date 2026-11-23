"""Changing the PC: delete, create, write scripts, uninstall.

The tools here only PLAN a change and describe it. Nothing happens until the owner approves that exact
description with their admin voice; then main.py calls execute(), which checks everything again.
"""

import os
import re
import shutil
import subprocess
import json
from pathlib import Path

from .. import config
from ..admin import Refused
from . import tool
from .system import NO_WINDOW, _match, _ps, recycle_bin_count, empty_recycle_bin, start_apps

HOME = Path.home().resolve()
_places: dict[str, Path] | None = None

LANGUAGES = {"python": ".py", "javascript": ".js", "typescript": ".ts", "powershell": ".ps1", "batch": ".bat",
             "html": ".html", "css": ".css", "java": ".java", "c": ".c", "c++": ".cpp", "cpp": ".cpp", "c#": ".cs",
             "go": ".go", "rust": ".rs", "sql": ".sql", "bash": ".sh", "shell": ".sh", "json": ".json",
             "markdown": ".md", "text": ".txt", "react": ".jsx", "jsx": ".jsx"}

# Never touched: Windows, installed programs, app data, JARVIS itself.
PROTECTED_APPS = re.compile(r"\b(windows|visual c\+\+|\.net|webview|driver|runtime|redistributable|ollama|python|"
                            r"microsoft edge|defender|nvidia|intel|amd|realtek|bios|firmware)\b", re.I)


def places() -> dict[str, Path]:
    """The user's real Desktop/Documents/... (OneDrive can move them)."""
    global _places
    if _places is None:
        out = _ps("'Desktop','MyDocuments','MyPictures','MyMusic','MyVideos' | ForEach-Object { "
                  "[Environment]::GetFolderPath($_) }").stdout.split()
        keys = ["desktop", "documents", "pictures", "music", "videos"]
        _places = {k: Path(v) for k, v in zip(keys, out) if v} or {}
        _places.setdefault("desktop", HOME / "Desktop")
        _places.setdefault("documents", HOME / "Documents")
        _places["downloads"] = HOME / "Downloads"
        _places["home"] = HOME
    return _places


def _where(location: str) -> Path:
    loc = (location or "").strip().strip('"')
    if not loc:
        return places()["documents"]
    key = loc.lower().removeprefix("my ").removesuffix(" folder")
    if key in places():
        return places()[key]
    return Path(loc).expanduser()


def _show(p: Path) -> str:
    try:
        return "~\\" + str(p.relative_to(HOME))
    except ValueError:
        return str(p)


def _forbidden() -> list[Path]:
    env = os.environ
    return [Path(env.get("SystemRoot", r"C:\Windows")), Path(env.get("ProgramFiles", r"C:\Program Files")),
            Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")), Path(env.get("ProgramData", r"C:\ProgramData")),
            HOME / "AppData", config.ROOT.resolve()]


def check(path: Path, *, deleting: bool) -> Path:
    p = path.expanduser().resolve()
    if p == Path(p.anchor):
        raise Refused("I won't touch the top of a drive.")
    if any(part.lower() in ("$recycle.bin", "system volume information", "windows", "boot") for part in p.parts[1:2]):
        raise Refused("That's a system folder; I won't touch it.")
    for f in _forbidden():
        f = f.resolve()
        if p == f or f in p.parents:
            raise Refused(f"{_show(p)} is inside {_show(f)}, which I never change.")
    system_drive = Path(os.environ.get("SystemDrive", "C:") + "\\").resolve()
    if p.anchor.lower() == system_drive.anchor.lower() and HOME not in p.parents and p != HOME:
        raise Refused("On the system drive I only work inside your own user folder.")
    if deleting and (p == HOME or p in {v.resolve() for v in places().values()}):
        raise Refused(f"I won't delete your whole {p.name} folder.")
    return p


def _plan(kind: str, summary: str, **args) -> dict:
    return {"kind": kind, "summary": summary, "args": args}


@tool("Delete a file or folder (it goes to the Recycle Bin). Needs the owner's admin voice. Use the exact path "
      "from find_files.", path="string: full path of the file or folder")
def delete_path(path: str) -> dict:
    p = check(Path(path), deleting=True)
    if not p.exists():
        raise Refused(f"There's nothing at {path}. Use find_files to get the exact path.")
    what = "the folder" if p.is_dir() else "the file"
    extra = ""
    if p.is_dir():
        n = sum(1 for _ in zip(range(100000), p.rglob("*")))
        extra = f" ({n} item{'s' if n != 1 else ''} inside)" if n else " (empty)"
    return _plan("delete", f"Move {what} {_show(p)}{extra} to the Recycle Bin", path=str(p))


@tool("Create a new folder. Needs the owner's admin voice.", name="string: the folder's name",
      location="string?: desktop, documents, downloads, or a full path; default documents")
def create_folder(name: str, location: str = "documents") -> dict:
    if re.search(r'[<>:"|?*]', name) or name.strip(". ") == "":
        raise Refused(f"{name} isn't a valid folder name.")
    p = check(_where(location) / name.strip(), deleting=False)
    if p.exists():
        raise Refused(f"{_show(p)} already exists.")
    return _plan("create_folder", f"Create the folder {_show(p)}", path=str(p))


@tool("Create a new file, optionally with text in it. Needs the owner's admin voice.",
      name="string: file name with extension, e.g. notes.txt",
      location="string?: desktop, documents, downloads, or a full path; default documents",
      content="string?: text to put in the file")
def create_file(name: str, location: str = "documents", content: str = "") -> dict:
    if re.search(r'[<>:"|?*\\/]', name) or name.strip(". ") == "":
        raise Refused(f"{name} isn't a valid file name.")
    p = check(_where(location) / name.strip(), deleting=False)
    if p.exists():
        raise Refused(f"{_show(p)} already exists; I won't overwrite it.")
    if p.suffix.lower() in (".exe", ".msi", ".scr", ".com", ".dll", ".sys", ".lnk", ".reg"):
        raise Refused("I don't create program or system files.")
    note = f" containing {len(content)} characters of text" if content else ""
    return _plan("create_file", f"Create the file {_show(p)}{note}", path=str(p), content=content)


@tool("Write a script or program for the user, save it, and open it in VS Code for them to review (it is never "
      "run). Needs the owner's admin voice.", task="string: what the script should do, in detail",
      language="string?: python, javascript, powershell, html...; default python",
      name="string?: file name without extension")
def write_script(task: str, language: str = "python", name: str = "") -> dict:
    lang = (language or "python").lower().strip()
    ext = LANGUAGES.get(lang, ".txt")
    base = re.sub(r"[^a-z0-9]+", "_", (name or task).lower()).strip("_")[:40] or "script"
    folder = places()["documents"] / "JARVIS Scripts"
    p = folder / f"{base}{ext}"
    n = 2
    while p.exists():
        p = folder / f"{base}_{n}{ext}"
        n += 1
    check(p, deleting=False)
    return _plan("write_script", f"Write a {lang} script that will {task.strip().rstrip('.')}, save it as {_show(p)} "
                 "and open it in VS Code. It won't be run", path=str(p), task=task, language=lang)


def _installed() -> list[dict]:
    script = r"""
$keys = 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
Get-ItemProperty $keys -ErrorAction SilentlyContinue |
  Where-Object { $_.DisplayName -and $_.UninstallString -and -not $_.SystemComponent -and -not $_.ParentKeyName } |
  Select-Object DisplayName, Publisher, UninstallString | ConvertTo-Json -Compress
"""
    try:
        rows = json.loads(_ps(script, timeout=40).stdout or "[]")
    except ValueError:
        rows = []
    rows = rows if isinstance(rows, list) else [rows]
    apps = [{"name": r["DisplayName"], "publisher": r.get("Publisher") or "", "kind": "desktop",
             "uninstall": r["UninstallString"]} for r in rows]
    for a in start_apps():  # Store apps: their Start-menu ID is "<PackageFamilyName>!<App>"
        if "!" in a["id"] and not any(x["name"].lower() == a["name"].lower() for x in apps):
            apps.append({"name": a["name"], "publisher": "", "kind": "store", "family": a["id"].split("!")[0]})
    return apps


@tool("Uninstall an app from this PC with its own uninstaller. Needs the owner's admin voice.",
      name="string: the app's name")
def uninstall_app(name: str) -> dict:
    apps = _installed()
    hit = _match(name, [a["name"] for a in apps])
    if not hit:
        raise Refused(f"I couldn't find an installed app called {name}.")
    app = next(a for a in apps if a["name"] == hit)
    if PROTECTED_APPS.search(app["name"]):
        raise Refused(f"{app['name']} is part of Windows, a driver or runtime, or something I run on; I won't remove it.")
    by = f" from {app['publisher']}" if app["publisher"] else ""
    return _plan("uninstall", f"Uninstall {app['name']}{by}", app=app)


def plan_empty_recycle_bin() -> dict:
    n = recycle_bin_count()
    if not n:
        raise Refused("The Recycle Bin is already empty.")
    return _plan("empty_recycle_bin", f"Permanently delete the {n} item{'s' if n != 1 else ''} in the Recycle Bin")


PLANNERS = {"delete_path", "create_folder", "create_file", "write_script", "uninstall_app", "ai_code_job"}


def _code_cli() -> str | None:
    found = shutil.which("code")
    if found:
        return found
    for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("ProgramFiles", "")):
        p = Path(base) / ("Programs" if "Local" in base else "") / "Microsoft VS Code" / "bin" / "code.cmd"
        if p.exists():
            return str(p)
    return None


def open_in_vscode(path: Path) -> bool:
    cli = _code_cli()
    if not cli:
        os.startfile(str(path.parent))
        return False
    subprocess.Popen([cli, "-r", str(path)], creationflags=NO_WINDOW, shell=False)
    return True


async def execute(plan: dict) -> str:
    """Runs an approved plan, re-checking it first (things may have changed since it was described)."""
    from send2trash import send2trash

    kind, a = plan["kind"], plan["args"]
    if kind == "delete":
        p = check(Path(a["path"]), deleting=True)
        if not p.exists():
            return "It's already gone."
        send2trash(str(p))
        return f"Moved {p.name} to the Recycle Bin. You can restore it from there."
    if kind == "create_folder":
        p = check(Path(a["path"]), deleting=False)
        p.mkdir(parents=True, exist_ok=False)
        return f"Created the folder {p.name}."
    if kind == "create_file":
        p = check(Path(a["path"]), deleting=False)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "x", encoding="utf-8") as f:
            f.write(a.get("content") or "")
        return f"Created {p.name}."
    if kind == "write_script":
        from ..brain import write_code

        p = check(Path(a["path"]), deleting=False)
        code = await write_code(a["task"], a["language"])
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "x", encoding="utf-8") as f:
            f.write(code)
        opened = open_in_vscode(p)
        where = "opened it in VS Code" if opened else "opened its folder (VS Code wasn't found)"
        return f"Wrote {p.name} and {where}. Read it through before you run it."
    if kind == "uninstall":
        app = a["app"]
        if app["kind"] == "store":
            r = _ps(f"Get-AppxPackage | Where-Object PackageFamilyName -eq '{app['family'].replace(chr(39), '')}' | "
                    "Remove-AppxPackage -ErrorAction Stop", timeout=180)
            return f"Uninstalled {app['name']}." if r.returncode == 0 else f"Windows wouldn't remove {app['name']}."
        cmd = app["uninstall"]
        cmd = re.sub(r"(?i)msiexec(\.exe)?\s+/i", "MsiExec.exe /X", cmd)  # MSI entries often point at "repair"
        subprocess.Popen(cmd, shell=False)
        return f"Started the {app['name']} uninstaller. Follow its window to finish."
    if kind == "empty_recycle_bin":
        return empty_recycle_bin()
    if kind == "ai_code":
        from .coders import start_job

        check(Path(a["folder"]), deleting=False)
        return start_job(a)
    return "I don't know how to do that."
