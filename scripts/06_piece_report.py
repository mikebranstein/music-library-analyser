"""Script 06: per-piece report generator.

Joins every upstream per-piece and per-document signal on ``piece_id`` into one human-readable
Markdown report and one machine-readable JSON record per piece, plus a top-level index. It computes
nothing new about the music -- it surfaces the deterministic facts Scripts 01-05 already produced
(observed parts, expected/missing parts, completeness, scan quality, notation source, confidence
tiers, review flags) and derives a prioritized list of recommended manual actions.

The stage is deterministic and fully offline (no AI, network, or rendering), so it is cheap and
unit-testable. It reuses the shared scaffolding in ``scripts._common`` and mirrors Script 04's
per-piece split + index + orphan-cleanup + incremental-streaming conventions
(see ``docs/SCRIPT06_PIECE_REPORT_IMPLEMENTATION_PLAN.md``).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

from scripts._common import (
    CompletenessTier,
    LookupStatus,
    ProcessingStatus,
    atomic_write_json,
    atomic_write_text,
    build_checkpoint,
    load_checkpoint,
    make_checkpoint_path,
    md_cell,
    new_record_envelope,
    piece_sort_key,
    read_jsonl,
    run_with_progress,
    setup_logging,
    sha256_text,
    utc_now_iso,
)

RECORD_VERSION = "1.0"

CHECKPOINT_FILENAME = ".piece_report_checkpoint.json"

app = typer.Typer(add_completion=False)

logger = logging.getLogger("script06.piece_report")


# --- Output vocabulary ------------------------------------------------------------------------


class ReasonCode:
    """Recommended-action reason codes recorded on each piece record."""

    MISSING_SCORE = "missing_score"
    MISSING_REQUIRED_PARTS = "missing_required_parts"
    UNEXPECTED_PARTS = "unexpected_parts"
    LOW_QUALITY_SCANS = "low_quality_scans"
    HANDWRITTEN_OR_ILLEGIBLE = "handwritten_or_illegible"
    LOW_CONFIDENCE_PARTS = "low_confidence_parts"
    DUPLICATE_PARTS = "duplicate_parts"
    INSTRUMENTATION_UNRESOLVED = "instrumentation_unresolved"


# Canonical order (also drives severity: the first codes are the most actionable).
REASON_CODE_ORDER: tuple[str, ...] = (
    ReasonCode.MISSING_SCORE,
    ReasonCode.MISSING_REQUIRED_PARTS,
    ReasonCode.LOW_QUALITY_SCANS,
    ReasonCode.HANDWRITTEN_OR_ILLEGIBLE,
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
    ReasonCode.LOW_QUALITY_SCANS: "Re-scan the low-quality document(s) at higher fidelity.",
    ReasonCode.HANDWRITTEN_OR_ILLEGIBLE: (
        "Verify the handwritten/low-legibility document(s) are usable; re-engrave if needed."
    ),
    ReasonCode.LOW_CONFIDENCE_PARTS: "Manually confirm the low-confidence / unmatched part label(s).",
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


# Quality bands considered actionable, matching Script 05's vocabulary.
_QUALITY_POOR = "poor"
_QUALITY_REVIEW = "review"

# Notation sources that warrant a legibility check, matching Script 05's vocabulary.
_NOTATION_HANDWRITTEN = "handwritten"
_NOTATION_MIXED = "mixed_or_uncertain"


# --- Input loading / indexing ----------------------------------------------------------------


def _group_by_piece(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group per-document records by ``piece_id`` (records without one are dropped)."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        piece_id = rec.get("piece_id")
        if piece_id:
            grouped.setdefault(piece_id, []).append(rec)
    return grouped


def _index_by_piece(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index single-per-piece records by ``piece_id`` (last write wins on duplicates)."""
    return {rec["piece_id"]: rec for rec in records if rec.get("piece_id")}


class PieceInputs:
    """All upstream slices for a single piece, joined on ``piece_id``."""

    def __init__(
        self,
        piece_id: str,
        expected: dict[str, Any] | None,
        observed: dict[str, Any] | None,
        predictions: list[dict[str, Any]],
        quality: list[dict[str, Any]],
        documents: list[dict[str, Any]],
        thumbnails: dict[str, str],
    ) -> None:
        self.piece_id = piece_id
        self.expected = expected
        self.observed = observed
        self.predictions = predictions
        self.quality = quality
        self.documents = documents
        self.thumbnails = thumbnails


def _resolve_identity(inputs: PieceInputs) -> dict[str, Any]:
    """Resolve piece identity with a fixed source precedence.

    expected_parts -> observed rollup -> first part_prediction -> first document.
    """
    sources: list[dict[str, Any]] = []
    if inputs.expected:
        sources.append(inputs.expected)
    if inputs.observed:
        sources.append(inputs.observed)
    sources.extend(inputs.predictions)
    sources.extend(inputs.documents)

    def first(field: str) -> Any:
        for src in sources:
            value = src.get(field)
            if value:
                return value
        return None

    return {
        "piece_id": inputs.piece_id,
        "catalog_number": first("catalog_number"),
        "piece_title_guess": first("piece_title_guess"),
        "piece_folder": first("piece_folder"),
    }


# --- Derived findings -------------------------------------------------------------------------


def _document_rows(inputs: PieceInputs) -> list[dict[str, Any]]:
    """Build the per-document detected-parts rows (part_predictions joined to quality by pdf_path).

    Ordered by Script 03's ``part_sort_key`` so parts appear in conventional score order.
    """
    quality_by_path = {q["pdf_path"]: q for q in inputs.quality if q.get("pdf_path")}
    docs_by_path = {d["pdf_path"]: d for d in inputs.documents if d.get("pdf_path")}
    rows: list[dict[str, Any]] = []
    for pred in inputs.predictions:
        pdf_path = pred.get("pdf_path")
        qual = quality_by_path.get(pdf_path or "", {})
        doc = docs_by_path.get(pdf_path or "", {})
        rows.append(
            {
                "pdf_path": pdf_path,
                "pdf_filename": pred.get("pdf_filename")
                or (Path(pdf_path).name if pdf_path else None),
                "predicted_part": pred.get("predicted_part"),
                "confidence_tier": pred.get("confidence_tier"),
                "needs_review": bool(pred.get("needs_review")),
                "is_score": bool(pred.get("is_score")),
                "score_type": pred.get("score_type"),
                "duplicate_in_piece": bool(pred.get("duplicate_in_piece")),
                "page_count": doc.get("page_count"),
                "quality_band": qual.get("quality_band"),
                "quality_score": qual.get("quality_score"),
                "notation_source_type": qual.get("notation_source_type"),
                "quality_top_issues": qual.get("top_issues") or [],
                "quality_needs_review": bool(qual.get("needs_review")),
                "thumbnail_path": inputs.thumbnails.get(pdf_path or ""),
                "part_sort_key": pred.get("part_sort_key") or "",
            }
        )
    rows.sort(key=lambda r: (r.get("part_sort_key") or "", r.get("pdf_filename") or ""))
    return rows


def _derive_findings(inputs: PieceInputs, doc_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll upstream flags into reason codes, a piece-level needs_review, and a severity hint."""
    expected = inputs.expected or {}
    observed = inputs.observed or {}

    reason_codes: list[str] = []

    # Score presence: prefer the Script 04 verdict, fall back to the observed rollup.
    has_score = observed.get("has_score")
    if inputs.expected is not None:
        has_score = expected.get("has_score", has_score)
    score_expected = expected.get("score_expected")
    score_missing = bool(expected.get("score_missing")) or (
        inputs.expected is None and has_score is False
    )
    if score_missing:
        reason_codes.append(ReasonCode.MISSING_SCORE)

    missing_required = expected.get("missing_required_parts") or []
    if missing_required:
        reason_codes.append(ReasonCode.MISSING_REQUIRED_PARTS)

    # Quality: any poor/review-band document, and any handwritten / low-legibility notation.
    poor_docs = [r for r in doc_rows if r.get("quality_band") == _QUALITY_POOR]
    review_docs = [r for r in doc_rows if r.get("quality_band") == _QUALITY_REVIEW]
    if poor_docs or review_docs:
        reason_codes.append(ReasonCode.LOW_QUALITY_SCANS)
    handwritten_docs = [
        r
        for r in doc_rows
        if r.get("notation_source_type") in (_NOTATION_HANDWRITTEN, _NOTATION_MIXED)
    ]
    if handwritten_docs:
        reason_codes.append(ReasonCode.HANDWRITTEN_OR_ILLEGIBLE)

    if expected.get("unexpected_parts"):
        reason_codes.append(ReasonCode.UNEXPECTED_PARTS)

    low_confidence = (
        int(observed.get("low_confidence_count") or 0) + int(observed.get("unmatched_count") or 0)
    ) > 0 or any(r.get("needs_review") for r in doc_rows)
    if low_confidence:
        reason_codes.append(ReasonCode.LOW_CONFIDENCE_PARTS)

    if int(observed.get("duplicate_count") or 0) > 0:
        reason_codes.append(ReasonCode.DUPLICATE_PARTS)

    lookup_status = expected.get("lookup_status")
    if inputs.expected is not None and lookup_status != LookupStatus.MATCHED:
        reason_codes.append(ReasonCode.INSTRUMENTATION_UNRESOLVED)

    # Canonical ordering + de-dupe.
    ordered = [c for c in REASON_CODE_ORDER if c in reason_codes]

    # Severity: high when a required part or the score is missing, or any scan is poor.
    high = bool(missing_required) or score_missing or bool(poor_docs)
    severity = Severity.HIGH if high else (Severity.REVIEW if ordered else Severity.OK)

    needs_review = (
        bool(ordered)
        or bool(expected.get("needs_review"))
        or (int(observed.get("needs_review_count") or 0) > 0)
    )

    return {
        "reason_codes": ordered,
        "severity": severity,
        "needs_review": needs_review,
        "has_score": has_score,
        "score_expected": score_expected,
        "score_missing": score_missing,
    }


_BAND_SEVERITY = {"unknown": 0, "good": 1, "review": 2, "poor": 3}


def _worst_band(bands: list[str]) -> str | None:
    """Return the worst (highest-severity) quality band from a list, or None when empty."""
    if not bands:
        return None
    return max(bands, key=lambda b: _BAND_SEVERITY.get(b, 0))


def build_piece_record(inputs: PieceInputs, run_id: str) -> dict[str, Any]:
    """Assemble the machine-readable per-piece JSON record (schema RECORD_VERSION)."""
    identity = _resolve_identity(inputs)
    doc_rows = _document_rows(inputs)
    findings = _derive_findings(inputs, doc_rows)
    expected = inputs.expected or {}
    observed = inputs.observed or {}

    record = new_record_envelope(run_id, RECORD_VERSION)
    record.update(identity)
    record["document_count"] = len(doc_rows)
    record["documents"] = doc_rows

    # Expected / observed instrumentation (echoed as-is from upstream).
    record["ensemble_type"] = expected.get("ensemble_type")
    record["ensemble_display_name"] = expected.get("ensemble_display_name")
    record["lookup_status"] = expected.get("lookup_status")
    record["identity_match_confidence"] = expected.get("identity_match_confidence")
    record["work_identity"] = expected.get("work_identity")
    record["evidence_sources"] = expected.get("evidence_sources") or []
    record["expected_parts"] = expected.get("expected_parts") or []
    record["missing_required_parts"] = expected.get("missing_required_parts") or []
    record["missing_optional_parts"] = expected.get("missing_optional_parts") or []
    record["unexpected_parts"] = expected.get("unexpected_parts") or []
    record["completeness_score"] = expected.get("completeness_score")
    record["completeness_tier"] = expected.get("completeness_tier") or CompletenessTier.UNKNOWN
    record["has_expected_parts"] = inputs.expected is not None

    record["observed_parts"] = observed.get("observed_parts") or []
    record["distinct_instruments"] = observed.get("distinct_instruments")
    record["families"] = observed.get("families") or []
    record["sections"] = observed.get("sections") or []
    record["score_types"] = observed.get("score_types") or []
    record["review_counts"] = {
        "needs_review_count": observed.get("needs_review_count"),
        "low_confidence_count": observed.get("low_confidence_count"),
        "unmatched_count": observed.get("unmatched_count"),
        "duplicate_count": observed.get("duplicate_count"),
    }

    # Quality roll-up (worst band across the piece's documents).
    bands = [r.get("quality_band") for r in doc_rows if r.get("quality_band")]
    record["worst_quality_band"] = _worst_band(bands)

    record.update(findings)
    record["recommended_actions"] = [REASON_ACTIONS[c] for c in findings["reason_codes"]]
    return record


# --- Markdown rendering ----------------------------------------------------------------------


def _fmt_completeness(rec: dict[str, Any]) -> str:
    score = rec.get("completeness_score")
    tier = rec.get("completeness_tier")
    if score is None:
        return f"- ({tier})" if tier else "-"
    return f"{score * 100:.0f}% ({tier})"


def _format_edition(resolved: dict[str, Any]) -> str:
    """Compact one-line edition identity from Script 04's resolved work identity."""
    if not isinstance(resolved, dict):
        return ""
    parts = [
        resolved.get("title"),
        resolved.get("composer"),
        resolved.get("publisher"),
        resolved.get("edition"),
    ]
    return " - ".join(str(p) for p in parts if p)


def _unexpected_label(item: Any) -> str:
    """Human label for an unexpected-part entry from Script 04."""
    if isinstance(item, dict):
        return (
            item.get("predicted_part")
            or item.get("label")
            or item.get("canonical_instrument")
            or "?"
        )
    return str(item)


def render_piece_report(rec: dict[str, Any]) -> str:
    """Render one piece's Markdown report from its JSON record."""
    catalog = rec.get("catalog_number") or "?"
    title = rec.get("piece_title_guess") or rec.get("piece_folder") or rec.get("piece_id")
    out: list[str] = []
    out.append(f"# {catalog} - {title}")
    out.append("")
    severity = rec.get("severity")
    flag = "needs review" if rec.get("needs_review") else "no flags"
    out.append(f"_Piece `{rec.get('piece_id')}` - severity **{severity}** - {flag}_")
    out.append("")

    # 1. Identity.
    out.append("## Identity")
    out.append("")
    out.append(f"- **Catalog:** {md_cell(catalog)}")
    out.append(f"- **Title:** {md_cell(title)}")
    out.append(f"- **Folder:** {md_cell(rec.get('piece_folder'))}")
    ensemble = rec.get("ensemble_display_name")
    if ensemble:
        out.append(f"- **Ensemble:** {md_cell(ensemble)} (`{rec.get('ensemble_type')}`)")
    if rec.get("has_expected_parts"):
        conf = rec.get("identity_match_confidence")
        out.append(
            f"- **Instrumentation lookup:** {rec.get('lookup_status')}"
            + (f" (confidence {conf})" if conf is not None else "")
        )
        resolved = (rec.get("work_identity") or {}).get("resolved") or {}
        edition = _format_edition(resolved)
        if edition:
            out.append(f"- **Edition:** {md_cell(edition)}")
    else:
        out.append("- **Instrumentation lookup:** not available (Script 04 not run for this piece)")
    out.append("")

    # 2. Detected documents & predicted parts.
    out.append("## Detected Documents & Parts")
    out.append("")
    docs = rec.get("documents") or []
    if docs:
        out.append("| Document | Predicted part | Conf. | Score | Quality | Notation | Review |")
        out.append("| --- | --- | --- | --- | --- | --- | --- |")
        for d in docs:
            score_cell = (d.get("score_type") or "score") if d.get("is_score") else "-"
            qual = d.get("quality_band") or "-"
            qscore = d.get("quality_score")
            if isinstance(qscore, (int, float)):
                qual = f"{qual} ({qscore:.0f})"
            review = "yes" if (d.get("needs_review") or d.get("quality_needs_review")) else "-"
            name = d.get("pdf_filename") or d.get("pdf_path")
            thumb = d.get("thumbnail_path")
            name_cell = f"[{md_cell(name)}]({thumb})" if thumb else md_cell(name)
            out.append(
                f"| {name_cell} | {md_cell(d.get('predicted_part'))} "
                f"| {md_cell(d.get('confidence_tier'))} | {score_cell} "
                f"| {md_cell(qual)} | {md_cell(d.get('notation_source_type'))} | {review} |"
            )
    else:
        out.append("_No classified documents for this piece._")
    out.append("")

    # 3. Expected & missing parts.
    out.append("## Expected & Missing Parts")
    out.append("")
    if rec.get("has_expected_parts"):
        out.append(f"- **Completeness:** {_fmt_completeness(rec)}")
        expected_parts = rec.get("expected_parts") or []
        if expected_parts:
            out.append("")
            out.append("| # | Instrument | Label | Section | Required | Observed |")
            out.append("| --- | --- | --- | --- | --- | --- |")
            for part in expected_parts:
                idx = part.get("part_index")
                idx_cell = "-" if idx is None else str(idx)
                required = "required" if part.get("required") else "optional"
                observed = "yes" if part.get("present") else "MISSING"
                out.append(
                    f"| {idx_cell} | {md_cell(part.get('canonical_instrument'))} "
                    f"| {md_cell(part.get('label'))} | {md_cell(part.get('section'))} "
                    f"| {required} | {observed} |"
                )
        missing_req = rec.get("missing_required_parts") or []
        missing_opt = rec.get("missing_optional_parts") or []
        if missing_req:
            out.append("")
            out.append(f"**Missing required:** {md_cell(', '.join(missing_req))}")
        if missing_opt:
            out.append("")
            out.append(f"_Missing optional:_ {md_cell(', '.join(missing_opt))}")
        unexpected = rec.get("unexpected_parts") or []
        if unexpected:
            labels = ", ".join(md_cell(_unexpected_label(u)) for u in unexpected)
            out.append("")
            out.append(f"_Observed but not expected: {labels}_")
    else:
        obs = rec.get("distinct_instruments")
        out.append(
            "_No authoritative instrumentation available; "
            f"{obs if obs is not None else 0} distinct instrument(s) observed._"
        )
    out.append("")

    # 4. Score presence.
    out.append("## Score")
    out.append("")
    has_score = rec.get("has_score")
    if has_score:
        types = ", ".join(rec.get("score_types") or []) or "unspecified"
        out.append(f"- Score present ({types}).")
    elif rec.get("score_missing"):
        out.append("- **No score detected** for this piece.")
    else:
        out.append("- Score presence unknown.")
    out.append("")

    # 5. Quality findings.
    out.append("## Quality Findings")
    out.append("")
    worst = rec.get("worst_quality_band")
    out.append(f"- **Worst document band:** {worst or 'unknown'}")
    flagged = [
        d
        for d in docs
        if d.get("quality_band") in (_QUALITY_POOR, _QUALITY_REVIEW) or d.get("quality_top_issues")
    ]
    if flagged:
        out.append("")
        for d in flagged:
            issues = ", ".join(d.get("quality_top_issues") or []) or "-"
            out.append(
                f"- {md_cell(d.get('pdf_filename'))}: {d.get('quality_band')} "
                f"(issues: {md_cell(issues)})"
            )
    out.append("")

    # 6. Confidence summary.
    out.append("## Confidence Summary")
    out.append("")
    counts = rec.get("review_counts") or {}
    out.append(f"- Needs review (parts): {counts.get('needs_review_count') or 0}")
    out.append(f"- Low confidence: {counts.get('low_confidence_count') or 0}")
    out.append(f"- Unmatched: {counts.get('unmatched_count') or 0}")
    out.append(f"- Duplicates: {counts.get('duplicate_count') or 0}")
    out.append("")

    # 7. Recommended manual actions.
    out.append("## Recommended Manual Actions")
    out.append("")
    actions = rec.get("recommended_actions") or []
    if actions:
        for code, action in zip(rec.get("reason_codes") or [], actions):
            out.append(f"- **{code}**: {action}")
    else:
        out.append("_No manual actions flagged from the available signals._")
    out.append("")

    return "\n".join(out) + "\n"


def build_index(records: list[dict[str, Any]], meta: dict[str, Any], output_dirname: str) -> str:
    """Render the top-level index: run metadata + a table linking to each per-piece report."""
    total = len(records)
    needs_review = sum(1 for r in records if r.get("needs_review"))
    high = sum(1 for r in records if r.get("severity") == Severity.HIGH)

    out: list[str] = []
    out.append("# Piece Reports")
    out.append("")
    out.append(
        f"_Generated {meta['generated_at']} - run `{meta['run_id']}` - mode **{meta['mode']}**_"
    )
    out.append("")
    out.append(
        f"{total} piece(s); {needs_review} need review ({high} high severity). "
        f"Each piece links to its own report under `{output_dirname}/`."
    )
    out.append("")
    out.append("| Catalog | Piece | Severity | Completeness | Missing required | Score | Details |")
    out.append("| --- | --- | --- | --- | ---: | --- | --- |")
    for rec in sorted(records, key=piece_sort_key):
        catalog = rec.get("catalog_number") or ""
        name = md_cell(rec.get("piece_title_guess") or rec.get("piece_folder"))
        severity = rec.get("severity") or ""
        completeness = _fmt_completeness(rec)
        missing = len(rec.get("missing_required_parts") or [])
        if rec.get("has_score"):
            score = "yes"
        elif rec.get("score_missing"):
            score = "MISSING"
        else:
            score = "?"
        link = f"[report]({output_dirname}/{rec.get('piece_id')}.md)"
        out.append(
            f"| {catalog} | {name} | {severity} | {completeness} | {missing} | {score} | {link} |"
        )
    out.append("")
    return "\n".join(out) + "\n"


# --- Fingerprints ----------------------------------------------------------------------------


def piece_fingerprint(inputs: PieceInputs) -> str:
    """Fingerprint a piece from the exact upstream slice that feeds its report.

    Any change to the joined inputs (or a RECORD_VERSION bump) invalidates the cached report.
    """
    basis = {
        "version": RECORD_VERSION,
        "expected": inputs.expected,
        "observed": inputs.observed,
        "predictions": sorted(inputs.predictions, key=lambda r: r.get("pdf_path") or ""),
        "quality": sorted(inputs.quality, key=lambda r: r.get("pdf_path") or ""),
        "documents": sorted(
            [
                {"pdf_path": d.get("pdf_path"), "page_count": d.get("page_count")}
                for d in inputs.documents
            ],
            key=lambda r: r.get("pdf_path") or "",
        ),
        "thumbnails": inputs.thumbnails,
    }
    return sha256_text(json.dumps(basis, sort_keys=True, default=str))


# --- CLI -------------------------------------------------------------------------------------


@app.command()
def main(
    expected_parts: Path = typer.Option(
        Path("data/expected_parts.jsonl"), help="Script 04 per-piece expected-parts JSONL input"
    ),
    observed_parts: Path = typer.Option(
        Path("data/observed_parts_by_piece.jsonl"),
        help="Script 03 per-piece observed-parts rollup JSONL input",
    ),
    part_predictions: Path = typer.Option(
        Path("data/part_predictions.jsonl"), help="Script 03 per-document predictions JSONL input"
    ),
    quality_metrics: Path = typer.Option(
        Path("data/quality_metrics.jsonl"), help="Script 05 per-document quality JSONL input"
    ),
    documents: Path = typer.Option(
        Path("data/documents.jsonl"), help="Script 02 per-document rollups (optional)"
    ),
    pages: Path = typer.Option(
        Path("data/pages.jsonl"), help="Script 02 per-page features (optional; thumbnails)"
    ),
    output_dir: Path = typer.Option(
        Path("data/piece_reports"), help="Directory for per-piece .md/.json reports"
    ),
    output_index: Path = typer.Option(
        Path("data/piece_reports.md"), help="Top-level index Markdown output"
    ),
    write_index: bool = typer.Option(
        True, "--index/--no-index", help="Write the top-level index Markdown file"
    ),
    thumbnails: bool = typer.Option(
        True, "--thumbnails/--no-thumbnails", help="Include page-1 thumbnail links per document"
    ),
    mode: str = typer.Option("full", help="Processing mode: full or incremental"),
    concurrency: int = typer.Option(
        1, "--concurrency", "-j",
        help="Pieces to render in parallel (report rendering is CPU-light)",
    ),
    log_level: str = typer.Option("INFO", help="DEBUG, INFO, WARNING, ERROR"),
) -> None:
    """Generate one Markdown + JSON report per piece, plus a linking index."""
    mode = mode.lower().strip()
    if mode not in {"full", "incremental"}:
        raise typer.BadParameter("mode must be 'full' or 'incremental'")

    setup_logging(log_level)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start_time = time.perf_counter()

    output_dir = output_dir.resolve()

    expected_by_piece = _index_by_piece(read_jsonl(expected_parts.resolve()))
    observed_by_piece = _index_by_piece(read_jsonl(observed_parts.resolve()))
    predictions_by_piece = _group_by_piece(read_jsonl(part_predictions.resolve()))
    quality_by_piece = _group_by_piece(read_jsonl(quality_metrics.resolve()))
    documents_by_piece = _group_by_piece(read_jsonl(documents.resolve()))

    # Page-1 thumbnail per pdf_path (only when thumbnails are enabled).
    thumbs_by_pdf: dict[str, str] = {}
    if thumbnails:
        for rec in read_jsonl(pages.resolve()):
            pdf_path = rec.get("pdf_path")
            if not pdf_path or thumbs_by_pdf.get(pdf_path):
                continue
            if int(rec.get("page_num") or 0) == 1 and rec.get("thumbnail_path"):
                thumbs_by_pdf[pdf_path] = rec["thumbnail_path"]

    # Piece universe: union of every per-piece source.
    piece_ids = sorted(
        set(expected_by_piece)
        | set(observed_by_piece)
        | set(predictions_by_piece)
        | set(quality_by_piece)
    )
    if not piece_ids:
        raise typer.BadParameter("No pieces found across the provided inputs.")

    total = len(piece_ids)
    logger.info("Building reports for %d piece(s) in %s mode.", total, mode)

    ckpt_path = make_checkpoint_path(output_dir / "x", CHECKPOINT_FILENAME)
    checkpoint = load_checkpoint(ckpt_path, RECORD_VERSION, logger)
    prior_fingerprints = checkpoint.get("fingerprints", {}) if mode == "incremental" else {}
    previous_records = {
        rec["piece_id"]: rec
        for rec in (_read_json_dir(output_dir) if mode == "incremental" else [])
        if rec.get("piece_id")
    }

    output_dir.mkdir(parents=True, exist_ok=True)

    rebuilt: list[dict[str, Any]] = []
    fingerprints: dict[str, str] = {}
    reused = 0

    def _build_inputs(piece_id: str) -> PieceInputs:
        preds = predictions_by_piece.get(piece_id, [])
        thumbs = {
            p["pdf_path"]: thumbs_by_pdf[p["pdf_path"]]
            for p in preds
            if p.get("pdf_path") in thumbs_by_pdf
        }
        return PieceInputs(
            piece_id=piece_id,
            expected=expected_by_piece.get(piece_id),
            observed=observed_by_piece.get(piece_id),
            predictions=preds,
            quality=quality_by_piece.get(piece_id, []),
            documents=documents_by_piece.get(piece_id, []),
            thumbnails=thumbs,
        )

    def _persist(_piece_id: str, record: dict[str, Any]) -> None:
        # Runs in the caller's thread as each piece completes: write the .md + .json and rewrite the
        # checkpoint so an interrupted run keeps everything finished so far.
        rebuilt.append(record)
        pid = record["piece_id"]
        atomic_write_json(output_dir / f"{pid}.json", record)
        atomic_write_text(output_dir / f"{pid}.md", render_piece_report(record))
        atomic_write_json(
            ckpt_path,
            build_checkpoint(
                RECORD_VERSION,
                run_id,
                {r["piece_id"]: fingerprints[r["piece_id"]] for r in rebuilt},
                output_dir=output_dir.as_posix(),
                record_count=len(rebuilt),
            ),
        )

    to_process: list[str] = []
    seen = 0
    for piece_id in piece_ids:
        seen += 1
        inputs = _build_inputs(piece_id)
        fingerprint = piece_fingerprint(inputs)
        fingerprints[piece_id] = fingerprint

        prior = previous_records.get(piece_id)
        if (
            mode == "incremental"
            and prior is not None
            and prior.get("record_version") == RECORD_VERSION
            and prior_fingerprints.get(piece_id) == fingerprint
        ):
            _persist(piece_id, prior)
            reused += 1
            logger.info("[%d/%d] Reusing cached report: %s", seen, total, piece_id)
            continue
        to_process.append(piece_id)

    def _process(piece_id: str) -> dict[str, Any]:
        idx = piece_ids.index(piece_id) + 1
        inputs = _build_inputs(piece_id)
        try:
            record = build_piece_record(inputs, run_id)
        except Exception as exc:
            logger.exception("Unexpected report error on piece %s", piece_id)
            record = new_record_envelope(run_id, RECORD_VERSION)
            record["processing_status"] = ProcessingStatus.ERROR
            record["piece_id"] = piece_id
            record["error_detail"] = str(exc)
            record["severity"] = Severity.HIGH
            record["needs_review"] = True
            record["reason_codes"] = []
            record["recommended_actions"] = []
        logger.info(
            "[%d/%d] %s -> severity=%s reasons=%d",
            idx, total, piece_id, record.get("severity"), len(record.get("reason_codes") or []),
        )
        return record

    workers = max(1, concurrency)
    if to_process:
        run_with_progress(to_process, _process, workers, on_result=_persist)

    rebuilt.sort(key=piece_sort_key)

    if write_index:
        meta = {"generated_at": utc_now_iso(), "run_id": run_id, "mode": mode}
        index = build_index(rebuilt, meta, output_dir.name)
        atomic_write_text(output_index.resolve(), index)
        logger.info("Wrote index: %s", output_index.resolve())

    # Orphan cleanup: prune reports for pieces that no longer exist (only *.md/*.json we manage;
    # never the checkpoint dotfile, which glob("*.json") would otherwise match).
    valid = set(piece_ids)
    for existing in (*output_dir.glob("*.json"), *output_dir.glob("*.md")):
        if existing.name == CHECKPOINT_FILENAME:
            continue
        if existing.stem not in valid:
            _safe_unlink(existing)

    logger.info(
        "Piece reports completed: total=%d processed=%d reused=%d elapsed=%.1fs output=%s",
        len(rebuilt), len(to_process), reused, time.perf_counter() - start_time, output_dir,
    )


def _read_json_dir(output_dir: Path) -> list[dict[str, Any]]:
    """Read all per-piece .json records back (for incremental reuse). Missing dir -> empty."""
    if not output_dir.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in output_dir.glob("*.json"):
        try:
            with path.open("r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


if __name__ == "__main__":
    app()
