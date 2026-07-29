from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
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


class ReasonCode:
    """Recommended-action reason codes recorded on each Script 06 piece record.

    Shared here (not in Script 06) so Scripts 06/07/08 group and filter by one definition.
    """

    MISSING_SCORE = "missing_score"
    MISSING_REQUIRED_PARTS = "missing_required_parts"
    UNEXPECTED_PARTS = "unexpected_parts"
    LOW_CONFIDENCE_PARTS = "low_confidence_parts"
    DUPLICATE_PARTS = "duplicate_parts"
    INSTRUMENTATION_UNRESOLVED = "instrumentation_unresolved"


# Canonical order (also drives Script 06 severity: the first codes are the most actionable).
REASON_CODE_ORDER: tuple[str, ...] = (
    ReasonCode.MISSING_SCORE,
    ReasonCode.MISSING_REQUIRED_PARTS,
    ReasonCode.UNEXPECTED_PARTS,
    ReasonCode.LOW_CONFIDENCE_PARTS,
    ReasonCode.DUPLICATE_PARTS,
    ReasonCode.INSTRUMENTATION_UNRESOLVED,
)

# Human-readable recommended action for each reason code.
REASON_ACTIONS: dict[str, str] = {
    ReasonCode.MISSING_SCORE: "Locate and add a full/conductor score for this piece.",
    ReasonCode.MISSING_REQUIRED_PARTS: "Source the missing required part(s) listed above.",
    ReasonCode.UNEXPECTED_PARTS: "Confirm the extra observed part(s) belong to this edition.",
    ReasonCode.LOW_CONFIDENCE_PARTS: (
        "Manually confirm the low-confidence / unmatched part label(s)."
    ),
    ReasonCode.DUPLICATE_PARTS: "Reconcile duplicated part(s) (keep the best copy).",
    ReasonCode.INSTRUMENTATION_UNRESOLVED: (
        "Instrumentation could not be resolved from an authority; confirm expected parts manually."
    ),
}


class Severity:
    """Piece-level severity hint (Script 08 owns the authoritative work-queue priority)."""

    OK = "ok"
    REVIEW = "review"
    HIGH = "high"


# Display/iteration order for severity tables: most severe first.
SEVERITY_ORDER: tuple[str, ...] = (Severity.HIGH, Severity.REVIEW, Severity.OK)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_rel_path(path: Path) -> str:
    return path.as_posix()


# Music-notation fonts (used by engraving software) map noteheads, clefs, accidentals, etc. into
# the Unicode Private Use Area (U+E000-U+F8FF). When a born-digital PDF embeds such a font, the
# text PyMuPDF extracts is polluted with these glyph code points interleaved with the real printed
# text (instrument names, titles, credits). They carry no readable meaning outside their font, so
# stripping them exposes the human-readable text the classifier needs.
_PUA_GLYPH_RE = re.compile("[\ue000-\uf8ff]+")


def strip_music_glyphs(text: str | None) -> str:
    """Replace Private Use Area glyph runs (U+E000-U+F8FF) with a single space.

    Returns readable text with music-font glyph pollution removed. Each run collapses to one space
    so adjacent real tokens stay separated (callers typically collapse whitespace afterwards).
    Plain text with no PUA glyphs is returned unchanged.
    """
    if not text:
        return ""
    return _PUA_GLYPH_RE.sub(" ", text)


def has_readable_text(text: str | None) -> bool:
    """True when the text has alphanumeric content after music-glyph pollution is stripped.

    A born-digital page whose embedded text is almost entirely notation-font glyphs has no useful
    readable content even though it is technically non-empty, so it should be treated as needing
    OCR rather than trusted as searchable text.
    """
    cleaned = strip_music_glyphs(text)
    return bool(cleaned.strip()) and any(c.isalnum() for c in cleaned)


def sha256_text(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def file_fingerprint(rel_path: str, size: int, mtime: float) -> str:
    basis = f"{rel_path}|{size}|{mtime}"
    return sha256_text(basis)


# --- Targeted single-piece reprocessing (`--only-piece`) -------------------------------------
# The pipeline groups PDFs into pieces by their top-level library folder (Script 01), whose name
# conventionally starts with the catalogue number (e.g. "368 Czardas"). The `--only-piece <int>`
# flag lets an operator force-recompute exactly one piece by that catalogue number while every
# other piece's prior records are preserved verbatim. These helpers give every script one shared,
# tested definition of "which piece does this record belong to" and "what should I do with it".

_CATALOG_PREFIX_RE = re.compile(r"\s*0*(\d{1,5})")


def catalog_int(value: str | int | None) -> int | None:
    """Return the leading catalogue number from a piece folder name or catalogue string.

    ``"368 Czardas" -> 368``, ``"368" -> 368``, ``"007 Bond" -> 7``, ``"Untitled" -> None``.
    Accepts an ``int`` (returned as-is) so callers can pass either a ``piece_folder`` or a parsed
    ``catalog_number`` field interchangeably.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    match = _CATALOG_PREFIX_RE.match(str(value))
    return int(match.group(1)) if match else None


def only_piece_decision(
    piece_key: str | int | None,
    only_piece: int | None,
    has_prior: bool,
) -> str:
    """Decide how a record should be handled under ``--only-piece`` scoping.

    Returns one of:

    - ``"normal"``  -- no scoping is active (or an untouched piece has no prior record); the
      script's usual full/incremental logic applies.
    - ``"process"`` -- this record belongs to the targeted catalogue number; force a recompute
      (bypass fingerprint reuse) so the operator always gets fresh results for the piece.
    - ``"reuse"``   -- this record belongs to a different piece that already has a prior record;
      preserve that prior record verbatim so scoping never drops or re-spends work on other pieces.
    """
    if only_piece is None:
        return "normal"
    if catalog_int(piece_key) == only_piece:
        return "process"
    return "reuse" if has_prior else "normal"


def _atomic_replace(tmp_path: Path, path: Path) -> None:
    """Replace ``path`` with ``tmp_path``, retrying transient Windows lock errors.

    On Windows ``os.replace`` can raise ``PermissionError`` (WinError 5) or an
    ``OSError`` (WinError 32) when the destination is momentarily held open by
    another process (antivirus, the Windows Search indexer, or a file-sync client
    such as OneDrive). These locks are typically released within milliseconds, so
    we retry a few times with a short backoff before giving up.
    """
    delays = (0.1, 0.25, 0.5, 1.0, 2.0)
    for attempt, delay in enumerate(delays):
        try:
            os.replace(tmp_path, path)
            return
        except PermissionError:
            logging.warning(
                "Atomic replace of %s blocked (attempt %d/%d); retrying in %.2fs. "
                "Close any program holding the file open (editor, antivirus, file sync).",
                path,
                attempt + 1,
                len(delays) + 1,
                delay,
            )
            time.sleep(delay)
    # Final attempt: let the exception propagate if it still fails.
    os.replace(tmp_path, path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
        _atomic_replace(tmp_path, path)
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
        _atomic_replace(tmp_path, path)
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
        _atomic_replace(tmp_path, path)
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


def load_instrument_taxonomy(rules_path: Path) -> dict[str, Any]:
    """Load the shared canonical instrument taxonomy from the YAML lexicon.

    Returns ``{"alias_to_canonical": {normalized_name: canonical}, "canonical_to_section":
    {canonical: section}, "interchangeable_groups": [[canonical, ...], ...]}`` built from the
    ``instruments``, ``sections`` and ``interchangeable_instruments`` blocks of ``rules_path`` (the
    same file Script 03 uses). Every canonical key maps to itself, and each alias maps to its
    canonical. ``interchangeable_groups`` lists sets of instruments that may substitute for one
    another during expected-parts reconciliation. Degrades to empty maps when PyYAML or the file is
    unavailable so callers no-op gracefully rather than failing.
    """
    alias_to_canonical: dict[str, str] = {}
    canonical_to_section: dict[str, str] = {}
    interchangeable_groups: list[list[str]] = []
    empty: dict[str, Any] = {
        "alias_to_canonical": alias_to_canonical,
        "canonical_to_section": canonical_to_section,
        "interchangeable_groups": interchangeable_groups,
    }
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
    groups = data.get("interchangeable_instruments")
    if isinstance(groups, list):
        for group in groups:
            if not isinstance(group, list):
                continue
            members = [str(m).strip().lower() for m in group if str(m).strip()]
            if len(members) >= 2:
                interchangeable_groups.append(members)
    return {
        "alias_to_canonical": alias_to_canonical,
        "canonical_to_section": canonical_to_section,
        "interchangeable_groups": interchangeable_groups,
    }


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
