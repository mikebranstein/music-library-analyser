"""Script 09: static site data builder.

Transforms the pipeline's existing output (Scripts 01-08) into the web-optimized data layer the
hand-authored ``web/`` scaffold reads: a set of ``web/data/*.js`` modules that register plain
objects onto the ``window.MLG`` global (classic ``<script>`` tags, so the site runs from
``file://`` with no server, no ``fetch``, and no build step), plus downsized WebP thumbnails under
``web/assets/thumbs/``.

This stage is a pure *presentation* transform: it never re-derives completeness, quality,
severity, instruments, or reason codes -- it only reshapes and copies the facts Scripts 02-08
produced into the shapes the site renders. Deterministic and offline; the only non-deterministic /
heavyweight part (WebP thumbnail generation via Pillow) is isolated so the builders stay unit
testable. See ``docs/SCRIPT09_STATIC_SITE_IMPLEMENTATION_PLAN.md``.

Inputs (under ``--data-dir``):
    collection_reports/summary.json    Script 07 rollup (dashboard + pieces list)
    piece_reports/*.json               Script 06 per-piece detail (documents, expected parts)
    documents.jsonl                    Script 02 per-document OCR / vision detail
    pages.jsonl                        Script 02 per-page metrics
    review_pack/manual_review_queue.json   Script 08 prioritized queue (phase 8)

Outputs (under ``--web-dir``):
    data/00_manifest.js  10_dashboard.js  20_phases.js
    data/30_pieces.js    40_documents.js  50_pages.js
    assets/thumbs/*.webp                   (when thumbnails are enabled)
    assets/pdfs/...                        (only with --copy-pdfs)
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import typer

from scripts._common import (
    piece_sort_key,
    read_json,
    read_jsonl,
    setup_logging,
)

SITE_SCHEMA_VERSION = "1.0"

# Upstream schema versions this builder is written against (informational; mismatches are logged
# but never fatal -- the site simply renders whatever fields are present).
EXPECTED_PIECE_SCHEMA = "1.1"
EXPECTED_SUMMARY_SCHEMA = "1.2"

app = typer.Typer(add_completion=False)

logger = logging.getLogger("script09.static_site")

# The generated data modules, in load order. Each is a (filename, registry-key) pair.
DATA_MODULES: tuple[tuple[str, str], ...] = (
    ("00_manifest.js", "manifest"),
    ("10_dashboard.js", "dashboard"),
    ("20_phases.js", "phases"),
    ("30_pieces.js", "pieces"),
    ("40_documents.js", "documents"),
    ("50_pages.js", "pages"),
)

# A thumbnail resolver maps a pipeline cache path (e.g. ``cache/render/<id>_p0001_hash.png``) to
# the web-root-relative thumbnail path the site should reference, or ``None`` when no usable
# thumbnail exists. Kept as a parameter so the model builders never touch the filesystem.
ThumbResolver = Callable[[str | None], str | None]


# --- identity synthesis -----------------------------------------------------------------------


def synth_doc_id(pdf_path: str) -> str:
    """Stable 16-hex document id derived from the (library-relative) PDF path.

    The pipeline has no persistent per-document id, so we hash the normalized path. Forward-slash
    normalization keeps the id identical regardless of the OS separator in the source records.
    """
    normalized = (pdf_path or "").replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def synth_page_id(doc_id: str, page_num: int | str) -> str:
    """Deterministic page id: ``<doc_id>__p<page_num>``."""
    return f"{doc_id}__p{page_num}"


def thumb_web_path(cache_path: str | None) -> str | None:
    """Web-root-relative WebP path for a pipeline render cache path (no filesystem access)."""
    if not cache_path:
        return None
    stem = Path(str(cache_path).replace("\\", "/")).stem
    if not stem:
        return None
    return f"assets/thumbs/{stem}.webp"


# --- report indexing --------------------------------------------------------------------------


class ReportIndex:
    """Cross-reference maps derived once from the Script 06 per-piece reports."""

    def __init__(self) -> None:
        self.by_piece_id: dict[str, dict[str, Any]] = {}
        self.doc_meta_by_pdf_path: dict[str, dict[str, Any]] = {}
        self.section_by_predicted_part: dict[str, str] = {}


def index_reports(reports: list[dict[str, Any]]) -> ReportIndex:
    """Build lookup maps from the per-piece reports for enriching documents and pieces."""
    index = ReportIndex()
    for report in reports:
        piece_id = report.get("piece_id")
        if not piece_id:
            continue
        index.by_piece_id[piece_id] = report
        for doc in report.get("documents") or []:
            pdf_path = doc.get("pdf_path")
            if pdf_path:
                index.doc_meta_by_pdf_path[pdf_path] = doc
        # predicted_part -> section, learned from observed parts (best-effort display hint).
        for observed in report.get("observed_parts") or []:
            part = observed.get("predicted_part")
            instruments = observed.get("instruments") or []
            section = instruments[0].get("section") if instruments else None
            if part and section and part not in index.section_by_predicted_part:
                index.section_by_predicted_part[part] = section
    return index


def _missing_required(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Project a report's expected parts to the required-but-absent ones the piece is missing."""
    if not report:
        return []
    out: list[dict[str, Any]] = []
    for part in report.get("expected_parts") or []:
        if part.get("required") and not part.get("present"):
            out.append(
                {
                    "label": part.get("label"),
                    "canonical_instrument": part.get("canonical_instrument"),
                    "section": part.get("section"),
                }
            )
    return out


def _part_facet_by_predicted_part(report: dict[str, Any]) -> dict[str, list[tuple[str, Any]]]:
    """Map a predicted_part label (as attached to documents) to all its ``(canonical, part_index)``.

    Learned from the Script 06 ``observed_parts`` rollup, where each observed part carries the
    predicted_part label and the instrument facet(s) it resolved to. Carrying every facet's
    ``part_index`` lets each expected line be linked to the specific document that fills it (e.g.
    Trumpet 3 -> the Trumpet 3 file), and lets one physical file that covers several chairs or
    instruments (e.g. a "Flutes" file resolved to Flute 1 / Flute 2) link to each of those slots.
    The rollup facet key is ``canonical`` (Script 03's token); ``canonical_instrument`` is accepted
    as a fallback for older reports.
    """
    mapping: dict[str, list[tuple[str, Any]]] = {}
    for observed in report.get("observed_parts") or []:
        part = observed.get("predicted_part")
        instruments = observed.get("instruments") or []
        if not part or not instruments or part in mapping:
            continue
        facets: list[tuple[str, Any]] = []
        for facet in instruments:
            canonical = facet.get("canonical") or facet.get("canonical_instrument")
            if canonical:
                facets.append((canonical, facet.get("part_index")))
        if facets:
            mapping[part] = facets
    return mapping


def _instrumentation(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Project a report's full expected-parts list into the site's instrumentation grid.

    Parts keep their upstream canonical order (``part_index``). Each present part is linked to the
    specific document(s) that satisfy it: a numbered part (e.g. Trumpet 3) matches the document
    whose observed part shares the same canonical instrument *and* part index, so multi-rank parts
    are no longer bunched onto a single line. A numbered part with no exact-index document falls back
    to an unnumbered document of the same instrument; an unnumbered expected part links every
    document of that instrument.
    """
    if not report:
        return []
    facet_by_part = _part_facet_by_predicted_part(report)
    # (canonical, part_index) -> [ {doc_id, filename}, ... ] and canonical -> [ ... ] for fallback.
    docs_by_key: dict[tuple[str, Any], list[dict[str, Any]]] = {}
    docs_by_canonical: dict[str, list[dict[str, Any]]] = {}
    for doc in report.get("documents") or []:
        pdf_path = doc.get("pdf_path")
        if not pdf_path:
            continue
        facets = facet_by_part.get(doc.get("predicted_part") or "")
        if not facets:
            continue
        entry = {
            "doc_id": synth_doc_id(pdf_path),
            "filename": doc.get("pdf_filename") or Path(pdf_path.replace("\\", "/")).name,
        }
        # One file may cover several chairs/instruments; link it under each facet, but list it only
        # once per canonical in the instrument-level fallback.
        seen_canonical: set[str] = set()
        for canonical, part_index in facets:
            docs_by_key.setdefault((canonical, part_index), []).append(entry)
            if canonical not in seen_canonical:
                docs_by_canonical.setdefault(canonical, []).append(entry)
                seen_canonical.add(canonical)

    def _documents_for(canonical: str | None, part_index: Any) -> list[dict[str, Any]]:
        if not canonical:
            return []
        if part_index is not None:
            exact = docs_by_key.get((canonical, part_index))
            if exact:
                return exact
            # No exact-index file: fall back to an unnumbered document of the same instrument.
            return docs_by_key.get((canonical, None), [])
        # Unnumbered expected part links every document of this instrument.
        return docs_by_canonical.get(canonical, [])

    parts: list[dict[str, Any]] = []
    for part in report.get("expected_parts") or []:
        canonical = part.get("canonical_instrument")
        part_index = part.get("part_index")
        present = bool(part.get("present"))
        parts.append(
            {
                "part_index": part_index,
                "canonical_instrument": canonical,
                "label": part.get("label"),
                "section": part.get("section"),
                "required": bool(part.get("required")),
                "present": present,
                "documents": _documents_for(canonical, part_index) if present else [],
            }
        )
    return parts


def _unlisted(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Project held parts that are NOT in the authoritative instrumentation ("Present (unlisted)").

    These come from reconcile's ``unexpected_parts`` -- library holdings that the resolved edition's
    instrumentation does not list (e.g. a surplus part). They are informational only and are
    deliberately kept OUT of the completeness math; the site shows them as their own rows so the
    instrumentation grid stays a faithful mirror of the published edition while still surfacing
    everything the library actually owns.
    """
    if not report:
        return []
    rows: list[dict[str, Any]] = []
    for entry in report.get("unexpected_parts") or []:
        instruments = entry.get("instruments") or []
        canonical = instruments[0].get("canonical") if instruments else None
        part_index = instruments[0].get("part_index") if instruments else None
        rows.append(
            {
                "label": _unexpected_display_label(entry),
                "canonical_instrument": canonical,
                "part_index": part_index,
                "count": int(entry.get("count") or 1),
            }
        )
    return rows


def _unexpected_display_label(entry: dict[str, Any]) -> str:
    """Human-readable label for a held-but-unlisted part (mirrors Script 04's `_unexpected_label`)."""
    predicted = entry.get("predicted_part")
    if predicted:
        return str(predicted)
    labels: list[str] = []
    for facet in entry.get("instruments") or []:
        canonical = facet.get("canonical")
        if not canonical:
            continue
        idx = facet.get("part_index")
        labels.append(str(canonical) if idx is None else f"{canonical} {idx}")
    return " / ".join(labels) if labels else "unknown"


def _score_info(report: dict[str, Any] | None) -> dict[str, Any]:
    """Summarize a piece's score presence and link to the score document(s)."""
    if not report:
        return {"has_score": None, "score_missing": False, "score_types": [], "documents": []}
    docs: list[dict[str, Any]] = []
    for doc in report.get("documents") or []:
        if not doc.get("is_score"):
            continue
        pdf_path = doc.get("pdf_path") or ""
        docs.append(
            {
                "doc_id": synth_doc_id(pdf_path),
                "filename": doc.get("pdf_filename") or Path(pdf_path.replace("\\", "/")).name,
                "pdf_path": pdf_path,
                "score_type": doc.get("score_type"),
            }
        )
    return {
        "has_score": report.get("has_score"),
        "score_missing": bool(report.get("score_missing")),
        "score_types": report.get("score_types") or [],
        "documents": docs,
    }


def _instrumentation_source(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """Pass through Script 04's instrumentation provenance summary.

    Answers "where did this instrumentation come from?" on the piece detail page: the resolution
    method (score OCR / web lookup / image OCR / fallback), confidence, LLM notes, and any web
    source URLs. Returns ``None`` when the upstream report predates this field.
    """
    prov = (report or {}).get("instrumentation_provenance")
    return prov if isinstance(prov, dict) else None


def _piece_metadata(report: dict[str, Any] | None) -> dict[str, Any]:
    """Project a piece's catalog metadata for the Overview panel.

    Prefers the online-lookup ``work_identity.resolved`` values (Script 04's authoritative edition
    identity), falling back to the best-effort ``identity_candidates`` scraped from the document
    text in Script 02. ``summary`` is only ever supplied by the lookup.
    """
    work_identity = (report or {}).get("work_identity") or {}
    resolved = work_identity.get("resolved") or {}
    candidates = work_identity.get("identity_candidates") or {}

    def pick(*values: Any) -> Any:
        for value in values:
            if value not in (None, ""):
                return value
        return None

    return {
        "composer": pick(resolved.get("composer"), candidates.get("composer")),
        "arranger": pick(resolved.get("arranger"), candidates.get("arranger")),
        "publisher": pick(resolved.get("publisher"), candidates.get("publisher")),
        "year": pick(resolved.get("year"), candidates.get("copyright_year")),
        "summary": pick(resolved.get("summary")),
    }


def _score_pct(value: Any) -> float | None:
    """Convert a pipeline 0..1 completeness score to a 0..100 percentage (1 decimal)."""
    if value is None:
        return None
    try:
        return round(float(value) * 100.0, 1)
    except (TypeError, ValueError):
        return None


# --- model builders (pure) --------------------------------------------------------------------


def build_manifest(
    run_id: str,
    generated_at: str,
    library_root: str,
    counts: dict[str, int],
    thumbnails: bool,
) -> dict[str, Any]:
    return {
        "site_schema_version": SITE_SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": generated_at,
        "library_root": library_root or "",
        "thumbnails": bool(thumbnails),
        "counts": counts,
    }


def _attention_reason(ap: dict[str, Any]) -> str:
    """Short human summary of why a piece needs attention (from Script 07 fields).

    Scan quality and handwriting are informational only and are never cited as a reason.
    """
    bits: list[str] = []
    missing = ap.get("missing_required_count") or 0
    if missing:
        bits.append(f"{missing} required part{'s' if missing != 1 else ''} missing")
    if not bits:
        codes = ap.get("reason_codes") or []
        if codes:
            bits.append(str(codes[0]).replace("_", " "))
    return "; ".join(bits) or "flagged for review"


def build_dashboard(
    summary: dict[str, Any],
    page_count: int,
    phase_flow: list[dict[str, Any]],
) -> dict[str, Any]:
    totals = summary.get("totals") or {}
    severity = summary.get("severity_distribution") or {}
    quality = summary.get("quality_band_distribution") or {}
    score_summary = summary.get("completeness_score_summary") or {}

    attention = [
        {
            "piece_id": ap.get("piece_id"),
            "title": ap.get("piece_title_guess"),
            "catalog_number": ap.get("catalog_number"),
            "severity": ap.get("severity"),
            "completeness_score": _score_pct(ap.get("completeness_score")),
            "reason": _attention_reason(ap),
        }
        for ap in summary.get("attention_pieces") or []
    ]

    return {
        "kpis": [
            {
                "label": "Pieces",
                "value": summary.get("piece_count") or totals.get("pieces") or 0,
                "hint": "in the collection",
                "href": "pages/pieces.html",
            },
            {
                "label": "Documents",
                "value": totals.get("documents") or 0,
                "hint": "PDF parts & scores",
                "href": "pages/documents.html",
            },
            {"label": "Pages", "value": page_count, "hint": "rendered & analysed"},
            {
                "label": "Need review",
                "value": totals.get("pieces_needs_review") or 0,
                "hint": "flagged for a human",
            },
        ],
        "completeness": {
            "score": _score_pct(score_summary.get("mean")) or 0,
            "label": "Collection completeness (mean)",
        },
        "severity": {
            "high": severity.get("high") or 0,
            "review": severity.get("review") or 0,
            "ok": severity.get("ok") or 0,
        },
        "quality": {
            "good": quality.get("good") or 0,
            "fair": quality.get("fair") or 0,
            "poor": quality.get("poor") or 0,
            "unknown": quality.get("unknown") or 0,
        },
        "attention": attention,
        "phase_flow": phase_flow,
    }


def build_pieces(
    summary_pieces: list[dict[str, Any]],
    index: ReportIndex,
    page_count_by_piece: dict[str, int],
    thumb_for: ThumbResolver,
) -> list[dict[str, Any]]:
    pieces: list[dict[str, Any]] = []
    for sp in summary_pieces:
        piece_id = sp.get("piece_id")
        report = index.by_piece_id.get(piece_id or "")
        first_thumb = None
        if report:
            docs = report.get("documents") or []
            if docs:
                first_thumb = thumb_for(docs[0].get("thumbnail_path"))
        expected_parts = (report or {}).get("expected_parts") or []
        expected_ct = int((report or {}).get("expected_part_count") or len(expected_parts))
        present_ct = sum(1 for e in expected_parts if e.get("present"))
        missing_ct = max(expected_ct - present_ct, 0)
        pieces.append(
            {
                "piece_id": piece_id,
                "title": sp.get("piece_title_guess"),
                "catalog_number": sp.get("catalog_number"),
                "piece_folder": (report or {}).get("piece_folder"),
                "severity": sp.get("severity"),
                "completeness_score": _score_pct(sp.get("completeness_score")),
                "completeness_tier": sp.get("completeness_tier"),
                "document_count": sp.get("document_count") or 0,
                "page_count": page_count_by_piece.get(piece_id or "", 0),
                "expected_part_count": expected_ct,
                "present_part_count": present_ct,
                "missing_part_count": missing_ct,
                "missing_required_count": sp.get("missing_required_count") or 0,
                "missing_required": _missing_required(report),
                "instrumentation": _instrumentation(report),
                "unlisted": _unlisted(report),
                "has_expected_parts": bool((report or {}).get("expected_parts")),
                "score": _score_info(report),
                "instrumentation_source": _instrumentation_source(report),
                "metadata": _piece_metadata(report),
                "thumbnail": first_thumb,
            }
        )
    pieces.sort(key=lambda p: piece_sort_key({"catalog_number": p.get("catalog_number"), "piece_folder": p.get("piece_folder")}))
    return pieces


def build_documents(
    doc_records: list[dict[str, Any]],
    index: ReportIndex,
    thumb_for: ThumbResolver,
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for rec in doc_records:
        pdf_path = rec.get("pdf_path") or ""
        piece_id = rec.get("piece_id")
        report = index.by_piece_id.get(piece_id or "")
        doc_meta = index.doc_meta_by_pdf_path.get(pdf_path, {})
        predicted_part = doc_meta.get("predicted_part")
        instrument = predicted_part or ", ".join(rec.get("ocr_llm_instruments") or []) or None
        documents.append(
            {
                "doc_id": synth_doc_id(pdf_path),
                "piece_id": piece_id,
                "piece_title": (report or {}).get("piece_title_guess"),
                "catalog_number": (report or {}).get("catalog_number"),
                "pdf_filename": rec.get("pdf_filename"),
                "pdf_path": pdf_path,
                "instrument": instrument,
                "section": index.section_by_predicted_part.get(predicted_part or ""),
                "is_score": bool(doc_meta.get("is_score")),
                "score_type": doc_meta.get("score_type"),
                "page_count": rec.get("page_count") or 0,
                "quality": doc_meta.get("quality_band"),
                "notation_source": rec.get("vision_notation_source")
                or doc_meta.get("notation_source_type"),
                "legibility": rec.get("vision_legibility"),
                "ocr_status": rec.get("ocr_llm_status"),
                "ocr_confidence": rec.get("ocr_llm_confidence"),
                "vision_status": rec.get("vision_status"),
                "vision_confidence": rec.get("vision_confidence"),
                "thumbnail": thumb_for(doc_meta.get("thumbnail_path")),
                "first_text": rec.get("first_page_text") or "",
                "vision_notes": rec.get("vision_notes") or "",
            }
        )
    documents.sort(key=lambda d: ((d.get("catalog_number") or "~"), d.get("pdf_filename") or ""))
    return documents


def build_pages(
    page_records: list[dict[str, Any]],
    thumb_for: ThumbResolver,
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    for rec in page_records:
        pdf_path = rec.get("pdf_path") or ""
        doc_id = synth_doc_id(pdf_path)
        page_num = rec.get("page_num")
        pages.append(
            {
                "page_id": synth_page_id(doc_id, page_num),
                "doc_id": doc_id,
                "doc_filename": Path(pdf_path.replace("\\", "/")).name,
                "piece_id": rec.get("piece_id"),
                "page_num": page_num,
                "thumbnail": thumb_for(rec.get("thumbnail_path")),
                "render_dpi": rec.get("render_dpi"),
                "width_px": rec.get("render_width_px"),
                "height_px": rec.get("render_height_px"),
                "orientation": rec.get("orientation"),
                "aspect_ratio": rec.get("aspect_ratio"),
                "text_density": rec.get("text_density"),
                "black_white_ratio": rec.get("black_white_ratio"),
                "is_blank": rec.get("is_blank"),
                "has_staves": rec.get("has_staves"),
                "staff_line_count": rec.get("staff_line_count"),
                "contrast_std": rec.get("contrast_std"),
                "blur_variance": rec.get("blur_variance"),
                "skew_angle_deg": rec.get("skew_angle_deg"),
                "estimated_dpi": rec.get("estimated_dpi"),
                "osd_rotation": rec.get("osd_rotation"),
                "osd_orientation_conf": rec.get("osd_orientation_conf"),
                "osd_script": rec.get("osd_script"),
                "first_text": "",
            }
        )
    pages.sort(key=lambda p: (p.get("doc_id") or "", p.get("page_num") or 0))
    return pages


def _phase_summary(records: int, unit: str, note: str, timestamp: str | None) -> dict[str, Any]:
    return {"records": records, "unit": unit, "note": note, "timestamp": timestamp}


def build_phases(
    summary: dict[str, Any],
    reports: list[dict[str, Any]],
    doc_records: list[dict[str, Any]],
    page_records: list[dict[str, Any]],
    review: dict[str, Any],
    timestamps: dict[str, str | None],
) -> dict[str, Any]:
    totals = summary.get("totals") or {}
    n_docs = len(doc_records)
    n_pages = len(page_records)
    n_pieces = summary.get("piece_count") or totals.get("pieces") or 0
    quality = summary.get("quality_band_distribution") or {}
    completeness = summary.get("completeness_distribution") or {}
    severity = summary.get("severity_distribution") or {}
    score_summary = summary.get("completeness_score_summary") or {}
    pieces = summary.get("pieces") or []
    queue = review.get("queue") or []

    t_extract = timestamps.get("extract")
    t_reports = timestamps.get("reports")
    t_review = timestamps.get("review")

    # Orientation distribution for phase 2 (small, safe to chart).
    orientation_counts: dict[str, int] = {}
    for rec in page_records:
        key = rec.get("orientation") or "unknown"
        orientation_counts[key] = orientation_counts.get(key, 0) + 1

    docs_needing_ocr = sum(1 for r in doc_records if (r.get("pages_ocr_applied") or 0) > 0)
    docs_needs_review = sum(
        1
        for rep in reports
        for doc in (rep.get("documents") or [])
        if doc.get("needs_review")
    )

    expected_parts_total = sum(int(rep.get("expected_part_count") or 0) for rep in reports)
    missing_required_total = sum(int(p.get("missing_required_count") or 0) for p in pieces)

    top_missing_rows = [
        {
            "section_id": s.get("section"),
            "section": _titlecase(s.get("section")),
            "missing_piece_count": s.get("missing_piece_count") or 0,
        }
        for s in (summary.get("top_missing_sections") or [])
    ]

    return {
        "1": {
            "summary": _phase_summary(n_docs, "documents", f"{n_docs} PDFs scanned and grouped into {n_pieces} pieces.", t_extract),
            "stats": [
                {"label": "PDFs found", "value": n_docs},
                {"label": "Pieces", "value": n_pieces},
            ],
            "table": {
                "caption": "Inventory",
                "entity": "document",
                "columns": [
                    {"key": "pdf_filename", "label": "Document", "link": "document"},
                    {"key": "piece_title", "label": "Piece"},
                    {"key": "page_count", "label": "Pages", "num": True},
                ],
                "source": "documents",
            },
        },
        "2": {
            "summary": _phase_summary(n_pages, "pages", "Pages rendered, scans OCR'd, and each document vision-analysed.", t_extract),
            "stats": [
                {"label": "Pages rendered", "value": n_pages},
                {"label": "Documents OCR'd", "value": docs_needing_ocr},
                {"label": "Documents", "value": n_docs},
            ],
            "charts": [
                {
                    "title": "Pages by orientation",
                    "rows": [
                        {"label": _titlecase(k), "value": v}
                        for k, v in sorted(orientation_counts.items())
                    ],
                }
            ],
            "table": {
                "caption": "Analysed pages",
                "entity": "page",
                "columns": [
                    {"key": "page_num", "label": "Page", "num": True, "link": "page"},
                    {"key": "doc_filename", "label": "Document"},
                    {"key": "orientation", "label": "Orientation"},
                    {"key": "staff_line_count", "label": "Staves", "num": True},
                ],
                "source": "pages",
            },
        },
        "3": {
            "summary": _phase_summary(n_docs, "documents", "Each document classified to a predicted instrument / part.", t_extract),
            "stats": [
                {"label": "Classified", "value": n_docs},
                {"label": "Flagged for review", "value": docs_needs_review},
            ],
            "table": {
                "caption": "Predicted parts",
                "entity": "document",
                "columns": [
                    {"key": "pdf_filename", "label": "Document", "link": "document"},
                    {"key": "instrument", "label": "Instrument"},
                    {"key": "section", "label": "Section"},
                ],
                "source": "documents",
            },
        },
        "4": {
            "summary": _phase_summary(n_pieces, "pieces", "Expected instrumentation inferred for each piece.", t_reports),
            "stats": [
                {"label": "Pieces", "value": n_pieces},
                {"label": "Expected parts", "value": expected_parts_total},
                {"label": "Missing required", "value": missing_required_total},
            ],
            "table": {
                "caption": "Expected instrumentation by piece",
                "entity": "piece",
                "sortKey": "missing_part_count",
                "dir": "desc",
                "columns": [
                    {"key": "catalog_number", "label": "Catalog #"},
                    {"key": "title", "label": "Piece", "link": "piece"},
                    {"key": "expected_part_count", "label": "Expected", "num": True},
                    {"key": "present_part_count", "label": "Have", "num": True},
                    {"key": "missing_part_count", "label": "Missing", "num": True, "bar": True, "barOf": "expected_part_count", "variant": "warning"},
                    {"key": "completeness_score", "label": "Complete", "num": True, "display": "pct"},
                ],
                "source": "pieces",
            },
        },
        "5": {
            "summary": _phase_summary(n_docs, "documents", "Scan quality scored; legibility issues flagged.", t_extract),
            "stats": [
                {"label": "Good", "value": quality.get("good") or 0},
                {"label": "Fair", "value": quality.get("fair") or 0},
                {"label": "Poor", "value": quality.get("poor") or 0},
            ],
            "charts": [
                {
                    "title": "Quality bands",
                    "rows": [
                        {"label": "Good", "value": quality.get("good") or 0, "variant": "success"},
                        {"label": "Fair", "value": quality.get("fair") or 0, "variant": "warning"},
                        {"label": "Poor", "value": quality.get("poor") or 0, "variant": "error"},
                    ],
                }
            ],
        },
        "6": {
            "summary": _phase_summary(n_pieces, "pieces", "Completeness and severity scored per piece.", t_reports),
            "stats": [
                {"label": "Pieces", "value": n_pieces},
                {"label": "Complete", "value": completeness.get("complete") or 0},
                {"label": "High severity", "value": severity.get("high") or 0},
            ],
            "table": {
                "caption": "Piece completeness",
                "entity": "piece",
                "sortKey": "missing_required_count",
                "dir": "desc",
                "columns": [
                    {"key": "catalog_number", "label": "Catalog #"},
                    {"key": "title", "label": "Piece", "link": "piece"},
                    {"key": "expected_part_count", "label": "Expected", "num": True},
                    {"key": "present_part_count", "label": "Have", "num": True},
                    {"key": "missing_required_count", "label": "Missing required", "num": True, "bar": True, "barOf": "expected_part_count", "variant": "warning"},
                    {"key": "completeness_score", "label": "Complete", "num": True, "display": "pct"},
                ],
                "source": "pieces",
            },
        },
        "7": {
            "summary": _phase_summary(1, "report", "Collection-wide rollup across all pieces.", t_reports),
            "stats": [
                {"label": "Mean completeness", "value": _score_pct(score_summary.get("mean")) or 0, "display": f"{_score_pct(score_summary.get('mean')) or 0:.0f}%"},
                {"label": "Pieces at risk", "value": (severity.get("high") or 0) + (severity.get("review") or 0)},
            ],
            "table": {
                "caption": "Top missing sections",
                "entity": "section",
                "sortKey": "missing_piece_count",
                "dir": "desc",
                "columns": [
                    {"key": "section", "label": "Section", "link": "section"},
                    {"key": "missing_piece_count", "label": "Pieces missing", "num": True, "bar": True, "variant": "warning"},
                ],
                "rows": top_missing_rows,
            },
        },
        "8": {
            "summary": _phase_summary(len(queue), "pieces flagged", "Prioritized manual-review queue.", t_review),
            "stats": [
                {"label": "In queue", "value": len(queue)},
                {"label": "Top priority", "value": (queue[0].get("priority_score") if queue else 0)},
            ],
            "table": {
                "caption": "Review queue",
                "entity": "piece",
                "sortKey": "missing_required_count",
                "dir": "desc",
                "columns": [
                    {"key": "catalog_number", "label": "Catalog #"},
                    {"key": "title", "label": "Piece", "link": "piece"},
                    {"key": "severity", "label": "Severity", "badge": "severity"},
                    {"key": "expected_part_count", "label": "Expected", "num": True},
                    {"key": "present_part_count", "label": "Have", "num": True},
                    {"key": "missing_required_count", "label": "Missing required", "num": True, "bar": True, "barOf": "expected_part_count", "variant": "warning"},
                    {"key": "completeness_score", "label": "Completeness", "num": True, "display": "pct"},
                ],
                "source": "pieces",
                "filter": {"field": "severity", "in": ["high", "review"]},
            },
        },
    }


def _tier_variant(tier: str | None) -> str:
    return {
        "complete": "success",
        "near_complete": "success",
        "incomplete": "warning",
        "severely_incomplete": "error",
    }.get(tier or "", "warning")


def _titlecase(value: str | None) -> str:
    return str(value or "").replace("_", " ").replace("-", " ").strip().title() or "Unknown"


# --- serialization ----------------------------------------------------------------------------


def render_data_module(key: str, payload: Any) -> str:
    """Render a ``web/data/*.js`` module that registers ``payload`` onto ``window.MLG``.

    ``ensure_ascii=True`` keeps the output pure ASCII, which also escapes U+2028/U+2029 -- valid in
    JSON but illegal inside JavaScript string literals -- so the emitted object literal is always
    safe to load via a classic ``<script>`` tag.
    """
    body = json.dumps(payload, ensure_ascii=True, indent=2)
    return (
        "/* Generated by scripts/09_static_site.py - DO NOT EDIT. */\n"
        f"MLG.register({json.dumps(key)}, {body});\n"
    )


def _atomic_write_text(path: Path, text: str) -> None:
    from scripts._common import atomic_write_text

    atomic_write_text(path, text)


# --- thumbnails (isolated: the only Pillow / filesystem-heavy step) ---------------------------


def collect_thumbnail_paths(
    reports: list[dict[str, Any]], page_records: list[dict[str, Any]]
) -> list[str]:
    """Every distinct render cache path referenced by a document or page (order-stable)."""
    seen: dict[str, None] = {}
    for report in reports:
        for doc in report.get("documents") or []:
            path = doc.get("thumbnail_path")
            if path:
                seen.setdefault(path, None)
    for rec in page_records:
        path = rec.get("thumbnail_path")
        if path:
            seen.setdefault(path, None)
    return list(seen.keys())


def generate_thumbnails(
    cache_paths: list[str],
    repo_root: Path,
    web_dir: Path,
    max_px: int,
    rebuild: bool,
) -> set[str]:
    """Downsize each referenced PNG to WebP under ``web/assets/thumbs/``.

    Returns the set of cache paths for which a usable web thumbnail now exists. Skips paths whose
    source is missing and, unless ``rebuild`` is set, paths already converted. Requires Pillow;
    raises ``RuntimeError`` if it is unavailable so the CLI can surface a clear message.
    """
    try:
        from PIL import Image  # type: ignore
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("Pillow is required for thumbnail generation (pip install Pillow).") from exc

    thumbs_dir = web_dir / "assets" / "thumbs"
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    available: set[str] = set()
    converted = skipped = missing = 0

    total = len(cache_paths)
    start_time = time.monotonic()
    # Log a progress line at most every ~2s (plus first and last) so long runs show they are
    # alive without flooding the log on fast, mostly-reused runs.
    last_log = start_time
    log_interval_s = 2.0
    logger.info("Thumbnails: generating for %d referenced render(s) (rebuild=%s).", total, rebuild)

    for index, cache_path in enumerate(cache_paths, start=1):
        web_rel = thumb_web_path(cache_path)
        if not web_rel:
            continue
        dst = web_dir / web_rel
        src = (repo_root / cache_path).resolve()
        if not src.is_file():
            missing += 1
            continue
        if dst.is_file() and not rebuild:
            available.add(cache_path)
            skipped += 1
            continue
        try:
            with Image.open(src) as im:
                im = im.convert("RGB")
                im.thumbnail((max_px, max_px))
                dst.parent.mkdir(parents=True, exist_ok=True)
                im.save(dst, format="WEBP", quality=80, method=6)
            available.add(cache_path)
            converted += 1
        except Exception as exc:  # noqa: BLE001 - one bad image must not abort the whole run
            logger.warning("Failed to convert thumbnail %s: %s", src, exc)
            missing += 1

        now = time.monotonic()
        if index == total or now - last_log >= log_interval_s:
            last_log = now
            logger.info(
                "Thumbnails: [%d/%d] %.0f%% (%d converted, %d reused, %d missing/failed, %.1fs elapsed)",
                index,
                total,
                (index / total * 100) if total else 100.0,
                converted,
                skipped,
                missing,
                now - start_time,
            )

    logger.info(
        "Thumbnails: %d converted, %d reused, %d missing/failed (of %d referenced) in %.1fs.",
        converted,
        skipped,
        missing,
        total,
        time.monotonic() - start_time,
    )
    return available


def copy_pdfs(documents: list[dict[str, Any]], library_root: Path, web_dir: Path) -> int:
    """Copy each source PDF into ``web/assets/pdfs/`` preserving its relative path. Returns count."""
    dest_root = web_dir / "assets" / "pdfs"
    copied = 0
    for doc in documents:
        rel = doc.get("pdf_path")
        if not rel:
            continue
        src = (library_root / rel).resolve()
        if not src.is_file():
            logger.warning("Source PDF not found, skipping: %s", src)
            continue
        dst = dest_root / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1
    logger.info("Copied %d PDF(s) into %s.", copied, dest_root)
    return copied


# --- CLI --------------------------------------------------------------------------------------


@app.command()
def main(
    data_dir: Path = typer.Option(Path("data"), help="Pipeline output directory (Scripts 01-08)"),
    web_dir: Path = typer.Option(Path("web"), help="Static-site root that holds the scaffold"),
    library_root: str = typer.Option(
        "", help="Absolute path to the folder holding the original PDFs (recorded in the manifest)"
    ),
    thumbnails: bool = typer.Option(
        True, "--thumbnails/--no-thumbnails", help="Generate downsized WebP thumbnails"
    ),
    thumb_max_px: int = typer.Option(480, help="Max WebP thumbnail edge in pixels"),
    rebuild_thumbs: bool = typer.Option(
        False, "--rebuild-thumbs", help="Re-render thumbnails even when they already exist"
    ),
    copy_source_pdfs: bool = typer.Option(
        False, "--copy-pdfs/--no-copy-pdfs", help="Bundle the original PDFs into web/assets/pdfs/"
    ),
    mode: str = typer.Option(
        "full", help="Accepted for pipeline uniformity; the site data is always fully rebuilt"
    ),
    only_piece: int = typer.Option(
        None,
        help=(
            "Accepted for pipeline uniformity; the site data always covers every piece report on "
            "disk (the targeted piece is already updated by Script 06)."
        ),
    ),
    log_level: str = typer.Option("INFO", help="DEBUG, INFO, WARNING, ERROR"),
) -> None:
    """Build the static-site data layer (web/data/*.js) and thumbnails from the pipeline output."""
    mode = mode.lower().strip()
    if mode not in {"full", "incremental"}:
        raise typer.BadParameter("mode must be 'full' or 'incremental'")

    setup_logging(log_level)
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    generated_at = datetime.now(UTC).isoformat()
    start_time = time.perf_counter()

    data_dir = data_dir.resolve()
    web_dir = web_dir.resolve()
    repo_root = Path.cwd()

    # --- load inputs ---
    summary = read_json(data_dir / "collection_reports" / "summary.json") or {}
    reports = [
        rep
        for path in sorted((data_dir / "piece_reports").glob("*.json"))
        if not path.name.startswith(".")
        for rep in [read_json(path)]
        if rep and rep.get("piece_id")
    ]
    doc_records = read_jsonl(data_dir / "documents.jsonl")
    page_records = read_jsonl(data_dir / "pages.jsonl")
    review = read_json(data_dir / "review_pack" / "manual_review_queue.json") or {}

    if not summary:
        raise typer.BadParameter(
            f"No collection summary found at {data_dir / 'collection_reports' / 'summary.json'}. "
            "Run Scripts 01-08 first."
        )
    _warn_schema(summary, reports)

    index = index_reports(reports)

    # --- thumbnails (before model build, so models only reference thumbs that exist) ---
    if thumbnails:
        cache_paths = collect_thumbnail_paths(reports, page_records)
        available = generate_thumbnails(cache_paths, repo_root, web_dir, thumb_max_px, rebuild_thumbs)
    else:
        available = set()

    def thumb_for(cache_path: str | None) -> str | None:
        if not cache_path:
            return None
        if thumbnails and cache_path not in available:
            return None
        return thumb_web_path(cache_path)

    # --- per-piece page counts ---
    page_count_by_piece: dict[str, int] = {}
    for rec in page_records:
        pid = rec.get("piece_id")
        if pid:
            page_count_by_piece[pid] = page_count_by_piece.get(pid, 0) + 1

    # --- build models ---
    pieces = build_pieces(summary.get("pieces") or [], index, page_count_by_piece, thumb_for)
    documents = build_documents(doc_records, index, thumb_for)
    pages = build_pages(page_records, thumb_for)

    timestamps = {
        "extract": (doc_records[0].get("processing_timestamp") if doc_records else None),
        "reports": summary.get("processing_timestamp"),
        "review": review.get("processing_timestamp"),
    }
    phase_flow = [
        {"n": 1, "records": len(documents), "note": f"{len(documents)} PDFs \u2192 {len(pieces)} pieces"},
        {"n": 2, "records": len(pages), "note": f"{len(pages)} pages rendered, OCR + vision"},
        {"n": 3, "records": len(documents), "note": f"{len(documents)} documents classified"},
        {"n": 4, "records": len(pieces), "note": f"expected parts for {len(pieces)} pieces"},
        {"n": 5, "records": len(documents), "note": f"{len(documents)} documents quality-checked"},
        {"n": 6, "records": len(pieces), "note": f"{len(pieces)} piece reports"},
        {"n": 7, "records": 1, "note": "1 collection report"},
        {"n": 8, "records": len(review.get("queue") or []), "note": f"{len(review.get('queue') or [])} in review queue"},
    ]

    counts = {
        "pieces": len(pieces),
        "documents": len(documents),
        "pages": len(pages),
        "render_images": len(pages),
    }
    manifest = build_manifest(run_id, generated_at, library_root, counts, thumbnails)
    dashboard = build_dashboard(summary, len(pages), phase_flow)
    phases = build_phases(summary, reports, doc_records, page_records, review, timestamps)

    if copy_source_pdfs:
        if not library_root:
            raise typer.BadParameter("--copy-pdfs requires --library-root to locate the source PDFs.")
        copy_pdfs(documents, Path(library_root).resolve(), web_dir)

    # --- write data modules ---
    payloads = {
        "manifest": manifest,
        "dashboard": dashboard,
        "phases": phases,
        "pieces": pieces,
        "documents": documents,
        "pages": pages,
    }
    out_dir = web_dir / "data"
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename, key in DATA_MODULES:
        _atomic_write_text(out_dir / filename, render_data_module(key, payloads[key]))

    logger.info(
        "Static site data written: pieces=%d documents=%d pages=%d elapsed=%.1fs output=%s",
        len(pieces),
        len(documents),
        len(pages),
        time.perf_counter() - start_time,
        out_dir,
    )


def _warn_schema(summary: dict[str, Any], reports: list[dict[str, Any]]) -> None:
    sv = summary.get("record_version")
    if sv and sv != EXPECTED_SUMMARY_SCHEMA:
        logger.warning(
            "Collection summary is schema %s (expected %s); some fields may be missing.",
            sv,
            EXPECTED_SUMMARY_SCHEMA,
        )
    bad = {r.get("record_version") for r in reports} - {EXPECTED_PIECE_SCHEMA, None}
    if bad:
        logger.warning(
            "Piece reports include schema version(s) other than %s (%s).",
            EXPECTED_PIECE_SCHEMA,
            sorted(v for v in bad if v),
        )


if __name__ == "__main__":
    app()
