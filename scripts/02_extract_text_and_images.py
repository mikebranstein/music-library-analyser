"""Script 02: extract embedded text and render page images/features.

Consumes the Script 01 inventory and produces three JSONL datasets:
- ``extracted_text.jsonl``: per-page text + text-quality signals + header candidates
- ``pages.jsonl``: per-page render, geometry, scanned/DPI, and image-quality metrics
- ``documents.jsonl``: per-PDF rollup for downstream identity/quality stages

Extraction/rendering uses ``pymupdf``. Image-quality metrics (blur/skew/contrast) use
``numpy`` and ``opencv-python`` when available and degrade gracefully to ``null`` otherwise.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None

try:
    import numpy as np  # type: ignore
except Exception:  # pragma: no cover
    np = None

try:
    import cv2  # type: ignore
except Exception:  # pragma: no cover
    cv2 = None

try:
    import pytesseract  # type: ignore
except Exception:  # pragma: no cover
    pytesseract = None

try:
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover
    Image = None

from scripts._common import (
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    build_checkpoint,
    canonicalize_instrument,
    has_readable_text,
    load_checkpoint,
    load_instrument_taxonomy,
    make_checkpoint_path,
    normalize_rel_path,
    pct,
    read_json,
    read_jsonl,
    setup_logging,
    sha256_text,
    utc_now_iso,
)

RECORD_VERSION = "2.4"
EXTRACTION_METHOD = "pymupdf_embedded"

CHECKPOINT_FILENAME = ".extraction_checkpoint.json"

# Wave-2 heuristic thresholds (zone/blank/staff detection).
BLANK_INK_THRESHOLD = 0.004  # text_density below this (with no text) => blank page
STAFF_DARK_ROW_FRACTION = 0.40  # row dark-pixel fraction to count as a staff line
STAFF_MIN_LINES = 5  # >= one 5-line staff => has_staves

# OCR / OSD (Tesseract) configuration and thresholds.
OCR_ENGINE = "tesseract"
DEFAULT_OCR_DPI = 300  # dedicated OCR render DPI (higher than thumbnail render)
DEFAULT_OCR_LANG = "eng"
OCR_LOW_CONFIDENCE = 0.60  # mean word confidence (0..1) below this is flagged in the report
# Multi-pass OCR: (render_dpi, tesseract_psm) tuples. A single fixed PSM misses text on hard
# scans -- PSM 3 reads the sparse, high-confidence header block, while PSM 6/11 recover denser
# body/sparse text, and a high-DPI pass sharpens small print. Every pass is retained as a
# candidate for the document-level LLM consolidation; the highest-scoring pass also populates the
# scalar ``ocr_text`` field for backward compatibility.
DEFAULT_OCR_PASSES: tuple[tuple[int, int], ...] = (
    (300, 3),
    (300, 6),
    (300, 11),
    (600, 3),
)
OCR_CANDIDATE_MAX_CHARS = 2000  # cap per-candidate OCR text retained for the LLM phase
_TESSERACT_COMMON_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)

# Heuristic quality thresholds used only for the human-readable report.
# They flag pages for attention; they do not change extraction outputs.
BLUR_WARN_THRESHOLD = 100.0  # Laplacian variance below this may indicate blur
LOW_CONTRAST_THRESHOLD = 12.0  # grayscale std below this may indicate a washed page
LOW_DPI_THRESHOLD = 150.0  # estimated scan DPI below this is low resolution
SKEW_MIN_DEG = 1.0  # abs skew at/above this is notable
SKEW_MAX_DEG = 45.0  # abs skew above this is treated as a sparse-page artifact

app = typer.Typer(add_completion=False)

logger = logging.getLogger("script02.extract")


@dataclass(frozen=True)
class InventoryItem:
    pdf_path: str
    piece_id: str
    piece_folder: str
    pdf_filename: str
    page_count: int
    file_fingerprint: str


@dataclass(frozen=True)
class OcrConfig:
    """Resolved OCR settings; ``enabled`` is False when Tesseract is unavailable."""

    enabled: bool
    dpi: int = DEFAULT_OCR_DPI
    lang: str = DEFAULT_OCR_LANG
    engine_version: str = ""
    multipass: bool = True
    passes: tuple[tuple[int, int], ...] = DEFAULT_OCR_PASSES


def is_readable_record(record: dict[str, Any]) -> bool:
    if not record.get("pdf_readable", False):
        return False
    if record.get("is_encrypted", False):
        return False
    if int(record.get("page_count", -1)) < 1:
        return False
    if record.get("processing_status") == "error":
        return False
    health = record.get("health_flags") or {}
    if health.get("is_zero_page", False):
        return False
    if health.get("is_malformed", False):
        return False
    return True


def load_inventory(inventory_path: Path) -> tuple[list[InventoryItem], int]:
    records = read_jsonl(inventory_path)
    items: list[InventoryItem] = []
    skipped = 0
    for record in records:
        if "pdf_path" not in record:
            continue
        if not is_readable_record(record):
            skipped += 1
            logger.info("Skipping non-readable PDF: %s", record.get("pdf_path"))
            continue
        items.append(
            InventoryItem(
                pdf_path=record["pdf_path"],
                piece_id=record.get("piece_id", ""),
                piece_folder=record.get("piece_folder", ""),
                pdf_filename=record.get("pdf_filename", Path(record["pdf_path"]).name),
                page_count=int(record.get("page_count", 0)),
                file_fingerprint=record.get("file_fingerprint", ""),
            )
        )
    return items, skipped


def hash_hex(hash_value: str) -> str:
    if ":" in hash_value:
        return hash_value.split(":", maxsplit=1)[1]
    return hash_value


def render_cache_path(
    cache_dir: Path,
    item: InventoryItem,
    page_num: int,
) -> Path:
    content_hash = sha256_text(f"{item.pdf_path}|{item.file_fingerprint}|{page_num}")
    prefix = hash_hex(content_hash)[:12]
    filename = f"{item.piece_id}_p{page_num:04d}_{prefix}.png"
    return cache_dir / "render" / filename


def compute_features_from_gray(samples: bytes) -> tuple[float, float]:
    """Pure-python fallback for text_density and black_white_ratio."""
    total = len(samples)
    if total == 0:
        return 0.0, 0.0
    dark = 0
    black_white = 0
    for value in samples:
        if value < 192:
            dark += 1
        if value == 0 or value == 255:
            black_white += 1
    return dark / total, black_white / total


def _gray_array(samples: bytes, width: int, height: int):
    """Return an (h, w) uint8 numpy array or None when numpy/shape unavailable."""
    if np is None or width <= 0 or height <= 0:
        return None
    try:
        return np.frombuffer(samples, dtype=np.uint8).reshape(height, width)
    except Exception:
        return None


def compute_gray_metrics(
    samples: bytes,
    width: int,
    height: int,
    enable_image_metrics: bool,
) -> dict[str, float | None]:
    """Compute pixel metrics; vectorized with numpy, else pure-python fallback."""
    arr = _gray_array(samples, width, height)
    if arr is not None:
        text_density = float((arr < 192).mean())
        black_white_ratio = float(((arr == 0) | (arr == 255)).mean())
        contrast_std: float | None = None
        blur_variance: float | None = None
        if enable_image_metrics:
            contrast_std = round(float(arr.std()), 4)
            a = arr.astype("float64")
            if a.shape[0] > 2 and a.shape[1] > 2:
                lap = (
                    a[:-2, 1:-1]
                    + a[2:, 1:-1]
                    + a[1:-1, :-2]
                    + a[1:-1, 2:]
                    - 4.0 * a[1:-1, 1:-1]
                )
                blur_variance = round(float(lap.var()), 4)
        return {
            "text_density": round(text_density, 6),
            "black_white_ratio": round(black_white_ratio, 6),
            "contrast_std": contrast_std,
            "blur_variance": blur_variance,
        }
    density, bw = compute_features_from_gray(samples)
    return {
        "text_density": density,
        "black_white_ratio": bw,
        "contrast_std": None,
        "blur_variance": None,
    }


def compute_skew_angle(samples: bytes, width: int, height: int) -> float | None:
    """Estimate deskew angle in degrees using opencv; None when unavailable."""
    if cv2 is None:
        return None
    arr = _gray_array(samples, width, height)
    if arr is None:
        return None
    try:
        _, thresh = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)
        coords = cv2.findNonZero(thresh)
        if coords is None:
            return None
        angle = cv2.minAreaRect(coords)[-1]
        if angle >= 45:
            angle -= 90
        return round(float(angle), 3)
    except Exception:
        return None


def compute_text_signals(text: str | None) -> dict[str, Any]:
    """Word count, alphanumeric ratio, and a normalized text hash for dedupe."""
    value = text or ""
    words = value.split()
    non_space = [c for c in value if not c.isspace()]
    alnum = [c for c in non_space if c.isalnum()]
    alnum_ratio = (len(alnum) / len(non_space)) if non_space else 0.0
    normalized = " ".join(words).lower()
    return {
        "word_count": len(words),
        "alnum_ratio": round(alnum_ratio, 4),
        "page_text_hash": sha256_text(normalized),
    }


def extract_text_structure(page: Any, limit: int = 8) -> dict[str, Any]:
    """Return prominent/top-line strings plus zoned corner/bottom text for a page.

    Zones are derived from each span's bounding-box center relative to the page
    size: a top band and a bottom band, with the top band split left/center/right.
    """
    empty = {
        "header_text_candidates": [],
        "top_lines": [],
        "zone_top_left": None,
        "zone_top_center": None,
        "zone_top_right": None,
        "zone_bottom": None,
    }
    try:
        data = page.get_text("dict")
    except Exception:
        return dict(empty)
    try:
        rect = page.rect
        page_w = float(rect.width)
        page_h = float(rect.height)
    except Exception:
        page_w = page_h = 0.0

    # (size, y0, text) for prominence/top-line ordering.
    spans: list[tuple[float, float, str]] = []
    # (x_center, y_center, text) for zone bucketing.
    zoned: list[tuple[float, float, str]] = []
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = (span.get("text") or "").strip()
                if not text:
                    continue
                size = float(span.get("size", 0.0))
                bbox = span.get("bbox", [0, 0, 0, 0])
                x0 = float(bbox[0]) if len(bbox) > 0 else 0.0
                y0 = float(bbox[1]) if len(bbox) > 1 else 0.0
                x1 = float(bbox[2]) if len(bbox) > 2 else x0
                y1 = float(bbox[3]) if len(bbox) > 3 else y0
                spans.append((size, y0, text))
                zoned.append(((x0 + x1) / 2.0, (y0 + y1) / 2.0, text))
    if not spans:
        return dict(empty)

    max_size = max(s[0] for s in spans)
    prominent: list[str] = []
    for size, _, text in sorted(spans, key=lambda s: -s[0]):
        if size >= max_size * 0.85 and text not in prominent:
            prominent.append(text)
        if len(prominent) >= limit:
            break
    top_lines: list[str] = []
    for _, _, text in sorted(spans, key=lambda s: s[1]):
        if text not in top_lines:
            top_lines.append(text)
        if len(top_lines) >= limit:
            break

    top_left: list[str] = []
    top_center: list[str] = []
    top_right: list[str] = []
    bottom: list[str] = []
    if page_w > 0 and page_h > 0:
        top_band = 0.22 * page_h
        bottom_band = 0.82 * page_h
        left_edge = 0.38 * page_w
        right_edge = 0.62 * page_w
        for xc, yc, text in sorted(zoned, key=lambda z: (z[1], z[0])):
            if yc <= top_band:
                if xc < left_edge:
                    top_left.append(text)
                elif xc > right_edge:
                    top_right.append(text)
                else:
                    top_center.append(text)
            elif yc >= bottom_band:
                bottom.append(text)

    def _join(parts: list[str]) -> str | None:
        joined = " ".join(parts).strip()
        return joined or None

    return {
        "header_text_candidates": prominent,
        "top_lines": top_lines,
        "zone_top_left": _join(top_left),
        "zone_top_center": _join(top_center),
        "zone_top_right": _join(top_right),
        "zone_bottom": _join(bottom),
    }


# --- Copyright / identity extraction (wave-2 item 2) -----------------------

_YEAR_RE = re.compile(r"\b(1[89]\d{2}|20\d{2})\b")
_COPYRIGHT_RE = re.compile(r"(?:\u00a9|\(c\)|copyright)", re.IGNORECASE)
_ARRANGER_RE = re.compile(
    r"\b(?:arr\.|arranged(?:\s+by)?|arr\s+by|arranger)\s*[:\-]?\s*(.+)", re.IGNORECASE
)
_COMPOSER_RE = re.compile(
    r"\b(?:words\s+and\s+music\s+by|music\s+by|composed\s+by|composer|by)\b"
    r"\s*[:\-]?\s*(.+)",
    re.IGNORECASE,
)
_BOILERPLATE_RE = re.compile(
    r"all rights reserved|international copyright secured|printed in.*",
    re.IGNORECASE,
)


def _clean_identity_value(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = _BOILERPLATE_RE.sub("", value)
    cleaned = cleaned.strip(" .,-\u2013\u2014;:\t")
    return cleaned or None


def extract_identity_candidates(page_texts: list[str]) -> dict[str, Any]:
    """Best-effort publisher/year/arranger/composer from page text (page 1 first)."""
    result: dict[str, Any] = {
        "publisher": None,
        "copyright_year": None,
        "arranger": None,
        "composer": None,
        "copyright_line": None,
    }
    if not page_texts:
        return result

    first_text = page_texts[0] or ""
    first_lines = [ln.strip() for ln in first_text.splitlines() if ln.strip()]

    # Arranger / composer are most reliable on page 1.
    for line in first_lines:
        if result["arranger"] is None:
            m = _ARRANGER_RE.search(line)
            if m:
                result["arranger"] = _clean_identity_value(m.group(1))
        if result["composer"] is None and _ARRANGER_RE.search(line) is None:
            m = _COMPOSER_RE.search(line)
            if m:
                result["composer"] = _clean_identity_value(m.group(1))

    # Copyright line: page 1 first, then later pages.
    for text in page_texts:
        for line in (ln.strip() for ln in (text or "").splitlines()):
            if line and _COPYRIGHT_RE.search(line):
                result["copyright_line"] = line
                year = _YEAR_RE.search(line)
                if year:
                    result["copyright_year"] = int(year.group(1))
                    remainder = line[year.end():]
                else:
                    remainder = _COPYRIGHT_RE.sub("", line)
                result["publisher"] = _clean_identity_value(remainder)
                break
        if result["copyright_line"] is not None:
            break

    if result["copyright_year"] is None:
        year = _YEAR_RE.search(first_text)
        if year:
            result["copyright_year"] = int(year.group(1))
    return result


# --- Staff (music-notation) detection (wave-2 item 5) ----------------------


def detect_staves(samples: bytes, width: int, height: int) -> dict[str, Any]:
    """Count long horizontal lines via row projection; None when numpy unavailable."""
    arr = _gray_array(samples, width, height)
    if arr is None:
        return {"has_staves": None, "staff_line_count": None}
    try:
        dark = arr < 160
        row_frac = dark.mean(axis=1)
        line_rows = row_frac > STAFF_DARK_ROW_FRACTION
        # Count runs of consecutive True rows (each run == one horizontal line).
        staff_line_count = 0
        prev = False
        for is_line in line_rows.tolist():
            if is_line and not prev:
                staff_line_count += 1
            prev = is_line
        return {
            "has_staves": bool(staff_line_count >= STAFF_MIN_LINES),
            "staff_line_count": int(staff_line_count),
        }
    except Exception:
        return {"has_staves": None, "staff_line_count": None}


def _compute_is_blank(
    render: dict[str, Any],
    embedded_text: str | None,
    image_analysis: dict[str, Any],
) -> bool | None:
    """Blank when render succeeded with near-zero ink and no text (and not scanned)."""
    if render.get("status") == "error":
        return None
    density = render.get("text_density")
    if density is None:
        return None
    text_empty = not (embedded_text or "").strip()
    return bool(
        density < BLANK_INK_THRESHOLD
        and text_empty
        and not image_analysis.get("is_image_based")
    )


# --- OCR / OSD (Tesseract, wave-3) -----------------------------------------


def resolve_tesseract_cmd(explicit: str | None) -> str | None:
    """Locate the Tesseract binary: explicit path, then PATH, then common dirs."""
    if explicit:
        if Path(explicit).exists():
            return explicit
        logger.warning("--tesseract-cmd path not found: %s", explicit)
    found = shutil.which("tesseract")
    if found:
        return found
    candidates = list(_TESSERACT_COMMON_PATHS)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(str(Path(local) / "Programs" / "Tesseract-OCR" / "tesseract.exe"))
    for cand in candidates:
        if Path(cand).exists():
            return cand
    return None


def _empty_ocr_result(status: str) -> dict[str, Any]:
    return {
        "ocr_text": None,
        "ocr_confidence": None,
        "ocr_word_count": None,
        "ocr_status": status,
        "ocr_candidates": [],
        "osd_rotation": None,
        "osd_orientation_conf": None,
        "osd_script": None,
    }


def _render_page_image(page: Any, dpi: int) -> Any:
    """Render a page to a PIL image at the given DPI."""
    matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)  # type: ignore[attr-defined]
    pix = page.get_pixmap(matrix=matrix)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def _ocr_pass(img: Any, lang: str, psm: int) -> tuple[str, float | None, int]:
    """Run one Tesseract pass at a fixed PSM; return (text, mean_confidence, word_count)."""
    data = pytesseract.image_to_data(
        img, lang=lang, config=f"--oem 1 --psm {psm}", output_type=pytesseract.Output.DICT
    )
    words: list[str] = []
    confs: list[float] = []
    for txt, conf in zip(data.get("text", []), data.get("conf", []), strict=False):
        token = (txt or "").strip()
        if not token:
            continue
        words.append(token)
        try:
            c = float(conf)
        except (TypeError, ValueError):
            c = -1.0
        if c >= 0:
            confs.append(c)
    text = " ".join(words)
    mean_conf = round(sum(confs) / len(confs) / 100.0, 4) if confs else None
    return text, mean_conf, len(words)


def _candidate_score(confidence: float | None, word_count: int) -> float:
    """Rank a candidate by confidence weighted by how much text it captured."""
    if not confidence or word_count <= 0:
        return 0.0
    return confidence * (word_count ** 0.5)


def run_ocr(page: Any, cfg: OcrConfig) -> dict[str, Any]:
    """Render a page and run multiple OCR passes; return OCR fields plus all pass candidates.

    Convenience wrapper (serial path): renders on this thread then runs OCR. For the parallel
    path, ``render_ocr_images`` (main thread, fitz) and ``ocr_from_images`` (worker thread,
    Tesseract) are called separately so rendering stays off worker threads. Never raises.
    """
    if pytesseract is None or Image is None:
        return _empty_ocr_result("unavailable")
    try:
        images = render_ocr_images(page, cfg)
    except Exception as exc:
        return _empty_ocr_result(f"render failed: {exc}")
    return ocr_from_images(images, cfg)


def render_ocr_images(page: Any, cfg: OcrConfig) -> dict[int, Any]:
    """Render the page once per distinct OCR-pass DPI. Uses fitz, so must run on the main thread."""
    passes = tuple(cfg.passes) if cfg.multipass else ((cfg.dpi, 3),)
    distinct_dpis = sorted({dpi for dpi, _ in passes})
    return {dpi: _render_page_image(page, dpi) for dpi in distinct_dpis}


def ocr_from_images(images: dict[int, Any], cfg: OcrConfig) -> dict[str, Any]:
    """Run OSD + multi-pass Tesseract on pre-rendered images.

    Thread-safe: touches only PIL images and the Tesseract subprocess (which releases the GIL),
    never fitz. Runs OSD once (on the lowest-DPI image) to correct orientation, then each
    (dpi, psm) pass. Every non-empty pass is kept in ``ocr_candidates``; the highest-scoring one
    populates the scalar ``ocr_text`` fields. Never raises.
    """
    result = _empty_ocr_result("success")
    passes = tuple(cfg.passes) if cfg.multipass else ((cfg.dpi, 3),)
    distinct_dpis = sorted({dpi for dpi, _ in passes})

    # Orientation/script detection once (on the lowest-DPI image); apply to all renders.
    base_dpi = distinct_dpis[0]
    try:
        osd = pytesseract.image_to_osd(images[base_dpi], output_type=pytesseract.Output.DICT)
        rotate = int(osd.get("rotate", 0) or 0)
        result["osd_rotation"] = rotate
        result["osd_orientation_conf"] = round(float(osd.get("orientation_conf", 0.0)), 3)
        result["osd_script"] = osd.get("script") or None
        if rotate in (90, 180, 270):
            images = {dpi: img.rotate(-rotate, expand=True) for dpi, img in images.items()}
    except Exception as exc:  # pragma: no cover - OSD fails on low-content pages
        logger.debug("OSD skipped: %s", exc)

    candidates: list[dict[str, Any]] = []
    for dpi, psm in passes:
        try:
            text, conf, wc = _ocr_pass(images[dpi], cfg.lang, psm)
        except Exception as exc:  # pragma: no cover - defensive
            result["ocr_status"] = f"ocr failed: {exc}"
            continue
        if not text:
            continue
        candidates.append(
            {
                "dpi": dpi,
                "psm": psm,
                "text": text[:OCR_CANDIDATE_MAX_CHARS],
                "confidence": conf,
                "word_count": wc,
            }
        )
    result["ocr_candidates"] = candidates
    if candidates:
        best = max(candidates, key=lambda c: _candidate_score(c["confidence"], c["word_count"]))
        result["ocr_text"] = best["text"] or None
        result["ocr_confidence"] = best["confidence"]
        result["ocr_word_count"] = best["word_count"]
    return result


def ocr_cache_path(
    cache_dir: Path, item: InventoryItem, page_num: int, cfg: OcrConfig
) -> Path:
    """Content- and config-addressed cache path for a page's OCR result."""
    key = sha256_text(
        f"{item.pdf_path}|{item.file_fingerprint}|{page_num}|"
        f"{cfg.dpi}|{cfg.lang}|{cfg.engine_version}|{cfg.multipass}|{cfg.passes}"
    )
    prefix = hash_hex(key)[:12]
    return cache_dir / "ocr" / f"{item.piece_id}_p{page_num:04d}_{prefix}.json"


def ocr_page_cached(
    page: Any,
    cache_dir: Path,
    item: InventoryItem,
    page_num: int,
    cfg: OcrConfig,
) -> dict[str, Any]:
    """Return a cached OCR result if present, else run OCR and cache success."""
    path = ocr_cache_path(cache_dir, item, page_num, cfg)
    cached = read_json(path)
    if cached is not None:
        return cached
    result = run_ocr(page, cfg)
    persist_ocr_cache(result, cache_dir, item, page_num, cfg)
    return result


def persist_ocr_cache(
    result: dict[str, Any], cache_dir: Path, item: InventoryItem, page_num: int, cfg: OcrConfig
) -> None:
    """Cache only deterministic successes; transient states (unavailable/errors) are retried."""
    if result.get("ocr_status") != "success":
        return
    path = ocr_cache_path(cache_dir, item, page_num, cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, result)
    except Exception:  # pragma: no cover - cache is best-effort
        logger.debug("Failed to write OCR cache for %s p%d", item.pdf_path, page_num)


def apply_ocr_fields(
    text_rec: dict[str, Any], page_rec: dict[str, Any] | None, ocr: dict[str, Any]
) -> None:
    """Patch OCR-derived fields onto records built before their OCR future resolved.

    Mirrors the OCR wiring in ``build_text_record``/``build_page_record`` for the parallel path.
    """
    ocr_text = ocr.get("ocr_text")
    text_rec["ocr_text"] = ocr_text
    text_rec["ocr_confidence"] = ocr.get("ocr_confidence")
    text_rec["ocr_word_count"] = ocr.get("ocr_word_count")
    text_rec["ocr_candidates"] = ocr.get("ocr_candidates") or []
    text_rec["ocr_applied"] = True
    text_rec["ocr_status"] = ocr.get("ocr_status", "not_applied")
    text_rec["ocr_engine"] = OCR_ENGINE
    text_rec["text_source"] = (
        "embedded" if text_rec.get("text_is_searchable") else ("ocr" if ocr_text else "none")
    )
    if page_rec is not None:
        page_rec["osd_rotation"] = ocr.get("osd_rotation")
        page_rec["osd_orientation_conf"] = ocr.get("osd_orientation_conf")
        page_rec["osd_script"] = ocr.get("osd_script")


def apply_llm_fields(doc_record: dict[str, Any], ocr_llm: dict[str, Any] | None) -> None:
    """Patch the five ``ocr_llm_*`` fields onto a document record after its LLM future resolves."""
    o = ocr_llm or {}
    doc_record["ocr_llm_status"] = o.get("ocr_llm_status", "not_applied")
    doc_record["ocr_llm_instruments"] = o.get("ocr_llm_instruments", [])
    doc_record["ocr_llm_raw"] = o.get("ocr_llm_raw", [])
    doc_record["ocr_llm_confidence"] = o.get("ocr_llm_confidence")
    doc_record["ocr_llm_notes"] = o.get("ocr_llm_notes", "")


def _should_ocr(embedded_text: str | None, image_analysis: dict[str, Any]) -> bool:
    """OCR pages with no readable embedded text or that look scanned.

    A born-digital page whose embedded text is almost entirely music-notation-font glyphs (Unicode
    Private Use Area) is not usefully searchable even though it is non-empty, so it is treated as
    needing OCR.
    """
    return (not has_readable_text(embedded_text)) or bool(image_analysis.get("is_image_based"))


# --- Document-level OCR -> instrumentation (LLM consolidation) --------------------------------
# A single physical part PDF (especially percussion) often covers several instruments that no
# single OCR pass reads cleanly. This stage feeds the multi-pass OCR candidates for a whole
# document to one LLM call (Copilot CLI) that reconciles the noisy passes into a canonical
# instrument list, which Script 03 turns into observed facets.

_RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "regex_rules.yaml"
_OCR_LLM_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "config" / "llm_prompts" / "identify_part_instruments.txt"
)
OCR_LLM_RESULT_START = "<<<PART_JSON>>>"
OCR_LLM_RESULT_END = "<<<END_PART_JSON>>>"
OCR_LLM_PROMPT_VERSION = "1"  # bump to invalidate cached consolidations when the prompt changes
OCR_LLM_MAX_PROMPT_CHARS = 16000  # cap the assembled OCR text fed to the LLM

DEFAULT_OCR_LLM_PROMPT = (
    "You are a music librarian assistant. Below is noisy multi-pass OCR text from the pages of a\n"
    "SINGLE instrumental PART PDF from a concert-band library. Several OCR passes are shown; each\n"
    "is unreliable on its own, so read them together and reconcile them.\n\n"
    "Decide which instrument(s) this ONE part is written for. A single physical part frequently\n"
    "covers several instruments -- e.g. a part labelled 'Percussion' may actually be Drums,\n"
    "Castanets, and Tambourine, and a 'Drums' line may mean snare drum + bass drum.\n\n"
    "Part context:\n"
    "- File name: {pdf_filename}\n"
    "- Source folder: {piece_folder}\n\n"
    "Allowed canonical instrument tokens -- choose ONLY from this list, using the closest match:\n"
    "{allowed_instruments}\n\n"
    "Multi-pass OCR text:\n"
    "{ocr_text}\n\n"
    "Return the canonical tokens for every instrument this part covers. Use ONLY tokens from the\n"
    "allowed list. If the OCR is too illegible to tell, return an empty instruments list with\n"
    "confidence 0. Never invent instruments the text does not support; treat the OCR as the only\n"
    "evidence and do not search the web.\n\n"
    "Respond with exactly one JSON object between the sentinel lines and nothing else:\n"
    f"{OCR_LLM_RESULT_START}\n"
    '{{"instruments": ["snare_drum"], "confidence": 0.0, "notes": ""}}\n'
    f"{OCR_LLM_RESULT_END}\n"
)

_OCR_LLM_TAXONOMY: dict[str, dict[str, str]] | None = None
_OCR_LLM_ALLOWED: list[str] | None = None
_OCR_LLM_TEMPLATE: str | None = None


@dataclass(frozen=True)
class OcrLlmConfig:
    """Resolved settings for the document-level OCR->instrumentation LLM consolidation."""

    enabled: bool = False
    command: str = "copilot"
    model: str = ""
    timeout_seconds: float = 300.0
    page_scope: str = "all"  # "all" | "first"


def _ocr_llm_taxonomy() -> dict[str, dict[str, str]]:
    """Cached instrument taxonomy (alias->canonical / canonical->section) from the shared YAML."""
    global _OCR_LLM_TAXONOMY
    if _OCR_LLM_TAXONOMY is None:
        _OCR_LLM_TAXONOMY = load_instrument_taxonomy(_RULES_PATH)
    return _OCR_LLM_TAXONOMY


def _ocr_llm_allowed_instruments() -> list[str]:
    """Sorted list of canonical instrument tokens the LLM may return."""
    global _OCR_LLM_ALLOWED
    if _OCR_LLM_ALLOWED is None:
        tax = _ocr_llm_taxonomy()
        _OCR_LLM_ALLOWED = sorted(set(tax.get("alias_to_canonical", {}).values()))
    return _OCR_LLM_ALLOWED


def _ocr_llm_template() -> str:
    """The consolidation prompt template (file override, else the built-in)."""
    global _OCR_LLM_TEMPLATE
    if _OCR_LLM_TEMPLATE is None:
        template = DEFAULT_OCR_LLM_PROMPT
        if _OCR_LLM_PROMPT_PATH.exists():
            try:
                template = _OCR_LLM_PROMPT_PATH.read_text(encoding="utf-8")
            except OSError as exc:  # pragma: no cover - best effort
                logger.warning("Failed to read %s (%s); using built-in prompt.",
                               _OCR_LLM_PROMPT_PATH, exc)
        _OCR_LLM_TEMPLATE = template
    return _OCR_LLM_TEMPLATE


def gather_ocr_candidates_text(text_records: list[dict[str, Any]], page_scope: str) -> str:
    """Assemble the multi-pass OCR candidates into one labelled text blob for the LLM."""
    ordered = sorted(text_records, key=lambda r: r.get("page_num", 0))
    if page_scope == "first":
        ordered = ordered[:1]
    chunks: list[str] = []
    for rec in ordered:
        candidates = rec.get("ocr_candidates") or []
        if not candidates:
            continue
        chunks.append(f"--- Page {rec.get('page_num')} ---")
        for c in candidates:
            chunks.append(
                f"[dpi={c.get('dpi')} psm={c.get('psm')} conf={c.get('confidence')}] "
                f"{c.get('text', '')}"
            )
    return "\n".join(chunks).strip()[:OCR_LLM_MAX_PROMPT_CHARS]


def build_ocr_llm_prompt(
    template: str, item: InventoryItem, allowed: list[str], ocr_text: str
) -> str:
    """Fill the consolidation prompt placeholders."""
    result = template
    for key, value in {
        "pdf_filename": item.pdf_filename,
        "piece_folder": item.piece_folder,
        "allowed_instruments": ", ".join(allowed),
        "ocr_text": ocr_text,
    }.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def parse_ocr_llm_response(stdout: str) -> dict[str, Any]:
    """Extract the single JSON object from the CLI stdout; raises ValueError when absent."""
    text = stdout or ""
    if OCR_LLM_RESULT_START in text and OCR_LLM_RESULT_END in text:
        start = text.index(OCR_LLM_RESULT_START) + len(OCR_LLM_RESULT_START)
        end = text.index(OCR_LLM_RESULT_END, start)
        candidate = text[start:end].strip().strip("`").strip()
        return json.loads(candidate)
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last <= first:
        raise ValueError("No JSON object found in OCR-LLM response.")
    return json.loads(text[first:last + 1])


def run_ocr_llm(prompt: str, cfg: OcrLlmConfig) -> dict[str, Any]:
    """Invoke the Copilot CLI headlessly for one consolidation and return the parsed JSON."""
    resolved = shutil.which(cfg.command)
    if not resolved:
        raise FileNotFoundError(f"Copilot CLI '{cfg.command}' not found on PATH.")
    args = [
        resolved, "-p", prompt,
        "--allow-all-tools", "--allow-all-urls", "--no-color", "--no-ask-user",
        "-s", "--log-level", "none",
    ]
    if cfg.model:
        args += ["--model", cfg.model]
    proc = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=cfg.timeout_seconds, check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stdout or proc.stderr or "").strip()[-500:]
        raise RuntimeError(f"Copilot CLI exited {proc.returncode}: {tail}")
    return parse_ocr_llm_response(proc.stdout)


def ocr_llm_cache_path(
    cache_dir: Path, item: InventoryItem, cfg: OcrLlmConfig, text_hash: str
) -> Path:
    """Content- and config-addressed cache path for a document's consolidation result."""
    key = sha256_text(
        f"{item.pdf_path}|{item.file_fingerprint}|{cfg.model}|{cfg.page_scope}|"
        f"{OCR_LLM_PROMPT_VERSION}|{text_hash}"
    )
    prefix = hash_hex(key)[:12]
    return cache_dir / "ocr_llm" / f"{item.piece_id}_{prefix}.json"


def _empty_ocr_llm_result(status: str) -> dict[str, Any]:
    return {
        "ocr_llm_status": status,
        "ocr_llm_instruments": [],
        "ocr_llm_raw": [],
        "ocr_llm_confidence": None,
        "ocr_llm_notes": "",
    }


def consolidate_part_instruments(
    item: InventoryItem,
    text_records: list[dict[str, Any]],
    cache_dir: Path,
    cfg: OcrLlmConfig,
    llm_fn: Callable[[str, OcrLlmConfig], dict[str, Any]] = run_ocr_llm,
) -> dict[str, Any] | None:
    """Consolidate a document's multi-pass OCR into a canonical instrument list via the LLM.

    Returns None when the document has no OCR candidate text (so no LLM call is made). Otherwise
    returns the consolidation record (cached to disk on success). Never raises.
    """
    ocr_text = gather_ocr_candidates_text(text_records, cfg.page_scope)
    if not ocr_text:
        return None

    text_hash = sha256_text(ocr_text)
    cache_path = ocr_llm_cache_path(cache_dir, item, cfg, text_hash)
    cached = read_json(cache_path)
    if cached is not None:
        return cached

    allowed = _ocr_llm_allowed_instruments()
    prompt = build_ocr_llm_prompt(_ocr_llm_template(), item, allowed, ocr_text)
    logger.info("OCR-LLM: consolidating instrumentation for %s", item.pdf_path)
    try:
        parsed = llm_fn(prompt, cfg)
    except Exception as exc:
        logger.warning("OCR-LLM consolidation failed for %s: %s", item.pdf_path, exc)
        return _empty_ocr_llm_result(f"error: {exc}")

    raw = parsed.get("instruments") if isinstance(parsed, dict) else None
    raw_list = [str(x) for x in raw] if isinstance(raw, list) else []
    alias_to_canonical = _ocr_llm_taxonomy().get("alias_to_canonical", {})
    allowed_set = set(allowed)
    canonicals: list[str] = []
    for name in raw_list:
        canon = canonicalize_instrument(name, alias_to_canonical)
        if canon in allowed_set and canon not in canonicals:
            canonicals.append(canon)

    result = _empty_ocr_llm_result("success")
    result["ocr_llm_instruments"] = canonicals
    result["ocr_llm_raw"] = raw_list
    if isinstance(parsed, dict):
        result["ocr_llm_confidence"] = parsed.get("confidence")
        result["ocr_llm_notes"] = str(parsed.get("notes") or "")
    logger.info("OCR-LLM: %s -> %s", item.pdf_path, canonicals or "none")
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(cache_path, result)
    except Exception:  # pragma: no cover - cache is best-effort
        logger.debug("Failed to write OCR-LLM cache for %s", item.pdf_path)
    return result


# --- Notation-source vision review (LLM image classification) ---------------------------------
# Deterministic OCR/geometry signals cannot separate a genuinely handwritten part from a readable
# but noisy printed photocopy (OCR confidence over music notation tracks notation density, not
# human legibility). This optional pass shows the rendered page image(s) to the Copilot CLI, which
# reports the notation source (printed/handwritten/mixed) and a legibility judgment. It reuses the
# OCR-LLM subprocess pattern but is cached separately (keyed by thumbnail hash, not OCR text) so a
# vision-prompt change never forces re-OCR and re-rendering never re-invokes vision.

_VISION_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent
    / "config" / "llm_prompts" / "classify_notation_source.txt"
)
VISION_RESULT_START = "<<<VISION_JSON>>>"
VISION_RESULT_END = "<<<END_VISION_JSON>>>"
VISION_PROMPT_VERSION = "1"  # bump to invalidate cached vision results when the prompt changes
VISION_NOTATION_VALUES = {"printed_original", "handwritten", "mixed_or_uncertain"}
VISION_LEGIBILITY_VALUES = {"good", "fair", "poor"}

DEFAULT_VISION_PROMPT = (
    "You are a music librarian assistant with expert score-reading skills. You are shown one or\n"
    "more rendered page images from a SINGLE instrumental PART PDF in a concert-band library. Look\n"
    "at the IMAGE(S) only -- do not run OCR, transcribe notes, or search the web.\n\n"
    "Part context:\n"
    "- File name: {pdf_filename}\n"
    "- Source folder: {piece_folder}\n\n"
    "Image file path(s) to open and inspect (absolute paths on this machine):\n"
    "{image_paths}\n\n"
    "Judge two things from the image(s):\n"
    "1) notation_source: printed_original, handwritten, or mixed_or_uncertain (hand-copied\n"
    "   manuscript counts as handwritten even when the staff lines are pre-printed).\n"
    "2) legibility: good, fair, or poor -- can a human musician actually read and play this page?\n"
    "Do not penalize legibility for mere page skew or rotation if the content is readable.\n\n"
    "Respond with exactly one JSON object between the sentinel lines and nothing else:\n"
    f"{VISION_RESULT_START}\n"
    '{{"notation_source": "printed_original", "legibility": "good", "confidence": 0.0, "notes": ""}}\n'
    f"{VISION_RESULT_END}\n"
)

_VISION_TEMPLATE: str | None = None


@dataclass(frozen=True)
class VisionConfig:
    """Resolved settings for the notation-source vision review."""

    enabled: bool = False
    command: str = "copilot"
    model: str = ""
    timeout_seconds: float = 300.0
    page_scope: str = "first"  # "first" | "all"


def _vision_template() -> str:
    """The vision prompt template (file override, else the built-in)."""
    global _VISION_TEMPLATE
    if _VISION_TEMPLATE is None:
        template = DEFAULT_VISION_PROMPT
        if _VISION_PROMPT_PATH.exists():
            try:
                template = _VISION_PROMPT_PATH.read_text(encoding="utf-8")
            except OSError as exc:  # pragma: no cover - best effort
                logger.warning("Failed to read %s (%s); using built-in prompt.",
                               _VISION_PROMPT_PATH, exc)
        _VISION_TEMPLATE = template
    return _VISION_TEMPLATE


def build_vision_prompt(template: str, item: InventoryItem, image_paths: list[str]) -> str:
    """Fill the vision prompt placeholders."""
    result = template
    for key, value in {
        "pdf_filename": item.pdf_filename,
        "piece_folder": item.piece_folder,
        "image_paths": "\n".join(image_paths),
    }.items():
        result = result.replace("{" + key + "}", str(value))
    return result


def parse_vision_response(stdout: str) -> dict[str, Any]:
    """Extract the single JSON object from the CLI stdout; raises ValueError when absent."""
    text = stdout or ""
    if VISION_RESULT_START in text and VISION_RESULT_END in text:
        start = text.index(VISION_RESULT_START) + len(VISION_RESULT_START)
        end = text.index(VISION_RESULT_END, start)
        candidate = text[start:end].strip().strip("`").strip()
        return json.loads(candidate)
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last <= first:
        raise ValueError("No JSON object found in vision response.")
    return json.loads(text[first:last + 1])


def run_vision_llm(prompt: str, cfg: VisionConfig) -> dict[str, Any]:
    """Invoke the Copilot CLI headlessly for one vision review and return the parsed JSON.

    The image path(s) are embedded in the prompt; the agent opens them via its file tools, so the
    same flag set as the OCR-LLM consolidation applies.
    """
    resolved = shutil.which(cfg.command)
    if not resolved:
        raise FileNotFoundError(f"Copilot CLI '{cfg.command}' not found on PATH.")
    args = [
        resolved, "-p", prompt,
        "--allow-all-tools", "--allow-all-urls", "--no-color", "--no-ask-user",
        "-s", "--log-level", "none",
    ]
    if cfg.model:
        args += ["--model", cfg.model]
    proc = subprocess.run(
        args, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=cfg.timeout_seconds, check=False,
    )
    if proc.returncode != 0:
        tail = (proc.stdout or proc.stderr or "").strip()[-500:]
        raise RuntimeError(f"Copilot CLI exited {proc.returncode}: {tail}")
    return parse_vision_response(proc.stdout)


def vision_cache_path(
    cache_dir: Path, item: InventoryItem, cfg: VisionConfig, thumb_hash: str
) -> Path:
    """Content- and config-addressed cache path for a document's vision result."""
    key = sha256_text(
        f"{item.pdf_path}|{item.file_fingerprint}|{cfg.model}|{cfg.page_scope}|"
        f"{VISION_PROMPT_VERSION}|{thumb_hash}"
    )
    prefix = hash_hex(key)[:12]
    return cache_dir / "vision" / f"{item.piece_id}_{prefix}.json"


def _empty_vision_result(status: str) -> dict[str, Any]:
    return {
        "vision_status": status,
        "vision_notation_source": None,
        "vision_legibility": None,
        "vision_confidence": None,
        "vision_notes": "",
    }


def _select_vision_thumbnails(
    page_records: list[dict[str, Any]], page_scope: str
) -> list[dict[str, Any]]:
    """Return the page records whose thumbnails feed the vision call (page 1, or all)."""
    with_thumbs = [p for p in page_records if p.get("thumbnail_path")]
    ordered = sorted(with_thumbs, key=lambda p: p.get("page_num", 0))
    if page_scope == "all":
        return ordered
    return ordered[:1]


def classify_notation_vision(
    item: InventoryItem,
    page_records: list[dict[str, Any]],
    cache_dir: Path,
    workspace_root: Path,
    cfg: VisionConfig,
    llm_fn: Callable[[str, VisionConfig], dict[str, Any]] = run_vision_llm,
) -> dict[str, Any] | None:
    """Classify a document's notation source + legibility from its rendered page image(s).

    Returns None when no page thumbnail is available (so no LLM call is made). Otherwise returns the
    vision record (cached to disk on success). Never raises.
    """
    selected = _select_vision_thumbnails(page_records, cfg.page_scope)
    if not selected:
        return None

    thumb_hash = sha256_text(
        "|".join(str(p.get("thumbnail_hash") or p.get("thumbnail_path")) for p in selected)
    )
    cache_path = vision_cache_path(cache_dir, item, cfg, thumb_hash)
    cached = read_json(cache_path)
    if cached is not None:
        return cached

    image_paths = [
        str((workspace_root / str(p.get("thumbnail_path"))).resolve()) for p in selected
    ]
    prompt = build_vision_prompt(_vision_template(), item, image_paths)
    logger.info("Vision: classifying notation source for %s", item.pdf_path)
    try:
        parsed = llm_fn(prompt, cfg)
    except Exception as exc:
        logger.warning("Vision review failed for %s: %s", item.pdf_path, exc)
        return _empty_vision_result(f"error: {exc}")

    result = _empty_vision_result("success")
    if isinstance(parsed, dict):
        source = str(parsed.get("notation_source") or "").strip().lower()
        legibility = str(parsed.get("legibility") or "").strip().lower()
        result["vision_notation_source"] = source if source in VISION_NOTATION_VALUES else None
        result["vision_legibility"] = (
            legibility if legibility in VISION_LEGIBILITY_VALUES else None
        )
        result["vision_confidence"] = parsed.get("confidence")
        result["vision_notes"] = str(parsed.get("notes") or "")
    logger.info(
        "Vision: %s -> %s / %s",
        item.pdf_path,
        result["vision_notation_source"] or "unknown",
        result["vision_legibility"] or "unknown",
    )
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(cache_path, result)
    except Exception:  # pragma: no cover - cache is best-effort
        logger.debug("Failed to write vision cache for %s", item.pdf_path)
    return result


def apply_vision_fields(doc_record: dict[str, Any], vision: dict[str, Any]) -> None:
    """Patch the vision_* fields of a document record from a vision result."""
    doc_record["vision_status"] = vision.get("vision_status", "not_applied")
    doc_record["vision_notation_source"] = vision.get("vision_notation_source")
    doc_record["vision_legibility"] = vision.get("vision_legibility")
    doc_record["vision_confidence"] = vision.get("vision_confidence")
    doc_record["vision_notes"] = vision.get("vision_notes", "")


def analyze_page_geometry(page: Any) -> dict[str, Any]:
    """Page size in points, rotation, orientation, and aspect ratio."""
    rect = page.rect
    width_pt = float(rect.width)
    height_pt = float(rect.height)
    rotation = int(getattr(page, "rotation", 0) or 0)
    landscape = width_pt >= height_pt
    if rotation in (90, 270):
        landscape = height_pt >= width_pt
    aspect_ratio = round(width_pt / height_pt, 4) if height_pt else None
    return {
        "page_width_pt": round(width_pt, 2),
        "page_height_pt": round(height_pt, 2),
        "rotation": rotation,
        "orientation": "landscape" if landscape else "portrait",
        "aspect_ratio": aspect_ratio,
    }


def analyze_page_images(page: Any, text_length: int) -> dict[str, Any]:
    """Image count, largest coverage, scanned heuristic, and estimated DPI."""
    try:
        images = page.get_images(full=True)
    except Exception:
        images = []
    image_count = len(images)
    rect = page.rect
    page_area = float(rect.width * rect.height)
    largest_coverage = 0.0
    estimated_dpi: float | None = None
    for img in images:
        xref = img[0]
        px_w = float(img[2]) if len(img) > 2 else 0.0
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            rects = []
        for r in rects:
            coverage = (float(r.width * r.height) / page_area) if page_area else 0.0
            if coverage > largest_coverage:
                largest_coverage = coverage
                if r.width > 0 and px_w > 0:
                    estimated_dpi = round(px_w / (float(r.width) / 72.0), 1)
    is_image_based = image_count > 0 and largest_coverage >= 0.6 and text_length < 30
    return {
        "image_count": image_count,
        "largest_image_coverage": round(largest_coverage, 4),
        "estimated_dpi": estimated_dpi,
        "is_image_based": is_image_based,
    }


def build_text_record(
    item: InventoryItem,
    run_id: str,
    page_num: int,
    embedded_text: str | None,
    status: str,
    error_message: str | None,
    header_candidates: list[str] | None = None,
    top_lines: list[str] | None = None,
    zones: dict[str, Any] | None = None,
    ocr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    text_value = embedded_text or ""
    is_searchable = has_readable_text(embedded_text)
    signals = compute_text_signals(embedded_text)
    zones = zones or {}
    ocr_applied = ocr is not None
    ocr_d = ocr or {}
    ocr_text = ocr_d.get("ocr_text")
    text_source = "embedded" if is_searchable else ("ocr" if ocr_text else "none")
    return {
        "record_version": RECORD_VERSION,
        "run_id": run_id,
        "pdf_path": item.pdf_path,
        "piece_id": item.piece_id,
        "page_num": page_num,
        "page_index_zero": page_num - 1,
        "embedded_text": embedded_text,
        "embedded_text_length": len(text_value),
        "extraction_method": EXTRACTION_METHOD,
        "text_is_searchable": is_searchable,
        "needs_ocr": not is_searchable,
        "ocr_text": ocr_text,
        "ocr_confidence": ocr_d.get("ocr_confidence"),
        "ocr_word_count": ocr_d.get("ocr_word_count"),
        "ocr_candidates": ocr_d.get("ocr_candidates") or [],
        "ocr_applied": ocr_applied,
        "ocr_status": ocr_d.get("ocr_status", "not_applied") if ocr_applied else "not_applied",
        "ocr_engine": OCR_ENGINE if ocr_applied else None,
        "text_source": text_source,
        "word_count": signals["word_count"],
        "alnum_ratio": signals["alnum_ratio"],
        "page_text_hash": signals["page_text_hash"],
        "header_text_candidates": header_candidates or [],
        "top_lines": top_lines or [],
        "zone_top_left": zones.get("zone_top_left"),
        "zone_top_center": zones.get("zone_top_center"),
        "zone_top_right": zones.get("zone_top_right"),
        "zone_bottom": zones.get("zone_bottom"),
        "processing_timestamp": utc_now_iso(),
        "processing_status": status,
        "error_message": error_message,
    }


def build_page_record(
    item: InventoryItem,
    run_id: str,
    page_num: int,
    render_dpi: int,
    thumbnail_rel: str | None,
    thumbnail_hash: str | None,
    width: int,
    height: int,
    file_size: int,
    text_density: float | None,
    black_white_ratio: float | None,
    status: str,
    error_message: str | None,
    geometry: dict[str, Any] | None = None,
    image_analysis: dict[str, Any] | None = None,
    contrast_std: float | None = None,
    blur_variance: float | None = None,
    skew_angle_deg: float | None = None,
    is_blank: bool | None = None,
    has_staves: bool | None = None,
    staff_line_count: int | None = None,
    ocr: dict[str, Any] | None = None,
) -> dict[str, Any]:
    geometry = geometry or {}
    image_analysis = image_analysis or {}
    ocr_d = ocr or {}
    return {
        "record_version": RECORD_VERSION,
        "run_id": run_id,
        "pdf_path": item.pdf_path,
        "piece_id": item.piece_id,
        "page_num": page_num,
        "page_index_zero": page_num - 1,
        "thumbnail_path": thumbnail_rel,
        "thumbnail_hash": thumbnail_hash,
        "render_dpi": render_dpi,
        "render_width_px": width,
        "render_height_px": height,
        "render_file_size_bytes": file_size,
        "text_density": text_density,
        "black_white_ratio": black_white_ratio,
        "page_width_pt": geometry.get("page_width_pt"),
        "page_height_pt": geometry.get("page_height_pt"),
        "rotation": geometry.get("rotation"),
        "orientation": geometry.get("orientation"),
        "aspect_ratio": geometry.get("aspect_ratio"),
        "image_count": image_analysis.get("image_count"),
        "largest_image_coverage": image_analysis.get("largest_image_coverage"),
        "is_image_based": image_analysis.get("is_image_based"),
        "estimated_dpi": image_analysis.get("estimated_dpi"),
        "contrast_std": contrast_std,
        "blur_variance": blur_variance,
        "skew_angle_deg": skew_angle_deg,
        "is_blank": is_blank,
        "has_staves": has_staves,
        "staff_line_count": staff_line_count,
        "osd_rotation": ocr_d.get("osd_rotation"),
        "osd_orientation_conf": ocr_d.get("osd_orientation_conf"),
        "osd_script": ocr_d.get("osd_script"),
        "render_timestamp": utc_now_iso(),
        "processing_status": status,
        "error_message": error_message,
    }


def build_document_record(
    item: InventoryItem,
    run_id: str,
    text_records: list[dict[str, Any]],
    page_records: list[dict[str, Any]],
    status: str,
    ocr_llm: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Aggregate per-page results into a single per-PDF rollup record."""
    pages_with_text = sum(1 for r in text_records if r.get("text_is_searchable"))
    pages_needing_ocr = sum(1 for r in text_records if r.get("needs_ocr"))
    total_text_length = sum(int(r.get("embedded_text_length", 0)) for r in text_records)
    total_word_count = sum(int(r.get("word_count", 0)) for r in text_records)
    pages_image_based = sum(1 for r in page_records if r.get("is_image_based"))
    blank_page_count = sum(1 for r in page_records if r.get("is_blank") is True)
    music_page_count = sum(1 for r in page_records if r.get("has_staves") is True)
    pages_ocr_applied = sum(1 for r in text_records if r.get("ocr_applied"))
    pages_ocr_recovered = sum(1 for r in text_records if r.get("ocr_text"))
    ocr_char_count = sum(len(r.get("ocr_text") or "") for r in text_records)
    pages_rotated = sum(1 for r in page_records if (r.get("osd_rotation") or 0))
    page_count = len(text_records)
    ocr_fraction = round(pages_needing_ocr / page_count, 4) if page_count else 0.0
    image_based_fraction = round(pages_image_based / page_count, 4) if page_count else 0.0

    first_page_text = None
    first_page_headers: list[str] = []
    if text_records:
        first = min(text_records, key=lambda r: r.get("page_num", 0))
        first_page_text = first.get("embedded_text")
        first_page_headers = first.get("header_text_candidates", []) or []

    ordered_texts = [
        r.get("embedded_text") or ""
        for r in sorted(text_records, key=lambda r: r.get("page_num", 0))
    ]
    identity_candidates = extract_identity_candidates(ordered_texts)

    return {
        "record_version": RECORD_VERSION,
        "run_id": run_id,
        "pdf_path": item.pdf_path,
        "piece_id": item.piece_id,
        "piece_folder": item.piece_folder,
        "pdf_filename": item.pdf_filename,
        "page_count": page_count,
        "pages_with_text": pages_with_text,
        "pages_needing_ocr": pages_needing_ocr,
        "ocr_fraction": ocr_fraction,
        "total_text_length": total_text_length,
        "total_word_count": total_word_count,
        "pages_image_based": pages_image_based,
        "image_based_fraction": image_based_fraction,
        "first_page_text": first_page_text,
        "first_page_header_candidates": first_page_headers,
        "blank_page_count": blank_page_count,
        "music_page_count": music_page_count,
        "identity_candidates": identity_candidates,
        "pages_ocr_applied": pages_ocr_applied,
        "pages_ocr_recovered": pages_ocr_recovered,
        "ocr_char_count": ocr_char_count,
        "pages_rotated": pages_rotated,
        "ocr_llm_status": (ocr_llm or {}).get("ocr_llm_status", "not_applied"),
        "ocr_llm_instruments": (ocr_llm or {}).get("ocr_llm_instruments", []),
        "ocr_llm_raw": (ocr_llm or {}).get("ocr_llm_raw", []),
        "ocr_llm_confidence": (ocr_llm or {}).get("ocr_llm_confidence"),
        "ocr_llm_notes": (ocr_llm or {}).get("ocr_llm_notes", ""),
        "vision_status": "not_applied",
        "vision_notation_source": None,
        "vision_legibility": None,
        "vision_confidence": None,
        "vision_notes": "",
        "processing_status": status,
        "processing_timestamp": utc_now_iso(),
    }


def render_page(
    page: Any,
    cache_dir: Path,
    workspace_root: Path,
    item: InventoryItem,
    page_num: int,
    render_dpi: int,
    enable_image_metrics: bool = True,
) -> dict[str, Any]:
    """Render one page to cache and return page-record fields. Never raises."""
    cache_path = render_cache_path(cache_dir, item, page_num)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        matrix = fitz.Matrix(render_dpi / 72.0, render_dpi / 72.0)  # type: ignore[attr-defined]
        pix = page.get_pixmap(matrix=matrix)
        width = int(pix.width)
        height = int(pix.height)

        gray = pix
        if (pix.n - int(pix.alpha)) >= 3 or pix.alpha:
            gray = fitz.Pixmap(fitz.csGRAY, pix)  # type: ignore[attr-defined]

        gray_bytes = bytes(gray.samples)
        metrics = compute_gray_metrics(gray_bytes, width, height, enable_image_metrics)
        skew_angle = (
            compute_skew_angle(gray_bytes, width, height) if enable_image_metrics else None
        )
        staves = (
            detect_staves(gray_bytes, width, height)
            if enable_image_metrics
            else {"has_staves": None, "staff_line_count": None}
        )

        if not cache_path.exists():
            pix.save(str(cache_path))

        png_bytes = cache_path.read_bytes()
        thumbnail_hash = sha256_text(png_bytes.decode("latin-1"))
        file_size = len(png_bytes)
        thumbnail_rel = normalize_rel_path(cache_path.relative_to(workspace_root))

        return {
            "thumbnail_rel": thumbnail_rel,
            "thumbnail_hash": thumbnail_hash,
            "width": width,
            "height": height,
            "file_size": file_size,
            "text_density": metrics["text_density"],
            "black_white_ratio": metrics["black_white_ratio"],
            "contrast_std": metrics["contrast_std"],
            "blur_variance": metrics["blur_variance"],
            "skew_angle_deg": skew_angle,
            "has_staves": staves["has_staves"],
            "staff_line_count": staves["staff_line_count"],
            "status": "success",
            "error_message": None,
        }
    except Exception as exc:  # pragma: no cover - defensive
        return {
            "thumbnail_rel": None,
            "thumbnail_hash": None,
            "width": -1,
            "height": -1,
            "file_size": -1,
            "text_density": None,
            "black_white_ratio": None,
            "contrast_std": None,
            "blur_variance": None,
            "skew_angle_deg": None,
            "has_staves": None,
            "staff_line_count": None,
            "status": "error",
            "error_message": f"render failed: {exc}",
        }


def process_pdf(
    item: InventoryItem,
    abs_path: Path,
    cache_dir: Path,
    workspace_root: Path,
    run_id: str,
    render_dpi: int,
    enable_rendering: bool,
    enable_image_metrics: bool = True,
    ocr_config: OcrConfig | None = None,
    ocr_executor: ThreadPoolExecutor | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], int, int]:
    """Return (text_records, page_records, document_record, page_error_count, pages_total).

    When ``ocr_executor`` is provided, each scanned page is rendered on this thread (fitz is not
    thread-safe) and its Tesseract passes are submitted to the pool, then resolved once all pages
    are rendered. The per-file LLM classification is submitted asynchronously by the caller, so
    the document record is built here with no LLM result (``ocr_llm=None``).
    """
    text_records: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    page_errors = 0
    ocr_config = ocr_config or OcrConfig(enabled=False)
    # page_num -> {future, text_rec, page_rec} for OCR passes running in the pool.
    pending_ocr: dict[int, dict[str, Any]] = {}

    doc = fitz.open(abs_path)  # type: ignore[attr-defined]
    try:
        page_total = int(doc.page_count)
        for page_idx in range(page_total):
            page_num = page_idx + 1
            page: Any = None
            embedded_text: str | None = None
            header_candidates: list[str] = []
            top_lines: list[str] = []
            zones: dict[str, Any] = {}
            geometry: dict[str, Any] = {}
            image_analysis: dict[str, Any] = {}
            ocr: dict[str, Any] | None = None
            try:
                page = doc[page_idx]
                embedded_text = page.get_text()
                structure = extract_text_structure(page)
                header_candidates = structure["header_text_candidates"]
                top_lines = structure["top_lines"]
                zones = structure
                geometry = analyze_page_geometry(page)
                image_analysis = analyze_page_images(page, len((embedded_text or "").strip()))
                ocr_future: Future | None = None
                if ocr_config.enabled and _should_ocr(embedded_text, image_analysis):
                    if ocr_executor is None:
                        ocr = ocr_page_cached(page, cache_dir, item, page_num, ocr_config)
                    else:
                        cached = read_json(ocr_cache_path(cache_dir, item, page_num, ocr_config))
                        if cached is not None:
                            ocr = cached
                        else:
                            try:
                                images = render_ocr_images(page, ocr_config)
                                ocr_future = ocr_executor.submit(
                                    ocr_from_images, images, ocr_config
                                )
                            except Exception as exc:  # pragma: no cover - defensive
                                ocr = _empty_ocr_result(f"render failed: {exc}")
                text_rec = build_text_record(
                    item, run_id, page_num, embedded_text, "success", None,
                    header_candidates, top_lines, zones, ocr,
                )
                text_records.append(text_rec)
                if ocr_future is not None:
                    pending_ocr[page_num] = {
                        "future": ocr_future, "text_rec": text_rec, "page_rec": None,
                    }
            except Exception as exc:
                page_errors += 1
                logger.warning(
                    "Text extraction failed on %s p%d: %s", item.pdf_path, page_num, exc
                )
                text_records.append(
                    build_text_record(
                        item, run_id, page_num, None, "error", f"text failed: {exc}"
                    )
                )
                page = None

            if not enable_rendering:
                text_empty = not (embedded_text or "").strip()
                is_blank_no_render = bool(
                    text_empty and not image_analysis.get("is_image_based")
                )
                page_rec = build_page_record(
                    item, run_id, page_num, render_dpi, None, None, -1, -1, -1,
                    None, None, "success", None,
                    geometry=geometry, image_analysis=image_analysis,
                    is_blank=is_blank_no_render, ocr=ocr,
                )
                page_records.append(page_rec)
                if page_num in pending_ocr:
                    pending_ocr[page_num]["page_rec"] = page_rec
                continue

            if page is None:
                try:
                    page = doc[page_idx]
                except Exception as exc:
                    page_errors += 1
                    page_records.append(
                        build_page_record(
                            item, run_id, page_num, render_dpi, None, None, -1, -1, -1,
                            None, None, "error", f"page load failed: {exc}",
                        )
                    )
                    continue

            render = render_page(
                page, cache_dir, workspace_root, item, page_num, render_dpi,
                enable_image_metrics,
            )
            if render["status"] == "error":
                page_errors += 1
                logger.warning(
                    "Render failed on %s p%d: %s",
                    item.pdf_path,
                    page_num,
                    render["error_message"],
                )
            page_rec = build_page_record(
                item,
                run_id,
                page_num,
                render_dpi,
                render["thumbnail_rel"],
                render["thumbnail_hash"],
                render["width"],
                render["height"],
                render["file_size"],
                render["text_density"],
                render["black_white_ratio"],
                render["status"],
                render["error_message"],
                geometry=geometry,
                image_analysis=image_analysis,
                contrast_std=render["contrast_std"],
                blur_variance=render["blur_variance"],
                skew_angle_deg=render["skew_angle_deg"],
                is_blank=_compute_is_blank(render, embedded_text, image_analysis),
                has_staves=render["has_staves"],
                staff_line_count=render["staff_line_count"],
                ocr=ocr,
            )
            page_records.append(page_rec)
            if page_num in pending_ocr:
                pending_ocr[page_num]["page_rec"] = page_rec

        # Resolve this file's OCR passes (running concurrently in the pool) and finalize records.
        for page_num, pend in pending_ocr.items():
            try:
                ocr_result = pend["future"].result()
            except Exception as exc:  # pragma: no cover - defensive
                ocr_result = _empty_ocr_result(f"ocr failed: {exc}")
            apply_ocr_fields(pend["text_rec"], pend["page_rec"], ocr_result)
            persist_ocr_cache(ocr_result, cache_dir, item, page_num, ocr_config)

        doc_status = "partial_error" if page_errors else "success"
        document_record = build_document_record(
            item, run_id, text_records, page_records, doc_status, None
        )
        return text_records, page_records, document_record, page_errors, page_total
    finally:
        doc.close()


def sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: (r.get("pdf_path", ""), r.get("page_num", 0)))


def sort_documents(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: r.get("pdf_path", ""))


def _is_notable_skew(angle: float | None) -> bool:
    return angle is not None and SKEW_MIN_DEG <= abs(angle) <= SKEW_MAX_DEG


def _stats(values: list[float]) -> tuple[float, float, float] | None:
    """Return (min, median, max) or None when empty."""
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    return ordered[0], median, ordered[-1]


def _count_by_pdf(records: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in records:
        key = r.get("pdf_path", "")
        counts[key] = counts.get(key, 0) + 1
    return counts


def _doc_status_icon(doc: dict[str, Any]) -> str:
    if doc.get("processing_status") == "partial_error":
        return "❌"
    if doc.get("image_based_fraction", 0.0):
        return "🖼️"
    if doc.get("ocr_fraction", 0.0):
        return "🔍"
    return "✅"


def build_markdown_report(
    text_records: list[dict[str, Any]],
    page_records: list[dict[str, Any]],
    document_records: list[dict[str, Any]],
    meta: dict[str, Any],
) -> str:
    """Render a human-readable Markdown summary of the extraction outputs."""
    total_docs = len(document_records)
    total_pages = len(page_records)
    pages_with_text = sum(1 for r in text_records if r.get("text_is_searchable"))
    pages_needing_ocr = sum(1 for r in text_records if r.get("needs_ocr"))
    pages_image_based = sum(1 for r in page_records if r.get("is_image_based"))
    blank_pages = sum(1 for r in page_records if r.get("is_blank") is True)
    music_pages = sum(1 for r in page_records if r.get("has_staves") is True)
    docs_with_music = sum(1 for d in document_records if d.get("music_page_count", 0))
    docs_with_identity = sum(
        1
        for d in document_records
        if any(
            (d.get("identity_candidates") or {}).get(k)
            for k in ("publisher", "copyright_year", "arranger", "composer")
        )
    )
    pages_ocr_applied = sum(1 for r in text_records if r.get("ocr_applied"))
    pages_ocr_recovered = sum(1 for r in text_records if r.get("ocr_text"))
    ocr_char_count = sum(len(r.get("ocr_text") or "") for r in text_records)
    pages_rotated = sum(1 for r in page_records if (r.get("osd_rotation") or 0))
    low_conf_ocr = [
        r
        for r in text_records
        if r.get("ocr_confidence") is not None and r["ocr_confidence"] < OCR_LOW_CONFIDENCE
    ]
    render_errors = [r for r in page_records if r.get("processing_status") == "error"]
    docs_partial = [
        d for d in document_records if d.get("processing_status") == "partial_error"
    ]
    docs_full_text = sum(1 for d in document_records if not d.get("ocr_fraction", 0.0))
    docs_scanned = [d for d in document_records if d.get("image_based_fraction", 0.0)]

    # Blur/skew/contrast/DPI are only meaningful on scanned (image-based) pages.
    # Born-digital pages are vector-rendered and always crisp, and sheet music is
    # mostly white space, so absolute thresholds there would be pure noise.
    scanned_pages = [r for r in page_records if r.get("is_image_based")]
    blur_vals = [
        r["blur_variance"] for r in scanned_pages if r.get("blur_variance") is not None
    ]
    contrast_vals = [
        r["contrast_std"] for r in scanned_pages if r.get("contrast_std") is not None
    ]
    dpi_vals = [
        r["estimated_dpi"] for r in scanned_pages if r.get("estimated_dpi") is not None
    ]
    blurry = [
        r
        for r in scanned_pages
        if r.get("blur_variance") is not None and r["blur_variance"] < BLUR_WARN_THRESHOLD
    ]
    low_contrast = [
        r
        for r in scanned_pages
        if r.get("contrast_std") is not None and r["contrast_std"] < LOW_CONTRAST_THRESHOLD
    ]
    skewed = [r for r in scanned_pages if _is_notable_skew(r.get("skew_angle_deg"))]
    low_dpi = [
        r
        for r in scanned_pages
        if r.get("estimated_dpi") is not None and r["estimated_dpi"] < LOW_DPI_THRESHOLD
    ]

    text_pct = pct(pages_with_text, total_pages)
    overall = "✅ Healthy"
    if docs_partial or render_errors:
        overall = "❌ Errors present"
    elif (
        pages_needing_ocr
        or pages_image_based
        or blurry
        or low_contrast
        or skewed
        or low_conf_ocr
    ):
        overall = "⚠️ Review recommended"

    out: list[str] = []
    out.append("# Extraction Report")
    out.append("")
    out.append(
        f"_Generated {meta['generated_at']} • run `{meta['run_id']}` • "
        f"mode **{meta['mode']}** • {meta['elapsed_seconds']:.1f}s_"
    )
    out.append("")
    out.append(f"**Status:** {overall}")
    out.append("")

    # Navigation
    out.append("## Contents")
    out.append("")
    out.append("- [At a Glance](#at-a-glance)")
    out.append("- [Text Coverage](#text-coverage)")
    out.append("- [Document Types](#document-types)")
    out.append("- [Image Quality](#image-quality)")
    out.append("- [Attention Needed](#attention-needed)")
    out.append("- [Per-Folder Breakdown](#per-folder-breakdown)")
    out.append("- [Per-Document Detail](#per-document-detail)")
    out.append("- [Configuration and Environment](#configuration-and-environment)")
    out.append("")

    # At a Glance
    out.append("## At a Glance")
    out.append("")
    out.append("| Metric | Value |")
    out.append("| --- | --- |")
    out.append(f"| Documents in output | {total_docs} |")
    out.append(
        f"| Processed this run | {meta['processed_pdfs']} "
        f"(reused {meta['reused_pdfs']}) |"
    )
    out.append(f"| Skipped (unreadable in inventory) | {meta['skipped']} |")
    out.append(f"| Document-level errors | {len(docs_partial)} |")
    out.append(f"| Total pages | {total_pages} |")
    out.append(
        f"| Pages with embedded text | {pages_with_text} ({text_pct:.1f}%) |"
    )
    out.append(f"| Pages needing OCR | {pages_needing_ocr} |")
    out.append(f"| Pages OCR'd | {pages_ocr_applied} |")
    out.append(f"| Pages with OCR text recovered | {pages_ocr_recovered} |")
    out.append(f"| Pages auto-rotated (OSD) | {pages_rotated} |")
    out.append(f"| Image-based (scanned) pages | {pages_image_based} |")
    out.append(f"| Blank / near-blank pages | {blank_pages} |")
    out.append(f"| Pages with music staves | {music_pages} |")
    out.append(f"| Page render errors | {len(render_errors)} |")
    out.append("")

    # Text coverage
    out.append("## Text Coverage")
    out.append("")
    out.append(
        f"- **{docs_full_text}/{total_docs}** documents have embedded text on every page."
    )
    out.append(
        f"- **{pages_with_text}/{total_pages}** pages ({text_pct:.1f}%) contain searchable "
        "embedded text; the rest are flagged `needs_ocr`."
    )
    if pages_ocr_applied:
        out.append(
            f"- OCR ran on **{pages_ocr_applied}** page(s) and recovered text on "
            f"**{pages_ocr_recovered}** ({ocr_char_count} chars); "
            f"**{len(low_conf_ocr)}** page(s) had low mean confidence "
            f"(< {OCR_LOW_CONFIDENCE:.2f})."
        )
        if pages_rotated:
            out.append(
                f"- OSD detected a non-zero rotation on **{pages_rotated}** page(s) and "
                "rotated them upright before OCR."
            )
    else:
        out.append(
            "- OCR was not run this pass (disabled or Tesseract unavailable); "
            "`needs_ocr` pages carry no recovered text."
        )
    words = [int(r.get("word_count", 0)) for r in text_records]
    if words:
        out.append(
            f"- Word count per page: min {min(words)}, "
            f"median {sorted(words)[len(words) // 2]}, max {max(words)}."
        )
    out.append("")

    # Document types
    out.append("## Document Types")
    out.append("")
    born_digital = total_docs - len(docs_scanned)
    out.append(
        f"- **{born_digital}** born-digital documents (no large full-page images with "
        "little text)."
    )
    out.append(
        f"- **{len(docs_scanned)}** documents contain scanned/image-based pages."
    )
    out.append(
        f"- **{docs_with_music}** documents have at least one page with detected music "
        f"staves ({music_pages} music pages total)."
    )
    out.append(
        f"- **{docs_with_identity}** documents have a detected publisher/copyright/"
        "arranger/composer candidate (best-effort)."
    )
    if dpi_vals:
        stats = _stats(dpi_vals)
        assert stats is not None
        out.append(
            f"- Estimated scan DPI (image pages): min {stats[0]:.0f}, "
            f"median {stats[1]:.0f}, max {stats[2]:.0f}."
        )
    out.append("")

    # Image quality
    out.append("## Image Quality")
    out.append("")
    if not meta["enable_image_metrics"]:
        out.append(
            "> Image-quality metrics were disabled or unavailable "
            "(`numpy`/`opencv` missing or `--no-image-metrics`). "
            "Blur/skew/contrast were not computed."
        )
        out.append("")
    elif not scanned_pages:
        out.append(
            "No scanned/image-based pages were detected. Blur, skew, contrast, and DPI "
            "checks apply only to scanned pages — born-digital pages are vector-rendered "
            "and crisp by construction, so they are not flagged here."
        )
        out.append("")
    else:
        out.append(
            f"Evaluated over **{len(scanned_pages)}** scanned/image-based pages only. "
            "Thresholds are heuristic and flag pages for review; they do not alter "
            "extraction output."
        )
        out.append("")
        out.append("| Signal | Flagged pages | Threshold | Range (min/median/max) |")
        out.append("| --- | --- | --- | --- |")
        b = _stats(blur_vals)
        c = _stats(contrast_vals)
        out.append(
            f"| Blur (Laplacian var) | {len(blurry)} | < {BLUR_WARN_THRESHOLD:.0f} | "
            + (f"{b[0]:.0f} / {b[1]:.0f} / {b[2]:.0f}" if b else "n/a")
            + " |"
        )
        out.append(
            f"| Low contrast (std) | {len(low_contrast)} | < {LOW_CONTRAST_THRESHOLD:.0f} | "
            + (f"{c[0]:.1f} / {c[1]:.1f} / {c[2]:.1f}" if c else "n/a")
            + " |"
        )
        out.append(
            f"| Notable skew | {len(skewed)} | "
            f"{SKEW_MIN_DEG:.0f}°–{SKEW_MAX_DEG:.0f}° | — |"
        )
        out.append(
            f"| Low DPI | {len(low_dpi)} | < {LOW_DPI_THRESHOLD:.0f} | — |"
        )
        out.append("")

    # Attention needed
    out.append("## Attention Needed")
    out.append("")
    attention_added = False

    if docs_partial:
        attention_added = True
        out.append(f"### ❌ Documents with processing errors ({len(docs_partial)})")
        out.append("")
        out.append("| Document | Pages | Status |")
        out.append("| --- | --- | --- |")
        for d in docs_partial[:15]:
            out.append(
                f"| {d.get('pdf_path', '')} | {d.get('page_count', 0)} | "
                f"{d.get('processing_status', '')} |"
            )
        if len(docs_partial) > 15:
            out.append(f"| … and {len(docs_partial) - 15} more | | |")
        out.append("")

    ocr_docs = sorted(
        [d for d in document_records if d.get("ocr_fraction", 0.0)],
        key=lambda d: d.get("ocr_fraction", 0.0),
        reverse=True,
    )
    if ocr_docs:
        attention_added = True
        out.append(f"### 🔍 Documents needing OCR ({len(ocr_docs)})")
        out.append("")
        out.append("| Document | Pages needing OCR | Of pages |")
        out.append("| --- | --- | --- |")
        for d in ocr_docs[:15]:
            out.append(
                f"| {d.get('pdf_path', '')} | {d.get('pages_needing_ocr', 0)} | "
                f"{d.get('page_count', 0)} ({100.0 * d.get('ocr_fraction', 0.0):.0f}%) |"
            )
        if len(ocr_docs) > 15:
            out.append(f"| … and {len(ocr_docs) - 15} more | | |")
        out.append("")

    for title, records in (
        ("⚠️ Blurry pages", blurry),
        ("⚠️ Low-contrast pages", low_contrast),
        ("⚠️ Skewed pages", skewed),
    ):
        if records:
            attention_added = True
            counts = _count_by_pdf(records)
            top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
            out.append(f"### {title} ({len(records)} across {len(counts)} documents)")
            out.append("")
            out.append("| Document | Flagged pages |")
            out.append("| --- | --- |")
            for path, count in top[:10]:
                out.append(f"| {path} | {count} |")
            if len(top) > 10:
                out.append(f"| … and {len(top) - 10} more | |")
            out.append("")

    if not attention_added:
        out.append("✅ Nothing flagged. All documents extracted cleanly.")
        out.append("")

    # Per-folder breakdown
    out.append("## Per-Folder Breakdown")
    out.append("")
    folders: dict[str, dict[str, int]] = {}
    for d in document_records:
        f = folders.setdefault(
            d.get("piece_folder", ""),
            {"docs": 0, "pages": 0, "text": 0, "ocr": 0, "scanned": 0, "errors": 0},
        )
        f["docs"] += 1
        f["pages"] += int(d.get("page_count", 0))
        f["text"] += int(d.get("pages_with_text", 0))
        f["ocr"] += int(d.get("pages_needing_ocr", 0))
        f["scanned"] += int(d.get("pages_image_based", 0))
        if d.get("processing_status") == "partial_error":
            f["errors"] += 1
    out.append("| Folder | Docs | Pages | Text | Needs OCR | Scanned | Errors |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for name in sorted(folders):
        f = folders[name]
        out.append(
            f"| {name or '(root)'} | {f['docs']} | {f['pages']} | {f['text']} | "
            f"{f['ocr']} | {f['scanned']} | {f['errors']} |"
        )
    out.append("")

    # Per-document detail
    out.append("## Per-Document Detail")
    out.append("")
    limit = int(meta["detail_limit"])
    shown = document_records[:limit]
    out.append(
        f"<details><summary>Show {len(shown)} of {total_docs} documents</summary>"
    )
    out.append("")
    out.append(
        "| | Document | Pages | Text | OCR | Scanned | Detected header |"
    )
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for d in shown:
        headers = d.get("first_page_header_candidates") or []
        header = headers[0] if headers else ""
        header = header.replace("|", "\\|")[:40]
        out.append(
            f"| {_doc_status_icon(d)} | {d.get('pdf_path', '')} | "
            f"{d.get('page_count', 0)} | {d.get('pages_with_text', 0)} | "
            f"{d.get('pages_needing_ocr', 0)} | {d.get('pages_image_based', 0)} | "
            f"{header} |"
        )
    out.append("")
    out.append("</details>")
    out.append("")
    if total_docs > limit:
        out.append(
            f"> Showing first {limit} of {total_docs} documents. "
            "See `documents.jsonl` for the complete dataset."
        )
        out.append("")

    # Configuration
    out.append("## Configuration and Environment")
    out.append("")
    out.append("| Setting | Value |")
    out.append("| --- | --- |")
    out.append(f"| Record schema version | {RECORD_VERSION} |")
    out.append(f"| Library root | `{meta['library_root']}` |")
    out.append(f"| Inventory input | `{meta['inventory']}` |")
    out.append(f"| Render DPI | {meta['render_dpi']} |")
    out.append(f"| Rendering enabled | {'yes' if meta['enable_rendering'] else 'no'} |")
    out.append(
        f"| Image metrics enabled | {'yes' if meta['enable_image_metrics'] else 'no'} |"
    )
    out.append(f"| numpy available | {'yes' if meta['numpy_available'] else 'no'} |")
    out.append(f"| opencv available | {'yes' if meta['opencv_available'] else 'no'} |")
    out.append(f"| OCR enabled | {'yes' if meta.get('ocr_enabled') else 'no'} |")
    if meta.get("ocr_enabled"):
        out.append(f"| OCR engine | tesseract {meta.get('ocr_engine_version', '')} |")
        out.append(f"| OCR DPI / language | {meta.get('ocr_dpi')} / {meta.get('ocr_lang')} |")
    out.append(f"| Text output | `{meta['output_text']}` |")
    out.append(f"| Pages output | `{meta['output_pages']}` |")
    out.append(f"| Documents output | `{meta['output_documents']}` |")
    out.append("")
    out.append("Status legend: ✅ text on every page • 🔍 needs OCR • "
               "🖼️ scanned/image-based • ❌ processing error.")
    out.append("")

    return "\n".join(out) + "\n"


@app.command()
def main(
    library_root: Path = typer.Option(
        ..., exists=True, file_okay=False, dir_okay=True,
        help="Root directory that inventory pdf_path values are relative to",
    ),
    inventory: Path = typer.Option(
        Path("data/raw_inventory.jsonl"), help="Script 01 inventory JSONL input"
    ),
    output_text: Path = typer.Option(
        Path("data/extracted_text.jsonl"), help="Per-page text output"
    ),
    output_pages: Path = typer.Option(
        Path("data/pages.jsonl"), help="Per-page feature output"
    ),
    output_documents: Path = typer.Option(
        Path("data/documents.jsonl"), help="Per-document rollup output"
    ),
    output_report: Path = typer.Option(
        Path("data/extraction_report.md"), help="Human-readable Markdown summary output"
    ),
    write_report: bool = typer.Option(
        True, "--report/--no-report", help="Write the Markdown summary report"
    ),
    report_detail_limit: int = typer.Option(
        200, help="Max rows in the per-document detail table of the report"
    ),
    cache_dir: Path = typer.Option(Path("cache"), help="Base cache directory"),
    mode: str = typer.Option("full", help="Processing mode: full or incremental"),
    render_dpi: int = typer.Option(150, help="Thumbnail render DPI"),
    enable_rendering: bool = typer.Option(
        True, "--enable-rendering/--no-rendering", help="Toggle page rendering + features"
    ),
    enable_image_metrics: bool = typer.Option(
        True,
        "--enable-image-metrics/--no-image-metrics",
        help="Toggle blur/skew/contrast image-quality metrics",
    ),
    enable_ocr: bool = typer.Option(
        True, "--ocr/--no-ocr", help="Run Tesseract OCR + OSD on scanned/no-text pages"
    ),
    ocr_dpi: int = typer.Option(DEFAULT_OCR_DPI, help="Dedicated OCR render DPI"),
    ocr_lang: str = typer.Option(DEFAULT_OCR_LANG, help="Tesseract language(s), e.g. 'eng'"),
    ocr_multipass: bool = typer.Option(
        True,
        "--ocr-multipass/--single-pass-ocr",
        help="Run multiple OCR passes (varied PSM + DPI) and keep all as LLM candidates",
    ),
    ocr_workers: int = typer.Option(
        0,
        help="Concurrent OCR worker threads (0 = auto min(8, CPU); 1 = serial). Also bounds "
        "concurrent OCR-LLM classification calls.",
    ),
    ocr_llm: bool = typer.Option(
        True,
        "--ocr-llm/--no-ocr-llm",
        help="Consolidate multi-pass OCR into an instrument list via the Copilot CLI (one call/PDF)",
    ),
    ocr_llm_command: str = typer.Option("copilot", help="Copilot CLI command for OCR-LLM"),
    ocr_llm_model: str = typer.Option("", help="Model for OCR-LLM consolidation (else CLI default)"),
    ocr_llm_page_scope: str = typer.Option(
        "all", help="OCR-LLM page scope: 'all' pages or 'first' page only"
    ),
    ocr_llm_timeout: float = typer.Option(300.0, help="OCR-LLM Copilot CLI timeout (seconds)"),
    use_vision: bool = typer.Option(
        True,
        "--use-vision/--no-vision",
        help="Classify notation source (printed/handwritten) from the page image via the Copilot "
        "CLI (scanned documents only)",
    ),
    vision_command: str = typer.Option("copilot", help="Copilot CLI command for the vision review"),
    vision_model: str = typer.Option("", help="Model for the vision review (else CLI default)"),
    vision_page_scope: str = typer.Option(
        "first", help="Vision page scope: 'first' page only or 'all' pages"
    ),
    vision_timeout: float = typer.Option(300.0, help="Vision Copilot CLI timeout (seconds)"),
    tesseract_cmd: str = typer.Option(
        "", help="Path to the tesseract binary (else PATH/common dirs are searched)"
    ),
    log_level: str = typer.Option("INFO", help="DEBUG, INFO, WARNING, ERROR"),
) -> None:
    """Extract per-page embedded text and render page thumbnails/features."""
    mode = mode.lower().strip()
    if mode not in {"full", "incremental"}:
        raise typer.BadParameter("mode must be 'full' or 'incremental'")
    if render_dpi < 24 or render_dpi > 600:
        raise typer.BadParameter("render-dpi must be between 24 and 600")
    if ocr_dpi < 72 or ocr_dpi > 1200:
        raise typer.BadParameter("ocr-dpi must be between 72 and 1200")
    if ocr_workers < 0:
        raise typer.BadParameter("ocr-workers must be >= 0")
    if ocr_workers == 0:
        ocr_workers = min(8, os.cpu_count() or 1)
    ocr_llm_page_scope = (ocr_llm_page_scope or "all").lower().strip()
    if ocr_llm_page_scope not in {"all", "first"}:
        raise typer.BadParameter("ocr-llm-page-scope must be 'all' or 'first'")

    vision_page_scope = (vision_page_scope or "first").lower().strip()
    if vision_page_scope not in {"all", "first"}:
        raise typer.BadParameter("vision-page-scope must be 'all' or 'first'")

    setup_logging(log_level)

    if fitz is None:
        logger.error("pymupdf is not installed; cannot run extraction")
        raise typer.Exit(code=1)

    if enable_image_metrics and np is None:
        logger.warning(
            "numpy is not installed; disabling image-quality metrics "
            "(blur/skew/contrast will be null)"
        )
        enable_image_metrics = False
    if enable_image_metrics and cv2 is None:
        logger.warning(
            "opencv-python is not installed; skew_angle_deg will be null"
        )

    ocr_engine_version = ""
    if enable_ocr:
        if pytesseract is None or Image is None:
            logger.warning(
                "pytesseract/Pillow not installed; disabling OCR "
                "(ocr_text will be null and pages stay flagged needs_ocr)"
            )
            enable_ocr = False
        else:
            cmd = resolve_tesseract_cmd(tesseract_cmd or None)
            if cmd is None:
                logger.warning(
                    "Tesseract binary not found on PATH or common install dirs; "
                    "disabling OCR. Install Tesseract or pass --tesseract-cmd"
                )
                enable_ocr = False
            else:
                pytesseract.pytesseract.tesseract_cmd = cmd
                try:
                    ocr_engine_version = str(pytesseract.get_tesseract_version())
                    logger.info(
                        "OCR enabled: tesseract %s at %s (dpi=%d lang=%s)",
                        ocr_engine_version, cmd, ocr_dpi, ocr_lang,
                    )
                except Exception as exc:
                    logger.warning(
                        "Tesseract found at %s but not runnable (%s); disabling OCR",
                        cmd, exc,
                    )
                    enable_ocr = False
    ocr_config = OcrConfig(
        enabled=enable_ocr, dpi=ocr_dpi, lang=ocr_lang, engine_version=ocr_engine_version,
        multipass=ocr_multipass,
    )

    if ocr_llm and not shutil.which(ocr_llm_command):
        logger.warning(
            "Copilot CLI '%s' not found on PATH; disabling OCR-LLM consolidation.",
            ocr_llm_command,
        )
        ocr_llm = False
    if ocr_llm and not enable_ocr:
        logger.warning(
            "OCR-LLM consolidation requires OCR, which is disabled; disabling OCR-LLM."
        )
        ocr_llm = False
    ocr_llm_config = OcrLlmConfig(
        enabled=ocr_llm, command=ocr_llm_command, model=ocr_llm_model,
        timeout_seconds=ocr_llm_timeout, page_scope=ocr_llm_page_scope,
    )
    if ocr_llm:
        logger.info(
            "OCR-LLM consolidation enabled (command=%s, model=%s, page_scope=%s)",
            ocr_llm_command, ocr_llm_model or "CLI default", ocr_llm_page_scope,
        )

    if use_vision and not enable_rendering:
        logger.warning(
            "Vision review requires page rendering, which is disabled; disabling vision review."
        )
        use_vision = False
    if use_vision and not shutil.which(vision_command):
        logger.warning(
            "Copilot CLI '%s' not found on PATH; disabling vision review.",
            vision_command,
        )
        use_vision = False
    vision_config = VisionConfig(
        enabled=use_vision, command=vision_command, model=vision_model,
        timeout_seconds=vision_timeout, page_scope=vision_page_scope,
    )
    if use_vision:
        logger.info(
            "Vision review enabled (command=%s, model=%s, page_scope=%s)",
            vision_command, vision_model or "CLI default", vision_page_scope,
        )

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    library_root = library_root.resolve()
    inventory = inventory.resolve()
    output_text = output_text.resolve()
    output_pages = output_pages.resolve()
    output_documents = output_documents.resolve()
    output_report = output_report.resolve()
    cache_dir = cache_dir.resolve()
    workspace_root = Path.cwd().resolve()

    if not inventory.exists():
        raise typer.BadParameter(f"inventory not found: {inventory}")

    logger.info("Extraction starting: mode=%s inventory=%s", mode, inventory)

    items, skipped = load_inventory(inventory)
    # Process folder-by-folder for readable, deterministic progress reporting.
    # Final outputs are re-sorted independently, so processing order is cosmetic.
    items.sort(key=lambda it: (it.piece_folder, it.pdf_path))
    total_items = len(items)
    total_folders = len({item.piece_folder for item in items})
    total_pages_expected = sum(item.page_count for item in items)
    logger.info(
        "Loaded %d readable PDFs across %d folders (~%d pages); %d skipped",
        total_items,
        total_folders,
        total_pages_expected,
        skipped,
    )

    checkpoint_path = make_checkpoint_path(output_text, CHECKPOINT_FILENAME)
    checkpoint = load_checkpoint(checkpoint_path, RECORD_VERSION, logger)
    prior_fingerprints: dict[str, str] = (
        checkpoint.get("fingerprints", {}) if mode == "incremental" else {}
    )

    prior_text_by_path: dict[str, list[dict[str, Any]]] = {}
    prior_pages_by_path: dict[str, list[dict[str, Any]]] = {}
    prior_documents_by_path: dict[str, dict[str, Any]] = {}
    if mode == "incremental":
        for rec in read_jsonl(output_text):
            prior_text_by_path.setdefault(rec.get("pdf_path", ""), []).append(rec)
        for rec in read_jsonl(output_pages):
            prior_pages_by_path.setdefault(rec.get("pdf_path", ""), []).append(rec)
        for rec in read_jsonl(output_documents):
            prior_documents_by_path[rec.get("pdf_path", "")] = rec

        current_paths = {item.pdf_path for item in items}
        for orphan in sorted(set(prior_text_by_path) - current_paths):
            logger.info("Orphaned PDF (no longer in inventory): %s", orphan)

    text_records: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    document_records: list[dict[str, Any]] = []
    processed_pdfs = 0
    reused_pdfs = 0
    pdf_errors = 0
    page_error_total = 0
    pages_done = 0
    processed_fingerprints: dict[str, str] = {}

    start_time = time.monotonic()
    current_folder: str | None = None
    folder_index = 0

    def _flush_progress() -> None:
        # Persist after each PDF so an interrupted run keeps finished work (page renders/OCR are
        # already cached) and can resume via --mode incremental. All three JSONLs and the
        # checkpoint are rewritten atomically; the checkpoint carries only completed fingerprints
        # so a resumed run reuses done PDFs and retries the rest.
        atomic_write_jsonl(output_text, sort_records(text_records))
        atomic_write_jsonl(output_pages, sort_records(page_records))
        atomic_write_jsonl(output_documents, sort_documents(document_records))
        checkpoint_now = build_checkpoint(
            RECORD_VERSION,
            run_id,
            dict(processed_fingerprints),
            inventory_input=normalize_rel_path(inventory),
            extracted_text_output=normalize_rel_path(output_text),
            pages_output=normalize_rel_path(output_pages),
            documents_output=normalize_rel_path(output_documents),
            library_root=normalize_rel_path(library_root),
            pdf_count_processed=processed_pdfs,
            page_count_processed=pages_done,
            reused_pdf_count=reused_pdfs,
            ocr_enabled=ocr_config.enabled,
            ocr_engine_version=ocr_config.engine_version,
        )
        atomic_write_json(checkpoint_path, checkpoint_now)

    # OCR runs each scanned page's Tesseract passes concurrently in this pool (rendering stays on
    # the main thread). The LLM pool runs the per-file classification asynchronously: once a file
    # is OCR'd we spin off its Copilot call and move on to the next PDF, then collect the results
    # after the loop. Both pools are bounded by --ocr-workers.
    ocr_executor = (
        ThreadPoolExecutor(max_workers=ocr_workers)
        if enable_ocr and ocr_workers > 1
        else None
    )
    llm_executor = (
        ThreadPoolExecutor(max_workers=ocr_workers) if ocr_llm_config.enabled else None
    )
    vision_executor = (
        ThreadPoolExecutor(max_workers=ocr_workers) if vision_config.enabled else None
    )
    if ocr_executor is not None:
        logger.info("OCR parallelism enabled: %d worker threads", ocr_workers)
    if llm_executor is not None:
        # Warm the shared taxonomy/prompt caches once so worker threads don't race to build them.
        _ocr_llm_allowed_instruments()
        _ocr_llm_template()
    if vision_executor is not None:
        _vision_template()
    # (LLM future, its document record) pairs to resolve and patch after the OCR loop.
    llm_futures: list[tuple[Future, dict[str, Any]]] = []
    # (Vision future, its document record) pairs to resolve and patch after the loop.
    vision_futures: list[tuple[Future, dict[str, Any]]] = []

    def _doc_is_scanned(doc_record: dict[str, Any]) -> bool:
        # Born-digital text PDFs are already, correctly, printed originals; vision only adds value
        # for scanned/image-based documents where handwriting is possible.
        return bool(
            (doc_record.get("pages_image_based") or 0) > 0
            or (doc_record.get("pages_needing_ocr") or 0) > 0
        )

    for idx, item in enumerate(items, start=1):
        if item.piece_folder != current_folder:
            current_folder = item.piece_folder
            folder_index += 1
            elapsed = time.monotonic() - start_time
            logger.info(
                "Folder %d/%d (%.0f%% files, %.1fs elapsed): %s",
                folder_index,
                total_folders,
                100.0 * (idx - 1) / total_items if total_items else 100.0,
                elapsed,
                current_folder or "(root)",
            )

        label = f"[{idx}/{total_items}] {item.pdf_path}"

        if (
            mode == "incremental"
            and item.file_fingerprint
            and prior_fingerprints.get(item.pdf_path) == item.file_fingerprint
            and item.pdf_path in prior_text_by_path
        ):
            text_records.extend(prior_text_by_path[item.pdf_path])
            page_records.extend(prior_pages_by_path.get(item.pdf_path, []))
            if item.pdf_path in prior_documents_by_path:
                reused_doc = prior_documents_by_path[item.pdf_path]
                document_records.append(reused_doc)
                # Enabling vision on an already-extracted library should add the vision signal to
                # reused documents without re-OCR: fire only when it's a scanned doc still missing a
                # successful vision result (the disk cache prevents rework across runs).
                if (
                    vision_executor is not None
                    and _doc_is_scanned(reused_doc)
                    and reused_doc.get("vision_status") != "success"
                ):
                    vfut = vision_executor.submit(
                        classify_notation_vision, item,
                        prior_pages_by_path.get(item.pdf_path, []),
                        cache_dir, workspace_root, vision_config,
                    )
                    vision_futures.append((vfut, reused_doc))
            reused_pdfs += 1
            processed_fingerprints[item.pdf_path] = item.file_fingerprint
            logger.info("%s reused (unchanged)", label)
            _flush_progress()
            continue

        abs_path = (library_root / item.pdf_path).resolve()
        if not abs_path.exists():
            pdf_errors += 1
            logger.error("%s missing on disk, skipping", label)
            continue

        pdf_start = time.monotonic()
        try:
            pdf_text, pdf_pages, pdf_document, page_errs, page_total = process_pdf(
                item,
                abs_path,
                cache_dir,
                workspace_root,
                run_id,
                render_dpi,
                enable_rendering,
                enable_image_metrics,
                ocr_config,
                ocr_executor,
            )
        except Exception as exc:
            pdf_errors += 1
            logger.error("%s failed: %s", label, exc)
            continue

        text_records.extend(pdf_text)
        page_records.extend(pdf_pages)
        document_records.append(pdf_document)
        if llm_executor is not None:
            # Spin off the per-file classification and move on to the next PDF's OCR.
            fut = llm_executor.submit(
                consolidate_part_instruments, item, pdf_text, cache_dir, ocr_llm_config
            )
            llm_futures.append((fut, pdf_document))
        if vision_executor is not None and _doc_is_scanned(pdf_document):
            vfut = vision_executor.submit(
                classify_notation_vision, item, pdf_pages, cache_dir, workspace_root,
                vision_config,
            )
            vision_futures.append((vfut, pdf_document))
        page_error_total += page_errs
        pages_done += page_total
        processed_pdfs += 1
        processed_fingerprints[item.pdf_path] = item.file_fingerprint

        pdf_elapsed = time.monotonic() - pdf_start
        error_note = f", {page_errs} page errors" if page_errs else ""
        logger.info(
            "%s done: %d pages in %.1fs%s (processed=%d reused=%d, %d pages total)",
            label,
            page_total,
            pdf_elapsed,
            error_note,
            processed_pdfs,
            reused_pdfs,
            pages_done,
        )
        _flush_progress()

    # Collect the asynchronous per-file LLM classifications and patch their document records.
    if llm_futures:
        logger.info("Waiting on %d OCR-LLM classification(s)...", len(llm_futures))
        for fut, doc_rec in llm_futures:
            try:
                result = fut.result()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "OCR-LLM classification failed for %s: %s", doc_rec.get("pdf_path"), exc
                )
                result = _empty_ocr_llm_result(f"error: {exc}")
            if result is not None:
                apply_llm_fields(doc_rec, result)
    if vision_futures:
        logger.info("Waiting on %d vision review(s)...", len(vision_futures))
        for vfut, doc_rec in vision_futures:
            try:
                vresult = vfut.result()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning(
                    "Vision review failed for %s: %s", doc_rec.get("pdf_path"), exc
                )
                vresult = _empty_vision_result(f"error: {exc}")
            if vresult is not None:
                apply_vision_fields(doc_rec, vresult)
    if ocr_executor is not None:
        ocr_executor.shutdown(wait=True)
    if llm_executor is not None:
        llm_executor.shutdown(wait=True)
    if vision_executor is not None:
        vision_executor.shutdown(wait=True)

    text_records = sort_records(text_records)
    page_records = sort_records(page_records)
    document_records = sort_documents(document_records)
    _flush_progress()

    total_elapsed = time.monotonic() - start_time
    logger.info(
        "Extraction completed in %.1fs: pdfs=%d reused=%d pdf_errors=%d pages=%d "
        "page_errors=%d text_out=%s pages_out=%s docs_out=%s",
        total_elapsed,
        processed_pdfs,
        reused_pdfs,
        pdf_errors,
        pages_done,
        page_error_total,
        output_text,
        output_pages,
        output_documents,
    )

    if write_report:
        report_meta = {
            "generated_at": utc_now_iso(),
            "run_id": run_id,
            "mode": mode,
            "elapsed_seconds": total_elapsed,
            "processed_pdfs": processed_pdfs,
            "reused_pdfs": reused_pdfs,
            "pdf_errors": pdf_errors,
            "skipped": skipped,
            "render_dpi": render_dpi,
            "enable_rendering": enable_rendering,
            "enable_image_metrics": enable_image_metrics,
            "numpy_available": np is not None,
            "opencv_available": cv2 is not None,
            "ocr_enabled": ocr_config.enabled,
            "ocr_engine_version": ocr_config.engine_version,
            "ocr_dpi": ocr_config.dpi,
            "ocr_lang": ocr_config.lang,
            "detail_limit": report_detail_limit,
            "library_root": normalize_rel_path(library_root),
            "inventory": normalize_rel_path(inventory),
            "output_text": normalize_rel_path(output_text),
            "output_pages": normalize_rel_path(output_pages),
            "output_documents": normalize_rel_path(output_documents),
        }
        try:
            report_md = build_markdown_report(
                text_records, page_records, document_records, report_meta
            )
            atomic_write_text(output_report, report_md)
            logger.info("Wrote Markdown report: %s", output_report)
        except Exception as exc:  # pragma: no cover - report is non-critical
            logger.warning("Failed to write Markdown report: %s", exc)


if __name__ == "__main__":
    app()
