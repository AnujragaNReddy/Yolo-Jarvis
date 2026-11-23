import re

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


async def synth(text: str, voice: str) -> bytes:
    if voice not in {v["id"] for v in VOICES}:
        voice = VOICES[0]["id"]
    for attempt in range(2):  # the free voice service drops a request now and then
        out = bytearray()
        try:
            async for chunk in edge_tts.Communicate(speakable(text), voice, rate="+4%", pitch="-2Hz").stream():
                if chunk["type"] == "audio":
                    out += chunk["data"]
            return bytes(out)
        except edge_tts.exceptions.NoAudioReceived:
            if attempt:
                raise
    return b""
