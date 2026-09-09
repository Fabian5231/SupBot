"""FastAPI-Anwendung: Weboberflaeche + Start der Telegram-Tasks."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time as time_module
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import config, db, services, telegram

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("supbot")

BASE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

UNITS = ["Kapsel", "Tablette", "Softgel", "Tropfen", "Messlöffel", "Gramm", "ml", "Beutel"]
WEEKDAYS = list(zip("1234567", services.WEEKDAY_NAMES))


# --------------------------------------------------------------------------
# Login
# --------------------------------------------------------------------------
def _sign(payload: str) -> str:
    return hmac.new(config.SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token() -> str:
    issued = str(int(time_module.time()))
    return f"{issued}.{_sign(issued)}"


def token_valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    issued, _, signature = token.partition(".")
    if not hmac.compare_digest(_sign(issued), signature):
        return False
    try:
        return int(issued) + config.COOKIE_MAX_AGE > time_module.time()
    except ValueError:
        return False


def logged_in(request: Request) -> bool:
    if not config.WEB_PASSWORD:
        return True
    return token_valid(request.cookies.get(config.COOKIE_NAME))


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    services.ensure_day(db.today())
    tasks = []
    if telegram.enabled():
        tasks = [
            asyncio.create_task(telegram.poll_loop(), name="telegram-poll"),
            asyncio.create_task(telegram.reminder_loop(), name="telegram-reminder"),
        ]
        log.info("Telegram-Tasks gestartet")
    else:
        log.warning("SUPBOT_TELEGRAM_TOKEN fehlt – nur Weboberfläche aktiv")
    if not config.WEB_PASSWORD:
        log.warning("SUPBOT_PASSWORD ist leer – die Oberfläche ist ungeschützt!")
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await telegram.bot.aclose()


app = FastAPI(title="SupBot", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if path.startswith(("/static", "/login", "/healthz")) or logged_in(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


def render(request: Request, template: str, **context) -> HTMLResponse:
    context.setdefault("today", db.today())
    context.setdefault("now", db.now())
    context.setdefault("nav", "")
    return templates.TemplateResponse(request, template, context)


templates.env.filters["amount"] = services.fmt_amount
templates.env.filters["weekdays"] = services.weekdays_label
templates.env.filters["de_date"] = lambda d: d.strftime("%d.%m.%Y") if d else "–"
templates.env.filters["de_short"] = lambda d: d.strftime("%d.%m.") if d else "–"
templates.env.globals["units"] = UNITS
templates.env.globals["weekday_options"] = WEEKDAYS


# --------------------------------------------------------------------------
# Auth-Routen
# --------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    if logged_in(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", error=None)


@app.post("/login")
async def login(request: Request, password: str = Form("")):
    if not config.WEB_PASSWORD or secrets.compare_digest(password, config.WEB_PASSWORD):
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            config.COOKIE_NAME,
            make_token(),
            max_age=config.COOKIE_MAX_AGE,
            httponly=True,
            samesite="lax",
        )
        return response
    return render(request, "login.html", error="Falsches Passwort.")


@app.get("/logout")
async def logout():
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(config.COOKIE_NAME)
    return response


@app.get("/healthz")
async def healthz():
    return {"ok": True, "time": db.now().isoformat(timespec="seconds")}


# --------------------------------------------------------------------------
# Heute
# --------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def today_page(request: Request, day: str | None = None):
    try:
        current = date.fromisoformat(day) if day else db.today()
    except ValueError:
        current = db.today()
    plan = services.day_plan(current)
    return render(
        request,
        "today.html",
        nav="today",
        day=current,
        prev_day=current - timedelta(days=1),
        next_day=current + timedelta(days=1),
        is_today=current == db.today(),
        plan=plan,
        stats=services.day_stats(current),
        streak=services.streak(),
        low=services.low_stock(db.get_int_setting("low_stock_days", 10)),
    )


@app.post("/api/intake/{intake_id}/toggle")
async def api_toggle(intake_id: int, request: Request):
    row = db.one("SELECT * FROM intakes WHERE id = ?", (intake_id,))
    if row is None:
        raise HTTPException(404, "Einnahme nicht gefunden")
    updated = services.set_intake(intake_id, not row["taken"], "web")
    day = date.fromisoformat(row["day"])
    return {
        "id": intake_id,
        "taken": bool(updated["taken"]),
        "stats": services.day_stats(day),
    }


@app.post("/api/slot/{slot_id}/done")
async def api_slot_done(slot_id: int, day: str | None = None):
    current = date.fromisoformat(day) if day else db.today()
    services.complete_slot(current, slot_id, "web")
    return {"ok": True, "stats": services.day_stats(current)}


# --------------------------------------------------------------------------
# Supplemente
# --------------------------------------------------------------------------
@app.get("/supplemente", response_class=HTMLResponse)
async def supplements_page(request: Request, edit: int | None = None):
    supplements = db.query(
        "SELECT * FROM supplements ORDER BY active DESC, name COLLATE NOCASE"
    )
    slots = db.query("SELECT * FROM slots WHERE active = 1 ORDER BY time, sort")
    plan_rows = db.query("SELECT * FROM plan")
    plan_map: dict[int, dict[int, dict]] = {}
    for row in plan_rows:
        plan_map.setdefault(row["supplement_id"], {})[row["slot_id"]] = dict(row)
    editing = None
    if edit:
        editing = db.one("SELECT * FROM supplements WHERE id = ?", (edit,))
    return render(
        request,
        "supplements.html",
        nav="supplements",
        supplements=[dict(s) for s in supplements],
        slots=[dict(s) for s in slots],
        plan_map=plan_map,
        editing=dict(editing) if editing else None,
    )


@app.post("/supplemente/speichern")
async def supplement_save(request: Request):
    form = await request.form()
    sup_id = form.get("id")
    name = (form.get("name") or "").strip()
    if not name:
        return RedirectResponse("/supplemente", status_code=303)

    values = (
        name,
        (form.get("brand") or "").strip(),
        (form.get("unit") or "Kapsel").strip(),
        (form.get("notes") or "").strip(),
        (form.get("url") or "").strip(),
    )
    if sup_id:
        db.execute(
            "UPDATE supplements SET name=?, brand=?, unit=?, notes=?, url=? WHERE id=?",
            (*values, int(sup_id)),
        )
        supplement_id = int(sup_id)
    else:
        cursor = db.execute(
            "INSERT INTO supplements (name, brand, unit, notes, url, active, created_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?)",
            (*values, db.now().isoformat(timespec="seconds")),
        )
        supplement_id = cursor.lastrowid

    # Plan je Slot aktualisieren
    for slot in db.query("SELECT id FROM slots"):
        slot_id = slot["id"]
        active = form.get(f"slot_{slot_id}")
        if not active:
            db.execute(
                "DELETE FROM plan WHERE supplement_id = ? AND slot_id = ?",
                (supplement_id, slot_id),
            )
            continue
        try:
            amount = float(str(form.get(f"amount_{slot_id}", "1")).replace(",", "."))
        except ValueError:
            amount = 1.0
        amount = max(amount, 0.01)
        days = "".join(sorted(form.getlist(f"days_{slot_id}"))) or "1234567"
        db.execute(
            "INSERT INTO plan (supplement_id, slot_id, amount, weekdays) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(supplement_id, slot_id) DO UPDATE SET "
            "amount = excluded.amount, weekdays = excluded.weekdays",
            (supplement_id, slot_id, amount, days),
        )

    services.ensure_day(db.today())
    return RedirectResponse("/supplemente", status_code=303)


@app.post("/supplemente/{supplement_id}/aktiv")
async def supplement_toggle(supplement_id: int):
    db.execute("UPDATE supplements SET active = 1 - active WHERE id = ?", (supplement_id,))
    services.ensure_day(db.today())
    return RedirectResponse("/supplemente", status_code=303)


@app.post("/supplemente/{supplement_id}/loeschen")
async def supplement_delete(supplement_id: int):
    db.execute("DELETE FROM intakes WHERE supplement_id = ?", (supplement_id,))
    db.execute("DELETE FROM supplements WHERE id = ?", (supplement_id,))
    return RedirectResponse("/supplemente", status_code=303)


# --------------------------------------------------------------------------
# Vorrat / Packungen
# --------------------------------------------------------------------------
@app.get("/vorrat", response_class=HTMLResponse)
async def stock_page(request: Request):
    overview = services.stock_overview()
    total_value = sum(
        p["price"] or 0 for e in overview for p in e["packages"] if p["status"] != "empty"
    )
    return render(
        request,
        "stock.html",
        nav="stock",
        overview=overview,
        threshold=db.get_int_setting("low_stock_days", 10),
        total_value=round(total_value, 2),
    )


@app.post("/vorrat/packung")
async def package_add(
    supplement_id: int = Form(...),
    label: str = Form(""),
    units_total: str = Form("0"),
    price: str = Form(""),
    bought_at: str = Form(""),
    status: str = Form("sealed"),
):
    try:
        total = float(units_total.replace(",", "."))
    except ValueError:
        total = 0.0
    if total <= 0:
        return RedirectResponse("/vorrat", status_code=303)
    try:
        price_value = float(price.replace(",", ".")) if price.strip() else None
    except ValueError:
        price_value = None
    now = db.now().isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO packages (supplement_id, label, units_total, units_left, price, "
        "status, bought_at, opened_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            supplement_id,
            label.strip(),
            total,
            total,
            price_value,
            "open" if status == "open" else "sealed",
            bought_at or None,
            now if status == "open" else None,
            now,
        ),
    )
    return RedirectResponse("/vorrat", status_code=303)


@app.post("/vorrat/packung/{package_id}/korrigieren")
async def package_correct(package_id: int, units_left: str = Form("0")):
    pkg = db.one("SELECT * FROM packages WHERE id = ?", (package_id,))
    if pkg is None:
        raise HTTPException(404)
    try:
        left = float(units_left.replace(",", "."))
    except ValueError:
        return RedirectResponse("/vorrat", status_code=303)
    left = max(0.0, min(left, pkg["units_total"]))
    status = "empty" if left <= 0 else ("sealed" if left >= pkg["units_total"] else "open")
    opened = pkg["opened_at"] or (db.now().isoformat(timespec="seconds") if status == "open" else None)
    db.execute(
        "UPDATE packages SET units_left = ?, status = ?, opened_at = ? WHERE id = ?",
        (left, status, opened, package_id),
    )
    return RedirectResponse("/vorrat", status_code=303)


@app.post("/vorrat/packung/{package_id}/loeschen")
async def package_delete(package_id: int):
    db.execute("DELETE FROM packages WHERE id = ?", (package_id,))
    return RedirectResponse("/vorrat", status_code=303)


# --------------------------------------------------------------------------
# Verlauf
# --------------------------------------------------------------------------
@app.get("/verlauf", response_class=HTMLResponse)
async def history_page(request: Request, days: int = 30):
    days = max(7, min(days, 120))
    entries = services.history(days)
    tracked = [e for e in entries if e["total"]]
    average = round(sum(e["percent"] for e in tracked) / len(tracked)) if tracked else None
    return render(
        request,
        "history.html",
        nav="history",
        entries=list(reversed(entries)),
        grid=entries,
        days=days,
        average=average,
        streak=services.streak(),
    )


# --------------------------------------------------------------------------
# Einstellungen
# --------------------------------------------------------------------------
@app.get("/einstellungen", response_class=HTMLResponse)
async def settings_page(request: Request, sent: str | None = None):
    slots = db.query("SELECT * FROM slots ORDER BY time, sort")
    settings = {row["key"]: row["value"] for row in db.query("SELECT * FROM settings")}
    return render(
        request,
        "settings.html",
        nav="settings",
        slots=[dict(s) for s in slots],
        settings=settings,
        telegram_on=telegram.enabled(),
        chat_id=telegram.chat_id(),
        sent=sent,
        tz=config.TZ_NAME,
    )


@app.post("/einstellungen/speichern")
async def settings_save(
    reminder_interval_min: str = Form("30"),
    max_reminders: str = Form("24"),
    codewords: str = Form("genommen"),
    quiet_from: str = Form("22:30"),
    quiet_to: str = Form("06:30"),
    low_stock_days: str = Form("10"),
    chat_id: str = Form(""),
):
    db.set_setting("reminder_interval_min", max(1, int(float(reminder_interval_min or 30))))
    db.set_setting("max_reminders", max(1, int(float(max_reminders or 24))))
    db.set_setting("codewords", codewords.strip() or "genommen")
    db.set_setting("quiet_from", quiet_from or "22:30")
    db.set_setting("quiet_to", quiet_to or "06:30")
    db.set_setting("low_stock_days", max(0, int(float(low_stock_days or 10))))
    db.set_setting("chat_id", chat_id.strip())
    return RedirectResponse("/einstellungen", status_code=303)


@app.post("/einstellungen/slot")
async def slot_save(
    id: str = Form(""),
    name: str = Form(...),
    time: str = Form(...),
    active: str = Form(""),
):
    parsed = services.parse_hhmm(time, None)
    value = f"{parsed.hour:02d}:{parsed.minute:02d}"
    if id:
        db.execute(
            "UPDATE slots SET name = ?, time = ?, active = ? WHERE id = ?",
            (name.strip(), value, 1 if active else 0, int(id)),
        )
    else:
        db.execute(
            "INSERT INTO slots (name, time, sort, active) VALUES (?, ?, 0, 1)",
            (name.strip(), value),
        )
    services.ensure_day(db.today())
    return RedirectResponse("/einstellungen", status_code=303)


@app.post("/einstellungen/slot/{slot_id}/loeschen")
async def slot_delete(slot_id: int):
    db.execute("DELETE FROM intakes WHERE slot_id = ?", (slot_id,))
    db.execute("DELETE FROM slots WHERE id = ?", (slot_id,))
    return RedirectResponse("/einstellungen", status_code=303)


@app.post("/einstellungen/test")
async def telegram_test():
    ok = await telegram.send_test_message()
    return RedirectResponse(f"/einstellungen?sent={'ok' if ok else 'fail'}", status_code=303)


def run() -> None:
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")


if __name__ == "__main__":
    run()
