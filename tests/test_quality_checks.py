"""Tests for Script 05 (scan-quality checks + notation-source classification).

These tests are fully offline: they build synthetic Script 02 page/text records and assert the
deterministic scoring, classification, and reporting behavior.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

runner = CliRunner()


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "05_quality_checks.py"
    spec = importlib.util.spec_from_file_location("quality_checks_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


qc = load_module()


# --- Fixtures --------------------------------------------------------------------------------


def _page(page_num: int = 1, pdf_path: str = "band/p1/cornet.pdf", **overrides: Any) -> dict:
    page = {
        "pdf_path": pdf_path,
        "piece_id": "p1",
        "page_num": page_num,
        "render_width_px": 1700,
        "render_height_px": 2200,
        "estimated_dpi": 300,
        "text_density": 0.05,
        "contrast_std": 60.0,
        "blur_variance": 500.0,
        "skew_angle_deg": 0.2,
        "is_blank": False,
        "is_image_based": False,
        "thumbnail_hash": f"sha256:thumb{page_num}",
        "processing_status": "success",
    }
    page.update(overrides)
    return page


def _text(page_num: int = 1, pdf_path: str = "band/p1/cornet.pdf", **overrides: Any) -> dict:
    text = {
        "pdf_path": pdf_path,
        "piece_id": "p1",
        "page_num": page_num,
        "text_is_searchable": True,
        "word_count": 50,
        "alnum_ratio": 0.9,
        "ocr_confidence": 0.95,
        "ocr_word_count": 50,
        "page_text_hash": f"sha256:text{page_num}",
    }
    text.update(overrides)
    return text


def _thr():
    thresholds, _ = qc.load_thresholds(Path("does-not-exist.yaml"))
    return thresholds


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


# --- Config loading --------------------------------------------------------------------------


def test_load_thresholds_builtin_when_missing(tmp_path: Path):
    thresholds, source = qc.load_thresholds(tmp_path / "nope.yaml")
    assert source == "builtin"
    assert thresholds["quality_checks"]["min_estimated_dpi"] == 200
    assert thresholds["notation_source"]["searchable_fraction_printed"] == 0.5


def test_load_thresholds_yaml_override(tmp_path: Path):
    if qc.yaml is None:
        return
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "quality_checks:\n  min_estimated_dpi: 150\n  bands:\n    good_min_score: 90\n",
        encoding="utf-8",
    )
    thresholds, source = qc.load_thresholds(cfg)
    assert source == "yaml"
    assert thresholds["quality_checks"]["min_estimated_dpi"] == 150
    # Overridden nested key is merged, not replaced wholesale.
    assert thresholds["quality_checks"]["bands"]["good_min_score"] == 90
    assert thresholds["quality_checks"]["bands"]["fair_min_score"] == 50
    # Untouched defaults survive.
    assert thresholds["quality_checks"]["max_skew_angle_deg"] == 3.0


# --- evaluate_page ---------------------------------------------------------------------------


def test_evaluate_page_clean_has_no_issues():
    assert qc.evaluate_page(_page(), _text(), _thr()) == []


def test_evaluate_page_low_resolution_by_dpi():
    issues = qc.evaluate_page(_page(estimated_dpi=120, is_image_based=True), _text(), _thr())
    assert qc.ISSUE_LOW_RESOLUTION in issues


def test_evaluate_page_born_digital_low_dpi_image_not_low_resolution():
    # A born-digital page may embed a low-res decorative image (e.g. a logo); its DPI must not
    # penalize the vector page, whose text has no intrinsic resolution limit.
    page = _page(estimated_dpi=97.9, image_count=1, is_image_based=False)
    issues = qc.evaluate_page(page, _text(), _thr())
    assert qc.ISSUE_LOW_RESOLUTION not in issues


def test_evaluate_page_low_resolution_by_dimensions():
    # No estimated_dpi, but a raster page rendered small.
    page = _page(
        estimated_dpi=None, render_width_px=600, render_height_px=800, is_image_based=True
    )
    issues = qc.evaluate_page(page, _text(), _thr())
    assert qc.ISSUE_LOW_RESOLUTION in issues


def test_evaluate_page_born_digital_small_render_not_low_resolution():
    # A vector page's render pixels reflect physical size, not quality; never flag low resolution.
    page = _page(
        estimated_dpi=None, render_width_px=600, render_height_px=800, is_image_based=False
    )
    issues = qc.evaluate_page(page, _text(), _thr())
    assert qc.ISSUE_LOW_RESOLUTION not in issues


def test_evaluate_page_excessive_skew():
    assert qc.ISSUE_EXCESSIVE_SKEW in qc.evaluate_page(_page(skew_angle_deg=-7.5), _text(), _thr())


def test_evaluate_page_rotation_not_flagged_as_skew():
    # Readings near a multiple of 90 are page rotation, not skew, and must not be flagged.
    assert qc.ISSUE_EXCESSIVE_SKEW not in qc.evaluate_page(_page(skew_angle_deg=-90.0), _text(), _thr())
    assert qc.ISSUE_EXCESSIVE_SKEW not in qc.evaluate_page(_page(skew_angle_deg=-88.4), _text(), _thr())


def test_evaluate_page_low_contrast():
    assert qc.ISSUE_LOW_CONTRAST in qc.evaluate_page(_page(contrast_std=10.0), _text(), _thr())


def test_evaluate_page_heavy_blur():
    assert qc.ISSUE_HEAVY_BLUR in qc.evaluate_page(_page(blur_variance=15.0), _text(), _thr())


def test_evaluate_page_blank_by_flag_suppresses_ocr_issue():
    page = _page(is_blank=True)
    text = _text(ocr_confidence=0.10, ocr_word_count=5, word_count=0)
    issues = qc.evaluate_page(page, text, _thr())
    assert qc.ISSUE_BLANK_PAGE in issues
    assert qc.ISSUE_OCR_ILLEGIBLE not in issues


def test_evaluate_page_blank_by_density():
    page = _page(is_blank=False, text_density=0.001)
    text = _text(word_count=0)
    assert qc.ISSUE_BLANK_PAGE in qc.evaluate_page(page, text, _thr())


def test_evaluate_page_noise_page():
    page = _page(text_density=0.8)
    text = _text(word_count=0, ocr_word_count=0)
    assert qc.ISSUE_NOISE_PAGE in qc.evaluate_page(page, text, _thr())


def test_evaluate_page_ocr_illegible_by_confidence():
    text = _text(ocr_confidence=0.20, ocr_word_count=30)
    assert qc.ISSUE_OCR_ILLEGIBLE in qc.evaluate_page(_page(), text, _thr())


def test_evaluate_page_ocr_illegible_by_alnum_proxy():
    # No OCR confidence available; alnum ratio is the interim proxy for OCR'd raster text.
    text = _text(
        text_source="ocr", ocr_confidence=None, ocr_word_count=0, alnum_ratio=0.2, word_count=20
    )
    assert qc.ISSUE_OCR_ILLEGIBLE in qc.evaluate_page(_page(is_image_based=True), text, _thr())


def test_evaluate_page_born_digital_low_alnum_not_illegible():
    # Embedded (born-digital) text is authoritative; low alnum density on a score is not illegible.
    text = _text(
        text_source="embedded", ocr_confidence=None, ocr_word_count=0, alnum_ratio=0.2, word_count=20
    )
    assert qc.ISSUE_OCR_ILLEGIBLE not in qc.evaluate_page(_page(is_image_based=False), text, _thr())


def test_evaluate_page_missing_metrics_are_not_flagged():
    page = _page(
        estimated_dpi=None, render_width_px=None, render_height_px=None,
        contrast_std=None, blur_variance=None, skew_angle_deg=None, text_density=None,
    )
    text = _text(ocr_confidence=None, ocr_word_count=0, alnum_ratio=None, word_count=0)
    assert qc.evaluate_page(page, text, _thr()) == []


def test_evaluate_page_pure_notation_page_not_ocr_flagged():
    # A staff-only page: no words, no OCR words -> should not be flagged illegible.
    text = _text(text_is_searchable=False, word_count=0, ocr_word_count=0, ocr_confidence=None,
                 alnum_ratio=0.0)
    assert qc.ISSUE_OCR_ILLEGIBLE not in qc.evaluate_page(_page(), text, _thr())


# --- score_document --------------------------------------------------------------------------


def test_score_document_clean_is_good():
    score, band, summary, top, count = qc.score_document([[], [], []], 3, _thr())
    assert score == 100.0
    assert band == qc.QualityBand.GOOD
    assert summary == {}
    assert top == []
    assert count == 0


def test_score_document_penalizes_by_fraction():
    # Heavy blur (weight 30) on all pages -> 100 - 30 = 70 -> fair band.
    lists = [[qc.ISSUE_HEAVY_BLUR], [qc.ISSUE_HEAVY_BLUR]]
    score, band, summary, top, count = qc.score_document(lists, 2, _thr())
    assert score == 70.0
    assert band == qc.QualityBand.FAIR
    assert summary[qc.ISSUE_HEAVY_BLUR] == 2
    assert top[0] == qc.ISSUE_HEAVY_BLUR
    assert count == 2


def test_score_document_partial_fraction():
    # Blur on 1 of 4 pages: 30 * 0.25 = 7.5 penalty -> 92.5 (good).
    lists = [[qc.ISSUE_HEAVY_BLUR], [], [], []]
    score, band, _summary, _top, count = qc.score_document(lists, 4, _thr())
    assert score == 92.5
    assert band == qc.QualityBand.GOOD
    assert count == 1


def test_score_document_poor_band():
    # Two heavy issues on every page drive the score below the fair cutoff.
    lists = [[qc.ISSUE_BLANK_PAGE, qc.ISSUE_NOISE_PAGE]] * 3
    score, band, _s, _t, _c = qc.score_document(lists, 3, _thr())
    assert score <= 50
    assert band == qc.QualityBand.POOR


# --- classify_notation_source ----------------------------------------------------------------


def test_classify_printed_by_searchable_fraction():
    texts = [_text(text_is_searchable=True) for _ in range(4)]
    pages = [_page(page_num=i) for i in range(4)]
    src, conf, evidence = qc.classify_notation_source(texts, pages, _thr())
    assert src == qc.NotationSource.PRINTED
    assert conf > 0.5
    assert any("searchable_fraction" in e for e in evidence)


def test_classify_handwritten_by_low_ocr():
    texts = [
        _text(text_is_searchable=False, ocr_confidence=0.30, ocr_word_count=20, alnum_ratio=0.3)
        for _ in range(3)
    ]
    pages = [_page(page_num=i, is_image_based=True) for i in range(3)]
    src, conf, _evidence = qc.classify_notation_source(texts, pages, _thr())
    assert src == qc.NotationSource.HANDWRITTEN
    assert conf >= _thr()["notation_source"]["min_confidence"]


def test_classify_image_scan_without_text_layer_is_uncertain():
    # A plain image scan (no embedded words -> no alnum signal) with low OCR confidence must NOT
    # be called handwritten: a photocopied printed part looks identical here.
    texts = [
        _text(text_is_searchable=False, ocr_confidence=0.38, ocr_word_count=200,
              alnum_ratio=0.0, word_count=0)
        for _ in range(3)
    ]
    pages = [_page(page_num=i, is_image_based=True) for i in range(3)]
    src, _conf, _evidence = qc.classify_notation_source(texts, pages, _thr())
    assert src == qc.NotationSource.MIXED


def test_classify_printed_by_high_ocr():
    texts = [
        _text(text_is_searchable=False, ocr_confidence=0.90, ocr_word_count=40, alnum_ratio=0.85)
        for _ in range(3)
    ]
    pages = [_page(page_num=i, is_image_based=True) for i in range(3)]
    src, _conf, _evidence = qc.classify_notation_source(texts, pages, _thr())
    assert src == qc.NotationSource.PRINTED


def test_classify_mixed_when_no_signal():
    texts = [_text(text_is_searchable=False, ocr_confidence=None, ocr_word_count=0,
                   alnum_ratio=None, word_count=0)]
    pages = [_page(is_image_based=True)]
    src, conf, evidence = qc.classify_notation_source(texts, pages, _thr())
    assert src == qc.NotationSource.MIXED
    assert conf == 0.0
    assert "no text signal" in evidence


# --- build_quality_record --------------------------------------------------------------------


def test_build_quality_record_good_document():
    pages = [_page(page_num=1), _page(page_num=2)]
    texts = {1: _text(1), 2: _text(2)}
    rec = qc.build_quality_record("band/p1/cornet.pdf", pages, texts, None, _thr(), "run1")
    assert rec["quality_band"] == qc.QualityBand.GOOD
    assert rec["quality_score"] == 100.0
    assert rec["page_count"] == 2
    assert rec["analyzed_page_count"] == 2
    assert rec["notation_source_type"] == qc.NotationSource.PRINTED
    assert rec["pdf_filename"] == "cornet.pdf"


def test_build_quality_record_flags_issues_and_worst_page():
    pages = [_page(page_num=1), _page(page_num=2, blur_variance=10.0, contrast_std=5.0)]
    texts = {1: _text(1), 2: _text(2)}
    rec = qc.build_quality_record("band/p1/cornet.pdf", pages, texts, None, _thr(), "run1")
    assert rec["worst_page"] == 2
    assert qc.ISSUE_HEAVY_BLUR in rec["issue_summary"]
    assert rec["page_issue_count"] == 1


def test_build_quality_record_uses_doc_meta():
    pages = [_page(page_num=1)]
    texts = {1: _text(1)}
    doc_meta = {"piece_id": "p1", "piece_folder": "band/p1", "pdf_filename": "Cornet 1.pdf"}
    rec = qc.build_quality_record("band/p1/cornet.pdf", pages, texts, doc_meta, _thr(), "run1")
    assert rec["piece_folder"] == "band/p1"
    assert rec["pdf_filename"] == "Cornet 1.pdf"


def test_build_quality_record_no_analyzable_pages_is_unknown():
    pages = [_page(page_num=1, processing_status="error")]
    rec = qc.build_quality_record("band/p1/cornet.pdf", pages, {}, None, _thr(), "run1")
    assert rec["quality_band"] == qc.QualityBand.UNKNOWN
    assert rec["quality_score"] is None
    assert rec["analyzed_page_count"] == 0


# --- vision adjudication ---------------------------------------------------------------------


def _vision_meta(**overrides: Any) -> dict:
    meta = {
        "piece_id": "p1",
        "vision_status": "success",
        "vision_notation_source": "handwritten",
        "vision_legibility": "good",
        "vision_confidence": 0.85,
        "vision_notes": "manuscript",
    }
    meta.update(overrides)
    return meta


def test_vision_handwritten_flags_but_does_not_cap_band():
    # A clean, readable hand-copied part is flagged as handwritten but keeps its GOOD band.
    pages = [_page(page_num=1)]
    texts = {1: _text(1)}
    rec = qc.build_quality_record(
        "band/p1/eb_horn.pdf", pages, texts, _vision_meta(), _thr(), "run1"
    )
    assert rec["vision_applied"] is True
    assert rec["notation_source_type"] == qc.NotationSource.HANDWRITTEN
    assert rec["quality_band"] == qc.QualityBand.GOOD
    assert qc.ISSUE_HANDWRITTEN in rec["top_issues"]


def test_vision_fair_legibility_caps_band_at_fair():
    # Genuine readability degradation caps a GOOD-scoring document at FAIR.
    pages = [_page(page_num=1)]
    texts = {1: _text(1)}
    rec = qc.build_quality_record(
        "band/p1/eb_horn.pdf", pages, texts,
        _vision_meta(vision_notation_source="printed_original", vision_legibility="fair"),
        _thr(), "run1",
    )
    assert rec["vision_applied"] is True
    assert rec["quality_band"] == qc.QualityBand.FAIR


def test_vision_poor_legibility_caps_band_at_poor():
    pages = [_page(page_num=1)]
    texts = {1: _text(1)}
    rec = qc.build_quality_record(
        "band/p1/eb_horn.pdf", pages, texts,
        _vision_meta(vision_legibility="poor"), _thr(), "run1",
    )
    assert rec["quality_band"] == qc.QualityBand.POOR
    assert qc.ISSUE_LOW_LEGIBILITY in rec["top_issues"]


def test_vision_low_confidence_is_ignored():
    pages = [_page(page_num=1)]
    texts = {1: _text(1)}
    rec = qc.build_quality_record(
        "band/p1/eb_horn.pdf", pages, texts,
        _vision_meta(vision_confidence=0.2), _thr(), "run1",
    )
    assert rec["vision_applied"] is False
    assert rec["quality_band"] == qc.QualityBand.GOOD
    assert rec["notation_source_type"] == qc.NotationSource.PRINTED


def test_vision_disabled_flag_skips_adjudication():
    pages = [_page(page_num=1)]
    texts = {1: _text(1)}
    rec = qc.build_quality_record(
        "band/p1/eb_horn.pdf", pages, texts, _vision_meta(), _thr(), "run1",
        apply_vision=False,
    )
    assert rec.get("vision_applied") is None
    assert rec["quality_band"] == qc.QualityBand.GOOD


def test_vision_disabled_in_config_skips_adjudication():
    thr = _thr()
    thr["vision"]["enabled"] = False
    pages = [_page(page_num=1)]
    texts = {1: _text(1)}
    rec = qc.build_quality_record(
        "band/p1/eb_horn.pdf", pages, texts, _vision_meta(), thr, "run1"
    )
    assert rec["vision_applied"] is False
    assert rec["quality_band"] == qc.QualityBand.GOOD


def test_vision_printed_verdict_does_not_cap_band():
    pages = [_page(page_num=1)]
    texts = {1: _text(1)}
    rec = qc.build_quality_record(
        "band/p1/cornet.pdf", pages, texts,
        _vision_meta(vision_notation_source="printed_original", vision_legibility="good"),
        _thr(), "run1",
    )
    assert rec["vision_applied"] is True
    assert rec["notation_source_type"] == qc.NotationSource.PRINTED
    assert rec["quality_band"] == qc.QualityBand.GOOD


# --- fingerprints ----------------------------------------------------------------------------


def test_document_fingerprint_changes_with_content():
    pages = [_page(page_num=1)]
    texts = {1: _text(1)}
    fp1 = qc.document_fingerprint(pages, texts, "cfg")
    fp2 = qc.document_fingerprint(pages, {1: _text(1, page_text_hash="sha256:different")}, "cfg")
    assert fp1 != fp2


def test_document_fingerprint_changes_with_vision_signal():
    pages = [_page(page_num=1)]
    texts = {1: _text(1)}
    fp_none = qc.document_fingerprint(pages, texts, "cfg", None)
    fp_vision = qc.document_fingerprint(pages, texts, "cfg", _vision_meta())
    assert fp_none != fp_vision



def test_config_fingerprint_changes_with_thresholds():
    thr_a = _thr()
    thr_b = _thr()
    thr_b["quality_checks"]["min_estimated_dpi"] = 999
    assert qc.config_fingerprint(thr_a) != qc.config_fingerprint(thr_b)


# --- report ----------------------------------------------------------------------------------


def _meta(**overrides: Any) -> dict:
    meta = {
        "generated_at": "2026-01-01T00:00:00Z",
        "run_id": "run1",
        "mode": "full",
        "pages": "data/pages.jsonl",
        "extracted_text": "data/extracted_text.jsonl",
        "thresholds_source": "builtin",
        "vision_enabled": False,
        "output": "data/quality_metrics.jsonl",
        "detail_limit": 200,
    }
    meta.update(overrides)
    return meta


def test_build_report_smoke():
    pages = [_page(page_num=1, blur_variance=10.0)]
    texts = {1: _text(1)}
    rec = qc.build_quality_record("band/p1/cornet.pdf", pages, texts, None, _thr(), "run1")
    report = qc.build_report([rec], _meta())
    assert "# Quality Checks Report" in report
    assert "Quality Bands" in report
    assert "Notation Source" in report
    assert "heavy_blur" in report


# --- end to end ------------------------------------------------------------------------------


def _run_cli(tmp_path: Path, pages: list[dict], texts: list[dict], docs: list[dict], extra=None):
    pages_path = tmp_path / "pages.jsonl"
    text_path = tmp_path / "extracted_text.jsonl"
    docs_path = tmp_path / "documents.jsonl"
    out_path = tmp_path / "quality_metrics.jsonl"
    report_path = tmp_path / "quality_report.md"
    _write_jsonl(pages_path, pages)
    _write_jsonl(text_path, texts)
    _write_jsonl(docs_path, docs)
    args = [
        "--pages", str(pages_path),
        "--extracted-text", str(text_path),
        "--documents", str(docs_path),
        "--output", str(out_path),
        "--output-report", str(report_path),
        "--config", str(tmp_path / "no_config.yaml"),
    ]
    if extra:
        args += extra
    result = runner.invoke(qc.app, args)
    return result, out_path, report_path


def test_e2e_full_run(tmp_path: Path):
    pages = [
        _page(page_num=1, pdf_path="band/p1/cornet.pdf"),
        _page(page_num=2, pdf_path="band/p1/cornet.pdf", blur_variance=10.0),
        _page(page_num=1, pdf_path="band/p1/flute.pdf", contrast_std=5.0),
    ]
    texts = [
        _text(page_num=1, pdf_path="band/p1/cornet.pdf"),
        _text(page_num=2, pdf_path="band/p1/cornet.pdf"),
        _text(page_num=1, pdf_path="band/p1/flute.pdf"),
    ]
    docs = [
        {"pdf_path": "band/p1/cornet.pdf", "piece_id": "p1", "piece_folder": "band/p1",
         "pdf_filename": "cornet.pdf"},
        {"pdf_path": "band/p1/flute.pdf", "piece_id": "p1", "piece_folder": "band/p1",
         "pdf_filename": "flute.pdf"},
    ]
    result, out_path, report_path = _run_cli(tmp_path, pages, texts, docs)
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
    assert len(records) == 2
    by_pdf = {r["pdf_path"]: r for r in records}
    assert by_pdf["band/p1/cornet.pdf"]["page_count"] == 2
    assert qc.ISSUE_HEAVY_BLUR in by_pdf["band/p1/cornet.pdf"]["issue_summary"]
    assert qc.ISSUE_LOW_CONTRAST in by_pdf["band/p1/flute.pdf"]["issue_summary"]
    assert report_path.exists()
    assert "Quality Checks Report" in report_path.read_text()
    assert (out_path.parent / ".quality_checks_checkpoint.json").exists()


def test_e2e_incremental_reuse(tmp_path: Path):
    pages = [_page(page_num=1, pdf_path="band/p1/cornet.pdf")]
    texts = [_text(page_num=1, pdf_path="band/p1/cornet.pdf")]
    docs = [{"pdf_path": "band/p1/cornet.pdf", "piece_id": "p1"}]

    r1, out_path, _ = _run_cli(tmp_path, pages, texts, docs)
    assert r1.exit_code == 0, r1.output
    first = next(json.loads(x) for x in out_path.read_text().splitlines() if x.strip())

    r2, out_path2, _ = _run_cli(tmp_path, pages, texts, docs, extra=["--mode", "incremental"])
    assert r2.exit_code == 0, r2.output
    second = next(json.loads(x) for x in out_path2.read_text().splitlines() if x.strip())
    # Unchanged document is reused verbatim (same run_id carried over from the first run).
    assert second["run_id"] == first["run_id"]


def test_e2e_missing_pages_input_errors(tmp_path: Path):
    result = runner.invoke(
        qc.app,
        ["--pages", str(tmp_path / "missing.jsonl"),
         "--extracted-text", str(tmp_path / "missing_text.jsonl")],
    )
    assert result.exit_code != 0
