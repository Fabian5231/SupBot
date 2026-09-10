"""Gewicht aus einem Waagen-Foto lesen.

Strategie: das Bild wird auf den Display-Ausschnitt zugeschnitten, in mehreren
Vorverarbeitungs-Varianten aufbereitet und jede Variante mit mehreren
Tesseract-Modi gelesen. Aus allen Treffern gewinnt der plausibelste Kandidat.
"""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np
import pytesseract
from PIL import Image, ImageFilter, ImageOps

from . import config

if config.TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = config.TESSERACT_CMD

# Ueber die Umgebungsvariable statt ueber --tessdata-dir: pytesseract zerlegt
# den Config-String auf Windows ohne Ruecksicht auf Anfuehrungszeichen, ein
# Pfad mit Leerzeichen wuerde dort auseinanderfallen.
if config.TESSERACT_LANG == "ssd" and config.BUNDLED_MODEL.is_file():
    os.environ["TESSDATA_PREFIX"] = str(config.TESSDATA_DIR)

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
# Sieben-Segment-Decoder
#
# Der direkte Weg fuer eine Waagen-Anzeige: die leuchtenden Balken sind die
# hellsten Pixel im Bild. Zeilen- und Spaltenprojektion trennen die Ziffern,
# danach entscheidet je Ziffer der Anteil heller Pixel in sieben Fenstern,
# welcher Balken brennt. Das ist unabhaengig von Schriftarten und liest die
# Null nicht als Neun, woran Tesseract bei Leuchtziffern regelmaessig scheitert.
# --------------------------------------------------------------------------

# Fenster je Segment, als Anteil der Ziffernbox (x0, y0, x1, y1).
SEGMENT_WINDOWS: dict[str, tuple[float, float, float, float]] = {
    "a": (0.25, 0.02, 0.75, 0.16),   # oben
    "b": (0.70, 0.14, 0.98, 0.40),   # rechts oben
    "c": (0.70, 0.60, 0.98, 0.86),   # rechts unten
    "d": (0.25, 0.84, 0.75, 0.98),   # unten
    "e": (0.02, 0.60, 0.30, 0.86),   # links unten
    "f": (0.02, 0.14, 0.30, 0.40),   # links oben
    "g": (0.25, 0.43, 0.75, 0.57),   # Mitte
}
SEGMENT_ORDER = "abcdefg"
SEGMENT_DIGITS: dict[str, str] = {
    "abcdef": "0", "bc": "1", "abdeg": "2", "abcdg": "3", "bcfg": "4",
    "acdfg": "5", "acdefg": "6", "abc": "7", "abcdefg": "8", "abcdfg": "9",
}

# Wie hell ein Pixel sein muss, um als Segment zu zaehlen. Zwei Familien von
# Schwellen, weil Displayhelligkeit, Umgebungslicht und Rahmengroesse
# schwanken: einmal ueber den Anteil der hellsten Pixel, einmal relativ zum
# hellsten Pixel ueberhaupt. Was mehrere davon uebereinstimmend lesen, stimmt.
SEGMENT_PERCENTILES: tuple[int, ...] = (88, 90, 92, 93, 95, 96, 97)
SEGMENT_RATIOS: tuple[float, ...] = (0.55, 0.65, 0.72, 0.80, 0.88)
MIN_SEGMENT_DIGITS = 3


def _bands(flags: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    """Zusammenhaengende True-Bereiche als (start, ende)."""
    out: list[tuple[int, int]] = []
    start: int | None = None
    for index, value in enumerate(flags):
        if value and start is None:
            start = index
        elif not value and start is not None:
            if index - start >= min_len:
                out.append((start, index))
            start = None
    if start is not None and len(flags) - start >= min_len:
        out.append((start, len(flags)))
    return out


def _decode_segments(mask: np.ndarray) -> str | None:
    """Ziffernfolge aus einer Maske heller Pixel, None wenn unschluessig."""
    rows = mask.sum(axis=1)
    if not rows.max():
        return None
    row_bands = _bands(rows > rows.max() * 0.08, max(3, mask.shape[0] // 40))
    if not row_bands:
        return None
    top, bottom = max(row_bands, key=lambda band: band[1] - band[0])
    band = mask[top:bottom]

    cols = band.sum(axis=0)
    if not cols.max():
        return None
    col_bands = _bands(cols > cols.max() * 0.06, max(3, mask.shape[1] // 80))
    if not 2 <= len(col_bands) <= 6:
        return None

    boxes: list[tuple[int, int, int, int]] = []
    for left, right in col_bands:
        heights = band[:, left:right].sum(axis=1)
        inner = _bands(heights > max(1, heights.max() * 0.12), 2)
        if inner:
            boxes.append((left, right, min(a for a, _ in inner), max(b for _, b in inner)))
    if not boxes:
        return None

    median_h = float(np.median([bottom - top for _, _, top, bottom in boxes]))
    median_w = float(np.median([right - left for left, right, _, _ in boxes]))

    digits = ""
    for left, right, box_top, box_bottom in boxes:
        height = box_bottom - box_top
        width = right - left
        if height < median_h * 0.55:
            continue  # Dezimalpunkt oder Rest vom Leuchtkranz
        if width < median_w * 0.45:
            digits += "1"  # die Eins hat als einzige Ziffer eine schmale Box
            continue

        cell = band[box_top:box_bottom, left:right]
        shares: dict[str, float] = {}
        for name, (x0, y0, x1, y1) in SEGMENT_WINDOWS.items():
            window = cell[
                int(y0 * height) : max(int(y1 * height), int(y0 * height) + 1),
                int(x0 * width) : max(int(x1 * width), int(x0 * width) + 1),
            ]
            shares[name] = float(window.mean()) if window.size else 0.0

        brightest = max(shares.values())
        darkest = min(shares.values())
        if brightest < 0.30:
            return None  # nichts leuchtet, hier steht keine Ziffer
        # Die Acht hat keinen dunklen Balken, deshalb der zweite Fall.
        limit = (brightest + darkest) / 2 if brightest - darkest > 0.25 else brightest * 0.55
        pattern = "".join(name for name in SEGMENT_ORDER if shares[name] > limit)
        digit = SEGMENT_DIGITS.get(pattern)
        if digit is None:
            return None
        digits += digit

    return digits if len(digits) >= MIN_SEGMENT_DIGITS else None


def read_segments(img: Image.Image) -> tuple[str | None, float]:
    """Ziffernfolge und Anteil der Schwellen, die sie uebereinstimmend lasen."""
    gray = np.asarray(
        _rescale(img).convert("L").filter(ImageFilter.MedianFilter(size=3)),
        dtype=np.uint8,
    )
    brightest = float(gray.max())
    thresholds = [float(np.percentile(gray, p)) for p in SEGMENT_PERCENTILES]
    thresholds += [brightest * ratio for ratio in SEGMENT_RATIOS]

    votes: dict[str, int] = {}
    for threshold in thresholds:
        digits = _decode_segments(gray >= max(threshold, 60.0))
        if digits:
            votes[digits] = votes.get(digits, 0) + 1
    if not votes:
        return None, 0.0
    best = max(votes, key=lambda key: votes[key])
    return best, votes[best] / len(thresholds)


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

    # Der Punkt zwischen den Segmenten geht beim Lesen oft verloren. Aus 6330
    # kann 6330, 633,0 oder 63,30 werden; die Plausibilitaetsgrenzen und der
    # Vergleich mit dem letzten Wert sortieren den Unsinn aus.
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits or len(digits) > 6:
        return out
    number = int(digits)
    seen = {value for value, _ in out}
    for decimals in (0, 1, 2):
        if decimals and len(digits) <= decimals:
            continue
        value = number / (10 ** decimals)
        if value not in seen:
            seen.add(value)
            out.append((value, False))
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
    {"x": 0.26, "y": 0.40, "w": 0.48, "h": 0.18},
    {"x": 0.15, "y": 0.30, "w": 0.70, "h": 0.40},
    {"x": 0.05, "y": 0.20, "w": 0.90, "h": 0.60},
)

# Sieben-Segment-Ziffern werden je nach Aufbereitung unterschiedlich gelesen.
# Deshalb laufen immer alle Varianten durch: der Wert, den mehrere Varianten
# uebereinstimmend liefern, ist verlaesslicher als der erste Treffer.


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

    return candidates, raw_texts


def _segment_candidates(
    image: Image.Image, hint: float | None
) -> tuple[list[Candidate], str | None]:
    digits, agreement = read_segments(image)
    if not digits:
        return [], None

    confidence = round(55 + 45 * agreement, 1)
    found: list[Candidate] = []
    for value, _ in _interpretations(digits):
        if not config.WEIGHT_MIN_KG <= value <= config.WEIGHT_MAX_KG:
            continue
        found.append(
            Candidate(
                value=value,
                score=_score(value, False, confidence, hint) + agreement,
                confidence=confidence,
                token=digits,
                variant="segmente",
                mode=f"{round(agreement * 100)}%",
            )
        )
    return found, digits


def read_weight(
    data: bytes,
    crop: dict[str, float] | None = None,
    hint: float | None = None,
) -> OcrResult:
    original = load_image(data)
    attempts: list[dict[str, float] | None] = [crop]
    attempts.extend(c for c in FALLBACK_CROPS if c != crop)

    # Erst die Balken direkt auswerten, das ist schnell und praezise.
    segment_texts: list[str] = []
    for attempt in attempts:
        found, digits = _segment_candidates(apply_crop(original, attempt), hint)
        if digits:
            segment_texts.append(f"segmente: {digits}")
        if found:
            merged = _merge(found)
            best = merged[0]
            return OcrResult(
                weight=best.value,
                confidence=best.confidence,
                raw_text="\n".join(segment_texts),
                variant=f"{best.variant}/{best.mode}",
                candidates=merged,
            )

    if not config.TESSERACT_CMD:
        raise TesseractMissing(
            "Tesseract wurde nicht gefunden. Bitte installieren und notfalls "
            "SUPBOT_TESSERACT_CMD in der .env setzen."
        )

    candidates: list[Candidate] = []
    raw_texts: list[str] = list(segment_texts)
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
