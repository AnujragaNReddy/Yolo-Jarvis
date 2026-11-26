"""Always-on "Hey JARVIS" on this PC, with no browser needed.

A background thread listens to the laptop microphone with openWakeWord's "hey_jarvis" model (small, offline,
CPU only). On the wake word it records the command until you stop talking, transcribes it, runs it through the
same brain as the HUD, and speaks the answer on the PC speakers. Admin approvals are spoken too: JARVIS asks for
the passphrase and checks it with the same voiceprint as the HUD. An open HUD tab shows each step.

Turn it off with JARVIS_WAKE=false in backend/.env. JARVIS_WAKE_THRESHOLD (default 0.4) sets how sure the model
must be; raise it if JARVIS wakes by mistake, lower it if he misses you.
"""

import asyncio
import io
import os
import re
import subprocess
import threading
import time
import wave

import numpy as np

from . import config, db, stt, tts

RATE = 16000
BLOCK = 1280  # 80 ms, the frame size openWakeWord expects
MODEL_DIR = config.DB_PATH.parent / "models" / "wakeword"
THRESHOLD = float(os.getenv("JARVIS_WAKE_THRESHOLD", "0.4"))
ENABLED = os.getenv("JARVIS_WAKE", "true").lower() != "false"
STOP = re.compile(r"^(stop|quiet|silence|shut up|be quiet|cancel|never ?mind|nothing|that's all|that is all)[.!]?$", re.I)
LEAD = re.compile(r"^\W*(?:hey|hi|ok|okay)?\W*(?:jarvis|jervis|jarvas|travis)\W*", re.I)

_state = {"running": False, "error": ""}
_loop: asyncio.AbstractEventLoop | None = None
notify = lambda event: None  # set by main: shows each step in an open HUD


def active() -> bool:
    return _state["running"]


def status() -> dict:
    return dict(_state)


def start(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    if not ENABLED or _state["running"]:
        return
    _loop = loop
    _state["running"] = True  # while the model loads too, so a HUD opened now doesn't listen as well
    threading.Thread(target=_run, name="wake-word", daemon=True).start()


# ------------------------------------------------------------------ audio helpers

def _rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))


def _wav(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(samples.astype(np.int16).tobytes())
    return buf.getvalue()


def _beep(freq: float = 880, ms: int = 110) -> None:
    import sounddevice as sd

    t = np.arange(int(24000 * ms / 1000)) / 24000
    tone = 0.25 * np.sin(2 * np.pi * freq * t) * np.minimum(1, np.minimum(t, t[::-1]) * 60)
    sd.play(tone.astype(np.float32), 24000)
    sd.wait()


def _say(text: str) -> None:
    """Speaks on the PC speakers: the HUD's neural voice, or Windows' own voice when offline."""
    import sounddevice as sd

    text = tts.speakable(text)
    if not text:
        return
    try:
        mp3 = asyncio.run_coroutine_threadsafe(tts.synth(text, db.setting("voice")), _loop).result(30)
        from faster_whisper.audio import decode_audio

        samples = decode_audio(io.BytesIO(mp3), sampling_rate=24000)
        sd.play(samples, 24000)
        sd.wait()
    except Exception as e:
        print(f"[wake] neural voice unavailable ({e}); using the Windows voice")
        script = ("Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                  "$s.Speak([Console]::In.ReadToEnd())")
        subprocess.run(["powershell", "-NoProfile", "-Command", script], input=text, text=True, timeout=60,
                       creationflags=subprocess.CREATE_NO_WINDOW)


class Mic:
    """The microphone as a queue of 80 ms blocks, so recording a command never misses the start."""

    def __init__(self):
        import queue

        import sounddevice as sd

        self.q: queue.Queue = queue.Queue()
        self.stream = sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK,
                                     callback=lambda data, *_: self.q.put(data[:, 0].copy()))
        self.stream.start()

    def read(self) -> np.ndarray:
        return self.q.get()

    def flush(self, settle_s: float = 0.0) -> None:
        """Drops what's queued, then settle_s more: the echo of JARVIS's own beep or voice still on its way in."""
        while not self.q.empty():
            self.q.get_nowait()
        for _ in range(int(settle_s * RATE / BLOCK)):
            self.q.get()

    def record(self, floor: float, wait_s: float = 4.0, max_s: float = 12.0, quiet_s: float = 0.9) -> np.ndarray | None:
        """Records one utterance: waits up to wait_s for speech, stops after quiet_s of silence. None if silent."""
        level = max(floor * 2.5, 120.0)
        got, spoke, loud_run, quiet, t0 = [], False, 0, 0.0, time.time()
        while True:
            block = self.read()
            got.append(block)
            loud = _rms(block) > level
            loud_run = loud_run + 1 if loud else 0
            if loud_run >= 3:  # 240 ms of sound is speech; a click or knock isn't
                spoke = True
            if loud:
                quiet = 0.0
            elif spoke:
                quiet += BLOCK / RATE
            elapsed = time.time() - t0
            if spoke and quiet >= quiet_s or elapsed >= max_s:
                break
            if not spoke and elapsed >= wait_s:
                return None
        return np.concatenate(got)


# ------------------------------------------------------------------ the listener

def _model():
    from openwakeword.model import Model
    from openwakeword.utils import download_models

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if not list(MODEL_DIR.glob("hey_jarvis*.onnx")):
        download_models(model_names=["hey_jarvis"], target_directory=str(MODEL_DIR))
        for f in MODEL_DIR.glob("*.tflite"):
            f.unlink(missing_ok=True)
    return Model(wakeword_models=[str(next(MODEL_DIR.glob("hey_jarvis*.onnx")))], inference_framework="onnx",
                 melspec_model_path=str(MODEL_DIR / "melspectrogram.onnx"),
                 embedding_model_path=str(MODEL_DIR / "embedding_model.onnx"))


def _run() -> None:
    try:
        model = _model()
        mic = Mic()
    except Exception as e:
        _state.update(running=False, error=str(e))
        print(f"[wake] 'Hey JARVIS' is off: {e}")
        return
    _state.update(running=True, error="")
    print('[wake] Listening for "Hey JARVIS" on this PC.', flush=True)
    floor = 60.0
    while True:
        try:
            block = mic.read()
            level = _rms(block)
            if level < floor * 2:  # background noise level, learnt from quiet moments only
                floor = max(20.0, 0.98 * floor + 0.02 * level)
            score = max(model.predict(block).values())
            if score >= 0.25:
                print(f"[wake] score {score:.2f}{'' if score >= THRESHOLD else ' (below threshold)'}", flush=True)
            if score < THRESHOLD:
                continue
            _conversation(mic, floor)
        except Exception as e:
            print(f"[wake] {e}")
            time.sleep(1)
        # Ignore everything heard while JARVIS was talking, so his own voice can't wake him.
        model.reset()
        mic.flush()


def _conversation(mic: Mic, floor: float) -> None:
    notify({"type": "voice", "stage": "wake"})
    _beep(660)
    mic.flush(0.3)
    audio = mic.record(floor, wait_s=2.5)
    if audio is None:
        _say(f"Yes, {_you()}?")
        mic.flush(0.3)
        audio = mic.record(floor, wait_s=5)
    if audio is None:
        print("[wake] woke, but no command was heard", flush=True)
        notify({"type": "voice", "stage": "done"})
        return
    raw = stt.transcribe(_wav(audio), db.setting("speech_lang") or "en", "audio/wav")
    print(f"[wake] transcript ({len(audio) / RATE:.1f}s of audio): {raw!r}", flush=True)
    text = LEAD.sub("", raw).strip(" ,.")
    t_heard = time.time()
    if not text or STOP.match(text):
        notify({"type": "voice", "stage": "done"})
        return
    print(f"[wake] heard: {text}", flush=True)
    notify({"type": "voice", "stage": "heard", "text": text})
    _beep(990, 70)  # got it
    job = asyncio.run_coroutine_threadsafe(_ask(text), _loop)
    try:
        answer = job.result(3)
    except TimeoutError:
        # The brain needs a while (quick commands answer instantly; questions for the AI take longer on this CPU).
        _say(f"One moment, {_you()}.")
        answer = job.result(300)
    print(f"[wake] answered in {time.time() - t_heard:.1f}s", flush=True)
    notify({"type": "voice", "stage": "answer", "text": answer["text"], "cards": answer.get("cards") or {},
            "tools": answer.get("tools") or []})
    _say(answer["text"])
    if answer.get("admin"):
        _approve(mic, floor, answer["admin"]["id"])
    notify({"type": "voice", "stage": "done"})


async def _ask(text: str) -> dict:
    from . import brain

    answer = {"text": "No reply came back."}
    async for ev in brain.handle(text):
        if ev["type"] == "tool":
            notify({"type": "voice", "stage": "tool", "label": ev.get("label") or ev.get("name")})
        elif ev["type"] == "answer":
            answer = ev
    return answer


def _approve(mic: Mic, floor: float, cid: str) -> None:
    """The passphrase, spoken: the same phrase-and-voiceprint check as the HUD's approval card."""
    import json

    from . import admin
    from .tools import files

    for _ in range(3):
        mic.flush()
        audio = mic.record(floor, wait_s=8, quiet_s=1.2)
        if audio is None:
            admin.cancel(cid)
            _say("Cancelled.")
            return
        ok, message, plan = admin.verify(cid, _wav(audio))
        if ok:
            try:
                result = asyncio.run_coroutine_threadsafe(files.execute(plan), _loop).result(300)
            except Exception as e:
                result = f"That didn't work: {e}"
            db.add_history("assistant", f"Admin approved: {plan['summary']}. {result}", json.dumps(["admin"]))
            notify({"type": "voice", "stage": "answer", "text": result, "cards": {}, "tools": ["admin"]})
            _say(result)
            return
        notify({"type": "voice", "stage": "answer", "text": message, "cards": {}, "tools": ["admin"]})
        _say(message)
        if "locked" in message or "expired" in message or "isn't set up" in message:
            return


def _you() -> str:
    return db.setting("honorific") or db.setting("user_name") or "sir"
