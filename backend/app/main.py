import asyncio
import json
import mimetypes
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import access, admin, brain, config, db, stt, tts
from .tools import info, memory, network, system, timers

mimetypes.add_type("application/manifest+json", ".webmanifest")
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


def _broadcast(event: dict) -> None:
    for q in list(_listeners):
        q.put_nowait(event)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from .tools import coders

    coders.notify = _broadcast
    db.conn()
    tasks = [asyncio.create_task(_reminder_loop()), asyncio.create_task(brain.warm_up()),
             asyncio.create_task(asyncio.to_thread(system.warm)), asyncio.create_task(asyncio.to_thread(stt.warm))]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="YOLO-JARVIS", lifespan=lifespan)

LOCKED_PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>J.A.R.V.I.S.</title><body style="background:#02060d;color:#d4f4ff;font:17px system-ui;display:grid;place-items:center;
min-height:100vh;margin:0;padding:16px;text-align:center"><div><h1 style="color:#6ce9ff;letter-spacing:.2em">J.A.R.V.I.S.</h1>
<p>This device needs the access link.</p><p style="color:#7ea8bf">On the PC running JARVIS, open Settings and use the
"Open on another device" link.</p></div>"""


@app.middleware("http")
async def guard(request: Request, call_next):
    # JARVIS can open apps and change the PC, so it refuses: hostnames that aren't an IP or localhost (DNS
    # rebinding), requests sent by other websites open in a browser, and other devices without the access key.
    host = request.headers.get("host", "")
    if not access.host_ok(host):
        return JSONResponse({"detail": "Refused: unknown host."}, status_code=403)
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != host:
        return JSONResponse({"detail": "Requests from other websites are refused."}, status_code=403)
    ctype = request.headers.get("content-type", "")
    audio_paths = ("/api/stt", "/api/admin/verify", "/api/admin/enroll/sample")
    wanted = ("audio/", "application/octet-stream") if request.url.path in audio_paths else ("application/json",)
    if request.method in ("POST", "PUT", "PATCH") and not ctype.startswith(wanted):
        return JSONResponse({"detail": "Wrong content type."}, status_code=415)
    # Public, harmless files: the certificate a phone installs before opening the link, and the home-screen icon
    # files, which browsers fetch without cookies.
    if request.url.path in ("/jarvis-ca.crt", "/icon.svg", "/manifest.webmanifest"):
        return await call_next(request)

    local = access.is_local(request.client.host if request.client else None)
    given = request.query_params.get("key")
    if not local and not access.check(request.cookies.get(access.COOKIE)) and not access.check(given):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "This device needs the access link."}, status_code=401)
        return HTMLResponse(LOCKED_PAGE, status_code=401)
    if not local and given and access.check(given):
        # Remember this device, and take the key out of the address bar and history.
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(access.COOKIE, given, max_age=60 * 60 * 24 * 365, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https")
        return resp
    response = await call_next(request)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


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
    news = await _cached("news", 900, info.get_news)
    return {
        "news": news or [],
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
    return {"ok": True, "ollama": bool(models), "model": model, "model_installed": model in models,
            "provider": brain.provider(), "cloud_model": config.GROQ_MODEL if config.GROQ_API_KEY else None}


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


@app.post("/api/stt")
async def speech_to_text(request: Request):
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "No audio.")
    if len(audio) > 8_000_000:
        raise HTTPException(413, "Recording too long.")
    try:
        text = await asyncio.to_thread(stt.transcribe, audio, db.setting("speech_lang"),
                                       request.headers.get("content-type", "audio/webm"))
    except Exception as e:
        raise HTTPException(500, f"Couldn't understand the recording: {e}")
    return {"text": text}


def _local(request: Request) -> bool:
    return access.is_local(request.client.host if request.client else None)


async def _audio(request: Request) -> bytes:
    audio = await request.body()
    if not audio or len(audio) > 4_000_000:
        raise HTTPException(400, "Send a short recording.")
    return audio


class EnrollBody(BaseModel):
    passphrase: str


class AdminRequestBody(BaseModel):
    kind: str
    args: dict = {}


@app.get("/api/admin/status")
async def admin_status(request: Request):
    return {**admin.status(), "can_enroll": _local(request)}


@app.post("/api/admin/enroll/start")
async def admin_enroll_start(body: EnrollBody, request: Request):
    # Only at the laptop itself: someone holding the phone link must not be able to enroll their own voice.
    if not _local(request):
        raise HTTPException(403, "Admin voice can only be set up on the laptop.")
    try:
        return admin.enroll_start(body.passphrase)
    except admin.Refused as e:
        raise HTTPException(400, str(e))


@app.post("/api/admin/enroll/sample")
async def admin_enroll_sample(request: Request):
    if not _local(request):
        raise HTTPException(403, "Admin voice can only be set up on the laptop.")
    audio = await _audio(request)
    try:
        return await asyncio.to_thread(admin.enroll_sample, audio)
    except admin.Refused as e:
        raise HTTPException(400, str(e))


@app.post("/api/admin/enroll/finish")
async def admin_enroll_finish(request: Request):
    if not _local(request):
        raise HTTPException(403, "Admin voice can only be set up on the laptop.")
    try:
        return admin.enroll_finish()
    except admin.Refused as e:
        raise HTTPException(400, str(e))


@app.post("/api/admin/remove")
async def admin_remove(request: Request):
    if not _local(request):
        raise HTTPException(403, "Admin voice can only be removed on the laptop.")
    return admin.remove()


@app.post("/api/admin/request")
async def admin_request(body: AdminRequestBody):
    """For buttons on the HUD (e.g. Delete on a file card): plans the change and asks for approval."""
    from .tools import TOOLS, files

    if body.kind not in files.PLANNERS:
        raise HTTPException(400, "Unknown action.")
    try:
        plan = await asyncio.to_thread(TOOLS[body.kind]["fn"], **body.args)
        return brain._approval(plan)
    except admin.Refused as e:
        raise HTTPException(400, str(e))
    except TypeError:
        raise HTTPException(400, "Bad request.")


@app.post("/api/admin/verify")
async def admin_verify(request: Request, id: str):
    from .tools import files

    audio = await _audio(request)
    ok, message, plan = await asyncio.to_thread(admin.verify, id, audio)
    if not ok:
        return {"ok": False, "done": "locked" in message or "expired" in message or "isn't set up" in message,
                "text": message}
    try:
        result = await files.execute(plan)
    except admin.Refused as e:
        result = f"I stopped: {e}"
    except Exception as e:
        result = f"That didn't work: {e}"
    db.add_history("assistant", f"Admin approved: {plan['summary']}. {result}", json.dumps(["admin"]))
    return {"ok": True, "done": True, "text": result}


@app.post("/api/admin/cancel")
async def admin_cancel(body: dict):
    admin.cancel(str(body.get("id", "")))
    return {"ok": True}


@app.get("/jarvis-ca.crt")
async def ca_cert():
    from . import certs

    if not certs.CA_CERT.exists():
        raise HTTPException(404, "Network mode is off.")
    return Response(certs.CA_CERT.read_bytes(), media_type="application/x-x509-ca-cert",
                    headers={"Content-Disposition": 'attachment; filename="jarvis-ca.crt"'})


@app.get("/api/lan")
async def lan(request: Request):
    """The access link, shown only on the PC itself (it is the key to everything)."""
    if not access.is_local(request.client.host if request.client else None):
        return {"enabled": config.LAN, "link": None}
    ip = access.lan_ip()
    return {"enabled": config.LAN, "link": access.link() if config.LAN else None,
            "ca_url": f"https://{ip}:{config.HTTPS_PORT}/jarvis-ca.crt" if config.LAN and ip else None}


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
