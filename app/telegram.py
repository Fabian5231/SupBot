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

from . import config, db, services, zentrale

log = logging.getLogger("supbot.telegram")

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 30
CHECK_INTERVAL = 30  # Sekunden zwischen zwei Reminder-Pruefungen
SNOOZE_MIN = 30      # "Später" in der Erinnerung

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

    async def edit_text(self, chat_id, message_id: int, text: str, keyboard: list | None = None):
        return await self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup={"inline_keyboard": keyboard or []},
        )

    async def delete(self, chat_id, message_id: int):
        return await self.call("deleteMessage", chat_id=chat_id, message_id=message_id)

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


def slot_keyboard(slot_id: int, day: date, snoozed: bool = False) -> list:
    """Nur zwei Knoepfe: alles abhaken oder spaeter erinnern."""
    done = {"text": "✅ Alle genommen", "callback_data": f"slot:{slot_id}:{day.isoformat()}"}
    if snoozed:
        return [[done]]
    later = {"text": f"⏰ Später ({SNOOZE_MIN} Min)", "callback_data": f"snooze:{slot_id}:{SNOOZE_MIN}"}
    return [[done, later]]


def done_message(slot: dict) -> str:
    """Text, durch den eine erledigte Erinnerung ersetzt wird."""
    times = [i["taken_at"][11:16] for i in slot["items"] if i["taken_at"]]
    when = f" · {max(times)} Uhr" if times else ""
    names = ", ".join(i["supplement_name"] for i in slot["items"])
    return f"✅ <b>{esc(slot['name'])}</b> erledigt{when}\n<i>{esc(names)}</i>"


def reminder_tag(day: date, slot_id: int) -> str:
    """Eine Kennung pro Slot und Tag: jede Erinnerung ersetzt in der App die vorige."""
    return f"slot-{slot_id}-{day.isoformat()}"


def slot_notice(slot: dict, day: date, nag: int = 0) -> dict:
    """Dieselbe Erinnerung fuer die Zentrale: Klartext und die zwei Knoepfe."""
    title = f"{'⏰' if nag == 0 else '🔁'} {slot['name']} – {slot['time']} Uhr"
    if nag:
        title += f" (Erinnerung {nag + 1})"
    lines = [
        f"• {i['supplement_name']} – {services.fmt_amount(i['amount'])} {i['unit']}"
        for i in slot["items"] if not i["taken"]
    ]
    done = [i["supplement_name"] for i in slot["items"] if i["taken"]]
    if done:
        lines += ["", "schon erledigt: " + ", ".join(done)]
    return {
        "title": title,
        "body": "\n".join(lines),
        "tag": reminder_tag(day, slot["slot_id"]),
        "actions": [
            {"id": f"slot:{slot['slot_id']}:{day.isoformat()}", "title": "✅ Alle genommen"},
            {"id": f"snooze:{slot['slot_id']}:{SNOOZE_MIN}", "title": f"⏰ Später ({SNOOZE_MIN} Min)"},
        ],
    }


def done_notice(slot: dict, day: date) -> dict:
    """Ersetzt die Erinnerung in der App leise, wenn anderswo abgehakt wurde."""
    times = [i["taken_at"][11:16] for i in slot["items"] if i["taken_at"]]
    when = f" · {max(times)} Uhr" if times else ""
    return {
        "title": f"✅ {slot['name']} erledigt{when}",
        "body": ", ".join(i["supplement_name"] for i in slot["items"]),
        "tag": reminder_tag(day, slot["slot_id"]),
        "priority": "low",
    }


def find_slot(day: date, slot_id: int) -> dict | None:
    return next((s for s in services.day_plan(day) if s["slot_id"] == slot_id), None)


async def close_reminder(chat: str, day: date, slot_id: int, message_id: int | None = None) -> None:
    """Ersetzt die Erinnerung(en) eines erledigten Slots durch 'erledigt'."""
    slot = find_slot(day, slot_id)
    if slot is None or not slot["complete"]:
        return
    state = db.one(
        "SELECT message_id, zentrale_open FROM reminders WHERE day = ? AND slot_id = ?",
        (day.isoformat(), slot_id),
    )
    ids = {message_id, state["message_id"] if state else None} - {None}
    for mid in ids:
        await bot.edit_text(chat, mid, done_message(slot))
    if state and state["zentrale_open"]:
        await zentrale.notify(**done_notice(slot, day))
    db.execute(
        "UPDATE reminders SET message_id = NULL, zentrale_open = 0 WHERE day = ? AND slot_id = ?",
        (day.isoformat(), slot_id),
    )


async def close_finished_reminders(chat: str, now: datetime) -> None:
    """Raeumt offene Erinnerungen auf, deren Slot inzwischen erledigt ist
    (per Codewort, Button oder Weboberflaeche)."""
    since = (now.date() - timedelta(days=1)).isoformat()
    rows = db.query(
        "SELECT day, slot_id FROM reminders "
        "WHERE (message_id IS NOT NULL OR zentrale_open = 1) AND day >= ?",
        (since,),
    )
    for row in rows:
        await close_reminder(chat, date.fromisoformat(row["day"]), row["slot_id"])


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
    await close_finished_reminders(chat, now)
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

    if parts[0] == "slot" and len(parts) in (2, 3):
        # Der Tag steckt im Button, damit eine Erinnerung von gestern nicht heute abhakt
        try:
            day = date.fromisoformat(parts[2]) if len(parts) == 3 else now.date()
        except ValueError:
            day = now.date()
        slot_id = int(parts[1])
        services.complete_slot(day, slot_id, "telegram")
        await bot.answer_callback(callback["id"], "Abgehakt ✅")
        await close_reminder(chat, day, slot_id, message_id)
    elif parts[0] == "item" and len(parts) == 2:
        # Nur noch fuer aeltere Nachrichten, die Einzel-Buttons hatten
        services.set_intake(int(parts[1]), True, "telegram")
        await bot.answer_callback(callback["id"], "Abgehakt ✅")
        row = db.one("SELECT day, slot_id FROM intakes WHERE id = ?", (int(parts[1]),))
        if message_id and row:
            day = date.fromisoformat(row["day"])
            slot = find_slot(day, row["slot_id"])
            if slot and slot["complete"]:
                await close_reminder(chat, day, row["slot_id"], message_id)
    elif parts[0] == "snooze" and len(parts) == 3:
        slot_id, minutes = int(parts[1]), int(parts[2])
        snooze_slot(now, slot_id, minutes)
        await bot.answer_callback(callback["id"], f"{minutes} Minuten Ruhe 😴")
        slot = find_slot(now.date(), slot_id)
        if message_id and slot and not slot["complete"]:
            until = (now + timedelta(minutes=minutes)).strftime("%H:%M")
            text = slot_message(slot) + f"\n\n😴 <i>Ich erinnere dich um {until} Uhr.</i>"
            await bot.edit_text(chat, message_id, text, slot_keyboard(slot_id, now.date(), snoozed=True))
    else:
        await bot.answer_callback(callback["id"])


async def handle_zentrale_action(action: str) -> dict:
    """Knopf in einer Benachrichtigung der Zentrale - wie handle_callback.

    Die App hat die Benachrichtigung dabei schon geschlossen und die Meldung
    als erledigt markiert; hier bleibt nur der Slot und die Telegram-Nachricht.
    """
    now = db.now()
    chat = chat_id()
    parts = action.split(":")
    try:
        if parts[0] == "slot" and len(parts) == 3:
            day, slot_id = date.fromisoformat(parts[2]), int(parts[1])
        elif parts[0] == "snooze" and len(parts) == 3:
            slot_id, minutes = int(parts[1]), int(parts[2])
        else:
            raise ValueError(action)
    except ValueError:
        return {"ok": False, "text": "Unbekannter Knopf"}

    if parts[0] == "slot":
        services.complete_slot(day, slot_id, "app")
        db.execute(
            "UPDATE reminders SET zentrale_open = 0 WHERE day = ? AND slot_id = ?",
            (day.isoformat(), slot_id),
        )
        if chat:
            await close_reminder(chat, day, slot_id)
        return {"ok": True, "text": "Abgehakt ✅"}

    snooze_slot(now, slot_id, minutes)
    state = db.one(
        "SELECT message_id FROM reminders WHERE day = ? AND slot_id = ?",
        (now.date().isoformat(), slot_id),
    )
    slot = find_slot(now.date(), slot_id)
    if chat and state and state["message_id"] and slot and not slot["complete"]:
        until = (now + timedelta(minutes=minutes)).strftime("%H:%M")
        text = slot_message(slot) + f"\n\n😴 <i>Ich erinnere dich um {until} Uhr.</i>"
        await bot.edit_text(chat, state["message_id"], text, slot_keyboard(slot_id, now.date(), snoozed=True))
    return {"ok": True, "text": f"{minutes} Minuten Ruhe 😴"}


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

    await close_finished_reminders(chat, now)

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

        # Zuerst an die Zentrale (Push mit Knoepfen, ersetzt dort die vorige Erinnerung)
        answer = await zentrale.notify(**slot_notice(slot, now.date(), nag=sent))
        message = None
        if zentrale.telegram_wanted(answer):
            message = await bot.send(
                chat,
                slot_message(slot, nag=sent),
                slot_keyboard(slot["slot_id"], now.date()),
            )
            # Neue Nachricht, damit das Handy wieder klingelt; die vorige verschwindet,
            # damit im Chat immer nur eine offene Erinnerung pro Slot steht.
            if message and state and state["message_id"]:
                await bot.delete(chat, state["message_id"])
        db.execute(
            "INSERT INTO reminders (day, slot_id, sent_count, last_sent, snooze_until, message_id, zentrale_open) "
            "VALUES (?, ?, 1, ?, NULL, ?, ?) "
            "ON CONFLICT(day, slot_id) DO UPDATE SET "
            "  sent_count = reminders.sent_count + 1, last_sent = excluded.last_sent, "
            "  snooze_until = NULL, "
            "  message_id = COALESCE(excluded.message_id, reminders.message_id), "
            "  zentrale_open = MAX(reminders.zentrale_open, excluded.zentrale_open)",
            (
                day_s,
                slot["slot_id"],
                now.isoformat(timespec="seconds"),
                message["message_id"] if message else None,
                1 if answer is not None else 0,
            ),
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
    plain = []
    for entry in entries:
        sup = entry["supplement"]
        lines.append(
            f"• <b>{esc(sup['name'])}</b> – noch {entry['days_left']} Tage "
            f"(leer am {entry['empty_on'].strftime('%d.%m.')})"
            + (f"\n  {esc(sup['url'])}" if sup["url"] else "")
        )
        plain.append(
            f"• {sup['name']} – noch {entry['days_left']} Tage (leer am {entry['empty_on'].strftime('%d.%m.')})"
            + (f"\n  {sup['url']}" if sup["url"] else "")
        )
    answer = await zentrale.notify("🛒 Nachbestellen", "\n".join(plain), tag="vorrat")
    if zentrale.telegram_wanted(answer):
        await bot.send(chat, "\n".join(lines))


async def send_test_message() -> bool:
    """Testknopf in den Einstellungen: geht an beide Wege, egal wie der Schalter steht."""
    answer = await zentrale.notify("🔔 Testnachricht von SupBot", "Verbindung steht.")
    chat = chat_id()
    if not chat or not enabled():
        return answer is not None
    result = await bot.send(chat, "🔔 Testnachricht von SupBot – Verbindung steht.")
    return result is not None or answer is not None
