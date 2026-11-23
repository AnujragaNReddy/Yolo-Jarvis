import re
import time
from datetime import datetime, timedelta

from .. import db
from . import tool


def pending() -> list[dict]:
    return db.q("SELECT id, text, due, kind FROM reminders WHERE fired=0 ORDER BY due")


def due_now() -> list[dict]:
    rows = db.q("SELECT id, text, due, kind FROM reminders WHERE fired=0 AND due<=?", time.time())
    for r in rows:
        db.x("UPDATE reminders SET fired=1 WHERE id=?", r["id"])
    return rows


def _span(seconds: float) -> str:
    seconds = round(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h} hour{'s' if h != 1 else ''}" if h else "", f"{m} minute{'s' if m != 1 else ''}" if m else "",
             f"{s} second{'s' if s != 1 else ''}" if s and not h else ""]
    return " ".join(p for p in parts if p) or "0 seconds"


def parse_clock(text: str) -> datetime | None:
    """'5pm', '17:30', '7:05 am', 'noon' -> the next time that clock time comes round."""
    t = text.strip().lower().replace(".", "")
    if t in ("noon", "midday"):
        h, m = 12, 0
    elif t == "midnight":
        h, m = 0, 0
    else:
        g = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
        if not g:
            return None
        h, m, ampm = int(g.group(1)), int(g.group(2) or 0), g.group(3)
        if ampm == "pm" and h < 12:
            h += 12
        if ampm == "am" and h == 12:
            h = 0
        if h > 23 or m > 59:
            return None
    now = datetime.now()
    at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


@tool("Start a countdown timer.", minutes="number?: minutes", seconds="number?: extra seconds",
      label="string?: what it's for, e.g. pasta")
def set_timer(minutes: float = 0, seconds: float = 0, label: str = "") -> str:
    total = float(minutes or 0) * 60 + float(seconds or 0)
    if total <= 0:
        return "How long should the timer be?"
    text = label.strip() or f"{_span(total)} timer"
    db.x("INSERT INTO reminders(text, due, kind, created) VALUES(?, ?, 'timer', ?)", text, time.time() + total, time.time())
    return f"Timer set for {_span(total)}."


@tool("Remind the user about something later, either after a delay or at a clock time.",
      text="string: what to remind them about", in_minutes="number?: minutes from now",
      at="string?: clock time today/tomorrow like 5pm or 17:30")
def set_reminder(text: str, in_minutes: float | None = None, at: str = "") -> str:
    if at:
        when = parse_clock(at)
        if not when:
            return f"I didn't understand the time {at}."
        due = when.timestamp()
    elif in_minutes:
        due = time.time() + float(in_minutes) * 60
    else:
        return "When should I remind you?"
    db.x("INSERT INTO reminders(text, due, kind, created) VALUES(?, ?, 'reminder', ?)", text.strip(), due, time.time())
    when_text = datetime.fromtimestamp(due).strftime("%I:%M %p").lstrip("0")
    return f"I'll remind you to {text.strip()} at {when_text}."


@tool("List active timers and reminders.")
def list_reminders() -> list[dict] | str:
    rows = pending()
    return [{"id": r["id"], "kind": r["kind"], "text": r["text"],
             "at": datetime.fromtimestamp(r["due"]).strftime("%a %I:%M %p"),
             "in": _span(max(0, r["due"] - time.time()))} for r in rows] or "Nothing scheduled."


@tool("Cancel timers or reminders.", which="string: words from the reminder, 'timer', or 'all'")
def cancel_reminder(which: str) -> str:
    rows = pending()
    w = which.lower().strip()
    hits = rows if w in ("all", "everything") else [r for r in rows if w in r["text"].lower() or w == r["kind"]
                                                    or w == str(r["id"])]
    if w in ("timer", "the timer") and not hits:
        hits = [r for r in rows if r["kind"] == "timer"]
    for r in hits:
        db.x("UPDATE reminders SET fired=1 WHERE id=?", r["id"])
    return f"Cancelled {len(hits)}." if hits else "Nothing matched."
