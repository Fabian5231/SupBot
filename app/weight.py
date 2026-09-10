"""Tagesgewicht: speichern, auswerten, Fotos aufraeumen.

Ein Eintrag je Tag. Wer denselben Tag erneut speichert, ueberschreibt ihn.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from . import config, db

DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IMAGE_NAME_RE = re.compile(r"^[0-9a-f]{32}\.jpg$")


def _stamp() -> str:
    return db.now().isoformat(timespec="seconds")


def valid_day(value: str | None) -> str:
    """Leere Eingabe wird zu heute, ungueltige loest ValueError aus."""
    if not value:
        return db.today().isoformat()
    if not DAY_RE.match(value):
        raise ValueError("Datum muss YYYY-MM-DD sein")
    date.fromisoformat(value)
    return value


# --------------------------------------------------------------------------
# Datenzugriff
# --------------------------------------------------------------------------
def save(
    day: str,
    weight_kg: float,
    *,
    source: str = "manual",
    ocr_raw: str | None = None,
    ocr_confidence: float | None = None,
    image_path: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    now = _stamp()
    db.execute(
        """
        INSERT INTO measurements
            (day, weight_kg, source, ocr_raw, ocr_confidence, image_path,
             note, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(day) DO UPDATE SET
            weight_kg      = excluded.weight_kg,
            source         = excluded.source,
            ocr_raw        = COALESCE(excluded.ocr_raw, measurements.ocr_raw),
            ocr_confidence = COALESCE(excluded.ocr_confidence, measurements.ocr_confidence),
            image_path     = COALESCE(excluded.image_path, measurements.image_path),
            note           = excluded.note,
            updated_at     = excluded.updated_at
        """,
        (
            day,
            round(float(weight_kg), 2),
            source,
            ocr_raw,
            ocr_confidence,
            image_path,
            note,
            now,
            now,
        ),
    )
    return get_day(day) or {}


def list_all(since: str | None = None) -> list[dict[str, Any]]:
    if since:
        rows = db.query(
            "SELECT * FROM measurements WHERE day >= ? ORDER BY day ASC", (since,)
        )
    else:
        rows = db.query("SELECT * FROM measurements ORDER BY day ASC")
    return [dict(row) for row in rows]


def get_day(day: str) -> dict[str, Any] | None:
    row = db.one("SELECT * FROM measurements WHERE day = ?", (day,))
    return dict(row) if row else None


def latest() -> dict[str, Any] | None:
    row = db.one("SELECT * FROM measurements ORDER BY day DESC LIMIT 1")
    return dict(row) if row else None


def delete_day(day: str) -> bool:
    row = get_day(day)
    if not row:
        return False
    if row.get("image_path"):
        (config.IMAGE_DIR / row["image_path"]).unlink(missing_ok=True)
    db.execute("DELETE FROM measurements WHERE day = ?", (day,))
    return True


# --------------------------------------------------------------------------
# Statistik
# --------------------------------------------------------------------------
def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {"count": 0}

    values = [(date.fromisoformat(r["day"]), float(r["weight_kg"])) for r in rows]
    values.sort()
    newest_day, newest_weight = values[-1]

    def window_avg(days: int) -> float | None:
        cutoff = newest_day - timedelta(days=days - 1)
        picked = [w for d, w in values if d >= cutoff]
        return sum(picked) / len(picked) if picked else None

    def closest_before(days: int) -> float | None:
        target = newest_day - timedelta(days=days)
        earlier = [(d, w) for d, w in values if d <= target]
        return earlier[-1][1] if earlier else None

    weights = [w for _, w in values]
    avg7 = window_avg(7)
    avg30 = window_avg(30)
    ref7 = closest_before(7)
    ref30 = closest_before(30)

    return {
        "count": len(values),
        "latest": round(newest_weight, 2),
        "latest_day": newest_day.isoformat(),
        "first_day": values[0][0].isoformat(),
        "avg7": round(avg7, 2) if avg7 is not None else None,
        "avg30": round(avg30, 2) if avg30 is not None else None,
        "delta7": round(newest_weight - ref7, 2) if ref7 is not None else None,
        "delta30": round(newest_weight - ref30, 2) if ref30 is not None else None,
        "min": round(min(weights), 2),
        "max": round(max(weights), 2),
        "total_change": round(newest_weight - weights[0], 2),
    }


# --------------------------------------------------------------------------
# Fotos
# --------------------------------------------------------------------------
def cleanup_orphan_images() -> None:
    """Fotos loeschen, die nach 24 Stunden noch zu keinem Eintrag gehoeren."""
    if not config.IMAGE_DIR.is_dir():
        return
    referenced = {row["image_path"] for row in list_all() if row.get("image_path")}
    cutoff = datetime.now().timestamp() - 24 * 3600
    for path in config.IMAGE_DIR.glob("*.jpg"):
        if path.name in referenced:
            continue
        if path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
