"""Zentrale (eigene App, app.fabian-social.dev): Erinnerungen als Push mit Knoepfen.

Jede Nachricht geht zuerst an die Zentrale. Deren Antwort enthaelt den
Telegram-Schalter aus der App - steht er auf aus, schickt der Bot nichts an
Telegram. Ist die Zentrale nicht eingerichtet oder nicht erreichbar, laeuft
alles wie bisher ueber Telegram.

Knoepfe in der Benachrichtigung meldet die Zentrale per
POST /zentrale/aktion zurueck (Bearer = dasselbe Token).
"""
from __future__ import annotations

import hmac
import logging

import httpx

from . import config

log = logging.getLogger("supbot.zentrale")

client = httpx.AsyncClient(timeout=10)


def enabled() -> bool:
    return bool(config.ZENTRALE_TOKEN)


def token_ok(authorization: str | None) -> bool:
    scheme, _, token = (authorization or "").partition(" ")
    return enabled() and scheme.lower() == "bearer" and hmac.compare_digest(
        token.strip().encode(), config.ZENTRALE_TOKEN.encode()
    )


async def notify(title: str, body: str = "", *, tag: str | None = None, priority: str = "normal",
                 actions: list | None = None) -> dict | None:
    """Antwort der Zentrale, None = nicht eingerichtet oder nicht erreichbar."""
    if not enabled():
        return None
    message = {"title": title, "body": body, "priority": priority}
    if tag:
        message["tag"] = tag
    if actions:
        message["actions"] = actions
    try:
        response = await client.post(
            f"{config.ZENTRALE_URL}/api/notify",
            json=message,
            headers={"Authorization": f"Bearer {config.ZENTRALE_TOKEN}"},
        )
    except httpx.HTTPError as exc:
        log.warning("Zentrale nicht erreichbar: %s", exc)
        return None
    if response.status_code != 200:
        log.warning("Zentrale lehnt ab (%s): %s", response.status_code, response.text[:200])
        return None
    return response.json()


def telegram_wanted(answer: dict | None) -> bool:
    """Telegram nur, wenn die Zentrale es nicht ausdruecklich abgeschaltet hat."""
    return answer is None or answer.get("telegram") is not False
