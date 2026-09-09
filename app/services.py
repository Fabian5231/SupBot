"""Fachlogik: Tagesplan, Abhaken, Vorratsverwaltung und Reichweite."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta

from . import db

WEEKDAY_NAMES = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]


# --------------------------------------------------------------------------
# Hilfsfunktionen
# --------------------------------------------------------------------------
def parse_hhmm(value: str, fallback: time | None = None) -> time:
    try:
        hour, _, minute = value.strip().partition(":")
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        if fallback is None:
            raise
        return fallback


def fmt_amount(value: float) -> str:
    """1.0 -> 1, 1.5 -> 1,5"""
    if value == int(value):
        return str(int(value))
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def weekdays_label(weekdays: str) -> str:
    if weekdays == "1234567":
        return "täglich"
    return ", ".join(WEEKDAY_NAMES[int(d) - 1] for d in weekdays if d.isdigit())


# --------------------------------------------------------------------------
# Tagesplan
# --------------------------------------------------------------------------
def ensure_day(day: date) -> None:
    """Legt die Einnahme-Zeilen fuer einen Tag aus dem Plan an bzw. raeumt auf.

    Bereits abgehakte Zeilen bleiben unangetastet, damit die Historie stabil ist.
    """
    iso = str(day.isoweekday())
    day_s = day.isoformat()

    db.execute(
        """
        INSERT OR IGNORE INTO intakes (day, slot_id, supplement_id, amount, taken)
        SELECT ?, p.slot_id, p.supplement_id, p.amount, 0
        FROM plan p
        JOIN supplements s ON s.id = p.supplement_id
        JOIN slots sl      ON sl.id = p.slot_id
        WHERE s.active = 1 AND sl.active = 1 AND instr(p.weekdays, ?) > 0
        """,
        (day_s, iso),
    )
    # Menge offener Einnahmen an den aktuellen Plan angleichen
    db.execute(
        """
        UPDATE intakes
           SET amount = (SELECT p.amount FROM plan p
                          WHERE p.slot_id = intakes.slot_id
                            AND p.supplement_id = intakes.supplement_id)
         WHERE day = ? AND taken = 0
           AND EXISTS (SELECT 1 FROM plan p
                        WHERE p.slot_id = intakes.slot_id
                          AND p.supplement_id = intakes.supplement_id)
        """,
        (day_s,),
    )
    # Nicht mehr geplante, noch offene Zeilen entfernen
    db.execute(
        """
        DELETE FROM intakes
         WHERE day = ? AND taken = 0
           AND NOT EXISTS (
               SELECT 1 FROM plan p
               JOIN supplements s ON s.id = p.supplement_id
               JOIN slots sl      ON sl.id = p.slot_id
               WHERE p.supplement_id = intakes.supplement_id
                 AND p.slot_id = intakes.slot_id
                 AND s.active = 1 AND sl.active = 1
                 AND instr(p.weekdays, ?) > 0
           )
        """,
        (day_s, iso),
    )


def day_plan(day: date) -> list[dict]:
    """Slots des Tages inkl. Einnahmen, chronologisch sortiert."""
    if day >= db.today():
        ensure_day(day)

    rows = db.query(
        """
        SELECT i.id, i.slot_id, i.supplement_id, i.amount, i.taken, i.taken_at, i.source,
               sl.name AS slot_name, sl.time AS slot_time, sl.sort AS slot_sort,
               s.name AS supplement_name, s.unit, s.brand
          FROM intakes i
          JOIN slots sl      ON sl.id = i.slot_id
          JOIN supplements s ON s.id = i.supplement_id
         WHERE i.day = ?
         ORDER BY sl.time, sl.sort, s.name COLLATE NOCASE
        """,
        (day.isoformat(),),
    )

    slots: dict[int, dict] = {}
    for row in rows:
        slot = slots.setdefault(
            row["slot_id"],
            {
                "slot_id": row["slot_id"],
                "name": row["slot_name"],
                "time": row["slot_time"],
                "items": [],
            },
        )
        slot["items"].append(dict(row))

    result = []
    for slot in slots.values():
        slot["total"] = len(slot["items"])
        slot["done"] = sum(1 for i in slot["items"] if i["taken"])
        slot["open"] = slot["total"] - slot["done"]
        slot["complete"] = slot["open"] == 0
        result.append(slot)
    result.sort(key=lambda s: (s["time"], s["name"]))
    return result


def day_stats(day: date) -> dict:
    row = db.one(
        "SELECT COUNT(*) AS total, COALESCE(SUM(taken), 0) AS done "
        "FROM intakes WHERE day = ?",
        (day.isoformat(),),
    )
    total, done = row["total"], row["done"]
    return {
        "total": total,
        "done": done,
        "open": total - done,
        "percent": round(done * 100 / total) if total else 100,
    }


def due_slots(now: datetime) -> list[dict]:
    """Slots von heute, deren Uhrzeit erreicht ist und die noch offen sind."""
    out = []
    for slot in day_plan(now.date()):
        if slot["complete"]:
            continue
        if parse_hhmm(slot["time"], time(0, 0)) <= now.time():
            out.append(slot)
    return out


# --------------------------------------------------------------------------
# Abhaken
# --------------------------------------------------------------------------
def set_intake(intake_id: int, taken: bool, source: str = "web") -> sqlite3.Row | None:
    row = db.one("SELECT * FROM intakes WHERE id = ?", (intake_id,))
    if row is None or bool(row["taken"]) == taken:
        return row
    if taken:
        db.execute(
            "UPDATE intakes SET taken = 1, taken_at = ?, source = ? WHERE id = ?",
            (db.now().isoformat(timespec="seconds"), source, intake_id),
        )
        consume_stock(row["supplement_id"], row["amount"])
    else:
        db.execute(
            "UPDATE intakes SET taken = 0, taken_at = NULL, source = NULL WHERE id = ?",
            (intake_id,),
        )
        restore_stock(row["supplement_id"], row["amount"])
    return db.one("SELECT * FROM intakes WHERE id = ?", (intake_id,))


def complete_slot(day: date, slot_id: int, source: str = "telegram") -> list[str]:
    """Hakt alle offenen Einnahmen eines Slots ab, gibt die Namen zurueck."""
    rows = db.query(
        """
        SELECT i.id, s.name
          FROM intakes i JOIN supplements s ON s.id = i.supplement_id
         WHERE i.day = ? AND i.slot_id = ? AND i.taken = 0
        """,
        (day.isoformat(), slot_id),
    )
    for row in rows:
        set_intake(row["id"], True, source)
    return [row["name"] for row in rows]


def complete_all_due(now: datetime, source: str = "telegram") -> list[tuple[str, list[str]]]:
    """Hakt alle faelligen, offenen Slots des Tages ab."""
    done = []
    for slot in due_slots(now):
        names = complete_slot(now.date(), slot["slot_id"], source)
        if names:
            done.append((slot["name"], names))
    return done


# --------------------------------------------------------------------------
# Vorrat
# --------------------------------------------------------------------------
def consume_stock(supplement_id: int, amount: float) -> float:
    """Bucht die Menge von den Packungen ab. Rueckgabe: ungedeckter Rest."""
    remaining = float(amount)
    rows = db.query(
        """
        SELECT * FROM packages
         WHERE supplement_id = ? AND status IN ('open', 'sealed') AND units_left > 0
         ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,
                  COALESCE(opened_at, bought_at, created_at), id
        """,
        (supplement_id,),
    )
    stamp = db.now().isoformat(timespec="seconds")
    for pkg in rows:
        if remaining <= 0:
            break
        take = min(pkg["units_left"], remaining)
        left = round(pkg["units_left"] - take, 4)
        db.execute(
            "UPDATE packages SET units_left = ?, status = ?, opened_at = COALESCE(opened_at, ?) "
            "WHERE id = ?",
            (left, "empty" if left <= 0 else "open", stamp, pkg["id"]),
        )
        remaining -= take
    return round(remaining, 4)


def restore_stock(supplement_id: int, amount: float) -> None:
    """Macht consume_stock rueckgaengig (zuletzt angebrochene Packung zuerst)."""
    remaining = float(amount)
    rows = db.query(
        """
        SELECT * FROM packages
         WHERE supplement_id = ? AND status IN ('open', 'empty')
         ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END,
                  COALESCE(opened_at, bought_at, created_at) DESC, id DESC
        """,
        (supplement_id,),
    )
    for pkg in rows:
        if remaining <= 0:
            break
        space = pkg["units_total"] - pkg["units_left"]
        give = min(space, remaining)
        if give <= 0:
            continue
        left = round(pkg["units_left"] + give, 4)
        db.execute(
            "UPDATE packages SET units_left = ?, status = ? WHERE id = ?",
            (left, "open" if left < pkg["units_total"] else "sealed", pkg["id"]),
        )
        remaining -= give


def daily_usage(supplement_id: int) -> float:
    """Durchschnittlicher Verbrauch pro Tag (beruecksichtigt Wochentage)."""
    rows = db.query(
        """
        SELECT p.amount, p.weekdays
          FROM plan p JOIN slots sl ON sl.id = p.slot_id
         WHERE p.supplement_id = ? AND sl.active = 1
        """,
        (supplement_id,),
    )
    total = 0.0
    for row in rows:
        days = sum(1 for c in row["weekdays"] if c.isdigit())
        total += row["amount"] * days / 7
    return round(total, 6)


def stock_overview() -> list[dict]:
    """Pro Supplement: Vorrat, Tagesverbrauch, Reichweite, Datum leer-am."""
    supplements = db.query(
        "SELECT * FROM supplements ORDER BY active DESC, name COLLATE NOCASE"
    )
    today = db.today()
    out = []
    for sup in supplements:
        packages = db.query(
            """
            SELECT * FROM packages WHERE supplement_id = ?
             ORDER BY CASE status WHEN 'open' THEN 0 WHEN 'sealed' THEN 1 ELSE 2 END,
                      COALESCE(opened_at, bought_at, created_at) DESC, id DESC
            """,
            (sup["id"],),
        )
        stock = sum(p["units_left"] for p in packages if p["status"] != "empty")
        usage = daily_usage(sup["id"])
        days_left = stock / usage if usage > 0 else None
        empty_on = today + timedelta(days=int(days_left)) if days_left is not None else None
        out.append(
            {
                "supplement": dict(sup),
                "packages": [dict(p) for p in packages],
                "open_packages": [dict(p) for p in packages if p["status"] != "empty"],
                "stock": round(stock, 2),
                "usage": usage,
                "days_left": None if days_left is None else int(days_left),
                "empty_on": empty_on,
                "plan": [
                    dict(r)
                    for r in db.query(
                        """
                        SELECT p.amount, p.weekdays, sl.name AS slot_name, sl.time AS slot_time
                          FROM plan p JOIN slots sl ON sl.id = p.slot_id
                         WHERE p.supplement_id = ? ORDER BY sl.time
                        """,
                        (sup["id"],),
                    )
                ],
            }
        )
    out.sort(
        key=lambda e: (
            not e["supplement"]["active"],
            e["days_left"] if e["days_left"] is not None else 10**6,
            e["supplement"]["name"].lower(),
        )
    )
    return out


def low_stock(threshold_days: int) -> list[dict]:
    return [
        entry
        for entry in stock_overview()
        if entry["supplement"]["active"]
        and entry["usage"] > 0
        and entry["days_left"] is not None
        and entry["days_left"] <= threshold_days
    ]


# --------------------------------------------------------------------------
# Historie
# --------------------------------------------------------------------------
def history(days: int = 30) -> list[dict]:
    end = db.today()
    start = end - timedelta(days=days - 1)
    rows = db.query(
        """
        SELECT day, COUNT(*) AS total, COALESCE(SUM(taken), 0) AS done
          FROM intakes WHERE day BETWEEN ? AND ?
         GROUP BY day
        """,
        (start.isoformat(), end.isoformat()),
    )
    by_day = {row["day"]: row for row in rows}
    out = []
    for offset in range(days):
        current = start + timedelta(days=offset)
        row = by_day.get(current.isoformat())
        total = row["total"] if row else 0
        done = row["done"] if row else 0
        out.append(
            {
                "day": current,
                "total": total,
                "done": done,
                "percent": round(done * 100 / total) if total else None,
                "weekday": WEEKDAY_NAMES[current.weekday()],
            }
        )
    return out


def streak() -> int:
    """Anzahl aufeinanderfolgender vollstaendiger Tage (heute zaehlt nur wenn fertig)."""
    count = 0
    current = db.today()
    first = True
    while True:
        row = db.one(
            "SELECT COUNT(*) AS total, COALESCE(SUM(taken), 0) AS done "
            "FROM intakes WHERE day = ?",
            (current.isoformat(),),
        )
        complete = bool(row) and row["total"] > 0 and row["done"] >= row["total"]
        if complete:
            count += 1
        elif not first:
            break
        current -= timedelta(days=1)
        first = False
        if count > 3650:
            break
    return count
