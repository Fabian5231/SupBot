"""Gewicht aus einem Waagen-Foto lesen.

Strategie: das Bild wird auf den Display-Ausschnitt zugeschnitten, in mehreren
Vorverarbeitungs-Varianten aufbereitet und jede Variante mit mehreren
Tesseract-Modi gelesen. Aus allen Treffern gewinnt der plausibelste Kandidat.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
import pytesseract
from PIL import Image, ImageFilter, ImageOps

from . import config

if config.TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD

NUMBER_RE = re.compile(r"\d+(?:[.,]\d{1,2})?")

# Zielhoehe des Ausschnitts vor der Erkennung. Tesseract braucht grosse Ziffern.
TARGET_CROP_HEIGHT = 260
MAX_CROP_HEIGHT = 520

WHITELIST = "0123456789.,"
CONFIGS: tuple[tuple[str, str], ...] = (
    ("psm7", f"--oem 3 --psm 7 -c tessedit_char_whitelist={WHITELIST}"),
    ("psm6", f"--oem 3 --psm 6 -c tessedit_char_whitelist={WHITELIST}"),
    ("psm13", "--oem 3 --psm 13"),
)


class TesseractMissing(RuntimeError):
    pass


@dataclass
class Candidate:
    value: float
    score: float
    confidence: float
    token: str
    variant: str
    mode: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "value": round(self.value, 2),
            "score": round(self.score, 3),
            "confidence": round(self.confidence, 1),
            "token": self.token,
            "variant": self.variant,
            "mode": self.mode,
        }


@dataclass
class OcrResult:
    weight: float | None
    confidence: float
    raw_text: str
    variant: str | None
    candidates: list[Candidate] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "weight": round(self.weight, 2) if self.weight is not None else None,
            "confidence": round(self.confidence, 1),
            "raw_text": self.raw_text,
            "variant": self.variant,
            "candidates": [c.as_dict() for c in self.candidates[:8]],
        }


# --------------------------------------------------------------------------
# Bildvorverarbeitung
# --------------------------------------------------------------------------

def load_image(data: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    return img.convert("RGB")


def apply_crop(img: Image.Image, crop: dict[str, float] | None) -> Image.Image:
    """crop sind normalisierte Werte 0..1 mit x, y, w, h."""
    if not crop:
        return img
    width, height = img.size
    x0 = max(0.0, min(1.0, float(crop.get("x", 0.0)))) * width
    y0 = max(0.0, min(1.0, float(crop.get("y", 0.0)))) * height
    x1 = x0 + max(0.02, min(1.0, float(crop.get("w", 1.0)))) * width
    y1 = y0 + max(0.02, min(1.0, float(crop.get("h", 1.0)))) * height
    box = (int(x0), int(y0), int(min(width, x1)), int(min(height, y1)))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return img
    return img.crop(box)


def _rescale(img: Image.Image) -> Image.Image:
    height = img.height
    if height == 0:
        return img
    if height < TARGET_CROP_HEIGHT:
        factor = TARGET_CROP_HEIGHT / height
    elif height > MAX_CROP_HEIGHT:
        factor = MAX_CROP_HEIGHT / height
    else:
        return img
    size = (max(1, int(img.width * factor)), max(1, int(height * factor)))
    return img.resize(size, Image.LANCZOS)


def _otsu_threshold(gray: np.ndarray) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = float(gray.size)
    levels = np.arange(256, dtype=np.float64)
    weight_bg = np.cumsum(hist)
    weight_fg = total - weight_bg
    cum_mean = np.cumsum(hist * levels)
    total_mean = cum_mean[-1]
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_bg = cum_mean / weight_bg
        mean_fg = (total_mean - cum_mean) / weight_fg
        variance = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    variance[~np.isfinite(variance)] = -1.0
    return int(np.argmax(variance))


def _local_mean(gray: np.ndarray, window: int) -> np.ndarray:
    """Fenstermittelwert ueber ein Integralbild, fuer adaptives Schwellwerten."""
    if window % 2 == 0:
        window += 1
    pad = window // 2
    padded = np.pad(gray.astype(np.float64), pad, mode="edge")
    integral = padded.cumsum(axis=0).cumsum(axis=1)
    integral = np.pad(integral, ((1, 0), (1, 0)), mode="constant")
    height, width = gray.shape
    total = (
        integral[window : window + height, window : window + width]
        - integral[0:height, window : window + width]
        - integral[window : window + height, 0:width]
        + integral[0:height, 0:width]
    )
    return total / float(window * window)


def _to_image(mask: np.ndarray) -> Image.Image:
    """mask True = Vordergrund (Ziffer). Ausgabe schwarz auf weiss mit Rand."""
    arr = np.where(mask, 0, 255).astype(np.uint8)
    img = Image.fromarray(arr, mode="L")
    return ImageOps.expand(img, border=24, fill=255)


def build_variants(img: Image.Image) -> list[tuple[str, Image.Image]]:
    base = _rescale(img)
    gray_img = ImageOps.autocontrast(base.convert("L"), cutoff=1)
    gray_img = gray_img.filter(ImageFilter.MedianFilter(size=3))
    gray = np.asarray(gray_img, dtype=np.uint8)

    otsu = _otsu_threshold(gray)
    window = max(15, (gray.shape[0] // 3) | 1)
    local = _local_mean(gray, window)

    sharpened = gray_img.filter(ImageFilter.UnsharpMask(radius=3, percent=180, threshold=3))

    return [
        ("otsu-dark", _to_image(gray < otsu)),
        ("otsu-light", _to_image(gray >= otsu)),
        ("adaptive-dark", _to_image(gray < (local - 12))),
        ("adaptive-light", _to_image(gray > (local + 12))),
        ("grayscale", ImageOps.expand(sharpened, border=24, fill=255)),
    ]


# --------------------------------------------------------------------------
# Erkennung
# --------------------------------------------------------------------------

def _interpretations(token: str) -> list[tuple[float, bool]]:
    """Liefert (Wert, hat_Dezimaltrenner) fuer einen erkannten Zahlen-Token."""
    text = token.replace(",", ".").strip(".")
    if not text:
        return []
    out: list[tuple[float, bool]] = []
    if "." in text:
        try:
            out.append((float(text), True))
        except ValueError:
            return []
    else:
        try:
            out.append((float(text), False))
        except ValueError:
            return []
        # Viele Displays zeigen 82.4, der Punkt geht beim Lesen aber verloren.
        if len(text) >= 3:
            out.append((float(f"{text[:-1]}.{text[-1]}"), False))
    return out


def _score(value: float, has_separator: bool, confidence: float, hint: float | None) -> float:
    score = confidence / 100.0
    if has_separator:
        score += 0.20
    if value != int(value):
        score += 0.05  # Nachkommastelle vorhanden, typisch fuer Waagen
    if hint is not None:
        delta = abs(value - hint)
        if delta <= 3:
            score += 0.18
        elif delta <= 8:
            score += 0.08
        elif delta > 25:
            score -= 0.25
    return score


def _tokens_from_data(data: dict[str, list[Any]]) -> list[tuple[str, float]]:
    words: list[tuple[str, float]] = []
    for text, conf in zip(data.get("text", []), data.get("conf", [])):
        text = (text or "").strip()
        if not text:
            continue
        try:
            confidence = float(conf)
        except (TypeError, ValueError):
            confidence = 0.0
        words.append((text, max(0.0, confidence)))

    tokens: list[tuple[str, float]] = []
    for text, confidence in words:
        for match in NUMBER_RE.findall(text):
            tokens.append((match, confidence))

    # Zusaetzlich die zusammengeklebte Zeile, falls "82" "." "4" getrennt kam.
    if len(words) > 1:
        joined = "".join(word for word, _ in words)
        mean_conf = sum(conf for _, conf in words) / len(words)
        for match in NUMBER_RE.findall(joined):
            tokens.append((match, mean_conf))
    return tokens


def _merge(candidates: Iterable[Candidate]) -> list[Candidate]:
    """Gleiche Werte zusammenfassen, Mehrfachtreffer erhoehen die Sicherheit."""
    best_by_value: dict[float, Candidate] = {}
    hits: dict[float, int] = {}
    for candidate in candidates:
        key = round(candidate.value, 2)
        hits[key] = hits.get(key, 0) + 1
        current = best_by_value.get(key)
        if current is None or candidate.score > current.score:
            best_by_value[key] = candidate
    merged: list[Candidate] = []
    for key, candidate in best_by_value.items():
        candidate.score += min(0.30, 0.05 * (hits[key] - 1))
        merged.append(candidate)
    merged.sort(key=lambda c: c.score, reverse=True)
    return merged


# Ohne Ausschnitt verwirrt der dunkle Waagen-Rand die Binarisierung. Diese
# Rueckfallschnitte greifen, wenn der erste Versuch nichts Brauchbares liefert.
FALLBACK_CROPS: tuple[dict[str, float], ...] = (
    {"x": 0.15, "y": 0.30, "w": 0.70, "h": 0.40},
    {"x": 0.05, "y": 0.20, "w": 0.90, "h": 0.60},
    {"x": 0.25, "y": 0.38, "w": 0.50, "h": 0.24},
)

# Ab diesem Score gilt ein Treffer als sicher genug, um nicht weiterzusuchen.
EARLY_EXIT_SCORE = 1.35


def _scan(image: Image.Image, hint: float | None) -> tuple[list[Candidate], list[str]]:
    candidates: list[Candidate] = []
    raw_texts: list[str] = []

    for variant_name, variant_img in build_variants(image):
        for mode, tess_config in CONFIGS:
            try:
                result = pytesseract.image_to_data(
                    variant_img,
                    lang=config.TESSERACT_LANG,
                    config=tess_config,
                    output_type=pytesseract.Output.DICT,
                )
            except pytesseract.TesseractNotFoundError as exc:
                raise TesseractMissing(str(exc)) from exc
            except pytesseract.TesseractError:
                continue

            tokens = _tokens_from_data(result)
            if tokens:
                raw_texts.append(
                    f"{variant_name}/{mode}: " + " ".join(token for token, _ in tokens)
                )
            for token, confidence in tokens:
                for value, has_separator in _interpretations(token):
                    if not config.WEIGHT_MIN_KG <= value <= config.WEIGHT_MAX_KG:
                        continue
                    candidates.append(
                        Candidate(
                            value=value,
                            score=_score(value, has_separator, confidence, hint),
                            confidence=confidence,
                            token=token,
                            variant=variant_name,
                            mode=mode,
                        )
                    )

        if candidates and max(c.score for c in candidates) >= EARLY_EXIT_SCORE:
            break

    return candidates, raw_texts


def read_weight(
    data: bytes,
    crop: dict[str, float] | None = None,
    hint: float | None = None,
) -> OcrResult:
    if not config.TESSERACT_CMD:
        raise TesseractMissing(
            "Tesseract wurde nicht gefunden. Bitte installieren und notfalls "
            "SUPBOT_TESSERACT_CMD in der .env setzen."
        )

    original = load_image(data)
    attempts: list[dict[str, float] | None] = [crop]
    attempts.extend(c for c in FALLBACK_CROPS if c != crop)

    candidates: list[Candidate] = []
    raw_texts: list[str] = []
    for attempt in attempts:
        found, texts = _scan(apply_crop(original, attempt), hint)
        candidates.extend(found)
        raw_texts.extend(texts)
        if candidates:
            break

    merged = _merge(candidates)
    raw_text = "\n".join(raw_texts[:10])

    if not merged:
        return OcrResult(weight=None, confidence=0.0, raw_text=raw_text, variant=None)

    best = merged[0]
    return OcrResult(
        weight=best.value,
        confidence=best.confidence,
        raw_text=raw_text,
        variant=f"{best.variant}/{best.mode}",
        candidates=merged,
    )
