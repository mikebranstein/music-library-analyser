"""Script 05: scan-quality checks and notation-source classification.

Consumes Script 02's per-page datasets (``data/pages.jsonl`` + ``data/extracted_text.jsonl``) and
writes one per-document quality record to ``data/quality_metrics.jsonl`` plus a Markdown report.

The engine is deterministic and threshold-driven: it *reuses* the objective metrics Script 02
already computed (resolution, skew, contrast, blur, OCR confidence, blankness) rather than
re-rendering pages, evaluates each page against ``config/quality_thresholds.yaml``, and rolls the
per-page findings up into a document quality score/band. It never flags an issue from a missing
(null) metric, and it classifies each document's notation source (printed/engraved vs handwritten)
from the same signals. A ``--use-vision`` hook is reserved for an optional model-assisted pass but
is not wired to a provider yet.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None

from scripts._common import (
    ProcessingStatus,
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    build_checkpoint,
    load_checkpoint,
    make_checkpoint_path,
    md_cell,
    new_record_envelope,
    pct,
    read_jsonl,
    run_with_progress,
    setup_logging,
    sha256_text,
    utc_now_iso,
)

RECORD_VERSION = "1.1"

CHECKPOINT_FILENAME = ".quality_checks_checkpoint.json"

app = typer.Typer(add_completion=False)

logger = logging.getLogger("script05.quality")


# --- Output vocabulary ------------------------------------------------------------------------

# Per-page issue codes (also used as keys in the penalty table and issue summaries).
ISSUE_LOW_RESOLUTION = "low_resolution"
ISSUE_EXCESSIVE_SKEW = "excessive_skew"
ISSUE_LOW_CONTRAST = "low_contrast"
ISSUE_HEAVY_BLUR = "heavy_blur"
ISSUE_OCR_ILLEGIBLE = "ocr_illegible"
ISSUE_BLANK_PAGE = "blank_page"
ISSUE_NOISE_PAGE = "noise_page"
# Document-level issues contributed by the optional vision review (not per-page metrics).
ISSUE_HANDWRITTEN = "handwritten_notation"
ISSUE_LOW_LEGIBILITY = "low_legibility"

ISSUE_ORDER: tuple[str, ...] = (
    ISSUE_LOW_RESOLUTION,
    ISSUE_EXCESSIVE_SKEW,
    ISSUE_LOW_CONTRAST,
    ISSUE_HEAVY_BLUR,
    ISSUE_OCR_ILLEGIBLE,
    ISSUE_BLANK_PAGE,
    ISSUE_NOISE_PAGE,
    ISSUE_HANDWRITTEN,
    ISSUE_LOW_LEGIBILITY,
)


class QualityBand:
    """Values of the ``quality_band`` field."""

    GOOD = "good"
    REVIEW = "review"
    POOR = "poor"
    UNKNOWN = "unknown"  # document had no scoreable pages


QUALITY_BAND_ORDER: tuple[str, ...] = (
    QualityBand.GOOD,
    QualityBand.REVIEW,
    QualityBand.POOR,
    QualityBand.UNKNOWN,
)


class NotationSource:
    """Values of the ``notation_source_type`` field."""

    PRINTED = "printed_original"
    HANDWRITTEN = "handwritten"
    MIXED = "mixed_or_uncertain"


NOTATION_SOURCE_ORDER: tuple[str, ...] = (
    NotationSource.PRINTED,
    NotationSource.HANDWRITTEN,
    NotationSource.MIXED,
)


# --- Built-in thresholds (authoritative default; YAML overrides/extends) ---------------------

DEFAULT_THRESHOLDS: dict[str, Any] = {
    "quality_checks": {
        "min_estimated_dpi": 200,
        "min_render_width_px": 1000,
        "min_render_height_px": 1400,
        "max_skew_angle_deg": 3.0,
        "min_contrast_std": 22.0,
        "min_blur_variance": 90.0,
        # OCR mean word confidence is a 0..1 fraction (Script 02 divides Tesseract's 0..100 by 100).
        # Music scans are notation-dominant, so OCR confidence runs low even when a human can read
        # the part; only genuinely garbage OCR (bottom few percent) is treated as illegible.
        "min_ocr_confidence": 0.30,
        "min_alnum_ratio_proxy": 0.55,
        "blank_text_density_max": 0.004,
        "noise_text_density_min": 0.55,
        "noise_max_word_count": 3,
        "penalties": {
            ISSUE_LOW_RESOLUTION: 25,
            # Skew is noted for visibility but only lightly penalized: a tilted page is still
            # readable unless content is cropped (a signal Script 02 does not yet expose).
            ISSUE_EXCESSIVE_SKEW: 5,
            ISSUE_LOW_CONTRAST: 20,
            ISSUE_HEAVY_BLUR: 30,
            ISSUE_OCR_ILLEGIBLE: 20,
            ISSUE_BLANK_PAGE: 35,
            ISSUE_NOISE_PAGE: 35,
        },
        "bands": {
            "good_min_score": 80,
            "review_min_score": 50,
        },
    },
    "notation_source": {
        "searchable_fraction_printed": 0.5,
        "printed_min_ocr_confidence": 0.80,
        "printed_min_alnum_ratio": 0.70,
        "handwritten_max_ocr_confidence": 0.35,
        "handwritten_max_alnum_ratio": 0.45,
        "min_confidence": 0.30,
    },
    # Adjudication of the optional Script 02 vision signal (vision_* fields on documents.jsonl).
    # A confident vision verdict overrides the deterministic notation source and caps the quality
    # band, because deterministic metrics cannot tell handwritten manuscript from a readable
    # printed photocopy. Set enabled=false to ignore vision fields even when present.
    "vision": {
        "enabled": True,
        "min_confidence": 0.50,
        "override_notation_source": True,
        "handwritten_caps_band_at": "review",
        "fair_legibility_caps_band_at": "review",
        "poor_legibility_caps_band_at": "poor",
    },
}


def _merge_section(base: dict[str, Any], override: Any) -> dict[str, Any]:
    """Shallow-merge one YAML section over a copy of the built-in defaults (one level deep)."""
    merged = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    if isinstance(override, dict):
        for key, value in override.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value
    return merged


def load_thresholds(config_path: Path) -> tuple[dict[str, Any], str]:
    """Return (thresholds, source) merging YAML overrides over the built-in defaults."""
    thresholds = {
        "quality_checks": _merge_section(DEFAULT_THRESHOLDS["quality_checks"], None),
        "notation_source": _merge_section(DEFAULT_THRESHOLDS["notation_source"], None),
        "vision": _merge_section(DEFAULT_THRESHOLDS["vision"], None),
    }
    if yaml is None:
        logger.warning("PyYAML unavailable; using built-in quality thresholds.")
        return thresholds, "builtin"
    if not config_path.exists():
        logger.warning("Thresholds file %s not found; using built-in defaults.", config_path)
        return thresholds, "builtin"
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to parse %s (%s); using built-in thresholds.", config_path, exc)
        return thresholds, "builtin"

    thresholds["quality_checks"] = _merge_section(
        DEFAULT_THRESHOLDS["quality_checks"], data.get("quality_checks")
    )
    thresholds["notation_source"] = _merge_section(
        DEFAULT_THRESHOLDS["notation_source"], data.get("notation_source")
    )
    thresholds["vision"] = _merge_section(
        DEFAULT_THRESHOLDS["vision"], data.get("vision")
    )
    return thresholds, "yaml"


# --- Numeric helpers -------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    """Coerce a metric to float, treating bools/None/non-numbers as 'not measured'."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return (sum(vals) / len(vals)) if vals else None


def _median(values: list[float | None]) -> float | None:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0


def _round(value: float | None, digits: int = 2) -> float | None:
    return round(value, digits) if value is not None else None


def _fold_skew(value: float | None) -> float | None:
    """Reduce a skew reading to genuine skew in [-45, 45], folding out 90-degree page rotation.

    A page scanned in the wrong orientation reports a skew near a multiple of 90 degrees; that is
    rotation, not skew, and should not count as a defect. Folding modulo 90 leaves only the true
    deviation from the nearest right angle.
    """
    if value is None:
        return None
    return ((value + 45.0) % 90.0) - 45.0


# --- Per-page evaluation ---------------------------------------------------------------------


def evaluate_page(
    page: dict[str, Any], text: dict[str, Any] | None, thresholds: dict[str, Any]
) -> list[str]:
    """Return the list of quality-issue codes for one page.

    Only checks whose underlying Script 02 metric is present are evaluated; a null metric never
    produces an issue. OCR illegibility is only asserted when OCR actually found words (so pure
    notation pages with no text are not penalized).

    Resolution and legibility are raster concerns: the render-pixel resolution fallback and the
    alphanumeric-ratio legibility proxy are applied only to image-based / OCR'd pages. Born-digital
    (embedded-text) pages have no intrinsic scan resolution and carry an authoritative text layer,
    so neither fallback fires for them.
    """
    qc = thresholds["quality_checks"]
    text = text or {}
    issues: list[str] = []

    page_is_raster = bool(page.get("is_image_based"))

    est_dpi = _num(page.get("estimated_dpi"))
    width = _num(page.get("render_width_px"))
    height = _num(page.get("render_height_px"))
    low_resolution = False
    if page_is_raster:
        # Resolution is only meaningful for scanned pages. On born-digital pages the readable
        # content is vector; a present estimated_dpi merely reflects some embedded decorative
        # image (e.g. a logo) and says nothing about the legibility of the page.
        if est_dpi is not None:
            low_resolution = est_dpi < qc["min_estimated_dpi"]
        elif width is not None and height is not None:
            # Render pixels only proxy scan resolution when there is no measured DPI.
            low_resolution = width < qc["min_render_width_px"] or height < qc["min_render_height_px"]
    if low_resolution:
        issues.append(ISSUE_LOW_RESOLUTION)

    skew = _num(page.get("skew_angle_deg"))
    genuine_skew = _fold_skew(skew)
    if genuine_skew is not None and abs(genuine_skew) > qc["max_skew_angle_deg"]:
        issues.append(ISSUE_EXCESSIVE_SKEW)

    contrast = _num(page.get("contrast_std"))
    if contrast is not None and contrast < qc["min_contrast_std"]:
        issues.append(ISSUE_LOW_CONTRAST)

    blur = _num(page.get("blur_variance"))
    if blur is not None and blur < qc["min_blur_variance"]:
        issues.append(ISSUE_HEAVY_BLUR)

    density = _num(page.get("text_density"))
    word_count = _num(text.get("word_count")) or 0.0
    is_blank = page.get("is_blank")
    is_blank_by_density = (
        density is not None and density <= qc["blank_text_density_max"] and word_count == 0
    )
    if is_blank is True or is_blank_by_density:
        issues.append(ISSUE_BLANK_PAGE)

    if (
        density is not None
        and density >= qc["noise_text_density_min"]
        and word_count <= qc["noise_max_word_count"]
    ):
        issues.append(ISSUE_NOISE_PAGE)

    # Legibility: only meaningful when the page was expected to carry text.
    if ISSUE_BLANK_PAGE not in issues:
        conf = _num(text.get("ocr_confidence"))
        ocr_word_count = _num(text.get("ocr_word_count")) or 0.0
        alnum = _num(text.get("alnum_ratio"))
        text_from_ocr = text.get("text_source") == "ocr" or bool(text.get("ocr_applied"))
        if conf is not None and ocr_word_count > 0:
            if conf < qc["min_ocr_confidence"]:
                issues.append(ISSUE_OCR_ILLEGIBLE)
        elif (
            text_from_ocr
            and alnum is not None
            and word_count > 0
            and alnum < qc["min_alnum_ratio_proxy"]
        ):
            # alnum-ratio is a legibility proxy for OCR'd raster text only. Embedded (born-digital)
            # text is authoritative regardless of its alphanumeric density, which runs naturally low
            # on scores (dynamics, rehearsal marks, tempo, numbers) without impairing legibility.
            issues.append(ISSUE_OCR_ILLEGIBLE)

    return issues


def band_for(score: float, bands: dict[str, Any]) -> str:
    if score >= bands["good_min_score"]:
        return QualityBand.GOOD
    if score >= bands["review_min_score"]:
        return QualityBand.REVIEW
    return QualityBand.POOR


def score_document(
    page_issue_lists: list[list[str]], analyzed: int, thresholds: dict[str, Any]
) -> tuple[float, str, dict[str, int], list[str], int]:
    """Roll per-page issues into (score, band, issue_summary, top_issues, page_issue_count).

    Each issue penalizes the score by its weight scaled by the fraction of analyzed pages it
    affects, so a defect on every page hurts more than a defect on one page.
    """
    qc = thresholds["quality_checks"]
    penalties = qc["penalties"]
    summary: dict[str, int] = {}
    for issues in page_issue_lists:
        for code in set(issues):
            summary[code] = summary.get(code, 0) + 1

    contributions: dict[str, float] = {}
    total_penalty = 0.0
    for code, count in summary.items():
        fraction = (count / analyzed) if analyzed else 0.0
        contrib = float(penalties.get(code, 0)) * fraction
        contributions[code] = contrib
        total_penalty += contrib

    score = max(0.0, 100.0 - total_penalty)
    band = band_for(score, qc["bands"])
    top_issues = [
        code
        for code, _ in sorted(contributions.items(), key=lambda kv: kv[1], reverse=True)
        if contributions[code] > 0
    ]
    page_issue_count = sum(1 for issues in page_issue_lists if issues)
    return round(score, 1), band, summary, top_issues, page_issue_count


# --- Notation-source classification ----------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def classify_notation_source(
    page_texts: list[dict[str, Any] | None], pages: list[dict[str, Any]], thresholds: dict[str, Any]
) -> tuple[str, float, list[str]]:
    """Classify a document's notation source from Script 02 text/image signals.

    Returns (type, confidence 0..1, evidence). Embedded/searchable text is the strongest printed
    signal; otherwise OCR confidence + alphanumeric ratio discriminate engraved print from
    handwriting. Falls back to ``mixed_or_uncertain`` when signals are weak or absent.
    """
    ns = thresholds["notation_source"]
    total = len(page_texts)
    if total == 0:
        return NotationSource.MIXED, 0.0, ["no analyzable pages"]

    searchable = sum(1 for t in page_texts if t and t.get("text_is_searchable"))
    searchable_fraction = searchable / total
    image_based = sum(1 for p in pages if p.get("is_image_based"))
    image_based_fraction = (image_based / len(pages)) if pages else 0.0

    conf_values = [
        _num(t.get("ocr_confidence"))
        for t in page_texts
        if t and (_num(t.get("ocr_word_count")) or 0) > 0
    ]
    alnum_values = [
        _num(t.get("alnum_ratio"))
        for t in page_texts
        if t and (_num(t.get("word_count")) or 0) > 0
    ]
    mean_conf = _mean(conf_values)
    mean_alnum = _mean(alnum_values)

    evidence = [
        f"searchable_fraction={searchable_fraction:.2f}",
        f"image_based_fraction={image_based_fraction:.2f}",
    ]
    if mean_conf is not None:
        evidence.append(f"mean_ocr_confidence={mean_conf:.1f}")
    if mean_alnum is not None:
        evidence.append(f"mean_alnum_ratio={mean_alnum:.2f}")

    min_conf = float(ns["min_confidence"])

    # Strongest signal: a real embedded text layer implies an engraved/printed original.
    if searchable_fraction >= ns["searchable_fraction_printed"]:
        confidence = _clamp(0.55 + 0.4 * searchable_fraction, min_conf, 0.95)
        return NotationSource.PRINTED, round(confidence, 2), evidence

    # OCR-based discrimination when the page carried OCR-recognized words.
    if mean_conf is not None:
        alnum_ok_printed = mean_alnum is None or mean_alnum >= ns["printed_min_alnum_ratio"]
        # Handwriting cannot be asserted from low OCR confidence alone: a photocopied *printed*
        # part scores just as low as manuscript because Tesseract is reading music notation, not
        # text. Require a corroborating low alphanumeric ratio from a real embedded text layer;
        # when no such signal exists (a plain image scan), stay uncertain rather than guess.
        hand_alnum_ok = mean_alnum is not None and mean_alnum <= ns["handwritten_max_alnum_ratio"]
        if mean_conf >= ns["printed_min_ocr_confidence"] and alnum_ok_printed:
            margin = (mean_conf - ns["printed_min_ocr_confidence"]) / 0.20
            return (
                NotationSource.PRINTED,
                round(_clamp(0.55 + margin, min_conf, 0.9), 2),
                evidence,
            )
        if mean_conf <= ns["handwritten_max_ocr_confidence"] and hand_alnum_ok:
            margin = (ns["handwritten_max_ocr_confidence"] - mean_conf) / 0.30
            return (
                NotationSource.HANDWRITTEN,
                round(_clamp(0.5 + margin, min_conf, 0.85), 2),
                evidence,
            )
        return (
            NotationSource.MIXED,
            round(min_conf, 2),
            [*evidence, "printed vs handwritten not determinable without vision"],
        )

    # Weak fallback: only an alnum ratio is available.
    if mean_alnum is not None:
        if mean_alnum >= ns["printed_min_alnum_ratio"]:
            return NotationSource.PRINTED, round(min_conf + 0.1, 2), evidence
        if mean_alnum <= ns["handwritten_max_alnum_ratio"]:
            return NotationSource.HANDWRITTEN, round(min_conf + 0.1, 2), evidence
        return NotationSource.MIXED, round(min_conf, 2), evidence

    # No usable text signal at all (e.g. pure-notation image pages with OCR disabled).
    return NotationSource.MIXED, 0.0, [*evidence, "no text signal"]


# --- Per-document record ---------------------------------------------------------------------


def _worst_page(page_findings: list[dict[str, Any]]) -> int | None:
    """Return the page number with the most issues (ties broken by lowest page number)."""
    worst = None
    worst_count = 0
    for finding in page_findings:
        count = len(finding["issues"])
        page_num = finding["page_num"]
        if count > worst_count or (count == worst_count and count > 0 and (
            worst is None or page_num < worst
        )):
            worst = page_num
            worst_count = count
    return worst if worst_count > 0 else None


# --- Vision adjudication ---------------------------------------------------------------------

_BAND_RANK = {QualityBand.GOOD: 3, QualityBand.REVIEW: 2, QualityBand.POOR: 1}


def _cap_band(current: str, cap: str) -> str:
    """Return the worse of ``current`` and ``cap`` (GOOD > REVIEW > POOR); UNKNOWN is untouched."""
    if current not in _BAND_RANK or cap not in _BAND_RANK:
        return current
    return current if _BAND_RANK[current] <= _BAND_RANK[cap] else cap


def adjudicate_with_vision(
    record: dict[str, Any], doc_meta: dict[str, Any] | None, thresholds: dict[str, Any]
) -> None:
    """Override notation source and cap the quality band from the Script 02 vision signal.

    Deterministic OCR/image metrics cannot separate handwritten manuscript from a readable printed
    photocopy, so a confident vision verdict (vision_status == "success") takes precedence: it
    replaces ``notation_source_type`` and prevents a handwritten / low-legibility document from
    scoring as ``good``. Vision fields are echoed onto the record for transparency; when the
    verdict is missing, disabled, or below ``min_confidence`` the deterministic result is kept.
    """
    vcfg = thresholds.get("vision", {}) or {}
    meta = doc_meta or {}
    source = meta.get("vision_notation_source")
    legibility = meta.get("vision_legibility")
    confidence = _num(meta.get("vision_confidence"))

    # Always echo the raw vision signal for downstream transparency.
    record["vision_notation_source"] = source
    record["vision_legibility"] = legibility
    record["vision_confidence"] = meta.get("vision_confidence")
    record["vision_notes"] = meta.get("vision_notes") or ""
    record["vision_applied"] = False

    if not vcfg.get("enabled", True):
        return
    if meta.get("vision_status") != "success" or not source:
        return
    min_conf = float(vcfg.get("min_confidence", 0.5))
    if confidence is None or confidence < min_conf:
        return

    record["vision_applied"] = True

    if vcfg.get("override_notation_source", True):
        record["notation_source_type"] = source
        record["notation_source_confidence"] = round(confidence, 2)
        evidence = list(record.get("notation_source_evidence") or [])
        note = f"vision: {source} ({confidence:.2f})"
        if legibility:
            note += f", legibility={legibility}"
        evidence.append(note)
        record["notation_source_evidence"] = evidence

    # Band caps only apply to documents that produced a scoreable band.
    if record.get("quality_band") not in _BAND_RANK:
        return

    new_issues: list[str] = []
    if source == NotationSource.HANDWRITTEN:
        record["quality_band"] = _cap_band(
            record["quality_band"], vcfg.get("handwritten_caps_band_at", QualityBand.REVIEW)
        )
        new_issues.append(ISSUE_HANDWRITTEN)
    if legibility == "poor":
        record["quality_band"] = _cap_band(
            record["quality_band"], vcfg.get("poor_legibility_caps_band_at", QualityBand.POOR)
        )
        new_issues.append(ISSUE_LOW_LEGIBILITY)
    elif legibility == "fair":
        record["quality_band"] = _cap_band(
            record["quality_band"], vcfg.get("fair_legibility_caps_band_at", QualityBand.REVIEW)
        )

    if new_issues:
        summary = dict(record.get("issue_summary") or {})
        top = list(record.get("top_issues") or [])
        for code in new_issues:
            summary[code] = summary.get(code, 0) + 1
            if code not in top:
                top.append(code)
        record["issue_summary"] = summary
        record["top_issues"] = top
    record["needs_review"] = record["quality_band"] != QualityBand.GOOD


def build_quality_record(
    pdf_path: str,
    pages: list[dict[str, Any]],
    texts_by_page: dict[int, dict[str, Any]],
    doc_meta: dict[str, Any] | None,
    thresholds: dict[str, Any],
    run_id: str,
    apply_vision: bool = True,
) -> dict[str, Any]:
    """Evaluate one document's pages and roll them up into a single quality record."""
    ordered_pages = sorted(pages, key=lambda p: p.get("page_num", 0))
    analyzed_pages = [p for p in ordered_pages if p.get("processing_status") != ProcessingStatus.ERROR]

    page_findings: list[dict[str, Any]] = []
    page_issue_lists: list[list[str]] = []
    aligned_texts: list[dict[str, Any] | None] = []
    for page in analyzed_pages:
        page_num = int(page.get("page_num") or 0)
        text = texts_by_page.get(page_num)
        aligned_texts.append(text)
        issues = evaluate_page(page, text, thresholds)
        page_issue_lists.append(issues)
        if issues:
            page_findings.append({"page_num": page_num, "issues": issues})

    analyzed = len(analyzed_pages)
    record = new_record_envelope(run_id, RECORD_VERSION)
    record["pdf_path"] = pdf_path
    record["piece_id"] = (doc_meta or {}).get("piece_id") or (
        ordered_pages[0].get("piece_id") if ordered_pages else None
    )
    record["piece_folder"] = (doc_meta or {}).get("piece_folder")
    record["pdf_filename"] = (doc_meta or {}).get("pdf_filename") or Path(pdf_path).name
    record["page_count"] = len(ordered_pages)
    record["analyzed_page_count"] = analyzed

    notation_type, notation_conf, notation_evidence = classify_notation_source(
        aligned_texts, analyzed_pages, thresholds
    )
    record["notation_source_type"] = notation_type
    record["notation_source_confidence"] = notation_conf
    record["notation_source_evidence"] = notation_evidence

    if analyzed == 0:
        record["quality_score"] = None
        record["quality_band"] = QualityBand.UNKNOWN
        record["needs_review"] = True
        record["page_issue_count"] = 0
        record["issue_summary"] = {}
        record["top_issues"] = []
        record["worst_page"] = None
        record["page_findings"] = []
        record["metrics"] = {}
        if apply_vision:
            adjudicate_with_vision(record, doc_meta, thresholds)
        return record

    score, band, summary, top_issues, page_issue_count = score_document(
        page_issue_lists, analyzed, thresholds
    )
    record["quality_score"] = score
    record["quality_band"] = band
    record["needs_review"] = band != QualityBand.GOOD
    record["page_issue_count"] = page_issue_count
    record["issue_summary"] = summary
    record["top_issues"] = top_issues
    record["worst_page"] = _worst_page(page_findings)
    record["page_findings"] = page_findings
    record["metrics"] = {
        "median_estimated_dpi": _round(_median([_num(p.get("estimated_dpi")) for p in analyzed_pages])),
        "mean_contrast_std": _round(_mean([_num(p.get("contrast_std")) for p in analyzed_pages])),
        "mean_blur_variance": _round(_mean([_num(p.get("blur_variance")) for p in analyzed_pages])),
        "max_abs_skew_deg": _round(
            max(
                (abs(v) for v in (_fold_skew(_num(p.get("skew_angle_deg"))) for p in analyzed_pages) if v is not None),
                default=None,
            )
            if any(_num(p.get("skew_angle_deg")) is not None for p in analyzed_pages)
            else None
        ),
        "mean_alnum_ratio": _round(_mean([_num(t.get("alnum_ratio")) for t in aligned_texts if t])),
        "mean_ocr_confidence": _round(
            _mean([_num(t.get("ocr_confidence")) for t in aligned_texts if t])
        ),
        "image_based_fraction": _round(
            (sum(1 for p in analyzed_pages if p.get("is_image_based")) / analyzed) if analyzed else 0.0
        ),
        "blank_page_count": sum(1 for issues in page_issue_lists if ISSUE_BLANK_PAGE in issues),
    }
    if apply_vision:
        adjudicate_with_vision(record, doc_meta, thresholds)
    return record


# --- Fingerprints ----------------------------------------------------------------------------


def config_fingerprint(thresholds: dict[str, Any]) -> str:
    """Fingerprint the thresholds so any change invalidates cached per-document results."""
    return sha256_text(json.dumps(thresholds, sort_keys=True))


def document_fingerprint(
    pages: list[dict[str, Any]],
    texts_by_page: dict[int, dict[str, Any]],
    cfg_fp: str,
    doc_meta: dict[str, Any] | None = None,
) -> str:
    """Fingerprint a document from its per-page content/render hashes plus the config fingerprint.

    The Script 02 vision signal is folded in so that enabling vision (which rewrites
    ``documents.jsonl``) invalidates cached quality records without a config change.
    """
    parts = [cfg_fp]
    for page in sorted(pages, key=lambda p: p.get("page_num", 0)):
        page_num = int(page.get("page_num") or 0)
        text = texts_by_page.get(page_num) or {}
        parts.append(
            f"{page_num}:{text.get('page_text_hash') or ''}:"
            f"{page.get('thumbnail_hash') or ''}:{page.get('processing_status') or ''}"
        )
    meta = doc_meta or {}
    parts.append(
        f"vision:{meta.get('vision_status') or ''}:{meta.get('vision_notation_source') or ''}:"
        f"{meta.get('vision_legibility') or ''}:{meta.get('vision_confidence')}"
    )
    return sha256_text("|".join(parts))


# --- Reporting -------------------------------------------------------------------------------


def build_report(records: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    """Render a human-readable Markdown summary of the quality checks."""
    total = len(records)
    out: list[str] = []
    out.append("# Quality Checks Report")
    out.append("")
    out.append(
        f"_Generated {meta['generated_at']} - run `{meta['run_id']}` - mode **{meta['mode']}**_"
    )
    out.append("")

    out.append("| Setting | Value |")
    out.append("| --- | --- |")
    out.append(f"| Pages input | `{meta['pages']}` |")
    out.append(f"| Text input | `{meta['extracted_text']}` |")
    out.append(f"| Thresholds source | {meta['thresholds_source']} |")
    out.append(f"| Vision review | {'enabled' if meta.get('vision_enabled') else 'disabled'} |")
    out.append(f"| Documents scored | {total} |")
    out.append(f"| Output | `{meta['output']}` |")
    out.append("")

    # Quality band distribution.
    band_counts: dict[str, int] = {b: 0 for b in QUALITY_BAND_ORDER}
    for rec in records:
        band = rec.get("quality_band") or QualityBand.UNKNOWN
        band_counts[band] = band_counts.get(band, 0) + 1
    out.append("## Quality Bands")
    out.append("")
    out.append("| Band | Documents | Share |")
    out.append("| --- | ---: | ---: |")
    for band in QUALITY_BAND_ORDER:
        count = band_counts.get(band, 0)
        out.append(f"| {band} | {count} | {pct(count, total):.1f}% |")
    out.append("")

    # Notation-source distribution.
    src_counts: dict[str, int] = {s: 0 for s in NOTATION_SOURCE_ORDER}
    for rec in records:
        src = rec.get("notation_source_type") or NotationSource.MIXED
        src_counts[src] = src_counts.get(src, 0) + 1
    out.append("## Notation Source")
    out.append("")
    out.append("| Source | Documents | Share |")
    out.append("| --- | ---: | ---: |")
    for src in NOTATION_SOURCE_ORDER:
        count = src_counts.get(src, 0)
        out.append(f"| {src} | {count} | {pct(count, total):.1f}% |")
    out.append("")
    vision_applied = sum(1 for rec in records if rec.get("vision_applied"))
    if vision_applied:
        out.append(
            f"_Vision adjudication applied to {vision_applied} of {total} document(s)._"
        )
        out.append("")

    # Top issues across the collection.
    issue_totals: dict[str, int] = {}
    for rec in records:
        for code, count in (rec.get("issue_summary") or {}).items():
            issue_totals[code] = issue_totals.get(code, 0) + int(count)
    out.append("## Issues Across Collection (affected page counts)")
    out.append("")
    out.append("| Issue | Pages affected |")
    out.append("| --- | ---: |")
    for code in ISSUE_ORDER:
        if issue_totals.get(code):
            out.append(f"| {code} | {issue_totals[code]} |")
    if not any(issue_totals.get(c) for c in ISSUE_ORDER):
        out.append("| (none) | 0 |")
    out.append("")

    # Per-document detail, worst quality first.
    limit = int(meta.get("detail_limit") or 200)
    ordered = sorted(
        records,
        key=lambda r: (
            r.get("quality_score") if r.get("quality_score") is not None else -1.0,
            r.get("pdf_path") or "",
        ),
    )
    out.append(f"## Per-Document Detail (worst {min(limit, total)} of {total})")
    out.append("")
    out.append("| Document | Piece | Pages | Score | Band | Notation | Top issues |")
    out.append("| --- | --- | ---: | ---: | --- | --- | --- |")
    for rec in ordered[:limit]:
        top = ", ".join(rec.get("top_issues") or []) or "-"
        score = rec.get("quality_score")
        score_str = f"{score:.1f}" if isinstance(score, (int, float)) else "-"
        out.append(
            f"| {md_cell(rec.get('pdf_filename') or rec.get('pdf_path'))} "
            f"| {md_cell(rec.get('piece_id'))} "
            f"| {rec.get('page_count', 0)} "
            f"| {score_str} "
            f"| {md_cell(rec.get('quality_band'))} "
            f"| {md_cell(rec.get('notation_source_type'))} "
            f"| {md_cell(top)} |"
        )
    out.append("")
    return "\n".join(out)


# --- CLI -------------------------------------------------------------------------------------


@app.command()
def main(
    pages: Path = typer.Option(
        Path("data/pages.jsonl"), help="Script 02 per-page features JSONL input"
    ),
    extracted_text: Path = typer.Option(
        Path("data/extracted_text.jsonl"), help="Script 02 per-page text JSONL input"
    ),
    documents: Path = typer.Option(
        Path("data/documents.jsonl"),
        help="Script 02 per-document rollups (optional; supplies piece_folder/pdf_filename)",
    ),
    config_path: Path = typer.Option(
        Path("config/quality_thresholds.yaml"), "--config",
        help="Quality thresholds YAML (optional)",
    ),
    output: Path = typer.Option(
        Path("data/quality_metrics.jsonl"), help="Per-document quality output JSONL"
    ),
    output_report: Path = typer.Option(
        Path("data/quality_report.md"), help="Markdown summary output"
    ),
    write_report: bool = typer.Option(
        True, "--report/--no-report", help="Write the Markdown summary report"
    ),
    report_detail_limit: int = typer.Option(
        200, help="Max rows in the per-document detail table of the report"
    ),
    mode: str = typer.Option("full", help="Processing mode: full or incremental"),
    use_vision: bool = typer.Option(
        True, "--use-vision/--no-vision",
        help="Honor the Script 02 vision signal (vision_* fields on documents.jsonl): override "
        "notation source and cap the band for handwritten/low-legibility scans",
    ),
    concurrency: int = typer.Option(
        1, "--concurrency", "-j", help="Documents to evaluate in parallel (CPU-light; 1 is fine)"
    ),
    log_level: str = typer.Option("INFO", help="DEBUG, INFO, WARNING, ERROR"),
) -> None:
    """Score scan quality and classify notation source per document."""
    mode = mode.lower().strip()
    if mode not in {"full", "incremental"}:
        raise typer.BadParameter("mode must be 'full' or 'incremental'")

    setup_logging(log_level)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start_time = time.perf_counter()

    pages = pages.resolve()
    output = output.resolve()

    page_records = read_jsonl(pages)
    if not page_records:
        raise typer.BadParameter(f"No page records found at {pages}")

    if use_vision:
        logger.info(
            "Vision adjudication enabled: confident vision_* verdicts on documents.jsonl will "
            "override notation source and cap the quality band."
        )

    thresholds, thresholds_source = load_thresholds(config_path.resolve())

    # Index Script 02 datasets by pdf_path (and by page within a document).
    pages_by_pdf: dict[str, list[dict[str, Any]]] = {}
    for rec in page_records:
        pdf_path = rec.get("pdf_path")
        if pdf_path:
            pages_by_pdf.setdefault(pdf_path, []).append(rec)

    texts_by_pdf: dict[str, dict[int, dict[str, Any]]] = {}
    for rec in read_jsonl(extracted_text.resolve()):
        pdf_path = rec.get("pdf_path")
        if pdf_path is None:
            continue
        page_num = int(rec.get("page_num") or 0)
        texts_by_pdf.setdefault(pdf_path, {})[page_num] = rec

    doc_meta_by_pdf = {
        r["pdf_path"]: r for r in read_jsonl(documents.resolve()) if r.get("pdf_path")
    }

    cfg_fp = config_fingerprint(thresholds)
    ckpt_path = make_checkpoint_path(output, CHECKPOINT_FILENAME)
    checkpoint = load_checkpoint(ckpt_path, RECORD_VERSION, logger)
    previous_records = {r["pdf_path"]: r for r in read_jsonl(output) if r.get("pdf_path")}
    prior_fingerprints = checkpoint.get("fingerprints", {}) if mode == "incremental" else {}

    ordered_pdf_paths = sorted(pages_by_pdf.keys())
    total_docs = len(ordered_pdf_paths)
    logger.info("Evaluating %d document(s) in %s mode.", total_docs, mode)

    fingerprints: dict[str, str] = {}
    rebuilt: list[dict[str, Any]] = []
    reused = 0
    to_process: list[tuple[int, str]] = []
    seen = 0
    for pdf_path in ordered_pdf_paths:
        seen += 1
        doc_pages = pages_by_pdf[pdf_path]
        texts_by_page = texts_by_pdf.get(pdf_path, {})
        fingerprint = document_fingerprint(
            doc_pages, texts_by_page, cfg_fp, doc_meta_by_pdf.get(pdf_path)
        )
        fingerprints[pdf_path] = fingerprint

        prior = previous_records.get(pdf_path)
        if (
            mode == "incremental"
            and prior is not None
            and prior.get("record_version") == RECORD_VERSION
            and prior_fingerprints.get(pdf_path) == fingerprint
        ):
            rebuilt.append(prior)
            reused += 1
            logger.info("[%d/%d] Reusing cached quality record: %s", seen, total_docs, pdf_path)
            continue
        to_process.append((seen, pdf_path))

    def _process(item: tuple[int, str]) -> dict[str, Any]:
        idx, pdf_path = item
        doc_pages = pages_by_pdf[pdf_path]
        texts_by_page = texts_by_pdf.get(pdf_path, {})
        logger.info("[%d/%d] Scoring %s (%d page(s))", idx, total_docs, pdf_path, len(doc_pages))
        try:
            return build_quality_record(
                pdf_path, doc_pages, texts_by_page,
                doc_meta_by_pdf.get(pdf_path), thresholds, run_id,
                apply_vision=use_vision,
            )
        except Exception as exc:
            logger.exception("Unexpected quality-check error on %s", pdf_path)
            rec = new_record_envelope(run_id, RECORD_VERSION)
            rec["processing_status"] = ProcessingStatus.ERROR
            rec["pdf_path"] = pdf_path
            rec["error_detail"] = str(exc)
            rec["quality_band"] = QualityBand.UNKNOWN
            rec["quality_score"] = None
            rec["needs_review"] = True
            return rec

    workers = max(1, concurrency)
    if workers <= 1 or len(to_process) <= 1:
        for item in to_process:
            rebuilt.append(_process(item))
    else:
        logger.info("Scoring %d document(s) with concurrency %d.", len(to_process), workers)
        rebuilt.extend(run_with_progress(to_process, _process, workers))

    rebuilt.sort(key=lambda r: r.get("pdf_path") or "")
    atomic_write_jsonl(output, rebuilt)

    if write_report:
        meta = {
            "generated_at": utc_now_iso(),
            "run_id": run_id,
            "mode": mode,
            "elapsed_seconds": time.perf_counter() - start_time,
            "processed": len(to_process),
            "reused": reused,
            "pages": pages.as_posix(),
            "extracted_text": extracted_text.resolve().as_posix(),
            "thresholds_source": thresholds_source,
            "vision_enabled": use_vision,
            "output": output.as_posix(),
            "detail_limit": report_detail_limit,
        }
        report = build_report(rebuilt, meta)
        atomic_write_text(output_report.resolve(), report)
        logger.info("Wrote Markdown report: %s", output_report.resolve())

    new_checkpoint = build_checkpoint(
        RECORD_VERSION,
        run_id,
        fingerprints,
        pages_input=pages.as_posix(),
        output=output.as_posix(),
        thresholds_source=thresholds_source,
        vision_enabled=use_vision,
        record_count=len(rebuilt),
    )
    atomic_write_json(ckpt_path, new_checkpoint)

    logger.info(
        "Quality checks completed: total=%d processed=%d reused=%d output=%s",
        len(rebuilt),
        len(to_process),
        reused,
        output,
    )


if __name__ == "__main__":
    app()
