import asyncio
import json
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import brain, config, db, tts
from .tools import info, memory, network, system, timers

_listeners: set[asyncio.Queue] = set()
_cache: dict[str, tuple[float, object]] = {}


async def _cached(key: str, ttl: float, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    try:
        value = await asyncio.to_thread(fn)
    except Exception:
        value = hit[1] if hit else None
    _cache[key] = (time.time(), value)
    return value


async def _reminder_loop():
    while True:
        await asyncio.sleep(1)
        try:
            due = await asyncio.to_thread(timers.due_now)
        except Exception:
            continue
        for r in due:
            event = {"type": "reminder", "kind": r["kind"], "text": r["text"], "id": r["id"]}
            for q in list(_listeners):
                q.put_nowait(event)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.conn()
    tasks = [asyncio.create_task(_reminder_loop()), asyncio.create_task(brain.warm_up()),
             asyncio.create_task(asyncio.to_thread(system.warm))]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="YOLO-JARVIS", lifespan=lifespan)

ALLOWED_HOSTS = {f"127.0.0.1:{config.PORT}", f"localhost:{config.PORT}"}


@app.middleware("http")
async def local_only(request: Request, call_next):
    # JARVIS can open apps and change the PC, so refuse anything that isn't this PC's own HUD: other hostnames
    # (DNS rebinding) and requests sent by other websites open in the browser.
    if request.headers.get("host", "") not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "JARVIS only answers on localhost."}, status_code=403)
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] not in ALLOWED_HOSTS:
        return JSONResponse({"detail": "Requests from other websites are refused."}, status_code=403)
    if request.method in ("POST", "PUT", "PATCH") and not request.headers.get("content-type", "").startswith("application/json"):
        return JSONResponse({"detail": "Send JSON."}, status_code=415)
    return await call_next(request)


class ChatBody(BaseModel):
    text: str


class TtsBody(BaseModel):
    text: str
    voice: str | None = None


class NoteBody(BaseModel):
    text: str


class PathBody(BaseModel):
    path: str


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


@app.post("/api/chat")
async def chat(body: ChatBody):
    if len(body.text) > 2000:
        raise HTTPException(400, "That's too long for me, keep it under 2000 characters.")

    async def stream():
        async for event in brain.handle(body.text):
            yield _sse(event)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/tts")
async def speak(body: TtsBody):
    try:
        audio = await tts.synth(body.text, body.voice or db.setting("voice"))
    except Exception as e:
        raise HTTPException(502, f"Voice service unavailable: {e}")
    if not audio:
        raise HTTPException(502, "Voice service returned no audio.")
    return Response(audio, media_type="audio/mpeg")


@app.get("/api/events")
async def events(request: Request):
    q: asyncio.Queue = asyncio.Queue()
    _listeners.add(q)

    async def stream():
        try:
            yield _sse({"type": "hello"})
            while not await request.is_disconnected():
                try:
                    yield _sse(await asyncio.wait_for(q.get(), timeout=15))
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            _listeners.discard(q)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/status")
async def status():
    weather = await _cached("weather", 600, info.get_weather)
    net = await _cached("network", 60, network.summary)
    return {
        "system": system.stats(),
        "weather": weather,
        "network": net,
        "reminders": [{"id": r["id"], "text": r["text"], "kind": r["kind"], "due": r["due"]} for r in timers.pending()],
        "notes": memory.notes(),
        "now": time.time(),
    }


@app.get("/api/health")
async def health():
    models = await brain.models()
    model = db.setting("model")
    return {"ok": True, "ollama": bool(models), "model": model, "model_installed": model in models}


@app.get("/api/settings")
async def get_settings():
    return {"settings": {k: v for k, v in db.settings().items() if k in config.DEFAULTS},
            "voices": tts.VOICES, "models": await brain.models()}


@app.post("/api/settings")
async def set_settings(values: dict):
    _cache.pop("weather", None)
    saved = db.save_settings(values)
    asyncio.create_task(brain.warm_up())
    return {"settings": {k: v for k, v in saved.items() if k in config.DEFAULTS}}


@app.get("/api/history")
async def history():
    return [{"role": h["role"], "content": h["content"], "tools": json.loads(h["tools"]) if h["tools"] else [],
             "ts": h["ts"]} for h in db.recent_history(40)]


@app.post("/api/new-session")
async def new_session():
    db.new_session()
    return {"ok": True}


@app.get("/api/memory")
async def get_memory():
    return memory.facts()


@app.delete("/api/memory/{fact_id}")
async def delete_memory(fact_id: int):
    db.x("DELETE FROM memories WHERE id=?", fact_id)
    return memory.facts()


@app.post("/api/notes")
async def add_note(body: NoteBody):
    memory.add_note(body.text)
    return memory.notes()


@app.post("/api/notes/{note_id}/done")
async def done_note(note_id: int):
    db.x("UPDATE notes SET done=1 WHERE id=?", note_id)
    return memory.notes()


@app.delete("/api/reminders/{rem_id}")
async def delete_reminder(rem_id: int):
    db.x("UPDATE reminders SET fired=1 WHERE id=?", rem_id)
    return {"ok": True}


@app.post("/api/open-file")
async def open_file(body: PathBody):
    return {"result": await asyncio.to_thread(system.open_file, body.path)}


app.mount("/", StaticFiles(directory=config.FRONTEND, html=True), name="hud")
