import re
from collections import OrderedDict

import edge_tts

VOICES = [
    {"id": "en-GB-RyanNeural", "label": "Ryan (British, male) - classic JARVIS"},
    {"id": "en-GB-ThomasNeural", "label": "Thomas (British, male)"},
    {"id": "en-GB-SoniaNeural", "label": "Sonia (British, female) - FRIDAY"},
    {"id": "en-US-AndrewNeural", "label": "Andrew (American, male)"},
    {"id": "en-US-GuyNeural", "label": "Guy (American, male)"},
    {"id": "en-US-AvaNeural", "label": "Ava (American, female)"},
    {"id": "en-IN-PrabhatNeural", "label": "Prabhat (Indian, male)"},
    {"id": "en-IN-NeerjaNeural", "label": "Neerja (Indian, female)"},
    {"id": "en-AU-WilliamNeural", "label": "William (Australian, male)"},
]


def speakable(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>|]", "", text)
    text = text.replace("°C", " degrees").replace("°F", " degrees").replace("km/h", "kilometres an hour")
    return re.sub(r"\s+", " ", text).strip()[:1500]


# Recent phrases ("Yes, sir?", greetings, "Timer set for 5 minutes.") come straight from memory.
_cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
CACHE_SIZE = 150


async def synth(text: str, voice: str) -> bytes:
    if voice not in {v["id"] for v in VOICES}:
        voice = VOICES[0]["id"]
    text = speakable(text)
    if (voice, text) in _cache:
        _cache.move_to_end((voice, text))
        return _cache[(voice, text)]
    for attempt in range(2):  # the free voice service drops a request now and then
        out = bytearray()
        try:
            # No rate/pitch change: adjusting prosody made the service take ~1.5 s longer per sentence.
            async for chunk in edge_tts.Communicate(text, voice).stream():
                if chunk["type"] == "audio":
                    out += chunk["data"]
            audio = bytes(out)
            if audio and len(text) <= 300:
                _cache[(voice, text)] = audio
                while len(_cache) > CACHE_SIZE:
                    _cache.popitem(last=False)
            return audio
        except edge_tts.exceptions.NoAudioReceived:
            if attempt:
                raise
    return b""
