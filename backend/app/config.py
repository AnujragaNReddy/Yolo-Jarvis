import os
from pathlib import Path

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
load_dotenv(BACKEND / ".env")

PORT = int(os.getenv("JARVIS_PORT", "8040"))
# Off by default: JARVIS controls this PC and has no login. When on, other devices need the access key.
LAN = os.getenv("JARVIS_LAN", "false").lower() == "true"
# Other devices connect over HTTPS on this port (phones only allow the microphone on HTTPS pages).
HTTPS_PORT = int(os.getenv("JARVIS_HTTPS_PORT", "8443"))
DB_PATH = BACKEND / os.getenv("DB_PATH", "data/jarvis.db")
FRONTEND = ROOT / "frontend"

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")

# Optional fast cloud brain and ears (free tier at console.groq.com). Without a key everything stays local.
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# Tried in order; each has its own free per-minute limit. Comma-separated to choose your own.
GROQ_MODELS = [m.strip() for m in os.getenv(
    "GROQ_MODEL", "openai/gpt-oss-120b,openai/gpt-oss-20b,qwen/qwen3.8-27b").split(",") if m.strip()]
GROQ_STT_MODEL = os.getenv("GROQ_STT_MODEL", "whisper-large-v3-turbo")
GROQ_URL = "https://api.groq.com/openai/v1"
NETWORK_SCANNER_URL = os.getenv("NETWORK_SCANNER_URL", "http://127.0.0.1:8030").rstrip("/")

# Defaults for settings the HUD can change; stored values in the database win.
DEFAULTS = {
    "model": os.getenv("OLLAMA_MODEL", "qwen2.5:3b"),
    "user_name": "",
    "honorific": "sir",
    "city": "",
    "voice": "en-GB-RyanNeural",
    "speech_lang": "en-US",
    "units": "metric",
}

HISTORY_TURNS = 8
MAX_TOOL_ROUNDS = 4
USER_AGENT = "YoloJarvis/1.0 (https://github.com/; local personal assistant) httpx/0.28"
