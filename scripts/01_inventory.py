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
    file_fingerprint,
    normalize_rel_path,
    read_json,
    read_jsonl,
    sha256_text,
    utc_now_iso,
)

RECORD_VERSION = "1.0"

app = typer.Typer(add_completion=False)


@dataclass(frozen=True)
class PdfEntry:
    abs_path: Path
    rel_path: str
    piece_folder: str
    piece_id: str


def setup_logging(log_level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


def to_iso_utc(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def hash_hex(hash_value: str) -> str:
    if ":" in hash_value:
        return hash_value.split(":", maxsplit=1)[1]
    return hash_value


def get_checkpoint_path(output: Path) -> Path:
    return output.parent / ".inventory_checkpoint.json"


def assert_output_path_writable(output: Path) -> None:
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        test_file = output.parent / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
    except OSError as exc:
        raise typer.BadParameter(
            f"Output directory is not writable: {output.parent} ({exc})"
        ) from exc


def discover_pdfs(library_root: Path) -> list[PdfEntry]:
    entries: list[PdfEntry] = []
    for pdf in sorted(library_root.rglob("*")):
        if pdf.suffix.lower() != ".pdf":
            continue
        if not pdf.is_file() or pdf.is_symlink():
            continue
        rel_file = normalize_rel_path(pdf.relative_to(library_root))
        piece_folder_path = pdf.parent.relative_to(library_root)
        piece_folder = normalize_rel_path(piece_folder_path)
        piece_hash = sha256_text(piece_folder)
        piece_id = hash_hex(piece_hash)[:16]
        entries.append(
            PdfEntry(
                abs_path=pdf,
                rel_path=rel_file,
                piece_folder=piece_folder,
                piece_id=piece_id,
            )
        )
    return entries


def extract_pdf_info(pdf_path: Path) -> dict[str, Any]:
    metadata = {
        "title": None,
        "author": None,
        "subject": None,
        "creator": None,
        "producer": None,
        "creation_date": None,
        "modification_date": None,
    }
    health_flags = {
        "is_zero_page": False,
        "is_malformed": False,
        "is_corrupted": False,
        "warnings": [],
    }

    if fitz is None:
        return {
            "page_count": -1,
            "pdf_readable": False,
            "is_encrypted": False,
            "pdf_metadata": metadata,
            "health_flags": {
                **health_flags,
                "warnings": ["pymupdf is not installed"],
            },
            "processing_status": "error",
            "error_message": "pymupdf is not installed",
        }

    try:
        with fitz.open(pdf_path) as doc:  # type: ignore[attr-defined]
            is_encrypted = bool(getattr(doc, "is_encrypted", False)) or bool(
                getattr(doc, "needs_pass", False)
            )
            if is_encrypted:
                health_flags["warnings"].append("PDF is encrypted")
                return {
                    "page_count": -1,
                    "pdf_readable": False,
                    "is_encrypted": True,
                    "pdf_metadata": metadata,
                    "health_flags": health_flags,
                    "processing_status": "error",
                    "error_message": "PDF is encrypted",
                }

            page_count = int(doc.page_count)
            if page_count == 0:
                health_flags["is_zero_page"] = True
                health_flags["warnings"].append("PDF has zero pages")

            raw_meta = doc.metadata or {}
            metadata = {
                "title": raw_meta.get("title"),
                "author": raw_meta.get("author"),
                "subject": raw_meta.get("subject"),
                "creator": raw_meta.get("creator"),
                "producer": raw_meta.get("producer"),
                "creation_date": raw_meta.get("creationDate"),
                "modification_date": raw_meta.get("modDate"),
            }

            status = "success_with_warnings" if health_flags["warnings"] else "success"
            return {
                "page_count": page_count,
                "pdf_readable": True,
                "is_encrypted": False,
                "pdf_metadata": metadata,
                "health_flags": health_flags,
                "processing_status": status,
                "error_message": None,
            }
    except RuntimeError as exc:
        msg = str(exc)
        lowered = msg.lower()
        if "encrypt" in lowered:
            is_encrypted = True
            health_flags["warnings"].append("PDF is encrypted")
        else:
            is_encrypted = False
            if "corrupt" in lowered:
                health_flags["is_corrupted"] = True
            else:
                health_flags["is_malformed"] = True
            health_flags["warnings"].append(msg)

        return {
            "page_count": -1,
            "pdf_readable": False,
            "is_encrypted": is_encrypted,
            "pdf_metadata": metadata,
            "health_flags": health_flags,
            "processing_status": "error",
            "error_message": msg,
        }
    except Exception as exc:  # pragma: no cover
        msg = str(exc)
        health_flags["is_malformed"] = True
        health_flags["warnings"].append(msg)
        return {
            "page_count": -1,
            "pdf_readable": False,
            "is_encrypted": False,
            "pdf_metadata": metadata,
            "health_flags": health_flags,
            "processing_status": "error",
            "error_message": msg,
        }


def build_record(entry: PdfEntry, run_id: str) -> dict[str, Any]:
    stat = entry.abs_path.stat()
    modified_iso = to_iso_utc(stat.st_mtime)
    fingerprint = file_fingerprint(entry.rel_path, stat.st_size, stat.st_mtime)

    pdf_info = extract_pdf_info(entry.abs_path)

    record = {
        "record_version": RECORD_VERSION,
        "run_id": run_id,
        "piece_id": entry.piece_id,
        "piece_folder": entry.piece_folder,
        "piece_folder_hash": sha256_text(entry.piece_folder),
        "pdf_path": entry.rel_path,
        "pdf_filename": entry.abs_path.name,
        "file_size_bytes": int(stat.st_size),
        "modified_timestamp": modified_iso,
        "file_fingerprint": fingerprint,
        "page_count": pdf_info["page_count"],
        "pdf_readable": pdf_info["pdf_readable"],
        "is_encrypted": pdf_info["is_encrypted"],
        "pdf_metadata": pdf_info["pdf_metadata"],
        "health_flags": pdf_info["health_flags"],
        "processing_status": pdf_info["processing_status"],
        "error_message": pdf_info["error_message"],
        "processing_timestamp": utc_now_iso(),
    }
    return record


def build_error_record(
    entry: PdfEntry,
    run_id: str,
    error_message: str,
    stat: Any | None = None,
) -> dict[str, Any]:
    file_size = int(stat.st_size) if stat is not None else -1
    modified_iso = to_iso_utc(stat.st_mtime) if stat is not None else utc_now_iso()
    fingerprint = (
        file_fingerprint(entry.rel_path, stat.st_size, stat.st_mtime)
        if stat is not None
        else sha256_text(f"{entry.rel_path}|missing")
    )
    return {
        "record_version": RECORD_VERSION,
        "run_id": run_id,
        "piece_id": entry.piece_id,
        "piece_folder": entry.piece_folder,
        "piece_folder_hash": sha256_text(entry.piece_folder),
        "pdf_path": entry.rel_path,
        "pdf_filename": entry.abs_path.name,
        "file_size_bytes": file_size,
        "modified_timestamp": modified_iso,
        "file_fingerprint": fingerprint,
        "page_count": -1,
        "pdf_readable": False,
        "is_encrypted": False,
        "pdf_metadata": {
            "title": None,
            "author": None,
            "subject": None,
            "creator": None,
            "producer": None,
            "creation_date": None,
            "modification_date": None,
        },
        "health_flags": {
            "is_zero_page": False,
            "is_malformed": True,
            "is_corrupted": False,
            "warnings": [error_message],
        },
        "processing_status": "error",
        "error_message": error_message,
        "processing_timestamp": utc_now_iso(),
    }


def load_previous_record_map(output: Path) -> dict[str, dict[str, Any]]:
    previous = read_jsonl(output)
    return {rec["pdf_path"]: rec for rec in previous if "pdf_path" in rec}


def should_reuse_record(
    prior_record: dict[str, Any] | None,
    current_fingerprint: str,
) -> bool:
    if prior_record is None:
        return False
    return prior_record.get("file_fingerprint") == current_fingerprint


@app.command()
def main(
    library_root: Path = typer.Option(..., exists=True, file_okay=False, dir_okay=True),
    output: Path = typer.Option(Path("data/raw_inventory.jsonl")),
    mode: str = typer.Option("full", help="Processing mode: full or incremental"),
    log_level: str = typer.Option("INFO", help="DEBUG, INFO, WARNING, ERROR"),
) -> None:
    """Discover PDFs and write an inventory JSONL with metadata and health flags."""
    mode = mode.lower().strip()
    if mode not in {"full", "incremental"}:
        raise typer.BadParameter("mode must be 'full' or 'incremental'")

    setup_logging(log_level)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    library_root = library_root.resolve()
    output = output.resolve()
    assert_output_path_writable(output)
    logging.info("Inventory run starting: mode=%s root=%s", mode, library_root)

    checkpoint_path = get_checkpoint_path(output)
    checkpoint = read_json(checkpoint_path) or {}
    if checkpoint and checkpoint.get("record_version") != RECORD_VERSION:
        logging.warning(
            "Checkpoint version mismatch (found=%s expected=%s). Ignoring checkpoint.",
            checkpoint.get("record_version"),
            RECORD_VERSION,
        )
        checkpoint = {}

    previous_records_map = load_previous_record_map(output)

    entries = discover_pdfs(library_root)
    logging.info("Discovered %d PDFs", len(entries))

    if mode == "incremental":
        previous_pdf_paths = set(previous_records_map.keys())
        discovered_pdf_paths = {entry.rel_path for entry in entries}
        orphaned_paths = sorted(previous_pdf_paths - discovered_pdf_paths)
        for orphaned_path in orphaned_paths:
            logging.info("Orphaned PDF (no longer on disk): %s", orphaned_path)

    rebuilt_records: list[dict[str, Any]] = []
    success_count = 0
    warning_count = 0
    error_count = 0
    reused_count = 0

    for entry in entries:
        try:
            stat = entry.abs_path.stat()
            current_fingerprint = file_fingerprint(entry.rel_path, stat.st_size, stat.st_mtime)
        except FileNotFoundError:
            record = build_error_record(
                entry,
                run_id,
                "File deleted before processing",
            )
            rebuilt_records.append(record)
            error_count += 1
            logging.warning("Error on %s: %s", entry.rel_path, record["error_message"])
            continue
        except PermissionError:
            record = build_error_record(entry, run_id, "Permission denied")
            rebuilt_records.append(record)
            error_count += 1
            logging.warning("Error on %s: %s", entry.rel_path, record["error_message"])
            continue

        prior_record = previous_records_map.get(entry.rel_path)
        if mode == "incremental" and should_reuse_record(prior_record, current_fingerprint):
            rebuilt_records.append(prior_record)
            reused_count += 1
            continue

        try:
            record = build_record(entry, run_id)
        except PermissionError:
            record = build_error_record(entry, run_id, "Permission denied", stat=stat)
        except FileNotFoundError:
            record = build_error_record(entry, run_id, "File deleted before processing", stat=stat)
        except Exception as exc:
            logging.exception("Unexpected error on %s", entry.rel_path)
            record = build_error_record(entry, run_id, str(exc), stat=stat)

        rebuilt_records.append(record)

        status = record["processing_status"]
        if status == "success":
            success_count += 1
        elif status == "success_with_warnings":
            warning_count += 1
            warnings = record.get("health_flags", {}).get("warnings", [])
            logging.info("Warnings on %s: %s", entry.rel_path, "; ".join(warnings))
        else:
            error_count += 1
            logging.warning("Error on %s: %s", entry.rel_path, record["error_message"])

    rebuilt_records.sort(key=lambda rec: rec["pdf_path"])
    atomic_write_jsonl(output, rebuilt_records)

    new_checkpoint = {
        "record_version": RECORD_VERSION,
        "last_run_id": run_id,
        "last_run_timestamp": utc_now_iso(),
        "library_root": normalize_rel_path(library_root),
        "output": normalize_rel_path(output),
        "fingerprints": {rec["pdf_path"]: rec["file_fingerprint"] for rec in rebuilt_records},
        "record_count": len(rebuilt_records),
    }
    atomic_write_json(checkpoint_path, new_checkpoint)

    logging.info(
        "Inventory completed: total=%d reused=%d success=%d warnings=%d errors=%d output=%s",
        len(rebuilt_records),
        reused_count,
        success_count,
        warning_count,
        error_count,
        output,
    )


if __name__ == "__main__":
    app()
