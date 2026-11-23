import sqlite3
import threading
import time

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY, fact TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, text TEXT NOT NULL, due REAL NOT NULL,
  kind TEXT NOT NULL DEFAULT 'reminder', fired INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS history (id INTEGER PRIMARY KEY, role TEXT NOT NULL, content TEXT NOT NULL,
  tools TEXT, ts REAL NOT NULL, session INTEGER NOT NULL DEFAULT 0);
"""


def conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(SCHEMA)
    return _conn


def q(sql: str, *args) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def x(sql: str, *args) -> int:
    with _lock:
        c = conn().execute(sql, args)
        conn().commit()
        return c.lastrowid


def settings() -> dict:
    stored = {r["key"]: r["value"] for r in q("SELECT key, value FROM settings")}
    return {**config.DEFAULTS, **stored}


def setting(key: str) -> str:
    return settings().get(key, "")


def save_settings(values: dict) -> dict:
    for k, v in values.items():
        if k in config.DEFAULTS:
            x("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", k, str(v))
    return settings()


def session() -> int:
    return int(settings().get("session", "0") or 0)


def new_session() -> int:
    s = session() + 1
    x("INSERT INTO settings(key, value) VALUES('session', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", str(s))
    return s


def add_history(role: str, content: str, tools: str | None = None) -> None:
    x("INSERT INTO history(role, content, tools, ts, session) VALUES(?, ?, ?, ?, ?)", role, content, tools, time.time(), session())


def recent_history(limit: int) -> list[dict]:
    rows = q("SELECT role, content, tools, ts FROM history WHERE session=? ORDER BY id DESC LIMIT ?", session(), limit)
    return rows[::-1]
