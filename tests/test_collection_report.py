"""Tests for Script 07 (collection-level report generator).

Fully offline: builds synthetic Script 06 per-piece records on disk and asserts the deterministic
aggregation (totals, distributions, reason-code frequency, top missing instruments, attention
pieces), CSV shape, empty-input error, and end-to-end CLI output.
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
    path = Path(__file__).resolve().parent.parent / "scripts" / "07_collection_report.py"
    spec = importlib.util.spec_from_file_location("collection_report_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cr = load_module()


# --- Fixtures --------------------------------------------------------------------------------


def _piece(piece_id: str = "p1", **overrides: Any) -> dict:
    rec = {
        "record_version": "1.1",
        "processing_status": "ok",
        "piece_id": piece_id,
        "catalog_number": "001",
        "piece_title_guess": "Test Piece",
        "piece_folder": "001 Test Piece",
        "document_count": 3,
        "lookup_status": "matched",
        "completeness_tier": "complete",
        "severity": "ok",
        "needs_review": False,
        "missing_required_count": 0,
        "unexpected_part_count": 0,
        "score_missing": False,
        "worst_quality_band": "good",
        "reason_codes": [],
        "expected_parts": [],
        "quality_summary": {
            "band_counts": {"good": 3, "fair": 0, "poor": 0, "unknown": 0},
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
    d = tmp_path / "piece_reports"
    d.mkdir()
    for rec in records:
        (d / f"{rec['piece_id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    return d


# --- Loading --------------------------------------------------------------------------------


def test_load_skips_checkpoint_and_non_piece_files(tmp_path: Path) -> None:
    d = _write_pieces(tmp_path, [_piece("p1")])
    (d / ".piece_report_checkpoint.json").write_text('{"run_id": "x"}', encoding="utf-8")
    (d / "notes.json").write_text('{"no_piece_id": true}', encoding="utf-8")
    (d / "broken.json").write_text("{not json", encoding="utf-8")
    loaded = cr.load_piece_records(d)
    assert [r["piece_id"] for r in loaded.records] == ["p1"]
    # notes.json (no piece_id) and broken.json (bad JSON) are counted as skipped; checkpoint is not.
    assert loaded.skipped == 2


# --- Totals ---------------------------------------------------------------------------------


def test_totals_count_expected_flags(tmp_path: Path) -> None:
    records = [
        _piece("p1"),
        _piece(
            "p2",
            completeness_tier="incomplete",
            severity="high",
            needs_review=True,
            missing_required_count=2,
            score_missing=True,
            document_count=4,
            quality_summary={
                "band_counts": {"good": 1, "fair": 1, "poor": 2, "unknown": 0},
                "low_quality_doc_count": 2,
                "handwritten_doc_count": 1,
            },
        ),
        _piece("p3", processing_status="error"),
    ]
    summary = cr.build_summary(records, "run1", "src")
    totals = summary["totals"]
    assert totals["pieces"] == 3
    assert totals["pieces_complete"] == 2  # p1 and p3 default to complete
    assert totals["pieces_with_missing_required"] == 1
    assert totals["pieces_missing_score"] == 1
    assert totals["pieces_needs_review"] == 1
    assert totals["pieces_high_severity"] == 1
    assert totals["pieces_with_errors"] == 1
    assert totals["documents"] == 3 + 4 + 3
    assert totals["documents_low_quality"] == 2
    assert totals["documents_handwritten"] == 1


# --- Distributions --------------------------------------------------------------------------


def test_distributions_seeded_and_counted(tmp_path: Path) -> None:
    records = [
        _piece("p1", completeness_tier="complete", lookup_status="matched", severity="ok"),
        _piece("p2", completeness_tier="incomplete", lookup_status="ambiguous", severity="high"),
        _piece("p3", completeness_tier="incomplete", lookup_status="matched", severity="review"),
    ]
    summary = cr.build_summary(records, "run1", "src")
    comp = summary["completeness_distribution"]
    assert comp["complete"] == 1
    assert comp["incomplete"] == 2
    # Every tier from the shared order is present as a key.
    for tier in cr.COMPLETENESS_TIER_ORDER:
        assert tier in comp
    lookup = summary["lookup_status_distribution"]
    assert lookup["matched"] == 2
    assert lookup["ambiguous"] == 1
    sev = summary["severity_distribution"]
    assert sev == {"high": 1, "review": 1, "ok": 1}


def test_quality_band_distribution_sums_documents(tmp_path: Path) -> None:
    records = [
        _piece(
            "p1",
            quality_summary={
                "band_counts": {"good": 2, "fair": 1, "poor": 0, "unknown": 0},
                "low_quality_doc_count": 1,
                "handwritten_doc_count": 0,
            },
        ),
        _piece(
            "p2",
            quality_summary={
                "band_counts": {"good": 1, "fair": 0, "poor": 3, "unknown": 1},
                "low_quality_doc_count": 3,
                "handwritten_doc_count": 0,
            },
        ),
    ]
    summary = cr.build_summary(records, "run1", "src")
    assert summary["quality_band_distribution"] == {
        "good": 3,
        "fair": 1,
        "poor": 3,
        "unknown": 1,
    }


# --- Reason-code frequency ------------------------------------------------------------------


def test_reason_code_frequency_counts_pieces_not_occurrences(tmp_path: Path) -> None:
    records = [
        _piece("p1", reason_codes=["missing_score", "missing_required_parts"]),
        _piece("p2", reason_codes=["missing_score"]),
        _piece("p3", reason_codes=[]),
    ]
    summary = cr.build_summary(records, "run1", "src")
    freq = summary["reason_code_frequency"]
    assert freq["missing_score"] == 2
    assert freq["missing_required_parts"] == 1
    # All codes from the shared order are present.
    for code in cr.REASON_CODE_ORDER:
        assert code in freq


# --- Top missing instruments ----------------------------------------------------------------


def test_top_missing_instruments_aggregates_and_orders(tmp_path: Path) -> None:
    def parts(*specs):
        return [
            {
                "canonical_instrument": inst,
                "section": section,
                "required": required,
                "present": present,
            }
            for inst, section, required, present in specs
        ]

    records = [
        _piece(
            "p1",
            expected_parts=parts(
                ("oboe", "double_reeds", True, False),
                ("flute", "flutes", True, True),
            ),
        ),
        _piece(
            "p2",
            expected_parts=parts(
                ("oboe", "double_reeds", True, False),
                ("bassoon", "double_reeds", True, False),
            ),
        ),
    ]
    summary = cr.build_summary(records, "run1", "src")
    top = summary["top_missing_instruments"]
    # oboe missing in 2 pieces -> first; bassoon in 1; flute present -> absent.
    assert top[0] == {
        "canonical_instrument": "oboe",
        "section": "double_reeds",
        "missing_piece_count": 2,
    }
    assert {"canonical_instrument": "bassoon", "section": "double_reeds", "missing_piece_count": 1} in top
    assert all(r["canonical_instrument"] != "flute" for r in top)


def test_top_missing_instruments_counts_pieces_once(tmp_path: Path) -> None:
    # Two required+absent oboe entries in one piece count as one missing piece.
    records = [
        _piece(
            "p1",
            expected_parts=[
                {"canonical_instrument": "oboe", "section": "dr", "required": True, "present": False},
                {"canonical_instrument": "oboe", "section": "dr", "required": True, "present": False},
            ],
        )
    ]
    summary = cr.build_summary(records, "run1", "src")
    assert summary["top_missing_instruments"] == [
        {"canonical_instrument": "oboe", "section": "dr", "missing_piece_count": 1}
    ]


# --- Attention pieces -----------------------------------------------------------------------


def test_attention_pieces_high_then_review_with_counts(tmp_path: Path) -> None:
    records = [
        _piece("p2", catalog_number="020", severity="high", reason_codes=["missing_score"]),
        _piece(
            "p1",
            catalog_number="010",
            severity="high",
            reason_codes=["missing_required_parts"],
            missing_required_count=3,
            quality_summary={
                "band_counts": {"good": 1, "review": 0, "poor": 1, "unknown": 0},
                "low_quality_doc_count": 1,
                "handwritten_doc_count": 0,
            },
        ),
        _piece("p3", catalog_number="005", severity="review"),
        _piece("p4", catalog_number="004", severity="ok"),
    ]
    summary = cr.build_summary(records, "run1", "src")
    attention = summary["attention_pieces"]
    # High severity first (ordered by catalog), then review; "ok" excluded.
    assert [a["catalog_number"] for a in attention] == ["010", "020", "005"]
    assert [a["severity"] for a in attention] == ["high", "high", "review"]
    # Enriched magnitude counts are carried for downstream sorting.
    assert attention[0]["missing_required_count"] == 3
    assert attention[0]["low_quality_doc_count"] == 1
    assert attention[0]["reason_codes"] == ["missing_required_parts"]


def test_top_missing_sections_aggregates_and_orders(tmp_path: Path) -> None:
    def parts(*specs):
        return [
            {"canonical_instrument": inst, "section": section, "required": req, "present": pres}
            for inst, section, req, pres in specs
        ]

    records = [
        _piece(
            "p1",
            expected_parts=parts(
                ("oboe", "double_reeds", True, False),
                ("bassoon", "double_reeds", True, False),
                ("timpani", "percussion", True, False),
            ),
        ),
        _piece(
            "p2",
            expected_parts=parts(
                ("snare", "percussion", True, False),
                ("flute", "flutes", True, True),
            ),
        ),
    ]
    summary = cr.build_summary(records, "run1", "src")
    sections = summary["top_missing_sections"]
    # percussion missing in 2 pieces -> first; double_reeds counts the piece once despite 2 parts.
    assert sections[0] == {"section": "percussion", "missing_piece_count": 2}
    assert {"section": "double_reeds", "missing_piece_count": 1} in sections
    assert all(r["section"] != "flutes" for r in sections)


# --- Review totals --------------------------------------------------------------------------


def test_review_totals_sum(tmp_path: Path) -> None:
    records = [
        _piece(
            "p1",
            review_counts={
                "needs_review_count": 1,
                "low_confidence_count": 2,
                "unmatched_count": 0,
                "duplicate_count": 1,
            },
        ),
        _piece(
            "p2",
            review_counts={
                "needs_review_count": 3,
                "low_confidence_count": 0,
                "unmatched_count": 4,
                "duplicate_count": 0,
            },
        ),
    ]
    summary = cr.build_summary(records, "run1", "src")
    assert summary["review_totals"] == {
        "needs_review_count": 4,
        "low_confidence_count": 2,
        "unmatched_count": 4,
        "duplicate_count": 1,
    }


# --- CSV ------------------------------------------------------------------------------------


def test_pieces_csv_shape_and_order(tmp_path: Path) -> None:
    records = [
        _piece("p2", catalog_number="020"),
        _piece("p1", catalog_number="010", reason_codes=["missing_score", "missing_required_parts"]),
    ]
    text = cr.render_pieces_csv(cr._pieces_index(records))
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == list(cr.CSV_COLUMNS)
    assert "completeness_score" in cr.CSV_COLUMNS
    # Sorted by catalog number.
    assert rows[1][0] == "010"
    assert rows[2][0] == "020"
    # reason_codes joined with "; ".
    assert rows[1][cr.CSV_COLUMNS.index("reason_codes")] == "missing_score; missing_required_parts"


# --- Schema 1.1 additions -------------------------------------------------------------------


def test_summary_record_version_and_pieces_index(tmp_path: Path) -> None:
    records = [
        _piece("p2", catalog_number="020", severity="high", reason_codes=["missing_score"]),
        _piece("p1", catalog_number="010", missing_required_count=2),
    ]
    summary = cr.build_summary(records, "run1", "src")
    assert summary["record_version"] == "1.2"
    pieces = summary["pieces"]
    # Ordered by catalog number, full structured payload for Script 08.
    assert [p["catalog_number"] for p in pieces] == ["010", "020"]
    p1 = pieces[0]
    assert p1["missing_required_count"] == 2
    assert isinstance(p1["reason_codes"], list)
    assert p1["review_counts"]["needs_review_count"] == 0
    assert pieces[1]["reason_codes"] == ["missing_score"]


def test_record_version_distribution_counts_versions(tmp_path: Path) -> None:
    records = [
        _piece("p1", record_version="1.1"),
        _piece("p2", record_version="1.1"),
        _piece("p3", record_version="1.0"),
    ]
    summary = cr.build_summary(records, "run1", "src")
    assert summary["record_version_distribution"] == {"1.0": 1, "1.1": 2}


def test_completeness_score_summary(tmp_path: Path) -> None:
    records = [
        _piece("p1", completeness_score=0.6),
        _piece("p2", completeness_score=0.8),
        _piece("p3", completeness_score=1.0),
    ]
    summary = cr.build_summary(records, "run1", "src")
    comp = summary["completeness_score_summary"]
    assert comp["count"] == 3
    assert comp["mean"] == 0.8
    assert comp["median"] == 0.8
    assert comp["min"] == 0.6
    assert comp["max"] == 1.0


def test_completeness_score_summary_handles_missing_scores(tmp_path: Path) -> None:
    records = [_piece("p1")]
    records[0].pop("completeness_score", None)
    summary = cr.build_summary(records, "run1", "src")
    assert summary["completeness_score_summary"] == {
        "count": 0,
        "mean": None,
        "median": None,
        "min": None,
        "max": None,
    }


def test_pieces_skipped_recorded(tmp_path: Path) -> None:
    summary = cr.build_summary([_piece("p1")], "run1", "src", skipped=3)
    assert summary["pieces_skipped"] == 3


def test_data_health_note_rendered_for_mixed_versions(tmp_path: Path) -> None:
    records = [_piece("p1", record_version="1.0")]
    summary = cr.build_summary(records, "run1", "src", skipped=1)
    md = cr.render_summary(summary, "piece_reports")
    assert "Data health" in md
    assert "1 file(s) skipped" in md


# --- CLI ------------------------------------------------------------------------------------


def test_cli_end_to_end_writes_all_outputs(tmp_path: Path) -> None:
    piece_dir = _write_pieces(
        tmp_path,
        [
            _piece("p1"),
            _piece("p2", severity="high", reason_codes=["missing_score"], score_missing=True),
        ],
    )
    out_dir = tmp_path / "collection_reports"
    result = runner.invoke(
        cr.app,
        [
            "--piece-reports-dir",
            str(piece_dir),
            "--output-dir",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    summary_json = json.loads((out_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary_json["record_version"] == "1.2"
    assert summary_json["piece_count"] == 2
    assert len(summary_json["pieces"]) == 2
    assert (out_dir / "summary.md").exists()
    assert (out_dir / "pieces.csv").exists()
    md = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "# Collection Report" in md
    assert "Pieces needing attention" in md


def test_cli_no_csv_flag_skips_csv(tmp_path: Path) -> None:
    piece_dir = _write_pieces(tmp_path, [_piece("p1")])
    out_dir = tmp_path / "collection_reports"
    result = runner.invoke(
        cr.app,
        ["--piece-reports-dir", str(piece_dir), "--output-dir", str(out_dir), "--no-csv"],
    )
    assert result.exit_code == 0, result.output
    assert not (out_dir / "pieces.csv").exists()


def test_cli_errors_when_no_piece_reports(tmp_path: Path) -> None:
    empty_dir = tmp_path / "piece_reports"
    empty_dir.mkdir()
    out_dir = tmp_path / "collection_reports"
    result = runner.invoke(
        cr.app,
        ["--piece-reports-dir", str(empty_dir), "--output-dir", str(out_dir)],
    )
    assert result.exit_code != 0
    assert "Run Script 06 first" in result.output
