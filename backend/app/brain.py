import asyncio
import json
import re
import time
from datetime import datetime
from typing import AsyncIterator

import httpx

from . import config, db, intents
from .tools import TOOLS, memory, run, schemas

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
}

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


def provider() -> str:
    return "groq" if config.GROQ_API_KEY and time.time() >= _groq_paused_until else "local"


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


async def _groq(messages: list[dict], use_tools: bool) -> dict:
    body = {"model": config.GROQ_MODEL, "messages": _to_openai(messages), "temperature": 0.4, "max_tokens": 400}
    if use_tools:
        body["tools"] = schemas()
    r = await _http.post(f"{config.GROQ_URL}/chat/completions", json=body, timeout=30,
                         headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"})
    r.raise_for_status()
    msg = r.json()["choices"][0]["message"]
    calls = [{"id": c["id"], "function": {"name": c["function"]["name"], "arguments": _args(c["function"].get("arguments"))}}
             for c in msg.get("tool_calls") or []]
    return {"content": msg.get("content") or "", "tool_calls": calls}


async def _llm(messages: list[dict], use_tools: bool) -> dict:
    """Groq when a key is set and it's answering; the local model otherwise, or whenever Groq fails."""
    global _groq_paused_until
    if provider() == "groq":
        try:
            return await _groq(messages, use_tools)
        except httpx.HTTPStatusError as e:
            code = e.response.status_code
            if code == 429:  # free-tier limit: use the local model until it resets
                wait = e.response.headers.get("retry-after", "60")
                _groq_paused_until = time.time() + min(float(wait) if wait.replace(".", "").isdigit() else 60, 3600)
            elif code in (401, 403):
                print("[brain] Groq refused the API key; using the local model. Check GROQ_API_KEY in backend/.env.")
                _groq_paused_until = time.time() + 3600
            print(f"[brain] Groq error {code}: {e.response.text[:200]}; answering locally")
        except (httpx.HTTPError, KeyError, ValueError) as e:
            print(f"[brain] Groq unavailable ({e}); answering locally")
            _groq_paused_until = time.time() + 60
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
        db.add_history("assistant", quick["text"], json.dumps([quick["tool"]]))
        yield {"type": "answer", "text": quick["text"], "cards": quick["cards"], "tools": [quick["tool"]], "fast": True}
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

    used, cards, answer = [], {}, ""
    try:
        for round_no in range(config.MAX_TOOL_ROUNDS + 1):
            msg = await _llm(messages, use_tools=round_no < config.MAX_TOOL_ROUNDS)
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
                result = await asyncio.to_thread(run, name, args) if name in TOOLS else f"Unknown tool {name}."
                used.append(name)
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
    yield {"type": "answer", "text": answer, "cards": cards, "tools": used, "fast": False}


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
