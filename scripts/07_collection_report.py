"""Script 07: collection-level report generator.

Aggregates every per-piece report (`data/piece_reports/*.json`, Script 06 schema 1.1) into one
collection-wide view: library size, completeness, scan quality, lookup coverage, the most-often
missing instruments, and the pieces that need attention. Like Script 06 it is a *reporting* stage:
it computes nothing new about the music, it only counts / groups / orders facts the earlier stages
already produced. Deterministic and fully offline (no AI, no network), so it is cheap and
unit-testable. See `docs/SCRIPT07_COLLECTION_REPORT_IMPLEMENTATION_PLAN.md`.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from scripts._common import (
    COMPLETENESS_TIER_ORDER,
    LOOKUP_STATUS_ORDER,
    REASON_ACTIONS,
    REASON_CODE_ORDER,
    SEVERITY_ORDER,
    CompletenessTier,
    ProcessingStatus,
    Severity,
    atomic_write_json,
    atomic_write_text,
    md_cell,
    new_record_envelope,
    pct,
    piece_sort_key,
    setup_logging,
)

RECORD_VERSION = "1.0"

app = typer.Typer(add_completion=False)

logger = logging.getLogger("script07.collection_report")

CHECKPOINT_FILENAME = ".piece_report_checkpoint.json"


# --- Input loading ----------------------------------------------------------------------------


def load_piece_records(piece_reports_dir: Path) -> list[dict[str, Any]]:
    """Read every per-piece JSON record from the Script 06 output directory.

    Only files whose payload is an object with a ``piece_id`` are kept (this skips the checkpoint
    dotfile and any stray files). Malformed JSON is skipped with a warning.
    """
    records: list[dict[str, Any]] = []
    if not piece_reports_dir.exists():
        return records
    for path in sorted(piece_reports_dir.glob("*.json")):
        if path.name == CHECKPOINT_FILENAME:
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable piece report %s: %s", path, exc)
            continue
        if isinstance(payload, dict) and payload.get("piece_id"):
            records.append(payload)
    return records


# --- Aggregation ------------------------------------------------------------------------------


def _count_by(records: list[dict[str, Any]], field: str, order: tuple[str, ...]) -> dict[str, int]:
    """Count records by ``field`` value, seeded with ``order`` so every key is present and sorted."""
    counts = {key: 0 for key in order}
    for rec in records:
        value = rec.get(field)
        if value in counts:
            counts[value] += 1
        elif value:
            counts[value] = counts.get(value, 0) + 1
    return counts


def _sum_band_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    """Sum the per-piece document-level quality band counts across the whole collection."""
    totals = {"good": 0, "review": 0, "poor": 0, "unknown": 0}
    for rec in records:
        band_counts = (rec.get("quality_summary") or {}).get("band_counts") or {}
        for band, count in band_counts.items():
            totals[band if band in totals else "unknown"] += int(count or 0)
    return totals


def _reason_code_frequency(records: list[dict[str, Any]]) -> dict[str, int]:
    """Count how many *pieces* exhibit each reason code (not total occurrences)."""
    freq = {code: 0 for code in REASON_CODE_ORDER}
    for rec in records:
        for code in rec.get("reason_codes") or []:
            if code in freq:
                freq[code] += 1
            else:
                freq[code] = freq.get(code, 0) + 1
    return freq


def _review_totals(records: list[dict[str, Any]]) -> dict[str, int]:
    """Sum the observed-rollup review counts across the collection."""
    keys = ("needs_review_count", "low_confidence_count", "unmatched_count", "duplicate_count")
    totals = dict.fromkeys(keys, 0)
    for rec in records:
        counts = rec.get("review_counts") or {}
        for key in keys:
            totals[key] += int(counts.get(key) or 0)
    return totals


def _top_missing_instruments(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate missing *required* parts across the collection, by instrument + section.

    Counts the number of distinct pieces missing each required instrument, so the librarian sees
    "what am I most often missing across the whole library." Sorted by count desc, then name.
    """
    tally: dict[tuple[str, str], int] = {}
    for rec in records:
        seen: set[tuple[str, str]] = set()
        for part in rec.get("expected_parts") or []:
            if part.get("required") and not part.get("present"):
                key = (
                    part.get("canonical_instrument") or "?",
                    part.get("section") or "",
                )
                seen.add(key)
        for key in seen:
            tally[key] = tally.get(key, 0) + 1
    rows = [
        {"canonical_instrument": inst, "section": section, "missing_piece_count": count}
        for (inst, section), count in tally.items()
    ]
    rows.sort(key=lambda r: (-r["missing_piece_count"], r["canonical_instrument"], r["section"]))
    return rows


def _attention_pieces(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """High-severity pieces for the dashboard call-out list, ordered by catalog."""
    high = [rec for rec in records if rec.get("severity") == Severity.HIGH]
    high.sort(key=piece_sort_key)
    return [
        {
            "piece_id": rec.get("piece_id"),
            "catalog_number": rec.get("catalog_number"),
            "piece_title_guess": rec.get("piece_title_guess") or rec.get("piece_folder"),
            "reason_codes": rec.get("reason_codes") or [],
        }
        for rec in high
    ]


def build_summary(records: list[dict[str, Any]], run_id: str, source_dir: str) -> dict[str, Any]:
    """Assemble the machine-readable collection aggregate record (schema RECORD_VERSION)."""
    total = len(records)

    def _count(pred) -> int:
        return sum(1 for rec in records if pred(rec))

    totals = {
        "pieces": total,
        "pieces_complete": _count(
            lambda r: r.get("completeness_tier") == CompletenessTier.COMPLETE
        ),
        "pieces_with_missing_required": _count(
            lambda r: int(r.get("missing_required_count") or 0) > 0
        ),
        "pieces_missing_score": _count(lambda r: bool(r.get("score_missing"))),
        "pieces_needs_review": _count(lambda r: bool(r.get("needs_review"))),
        "pieces_high_severity": _count(lambda r: r.get("severity") == Severity.HIGH),
        "pieces_with_errors": _count(
            lambda r: r.get("processing_status") == ProcessingStatus.ERROR
        ),
        "documents": sum(int(r.get("document_count") or 0) for r in records),
        "documents_low_quality": sum(
            int((r.get("quality_summary") or {}).get("low_quality_doc_count") or 0)
            for r in records
        ),
        "documents_handwritten": sum(
            int((r.get("quality_summary") or {}).get("handwritten_doc_count") or 0)
            for r in records
        ),
    }

    record = new_record_envelope(run_id, RECORD_VERSION)
    record["piece_count"] = total
    record["source_dir"] = source_dir
    record["totals"] = totals
    record["completeness_distribution"] = _count_by(
        records, "completeness_tier", COMPLETENESS_TIER_ORDER
    )
    record["lookup_status_distribution"] = _count_by(records, "lookup_status", LOOKUP_STATUS_ORDER)
    record["severity_distribution"] = _count_by(records, "severity", SEVERITY_ORDER)
    record["quality_band_distribution"] = _sum_band_counts(records)
    record["reason_code_frequency"] = _reason_code_frequency(records)
    record["review_totals"] = _review_totals(records)
    record["top_missing_instruments"] = _top_missing_instruments(records)
    record["attention_pieces"] = _attention_pieces(records)
    return record


# --- Markdown rendering -----------------------------------------------------------------------


def _distribution_table(title: str, counts: dict[str, int], total: int, out: list[str]) -> None:
    out.append(f"### {title}")
    out.append("")
    out.append("| Value | Pieces | Share |")
    out.append("| --- | ---: | ---: |")
    for key, count in counts.items():
        out.append(f"| {md_cell(key)} | {count} | {pct(count, total):.0f}% |")
    out.append("")


def render_summary(rec: dict[str, Any], piece_reports_dirname: str) -> str:
    """Render the collection dashboard Markdown from the aggregate record."""
    totals = rec.get("totals") or {}
    total = int(totals.get("pieces") or 0)
    out: list[str] = []

    out.append("# Collection Report")
    out.append("")
    out.append(
        f"_Generated {rec.get('processing_timestamp')} - run `{rec.get('run_id')}` - "
        f"{total} piece(s) from `{rec.get('source_dir')}`_"
    )
    out.append("")

    # Headline totals.
    out.append("## Headline")
    out.append("")
    out.append("| Metric | Count | Share |")
    out.append("| --- | ---: | ---: |")
    headline = [
        ("Complete sets", "pieces_complete"),
        ("Missing required part(s)", "pieces_with_missing_required"),
        ("Missing score", "pieces_missing_score"),
        ("Need review", "pieces_needs_review"),
        ("High severity", "pieces_high_severity"),
        ("Processing errors", "pieces_with_errors"),
    ]
    for label, key in headline:
        count = int(totals.get(key) or 0)
        out.append(f"| {label} | {count} | {pct(count, total):.0f}% |")
    out.append(f"| Documents (total) | {int(totals.get('documents') or 0)} | |")
    out.append(f"| Documents low-quality | {int(totals.get('documents_low_quality') or 0)} | |")
    out.append(
        f"| Documents handwritten/uncertain | {int(totals.get('documents_handwritten') or 0)} | |"
    )
    out.append("")

    out.append("## Distributions")
    out.append("")
    _distribution_table("Completeness", rec.get("completeness_distribution") or {}, total, out)
    _distribution_table(
        "Instrumentation lookup", rec.get("lookup_status_distribution") or {}, total, out
    )
    _distribution_table("Severity", rec.get("severity_distribution") or {}, total, out)

    # Scan quality (document-level bands).
    out.append("### Scan quality (documents)")
    out.append("")
    bands = rec.get("quality_band_distribution") or {}
    band_total = sum(bands.values()) or 0
    out.append("| Band | Documents | Share |")
    out.append("| --- | ---: | ---: |")
    for band in ("good", "review", "poor", "unknown"):
        count = int(bands.get(band) or 0)
        out.append(f"| {band} | {count} | {pct(count, band_total):.0f}% |")
    out.append("")

    # Reason-code frequency.
    out.append("## Reason-code frequency")
    out.append("")
    out.append("| Reason code | Pieces | Recommended action |")
    out.append("| --- | ---: | --- |")
    freq = rec.get("reason_code_frequency") or {}
    for code in REASON_CODE_ORDER:
        count = int(freq.get(code) or 0)
        out.append(f"| {code} | {count} | {md_cell(REASON_ACTIONS.get(code, ''))} |")
    out.append("")

    # Top missing instruments.
    out.append("## Top missing required instruments")
    out.append("")
    missing = rec.get("top_missing_instruments") or []
    if missing:
        out.append("| Instrument | Section | Pieces missing |")
        out.append("| --- | --- | ---: |")
        for row in missing:
            out.append(
                f"| {md_cell(row.get('canonical_instrument'))} "
                f"| {md_cell(row.get('section'))} | {row.get('missing_piece_count')} |"
            )
    else:
        out.append("_No missing required parts across the collection._")
    out.append("")

    # Pieces needing attention.
    out.append("## Pieces needing attention")
    out.append("")
    attention = rec.get("attention_pieces") or []
    if attention:
        out.append("| Catalog | Piece | Reason codes | Report |")
        out.append("| --- | --- | --- | --- |")
        for piece in attention:
            catalog = piece.get("catalog_number") or ""
            name = md_cell(piece.get("piece_title_guess"))
            codes = md_cell(", ".join(piece.get("reason_codes") or []))
            link = f"[report]({piece_reports_dirname}/{piece.get('piece_id')}.md)"
            out.append(f"| {catalog} | {name} | {codes} | {link} |")
    else:
        out.append("_No high-severity pieces._")
    out.append("")

    return "\n".join(out) + "\n"


# --- CSV rendering ----------------------------------------------------------------------------

CSV_COLUMNS = (
    "catalog_number",
    "piece_title_guess",
    "piece_id",
    "severity",
    "completeness_tier",
    "needs_review",
    "missing_required_count",
    "unexpected_part_count",
    "score_missing",
    "worst_quality_band",
    "low_quality_doc_count",
    "handwritten_doc_count",
    "document_count",
    "reason_codes",
)


def render_pieces_csv(records: list[dict[str, Any]]) -> str:
    """Render the flat, non-prioritized one-row-per-piece CSV export (ordered by catalog)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for rec in sorted(records, key=piece_sort_key):
        quality = rec.get("quality_summary") or {}
        writer.writerow(
            [
                rec.get("catalog_number") or "",
                rec.get("piece_title_guess") or rec.get("piece_folder") or "",
                rec.get("piece_id") or "",
                rec.get("severity") or "",
                rec.get("completeness_tier") or "",
                bool(rec.get("needs_review")),
                int(rec.get("missing_required_count") or 0),
                int(rec.get("unexpected_part_count") or 0),
                bool(rec.get("score_missing")),
                rec.get("worst_quality_band") or "",
                int(quality.get("low_quality_doc_count") or 0),
                int(quality.get("handwritten_doc_count") or 0),
                int(rec.get("document_count") or 0),
                "; ".join(rec.get("reason_codes") or []),
            ]
        )
    return buffer.getvalue()


# --- CLI --------------------------------------------------------------------------------------


@app.command()
def main(
    piece_reports_dir: Path = typer.Option(
        Path("data/piece_reports"), help="Directory of Script 06 per-piece JSON records"
    ),
    output_dir: Path = typer.Option(
        Path("data/collection_reports"), help="Directory for the collection outputs"
    ),
    write_csv: bool = typer.Option(
        True, "--csv/--no-csv", help="Write the flat pieces.csv collection export"
    ),
    mode: str = typer.Option(
        "full",
        help="Accepted for pipeline uniformity; the collection aggregate is always fully recomputed",
    ),
    log_level: str = typer.Option("INFO", help="DEBUG, INFO, WARNING, ERROR"),
) -> None:
    """Aggregate the per-piece reports into a collection summary (Markdown + JSON + CSV)."""
    mode = mode.lower().strip()
    if mode not in {"full", "incremental"}:
        raise typer.BadParameter("mode must be 'full' or 'incremental'")

    setup_logging(log_level)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    start_time = time.perf_counter()

    piece_reports_dir = piece_reports_dir.resolve()
    output_dir = output_dir.resolve()

    records = load_piece_records(piece_reports_dir)
    if not records:
        raise typer.BadParameter(
            f"No piece reports found in {piece_reports_dir}. Run Script 06 first."
        )

    logger.info("Aggregating %d piece report(s) from %s.", len(records), piece_reports_dir)

    summary = build_summary(records, run_id, piece_reports_dir.as_posix())

    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "summary.json", summary)
    atomic_write_text(output_dir / "summary.md", render_summary(summary, piece_reports_dir.name))
    if write_csv:
        atomic_write_text(output_dir / "pieces.csv", render_pieces_csv(records))

    totals = summary["totals"]
    logger.info(
        "Collection report completed: pieces=%d complete=%d missing_required=%d high=%d "
        "elapsed=%.1fs output=%s",
        totals["pieces"],
        totals["pieces_complete"],
        totals["pieces_with_missing_required"],
        totals["pieces_high_severity"],
        time.perf_counter() - start_time,
        output_dir,
    )


if __name__ == "__main__":
    app()
