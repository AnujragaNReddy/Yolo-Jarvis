"""Speech to text on this PC (faster-whisper), for phones and browsers without good speech recognition."""

import io
import os
import re
import threading

from . import config

MODEL = os.getenv("STT_MODEL", "base.en")
MODELS_DIR = config.DB_PATH.parent / "models"
_model = None
_lock = threading.Lock()

# What Whisper "hears" in silence or noise.
PHANTOMS = re.compile(r"^(thanks? (you|for watching)[.!]*|you[.!]*|bye[.!]*|\.+|\[.*\]|\(.*\))$", re.I)


def _load():
    global _model
    if _model is None:
        os.environ.setdefault("HF_HOME", str(MODELS_DIR / "hf"))
        from faster_whisper import WhisperModel

        _model = WhisperModel(MODEL, device="cpu", compute_type="int8", download_root=str(MODELS_DIR))
    return _model


def warm() -> None:
    if config.GROQ_API_KEY:
        return  # Groq transcribes; the local model loads only as a backup
    try:
        with _lock:
            _load()
    except Exception as e:
        print(f"[stt] speech model not loaded: {e}")


def _groq(audio: bytes, lang: str, mime: str) -> str:
    import httpx

    ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/mpeg": "mp3", "audio/wav": "wav"}.get(
        mime.split(";")[0].strip(), "webm")
    r = httpx.post(f"{config.GROQ_URL}/audio/transcriptions", timeout=30,
                   headers={"Authorization": f"Bearer {config.GROQ_API_KEY}"},
                   files={"file": (f"speech.{ext}", audio, mime.split(";")[0] or "audio/webm")},
                   data={"model": config.GROQ_STT_MODEL, "language": (lang or "en")[:2], "temperature": "0",
                         "prompt": "Jarvis, open Spotify. What's the weather?"})
    r.raise_for_status()
    return r.json().get("text", "").strip()


def transcribe(audio: bytes, lang: str = "en", mime: str = "audio/webm", local_only: bool = False) -> str:
    if config.GROQ_API_KEY and not local_only:
        try:
            text = _groq(audio, lang, mime)
            return "" if PHANTOMS.match(text) else text
        except Exception as e:
            print(f"[stt] Groq transcription failed ({e}); using the local model")
    with _lock:
        model = _load()
        segments, _ = model.transcribe(io.BytesIO(audio), language="en" if MODEL.endswith(".en") else (lang or "en")[:2],
                                       beam_size=1, vad_filter=True, initial_prompt="Jarvis, open Spotify. What's the weather?")
        text = " ".join(s.text.strip() for s in segments).strip()
    return "" if PHANTOMS.match(text) else text
