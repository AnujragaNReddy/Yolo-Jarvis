import time

from .. import db
from . import tool


def facts() -> list[dict]:
    return db.q("SELECT id, fact, created FROM memories ORDER BY id")


@tool("Remember a lasting fact about the user or their preferences (name, birthday, favourite team, where things are...).",
      fact="string: the fact, written as a full sentence about the user")
def remember(fact: str) -> str:
    fact = fact.strip().rstrip(".") + "."
    existing = {f["fact"].lower() for f in facts()}
    if fact.lower() not in existing:
        db.x("INSERT INTO memories(fact, created) VALUES(?, ?)", fact, time.time())
    return f"Remembered: {fact}"


@tool("Forget remembered facts that mention something.", about="string: word or phrase to match")
def forget(about: str) -> str:
    hits = [f for f in facts() if about.lower() in f["fact"].lower()]
    for f in hits:
        db.x("DELETE FROM memories WHERE id=?", f["id"])
    return f"Forgot {len(hits)} thing{'s' if len(hits) != 1 else ''}." if hits else f"I had nothing about {about}."


def notes(include_done: bool = False) -> list[dict]:
    sql = "SELECT id, text, done, created FROM notes" + ("" if include_done else " WHERE done=0") + " ORDER BY id"
    return db.q(sql)


@tool("Add an item to the user's to-do list or notes.", text="string: the item")
def add_note(text: str) -> str:
    db.x("INSERT INTO notes(text, created) VALUES(?, ?)", text.strip(), time.time())
    return f"Added to your list: {text.strip()}"


@tool("Read the user's to-do list / notes.")
def list_notes() -> list[dict] | str:
    items = notes()
    return [{"id": n["id"], "text": n["text"]} for n in items] or "The list is empty."


@tool("Tick off (complete) a to-do item.", item="string: words from the item, or its id number")
def complete_note(item: str) -> str:
    items = notes()
    hit = next((n for n in items if str(n["id"]) == str(item).strip()), None) or \
        next((n for n in items if str(item).lower() in n["text"].lower()), None)
    if not hit:
        return f"Nothing on the list matches {item}."
    db.x("UPDATE notes SET done=1 WHERE id=?", hit["id"])
    return f"Done: {hit['text']}"
