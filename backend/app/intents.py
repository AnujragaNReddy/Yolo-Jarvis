"""Instant answers for common commands, no language model needed.

A 3B model takes a second or two and sometimes picks the wrong tool; "volume up" or
"set a timer for 5 minutes" shouldn't depend on it. Anything not matched here goes
to the model.
"""

import re
import time
from datetime import datetime

from . import db
from .tools import info, memory, network, system, timers

_pending: dict = {}

NUM_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45,
             "fifty": 50, "sixty": 60, "ninety": 90, "half an": 0.5, "half a": 0.5}
UNIT = {"second": 1 / 60, "sec": 1 / 60, "minute": 1, "min": 1, "hour": 60, "hr": 60}
NUM = r"(\d+(?:\.\d+)?|half an?|an?|one|two|three|four|five|six|seven|eight|nine|ten|fifteen|twenty|thirty|forty five|forty|fifty|sixty|ninety)"
UNITS = r"(seconds?|secs?|minutes?|mins?|hours?|hrs?)"


def _num(s: str) -> float:
    return float(s) if re.match(r"\d", s) else NUM_WORDS[s]


def _sentence(title: str) -> str:
    title = title.strip()
    return title if title[-1:] in ".?!" else title + "."


def _minutes(n: str, unit: str) -> float:
    return _num(n) * UNIT[unit.rstrip("s")]


def clean(text: str) -> str:
    t = text.lower().strip().replace("’", "'").replace("a.m.", "am").replace("p.m.", "pm")
    t = re.sub(r"[^\w\s:%'.-]", " ", t)
    t = re.sub(r"\b(hey |ok |okay )?jarvis\b", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .,")
    t = re.sub(r"^(please |can you |could you |would you |will you |i want you to |i need you to )+", "", t)
    t = re.sub(r"( please| for me| now| right now| thanks| thank you)+$", "", t)
    return t.strip(" .")


def you() -> str:
    return db.setting("honorific") or db.setting("user_name") or "sir"


def greeting() -> str:
    h = datetime.now().hour
    part = "morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17 else "evening"
    name = db.setting("user_name")
    return f"Good {part}{', ' + name if name else ', ' + you()}"


def briefing() -> dict:
    lines, cards = [f"{greeting()}. It's {datetime.now().strftime('%I:%M %p').lstrip('0')}."], {}
    try:
        w = info.get_weather()
        cards["weather"] = w
        lines.append(f"In {w['place'].split(',')[0]} it's {w['temperature']} and {w['now']}, "
                     f"heading for {w['today_high']}, with a {w['rain_chance']} chance of rain.")
    except Exception:
        pass
    today_end = datetime.now().replace(hour=23, minute=59).timestamp()
    due = [r for r in timers.pending() if r["due"] <= today_end]
    if due:
        first = due[0]
        lines.append(f"You have {len(due)} reminder{'s' if len(due) > 1 else ''} today; the next is "
                     f"{first['text']} at {datetime.fromtimestamp(first['due']).strftime('%I:%M %p').lstrip('0')}.")
    todo = memory.notes()
    if todo:
        lines.append(f"{len(todo)} item{'s' if len(todo) > 1 else ''} on your list, starting with {todo[0]['text']}.")
    net = network.summary()
    if net:
        extra = f", {len(net['new'])} of them new" if net["new"] else ""
        lines.append(f"{net['online']} devices are on your network{extra}.")
    try:
        news = info.get_news()[:3]
        if news:
            cards["news"] = news
            lines.append("Top headlines. " + " ".join(_sentence(n["title"]) for n in news))
    except Exception:
        pass
    return {"text": " ".join(lines), "tool": "briefing", "cards": cards}


def _r(text: str, tool: str, **cards) -> dict:
    return {"text": text, "tool": tool, "cards": cards}


def match(raw: str) -> dict | None:
    t = clean(raw)
    if not t:
        return _r(f"Yes, {you()}?", "wake")

    if _pending:
        run, label, expires = _pending.pop("run"), _pending.pop("label"), _pending.pop("expires", 0)
        _pending.clear()
        if time.time() < expires and re.fullmatch(r"(yes|yeah|yep|yes please|sure|ok|okay|confirm(ed)?|do it|go ahead|proceed)( please)?", t):
            return _r(run(), label)
        if re.fullmatch(r"(no|nope|cancel|don't|do not|stop|never ?mind|abort)", t):
            return _r("Cancelled.", label)

    def ask(question: str, run, label: str) -> dict:
        # Things that can't be undone wait for a spoken "confirm" (30 s), and the model can never trigger them.
        _pending.update(run=run, label=label, expires=time.time() + 30)
        return _r(f"{question} Say confirm to proceed.", label)

    if m := re.fullmatch(r"(shut ?down|turn off|power off|restart|reboot)( the| my)? ?(computer|pc|laptop|system)?", t):
        action = "restart" if m.group(1) in ("restart", "reboot") else "shutdown"
        verb = "restart" if action == "restart" else "shut down"
        return ask(f"Shall I {verb} the computer, {you()}?", lambda: system.power(action), "power")
    if re.fullmatch(r"(cancel|abort|stop) (the )?(shut ?down|restart|reboot)", t):
        return _r(system.power("cancel"), "power")
    if re.fullmatch(r"(go to |put (the |my )?(computer|pc|laptop) to )?sleep|sleep (mode|the (computer|pc|laptop))", t):
        return ask(f"Put the computer to sleep, {you()}?", system.sleep, "power")
    if re.search(r"\b(recycle|recylce|recyle|recycling) ?bin\b|\btrash\b", t) and re.search(r"\b(empty|clear|clean|delete|remove)\b", t):
        n = system.recycle_bin_count()
        if not n:
            return _r(f"The Recycle Bin is already empty, {you()}.", "recycle_bin")
        return ask(f"Empty the Recycle Bin? That permanently deletes {n} item{'s' if n != 1 else ''}.",
                   system.empty_recycle_bin, "recycle_bin")
    if re.search(r"\b(recycle|recyle|recylce) ?bin\b", t):
        n = system.recycle_bin_count()
        return _r(f"The Recycle Bin has {n} item{'s' if n != 1 else ''}." if n else "The Recycle Bin is empty.", "recycle_bin")

    if m := re.fullmatch(r"(from now on |please )?(call me|address me as|my name is) (?P<name>[\w .'-]{1,30})", t):
        name = re.search(r"(?:call me|address me as|my name is) (.+)", raw, re.I).group(1).strip(" .!")
        db.save_settings({"user_name" if "name is" in t else "honorific": name})
        return _r(f"Very well, {name}." if "name is" not in t else f"Nice to meet you, {name}.", "settings")
    if re.fullmatch(r"(no|nope|yes|yeah|yep|ok|okay|sure|fine|alright|all right|got it|cool|hmm+|um+|uh+)", t):
        return _r(f"Very good, {you()}.", "chat")

    if m := re.fullmatch(r"(call|phone|ring|dial|video call|message|text|whatsapp|send a message to|send message to) (to )?(?!me\b)(?P<who>.+)", t):
        return _r(f"I can't make calls or send messages yet, {you()}. I can open WhatsApp for you if you like.", "chat")

    if re.fullmatch(r"(take a |take |capture (a |the )?)?screen ?shot|screen capture|capture (the |my )?screen", t):
        return _r(system.screenshot(), "screenshot")
    if m := re.fullmatch(r"(set |change )?(the )?(screen )?brightness (to |at )?(?P<n>\d{1,3})( percent| %|%)?", t):
        return _r(system.brightness("set", int(m.group("n"))), "brightness")
    if re.fullmatch(r"(increase|raise|turn up)( the)?( screen)? brightness|brightness up|brighter|make (it|the screen) brighter", t):
        return _r(system.brightness("up"), "brightness")
    if re.fullmatch(r"(decrease|lower|reduce|turn down|dim)( the)?( screen)? brightness|dim( the)? screen|brightness down|dimmer|make (it|the screen) (dimmer|darker)", t):
        return _r(system.brightness("down"), "brightness")
    if re.fullmatch(r"(show( the)? desktop|minimi[sz]e (all|everything)( windows)?|hide (all )?(the )?windows)", t):
        return _r(system.show_desktop(), "show_desktop")
    if m := re.fullmatch(r"open (the )?(?P<page>wi-?fi|bluetooth|display|sound|battery|update|windows update|network|apps|storage) settings", t):
        return _r(system.open_settings(m.group("page")), "open_settings")

    if re.fullmatch(r"(hi|hello|hey|yo|good (morning|afternoon|evening)|wake up|are you there|you there)", t):
        if t.startswith("good morning") or t == "wake up":
            return briefing()
        return _r(f"{greeting()}. At your service.", "greeting")
    if re.fullmatch(r"(daily |morning |my )?(briefing|brief me|status report|what did i miss|catch me up)", t):
        return briefing()
    if re.fullmatch(r"(thanks|thank you|cheers|good job|well done|nice)( jarvis)?", t):
        return _r(f"Always a pleasure, {you()}.", "chat")

    if re.fullmatch(r"(what('s| is) the )?time( is it)?|what time is it|tell me the time|current time", t):
        return _r(f"It's {datetime.now().strftime('%I:%M %p').lstrip('0')}, {you()}.", "get_time")
    if re.fullmatch(r"(what('s| is) )?(the |today's )?(date|day)( today| is it)?|what day is (it|today)|what('s| is) today", t):
        return _r(f"Today is {datetime.now().strftime('%A, %d %B %Y').replace(' 0', ' ')}.", "get_time")

    if m := re.fullmatch(r"(what('s| is) the |how('s| is) the |show me the |check the )?weather( like)?( (in|for|at) (?P<city>[\w\s.'-]+?))?( today| now| outside)?", t):
        w = info.get_weather(m.group("city") or "")
        return _r(f"{w['place'].split(',')[0]}: {w['temperature']} and {w['now']}, feels like {w['feels_like']}. "
                  f"Today {w['today_low']} to {w['today_high']}, {w['rain_chance']} chance of rain.", "get_weather", weather=w)

    if m := re.fullmatch(r"(what('s| is) the |read me the |give me the |show me the |latest |top )*(news|headlines)( about| on| for)?(?P<topic>.*)", t):
        topic = m.group("topic").strip()
        news = info.get_news(topic)
        if not news:
            return _r(f"I found no news{' about ' + topic if topic else ''}.", "get_news")
        return _r("Here are the headlines. " + " ".join(_sentence(n["title"]) for n in news[:3]), "get_news", news=news)

    if re.fullmatch(r"(volume up|turn (it|the volume) up|louder|increase (the )?volume|raise (the )?volume)", t):
        return _r(system.volume("up"), "volume")
    if re.fullmatch(r"(volume down|turn (it|the volume) down|quieter|lower (the )?volume|decrease (the )?volume)", t):
        return _r(system.volume("down"), "volume")
    if re.fullmatch(r"(mute|unmute)( the)?( volume| sound| audio| pc)?", t):
        return _r(system.volume("mute"), "volume")
    if m := re.fullmatch(r"(set (the )?)?volume (to |at )?(\d{1,3})( percent| %|%)?", t):
        return _r(system.volume("set", int(m.group(4))), "volume")

    if re.fullmatch(r"(pause|resume|play)( the)?( music| song| video| track| it| playback| spotify)?|(play|resume) (music|it again)", t):
        return _r(system.media("play"), "media")
    if re.fullmatch(r"(next|skip)( song| track| this( song| track)?| it)?|play (the )?next( song| track)?", t):
        return _r(system.media("next"), "media")
    if re.fullmatch(r"(previous|last|go back)( song| track)?|play (the )?previous( song| track)?", t):
        return _r(system.media("previous"), "media")
    if m := re.fullmatch(r"play (?P<q>.+?)( on youtube| from youtube| video)?", t):
        return _r(system.play_youtube(m.group("q")), "play_youtube")

    span = rf"{NUM} {UNITS}(?:,? (?:and )?{NUM} {UNITS})?"
    if re.fullmatch(rf"(set |start |make )?(an? )?(timer (for )?{span}|{span} timer)", t):
        mins = sum(_minutes(n, u) for n, u in re.findall(rf"{NUM} {UNITS}", t))
        return _r(timers.set_timer(minutes=mins), "set_timer")
    if m := re.fullmatch(rf"remind me (to |about )?(?P<what>.+?) in {NUM} {UNITS}", t):
        return _r(timers.set_reminder(m.group("what"), in_minutes=_minutes(m.group(3), m.group(4))), "set_reminder")
    if m := re.fullmatch(rf"remind me in {NUM} {UNITS} (to |about )?(?P<what>.+)", t):
        return _r(timers.set_reminder(m.group("what"), in_minutes=_minutes(m.group(1), m.group(2))), "set_reminder")
    if m := re.fullmatch(r"remind me (to |about )?(?P<what>.+?) at (?P<at>\d{1,2}(:\d{2})? ?(am|pm|a m|p m)?|noon|midnight)", t):
        at = m.group("at").replace("a m", "am").replace("p m", "pm")
        return _r(timers.set_reminder(m.group("what"), at=at), "set_reminder")
    if re.fullmatch(r"(what are |list |show )?(my |the )?(timers|reminders)|(any|do i have any) (timers|reminders)", t):
        items = timers.list_reminders()
        if isinstance(items, str):
            return _r(items, "list_reminders")
        return _r(f"{len(items)} scheduled. " + ". ".join(f"{i['text']} in {i['in']}" for i in items[:4]) + ".", "list_reminders")
    if m := re.fullmatch(r"(cancel|stop|delete|clear) (the |my |all )?(?P<w>timers?|reminders?|all timers|everything)", t):
        w = m.group("w")
        return _r(timers.cancel_reminder("all" if "all" in t or w.endswith("s") else "timer" if "timer" in w else "all"), "cancel_reminder")

    if re.fullmatch(r"lock( the| my)? (computer|pc|screen|laptop|system)|lock it", t):
        return _r(system.lock_screen(), "lock_screen")
    if re.fullmatch(r"(system|computer|pc) (status|report|check|diagnostics)|diagnostics|run diagnostics|how('s| is) (the|my) (system|computer|pc)( doing)?", t):
        s = system.system_status()
        bat = f" Battery {s['battery']}%{' and charging' if s['plugged'] else ''}." if s["battery"] is not None else ""
        return _r(f"CPU at {round(s['cpu'])}%, memory {round(s['ram'])}% used of {s['ram_total_gb']} gigabytes, "
                  f"{s['disk_free_gb']} gigabytes free on the system drive.{bat} Up for {s['uptime_h']} hours.",
                  "system_status", system=s)

    if re.fullmatch(r"(who('s| is) (on|connected to) (my |the )?(wi-?fi|network)|how many devices( are)?( online| connected)?|network (status|report)|scan (my |the )?network)", t):
        s = network.summary()
        if s is None:
            return _r("Your network scanner isn't running, so I can't see the network.", "network_devices")
        extra = f" {len(s['new'])} new: {', '.join(s['new'][:3])}." if s["new"] else ""
        risky = f" {len(s['risky'])} with risky services open." if s["risky"] else ""
        return _r(f"{s['online']} of {s['known']} known devices are online.{extra}{risky}", "network_devices", network=s)

    if t.startswith("remember "):
        m = re.search(r"remember (that )?(?P<f>.+)", raw, re.I)
        fact = m.group("f").strip(" .") if m else t[9:]
        return _r(memory.remember(fact[0].upper() + fact[1:]).replace("Remembered:", "Noted:"), "remember")
    if m := re.fullmatch(r"(add|put) (?P<x>.+?) (to|on) (my |the )?((to-?do|todo|shopping) )?(list|notes)", t):
        return _r(memory.add_note(m.group("x")), "add_note")
    if re.fullmatch(r"(what('s| is) on |read |show )?(me )?my (to-?do |todo |shopping )?(list|notes|to-?dos)", t):
        items = memory.list_notes()
        if isinstance(items, str):
            return _r(items, "list_notes")
        return _r(f"{len(items)} on your list: " + "; ".join(i["text"] for i in items[:6]) + ".", "list_notes")

    if re.fullmatch(r"(start a )?new (conversation|chat|topic)|forget (this|our) conversation|clear (the )?(chat|conversation)", t):
        db.new_session()
        return _r("Fresh start. What can I do for you?", "new_session")

    if m := re.fullmatch(r"(open|launch|start|run|fire up) (up )?(the )?(?P<name>[\w\s.'+-]+?)( app| application| website| site| in (the )?browser)?", t):
        name = m.group("name").strip()
        if name in system.SITES or re.fullmatch(r"[\w-]+(\.[\w-]+)+(/\S*)?", name) or "website" in t or "site" in t:
            return _r(system.open_website(name), "open_website")
        result = system.open_app(name)
        if not result.startswith("I couldn't"):
            return _r(result, "open_app")
        return None
    if m := re.fullmatch(r"(close|quit|exit|kill) (the )?(?P<name>[\w\s.'+-]+?)( app| application| window)?", t):
        return _r(system.close_app(m.group("name")), "close_app")
    if m := re.fullmatch(r"(google|search google for|look up on google) (?P<q>.+)", t):
        return _r(system.search_in_browser(m.group("q")), "search_in_browser")
    return None
