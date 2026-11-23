"""Hands work to AI coding assistants through their official command-line tools, signed in with the owner's own
account (no API keys): Claude Code (`claude -p`) and Gemini CLI (`gemini -p`).

Questions get an answer with no tools that can change anything. Coding jobs that write and run code are planned
here and only start after admin-voice approval; each runs in its own folder under Documents\\JARVIS Workspace.
The prompt goes in on stdin, never on the command line, so nothing in it can be read as a command.
"""

import asyncio
import json
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..admin import Refused
from . import tool
from .files import _show, check, places

HELPERS = {"claude": "Claude", "gemini": "Gemini"}
JOB_TIMEOUT = 600
ASK_TIMEOUT = 180
CLAUDE_MODEL = "sonnet"  # lighter on the owner's plan limits than the default

notify: Callable[[dict], None] = lambda event: None  # main.py points this at the HUD's event stream
_running: dict[str, asyncio.Task] = {}

RULES = ("You are working for JARVIS, the user's assistant. Work only inside the current folder: never read, change "
         "or delete anything outside it, never install software system-wide, never use admin rights. Keep the "
         "solution small and clear. When you finish, reply in plain text (no markdown) with: what you made, the file "
         "names, how to run it, and the output if you ran it. Start with a one-sentence summary.")


def workspace() -> Path:
    return places()["documents"] / "JARVIS Workspace"


def _launch(helper: str) -> list[str] | None:
    """The real program behind npm's .cmd launcher: running a .cmd goes through cmd.exe, which would treat
    characters like ( ) & in arguments as commands."""
    launcher = shutil.which(helper)
    if not launcher:
        return None
    npm = Path(launcher).parent / "node_modules"
    if helper == "claude":
        exe = npm / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        return [str(exe)] if exe.exists() else ([launcher] if launcher.lower().endswith(".exe") else None)
    script = npm / "@google" / "gemini-cli" / "bundle" / "gemini.js"
    node = shutil.which("node")
    return [node, str(script)] if node and script.exists() else None


def _which(helper: str) -> bool:
    return _launch(helper) is not None


def _helper(name: str) -> str:
    n = (name or "claude").lower().strip()
    n = {"claude code": "claude", "anthropic": "claude", "google": "gemini", "gemini cli": "gemini"}.get(n, n)
    if n in ("chatgpt", "openai", "gpt", "codex"):
        raise Refused("ChatGPT isn't connected; I can use Claude or Gemini instead.")
    if n not in HELPERS:
        raise Refused(f"I can only hand work to Claude or Gemini, not {name}.")
    if not _which(n):
        raise Refused(f"{HELPERS[n]}'s command-line tool isn't installed on this PC.")
    return n


def _command(helper: str, mode: str) -> list[str]:
    exe = _launch(helper)
    if helper == "claude":
        cmd = [*exe, "-p", "--output-format", "json", "--model", CLAUDE_MODEL, "--append-system-prompt", RULES]
        if mode == "ask":
            return cmd + ["--disallowedTools", "Bash", "Write", "Edit", "NotebookEdit"]
        return cmd + ["--permission-mode", "acceptEdits",
                      "--allowedTools", "Write", "Edit", "Read", "Glob", "Grep", "Bash(python:*)", "Bash(node:*)"]
    cmd = [*exe, "-p", "", "-o", "json", "--skip-trust"]
    if mode == "ask":
        return cmd + ["--approval-mode", "plan"]
    return cmd + ["--approval-mode", "auto_edit", "--allowed-tools", "run_shell_command(python)",
                  "run_shell_command(node)", "write_file", "replace", "read_file"]


def _parse(helper: str, out: str) -> str:
    try:
        data = json.loads(out[out.index("{"):])
    except ValueError:
        return out.strip()[-2000:] or "No answer came back."
    if helper == "claude":
        if data.get("is_error"):
            return f"Claude reported a problem: {data.get('result') or data.get('subtype')}"
        return (data.get("result") or "").strip()
    if data.get("error"):
        msg = data["error"].get("message", str(data["error"])) if isinstance(data["error"], dict) else str(data["error"])
        return f"Gemini reported a problem: {msg}"
    return (data.get("response") or "").strip()


async def _run(helper: str, mode: str, prompt: str, cwd: Path, timeout: int) -> str:
    proc = await asyncio.create_subprocess_exec(*_command(helper, mode), cwd=str(cwd), stdin=asyncio.subprocess.PIPE,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"{HELPERS[helper]} took longer than {timeout // 60} minutes, so I stopped it."
    text = out.decode(errors="replace")
    if proc.returncode and not text.strip():
        e = err.decode(errors="replace").strip()
        if helper == "gemini" and re.search(r"auth|login|sign in|credential", e, re.I):
            return "Gemini isn't signed in yet. Open a terminal, run gemini, and sign in with Google once."
        return f"{HELPERS[helper]} failed: {e[-400:] or 'no details'}"
    return _parse(helper, text)


def speakable(text: str) -> str:
    """The first sentence or two, for the voice; the screen shows everything."""
    lead = next((line for line in text.strip().splitlines() if line.strip()), "")
    first = re.split(r"(?<=[.!?])\s", lead.strip())
    out = " ".join(first[:2])
    return out if len(out) <= 220 else out[:220].rsplit(" ", 1)[0] + "…"


@tool("Ask Claude or Gemini (a more powerful AI) a question and get its answer. Use when the user says 'ask Claude' "
      "or 'ask Gemini', or for hard questions about code. It can't run or change anything.",
      question="string: the question, with all needed detail", helper="string?: claude or gemini; default claude")
def ask_ai(question: str, helper: str = "claude") -> str:
    h = _helper(helper)
    folder = workspace() / "_questions"
    folder.mkdir(parents=True, exist_ok=True)
    answer = asyncio.run(_run(h, "ask", question, folder, ASK_TIMEOUT))
    return f"{HELPERS[h]} says: {answer}"


@tool("Have Claude or Gemini write code AND run it, then report the result (e.g. 'ask Claude to write a script that "
      "renames my photos and run it'). Needs the owner's admin voice. Runs in its own folder in JARVIS Workspace.",
      task="string: what to build and what to run, in detail", helper="string?: claude or gemini; default claude")
def ai_code_job(task: str, helper: str = "claude") -> dict:
    h = _helper(helper)
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")[:40] or "job"
    folder = workspace() / f"{datetime.now():%Y-%m-%d %H%M} {slug}"
    check(folder, deleting=False)
    return {"kind": "ai_code", "args": {"helper": h, "task": task, "folder": str(folder)},
            "summary": f"Let {HELPERS[h]} write and run code to {task.strip().rstrip('.')}, working only in "
                       f"{_show(folder)}"}


def start_job(args: dict) -> str:
    """Starts an approved coding job in the background; the result arrives later as an event."""
    helper, folder = args["helper"], Path(args["folder"])
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "TASK.txt").write_text(f"{args['task']}\n\nAsked {datetime.now():%Y-%m-%d %H:%M}, done by {HELPERS[helper]}.\n",
                                     encoding="utf-8")

    async def job():
        started = time.time()
        result = await _run(helper, "code", f"{args['task']}\n\n(The task is also in TASK.txt.)", folder, JOB_TIMEOUT)
        (folder / "RESULT.txt").write_text(result, encoding="utf-8")
        notify({"type": "job", "helper": HELPERS[helper], "task": args["task"], "folder": str(folder),
                "result": result, "say": f"{HELPERS[helper]} has finished. {speakable(result)}",
                "seconds": round(time.time() - started)})
        _running.pop(str(folder), None)

    _running[str(folder)] = asyncio.get_running_loop().create_task(job())
    return f"{HELPERS[helper]} is on it. I'll tell you when it's done."
