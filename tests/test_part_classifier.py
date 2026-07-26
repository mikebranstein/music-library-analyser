from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from scripts._common import read_json, read_jsonl

runner = CliRunner()


def load_module(script_name: str, module_name: str):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load {script_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


classifier = load_module("03_part_classifier.py", "part_classifier_under_test")


def _lexicon_and_compiled():
    lexicon, _ = classifier.load_lexicon(Path("does/not/exist.yaml"))
    return lexicon, classifier.compile_aliases(lexicon)


def _classify(inv, doc=None, page1=None):
    lexicon, compiled = _lexicon_and_compiled()
    section_map = classifier.build_section_map(lexicon)
    return classifier.classify_document(
        inv, doc, page1, lexicon, compiled, section_map, "run1"
    )


# --- Unit tests -----------------------------------------------------------------------------


def test_isolate_part_segment_strips_folder_prefix():
    seg = classifier.isolate_part_segment(
        "241 Chick Corea Ole - Cornet 1.pdf", "241 Chick Corea Ole"
    )
    assert seg == "Cornet 1"


def test_isolate_part_segment_dash_inside_part():
    seg = classifier.isolate_part_segment(
        "241 Chick Corea Ole - Basses - Tuba.pdf", "241 Chick Corea Ole"
    )
    assert seg == "Basses - Tuba"


def test_isolate_part_segment_underscore_separator():
    seg = classifier.isolate_part_segment(
        "681 A Night On A Lonely Moor_Horn in F 1.pdf", "681 A Night On A Lonely Moor"
    )
    assert seg == "Horn in F 1"


def test_longest_alias_wins_bass_trombone():
    _, compiled = _lexicon_and_compiled()
    canonical, alias, _ = classifier.match_instrument(
        classifier.normalize("Bass Trombone"), compiled
    )
    assert canonical == "bass_trombone"
    assert alias == "bass trombone"


def test_plain_trombone_not_bass():
    _, compiled = _lexicon_and_compiled()
    canonical, _, _ = classifier.match_instrument(
        classifier.normalize("Trombone 3 (Bass)"), compiled
    )
    assert canonical == "trombone"


def test_short_alias_does_not_match_inside_word():
    _, compiled = _lexicon_and_compiled()
    # "cl" must not match inside "clarinet"; "clarinet" should win.
    canonical, alias, _ = classifier.match_instrument(
        classifier.normalize("Clarinet 2"), compiled
    )
    assert canonical == "clarinet"
    assert alias == "clarinet"


def test_reversed_word_order_eb_clarinets():
    # This library names files with the qualifier AFTER "Clarinet" (e.g. "Clarinet Eb"),
    # so the taxonomy must classify these distinct instruments, not fall back to plain clarinet.
    _, compiled = _lexicon_and_compiled()
    eb, _, _ = classifier.match_instrument(classifier.normalize("Clarinet Eb"), compiled)
    assert eb == "eb_clarinet"
    alto, _, _ = classifier.match_instrument(classifier.normalize("Clarinet Eb Alto"), compiled)
    assert alto == "alto_clarinet"
    # A plain Bb clarinet part is unaffected.
    plain, _, _ = classifier.match_instrument(classifier.normalize("Clarinet 3"), compiled)
    assert plain == "clarinet"


def test_clef_and_transposition_and_index():
    lexicon, _ = _lexicon_and_compiled()
    assert classifier.extract_clef(classifier.normalize("Baritone (BC)"), lexicon) == "bass"
    assert classifier.extract_clef(classifier.normalize("Baritone (TC)"), lexicon) == "treble"
    assert classifier.extract_transposition(
        classifier.normalize("Horn in F 2"), lexicon
    ) == "F"
    assert classifier.extract_part_index(classifier.normalize("Cornet 3")) == 3


def test_detect_score():
    lexicon, _ = _lexicon_and_compiled()
    assert classifier.detect_score(classifier.normalize("Full Score"), lexicon) == "full"
    assert classifier.detect_score(classifier.normalize("Conductor"), lexicon) == "conductor"
    assert classifier.detect_score(classifier.normalize("Trumpet 1"), lexicon) is None


def test_compose_label_variants():
    assert classifier.compose_label("horn", "F", 2, None, False, None) == "Horn in F 2"
    assert classifier.compose_label("baritone_horn", None, None, "bass", False, None) == (
        "Baritone (BC)"
    )
    assert classifier.compose_label("cornet", None, 1, None, False, None) == "Cornet 1"
    assert classifier.compose_label(None, None, None, None, True, "full") == "Full Score"


def test_classify_filename_only_confidence():
    inv = {
        "pdf_path": "P/Song - Cornet 2.pdf",
        "pdf_filename": "Song - Cornet 2.pdf",
        "piece_folder": "Song",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    rec = _classify(inv)
    assert rec["canonical_instrument"] == "cornet"
    assert rec["part_index"] == 2
    assert rec["evidence_source"] == "filename"
    assert rec["confidence"] == 0.75


def test_classify_combined_confidence_with_text():
    inv = {
        "pdf_path": "P/Song - Trumpet 1.pdf",
        "pdf_filename": "Song - Trumpet 1.pdf",
        "piece_folder": "Song",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    doc = {"first_page_header_candidates": ["Trumpet"], "first_page_text": "Trumpet in Bb"}
    rec = _classify(inv, doc)
    assert rec["canonical_instrument"] == "trumpet"
    assert rec["evidence_source"] == "combined"
    assert rec["confidence"] >= 0.90


def test_text_only_recovery():
    inv = {
        "pdf_path": "P/scan001.pdf",
        "pdf_filename": "scan001.pdf",
        "piece_folder": "P",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    doc = {"first_page_header_candidates": ["Flute"], "first_page_text": "Flute solo"}
    rec = _classify(inv, doc)
    assert rec["canonical_instrument"] == "flute"
    assert rec["evidence_source"] == "text"
    assert rec["confidence"] == 0.50


def test_no_match_is_unknown():
    inv = {
        "pdf_path": "P/mystery.pdf",
        "pdf_filename": "mystery.pdf",
        "piece_folder": "P",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    rec = _classify(inv)
    assert rec["canonical_instrument"] is None
    assert rec["family"] == "unknown"
    assert rec["evidence_source"] == "none"


def test_apply_ensemble_flags_duplicates():
    records = [
        {"piece_id": "p", "canonical_instrument": "trumpet", "part_index": 1,
         "clef": None, "is_score": False, "duplicate_in_piece": False},
        {"piece_id": "p", "canonical_instrument": "trumpet", "part_index": 1,
         "clef": None, "is_score": False, "duplicate_in_piece": False},
        {"piece_id": "p", "canonical_instrument": "flute", "part_index": 1,
         "clef": None, "is_score": False, "duplicate_in_piece": False},
    ]
    classifier.apply_ensemble(records)
    assert records[0]["duplicate_in_piece"] is True
    assert records[1]["duplicate_in_piece"] is True
    assert records[2]["duplicate_in_piece"] is False
    # Duplicates are also flagged for review.
    assert records[0]["needs_review"] is True
    assert records[1]["needs_review"] is True


# --- v1.1 field tests -----------------------------------------------------------------------


def test_confidence_tier_bands():
    assert classifier.confidence_tier(0.95) == "high"
    assert classifier.confidence_tier(0.90) == "high"
    assert classifier.confidence_tier(0.80) == "medium"
    assert classifier.confidence_tier(0.75) == "medium"
    assert classifier.confidence_tier(0.50) == "low"
    assert classifier.confidence_tier(0.0) == "none"


def test_needs_review_and_tier_on_records():
    # Filename-only match at the threshold is trusted (no review).
    filename_rec = _classify(
        {"pdf_path": "P/Song - Cornet 2.pdf", "pdf_filename": "Song - Cornet 2.pdf",
         "piece_folder": "Song", "piece_id": "p", "file_fingerprint": "f"}
    )
    assert filename_rec["confidence_tier"] == "medium"
    assert filename_rec["needs_review"] is False

    # Text-only recovery is low confidence -> review.
    text_rec = _classify(
        {"pdf_path": "P/scan.pdf", "pdf_filename": "scan.pdf",
         "piece_folder": "P", "piece_id": "p", "file_fingerprint": "f"},
        {"first_page_header_candidates": ["Flute"], "first_page_text": "Flute solo"},
    )
    assert text_rec["confidence_tier"] == "low"
    assert text_rec["needs_review"] is True

    # No match at all -> review.
    unknown_rec = _classify(
        {"pdf_path": "P/mystery.pdf", "pdf_filename": "mystery.pdf",
         "piece_folder": "P", "piece_id": "p", "file_fingerprint": "f"}
    )
    assert unknown_rec["confidence_tier"] == "none"
    assert unknown_rec["needs_review"] is True


def test_parse_piece_identity():
    assert classifier.parse_piece_identity("241 Chick Corea Ole", "x.pdf") == (
        "241", "Chick Corea Ole"
    )
    assert classifier.parse_piece_identity("681 A Night On A Lonely Moor", "x.pdf") == (
        "681", "A Night On A Lonely Moor"
    )
    # No catalog number -> title only.
    assert classifier.parse_piece_identity("Some Folder", "x.pdf") == (None, "Some Folder")


def test_catalog_number_on_record():
    rec = _classify(
        {"pdf_path": "241 Chick Corea Ole/241 Chick Corea Ole - Cornet 1.pdf",
         "pdf_filename": "241 Chick Corea Ole - Cornet 1.pdf",
         "piece_folder": "241 Chick Corea Ole", "piece_id": "p", "file_fingerprint": "f"}
    )
    assert rec["catalog_number"] == "241"
    assert rec["piece_title_guess"] == "Chick Corea Ole"


def test_section_assignment():
    cornet = _classify(
        {"pdf_path": "P/Song - Cornet 1.pdf", "pdf_filename": "Song - Cornet 1.pdf",
         "piece_folder": "Song", "piece_id": "p", "file_fingerprint": "f"}
    )
    assert cornet["section"] == "cornets_trumpets"
    tuba = _classify(
        {"pdf_path": "P/Song - Tuba.pdf", "pdf_filename": "Song - Tuba.pdf",
         "piece_folder": "Song", "piece_id": "p", "file_fingerprint": "f"}
    )
    assert tuba["section"] == "tubas"
    # Score and unmatched fall back to score/unknown.
    score = _classify(
        {"pdf_path": "P/Song - Full Score.pdf", "pdf_filename": "Song - Full Score.pdf",
         "piece_folder": "Song", "piece_id": "p", "file_fingerprint": "f"}
    )
    assert score["section"] == "score"


def test_part_sort_key_orders_scores_first_and_by_index():
    score_key = classifier.compute_part_sort_key(None, None, None, True, "full")
    cornet1 = classifier.compute_part_sort_key("cornet", 1, None, False, None)
    cornet2 = classifier.compute_part_sort_key("cornet", 2, None, False, None)
    trombone1 = classifier.compute_part_sort_key("trombone", 1, None, False, None)
    assert score_key < cornet1  # scores sort first
    assert cornet1 < cornet2  # part index orders within an instrument
    assert cornet1 < trombone1  # cornet precedes trombone in lexicon order


def test_build_piece_rollups():
    recs = [
        _classify({"pdf_path": "S/S - Cornet 1.pdf", "pdf_filename": "S - Cornet 1.pdf",
                   "piece_folder": "241 S", "piece_id": "p1", "file_fingerprint": "a"}),
        _classify({"pdf_path": "S/S - Cornet 1 dup.pdf", "pdf_filename": "S - Cornet 1.pdf",
                   "piece_folder": "241 S", "piece_id": "p1", "file_fingerprint": "b"}),
        _classify({"pdf_path": "S/S - Full Score.pdf", "pdf_filename": "S - Full Score.pdf",
                   "piece_folder": "241 S", "piece_id": "p1", "file_fingerprint": "c"}),
    ]
    classifier.apply_ensemble(recs)
    rollups = classifier.build_piece_rollups(recs, "run1")
    assert len(rollups) == 1
    piece = rollups[0]
    assert piece["piece_id"] == "p1"
    assert piece["catalog_number"] == "241"
    assert piece["document_count"] == 3
    assert piece["has_score"] is True
    assert piece["distinct_instruments"] == 1
    assert piece["duplicate_count"] == 2
    # The two Cornet 1 docs collapse to one observed-part key with count 2.
    cornet_entries = [p for p in piece["observed_parts"] if p["canonical_instrument"] == "cornet"]
    assert len(cornet_entries) == 1
    assert cornet_entries[0]["count"] == 2
    assert cornet_entries[0]["duplicate"] is True


# --- End-to-end test ------------------------------------------------------------------------


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def test_full_run_end_to_end(tmp_path: Path):
    inventory = tmp_path / "raw_inventory.jsonl"
    documents = tmp_path / "documents.jsonl"
    pages = tmp_path / "pages.jsonl"
    output = tmp_path / "part_predictions.jsonl"
    pieces = tmp_path / "observed_parts_by_piece.jsonl"
    report = tmp_path / "part_classification_report.md"

    inv_records = [
        {"pdf_path": "Song/Song - Cornet 1.pdf", "pdf_filename": "Song - Cornet 1.pdf",
         "piece_folder": "Song", "piece_id": "p1", "file_fingerprint": "f1",
         "pdf_readable": True},
        {"pdf_path": "Song/Song - Cornet 1 dup.pdf", "pdf_filename": "Song - Cornet 1.pdf",
         "piece_folder": "Song", "piece_id": "p1", "file_fingerprint": "f2",
         "pdf_readable": True},
        {"pdf_path": "Song/Song - Baritone (BC).pdf", "pdf_filename": "Song - Baritone (BC).pdf",
         "piece_folder": "Song", "piece_id": "p1", "file_fingerprint": "f3",
         "pdf_readable": True},
        {"pdf_path": "Song/Song - Full Score.pdf", "pdf_filename": "Song - Full Score.pdf",
         "piece_folder": "Song", "piece_id": "p1", "file_fingerprint": "f4",
         "pdf_readable": True},
        {"pdf_path": "Song/broken.pdf", "pdf_filename": "broken.pdf",
         "piece_folder": "Song", "piece_id": "p1", "file_fingerprint": "f5",
         "pdf_readable": False},
    ]
    _write_jsonl(inventory, inv_records)
    _write_jsonl(documents, [])
    _write_jsonl(pages, [])

    result = runner.invoke(
        classifier.app,
        [
            "--inventory", str(inventory),
            "--documents", str(documents),
            "--pages", str(pages),
            "--output", str(output),
            "--output-pieces", str(pieces),
            "--output-report", str(report),
            "--mode", "full",
        ],
    )
    assert result.exit_code == 0, result.output

    records = read_jsonl(output)
    assert len(records) == 5
    by_path = {r["pdf_path"]: r for r in records}

    cornet = by_path["Song/Song - Cornet 1.pdf"]
    assert cornet["canonical_instrument"] == "cornet"
    assert cornet["part_index"] == 1
    assert cornet["duplicate_in_piece"] is True  # two Cornet 1 in the piece

    baritone = by_path["Song/Song - Baritone (BC).pdf"]
    assert baritone["canonical_instrument"] == "baritone_horn"
    assert baritone["clef"] == "bass"
    assert baritone["predicted_part"] == "Baritone (BC)"

    score = by_path["Song/Song - Full Score.pdf"]
    assert score["is_score"] is True
    assert score["family"] == "score"

    broken = by_path["Song/broken.pdf"]
    assert broken["processing_status"] == "skipped_unreadable"

    assert report.exists()
    checkpoint = read_json(tmp_path / ".part_classifier_checkpoint.json")
    assert checkpoint is not None
    assert checkpoint["record_count"] == 5

    # Per-piece rollup is written and aggregates the piece correctly.
    piece_rollups = read_jsonl(pieces)
    assert len(piece_rollups) == 1
    piece = piece_rollups[0]
    assert piece["piece_id"] == "p1"
    assert piece["has_score"] is True
    assert piece["duplicate_count"] == 2
    cornet = [p for p in piece["observed_parts"] if p["canonical_instrument"] == "cornet"]
    assert cornet and cornet[0]["count"] == 2


def test_incremental_reuse(tmp_path: Path):
    inventory = tmp_path / "raw_inventory.jsonl"
    output = tmp_path / "part_predictions.jsonl"

    inv_records = [
        {"pdf_path": "Song/Song - Tuba.pdf", "pdf_filename": "Song - Tuba.pdf",
         "piece_folder": "Song", "piece_id": "p1", "file_fingerprint": "f1",
         "pdf_readable": True},
    ]
    _write_jsonl(inventory, inv_records)
    _write_jsonl(tmp_path / "documents.jsonl", [])
    _write_jsonl(tmp_path / "pages.jsonl", [])

    base_args = [
        "--inventory", str(inventory),
        "--documents", str(tmp_path / "documents.jsonl"),
        "--pages", str(tmp_path / "pages.jsonl"),
        "--output", str(output),
        "--output-pieces", str(tmp_path / "observed_parts_by_piece.jsonl"),
        "--no-report",
    ]
    assert runner.invoke(classifier.app, [*base_args, "--mode", "full"]).exit_code == 0
    first = read_jsonl(output)[0]

    result = runner.invoke(classifier.app, [*base_args, "--mode", "incremental"])
    assert result.exit_code == 0
    second = read_jsonl(output)[0]
    assert second["run_id"] == first["run_id"]  # reused, not reclassified
