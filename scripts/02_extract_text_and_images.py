"""Script 02: extract embedded text and render page images/features.

Phase 1 (pymupdf only):
- per-page embedded text extraction
- per-page PNG render into cache/render
- lightweight page features (text_density, black_white_ratio)
- full and incremental modes mirroring Script 01
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

try:
    import fitz  # type: ignore
except Exception:  # pragma: no cover
    fitz = None

from scripts._common import (
    atomic_write_json,
    atomic_write_jsonl,
    normalize_rel_path,
    read_json,
    read_jsonl,
    sha256_text,
    utc_now_iso,
)

RECORD_VERSION = "1.0"
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


def build_text_record(
    item: InventoryItem,
    run_id: str,
    page_num: int,
    embedded_text: str | None,
    status: str,
    error_message: str | None,
) -> dict[str, Any]:
    text_value = embedded_text or ""
    is_searchable = bool(text_value.strip()) and any(c.isalnum() for c in text_value)
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
) -> dict[str, Any]:
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
        "render_timestamp": utc_now_iso(),
        "processing_status": status,
        "error_message": error_message,
    }


def render_page(
    page: Any,
    cache_dir: Path,
    workspace_root: Path,
    item: InventoryItem,
    page_num: int,
    render_dpi: int,
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

        text_density, black_white_ratio = compute_features_from_gray(bytes(gray.samples))

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
            "text_density": text_density,
            "black_white_ratio": black_white_ratio,
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, int]:
    """Return (text_records, page_records, page_error_count, pages_total)."""
    text_records: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    page_errors = 0

    doc = fitz.open(abs_path)  # type: ignore[attr-defined]
    try:
        page_total = int(doc.page_count)
        for page_idx in range(page_total):
            page_num = page_idx + 1
            page: Any = None
            try:
                page = doc[page_idx]
                embedded_text = page.get_text()
                text_records.append(
                    build_text_record(item, run_id, page_num, embedded_text, "success", None)
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
                page, cache_dir, workspace_root, item, page_num, render_dpi
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
                )
            )
        return text_records, page_records, page_errors, page_total
    finally:
        doc.close()


def sort_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(records, key=lambda r: (r.get("pdf_path", ""), r.get("page_num", 0)))


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
    cache_dir: Path = typer.Option(Path("cache"), help="Base cache directory"),
    mode: str = typer.Option("full", help="Processing mode: full or incremental"),
    render_dpi: int = typer.Option(150, help="Thumbnail render DPI"),
    enable_rendering: bool = typer.Option(
        True, "--enable-rendering/--no-rendering", help="Toggle page rendering + features"
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

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    library_root = library_root.resolve()
    inventory = inventory.resolve()
    output_text = output_text.resolve()
    output_pages = output_pages.resolve()
    cache_dir = cache_dir.resolve()
    workspace_root = Path.cwd().resolve()

    if not inventory.exists():
        raise typer.BadParameter(f"inventory not found: {inventory}")

    logger.info("Extraction starting: mode=%s inventory=%s", mode, inventory)

    items, skipped = load_inventory(inventory)
    logger.info("Loaded %d readable PDFs (%d skipped)", len(items), skipped)

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
    if mode == "incremental":
        for rec in read_jsonl(output_text):
            prior_text_by_path.setdefault(rec.get("pdf_path", ""), []).append(rec)
        for rec in read_jsonl(output_pages):
            prior_pages_by_path.setdefault(rec.get("pdf_path", ""), []).append(rec)

        current_paths = {item.pdf_path for item in items}
        for orphan in sorted(set(prior_text_by_path) - current_paths):
            logger.info("Orphaned PDF (no longer in inventory): %s", orphan)

    text_records: list[dict[str, Any]] = []
    page_records: list[dict[str, Any]] = []
    processed_pdfs = 0
    reused_pdfs = 0
    pdf_errors = 0
    page_error_total = 0
    pages_done = 0

    for item in items:
        if (
            mode == "incremental"
            and item.file_fingerprint
            and prior_fingerprints.get(item.pdf_path) == item.file_fingerprint
            and item.pdf_path in prior_text_by_path
        ):
            text_records.extend(prior_text_by_path[item.pdf_path])
            page_records.extend(prior_pages_by_path.get(item.pdf_path, []))
            reused_pdfs += 1
            continue

        abs_path = (library_root / item.pdf_path).resolve()
        if not abs_path.exists():
            pdf_errors += 1
            logger.error("PDF missing on disk, skipping: %s", item.pdf_path)
            continue

        try:
            pdf_text, pdf_pages, page_errs, page_total = process_pdf(
                item,
                abs_path,
                cache_dir,
                workspace_root,
                run_id,
                render_dpi,
                enable_rendering,
            )
        except Exception as exc:
            pdf_errors += 1
            logger.error("Failed to process %s: %s", item.pdf_path, exc)
            continue

        text_records.extend(pdf_text)
        page_records.extend(pdf_pages)
        page_error_total += page_errs
        pages_done += page_total
        processed_pdfs += 1

    text_records = sort_records(text_records)
    page_records = sort_records(page_records)
    atomic_write_jsonl(output_text, text_records)
    atomic_write_jsonl(output_pages, page_records)

    new_checkpoint = {
        "record_version": RECORD_VERSION,
        "last_run_id": run_id,
        "last_run_timestamp": utc_now_iso(),
        "inventory_input": normalize_rel_path(inventory),
        "extracted_text_output": normalize_rel_path(output_text),
        "pages_output": normalize_rel_path(output_pages),
        "library_root": normalize_rel_path(library_root),
        "fingerprints": {item.pdf_path: item.file_fingerprint for item in items},
        "pdf_count_processed": processed_pdfs,
        "page_count_processed": pages_done,
        "reused_pdf_count": reused_pdfs,
    }
    atomic_write_json(checkpoint_path, new_checkpoint)

    logger.info(
        "Extraction completed: pdfs=%d reused=%d pdf_errors=%d pages=%d page_errors=%d "
        "text_out=%s pages_out=%s",
        processed_pdfs,
        reused_pdfs,
        pdf_errors,
        pages_done,
        page_error_total,
        output_text,
        output_pages,
    )


if __name__ == "__main__":
    app()
