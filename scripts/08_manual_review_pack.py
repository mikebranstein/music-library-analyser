"""Script 08: manual review pack (prioritized work queue).

Turns every Script 06 per-piece report (`data/piece_reports/*.json`, schema 1.1) into a
*prioritized* manual-review queue so limited librarian time targets the highest-value fixes first.
Unlike Scripts 06/07 (pure reporting stages), this stage computes something new: a transparent,
auditable **priority ordering**. It still never re-derives completeness, quality, severity, or
reason codes -- it only weights and orders the facts the earlier stages produced, and copies Script
06's `reason_codes` / `recommended_actions` / `action_items` through verbatim. Deterministic and
fully offline (no AI, no network), so it is cheap and unit-testable. See
`docs/SCRIPT08_MANUAL_REVIEW_PACK_IMPLEMENTATION_PLAN.md`.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

import typer

from scripts._common import (
    Severity,
    atomic_write_json,
    atomic_write_text,
    md_cell,
    new_record_envelope,
    piece_sort_key,
    setup_logging,
)

RECORD_VERSION = "1.0"

# The Script 06 per-piece schema this pack is written against. Records at any other version are
# still queued, but their newer/older fields may be missing, so priority scored from them can be
# incomplete; main() warns when the input set is not uniformly this version.
EXPECTED_PIECE_SCHEMA = "1.1"

app = typer.Typer(add_completion=False)

logger = logging.getLogger("script08.manual_review_pack")

CHECKPOINT_FILENAME = ".piece_report_checkpoint.json"

# --- Priority model ---------------------------------------------------------------------------

# Per-unit weights applied to each magnitude count. Named here (not inlined) so the ranking is
# transparent, retunable, and echoed into the outputs. See plan doc section 4.
WEIGHTS: dict[str, int] = {
    "missing_score": 40,  # a missing conductor score blocks performance
    "missing_required_part": 10,  # per missing required part
    "low_confidence_part": 5,  # per low-confidence part label
    "unmatched_part": 5,  # per unmatched part
    "duplicate_part": 2,  # per duplicated part to reconcile
    "unexpected_part": 1,  # per unexpected observed part
}

# Base points contributed by Script 06's severity hint (most severe first).
SEVERITY_BASE: dict[str, int] = {
    Severity.HIGH: 20,
    Severity.REVIEW: 8,
    Severity.OK: 0,
}


class LoadResult(NamedTuple):
    """Outcome of scanning the piece-reports directory."""

    records: list[dict[str, Any]]
    skipped: int


# --- Input loading ----------------------------------------------------------------------------


def load_piece_records(piece_reports_dir: Path) -> LoadResult:
    """Read every per-piece JSON record from the Script 06 output directory.

    Only files whose payload is an object with a ``piece_id`` are kept (this skips the checkpoint
    dotfile and any stray files). Unreadable/malformed files and non-piece payloads are skipped and
    counted so the caller can surface a data-health warning. Mirrors Script 07's loader contract.
    """
    records: list[dict[str, Any]] = []
    skipped = 0
    if not piece_reports_dir.exists():
        return LoadResult(records, skipped)
    for path in sorted(piece_reports_dir.glob("*.json")):
        if path.name == CHECKPOINT_FILENAME:
            continue
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Skipping unreadable piece report %s: %s", path, exc)
            skipped += 1
            continue
        if isinstance(payload, dict) and payload.get("piece_id"):
            records.append(payload)
        else:
            logger.warning("Skipping %s: not a piece report (no piece_id).", path)
            skipped += 1
    return LoadResult(records, skipped)


# --- Priority scoring -------------------------------------------------------------------------


def _priority_breakdown(rec: dict[str, Any]) -> dict[str, int]:
    """Per-component point contributions for one piece (self-describing; zeros included).

    Scan quality and handwriting are informational only and deliberately excluded from priority.
    """
    review = rec.get("review_counts") or {}
    severity = rec.get("severity")

    missing_required = int(rec.get("missing_required_count") or 0)
    low_confidence = int(review.get("low_confidence_count") or 0)
    unmatched = int(review.get("unmatched_count") or 0)
    duplicate = int(review.get("duplicate_count") or 0)
    unexpected = int(rec.get("unexpected_part_count") or 0)

    return {
        "severity_base": SEVERITY_BASE.get(severity, 0),
        "missing_score": WEIGHTS["missing_score"] if rec.get("score_missing") else 0,
        "missing_required_parts": missing_required * WEIGHTS["missing_required_part"],
        "low_confidence_parts": low_confidence * WEIGHTS["low_confidence_part"],
        "unmatched_parts": unmatched * WEIGHTS["unmatched_part"],
        "duplicate_parts": duplicate * WEIGHTS["duplicate_part"],
        "unexpected_parts": unexpected * WEIGHTS["unexpected_part"],
    }


def _missing_required_parts(rec: dict[str, Any]) -> list[dict[str, Any]]:
    """Project the likely-missing parts from ``expected_parts`` (required and not present)."""
    parts: list[dict[str, Any]] = []
    for part in rec.get("expected_parts") or []:
        if part.get("required") and not part.get("present"):
            parts.append(
                {
                    "label": part.get("label"),
                    "canonical_instrument": part.get("canonical_instrument"),
                    "section": part.get("section"),
                }
            )
    return parts


def _sample_thumbnail(rec: dict[str, Any]) -> str | None:
    """First document's page-1 thumbnail path (a representative sample), if any."""
    for doc in rec.get("documents") or []:
        thumb = doc.get("thumbnail_path")
        if thumb:
            return thumb
    return None


def _queue_entry(rec: dict[str, Any], report_dirname: str) -> dict[str, Any]:
    """Build one prioritized queue entry for a piece (``rank`` filled in after sorting)."""
    breakdown = _priority_breakdown(rec)
    quality = rec.get("quality_summary") or {}
    review = rec.get("review_counts") or {}
    piece_id = rec.get("piece_id")
    return {
        "rank": 0,
        "piece_id": piece_id,
        "catalog_number": rec.get("catalog_number"),
        "piece_title_guess": rec.get("piece_title_guess") or rec.get("piece_folder"),
        "piece_folder": rec.get("piece_folder"),
        "priority_score": sum(breakdown.values()),
        "priority_breakdown": breakdown,
        "severity": rec.get("severity"),
        "completeness_tier": rec.get("completeness_tier"),
        "completeness_score": rec.get("completeness_score"),
        "score_missing": bool(rec.get("score_missing")),
        "worst_quality_band": rec.get("worst_quality_band"),
        "missing_required_count": int(rec.get("missing_required_count") or 0),
        "unexpected_part_count": int(rec.get("unexpected_part_count") or 0),
        "low_quality_doc_count": int(quality.get("low_quality_doc_count") or 0),
        "handwritten_doc_count": int(quality.get("handwritten_doc_count") or 0),
        "document_count": int(rec.get("document_count") or 0),
        "review_counts": {
            "low_confidence_count": int(review.get("low_confidence_count") or 0),
            "unmatched_count": int(review.get("unmatched_count") or 0),
            "duplicate_count": int(review.get("duplicate_count") or 0),
        },
        "reason_codes": list(rec.get("reason_codes") or []),
        "recommended_actions": list(rec.get("recommended_actions") or []),
        "action_items": list(rec.get("action_items") or []),
        "missing_required_parts": _missing_required_parts(rec),
        "sample_thumbnail": _sample_thumbnail(rec),
        "report_path": f"{report_dirname}/{piece_id}.md" if piece_id else None,
    }


def build_queue(records: list[dict[str, Any]], report_dirname: str) -> list[dict[str, Any]]:
    """Prioritized queue: score every piece, sort by priority desc then ``piece_sort_key``."""
    entries = [_queue_entry(rec, report_dirname) for rec in records]
    entries.sort(key=lambda e: (-int(e["priority_score"]), *piece_sort_key(e)))
    for index, entry in enumerate(entries, start=1):
        entry["rank"] = index
    return entries


def _record_version_distribution(records: list[dict[str, Any]]) -> dict[str, int]:
    """Count the Script 06 schema version of each input record (mixed versions => stale reports)."""
    dist: dict[str, int] = {}
    for rec in records:
        version = rec.get("record_version") or "unknown"
        dist[version] = dist.get(version, 0) + 1
    return dict(sorted(dist.items()))


def build_pack(
    records: list[dict[str, Any]],
    run_id: str,
    source_dir: str,
    report_dirname: str,
    skipped: int = 0,
    limit: int = 0,
) -> dict[str, Any]:
    """Assemble the machine-readable prioritized-queue record (schema RECORD_VERSION)."""
    queue = build_queue(records, report_dirname)
    if limit and limit > 0:
        queue = queue[:limit]

    record = new_record_envelope(run_id, RECORD_VERSION)
    record["piece_count"] = len(records)
    record["pieces_skipped"] = skipped
    record["source_dir"] = source_dir
    record["record_version_distribution"] = _record_version_distribution(records)
    record["weights"] = {**WEIGHTS, "severity_base": dict(SEVERITY_BASE)}
    record["queue"] = queue
    return record


# --- Markdown rendering -----------------------------------------------------------------------


def render_pack(rec: dict[str, Any], detail_limit: int = 20) -> str:
    """Render the human-readable prioritized review pack Markdown from the queue record."""
    queue = rec.get("queue") or []
    out: list[str] = []

    out.append("# Manual Review Pack")
    out.append("")
    out.append(
        f"_Generated {rec.get('processing_timestamp')} - run `{rec.get('run_id')}` - "
        f"{len(queue)} queued piece(s) from `{rec.get('source_dir')}`_"
    )
    out.append("")

    # Data-health callout: only shown when something needs the maintainer's attention.
    skipped = int(rec.get("pieces_skipped") or 0)
    versions = rec.get("record_version_distribution") or {}
    unexpected_versions = {v: c for v, c in versions.items() if v != EXPECTED_PIECE_SCHEMA}
    if skipped or unexpected_versions:
        notes: list[str] = []
        if skipped:
            notes.append(f"{skipped} file(s) skipped (unreadable or not a piece report)")
        if unexpected_versions:
            rendered = ", ".join(f"{v}: {c}" for v, c in unexpected_versions.items())
            notes.append(
                f"schema version(s) other than {EXPECTED_PIECE_SCHEMA} present ({rendered}); "
                "some priorities may be incomplete - re-run Script 06"
            )
        out.append(f"> **Data health:** {'; '.join(notes)}.")
        out.append("")

    # Priority weights (transparency: how the ranking was computed).
    weights = rec.get("weights") or {}
    out.append("## Priority weights")
    out.append("")
    out.append("| Component | Points |")
    out.append("| --- | ---: |")
    for key in (
        "missing_score",
        "missing_required_part",
        "low_quality_doc",
        "handwritten_doc",
        "low_confidence_part",
        "unmatched_part",
        "duplicate_part",
        "unexpected_part",
    ):
        out.append(f"| {key} | {int(weights.get(key) or 0)} |")
    severity_base = weights.get("severity_base") or {}
    base_rendered = ", ".join(f"{k}: {v}" for k, v in severity_base.items())
    out.append(f"| severity_base | {md_cell(base_rendered)} |")
    out.append("")

    # Ranked queue table.
    out.append("## Review queue")
    out.append("")
    if queue:
        out.append(
            "| # | Priority | Catalog | Piece | Severity | Missing req. | Reason codes | Report |"
        )
        out.append("| ---: | ---: | --- | --- | --- | ---: | --- | --- |")
        for entry in queue:
            catalog = entry.get("catalog_number") or ""
            name = md_cell(entry.get("piece_title_guess"))
            severity = entry.get("severity") or ""
            missing_req = int(entry.get("missing_required_count") or 0)
            codes = md_cell(", ".join(entry.get("reason_codes") or []))
            report_path = entry.get("report_path")
            link = f"[report]({report_path})" if report_path else ""
            out.append(
                f"| {entry.get('rank')} | {entry.get('priority_score')} | {catalog} | {name} "
                f"| {severity} | {missing_req} | {codes} | {link} |"
            )
    else:
        out.append("_No pieces to review._")
    out.append("")

    # Per-piece detail for the top pieces.
    detail = queue if detail_limit <= 0 else queue[:detail_limit]
    if detail:
        out.append("## Top pieces (detail)")
        out.append("")
        for entry in detail:
            _render_piece_detail(entry, out)

    return "\n".join(out) + "\n"


def _render_piece_detail(entry: dict[str, Any], out: list[str]) -> None:
    """Append one piece's detail block to the Markdown pack."""
    catalog = entry.get("catalog_number") or ""
    title = entry.get("piece_title_guess") or entry.get("piece_folder") or entry.get("piece_id")
    out.append(
        f"### {entry.get('rank')}. {md_cell(f'{catalog} {title}'.strip())} "
        f"(priority {entry.get('priority_score')})"
    )
    out.append("")

    breakdown = entry.get("priority_breakdown") or {}
    contributions = [f"{key} {value}" for key, value in breakdown.items() if value]
    if contributions:
        out.append(f"- Priority breakdown: {md_cell(', '.join(contributions))}")

    missing = entry.get("missing_required_parts") or []
    if missing:
        labels = ", ".join(
            md_cell(p.get("label") or p.get("canonical_instrument")) for p in missing
        )
        out.append(f"- Likely missing parts: {labels}")

    for action in entry.get("recommended_actions") or []:
        out.append(f"- Action: {md_cell(action)}")

    for item in entry.get("action_items") or []:
        targets = ", ".join(md_cell(t) for t in item.get("targets") or [])
        if targets:
            out.append(f"  - {md_cell(item.get('reason_code'))}: {targets}")

    thumb = entry.get("sample_thumbnail")
    if thumb:
        out.append(f"- Sample page: `{thumb}`")

    report_path = entry.get("report_path")
    if report_path:
        out.append(f"- Full report: [report]({report_path})")
    out.append("")


# --- CSV rendering ----------------------------------------------------------------------------

CSV_COLUMNS = (
    "rank",
    "priority_score",
    "catalog_number",
    "piece_title_guess",
    "piece_id",
    "severity",
    "completeness_tier",
    "completeness_score",
    "missing_required_count",
    "missing_required_parts",
    "low_quality_doc_count",
    "handwritten_doc_count",
    "unexpected_part_count",
    "duplicate_count",
    "reason_codes",
    "next_action",
)


def render_queue_csv(queue: list[dict[str, Any]]) -> str:
    """Render the flat prioritized queue CSV as a projection of the queue record."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for entry in queue:
        missing_labels = "; ".join(
            p.get("label") or p.get("canonical_instrument") or ""
            for p in entry.get("missing_required_parts") or []
        )
        actions = entry.get("recommended_actions") or []
        review = entry.get("review_counts") or {}
        writer.writerow(
            [
                int(entry.get("rank") or 0),
                int(entry.get("priority_score") or 0),
                entry.get("catalog_number") or "",
                entry.get("piece_title_guess") or "",
                entry.get("piece_id") or "",
                entry.get("severity") or "",
                entry.get("completeness_tier") or "",
                entry.get("completeness_score")
                if entry.get("completeness_score") is not None
                else "",
                int(entry.get("missing_required_count") or 0),
                missing_labels,
                int(entry.get("low_quality_doc_count") or 0),
                int(entry.get("handwritten_doc_count") or 0),
                int(entry.get("unexpected_part_count") or 0),
                int(review.get("duplicate_count") or 0),
                "; ".join(entry.get("reason_codes") or []),
                actions[0] if actions else "",
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
        Path("data/review_pack"), help="Directory for the review-pack outputs"
    ),
    write_csv: bool = typer.Option(
        True, "--csv/--no-csv", help="Write the flat manual_review_queue.csv export"
    ),
    limit: int = typer.Option(
        0, help="Keep only the top-N ranked pieces in all outputs (0 = full queue)"
    ),
    detail_limit: int = typer.Option(
        20, help="How many top pieces get a detail block in review_pack.md (0 = all)"
    ),
    mode: str = typer.Option(
        "full",
        help="Accepted for pipeline uniformity; the review pack is always fully recomputed",
    ),
    log_level: str = typer.Option("INFO", help="DEBUG, INFO, WARNING, ERROR"),
) -> None:
    """Build the prioritized manual-review pack (Markdown + JSON + CSV) from the per-piece reports."""
    mode = mode.lower().strip()
    if mode not in {"full", "incremental"}:
        raise typer.BadParameter("mode must be 'full' or 'incremental'")

    setup_logging(log_level)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    start_time = time.perf_counter()

    piece_reports_dir = piece_reports_dir.resolve()
    output_dir = output_dir.resolve()

    loaded = load_piece_records(piece_reports_dir)
    records = loaded.records
    if not records:
        raise typer.BadParameter(
            f"No piece reports found in {piece_reports_dir}. Run Script 06 first."
        )

    logger.info("Prioritizing %d piece report(s) from %s.", len(records), piece_reports_dir)

    # Relative path from the output dir to the per-piece reports, so the Markdown links resolve
    # from wherever the pack is written (piece_reports is typically a sibling of review_pack).
    report_dirname = Path(os.path.relpath(piece_reports_dir, output_dir)).as_posix()

    pack = build_pack(
        records,
        run_id,
        piece_reports_dir.as_posix(),
        report_dirname,
        skipped=loaded.skipped,
        limit=limit,
    )

    # Data-health warnings (do not fail the run; the pack is still written).
    if loaded.skipped:
        logger.warning(
            "Skipped %d unreadable/invalid file(s) in %s.", loaded.skipped, piece_reports_dir
        )
    unexpected_versions = {
        v: c
        for v, c in (pack.get("record_version_distribution") or {}).items()
        if v != EXPECTED_PIECE_SCHEMA
    }
    if unexpected_versions:
        logger.warning(
            "Piece reports include schema version(s) other than %s (%s); some priorities may be "
            "incomplete. Re-run Script 06 to refresh them.",
            EXPECTED_PIECE_SCHEMA,
            unexpected_versions,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "manual_review_queue.json", pack)
    atomic_write_text(output_dir / "review_pack.md", render_pack(pack, detail_limit=detail_limit))
    if write_csv:
        atomic_write_text(output_dir / "manual_review_queue.csv", render_queue_csv(pack["queue"]))

    queue = pack["queue"]
    top = queue[0] if queue else None
    logger.info(
        "Manual review pack completed: queued=%d top_priority=%s elapsed=%.1fs output=%s",
        len(queue),
        top["priority_score"] if top else 0,
        time.perf_counter() - start_time,
        output_dir,
    )


if __name__ == "__main__":
    app()
