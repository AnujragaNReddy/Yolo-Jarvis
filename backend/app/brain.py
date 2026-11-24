import asyncio
import inspect
import json
import re
import time
from datetime import datetime
from typing import AsyncIterator

import httpx

from . import admin, config, db, intents
from .tools import TOOLS, files, memory, run, schemas

LABELS = {
    "get_time": "Checking the clock", "get_weather": "Reading the weather", "web_search": "Searching the web",
    "wikipedia": "Consulting Wikipedia", "get_news": "Pulling headlines", "calculate": "Calculating",
    "open_app": "Launching", "close_app": "Closing", "open_website": "Opening browser",
    "search_in_browser": "Searching Google", "play_youtube": "Finding it on YouTube", "volume": "Adjusting volume",
    "media": "Media control", "system_status": "Running diagnostics", "lock_screen": "Locking",
    "find_files": "Searching your files", "open_file": "Opening file", "remember": "Storing to memory",
    "forget": "Erasing memory", "add_note": "Updating your list", "list_notes": "Reading your list",
    "complete_note": "Updating your list", "set_timer": "Starting timer", "set_reminder": "Scheduling reminder",
    "list_reminders": "Checking schedule", "cancel_reminder": "Cancelling", "network_devices": "Scanning network",
    "delete_path": "Preparing deletion", "create_folder": "Preparing folder", "create_file": "Preparing file",
    "write_script": "Preparing script", "uninstall_app": "Finding the app", "brightness": "Adjusting brightness",
    "screenshot": "Taking a screenshot", "ask_ai": "Consulting another AI", "ai_code_job": "Preparing a coding job",
    "phone_open_app": "Opening on your phone", "phone_close_app": "Closing on your phone", "phone_volume": "Phone volume",
    "phone_media": "Phone media", "phone_status": "Checking your phone", "phone_screenshot": "Capturing your phone",
    "phone_call": "Opening the dialer", "phone_message": "Writing the message", "phone_open": "Opening on your phone",
    "phone_type": "Typing on your phone", "phone_screen": "Phone screen", "phone_uninstall_app": "Finding the phone app",
}


def _approval(plan: dict) -> dict:
    """Turns a planned change into an admin request: the answer says exactly what will happen and asks for the
    owner's passphrase. Raises admin.Refused when admin voice isn't set up."""
    ch = admin.request(plan)
    return {"text": f"{plan['summary']}. Say your passphrase to approve.", "admin": ch}

# Tool results the HUD shows as cards alongside the spoken answer.
CARD_TOOLS = {"get_weather": "weather", "get_news": "news", "find_files": "files", "system_status": "system",
              "network_devices": "network", "web_search": "search"}

_http = httpx.AsyncClient(timeout=180)


def system_prompt() -> str:
    # Kept identical between requests (no clock, no memories) so Ollama reuses its cache of this prompt and the
    # tool list; re-reading ~1,900 tokens on a CPU takes about 25 seconds. Changing details go in context_note().
    s = db.settings()
    name = s.get("user_name") or "the user"
    return f"""You are J.A.R.V.I.S., {name}'s personal AI assistant on their Windows PC, modelled on Tony Stark's \
JARVIS: calm, precise, loyal and quietly witty, with dry British understatement. Call the user "{s.get('honorific') or 'sir'}".
You have live tools and you MUST use them. Never say you lack access, real-time data or the ability to check: call the tool instead.
- weather, temperature, hot, cold, rain, umbrella -> get_weather (leave city empty for the user's own location)
- news or headlines -> get_news
- current events, sports results, prices, who currently holds a job or title, recent news about people or \
companies -> web_search
- timeless general knowledge (capitals, science, history, geography, definitions, how things work) -> answer \
directly from your own knowledge, without a tool
- background on a person, place or thing -> wikipedia
- any arithmetic or percentages -> calculate
- finding documents, photos or files on the PC -> find_files, then open_file with a path it returned
- opening apps, sites, music, timers, reminders, lists, volume, the home network -> the matching tool
- anything on the user's phone ("on my phone"), calls and messages -> the phone_ tools; without "phone", apps, \
volume and media mean this PC
- "ask Claude" / "ask Gemini", or a hard coding question -> ask_ai; writing code AND running it -> ai_code_job
- deleting a file or folder -> find_files first, then delete_path with the exact path it returned
- creating files or folders, writing a script or program, uninstalling an app -> create_file, create_folder, \
write_script, uninstall_app. These are only carried out after the owner approves by voice, so never say they are done.
- the user TELLS you a new lasting fact about themselves (a statement, not a question) -> remember
Answer jokes, opinions and chat yourself; answer questions about the user from the "Known about the user" context.
Search results mix old and new pages: for "latest" or "most recent" questions, use the result with the newest date, \
and remember the current date is in the context note.
Never invent a tool result. If a tool fails or finds nothing, say so in a few words.
If no tool can do what the user asks (calls, messages, emails, payments, clicking inside apps), say plainly that \
you can't do that yet; never use an unrelated tool instead.
Replies are spoken aloud: one to three short sentences of plain speech, no markdown, lists, emojis or links."""


def context_note() -> str:
    s = db.settings()
    facts = memory.facts()[-40:]
    known = "; ".join(f["fact"] for f in facts) or "nothing yet"
    return (f"[Context, not from the user: it is {datetime.now().strftime('%A %d %B %Y, %I:%M %p')}. "
            f"City: {s.get('city') or 'unknown, get_weather finds it'}. Known about the user: {known}]")


REFUSAL = re.compile(r"(don't|do not|can't|cannot|unable to) (have )?(direct )?(access|real-time|browse|retrieve)"
                     r"|(as of|since) my (last|knowledge|training)|my (knowledge|training) (cutoff|data)", re.I)
PROMISE = re.compile(r"^(sure[,!.]? )?(let me|i'll|i will) (check|look|fetch|find|search)", re.I)

THINKING_FAMILIES = ("qwen3", "deepseek-r1", "magistral", "gpt-oss")


def _keep_alive() -> str:
    # As the main brain, stay loaded all day (reloading from disk takes ~30 s). As Groq's backup, free the
    # ~2.3 GB of RAM soon after use.
    return "10m" if config.GROQ_API_KEY else "24h"


async def _ollama(messages: list[dict], use_tools: bool) -> dict:
    model = db.setting("model")
    body = {"model": model, "messages": messages, "stream": False, "keep_alive": _keep_alive(),
            "options": {"temperature": 0.4, "num_ctx": 6144, "num_predict": 220}}
    if model.startswith(THINKING_FAMILIES):
        body["think"] = False  # spoken replies need speed more than a long hidden reasoning pass
    if use_tools:
        body["tools"] = schemas()
    r = await _http.post(f"{config.OLLAMA_URL}/api/chat", json=body)
    if r.status_code == 404:
        raise RuntimeError(f"The model {db.setting('model')} isn't installed. Run: ollama pull {db.setting('model')}")
    r.raise_for_status()
    return r.json()["message"]


_groq_paused_until = 0.0
# Each Groq model has its own per-minute budget (8,000 tokens on the free plan), so when one is used up the next
# one answers instead of the slow local model. Models the account can't use are dropped.
_model_paused: dict[str, float] = {}
_model_gone: set[str] = set()


def _groq_models() -> list[str]:
    return [m for m in config.GROQ_MODELS if m not in _model_gone]


def _ready_models() -> list[str]:
    now = time.time()
    return [m for m in _groq_models() if _model_paused.get(m, 0) <= now]


def provider() -> str:
    return "groq" if config.GROQ_API_KEY and time.time() >= _groq_paused_until and _ready_models() else "local"


# Tools sent to the cloud model: the core set plus groups the question mentions. All 48 descriptions are ~2,000
# tokens, a quarter of the per-minute budget, on every call.
CORE_TOOLS = {"web_search", "wikipedia", "get_weather", "calculate", "remember", "get_news"}
TOOL_GROUPS = [
    (r"\b(phone|mobile|android|call|dial|text|sms|message|whatsapp)\b", "phone_"),
    (r"\b(delete|remove|trash|create|make|new|folder|file|document|script|program|code|uninstall|write)\b",
     {"find_files", "open_file", "delete_path", "create_folder", "create_file", "write_script", "uninstall_app"}),
    (r"\b(claude|gemini|chatgpt|gpt|ai|code|script|program|run)\b", {"ask_ai", "ai_code_job"}),
    (r"\b(open|close|launch|start|quit|app|website|site|play|youtube|song|music|video|pause|next|volume|mute|louder|quieter)\b",
     {"open_app", "close_app", "open_website", "search_in_browser", "play_youtube", "media", "volume"}),
    (r"\b(timer|remind|reminder|alarm|minutes?|hours?|tomorrow|tonight|at \d)\b",
     {"set_timer", "set_reminder", "list_reminders", "cancel_reminder"}),
    (r"\b(list|todo|to-do|note|notes|shopping|tick|done)\b", {"add_note", "list_notes", "complete_note"}),
    (r"\b(remember|forget|know about me|my name|favou?rite)\b", {"forget"}),
    (r"\b(cpu|memory|ram|disk|battery|system|pc|laptop|computer|screen|brightness|screenshot|lock)\b",
     {"system_status", "brightness", "screenshot", "lock_screen"}),
    (r"\b(wifi|wi-fi|network|devices?|router|online)\b", {"network_devices"}),
    (r"\b(find|search|where|my files?|photos?|pictures?|resume)\b", {"find_files", "open_file"}),
    (r"\b(time|date|day|today)\b", {"get_time"}),
]


def tools_for(text: str) -> list[dict]:
    t = text.lower()
    names = set(CORE_TOOLS)
    for pattern, group in TOOL_GROUPS:
        if re.search(pattern, t):
            if isinstance(group, str):
                names |= {n for n in TOOLS if n.startswith(group)}
            else:
                names |= group
    return [TOOLS[n]["schema"] for n in TOOLS if n in names]


def _to_openai(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or None, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {
                    "name": c["function"]["name"], "arguments": json.dumps(c["function"].get("arguments") or {})}}
                for c in m["tool_calls"]]})
        elif m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        else:
            out.append({"role": m["role"], "content": m["content"]})
    return out


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def _groq_body(model: str, messages: list[dict], max_tokens: int, temperature: float) -> dict:
    body = {"model": model, "messages": _to_openai(messages), "temperature": temperature, "max_tokens": max_tokens}
    if model.startswith("openai/gpt-oss"):
        body["reasoning_effort"] = "low"  # quick spoken answers, not long hidden reasoning
    elif model.startswith("qwen/qwen3"):
        body["reasoning_format"] = "hidden"
    return body


async def _groq_post(body_for, timeout: int = 30) -> dict:
    """Tries each ready Groq model in turn. Raises the last error if none answers."""
    global _groq_paused_until
    last: Exception | None = None
    for model in _ready_models():
        try:
            r = await _http.post(f"{config.GROQ_URL}/chat/completions", json=body_for(model), timeout=timeout,
                                 headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"})
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            last = e
            code = e.response.status_code
            if code == 429:
                wait = e.response.headers.get("retry-after", "30")
                _model_paused[model] = time.time() + min(float(wait) if wait.replace(".", "").isdigit() else 30, 3600)
                print(f"[brain] {model} is at its free limit; trying the next model")
            elif code == 404 or "model_not_found" in e.response.text or "decommissioned" in e.response.text:
                _model_gone.add(model)
                print(f"[brain] {model} isn't available to this Groq account; dropping it")
            elif code in (401, 403):
                print("[brain] Groq refused the API key; using the local model. Check GROQ_API_KEY in backend/.env.")
                _groq_paused_until = time.time() + 3600
                raise
            elif code == 400 and "tool" in e.response.text.lower():
                raise  # a bad tool call from this model; the caller retries without tools
            else:
                print(f"[brain] Groq error {code} on {model}: {e.response.text[:160]}")
        except (httpx.HTTPError, KeyError, ValueError) as e:
            last = e
            print(f"[brain] Groq unreachable ({e}); answering locally")
            _groq_paused_until = time.time() + 30
            break
    raise last or RuntimeError("No Groq model available")


async def _groq(messages: list[dict], tools: list[dict] | None) -> dict:
    def body_for(model: str) -> dict:
        body = _groq_body(model, messages, 400, 0.4)
        if tools:
            body["tools"] = tools
        return body

    data = await _groq_post(body_for)
    msg = data["choices"][0]["message"]
    calls = [{"id": c["id"], "function": {"name": c["function"]["name"], "arguments": _args(c["function"].get("arguments"))}}
             for c in msg.get("tool_calls") or []]
    return {"content": msg.get("content") or "", "tool_calls": calls}


async def _llm(messages: list[dict], use_tools: bool, tools: list[dict] | None = None) -> dict:
    """Groq when a key is set and a model is free; the local model otherwise, or whenever Groq fails."""
    if provider() == "groq":
        try:
            return await _groq(messages, (tools or schemas()) if use_tools else None)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 400 and use_tools:
                try:  # the model fumbled a tool call; ask again without tools
                    return await _groq(messages, None)
                except Exception:
                    pass
        except Exception:
            pass
    return await _ollama(messages, use_tools)


def _card(name: str, result: str) -> tuple[str, object] | None:
    key = CARD_TOOLS.get(name)
    if not key:
        return None
    try:
        return key, json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return (key, result) if key == "search" else None


def _tidy(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    text = re.sub(r"\*\*|__|`|^#+\s*", "", text, flags=re.M)
    return text.strip()


async def handle(text: str) -> AsyncIterator[dict]:
    text = text.strip()
    if not text:
        return
    history = db.recent_history(config.HISTORY_TURNS)
    db.add_history("user", text)

    try:
        quick = await asyncio.to_thread(intents.match, text)
    except Exception as e:
        quick = {"text": f"That didn't work: {e}", "tool": "error", "cards": {}}
    if quick:
        yield {"type": "tool", "name": quick["tool"], "label": LABELS.get(quick["tool"], "Working")}
        approval = None
        if quick.get("plan"):
            try:
                approval = _approval(quick["plan"])
                quick["text"] = approval["text"]
            except admin.Refused as e:
                quick["text"] = str(e)
        db.add_history("assistant", quick["text"], json.dumps([quick["tool"]]))
        yield {"type": "answer", "text": quick["text"], "cards": quick["cards"], "tools": [quick["tool"]], "fast": True,
               "admin": approval["admin"] if approval else None}
        return

    yield {"type": "thinking"}
    # Earlier turns go in as a short recap, not replayed messages: a 3B model shown its own past plain-text
    # answers copies them (stale weather, wrong maths) instead of calling tools.
    asked = [h["content"][:120] for h in history if h["role"] == "user"][-3:]
    replies = [h["content"] for h in history if h["role"] == "assistant"]
    recap = (f"\n[Earlier the user asked: {'; '.join(asked)}. Your last reply: {replies[-1][:220] if replies else ''}. "
             "Use this only to understand follow-ups; look facts up again.]") if asked else ""
    messages = [{"role": "system", "content": system_prompt()},
                {"role": "user", "content": f"{text}\n\n{context_note()}{recap}"}]

    used, cards, answer, approval = [], {}, "", None
    cloud_tools = tools_for(" ".join([text, *asked[-1:]]))
    try:
        for round_no in range(config.MAX_TOOL_ROUNDS + 1):
            if approval:
                break
            msg = await _llm(messages, use_tools=round_no < config.MAX_TOOL_ROUNDS, tools=cloud_tools)
            calls = msg.get("tool_calls") or []
            content = _tidy(msg.get("content") or "")
            if not calls and not used and (not content or REFUSAL.search(content)
                                            or (PROMISE.search(content) and len(content) < 140)):
                # Small models sometimes claim they can't look something up; look it up for them.
                calls = [{"function": {"name": "web_search", "arguments": {"query": text}}}]
            if not calls:
                answer = content
                break
            for i, call in enumerate(calls):
                call.setdefault("id", f"call_{round_no}_{i}")
            messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
            for call in calls:
                fn = call.get("function", {})
                name, args = fn.get("name", ""), fn.get("arguments") or {}
                yield {"type": "tool", "name": name, "label": LABELS.get(name, name), "args": args}
                used.append(name)
                if name in files.PLANNERS:
                    # Changes to the PC are only planned here; the owner approves the exact plan by voice.
                    try:
                        fn = TOOLS[name]["fn"]
                        accepted = inspect.signature(fn).parameters
                        plan = await asyncio.to_thread(fn, **{k: v for k, v in args.items() if k in accepted and v is not None})
                        approval = _approval(plan)
                        answer = approval["text"]
                        break
                    except admin.Refused as e:
                        result = str(e)
                    except TypeError as e:
                        result = f"Bad arguments for {name}: {e}"
                    messages.append({"role": "tool", "content": result, "tool_name": name, "tool_call_id": call["id"]})
                    continue
                result = await asyncio.to_thread(run, name, args) if name in TOOLS else f"Unknown tool {name}."
                card = _card(name, result)
                if card:
                    cards[card[0]] = card[1]
                # The CPU reads ~75 tokens/s, so every extra 1,000 characters here costs ~3 seconds.
                messages.append({"role": "tool", "content": result[:1800], "tool_name": name, "tool_call_id": call["id"]})
            messages.append({"role": "system", "content": "If you have what you need, answer now in one or two short "
                             "spoken sentences with only the key facts; the screen shows the details."})
    except httpx.ConnectError:
        answer = "I can't reach my language model. Is Ollama running?"
    except Exception as e:
        answer = f"Something went wrong in my head: {e}"

    answer = answer or f"I'm afraid I have nothing useful to say to that, {intents.you()}."
    db.add_history("assistant", answer, json.dumps(used) if used else None)
    yield {"type": "answer", "text": answer, "cards": cards, "tools": used, "fast": False,
           "admin": approval["admin"] if approval else None}


async def write_code(task: str, language: str) -> str:
    """The script itself, for an approved write_script. Not run, only saved and opened for review."""
    messages = [{"role": "system", "content": (
        f"You are an expert {language} programmer. Write one complete, working {language} file for the task. "
        "Start with a short comment saying what it does and how to run it. Keep it simple and safe: no deleting "
        "files, no system changes, no network calls unless the task needs them. Reply with only the code, no "
        "explanations and no markdown fences.")},
        {"role": "user", "content": task}]
    if provider() == "groq":
        try:
            data = await _groq_post(lambda m: _groq_body(m, messages, 3000, 0.2), timeout=60)
            code = data["choices"][0]["message"]["content"]
        except Exception:
            code = None
    else:
        code = None
    if code is None:
        r = await _http.post(f"{config.OLLAMA_URL}/api/chat", timeout=600, json={
            "model": db.setting("model"), "messages": messages, "stream": False, "keep_alive": _keep_alive(),
            "options": {"temperature": 0.2, "num_ctx": 6144, "num_predict": 1800}})
        r.raise_for_status()
        code = r.json()["message"]["content"]
    code = re.sub(r"^\s*```[\w+#-]*\s*\n|\n?```\s*$", "", code.strip())
    return code.rstrip() + "\n"


async def warm_up() -> None:
    """Load the model and have it read the instructions and tool list once, so Ollama's prompt cache
    already holds them and the first real question is fast."""
    if config.GROQ_API_KEY:
        return  # Groq answers; the local model loads only if it's ever needed as a backup
    model = db.setting("model")
    body = {"model": model, "stream": False, "keep_alive": _keep_alive(), "tools": schemas(),
            "options": {"num_ctx": 6144, "num_predict": 1},
            "messages": [{"role": "system", "content": system_prompt()}, {"role": "user", "content": "Hello"}]}
    if model.startswith(THINKING_FAMILIES):
        body["think"] = False
    try:
        await _http.post(f"{config.OLLAMA_URL}/api/chat", json=body, timeout=300)
    except httpx.HTTPError:
        pass


async def models() -> list[str]:
    try:
        r = await _http.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        return [m["name"] for m in r.json().get("models", [])]
    except (httpx.HTTPError, ValueError):
        return []
