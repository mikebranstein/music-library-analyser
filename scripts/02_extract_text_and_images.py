"""Script 02: extract embedded text and render page images/features.

Consumes the Script 01 inventory and produces three JSONL datasets:
- ``extracted_text.jsonl``: per-page text + text-quality signals + header candidates
- ``pages.jsonl``: per-page render, geometry, scanned/DPI, and image-quality metrics
- ``documents.jsonl``: per-PDF rollup for downstream identity/quality stages

Extraction/rendering uses ``pymupdf``. Image-quality metrics (blur/skew/contrast) use
``numpy`` and ``opencv-python`` when available and degrade gracefully to ``null`` otherwise.
"""

from __future__ import annotations

import logging
import time
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

from scripts._common import (
    atomic_write_json,
    atomic_write_jsonl,
    normalize_rel_path,
    read_json,
    read_jsonl,
    sha256_text,
    utc_now_iso,
)

RECORD_VERSION = "2.0"
EXTRACTION_METHOD = "pymupdf_embedded"

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


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def get_checkpoint_path(output_text: Path) -> Path:
    return output_text.parent / ".extraction_checkpoint.json"


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


def extract_header_candidates(page: Any, limit: int = 8) -> tuple[list[str], list[str]]:
    """Return (prominent largest-font strings, top-of-page lines) for a page."""
    try:
        data = page.get_text("dict")
    except Exception:
        return [], []
    spans: list[tuple[float, float, str]] = []
    for block in data.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = (span.get("text") or "").strip()
                if not text:
                    continue
                size = float(span.get("size", 0.0))
                bbox = span.get("bbox", [0, 0, 0, 0])
                y0 = float(bbox[1]) if len(bbox) > 1 else 0.0
                spans.append((size, y0, text))
    if not spans:
        return [], []
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
    return prominent, top_lines


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
) -> dict[str, Any]:
    text_value = embedded_text or ""
    is_searchable = bool(text_value.strip()) and any(c.isalnum() for c in text_value)
    signals = compute_text_signals(embedded_text)
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
        "ocr_text": None,
        "ocr_confidence": None,
        "word_count": signals["word_count"],
        "alnum_ratio": signals["alnum_ratio"],
        "page_text_hash": signals["page_text_hash"],
        "header_text_candidates": header_candidates or [],
        "top_lines": top_lines or [],
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
) -> dict[str, Any]:
    geometry = geometry or {}
    image_analysis = image_analysis or {}
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
) -> dict[str, Any]:
    """Aggregate per-page results into a single per-PDF rollup record."""
    pages_with_text = sum(1 for r in text_records if r.get("text_is_searchable"))
    pages_needing_ocr = sum(1 for r in text_records if r.get("needs_ocr"))
    total_text_length = sum(int(r.get("embedded_text_length", 0)) for r in text_records)
    total_word_count = sum(int(r.get("word_count", 0)) for r in text_records)
    pages_image_based = sum(1 for r in page_records if r.get("is_image_based"))
    page_count = len(text_records)
    ocr_fraction = round(pages_needing_ocr / page_count, 4) if page_count else 0.0
    image_based_fraction = round(pages_image_based / page_count, 4) if page_count else 0.0

    first_page_text = None
    first_page_headers: list[str] = []
    if text_records:
        first = min(text_records, key=lambda r: r.get("page_num", 0))
        first_page_text = first.get("embedded_text")
        first_page_headers = first.get("header_text_candidates", []) or []

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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any], int, int]:
    """Return (text_records, page_records, document_record, page_error_count, pages_total)."""
    text_records: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    page_errors = 0

    doc = fitz.open(abs_path)  # type: ignore[attr-defined]
    try:
        page_total = int(doc.page_count)
        for page_idx in range(page_total):
            page_num = page_idx + 1
            page: Any = None
            embedded_text: str | None = None
            header_candidates: list[str] = []
            top_lines: list[str] = []
            geometry: dict[str, Any] = {}
            image_analysis: dict[str, Any] = {}
            try:
                page = doc[page_idx]
                embedded_text = page.get_text()
                header_candidates, top_lines = extract_header_candidates(page)
                geometry = analyze_page_geometry(page)
                image_analysis = analyze_page_images(page, len((embedded_text or "").strip()))
                text_records.append(
                    build_text_record(
                        item, run_id, page_num, embedded_text, "success", None,
                        header_candidates, top_lines,
                    )
                )
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
                page_records.append(
                    build_page_record(
                        item, run_id, page_num, render_dpi, None, None, -1, -1, -1,
                        None, None, "success", None,
                        geometry=geometry, image_analysis=image_analysis,
                    )
                )
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
            page_records.append(
                build_page_record(
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
                )
            )
        doc_status = "partial_error" if page_errors else "success"
        document_record = build_document_record(
            item, run_id, text_records, page_records, doc_status
        )
        return text_records, page_records, document_record, page_errors, page_total
    finally:
        doc.close()


def sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: (r.get("pdf_path", ""), r.get("page_num", 0)))


def sort_documents(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: r.get("pdf_path", ""))


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
    log_level: str = typer.Option("INFO", help="DEBUG, INFO, WARNING, ERROR"),
) -> None:
    """Extract per-page embedded text and render page thumbnails/features."""
    mode = mode.lower().strip()
    if mode not in {"full", "incremental"}:
        raise typer.BadParameter("mode must be 'full' or 'incremental'")
    if render_dpi < 24 or render_dpi > 600:
        raise typer.BadParameter("render-dpi must be between 24 and 600")

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

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    library_root = library_root.resolve()
    inventory = inventory.resolve()
    output_text = output_text.resolve()
    output_pages = output_pages.resolve()
    output_documents = output_documents.resolve()
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

    checkpoint_path = get_checkpoint_path(output_text)
    checkpoint = read_json(checkpoint_path) or {}
    if checkpoint and checkpoint.get("record_version") != RECORD_VERSION:
        logger.warning("Checkpoint version mismatch; ignoring checkpoint")
        checkpoint = {}
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

    start_time = time.monotonic()
    current_folder: str | None = None
    folder_index = 0

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
                document_records.append(prior_documents_by_path[item.pdf_path])
            reused_pdfs += 1
            logger.info("%s reused (unchanged)", label)
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
            )
        except Exception as exc:
            pdf_errors += 1
            logger.error("%s failed: %s", label, exc)
            continue

        text_records.extend(pdf_text)
        page_records.extend(pdf_pages)
        document_records.append(pdf_document)
        page_error_total += page_errs
        pages_done += page_total
        processed_pdfs += 1

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

    text_records = sort_records(text_records)
    page_records = sort_records(page_records)
    document_records = sort_documents(document_records)
    atomic_write_jsonl(output_text, text_records)
    atomic_write_jsonl(output_pages, page_records)
    atomic_write_jsonl(output_documents, document_records)

    new_checkpoint = {
        "record_version": RECORD_VERSION,
        "last_run_id": run_id,
        "last_run_timestamp": utc_now_iso(),
        "inventory_input": normalize_rel_path(inventory),
        "extracted_text_output": normalize_rel_path(output_text),
        "pages_output": normalize_rel_path(output_pages),
        "documents_output": normalize_rel_path(output_documents),
        "library_root": normalize_rel_path(library_root),
        "fingerprints": {item.pdf_path: item.file_fingerprint for item in items},
        "pdf_count_processed": processed_pdfs,
        "page_count_processed": pages_done,
        "reused_pdf_count": reused_pdfs,
    }
    atomic_write_json(checkpoint_path, new_checkpoint)

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


if __name__ == "__main__":
    app()
