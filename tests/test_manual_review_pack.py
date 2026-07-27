"""Tests for Script 08 (prioritized manual review pack).

Fully offline: builds synthetic Script 06 per-piece records on disk (or in memory) and asserts the
deterministic priority scoring, queue ordering / ranking, likely-missing-part projection,
verbatim pass-through of Script 06 advice, ``--limit`` truncation, CSV shape, data-health
accounting, the empty-input error, and end-to-end CLI output.
"""

from __future__ import annotations

import csv
import importlib.util
import io
import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

runner = CliRunner()


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "08_manual_review_pack.py"
    spec = importlib.util.spec_from_file_location("manual_review_pack_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mrp = load_module()


# --- Fixtures --------------------------------------------------------------------------------


def _piece(piece_id: str = "p1", **overrides: Any) -> dict:
    rec = {
        "record_version": "1.1",
        "processing_status": "success",
        "piece_id": piece_id,
        "catalog_number": "001",
        "piece_title_guess": "Test Piece",
        "piece_folder": "001 Test Piece",
        "document_count": 3,
        "lookup_status": "matched",
        "completeness_tier": "complete",
        "completeness_score": 1.0,
        "severity": "ok",
        "needs_review": False,
        "score_missing": False,
        "missing_required_count": 0,
        "unexpected_part_count": 0,
        "worst_quality_band": "good",
        "reason_codes": [],
        "recommended_actions": [],
        "action_items": [],
        "expected_parts": [],
        "documents": [],
        "quality_summary": {
            "band_counts": {"good": 3, "review": 0, "poor": 0, "unknown": 0},
            "low_quality_doc_count": 0,
            "handwritten_doc_count": 0,
        },
        "review_counts": {
            "needs_review_count": 0,
            "low_confidence_count": 0,
            "unmatched_count": 0,
            "duplicate_count": 0,
        },
    }
    rec.update(overrides)
    return rec


def _write_pieces(tmp_path: Path, records: list[dict]) -> Path:
    piece_dir = tmp_path / "piece_reports"
    piece_dir.mkdir()
    for rec in records:
        (piece_dir / f"{rec['piece_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    return piece_dir


# --- Priority scoring ------------------------------------------------------------------------


def test_priority_breakdown_and_score():
    rec = _piece(
        severity="high",
        score_missing=True,
        missing_required_count=2,
        unexpected_part_count=3,
        quality_summary={
            "band_counts": {},
            "low_quality_doc_count": 1,
            "handwritten_doc_count": 1,
        },
        review_counts={
            "needs_review_count": 4,
            "low_confidence_count": 1,
            "unmatched_count": 1,
            "duplicate_count": 1,
        },
    )
    breakdown = mrp._priority_breakdown(rec)
    assert breakdown == {
        "severity_base": 20,
        "missing_score": 40,
        "missing_required_parts": 20,
        "low_quality_docs": 6,
        "handwritten_docs": 4,
        "low_confidence_parts": 5,
        "unmatched_parts": 5,
        "duplicate_parts": 2,
        "unexpected_parts": 3,
    }
    assert sum(breakdown.values()) == 105


def test_needs_review_count_not_double_counted():
    # needs_review_count is high but every scored component is zero => only the severity base.
    rec = _piece(
        severity="review",
        review_counts={
            "needs_review_count": 5,
            "low_confidence_count": 0,
            "unmatched_count": 0,
            "duplicate_count": 0,
        },
    )
    breakdown = mrp._priority_breakdown(rec)
    assert "needs_review" not in breakdown
    assert sum(breakdown.values()) == 8  # severity_base for "review"


# --- Queue ordering --------------------------------------------------------------------------


def test_queue_ordering_and_rank_assignment():
    low = _piece("low", catalog_number="005", severity="ok")
    mid = _piece("mid", catalog_number="010", severity="review", missing_required_count=1)
    high = _piece("high", catalog_number="020", severity="high", score_missing=True)
    queue = mrp.build_queue([low, mid, high], "piece_reports")
    assert [e["piece_id"] for e in queue] == ["high", "mid", "low"]
    assert [e["rank"] for e in queue] == [1, 2, 3]
    assert queue[0]["priority_score"] > queue[1]["priority_score"] > queue[2]["priority_score"]


def test_queue_tie_break_by_piece_sort_key():
    # Same priority (both plain "ok"); catalog number breaks the tie.
    a = _piece("a", catalog_number="200", severity="ok")
    b = _piece("b", catalog_number="100", severity="ok")
    queue = mrp.build_queue([a, b], "piece_reports")
    assert [e["catalog_number"] for e in queue] == ["100", "200"]


# --- Projections / pass-through --------------------------------------------------------------


def test_missing_required_parts_projection():
    rec = _piece(
        expected_parts=[
            {"label": "Percussion", "canonical_instrument": "percussion",
             "section": "percussion", "required": True, "present": False},
            {"label": "Flute I", "canonical_instrument": "flute",
             "section": "flutes", "required": True, "present": True},
            {"label": "Harp", "canonical_instrument": "harp",
             "section": "strings", "required": False, "present": False},
        ]
    )
    missing = mrp._missing_required_parts(rec)
    assert missing == [
        {"label": "Percussion", "canonical_instrument": "percussion", "section": "percussion"}
    ]


def test_queue_entry_passthrough_and_sample_thumbnail():
    rec = _piece(
        recommended_actions=["Source the missing required part(s) listed above."],
        action_items=[
            {"reason_code": "missing_required_parts", "action": "Source it", "targets": ["Perc"]}
        ],
        documents=[
            {"thumbnail_path": "cache/render/p1_p0001_abc.png"},
            {"thumbnail_path": "cache/render/p1_p0002_def.png"},
        ],
    )
    entry = mrp._queue_entry(rec, "piece_reports")
    assert entry["recommended_actions"] == ["Source the missing required part(s) listed above."]
    assert entry["action_items"][0]["targets"] == ["Perc"]
    assert entry["sample_thumbnail"] == "cache/render/p1_p0001_abc.png"
    assert entry["report_path"] == "piece_reports/p1.md"


# --- build_pack ------------------------------------------------------------------------------


def test_pack_record_version_weights_and_counts():
    records = [_piece("p1"), _piece("p2", catalog_number="002")]
    pack = mrp.build_pack(records, "run1", "src", "piece_reports")
    assert pack["record_version"] == "1.0"
    assert pack["piece_count"] == 2
    assert pack["weights"]["missing_required_part"] == 10
    assert pack["weights"]["severity_base"]["high"] == 20
    assert len(pack["queue"]) == 2


def test_limit_truncates_queue_but_not_counts():
    records = [
        _piece("a", catalog_number="005", severity="ok"),
        _piece("b", catalog_number="010", severity="high", score_missing=True),
        _piece("c", catalog_number="020", severity="review", missing_required_count=1),
    ]
    pack = mrp.build_pack(records, "run1", "src", "piece_reports", limit=1)
    assert pack["piece_count"] == 3
    assert len(pack["queue"]) == 1
    assert pack["queue"][0]["piece_id"] == "b"  # highest priority survives the truncation


def test_record_version_distribution_counts_versions():
    records = [_piece("p1"), _piece("p2", record_version="1.0")]
    pack = mrp.build_pack(records, "run1", "src", "piece_reports")
    assert pack["record_version_distribution"] == {"1.0": 1, "1.1": 1}


# --- CSV -------------------------------------------------------------------------------------


def test_queue_csv_shape_and_order():
    records = [
        _piece("a", catalog_number="005", severity="ok"),
        _piece(
            "b",
            catalog_number="010",
            severity="high",
            missing_required_count=1,
            recommended_actions=["Do the thing"],
            expected_parts=[
                {"label": "Percussion", "canonical_instrument": "percussion",
                 "section": "percussion", "required": True, "present": False}
            ],
        ),
    ]
    pack = mrp.build_pack(records, "run1", "src", "piece_reports")
    text = mrp.render_queue_csv(pack["queue"])
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == list(mrp.CSV_COLUMNS)
    # Highest priority (b) comes first.
    assert rows[1][2] == "010"  # catalog_number
    assert rows[1][0] == "1"  # rank
    assert rows[1][9] == "Percussion"  # missing_required_parts
    assert rows[1][15] == "Do the thing"  # next_action


# --- Loading / data health -------------------------------------------------------------------


def test_load_skips_checkpoint_and_non_piece_files(tmp_path: Path):
    piece_dir = _write_pieces(tmp_path, [_piece("p1")])
    (piece_dir / ".piece_report_checkpoint.json").write_text("{}", encoding="utf-8")
    (piece_dir / "stray.json").write_text(json.dumps({"not": "a piece"}), encoding="utf-8")
    loaded = mrp.load_piece_records(piece_dir)
    assert [r["piece_id"] for r in loaded.records] == ["p1"]
    assert loaded.skipped == 1  # stray.json (the checkpoint dotfile is not counted)


def test_pieces_skipped_recorded_and_rendered(tmp_path: Path):
    pack = mrp.build_pack([_piece("p1")], "run1", "src", "piece_reports", skipped=2)
    assert pack["pieces_skipped"] == 2
    md = mrp.render_pack(pack)
    assert "Data health" in md
    assert "2 file(s) skipped" in md


def test_data_health_note_rendered_for_mixed_versions():
    records = [_piece("p1"), _piece("p2", record_version="0.9")]
    pack = mrp.build_pack(records, "run1", "src", "piece_reports")
    md = mrp.render_pack(pack)
    assert "Data health" in md
    assert "0.9" in md


def test_render_pack_has_weights_and_queue_sections():
    pack = mrp.build_pack([_piece("p1", severity="high")], "run1", "src", "piece_reports")
    md = mrp.render_pack(pack)
    assert "## Priority weights" in md
    assert "## Review queue" in md
    assert "## Top pieces (detail)" in md


# --- CLI -------------------------------------------------------------------------------------


def test_cli_end_to_end_writes_all_outputs(tmp_path: Path):
    piece_dir = _write_pieces(
        tmp_path,
        [
            _piece("p1", catalog_number="005", severity="ok"),
            _piece("p2", catalog_number="010", severity="high", score_missing=True),
        ],
    )
    out_dir = tmp_path / "review_pack"
    result = runner.invoke(
        mrp.app,
        [
            "--piece-reports-dir", str(piece_dir),
            "--output-dir", str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    pack = json.loads((out_dir / "manual_review_queue.json").read_text(encoding="utf-8"))
    assert pack["record_version"] == "1.0"
    assert len(pack["queue"]) == 2
    assert pack["queue"][0]["piece_id"] == "p2"  # highest priority ranked first
    assert (out_dir / "review_pack.md").exists()
    assert (out_dir / "manual_review_queue.csv").exists()


def test_cli_no_csv_flag_skips_csv(tmp_path: Path):
    piece_dir = _write_pieces(tmp_path, [_piece("p1")])
    out_dir = tmp_path / "review_pack"
    result = runner.invoke(
        mrp.app,
        [
            "--piece-reports-dir", str(piece_dir),
            "--output-dir", str(out_dir),
            "--no-csv",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "manual_review_queue.json").exists()
    assert not (out_dir / "manual_review_queue.csv").exists()


def test_cli_errors_when_no_piece_reports(tmp_path: Path):
    piece_dir = tmp_path / "piece_reports"
    piece_dir.mkdir()
    out_dir = tmp_path / "review_pack"
    result = runner.invoke(
        mrp.app,
        [
            "--piece-reports-dir", str(piece_dir),
            "--output-dir", str(out_dir),
        ],
    )
    assert result.exit_code != 0
    assert "Run Script 06 first" in result.output
