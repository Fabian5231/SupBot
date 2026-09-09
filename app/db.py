"""SQLite-Zugriff: Verbindung pro Thread, Schema-Setup, Settings-Helfer."""
from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime
from typing import Any

from . import config

_local = threading.local()
_write_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS slots (
    id       INTEGER PRIMARY KEY,
    name     TEXT    NOT NULL,
    time     TEXT    NOT NULL,               -- 'HH:MM'
    sort     INTEGER NOT NULL DEFAULT 0,
    active   INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS supplements (
    id         INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    brand      TEXT    NOT NULL DEFAULT '',
    unit       TEXT    NOT NULL DEFAULT 'Kapsel',
    notes      TEXT    NOT NULL DEFAULT '',
    url        TEXT    NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL
);

-- Welches Supplement wird in welchem Slot in welcher Menge genommen
CREATE TABLE IF NOT EXISTS plan (
    id            INTEGER PRIMARY KEY,
    supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
    slot_id       INTEGER NOT NULL REFERENCES slots(id)       ON DELETE CASCADE,
    amount        REAL    NOT NULL DEFAULT 1,
    weekdays      TEXT    NOT NULL DEFAULT '1234567',         -- ISO-Wochentage
    UNIQUE (supplement_id, slot_id)
);

-- Gekaufte Packungen / Artikel
CREATE TABLE IF NOT EXISTS packages (
    id            INTEGER PRIMARY KEY,
    supplement_id INTEGER NOT NULL REFERENCES supplements(id) ON DELETE CASCADE,
    label         TEXT    NOT NULL DEFAULT '',
    units_total   REAL    NOT NULL,
    units_left    REAL    NOT NULL,
    price         REAL,
    status        TEXT    NOT NULL DEFAULT 'sealed',          -- sealed | open | empty
    bought_at     TEXT,
    opened_at     TEXT,
    created_at    TEXT    NOT NULL
);

-- Eine Zeile je (Tag, Slot, Supplement)
CREATE TABLE IF NOT EXISTS intakes (
    id            INTEGER PRIMARY KEY,
    day           TEXT    NOT NULL,                           -- YYYY-MM-DD
    slot_id       INTEGER NOT NULL,
    supplement_id INTEGER NOT NULL,
    amount        REAL    NOT NULL DEFAULT 1,
    taken         INTEGER NOT NULL DEFAULT 0,
    taken_at      TEXT,
    source        TEXT,                                       -- web | telegram
    UNIQUE (day, slot_id, supplement_id)
);
CREATE INDEX IF NOT EXISTS idx_intakes_day ON intakes(day);

-- Status der Erinnerungen je (Tag, Slot)
CREATE TABLE IF NOT EXISTS reminders (
    day           TEXT    NOT NULL,
    slot_id       INTEGER NOT NULL,
    sent_count    INTEGER NOT NULL DEFAULT 0,
    last_sent     TEXT,
    snooze_until  TEXT,
    PRIMARY KEY (day, slot_id)
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "reminder_interval_min": "30",   # Abstand zwischen den Nervnachrichten
    "max_reminders": "24",           # Sicherheitsnetz pro Slot und Tag
    "codewords": "genommen,erledigt,done,fertig,ok,jo,x,✅",
    "quiet_from": "22:30",           # ab hier keine Wiederholungen mehr
    "quiet_to": "06:30",
    "low_stock_days": "10",          # Warnung wenn Reichweite darunter faellt
    "chat_id": "",
    "update_offset": "0",
    "last_stock_warning": "",
}

DEFAULT_SLOTS = [
    ("Morgens", "08:00", 10),
    ("Mittags", "13:00", 20),
    ("Abends", "20:00", 30),
]


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        _local.conn = conn
    return conn


def query(sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
    return connect().execute(sql, params).fetchall()


def one(sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
    return connect().execute(sql, params).fetchone()


def execute(sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
    with _write_lock:
        return connect().execute(sql, params)


def init_db() -> None:
    conn = connect()
    with _write_lock:
        conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )
        if conn.execute("SELECT COUNT(*) AS c FROM slots").fetchone()["c"] == 0:
            conn.executemany(
                "INSERT INTO slots(name, time, sort, active) VALUES (?, ?, ?, 1)",
                DEFAULT_SLOTS,
            )
    if config.TELEGRAM_CHAT_ID and not get_setting("chat_id"):
        set_setting("chat_id", config.TELEGRAM_CHAT_ID)


def get_setting(key: str, default: str = "") -> str:
    row = one("SELECT value FROM settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: Any) -> None:
    execute(
        "INSERT INTO settings(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def get_int_setting(key: str, default: int) -> int:
    try:
        return int(float(get_setting(key, str(default))))
    except (TypeError, ValueError):
        return default


def now() -> datetime:
    return datetime.now(config.TZ)


def today() -> date:
    return now().date()
