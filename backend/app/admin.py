"""Admin approval: the owner's spoken passphrase AND their voiceprint, for anything that deletes, uninstalls or
writes. Requests only describe a change (tools/files.py plans it); nothing runs until verify() passes.

The passphrase is checked by the local speech model (never the cloud) and stored only as scrypt hashes. The
voiceprint is a speaker embedding (3D-Speaker ERes2Net via sherpa-onnx) averaged over the enrollment samples.
"""

import hashlib
import io
import json
import re
import secrets
import threading
import time

import numpy as np

from . import config, stt

MODEL = config.DB_PATH.parent / "models" / "voiceid" / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
MODEL_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
             "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx")
PROFILE = config.DB_PATH.parent / "admin_voice.json"
SAMPLES = 5
MAX_FAILS = 3
LOCK_SECONDS = 300
CHALLENGE_SECONDS = 90

_lock = threading.Lock()
_extractor = None
_challenges: dict[str, dict] = {}
_enroll: dict | None = None
_fails = 0
_locked_until = 0.0

DIGITS = {"zero": "0", "oh": "0", "one": "1", "two": "2", "to": "2", "too": "2", "three": "3", "four": "4",
          "for": "4", "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9"}


class Refused(Exception):
    pass


def normalize(text: str) -> str:
    """'Blue falcon, seven two nine!' and 'blue Falcon 729' both become 'blue falcon 729'."""
    words = re.sub(r"[^a-z0-9 ]+", " ", text.lower().replace("-", " ")).split()
    words = [w for w in words if w not in ("jarvis", "the", "a", "um", "uh")]
    out: list[str] = []
    for w in words:
        d = DIGITS.get(w, w)
        if d.isdigit() and out and out[-1].isdigit():
            out[-1] += d
        else:
            out.append(d)
    return " ".join(out)


def _hash(phrase: str, salt: bytes) -> str:
    return hashlib.scrypt(phrase.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32).hex()


def _load_profile() -> dict | None:
    try:
        return json.loads(PROFILE.read_text())
    except (OSError, ValueError):
        return None


def _model():
    global _extractor
    if _extractor is None:
        if not MODEL.exists():
            import httpx

            MODEL.parent.mkdir(parents=True, exist_ok=True)
            MODEL.write_bytes(httpx.get(MODEL_URL, follow_redirects=True, timeout=120).content)
        import sherpa_onnx

        _extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL), num_threads=2))
    return _extractor


def _embed(audio: bytes) -> np.ndarray:
    from faster_whisper.audio import decode_audio

    samples = decode_audio(io.BytesIO(audio), sampling_rate=16000)
    if len(samples) < 16000 * 0.8:
        raise Refused("That was too short. Say the whole passphrase.")
    with _lock:
        ext = _model()
        stream = ext.create_stream()
        stream.accept_waveform(16000, samples)
        stream.input_finished()
        v = np.array(ext.compute(stream), dtype=np.float32)
    return v / np.linalg.norm(v)


def _hear(audio: bytes) -> str:
    return stt.transcribe(audio, "en", "audio/webm", local_only=True)


# ------------------------------------------------------------------ status and enrollment (laptop only)

def status() -> dict:
    p = _load_profile()
    return {"enrolled": bool(p), "enrolled_at": p.get("created") if p else None,
            "locked_for": max(0, round(_locked_until - time.time())),
            "enrolling": len(_enroll["embs"]) if _enroll else None, "samples_needed": SAMPLES}


def enroll_start(passphrase: str) -> dict:
    global _enroll
    norm = normalize(passphrase)
    if len(norm.replace(" ", "")) < 6 or len(norm.split()) < 2:
        raise Refused("Pick at least two words, or a word and a number, e.g. 'blue falcon 729'.")
    if len(norm.split()) > 8:
        raise Refused("Keep it under eight words, so it's quick to say.")
    _enroll = {"phrase": norm, "embs": [], "heard": set(), "started": time.time()}
    return status()


def enroll_sample(audio: bytes) -> dict:
    if not _enroll or time.time() - _enroll["started"] > 900:
        raise Refused("Start the setup again.")
    heard = normalize(_hear(audio))
    if heard != _enroll["phrase"]:
        # Close enough to be the same phrase misheard? Accept it as a variant, so later checks don't fail on the
        # same mishearing; otherwise ask again.
        a, b = set(heard.split()), set(_enroll["phrase"].split())
        if not heard or len(a & b) < max(1, len(b) - 1):
            return {**status(), "ok": False, "heard": heard or "(nothing)"}
    _enroll["embs"].append(_embed(audio).tolist())
    _enroll["heard"].add(heard)
    return {**status(), "ok": True, "heard": heard}


def enroll_finish() -> dict:
    global _enroll, _fails, _locked_until
    if not _enroll or len(_enroll["embs"]) < SAMPLES:
        raise Refused(f"Record all {SAMPLES} samples first.")
    E = np.array(_enroll["embs"])
    centroid = E.mean(0)
    centroid /= np.linalg.norm(centroid)
    pairs = [float(E[i] @ E[j]) for i in range(len(E)) for j in range(i + 1, len(E))]
    # Through a browser mic the owner scored 0.83 and a different voice 0.56, so the bar sits close to the owner's
    # own spread and never below 0.62. A cold or a different mic can mean a retry; re-enroll if it keeps failing.
    threshold = float(np.clip(min(pairs) - 0.08, 0.62, 0.78))
    salt = secrets.token_bytes(16)
    phrases = {_enroll["phrase"], *_enroll["heard"]}
    PROFILE.write_text(json.dumps({
        "salt": salt.hex(), "phrase_hashes": sorted(_hash(p, salt) for p in phrases),
        "centroid": centroid.tolist(), "threshold": threshold, "created": time.time(), "samples": len(E),
    }))
    _enroll = None
    _fails, _locked_until = 0, 0.0
    return {**status(), "threshold": round(threshold, 2)}


def remove() -> dict:
    global _enroll
    _enroll = None
    PROFILE.unlink(missing_ok=True)
    _challenges.clear()
    return status()


# ------------------------------------------------------------------ approvals

def request(plan: dict) -> dict:
    if not _load_profile():
        raise Refused("That needs your admin voice, which isn't set up yet. Set it up in Settings on the laptop.")
    _challenges.clear()  # one approval at a time: a new request replaces any older one
    cid = secrets.token_urlsafe(12)
    _challenges[cid] = {"plan": plan, "expires": time.time() + CHALLENGE_SECONDS}
    return {"id": cid, "summary": plan["summary"], "expires_in": CHALLENGE_SECONDS}


def cancel(cid: str) -> None:
    _challenges.pop(cid, None)


def verify(cid: str, audio: bytes) -> tuple[bool, str, dict | None]:
    """(approved, message for the user, the plan to run when approved)."""
    global _fails, _locked_until
    if time.time() < _locked_until:
        return False, f"Admin actions are locked for {round(_locked_until - time.time())} more seconds.", None
    ch = _challenges.get(cid)
    if not ch or time.time() > ch["expires"]:
        _challenges.pop(cid, None)
        return False, "That approval expired. Ask again.", None
    profile = _load_profile()
    if not profile:
        return False, "Admin voice isn't set up.", None
    try:
        heard = normalize(_hear(audio))
        score = float(_embed(audio) @ np.array(profile["centroid"]))
    except Refused as e:
        return False, str(e), None
    phrase_ok = _hash(heard, bytes.fromhex(profile["salt"])) in profile["phrase_hashes"]
    voice_ok = score >= profile["threshold"]
    print(f"[admin] check: phrase {'ok' if phrase_ok else 'WRONG'}, voice {score:.2f} "
          f"(needs {profile['threshold']:.2f}) {'ok' if voice_ok else 'NO MATCH'}")
    if phrase_ok and voice_ok:
        _fails = 0
        _challenges.pop(cid, None)
        return True, "Verified.", ch["plan"]
    _fails += 1
    if _fails >= MAX_FAILS:
        _fails = 0
        _locked_until = time.time() + LOCK_SECONDS
        _challenges.clear()
        return False, "Admin check failed three times. Admin actions are locked for 5 minutes.", None
    left = MAX_FAILS - _fails
    # Which part failed is printed on the laptop only; saying it aloud would help someone guessing.
    return False, f"That didn't pass the admin check. {left} {'try' if left == 1 else 'tries'} left.", None
