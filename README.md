# YOLO-JARVIS

Your own J.A.R.V.I.S.: a voice assistant with an Iron Man-style HUD that runs on your Windows PC. Say
"Jarvis", ask anything, and he answers out loud in a British voice. He can check the weather and news,
search the web, open apps and websites, play music, set timers and reminders, keep your to-do list, remember
things about you, report on your PC, and tell you who is on your Wi-Fi.

Free: the brain is a local AI model (Ollama), so there are no API keys or accounts.

## Run

Needs Python 3.11+, [Ollama](https://ollama.com/download), and Chrome or Edge.

```
ollama pull qwen2.5:3b
start.bat
```

`start.bat` sets everything up on first run, then opens http://localhost:8040. Click **Activate** and allow the
microphone.

## Talking to him

- **Wake word:** say "Jarvis, what's the weather?" or just "Jarvis", wait for the chime, then speak.
- After he answers he keeps listening for about 6 seconds, so follow-ups need no wake word.
- **Push-to-talk:** press Space, the mic button, or the reactor. **Esc** or "stop" silences him.
- Or type in the command box. **M** mutes his voice.

| Say | What happens |
|---|---|
| "Good morning" / "brief me" | Time, weather, today's reminders, your list, network and top headlines |
| "What's the weather in London?" | Live weather (Open-Meteo) |
| "Latest news about AI" | Headlines (Google News) with links on screen |
| "Who won the last World Cup?" / "price of bitcoin" | Web search (DuckDuckGo), answered in a sentence |
| "Who is Tony Stark?" | Wikipedia summary |
| "What's 18% of 2450?" | Exact maths |
| "Open Spotify" / "open youtube.com" / "close Notepad" | Any app in your Start menu, any website |
| "Play lo-fi beats" / "next song" / "pause" | Plays on YouTube; media keys for Spotify and others |
| "Volume to 30" / "mute" | PC volume |
| "Set a timer for 10 minutes" / "remind me to call mom at 6pm" | Spoken alarm, plus a notification if the tab is hidden |
| "Add milk to my list" / "what's on my list" | To-do list |
| "Remember that my car is on level 3" | Stored; he uses it in later answers ("where's my car?") |
| "Find my resume" | Searches Desktop, Documents, Downloads…; Open buttons on screen |
| "System status" | CPU, memory, disk, battery, uptime |
| "Who is on my Wi-Fi?" | From [Yolo Network Scanner](../network-scanner) if it is running |
| "Lock the computer" | Locks the screen |
| "Shut down the computer" | Asks you to say "confirm", then shuts down in 30 s ("cancel shutdown" stops it) |

## How it works

```
mic -> browser speech recognition -> "Jarvis" wake word -> /api/chat
        -> instant commands (time, timers, volume, apps... no AI needed)
        -> otherwise Ollama qwen2.5:3b with 27 tools, up to 4 tool rounds
-> answer on screen + edge-tts neural voice (en-GB Ryan) -> reactor pulses with the voice
```

- `backend/app/intents.py` instant commands. `brain.py` the AI loop. `tools/` everything he can do.
- `frontend/` the HUD: plain HTML, CSS and JavaScript served by the backend, no build step.
- Data (memories, list, reminders, conversation) is in `backend/data/jarvis.db`.

**Settings** (gear icon): your name, what he calls you, city, units, voice (9 voices, including FRIDAY), speech
language, AI model, and whether he always listens for "Jarvis".

## Privacy and safety

- Your conversation, memories and the AI model stay on this PC. Speech recognition is your browser's own
  service, the voice is Microsoft's free online voices, and web searches, weather and news go to those sites.
- JARVIS only listens on this PC (127.0.0.1), and refuses requests from other websites, so a web page can't
  tell him to open things.
- He won't run programs or scripts found in your files, only open documents, pictures and folders. He can't run
  shell commands. Shutdown and restart need a spoken "confirm" and are never chosen by the AI.

## Speed and the AI model

On a PC without a graphics card, instant commands answer immediately and AI questions take about 5 to 15
seconds. `qwen2.5:3b` is small enough for that; it can occasionally pick the wrong tool or misread a search
result. With a stronger PC, pull a bigger model (`ollama pull qwen2.5:7b`) and choose it in Settings.
