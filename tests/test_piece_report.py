"""Tests for Script 06 (per-piece report generator).

Fully offline: builds synthetic upstream records and asserts the deterministic join, reason-code /
severity / needs_review derivation, Markdown rendering, index generation, orphan cleanup, and
incremental reuse.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

runner = CliRunner()


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "06_piece_report.py"
    spec = importlib.util.spec_from_file_location("piece_report_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pr = load_module()


# --- Fixtures --------------------------------------------------------------------------------


def _expected(piece_id: str = "p1", **overrides: Any) -> dict:
    rec = {
        "piece_id": piece_id,
        "piece_folder": "001 Test Piece",
        "catalog_number": "001",
        "piece_title_guess": "Test Piece",
        "has_score": True,
        "ensemble_type": "concert_band",
        "ensemble_display_name": "Concert Band",
        "lookup_status": "matched",
        "identity_match_confidence": 0.9,
        "work_identity": {"resolved": {"title": "Test Piece", "composer": "Anon"}},
        "evidence_sources": [],
        "expected_parts": [
            {
                "canonical_instrument": "flute",
                "part_index": None,
                "label": "Flute",
                "section": "flutes",
                "required": True,
                "present": True,
            },
            {
                "canonical_instrument": "oboe",
                "part_index": None,
                "label": "Oboe",
                "section": "double_reeds",
                "required": True,
                "present": False,
            },
        ],
        "missing_required_parts": ["Oboe"],
        "missing_optional_parts": [],
        "unexpected_parts": [],
        "completeness_score": 0.5,
        "completeness_tier": "incomplete",
        "score_expected": True,
        "score_missing": False,
        "needs_review": True,
    }
    rec.update(overrides)
    return rec


def _observed(piece_id: str = "p1", **overrides: Any) -> dict:
    rec = {
        "piece_id": piece_id,
        "piece_folder": "001 Test Piece",
        "catalog_number": "001",
        "piece_title_guess": "Test Piece",
        "has_score": True,
        "score_types": ["conductor"],
        "distinct_instruments": 2,
        "families": ["woodwind"],
        "sections": ["flutes", "double_reeds"],
        "needs_review_count": 0,
        "unmatched_count": 0,
        "low_confidence_count": 0,
        "duplicate_count": 0,
        "observed_parts": [],
    }
    rec.update(overrides)
    return rec


def _prediction(pdf: str, part: str, piece_id: str = "p1", **overrides: Any) -> dict:
    rec = {
        "piece_id": piece_id,
        "pdf_path": f"001 Test Piece/{pdf}",
        "pdf_filename": pdf,
        "predicted_part": part,
        "confidence_tier": "high",
        "needs_review": False,
        "is_score": False,
        "score_type": None,
        "duplicate_in_piece": False,
        "part_sort_key": "1-001-00-0",
    }
    rec.update(overrides)
    return rec


def _quality(pdf: str, piece_id: str = "p1", **overrides: Any) -> dict:
    rec = {
        "piece_id": piece_id,
        "pdf_path": f"001 Test Piece/{pdf}",
        "pdf_filename": pdf,
        "quality_band": "good",
        "quality_score": 100.0,
        "notation_source_type": "printed_original",
        "top_issues": [],
        "needs_review": False,
    }
    rec.update(overrides)
    return rec


def _inputs(**overrides: Any) -> Any:
    defaults = {
        "piece_id": "p1",
        "expected": _expected(),
        "observed": _observed(),
        "predictions": [_prediction("Flute.pdf", "Flute")],
        "quality": [_quality("Flute.pdf")],
        "documents": [],
        "thumbnails": {},
    }
    defaults.update(overrides)
    return pr.PieceInputs(**defaults)


# --- Identity + join -------------------------------------------------------------------------


def test_identity_precedence_prefers_expected():
    inputs = _inputs(
        expected=_expected(catalog_number="042", piece_title_guess="From Expected"),
        observed=_observed(catalog_number="999", piece_title_guess="From Observed"),
    )
    ident = pr._resolve_identity(inputs)
    assert ident["catalog_number"] == "042"
    assert ident["piece_title_guess"] == "From Expected"


def test_identity_falls_back_to_observed_then_predictions():
    inputs = _inputs(
        expected=None,
        observed=_observed(catalog_number=None, piece_title_guess=None, piece_folder=None),
        predictions=[_prediction("Flute.pdf", "Flute", catalog_number="007",
                                 piece_title_guess="From Pred", piece_folder="007 From Pred")],
    )
    ident = pr._resolve_identity(inputs)
    assert ident["catalog_number"] == "007"
    assert ident["piece_title_guess"] == "From Pred"


def test_document_rows_join_quality_by_pdf_path_and_sort():
    inputs = _inputs(
        predictions=[
            _prediction("Oboe.pdf", "Oboe", part_sort_key="1-002-00-0"),
            _prediction("Flute.pdf", "Flute", part_sort_key="1-001-00-0"),
        ],
        quality=[
            _quality("Oboe.pdf", quality_band="review", notation_source_type="handwritten"),
            _quality("Flute.pdf", quality_band="good"),
        ],
    )
    rows = pr._document_rows(inputs)
    # Sorted by part_sort_key: Flute (001) before Oboe (002).
    assert [r["predicted_part"] for r in rows] == ["Flute", "Oboe"]
    oboe = rows[1]
    assert oboe["quality_band"] == "review"
    assert oboe["notation_source_type"] == "handwritten"


# --- Reason-code / severity / needs_review derivation ----------------------------------------


def test_clean_piece_has_no_reasons_and_ok_severity():
    inputs = _inputs(
        expected=_expected(missing_required_parts=[], needs_review=False,
                           expected_parts=[{"canonical_instrument": "flute", "part_index": None,
                                            "label": "Flute", "section": "flutes",
                                            "required": True, "present": True}]),
        observed=_observed(),
    )
    rec = pr.build_piece_record(inputs, "run1")
    assert rec["reason_codes"] == []
    assert rec["severity"] == pr.Severity.OK
    assert rec["needs_review"] is False


def test_missing_required_part_is_high_severity():
    rec = pr.build_piece_record(_inputs(), "run1")
    assert pr.ReasonCode.MISSING_REQUIRED_PARTS in rec["reason_codes"]
    assert rec["severity"] == pr.Severity.HIGH
    assert rec["needs_review"] is True


def test_missing_score_flagged():
    inputs = _inputs(
        expected=_expected(has_score=False, score_missing=True, missing_required_parts=[]),
        observed=_observed(has_score=False),
    )
    rec = pr.build_piece_record(inputs, "run1")
    assert pr.ReasonCode.MISSING_SCORE in rec["reason_codes"]
    assert rec["severity"] == pr.Severity.HIGH
    assert rec["score_missing"] is True


def test_poor_quality_is_high_severity():
    inputs = _inputs(
        expected=_expected(missing_required_parts=[], needs_review=False),
        quality=[_quality("Flute.pdf", quality_band="poor", quality_score=20.0)],
    )
    rec = pr.build_piece_record(inputs, "run1")
    assert pr.ReasonCode.LOW_QUALITY_SCANS in rec["reason_codes"]
    assert rec["severity"] == pr.Severity.HIGH


def test_review_quality_is_review_severity():
    inputs = _inputs(
        expected=_expected(missing_required_parts=[], needs_review=False),
        quality=[_quality("Flute.pdf", quality_band="review")],
    )
    rec = pr.build_piece_record(inputs, "run1")
    assert pr.ReasonCode.LOW_QUALITY_SCANS in rec["reason_codes"]
    assert rec["severity"] == pr.Severity.REVIEW


def test_handwritten_notation_flagged():
    inputs = _inputs(
        expected=_expected(missing_required_parts=[], needs_review=False),
        quality=[_quality("Flute.pdf", notation_source_type="handwritten")],
    )
    rec = pr.build_piece_record(inputs, "run1")
    assert pr.ReasonCode.HANDWRITTEN_OR_ILLEGIBLE in rec["reason_codes"]


def test_unexpected_and_duplicate_and_lowconf_flagged():
    inputs = _inputs(
        expected=_expected(missing_required_parts=[], needs_review=False,
                           unexpected_parts=[{"predicted_part": "Kazoo"}]),
        observed=_observed(duplicate_count=2, low_confidence_count=1),
    )
    rec = pr.build_piece_record(inputs, "run1")
    assert pr.ReasonCode.UNEXPECTED_PARTS in rec["reason_codes"]
    assert pr.ReasonCode.DUPLICATE_PARTS in rec["reason_codes"]
    assert pr.ReasonCode.LOW_CONFIDENCE_PARTS in rec["reason_codes"]


def test_reason_codes_in_canonical_order():
    inputs = _inputs(
        expected=_expected(score_missing=True, has_score=False,
                           unexpected_parts=[{"predicted_part": "Kazoo"}]),
        observed=_observed(duplicate_count=1),
        quality=[_quality("Flute.pdf", quality_band="poor")],
    )
    rec = pr.build_piece_record(inputs, "run1")
    codes = rec["reason_codes"]
    # Order must follow REASON_CODE_ORDER.
    assert codes == [c for c in pr.REASON_CODE_ORDER if c in codes]
    assert codes[0] == pr.ReasonCode.MISSING_SCORE


def test_unresolved_lookup_flagged_only_when_expected_present():
    with_lookup = _inputs(
        expected=_expected(lookup_status="no_match", missing_required_parts=[], needs_review=False),
    )
    rec = pr.build_piece_record(with_lookup, "run1")
    assert pr.ReasonCode.INSTRUMENTATION_UNRESOLVED in rec["reason_codes"]

    # No expected record at all -> not flagged as unresolved (Script 04 simply not run).
    without = _inputs(expected=None, observed=_observed())
    rec2 = pr.build_piece_record(without, "run1")
    assert pr.ReasonCode.INSTRUMENTATION_UNRESOLVED not in rec2["reason_codes"]
    assert rec2["has_expected_parts"] is False


# --- Rendering -------------------------------------------------------------------------------


def test_render_report_contains_sections_and_actions():
    rec = pr.build_piece_record(_inputs(), "run1")
    md = pr.render_piece_report(rec)
    assert "# 001 - Test Piece" in md
    assert "## Detected Documents & Parts" in md
    assert "## Expected & Missing Parts" in md
    assert "## Recommended Manual Actions" in md
    assert "Source the missing required part(s)" in md


def test_render_thumbnail_link_when_present():
    inputs = _inputs(thumbnails={"001 Test Piece/Flute.pdf": "cache/render/x.png"})
    rec = pr.build_piece_record(inputs, "run1")
    md = pr.render_piece_report(rec)
    assert "(cache/render/x.png)" in md


def test_index_lists_pieces_and_links():
    rec = pr.build_piece_record(_inputs(), "run1")
    meta = {"generated_at": "now", "run_id": "run1", "mode": "full"}
    index = pr.build_index([rec], meta, "piece_reports")
    assert "# Piece Reports" in index
    assert "[report](piece_reports/p1.md)" in index
    assert "high" in index  # severity column


# --- CLI end-to-end (offline) ----------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def _setup_inputs(tmp_path: Path, piece_ids: list[str]) -> dict[str, Path]:
    data = tmp_path / "data"
    data.mkdir()
    paths = {
        "expected": data / "expected_parts.jsonl",
        "observed": data / "observed_parts_by_piece.jsonl",
        "predictions": data / "part_predictions.jsonl",
        "quality": data / "quality_metrics.jsonl",
        "documents": data / "documents.jsonl",
        "pages": data / "pages.jsonl",
    }
    _write_jsonl(paths["expected"], [_expected(pid) for pid in piece_ids])
    _write_jsonl(paths["observed"], [_observed(pid) for pid in piece_ids])
    _write_jsonl(
        paths["predictions"],
        [_prediction("Flute.pdf", "Flute", piece_id=pid) for pid in piece_ids],
    )
    _write_jsonl(paths["quality"], [_quality("Flute.pdf", piece_id=pid) for pid in piece_ids])
    _write_jsonl(paths["documents"], [])
    _write_jsonl(paths["pages"], [])
    return paths


def _run(tmp_path: Path, paths: dict[str, Path], *extra: str):
    out_dir = tmp_path / "data" / "piece_reports"
    out_index = tmp_path / "data" / "piece_reports.md"
    args = [
        "--expected-parts", str(paths["expected"]),
        "--observed-parts", str(paths["observed"]),
        "--part-predictions", str(paths["predictions"]),
        "--quality-metrics", str(paths["quality"]),
        "--documents", str(paths["documents"]),
        "--pages", str(paths["pages"]),
        "--output-dir", str(out_dir),
        "--output-index", str(out_index),
        *extra,
    ]
    result = runner.invoke(pr.app, args)
    return result, out_dir, out_index


def test_cli_writes_reports_and_index(tmp_path: Path):
    paths = _setup_inputs(tmp_path, ["p1", "p2"])
    result, out_dir, out_index = _run(tmp_path, paths)
    assert result.exit_code == 0, result.stdout
    assert (out_dir / "p1.md").exists()
    assert (out_dir / "p1.json").exists()
    assert (out_dir / "p2.md").exists()
    assert out_index.exists()
    rec = json.loads((out_dir / "p1.json").read_text(encoding="utf-8"))
    assert rec["record_version"] == pr.RECORD_VERSION
    assert rec["piece_id"] == "p1"


def test_cli_union_piece_universe(tmp_path: Path):
    # A piece present only in quality_metrics still gets a report.
    paths = _setup_inputs(tmp_path, ["p1"])
    _write_jsonl(paths["quality"], [_quality("Flute.pdf", piece_id="p1"),
                                    _quality("Solo.pdf", piece_id="pQualityOnly")])
    result, out_dir, _ = _run(tmp_path, paths)
    assert result.exit_code == 0, result.stdout
    assert (out_dir / "pQualityOnly.json").exists()


def test_cli_orphan_cleanup(tmp_path: Path):
    paths = _setup_inputs(tmp_path, ["p1", "p2"])
    result, out_dir, _ = _run(tmp_path, paths)
    assert result.exit_code == 0, result.stdout
    assert (out_dir / "p2.md").exists()

    # Re-run with only p1: p2's files must be pruned.
    _write_jsonl(paths["expected"], [_expected("p1")])
    _write_jsonl(paths["observed"], [_observed("p1")])
    _write_jsonl(paths["predictions"], [_prediction("Flute.pdf", "Flute", piece_id="p1")])
    _write_jsonl(paths["quality"], [_quality("Flute.pdf", piece_id="p1")])
    result2, out_dir2, _ = _run(tmp_path, paths)
    assert result2.exit_code == 0, result2.stdout
    assert (out_dir2 / "p1.md").exists()
    assert not (out_dir2 / "p2.md").exists()
    assert not (out_dir2 / "p2.json").exists()


def test_cli_incremental_reuse(tmp_path: Path, monkeypatch):
    paths = _setup_inputs(tmp_path, ["p1", "p2"])
    result, out_dir, _ = _run(tmp_path, paths)
    assert result.exit_code == 0, result.stdout
    ckpt = out_dir / pr.CHECKPOINT_FILENAME
    assert ckpt.exists()

    # Second run in incremental mode with unchanged inputs must reuse the cached records and
    # never re-render them: build_piece_record should not be invoked at all.
    calls = {"n": 0}
    original = pr.build_piece_record

    def counting_build(inputs, run_id):
        calls["n"] += 1
        return original(inputs, run_id)

    monkeypatch.setattr(pr, "build_piece_record", counting_build)
    result2, _, _ = _run(tmp_path, paths, "--mode", "incremental")
    assert result2.exit_code == 0, result2.stdout
    assert calls["n"] == 0


def test_cli_incremental_rebuilds_changed_piece(tmp_path: Path, monkeypatch):
    paths = _setup_inputs(tmp_path, ["p1", "p2"])
    result, _out_dir, _ = _run(tmp_path, paths)
    assert result.exit_code == 0, result.stdout

    # Change only p2's inputs; incremental must rebuild p2 only, reusing p1.
    _write_jsonl(paths["expected"], [_expected("p1"),
                                     _expected("p2", missing_required_parts=["Trombone III"])])

    calls = {"pieces": []}
    original = pr.build_piece_record

    def tracking_build(inputs, run_id):
        calls["pieces"].append(inputs.piece_id)
        return original(inputs, run_id)

    monkeypatch.setattr(pr, "build_piece_record", tracking_build)
    result2, _, _ = _run(tmp_path, paths, "--mode", "incremental")
    assert result2.exit_code == 0, result2.stdout
    assert calls["pieces"] == ["p2"]
