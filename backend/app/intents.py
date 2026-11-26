"""Instant answers for common commands, no language model needed.

A 3B model takes a second or two and sometimes picks the wrong tool; "volume up" or
"set a timer for 5 minutes" shouldn't depend on it. Anything not matched here goes
to the model.
"""

import re
import time
from datetime import datetime

from . import db
from .admin import Refused
from .tools import coders, control, files, info, memory, network, phone, system, timers

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
    t = re.sub(r"\s+", " ", t).strip(" .,")
    # The wake word only at the start or end: "create a folder called Jarvis notes" keeps it.
    t = re.sub(r"^((hey|hi|ok|okay) )?jarvis\b[ .,]*|[ .,]*\bjarvis$", "", t).strip(" .,")
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


PHONE = r"(?: (?:on|in|from|of) (?:my |the )?(?:phone|mobile|android))"


def _phone(t: str, raw: str) -> dict | None:
    """Phone commands: anything ending "on my phone", plus calls and messages (only a phone can do those)."""
    def run(fn, label, *a, **k) -> dict:
        try:
            return _r(fn(*a, **k), label)
        except Refused as e:
            return _r(str(e), label)
        except Exception as e:
            return _r(f"Your phone didn't respond: {e}", label)

    def original(fragment: str) -> str:
        m = re.search(re.escape(fragment), raw, re.I)
        return m.group(0) if m else fragment

    if m := re.search(r"\b(?:pair(?:ing)?|code)\b.*?(?P<code>\d(?:[\s-]?\d){5})\b.*?\b(?:port|address|on)\b\D*?"
                      r"(?:(?P<ip>\d{1,3}(?:\.\d{1,3}){3})\D+)?(?P<port>\d{4,5})\b", t):
        ip = m.group("ip") or db.setting("phone_ip")
        if not ip:
            return _r("Type the full address from the pairing box, like 192.168.1.20:40447, in the phone panel.", "phone_pair")
        return run(phone.pair, "phone_pair", f"{ip}:{m.group('port')}", m.group("code"))
    if re.fullmatch(r"(pair|link|connect|reconnect)( to)? (my |the )?(phone|mobile)|(phone|mobile) (connect|reconnect|pair)", t):
        if t.startswith("pair") or t.startswith("link") or t.endswith("pair"):
            return _r(phone.PROBLEMS["not_set_up"].replace("Your phone isn't linked yet. ", ""), "phone_pair")
        try:
            phone.connect(force=True)
            return _r("Your phone is connected.", "phone_connect")
        except Refused as e:
            return _r(str(e), "phone_connect")
    if m := re.fullmatch(rf"(uninstall|remove|delete) (the )?(app )?(?P<n>[\w\s.'+&-]+?)( app)?{PHONE}", t):
        return _plan(lambda: phone.phone_uninstall_app(m.group("n")), "phone_uninstall_app")
    if m := re.fullmatch(rf"open (?P<u>[\w-]+(\.[\w-]+)+(/\S*)?){PHONE}", t):
        return run(phone.phone_open, "phone_open", url=m.group("u"))
    if m := re.fullmatch(rf"(open|launch|start) (the )?(?P<n>[\w\s.'+&-]+?)( app)?{PHONE}", t):
        return run(phone.phone_open_app, "phone_open_app", m.group("n"))
    if m := re.fullmatch(rf"(close|quit|kill|stop) (the )?(?P<n>[\w\s.'+&-]+?)( app)?{PHONE}", t):
        return run(phone.phone_close_app, "phone_close_app", m.group("n"))
    if m := re.fullmatch(rf"play (?P<q>.+?)( on youtube)?{PHONE}", t):
        if m.group("q") in ("music", "it", "the music", "the song"):
            return run(phone.phone_media, "phone_media", "play")
        return run(phone.phone_open, "phone_open", youtube=m.group("q"))
    if m := re.fullmatch(rf"(?:(?:turn|set) )?(?:the )?(?:volume )?(?P<a>up|down|louder|quieter|mute|unmute)(?: the volume)?{PHONE}|"
                         rf"(?:volume|turn the volume) (?P<b>up|down){PHONE}|mute{PHONE}", t):
        a = (m.group("a") or m.group("b") or "mute").replace("louder", "up").replace("quieter", "down")
        return run(phone.phone_volume, "phone_volume", a)
    if m := re.fullmatch(rf"(set )?(the )?volume (to |at )?(?P<n>\d{{1,3}})( percent|%)?{PHONE}", t):
        return run(phone.phone_volume, "phone_volume", "set", int(m.group("n")))
    if m := re.fullmatch(rf"(?P<a>pause|resume|play|next|skip|previous)( the)?( song| track| music| video)?{PHONE}", t):
        return run(phone.phone_media, "phone_media", m.group("a"))
    if re.fullmatch(rf"(take a |take |capture (a |the )?)?screen ?shot{PHONE}|screen ?shot (of )?(my |the )?(phone|mobile)", t):
        return run(phone.phone_screenshot, "phone_screenshot")
    if re.fullmatch(rf"lock{PHONE}|lock (my |the )?(phone|mobile)|(turn|switch) off (my |the )?(phone|mobile) screen", t):
        return run(phone.phone_screen, "phone_screen", "lock")
    if re.fullmatch(r"(wake|unlock|turn on) (up )?(my |the )?(phone|mobile)( screen)?", t):
        return run(phone.phone_screen, "phone_screen", "wake")
    if re.fullmatch(r"(how('s| is) |what('s| is) )?(my |the )?(phone|mobile)('s)? (battery|status|charge)( level)?|"
                    r"(phone|mobile) (battery|status)|how much (battery|charge) (is )?(on|in|left on) (my |the )?(phone|mobile)", t):
        try:
            s = phone.phone_status()
            return _r(f"Your {s['maker']} phone is at {s['battery']}%{', charging' if s['charging'] else ''}.", "phone_status")
        except Refused as e:
            return _r(str(e), "phone_status")
    if m := re.fullmatch(rf"open (?P<u>[\w-]+(\.[\w-]+)+(/\S*)?){PHONE}", t):
        return run(phone.phone_open, "phone_open", url=m.group("u"))
    if m := re.fullmatch(r"(whatsapp|send a whatsapp( message)? to|whatsapp message) (?P<who>.+?) (saying|that says|to say|:) (?P<msg>.+)", t):
        return run(phone.phone_message, "phone_message", original(m.group("who")), original(m.group("msg")), "whatsapp")
    if m := re.fullmatch(r"(text|message|send a message to|send message to|send a text to|sms) (?P<who>.+?) (saying|that says|to say|:) (?P<msg>.+)", t):
        return run(phone.phone_message, "phone_message", original(m.group("who")), original(m.group("msg")), "sms")
    if m := re.fullmatch(rf"(call|phone|ring|dial) (to )?(?!me\b)(?P<who>.+?){PHONE}?", t):
        return run(phone.phone_call, "phone_call", original(m.group("who")))
    if re.fullmatch(r"(message|text|whatsapp|send a message to) (to )?(?!me\b).+", t):
        return _r(f"What should the message say, {you()}? Say for example: message Thangam saying I'm on my way.", "chat")
    return None


SHORTCUTS = {
    "copy": "ctrl+c", "copy that": "ctrl+c", "copy this": "ctrl+c", "copy it": "ctrl+c",
    "paste": "ctrl+v", "paste it": "ctrl+v", "paste that": "ctrl+v", "paste here": "ctrl+v",
    "cut": "ctrl+x", "cut that": "ctrl+x", "undo": "ctrl+z", "undo that": "ctrl+z", "redo": "ctrl+y",
    "select all": "ctrl+a", "select everything": "ctrl+a", "save": "ctrl+s", "save it": "ctrl+s",
    "save this": "ctrl+s", "save the file": "ctrl+s", "new tab": "ctrl+t", "open a new tab": "ctrl+t",
    "close tab": "ctrl+w", "close the tab": "ctrl+w", "close this tab": "ctrl+w", "reopen tab": "ctrl+shift+t",
    "reopen the tab": "ctrl+shift+t", "next tab": "ctrl+tab", "previous tab": "ctrl+shift+tab",
    "refresh": "f5", "refresh the page": "f5", "reload": "f5", "reload the page": "f5",
    "find": "ctrl+f", "find on page": "ctrl+f", "print": "ctrl+p", "zoom in": "ctrl+plus", "zoom out": "ctrl+minus",
    "switch window": "alt+tab", "switch windows": "alt+tab", "alt tab": "alt+tab",
    "open task view": "win+tab", "task view": "win+tab", "open notifications": "win+n", "open emoji": "win+period",
    "press enter": "enter", "hit enter": "enter", "press escape": "esc", "press tab": "tab",
}


def _ctl(make, tool: str) -> dict:
    try:
        out = make()
    except Refused as e:
        return _r(str(e), tool)
    if isinstance(out, list):
        out = "; ".join(out[:8]) + "."
    return _r(out, tool)


def _control(t: str, raw: str) -> dict | None:
    """Keyboard, windows, settings, processes, clipboard and screen commands that need no AI."""
    if t in SHORTCUTS:
        return _ctl(lambda: control.press_keys(SHORTCUTS[t]), "press_keys")
    if m := re.fullmatch(r"(press|hit|push) (the )?(?P<k>[\w +]+?)( key| keys| shortcut)?( (?P<n>\d+|two|three|four|five) times)?", t):
        try:
            control.parse_combo(m.group("k"))  # "press the Save button" is a click, not a key
        except Refused:
            m = None
        if m:
            n = {"two": 2, "three": 3, "four": 4, "five": 5}.get(m.group("n") or "", None) or int(m.group("n") or 1)
            return _ctl(lambda: control.press_keys(m.group("k"), times=n), "press_keys")
    if re.match(r"(type|type out|write out|dictate) \S", t):
        m = re.search(r"\b(?:type out|type|write out|dictate)\s+(.+?)\s*$", raw, re.I | re.S)
        text, app = (m.group(1) if m else t.split(" ", 1)[1]).strip(), ""
        tail = re.search(r"^(.*\S)\s+in(?:to)?\s+(?:the\s+)?([\w .'-]{2,30})$", text, re.I)
        if tail:
            try:
                control.pick_window(tail.group(2))
                text, app = tail.group(1), tail.group(2)
            except Refused:
                pass
        text = text.strip().strip('"\'')
        return _ctl(lambda: control.type_text(text, app), "type_text")

    win = r"(this |the |current |that )?(window|app)"
    if m := re.fullmatch(rf"(?P<a>minimi[sz]e|maximi[sz]e|restore|close) {win}", t):
        return _ctl(lambda: control.window_control(m.group("a").replace("is", "iz")), "window_control")
    if m := re.fullmatch(r"(?P<a>minimi[sz]e|maximi[sz]e|restore) (the )?(?P<app>[\w .'+-]+?)( window| app)?", t):
        return _ctl(lambda: control.window_control(m.group("a").replace("is", "iz"), m.group("app")), "window_control")
    if (m := re.fullmatch(r"(switch|go|change|jump) (back )?to (the )?(?P<app>[\w .'+-]+?)( window| app)?", t)) \
            and not re.search(r"\b(mode|theme|sleep|bed)\b", t):
        try:  # only when that window is open; "go to youtube" etc. carry on to the other commands
            control.pick_window(m.group("app"))
            return _ctl(lambda: control.window_control("switch", m.group("app")), "window_control")
        except Refused:
            pass
    if m := re.fullmatch(r"snap (?P<app>[\w .'+-]+? )?(to (the )?)?(?P<side>left|right)", t):
        app = (m.group("app") or "").strip()
        app = "" if app in ("this", "it", "the window", "this window", "window") else app
        return _ctl(lambda: control.window_control("snap_" + m.group("side"), app), "window_control")
    if m := re.fullmatch(r"scroll (?P<d>up|down)( (a bit|more|the page))?", t):
        return _ctl(lambda: control.window_control("scroll_" + m.group("d")), "window_control")
    if re.fullmatch(r"(list|show|what are) (my |the |all )?(open )?windows( are open)?|what windows are open", t):
        return _ctl(lambda: control.window_control("list"), "window_control")

    if m := re.fullmatch(r"(turn|switch) (?P<v>on|off) (the )?(?P<s>wi-?fi|wifi|bluetooth|dark mode|light mode)", t) or \
            re.fullmatch(r"(turn|switch) (the )?(?P<s>wi-?fi|wifi|bluetooth|dark mode|light mode) (?P<v>on|off)", t) or \
            re.fullmatch(r"(?P<v>enable|disable) (the )?(?P<s>wi-?fi|wifi|bluetooth|dark mode|light mode)", t) or \
            re.fullmatch(r"(?P<s>wi-?fi|wifi|bluetooth|dark mode|light mode) (?P<v>on|off)", t):
        v = {"enable": "on", "disable": "off"}.get(m.group("v"), m.group("v"))
        s = m.group("s").replace("-", "").replace(" ", "_")
        return _ctl(lambda: control.pc_setting(s, v), "pc_setting")
    if re.fullmatch(r"(go |switch )?(to )?dark mode( please)?|make (it|everything|the screen) dark", t):
        return _ctl(lambda: control.pc_setting("dark_mode", "on"), "pc_setting")
    if re.fullmatch(r"(go |switch )?(to )?light mode( please)?", t):
        return _ctl(lambda: control.pc_setting("light_mode", "on"), "pc_setting")
    if m := re.fullmatch(r"(connect|join) (to )?(the )?(wi-?fi( network)? )?(?P<n>.+?)( wi-?fi)?( network)?", t):
        if "wifi" in t.replace("-", "") or "network" in t:
            name = re.search(re.escape(m.group("n")), raw, re.I)
            return _ctl(lambda: control.pc_setting("wifi", name.group(0) if name else m.group("n")), "pc_setting")
    if re.fullmatch(r"(what|which|show|list|scan for)( are)?( the)?( wi-?fi)? networks( are)?( (nearby|around|in range))?|what wi-?fi networks can you see", t):
        return _ctl(lambda: control.pc_setting("wifi", "list"), "pc_setting")
    if re.fullmatch(r"(is )?(the )?(wi-?fi|bluetooth)( status| on| off)?", t) and "is" in t.split()[:1]:
        return _ctl(lambda: control.pc_setting("bluetooth" if "bluetooth" in t else "wifi", "status"), "pc_setting")

    if m := re.fullmatch(r"(force (close|quit|kill|stop)|kill|end task( for)?|terminate) (the )?(?P<n>[\w .'+-]+?)( app| process| program)?( (now|it'?s (frozen|stuck|not responding)))?", t):
        if t.startswith(("force", "end task", "terminate")) or re.search(r"frozen|stuck|not responding", t):
            return _ctl(lambda: control.processes("kill", m.group("n")), "processes")
    if re.fullmatch(r"(what('s| is) running|list (the )?(running )?(apps|programs|processes)|what('s| is) using (the most )?(my )?(memory|ram|cpu)|(show|list) processes)", t):
        return _ctl(lambda: control.processes("list"), "processes")
    if re.fullmatch(r"restart (windows )?explorer|fix (the |my )?taskbar|restart (the )?taskbar", t):
        return _ctl(lambda: control.processes("restart_explorer"), "processes")

    if re.fullmatch(r"(what('s| is) (in|on) (my |the )?clipboard|read (my |the )?clipboard|what did i copy)", t):
        return _ctl(lambda: control.clipboard("read"), "clipboard")
    if re.fullmatch(r"clear (my |the )?clipboard", t):
        return _ctl(lambda: control.clipboard("clear"), "clipboard")

    if m := re.fullmatch(r"(what('s| is) on|read|describe|look at|what do you see on) (my |the |this )?(screen|window|page)( (in|of) (?P<app>[\w .'+-]+))?", t):
        def say() -> str:
            s = control.read_screen(m.group("app") or "")
            ctrls = [c for c in s["controls"] if not c.startswith(("toolbar", "menu bar"))]
            parts = [f"In {s['window']}"]
            if ctrls:
                parts.append("I can see " + "; ".join(ctrls[:8]))
            if s["text"]:
                parts.append("The text includes: " + " ".join(s["text"][:4])[:300])
            return ". ".join(parts) + "."
        return _ctl(say, "read_screen")
    if (m := re.fullmatch(r"(?P<a>double click|double-click|right click|right-click|click|tap|press|tick|untick) (on )?(the )?(?P<x>.+?)( button| link| tab| checkbox| box| option| menu)?( (in|on) (the )?(?P<app>[\w .'+-]+?)( window| app)?)?", t)) \
            and (m.group("a") not in ("press", "tap") or re.search(r"\b(button|link|tab|checkbox|box|option|menu)\b", t)):
        action = {"double click": "double_click", "double-click": "double_click", "right click": "right_click",
                  "right-click": "right_click"}.get(m.group("a"), "click")
        want = re.search(re.escape(m.group("x")), raw, re.I)
        app = m.group("app") or ""
        if app:
            try:
                control.pick_window(app)
            except Refused:
                want, app = re.search(re.escape(m.group("x") + " " + t.split(m.group("x"), 1)[1].strip()), raw, re.I), ""
        label = want.group(0) if want else m.group("x")
        return _ctl(lambda: control.click_on(label, app, action), "click_on")
    return None


def _plan(make, tool: str) -> dict:
    try:
        return {"text": "", "tool": tool, "cards": {}, "plan": make()}
    except Refused as e:
        return _r(str(e), tool)


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

    # Phone first, so "... on my phone" never reaches the laptop's commands.
    if (r := _phone(t, raw)) is not None:
        return r

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
        return _plan(files.plan_empty_recycle_bin, "recycle_bin")

    if m := re.fullmatch(r"(ask|tell|get|have|let) (?P<who>claude|gemini|chatgpt|chat gpt|gpt|codex)( code| cli)?( to| about|,|:)? (?P<q>.+)", t):
        who = m.group("who").replace(" ", "")
        q = re.search(rf"(?:{m.group('who')})(?: code| cli)?(?: to| about|,|:)? (.+)$", raw, re.I)
        q = q.group(1).strip() if q else m.group("q")
        if re.search(r"\b(run|execute|test it|and run|try it)\b", m.group("q")):
            return _plan(lambda: coders.ai_code_job(q, who), "ai_code_job")
        try:
            return _r(coders.ask_ai(q, who), "ask_ai")
        except Refused as e:
            return _r(str(e), "ask_ai")

    # Changes that need the admin voice: these only plan; brain.py asks for approval.
    if m := re.fullmatch(r"(uninstall|remove|delete) (the )?(app |application |program )?(?P<name>[\w\s.'+&-]+?)( app| application| program)?( from (my |the |this )?(pc|computer|laptop))?", t):
        if m.group(1) == "uninstall" or m.group(3) or m.group(5):
            return _plan(lambda: files.uninstall_app(m.group("name")), "uninstall_app")
    if m := re.fullmatch(r"(create|make|add) (a )?(new )?folder (called |named )?(?P<name>[\w\s.'-]+?)( (on|in|inside) (the |my )?(?P<loc>desktop|documents|downloads|pictures|music|videos|home)( folder)?)?", t):
        name = re.search(re.escape(m.group("name")), raw, re.I)  # keep the user's capitals
        return _plan(lambda: files.create_folder(name.group(0) if name else m.group("name"), m.group("loc") or "documents"),
                     "create_folder")
    if m := re.fullmatch(r"(write|create|make|code) (me )?(a |an )?(?P<lang>python|javascript|powershell|html|java|c\+\+|go|bash|batch|sql|react)? ?(script|program|code|file|app) (that |which |to |for )(?P<task>.+)", t):
        task = re.search(r"(?:that|which|to|for) (.+)$", raw, re.I)
        return _plan(lambda: files.write_script(task.group(1) if task else m.group("task"), m.group("lang") or "python"),
                     "write_script")
    if m := re.fullmatch(r"(delete|remove|trash) (the |my )?(file|folder|document|photo|picture|video) (called |named )?(?P<name>.+)", t):
        found = system.find_files(m.group("name"))
        if isinstance(found, str):
            return _r(f"I couldn't find a file called {m.group('name')}.", "find_files")
        want = m.group("name").lower()
        kind = m.group(3)
        found = [f for f in found if (kind == "folder") == bool(f.get("folder"))] if kind in ("file", "folder") else found
        if not found:
            return _r(f"I couldn't find a {kind} called {m.group('name')}.", "find_files")
        exact = [f for f in found if f["name"].lower().rstrip("\\").split(".")[0] == want or f["name"].lower().rstrip("\\") == want]
        if len(found) == 1 or len(exact) == 1:
            target = (exact or found)[0]["path"]
            return _plan(lambda: files.delete_path(target), "delete_path")
        return _r(f"I found {len(found)} matches. Tap Delete next to the one you mean.", "find_files", files=found)
    if re.search(r"\b(recycle|recyle|recylce) ?bin\b", t):
        n = system.recycle_bin_count()
        return _r(f"The Recycle Bin has {n} item{'s' if n != 1 else ''}." if n else "The Recycle Bin is empty.", "recycle_bin")

    if m := re.fullmatch(r"(from now on |please )?(call me|address me as|my name is) (?P<name>[\w .'-]{1,30})", t):
        name = re.search(r"(?:call me|address me as|my name is) (.+)", raw, re.I).group(1).strip(" .!")
        db.save_settings({"user_name" if "name is" in t else "honorific": name})
        return _r(f"Very well, {name}." if "name is" not in t else f"Nice to meet you, {name}.", "settings")
    if re.fullmatch(r"(no|nope|yes|yeah|yep|ok|okay|sure|fine|alright|all right|got it|cool|hmm+|um+|uh+)", t):
        return _r(f"Very good, {you()}.", "chat")


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

    if (r := _control(t, raw)) is not None:
        return r

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
