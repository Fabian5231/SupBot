"""Telegram-Bot: nervt so lange, bis das Codewort zurueckkommt.

Laeuft als asyncio-Task im selben Prozess wie die Weboberflaeche:
  * poll_loop()      - long polling auf getUpdates, verarbeitet Nachrichten/Buttons
  * reminder_loop()  - prueft jede Minute, welcher Slot faellig und offen ist
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import date, datetime, time, timedelta

import httpx

from . import config, db, services

log = logging.getLogger("supbot.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30
CHECK_INTERVAL = 30  # Sekunden zwischen zwei Reminder-Pruefungen

HELP = (
    "<b>SupBot</b>\n\n"
    "Ich erinnere dich an deine Supplemente und höre erst auf, wenn du "
    "das Codewort zurückschreibst.\n\n"
    "<b>Befehle</b>\n"
    "/heute – Tagesplan mit Status\n"
    "/status – dasselbe, kurz\n"
    "/vorrat – Reichweite aller Packungen\n"
    "/genommen – alles Fällige abhaken\n"
    "/snooze – 30 Minuten Ruhe\n"
    "/id – deine Chat-ID\n"
    "/hilfe – diese Übersicht\n\n"
    "Oder schreib einfach eines deiner Codewörter."
)


# --------------------------------------------------------------------------
# API-Grundlagen
# --------------------------------------------------------------------------
class Telegram:
    def __init__(self, token: str) -> None:
        self.token = token
        self.client = httpx.AsyncClient(timeout=POLL_TIMEOUT + 15)

    async def call(self, method: str, **params) -> dict | None:
        if not self.token:
            return None
        try:
            response = await self.client.post(
                API.format(token=self.token, method=method), json=params
            )
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("Telegram-Aufruf %s fehlgeschlagen: %s", method, exc)
            return None
        if not data.get("ok"):
            log.warning("Telegram-Fehler bei %s: %s", method, data.get("description"))
            return None
        return data.get("result")

    async def send(self, chat_id: str | int, text: str, keyboard: list | None = None):
        params = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if keyboard:
            params["reply_markup"] = {"inline_keyboard": keyboard}
        return await self.call("sendMessage", **params)

    async def answer_callback(self, callback_id: str, text: str = ""):
        return await self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    async def edit_markup(self, chat_id, message_id: int, keyboard: list | None = None):
        return await self.call(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup={"inline_keyboard": keyboard or []},
        )

    async def aclose(self) -> None:
        await self.client.aclose()


bot = Telegram(config.TELEGRAM_TOKEN)


def chat_id() -> str:
    return db.get_setting("chat_id") or config.TELEGRAM_CHAT_ID


def enabled() -> bool:
    return bool(config.TELEGRAM_TOKEN)


# --------------------------------------------------------------------------
# Textbausteine
# --------------------------------------------------------------------------
def esc(value) -> str:
    return html.escape(str(value))


def slot_message(slot: dict, nag: int = 0) -> str:
    open_items = [i for i in slot["items"] if not i["taken"]]
    head = "⏰" if nag == 0 else "🔁"
    lines = [f"{head} <b>{esc(slot['name'])}</b> – {esc(slot['time'])} Uhr"]
    if nag:
        lines[0] += f"  <i>(Erinnerung {nag + 1})</i>"
    lines.append("")
    for item in open_items:
        amount = services.fmt_amount(item["amount"])
        lines.append(f"• {esc(item['supplement_name'])} – {amount} {esc(item['unit'])}")
    done = [i for i in slot["items"] if i["taken"]]
    if done:
        lines.append("")
        lines.append("<i>schon erledigt: " + esc(", ".join(i["supplement_name"] for i in done)) + "</i>")
    lines.append("")
    lines.append('Antworte mit <b>„genommen“</b> oder tippe unten.')
    return "\n".join(lines)


def slot_keyboard(slot_id: int, items: list[dict]) -> list:
    rows = [
        [
            {"text": "✅ Genommen", "callback_data": f"slot:{slot_id}"},
            {"text": "😴 30 Min", "callback_data": f"snooze:{slot_id}:30"},
        ]
    ]
    open_items = [i for i in items if not i["taken"]]
    if len(open_items) > 1:
        for item in open_items:
            rows.append(
                [
                    {
                        "text": f"✔ nur {item['supplement_name']}",
                        "callback_data": f"item:{item['id']}",
                    }
                ]
            )
    return rows


def today_text(day: date | None = None) -> str:
    day = day or db.today()
    plan = services.day_plan(day)
    if not plan:
        return "Für heute ist nichts geplant. Leg dir in der Weboberfläche einen Plan an."
    lines = [f"📋 <b>Plan für {day.strftime('%d.%m.%Y')}</b>", ""]
    for slot in plan:
        mark = "✅" if slot["complete"] else "⬜"
        lines.append(f"{mark} <b>{esc(slot['name'])}</b> ({esc(slot['time'])})")
        for item in slot["items"]:
            tick = "✔" if item["taken"] else "·"
            amount = services.fmt_amount(item["amount"])
            lines.append(f"   {tick} {esc(item['supplement_name'])} – {amount} {esc(item['unit'])}")
        lines.append("")
    stats = services.day_stats(day)
    lines.append(f"<b>{stats['done']}/{stats['total']}</b> erledigt ({stats['percent']} %)")
    return "\n".join(lines)


def stock_text() -> str:
    entries = [e for e in services.stock_overview() if e["supplement"]["active"]]
    if not entries:
        return "Noch keine Supplemente angelegt."
    lines = ["📦 <b>Vorrat</b>", ""]
    for entry in entries:
        sup = entry["supplement"]
        if entry["usage"] <= 0:
            lines.append(f"• {esc(sup['name'])} – kein Plan hinterlegt")
            continue
        icon = "🔴" if entry["days_left"] <= 7 else "🟡" if entry["days_left"] <= 21 else "🟢"
        empty_on = entry["empty_on"].strftime("%d.%m.%Y")
        lines.append(
            f"{icon} <b>{esc(sup['name'])}</b>\n"
            f"   {services.fmt_amount(entry['stock'])} {esc(sup['unit'])} übrig · "
            f"{services.fmt_amount(entry['usage'])}/Tag\n"
            f"   reicht {entry['days_left']} Tage – leer am {empty_on}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Eingehende Updates
# --------------------------------------------------------------------------
def codewords() -> set[str]:
    raw = db.get_setting("codewords", "genommen")
    return {w.strip().lower() for w in raw.split(",") if w.strip()}


async def handle_message(message: dict) -> None:
    chat = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()
    if not text:
        return

    known = chat_id()
    lowered = text.lower().lstrip("/").split("@")[0].split()[0]

    # Erstkontakt: Chat-ID merken
    if lowered in {"start", "id"} and (not known or chat == known):
        if not known:
            db.set_setting("chat_id", chat)
            await bot.send(chat, f"👋 Chat verbunden (ID <code>{esc(chat)}</code>).\n\n{HELP}")
        else:
            await bot.send(chat, f"Deine Chat-ID: <code>{esc(chat)}</code>\n\n{HELP}")
        return

    if known and chat != known:
        await bot.send(chat, "Dieser Bot ist privat. 🙈")
        return

    now = db.now()

    if lowered in {"hilfe", "help", "hilfe?"}:
        await bot.send(chat, HELP)
    elif lowered in {"heute", "status", "plan"}:
        await bot.send(chat, today_text())
    elif lowered in {"vorrat", "stock", "packungen"}:
        await bot.send(chat, stock_text())
    elif lowered == "snooze":
        snooze_all(now, 30)
        await bot.send(chat, "😴 Okay, 30 Minuten Ruhe.")
    elif lowered in codewords() or lowered in {"genommen", "alles", "alle"}:
        await confirm_taken(chat, now)
    else:
        await bot.send(
            chat,
            "Verstehe ich nicht. Codewörter: <b>"
            + esc(", ".join(sorted(codewords())))
            + "</b>\nOder /hilfe",
        )


async def confirm_taken(chat: str, now: datetime) -> None:
    done = services.complete_all_due(now, source="telegram")
    if not done:
        stats = services.day_stats(now.date())
        if stats["total"] and stats["open"] == 0:
            await bot.send(chat, "✅ Für heute ist schon alles abgehakt. Stark!")
        else:
            await bot.send(chat, "Gerade ist nichts fällig. /heute zeigt dir den Plan.")
        return
    lines = ["✅ <b>Abgehakt</b>", ""]
    for slot_name, names in done:
        lines.append(f"<b>{esc(slot_name)}</b>: " + esc(", ".join(names)))
    stats = services.day_stats(now.date())
    lines.append("")
    lines.append(f"Heute: {stats['done']}/{stats['total']} erledigt.")
    await bot.send(chat, "\n".join(lines))


async def handle_callback(callback: dict) -> None:
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat = str(message.get("chat", {}).get("id", ""))
    message_id = message.get("message_id")
    known = chat_id()
    if known and chat != known:
        await bot.answer_callback(callback["id"], "Nicht dein Bot 🙈")
        return

    now = db.now()
    parts = data.split(":")

    if parts[0] == "slot" and len(parts) == 2:
        names = services.complete_slot(now.date(), int(parts[1]), "telegram")
        await bot.answer_callback(callback["id"], "Abgehakt ✅")
        if message_id:
            await bot.edit_markup(chat, message_id, [])
        text = ", ".join(names) if names else "war schon erledigt"
        await bot.send(chat, f"✅ {esc(text)}")
    elif parts[0] == "item" and len(parts) == 2:
        services.set_intake(int(parts[1]), True, "telegram")
        await bot.answer_callback(callback["id"], "Abgehakt ✅")
        slot_id = None
        row = db.one("SELECT slot_id FROM intakes WHERE id = ?", (int(parts[1]),))
        if row:
            slot_id = row["slot_id"]
        if message_id and slot_id is not None:
            plan = {s["slot_id"]: s for s in services.day_plan(now.date())}
            slot = plan.get(slot_id)
            if slot and not slot["complete"]:
                await bot.edit_markup(chat, message_id, slot_keyboard(slot_id, slot["items"]))
            else:
                await bot.edit_markup(chat, message_id, [])
    elif parts[0] == "snooze" and len(parts) == 3:
        minutes = int(parts[2])
        snooze_slot(now, int(parts[1]), minutes)
        await bot.answer_callback(callback["id"], f"{minutes} Minuten Ruhe 😴")
    else:
        await bot.answer_callback(callback["id"])


# --------------------------------------------------------------------------
# Snooze / Reminder-Status
# --------------------------------------------------------------------------
def snooze_slot(now: datetime, slot_id: int, minutes: int) -> None:
    until = (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO reminders (day, slot_id, sent_count, snooze_until) VALUES (?, ?, 0, ?) "
        "ON CONFLICT(day, slot_id) DO UPDATE SET snooze_until = excluded.snooze_until",
        (now.date().isoformat(), slot_id, until),
    )


def snooze_all(now: datetime, minutes: int) -> None:
    for slot in services.due_slots(now):
        snooze_slot(now, slot["slot_id"], minutes)


def in_quiet_hours(now: datetime) -> bool:
    start = services.parse_hhmm(db.get_setting("quiet_from", "22:30"), time(22, 30))
    end = services.parse_hhmm(db.get_setting("quiet_to", "06:30"), time(6, 30))
    current = now.time()
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


# --------------------------------------------------------------------------
# Loops
# --------------------------------------------------------------------------
async def poll_loop() -> None:
    if not enabled():
        log.warning("Kein SUPBOT_TELEGRAM_TOKEN gesetzt – Bot bleibt aus.")
        return
    me = await bot.call("getMe")
    if me:
        log.info("Telegram verbunden als @%s", me.get("username"))
    offset = db.get_int_setting("update_offset", 0)
    while True:
        try:
            updates = await bot.call(
                "getUpdates",
                offset=offset,
                timeout=POLL_TIMEOUT,
                allowed_updates=["message", "callback_query"],
            )
            if updates is None:
                await asyncio.sleep(5)
                continue
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                db.set_setting("update_offset", offset)
                try:
                    if "message" in update:
                        await handle_message(update["message"])
                    elif "callback_query" in update:
                        await handle_callback(update["callback_query"])
                except Exception:
                    log.exception("Update konnte nicht verarbeitet werden")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Fehler im Polling-Loop")
            await asyncio.sleep(5)


async def reminder_loop() -> None:
    if not enabled():
        return
    while True:
        try:
            await check_reminders()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Fehler im Reminder-Loop")
        await asyncio.sleep(CHECK_INTERVAL)


async def check_reminders() -> None:
    chat = chat_id()
    if not chat:
        return
    now = db.now()
    interval = db.get_int_setting("reminder_interval_min", 30)
    max_reminders = db.get_int_setting("max_reminders", 24)
    day_s = now.date().isoformat()

    for slot in services.due_slots(now):
        state = db.one(
            "SELECT * FROM reminders WHERE day = ? AND slot_id = ?", (day_s, slot["slot_id"])
        )
        sent = state["sent_count"] if state else 0

        if state and state["snooze_until"]:
            if datetime.fromisoformat(state["snooze_until"]) > now:
                continue

        if sent == 0:
            # Erste Nachricht geht immer raus, auch in der Ruhezeit
            pass
        else:
            if sent >= max_reminders or in_quiet_hours(now):
                continue
            last = datetime.fromisoformat(state["last_sent"]) if state["last_sent"] else None
            if last and (now - last) < timedelta(minutes=interval):
                continue

        await bot.send(
            chat,
            slot_message(slot, nag=sent),
            slot_keyboard(slot["slot_id"], slot["items"]),
        )
        db.execute(
            "INSERT INTO reminders (day, slot_id, sent_count, last_sent, snooze_until) "
            "VALUES (?, ?, 1, ?, NULL) "
            "ON CONFLICT(day, slot_id) DO UPDATE SET "
            "  sent_count = reminders.sent_count + 1, last_sent = excluded.last_sent, "
            "  snooze_until = NULL",
            (day_s, slot["slot_id"], now.isoformat(timespec="seconds")),
        )

    await check_low_stock(chat, now)


async def check_low_stock(chat: str, now: datetime) -> None:
    threshold = db.get_int_setting("low_stock_days", 10)
    if threshold <= 0:
        return
    day_s = now.date().isoformat()
    if db.get_setting("last_stock_warning") == day_s or now.hour < 9:
        return
    db.set_setting("last_stock_warning", day_s)
    entries = services.low_stock(threshold)
    if not entries:
        return
    lines = ["🛒 <b>Nachbestellen</b>", ""]
    for entry in entries:
        sup = entry["supplement"]
        lines.append(
            f"• <b>{esc(sup['name'])}</b> – noch {entry['days_left']} Tage "
            f"(leer am {entry['empty_on'].strftime('%d.%m.')})"
            + (f"\n  {esc(sup['url'])}" if sup["url"] else "")
        )
    await bot.send(chat, "\n".join(lines))


async def send_test_message() -> bool:
    chat = chat_id()
    if not chat or not enabled():
        return False
    result = await bot.send(chat, "🔔 Testnachricht von SupBot – Verbindung steht.")
    return result is not None
