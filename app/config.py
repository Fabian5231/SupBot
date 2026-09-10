"""Konfiguration: liest .env (falls vorhanden) und Umgebungsvariablen."""
from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimaler .env-Loader. Vorhandene Umgebungsvariablen gewinnen."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BASE_DIR / ".env")


def _env(key: str, default: str = "") -> str:
    value = os.environ.get(key, "")
    return value if value.strip() else default


TELEGRAM_TOKEN = _env("SUPBOT_TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _env("SUPBOT_TELEGRAM_CHAT_ID")
WEB_PASSWORD = _env("SUPBOT_PASSWORD")
SECRET = _env("SUPBOT_SECRET", "supbot-insecure-default-change-me")
TZ_NAME = _env("SUPBOT_TZ", "Europe/Berlin")
try:
    TZ = ZoneInfo(TZ_NAME)
except Exception:  # tzdata fehlt -> Systemzeitzone verwenden
    TZ = datetime.now().astimezone().tzinfo
    TZ_NAME = f"{TZ_NAME} (nicht gefunden, nutze Systemzeit)"
HOST = _env("SUPBOT_HOST", "127.0.0.1")
PORT = int(_env("SUPBOT_PORT", "8080"))

DATA_DIR = Path(_env("SUPBOT_DATA_DIR", str(BASE_DIR / "data")))
if not DATA_DIR.is_absolute():
    DATA_DIR = (BASE_DIR / DATA_DIR).resolve()
DB_PATH = DATA_DIR / "supbot.db"

COOKIE_NAME = "supbot_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 90  # 90 Tage

# ---- Gewicht --------------------------------------------------------------
IMAGE_DIR = DATA_DIR / "images"

WINDOWS_TESSERACT_CANDIDATES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    str(Path.home() / r"AppData\Local\Programs\Tesseract-OCR\tesseract.exe"),
)


def _find_tesseract() -> str | None:
    configured = _env("SUPBOT_TESSERACT_CMD") or _env("TESSERACT_CMD")
    if configured:
        return configured
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in WINDOWS_TESSERACT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


TESSERACT_CMD = _find_tesseract()

# Mitgeliefertes Modell fuer Sieben-Segment-Anzeigen. Fehlt es, tut es auch
# das systemeigene eng-Modell, nur mit mehr Fehlgriffen bei der Ziffer 0.
TESSDATA_DIR = BASE_DIR / "tessdata"
BUNDLED_MODEL = TESSDATA_DIR / "ssd.traineddata"
TESSERACT_LANG = _env(
    "SUPBOT_TESSERACT_LANG", "ssd" if BUNDLED_MODEL.is_file() else "eng"
)
WEIGHT_MIN_KG = _float("SUPBOT_WEIGHT_MIN_KG", 30.0)
WEIGHT_MAX_KG = _float("SUPBOT_WEIGHT_MAX_KG", 250.0)
KEEP_IMAGES = _env("SUPBOT_KEEP_IMAGES", "1").lower() in {"1", "true", "yes", "on", "ja"}
