"""Tests for Script 09 (static-site data builder).

Fully offline: exercises the pure model builders on synthetic pipeline records (id synthesis,
score conversion, cross-referencing, deterministic serialization, phase accounting) and runs the
CLI end-to-end with ``--no-thumbnails`` so no Pillow / image work is required.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

runner = CliRunner()


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "09_static_site.py"
    spec = importlib.util.spec_from_file_location("static_site_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ss = load_module()


# --- fixtures --------------------------------------------------------------------------------


def _report(piece_id: str = "p1", **overrides: Any) -> dict:
    rec = {
        "record_version": "1.1",
        "piece_id": piece_id,
        "catalog_number": "001",
        "piece_title_guess": "Test Piece",
        "piece_folder": "001 Test Piece",
        "document_count": 2,
        "expected_part_count": 3,
        "severity": "review",
        "completeness_score": 0.5,
        "completeness_tier": "incomplete",
        "missing_required_count": 1,
        "expected_parts": [
            {"label": "Flute", "canonical_instrument": "flute", "section": "woodwind", "required": True, "present": True},
            {"label": "Oboe", "canonical_instrument": "oboe", "section": "woodwind", "required": True, "present": False},
            {"label": "Harp", "canonical_instrument": "harp", "section": "strings", "required": False, "present": False},
        ],
        "observed_parts": [
            {"predicted_part": "Flute", "instruments": [{"canonical_instrument": "flute", "section": "woodwind"}]},
        ],
        "has_score": True,
        "score_missing": False,
        "score_types": ["full_score"],
        "work_identity": {
            "title_guess": "Test Piece",
            "catalog_number": "001",
            "identity_candidates": {
                "composer": "Regex Composer",
                "arranger": "Regex Arranger",
                "publisher": "Regex Publisher",
                "copyright_year": 1975,
            },
            "resolved": {
                "composer": "Jane Doe",
                "publisher": "Acme Editions",
                "year": "1981",
                "summary": "A short test work. It exists only in fixtures.",
            },
        },
        "documents": [
            {"pdf_path": "001/flute.pdf", "pdf_filename": "flute.pdf", "predicted_part": "Flute",
             "quality_band": "good", "thumbnail_path": "cache/render/p1_p0001_aaa.png",
             "notation_source_type": "printed_original", "needs_review": False, "is_score": False},
            {"pdf_path": "001/oboe.pdf", "pdf_filename": "oboe.pdf", "predicted_part": "Oboe",
             "quality_band": "poor", "thumbnail_path": "cache/render/p1_p0002_bbb.png",
             "notation_source_type": "handwritten", "needs_review": True, "is_score": False},
            {"pdf_path": "001/score.pdf", "pdf_filename": "score.pdf", "predicted_part": "Full Score",
             "quality_band": "good", "notation_source_type": "printed_original", "needs_review": False,
             "is_score": True, "score_type": "full_score"},
        ],
    }
    rec.update(overrides)
    return rec


def _summary(**overrides: Any) -> dict:
    rec = {
        "record_version": "1.2",
        "piece_count": 1,
        "processing_timestamp": "2025-01-01T00:00:00Z",
        "totals": {"pieces": 1, "documents": 2, "pieces_needs_review": 1},
        "severity_distribution": {"high": 0, "review": 1, "ok": 0},
        "quality_band_distribution": {"good": 1, "review": 0, "poor": 1, "unknown": 0},
        "completeness_distribution": {"complete": 0, "near_complete": 0, "incomplete": 1, "severely_incomplete": 0, "unknown": 0},
        "completeness_score_summary": {"mean": 0.5, "median": 0.5, "min": 0.5, "max": 0.5, "count": 1},
        "top_missing_sections": [{"section": "woodwind", "missing_piece_count": 1}],
        "attention_pieces": [
            {"piece_id": "p1", "catalog_number": "001", "piece_title_guess": "Test Piece",
             "severity": "review", "completeness_score": 0.5, "missing_required_count": 1,
             "low_quality_doc_count": 1, "handwritten_doc_count": 1, "reason_codes": ["missing_required_part"]},
        ],
        "pieces": [
            {"piece_id": "p1", "catalog_number": "001", "piece_title_guess": "Test Piece",
             "severity": "review", "completeness_tier": "incomplete", "completeness_score": 0.5,
             "missing_required_count": 1, "document_count": 2},
        ],
    }
    rec.update(overrides)
    return rec


def _documents_jsonl() -> list[dict]:
    return [
        {"pdf_path": "001/flute.pdf", "pdf_filename": "flute.pdf", "piece_id": "p1", "page_count": 1,
         "vision_notation_source": "printed_original", "vision_legibility": "good",
         "ocr_llm_status": "success", "ocr_llm_confidence": 0.9, "ocr_llm_instruments": ["Flute"],
         "vision_status": "success", "vision_confidence": 0.95, "first_page_text": "Flute part",
         "vision_notes": "clean", "pages_ocr_applied": 0, "processing_timestamp": "2025-01-01T00:00:00Z"},
        {"pdf_path": "001/oboe.pdf", "pdf_filename": "oboe.pdf", "piece_id": "p1", "page_count": 1,
         "vision_notation_source": "handwritten", "vision_legibility": "poor",
         "ocr_llm_status": "success", "ocr_llm_confidence": 0.4, "ocr_llm_instruments": [],
         "vision_status": "success", "vision_confidence": 0.6, "first_page_text": "",
         "vision_notes": "faint", "pages_ocr_applied": 1, "processing_timestamp": "2025-01-01T00:00:00Z"},
    ]


def _pages_jsonl() -> list[dict]:
    return [
        {"pdf_path": "001/flute.pdf", "piece_id": "p1", "page_num": 1, "render_dpi": 150,
         "render_width_px": 1275, "render_height_px": 1650, "orientation": "portrait",
         "thumbnail_path": "cache/render/p1_p0001_aaa.png", "has_staves": True, "staff_line_count": 10},
        {"pdf_path": "001/oboe.pdf", "piece_id": "p1", "page_num": 1, "render_dpi": 150,
         "render_width_px": 1275, "render_height_px": 1650, "orientation": "portrait",
         "thumbnail_path": "cache/render/p1_p0002_bbb.png", "has_staves": True, "staff_line_count": 8},
    ]


# --- id synthesis ----------------------------------------------------------------------------


def test_synth_doc_id_is_stable_and_separator_agnostic():
    a = ss.synth_doc_id("001/flute.pdf")
    b = ss.synth_doc_id("001\\flute.pdf")
    assert a == b
    assert len(a) == 16 and re.fullmatch(r"[0-9a-f]{16}", a)


def test_synth_page_id():
    assert ss.synth_page_id("abc123", 4) == "abc123__p4"


def test_thumb_web_path():
    assert ss.thumb_web_path("cache/render/p1_p0001_aaa.png") == "assets/thumbs/p1_p0001_aaa.webp"
    assert ss.thumb_web_path(None) is None
    assert ss.thumb_web_path("") is None


# --- indexing & builders ---------------------------------------------------------------------


def test_index_reports_builds_cross_references():
    index = ss.index_reports([_report()])
    assert "p1" in index.by_piece_id
    assert "001/oboe.pdf" in index.doc_meta_by_pdf_path
    assert index.section_by_predicted_part.get("Flute") == "woodwind"


def test_build_pieces_projects_missing_required_and_score_pct():
    index = ss.index_reports([_report()])
    pieces = ss.build_pieces(_summary()["pieces"], index, {"p1": 2}, lambda c: ss.thumb_web_path(c))
    assert len(pieces) == 1
    p = pieces[0]
    assert p["completeness_score"] == 50.0
    assert p["page_count"] == 2
    assert p["missing_required"] == [
        {"label": "Oboe", "canonical_instrument": "oboe", "section": "woodwind"}
    ]
    assert p["thumbnail"] == "assets/thumbs/p1_p0001_aaa.webp"


def test_build_pieces_includes_instrumentation_and_score():
    index = ss.index_reports([_report()])
    pieces = ss.build_pieces(_summary()["pieces"], index, {"p1": 2}, lambda c: None)
    p = pieces[0]

    # Full expected-parts grid preserved in canonical order, with present/required flags.
    assert p["has_expected_parts"] is True
    labels = [part["label"] for part in p["instrumentation"]]
    assert labels == ["Flute", "Oboe", "Harp"]

    flute = p["instrumentation"][0]
    assert flute["present"] is True and flute["required"] is True
    # A present part links to its document(s) via the observed-parts canonical mapping.
    assert flute["documents"] == [{"doc_id": ss.synth_doc_id("001/flute.pdf"), "filename": "flute.pdf"}]

    oboe = p["instrumentation"][1]
    assert oboe["present"] is False
    assert oboe["documents"] == []  # missing -> no linked document

    # Score is broken out with its linked document(s).
    assert p["score"]["has_score"] is True
    assert p["score"]["score_missing"] is False
    assert p["score"]["score_types"] == ["full_score"]
    assert p["score"]["documents"] == [
        {
            "doc_id": ss.synth_doc_id("001/score.pdf"),
            "filename": "score.pdf",
            "pdf_path": "001/score.pdf",
            "score_type": "full_score",
        }
    ]


def test_build_pieces_projects_metadata_preferring_resolved():
    index = ss.index_reports([_report()])
    pieces = ss.build_pieces(_summary()["pieces"], index, {"p1": 2}, lambda c: None)
    meta = pieces[0]["metadata"]
    # Resolved (online-lookup) identity wins over the regex identity_candidates.
    assert meta["composer"] == "Jane Doe"
    assert meta["publisher"] == "Acme Editions"
    assert meta["year"] == "1981"
    assert meta["summary"] == "A short test work. It exists only in fixtures."
    # Falls back to identity_candidates when the lookup did not resolve the field.
    assert meta["arranger"] == "Regex Arranger"


def test_build_pieces_metadata_defaults_to_none_without_identity():
    report = _report()
    report.pop("work_identity", None)
    index = ss.index_reports([report])
    pieces = ss.build_pieces(_summary()["pieces"], index, {"p1": 2}, lambda c: None)
    meta = pieces[0]["metadata"]
    assert meta == {
        "composer": None,
        "arranger": None,
        "publisher": None,
        "year": None,
        "summary": None,
    }


def test_build_documents_synthesizes_ids_and_enriches():
    index = ss.index_reports([_report()])
    docs = ss.build_documents(_documents_jsonl(), index, lambda c: None)
    by_file = {d["pdf_filename"]: d for d in docs}
    assert by_file["flute.pdf"]["doc_id"] == ss.synth_doc_id("001/flute.pdf")
    assert by_file["flute.pdf"]["instrument"] == "Flute"
    assert by_file["flute.pdf"]["section"] == "woodwind"
    assert by_file["oboe.pdf"]["quality"] == "poor"
    # score flags projected from the report's per-document metadata
    assert by_file["flute.pdf"]["is_score"] is False
    assert by_file["flute.pdf"]["score_type"] is None
    # thumbnail resolver returned None -> stored None
    assert by_file["flute.pdf"]["thumbnail"] is None


def test_build_documents_instrument_falls_back_to_ocr_when_unclassified():
    index = ss.index_reports([])  # no report -> no predicted_part
    docs = ss.build_documents(_documents_jsonl(), index, lambda c: None)
    by_file = {d["pdf_filename"]: d for d in docs}
    assert by_file["flute.pdf"]["instrument"] == "Flute"  # from ocr_llm_instruments
    assert by_file["oboe.pdf"]["instrument"] is None


def test_build_pages_maps_geometry_and_ids():
    pages = ss.build_pages(_pages_jsonl(), lambda c: ss.thumb_web_path(c))
    flute_doc_id = ss.synth_doc_id("001/flute.pdf")
    p = next(pg for pg in pages if pg["doc_id"] == flute_doc_id)
    assert p["page_id"] == ss.synth_page_id(flute_doc_id, 1)
    assert p["width_px"] == 1275 and p["height_px"] == 1650
    assert p["thumbnail"] == "assets/thumbs/p1_p0001_aaa.webp"


def test_build_dashboard_reason_and_completeness():
    dash = ss.build_dashboard(_summary(), page_count=2, phase_flow=[])
    assert dash["completeness"]["score"] == 50.0
    assert dash["severity"] == {"high": 0, "review": 1, "ok": 0}
    att = dash["attention"][0]
    assert "1 required part missing" in att["reason"]
    assert att["completeness_score"] == 50.0


def test_build_phases_accounting():
    phases = ss.build_phases(
        _summary(), [_report()], _documents_jsonl(), _pages_jsonl(), {"queue": []},
        {"extract": None, "reports": None, "review": None},
    )
    assert set(phases.keys()) == {str(i) for i in range(1, 9)}
    assert phases["1"]["summary"]["records"] == 2  # documents
    assert phases["2"]["summary"]["records"] == 2  # pages
    # expected parts total from report.expected_part_count
    expected_stat = next(s for s in phases["4"]["stats"] if s["label"] == "Expected parts")
    assert expected_stat["value"] == 3
    review_stat = next(s for s in phases["3"]["stats"] if s["label"] == "Flagged for review")
    assert review_stat["value"] == 1  # oboe doc needs_review


# --- serialization ---------------------------------------------------------------------------


def test_render_data_module_registers_and_escapes():
    text = ss.render_data_module("pieces", [{"title": "Se\u00f1or \u2028break"}])
    assert text.startswith("/* Generated")
    assert 'MLG.register("pieces"' in text
    # non-ascii (and the line-separator U+2028) must be escaped for classic <script> safety
    assert "\u2028" not in text
    assert "\\u2028" in text
    assert "\u00f1" not in text


def test_collect_thumbnail_paths_dedups_in_order():
    paths = ss.collect_thumbnail_paths([_report()], _pages_jsonl())
    assert paths == ["cache/render/p1_p0001_aaa.png", "cache/render/p1_p0002_bbb.png"]


# --- end-to-end CLI --------------------------------------------------------------------------


def _write_inputs(data_dir: Path) -> None:
    (data_dir / "collection_reports").mkdir(parents=True)
    (data_dir / "piece_reports").mkdir(parents=True)
    (data_dir / "review_pack").mkdir(parents=True)
    (data_dir / "collection_reports" / "summary.json").write_text(json.dumps(_summary()), encoding="utf-8")
    (data_dir / "piece_reports" / "p1.json").write_text(json.dumps(_report()), encoding="utf-8")
    with (data_dir / "documents.jsonl").open("w", encoding="utf-8") as fh:
        for rec in _documents_jsonl():
            fh.write(json.dumps(rec) + "\n")
    with (data_dir / "pages.jsonl").open("w", encoding="utf-8") as fh:
        for rec in _pages_jsonl():
            fh.write(json.dumps(rec) + "\n")
    (data_dir / "review_pack" / "manual_review_queue.json").write_text(
        json.dumps({"queue": [{"piece_id": "p1", "priority_score": 42}], "processing_timestamp": "2025-01-01T00:00:00Z"}),
        encoding="utf-8",
    )


def test_cli_end_to_end_writes_all_modules(tmp_path: Path):
    data_dir = tmp_path / "data"
    web_dir = tmp_path / "web"
    _write_inputs(data_dir)

    result = runner.invoke(
        ss.app,
        ["--data-dir", str(data_dir), "--web-dir", str(web_dir), "--no-thumbnails"],
    )
    assert result.exit_code == 0, result.output

    out = web_dir / "data"
    for filename, key in ss.DATA_MODULES:
        text = (out / filename).read_text(encoding="utf-8")
        assert f'MLG.register("{key}"' in text

    # manifest counts reflect the synthetic inputs
    manifest_text = (out / "00_manifest.js").read_text(encoding="utf-8")
    payload = json.loads(re.search(r"MLG\.register\(\"manifest\", (.*)\);", manifest_text, re.S).group(1))
    assert payload["counts"] == {"pieces": 1, "documents": 2, "pages": 2, "render_images": 2}
    assert payload["site_schema_version"] == ss.SITE_SCHEMA_VERSION
    assert payload["thumbnails"] is False


def test_cli_errors_without_summary(tmp_path: Path):
    data_dir = tmp_path / "data"
    (data_dir / "collection_reports").mkdir(parents=True)
    result = runner.invoke(
        ss.app,
        ["--data-dir", str(data_dir), "--web-dir", str(tmp_path / "web"), "--no-thumbnails"],
    )
    assert result.exit_code != 0
