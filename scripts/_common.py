from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar

# Unified log line format used by every pipeline script (change 1: shared logging).
LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"


# --- Shared output-vocabulary constants (change 5) -------------------------------------------
# Downstream scripts (05-08) must filter Script 04's records by these exact string values.
# Import them from here instead of hardcoding literals so a rename can never silently break a
# consumer's filter.


class ProcessingStatus:
    """Values of the ``processing_status`` field on every pipeline record."""

    SUCCESS = "success"
    ERROR = "error"
    SKIPPED_UNREADABLE = "skipped_unreadable"


class LookupStatus:
    """Values of Script 04's ``lookup_status`` field."""

    MATCHED = "matched"
    LOW_CONFIDENCE = "low_confidence"
    NO_MATCH = "no_match"
    DISABLED = "disabled"
    ERROR = "error"


class CompletenessTier:
    """Values of Script 04's ``completeness_tier`` field."""

    COMPLETE = "complete"
    NEAR_COMPLETE = "near_complete"
    INCOMPLETE = "incomplete"
    SEVERELY_INCOMPLETE = "severely_incomplete"
    UNKNOWN = "unknown"


# Canonical display/iteration order for report tables that group by these fields.
LOOKUP_STATUS_ORDER: tuple[str, ...] = (
    LookupStatus.MATCHED,
    LookupStatus.LOW_CONFIDENCE,
    LookupStatus.NO_MATCH,
    LookupStatus.DISABLED,
    LookupStatus.ERROR,
)
COMPLETENESS_TIER_ORDER: tuple[str, ...] = (
    CompletenessTier.COMPLETE,
    CompletenessTier.NEAR_COMPLETE,
    CompletenessTier.INCOMPLETE,
    CompletenessTier.SEVERELY_INCOMPLETE,
    CompletenessTier.UNKNOWN,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_rel_path(path: Path) -> str:
    return path.as_posix()


def sha256_text(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def file_fingerprint(rel_path: str, size: int, mtime: float) -> str:
    basis = f"{rel_path}|{size}|{mtime}"
    return sha256_text(basis)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def atomic_write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=True) + "\n")
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except json.JSONDecodeError as exc:
        logging.warning("Failed to parse JSON file %s: %s", path, exc)
        return None

    if not isinstance(payload, dict):
        logging.warning("JSON file %s did not contain an object payload", path)
        return None
    return payload


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError as exc:
                logging.warning(
                    "Skipping malformed JSONL line %d in %s: %s",
                    line_num,
                    path,
                    exc,
                )
                continue
            if not isinstance(item, dict):
                logging.warning(
                    "Skipping non-object JSONL line %d in %s",
                    line_num,
                    path,
                )
                continue
            records.append(item)
    return records


# --- Shared instrument taxonomy (single canonical vocabulary for the whole pipeline) ---------
# Script 03 classifies observed parts into canonical snake_case instrument tokens (e.g.
# ``alto_sax``) using ``config/regex_rules.yaml``. Downstream stages that obtain instrument names
# from other sources (e.g. Script 04's LLM-derived expected parts) must map those names onto the
# SAME canonical tokens, otherwise reconciliation mismatches a part against itself (e.g. an
# LLM-supplied ``alto_saxophone`` never matching an observed ``alto_sax``). These helpers load and
# apply that shared vocabulary so every stage tracks an instrument the same way end to end.

_INSTRUMENT_SEPARATORS_RE = re.compile(r"[-_]")
_INSTRUMENT_WHITESPACE_RE = re.compile(r"\s+")


def normalize_instrument_name(text: str) -> str:
    """Lowercase an instrument token and collapse separators/whitespace into single spaces.

    Mirrors Script 03's alias normalization so a name from any source keys the same lookup entry.
    """
    lowered = (text or "").lower()
    lowered = _INSTRUMENT_SEPARATORS_RE.sub(" ", lowered)
    lowered = _INSTRUMENT_WHITESPACE_RE.sub(" ", lowered)
    return lowered.strip()


def load_instrument_taxonomy(rules_path: Path) -> dict[str, dict[str, str]]:
    """Load the shared canonical instrument taxonomy from the YAML lexicon.

    Returns ``{"alias_to_canonical": {normalized_name: canonical}, "canonical_to_section":
    {canonical: section}}`` built from the ``instruments`` and ``sections`` blocks of ``rules_path``
    (the same file Script 03 uses). Every canonical key maps to itself, and each alias maps to its
    canonical. Degrades to empty maps when PyYAML or the file is unavailable so callers no-op
    gracefully rather than failing.
    """
    alias_to_canonical: dict[str, str] = {}
    canonical_to_section: dict[str, str] = {}
    empty = {"alias_to_canonical": alias_to_canonical, "canonical_to_section": canonical_to_section}
    try:
        import yaml  # type: ignore
    except ImportError:
        return empty
    if not rules_path.exists():
        return empty
    try:
        with rules_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return empty

    instruments = data.get("instruments")
    if isinstance(instruments, dict):
        for canonical, aliases in instruments.items():
            canon = str(canonical).strip().lower()
            if not canon:
                continue
            alias_to_canonical.setdefault(normalize_instrument_name(canon), canon)
            if isinstance(aliases, list):
                for alias in aliases:
                    norm = normalize_instrument_name(str(alias))
                    if norm:
                        alias_to_canonical.setdefault(norm, canon)
    sections = data.get("sections")
    if isinstance(sections, dict):
        for section, canonicals in sections.items():
            if isinstance(canonicals, list):
                for canonical in canonicals:
                    canonical_to_section[str(canonical).strip().lower()] = str(section)
    return {"alias_to_canonical": alias_to_canonical, "canonical_to_section": canonical_to_section}


def canonicalize_instrument(value: str, alias_to_canonical: dict[str, str]) -> str:
    """Map an instrument name onto its canonical taxonomy token.

    Normalizes ``value`` and looks it up in ``alias_to_canonical``. Unknown names fall back to a
    uniform snake_case form of the input so the format stays consistent even when the taxonomy has
    no entry. Returns "" only for empty input.
    """
    norm = normalize_instrument_name(value)
    if not norm:
        return ""
    return alias_to_canonical.get(norm) or norm.replace(" ", "_")


# --- Shared script scaffolding ---------------------------------------------------------------


def setup_logging(log_level: str) -> None:
    """Configure root logging with the shared pipeline format (change 1)."""
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format=LOG_FORMAT,
    )


def make_checkpoint_path(output: Path, filename: str) -> Path:
    """Return the checkpoint path for an output file (change 1).

    Checkpoints live beside their output; ``filename`` is the per-script dotfile name
    (e.g. ``".expected_parts_checkpoint.json"``).
    """
    return output.parent / filename


def pct(part: int, whole: int) -> float:
    """Percentage of ``part`` out of ``whole`` (0.0 when ``whole`` is falsy) (change 1)."""
    return (100.0 * part / whole) if whole else 0.0


def md_cell(value: Any) -> str:
    """Escape a value for safe inclusion in a Markdown table cell (change 1)."""
    return str(value if value is not None else "").replace("|", "\\|")


def new_record_envelope(run_id: str, record_version: str) -> dict[str, Any]:
    """Return the common header shared by every pipeline output record (change 3).

    Callers add their own domain fields on top of this envelope.
    """
    return {
        "record_version": record_version,
        "run_id": run_id,
        "processing_status": ProcessingStatus.SUCCESS,
        "processing_timestamp": utc_now_iso(),
    }


def load_checkpoint(
    path: Path,
    record_version: str,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Load a checkpoint, discarding it when its ``record_version`` no longer matches (change 2).

    Returns an empty dict when the checkpoint is missing or stale, so incremental reuse simply
    reprocesses everything after a schema bump.
    """
    checkpoint = read_json(path) or {}
    if checkpoint and checkpoint.get("record_version") != record_version:
        (logger or logging.getLogger(__name__)).warning(
            "Checkpoint version mismatch (found=%s expected=%s). Ignoring checkpoint.",
            checkpoint.get("record_version"),
            record_version,
        )
        return {}
    return checkpoint


def build_checkpoint(
    record_version: str,
    run_id: str,
    fingerprints: dict[str, str],
    **extra: Any,
) -> dict[str, Any]:
    """Assemble a checkpoint payload with the standard fields plus per-script ``extra`` (change 2)."""
    checkpoint: dict[str, Any] = {
        "record_version": record_version,
        "last_run_id": run_id,
        "last_run_timestamp": utc_now_iso(),
        "fingerprints": fingerprints,
    }
    checkpoint.update(extra)
    return checkpoint


_T = TypeVar("_T")
_R = TypeVar("_R")


def run_with_progress(
    items: list[_T],
    worker: Callable[[_T], _R],
    max_workers: int = 1,
    on_result: Callable[[_T, _R], None] | None = None,
) -> list[_R]:
    """Apply ``worker`` to each item, in parallel when ``max_workers > 1`` (change 4).

    Results preserve input order regardless of concurrency. Runs sequentially for a single worker
    or a single item (so there is no thread-pool overhead in the common case). Per-item progress
    logging belongs in ``worker`` itself.

    When ``on_result`` is provided it is called once per completed item as ``on_result(item,
    result)``, in the **caller's thread** (never from a worker thread), so callers can persist
    results incrementally without their own locking. Under concurrency ``on_result`` fires in
    completion order, but the returned list is still in input order.
    """
    workers = max(1, max_workers)
    if workers <= 1 or len(items) <= 1:
        results: list[_R] = []
        for item in items:
            result = worker(item)
            if on_result is not None:
                on_result(item, result)
            results.append(result)
        return results
    ordered: dict[int, _R] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_index = {pool.submit(worker, item): idx for idx, item in enumerate(items)}
        for future in as_completed(future_to_index):
            idx = future_to_index[future]
            result = future.result()
            ordered[idx] = result
            if on_result is not None:
                on_result(items[idx], result)
    return [ordered[i] for i in range(len(items))]


def piece_sort_key(record: dict[str, Any]) -> tuple[str, str]:
    """Canonical ordering for piece-level records: by catalog number then folder (change 6).

    Records without a catalog number sort last (``"~"`` sentinel). Every script that emits or
    reports piece records should use this so output ordering is identical across the pipeline.
    """
    catalog = record.get("catalog_number") or "~"
    return (catalog, record.get("piece_folder") or "")
