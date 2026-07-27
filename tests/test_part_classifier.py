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


def test_bass_clarinet_in_bb_not_plain_clarinet():
    # Regression (The Wellerman): the transposition-qualified filename "Bass Clarinet in Bb" must
    # classify as bass_clarinet. The redundant "clarinet in bb" alias (longer than "bass clarinet")
    # previously shadowed it, so the part was mislabeled Clarinet and the bass clarinet was reported
    # missing even though the file was present.
    _, compiled = _lexicon_and_compiled()
    canonical, _, _ = classifier.match_instrument(
        classifier.normalize("The Wellerman - Bass Clarinet in Bb"), compiled
    )
    assert canonical == "bass_clarinet"
    # A plain "Clarinet in Bb" label still resolves to clarinet (Bb captured as transposition).
    plain, _, _ = classifier.match_instrument(classifier.normalize("Clarinet in Bb"), compiled)
    assert plain == "clarinet"


def test_alto_clarinet_in_eb_not_eb_clarinet():
    # Regression: "Alto Clarinet in Eb" must classify as alto_clarinet, not the Eb (sopranino)
    # clarinet. The eb_clarinet alias "clarinet in eb" would otherwise shadow "alto clarinet".
    _, compiled = _lexicon_and_compiled()
    for label in ("Alto Clarinet in Eb", "Alto Clarinet in E flat"):
        canonical, _, _ = classifier.match_instrument(classifier.normalize(label), compiled)
        assert canonical == "alto_clarinet", f"{label} -> {canonical}"
    # A plain Eb (sopranino) clarinet is unaffected.
    eb, _, _ = classifier.match_instrument(classifier.normalize("Clarinet in Eb"), compiled)
    assert eb == "eb_clarinet"


def test_classify_bass_clarinet_filename_only():
    # End-to-end (filename baseline, no page text): the Wellerman bass clarinet part classifies as
    # bass_clarinet with the transposition captured, so reconciliation no longer reports it missing.
    inv = {
        "pdf_path": "741 vThe Wellerman/741 The Wellerman - Bass Clarinet in Bb,.pdf",
        "pdf_filename": "741 The Wellerman - Bass Clarinet in Bb,.pdf",
        "piece_folder": "741 vThe Wellerman",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    rec = _classify(inv)
    assert [f["canonical"] for f in rec["instruments"]] == ["bass_clarinet"]
    assert rec["transposition"] == "Bb"


def test_guitar_part_is_classified():
    # Regression: "Guitar (Opt)" must classify as guitar, not fall through unmatched
    # (which previously produced a false "missing guitar" report downstream).
    lexicon, compiled = _lexicon_and_compiled()
    canonical, alias, _ = classifier.match_instrument(
        classifier.normalize("Guitar (Opt)"), compiled
    )
    assert canonical == "guitar"
    assert alias == "guitar"
    assert lexicon["families"]["guitar"] == "strings"


def test_expanded_lexicon_disambiguation():
    # New rhythm-section / auxiliary-percussion entries must not shadow existing instruments,
    # and multi-word names must win over their shorter substrings (longest-alias-wins).
    _, compiled = _lexicon_and_compiled()
    cases = {
        "Bass Guitar": "bass_guitar",
        "Electric Bass": "bass_guitar",
        "Eb Bass": "tuba",
        "Bass Flute": "bass_flute",
        "Contra Bassoon": "contrabassoon",
        "Bassoon": "bassoon",
        "Violoncello": "cello",
        "Wind Chimes": "wind_chimes",
        "Finger Cymbals": "finger_cymbals",
        "Tenor Drum": "tenor_drum",
        "Gong": "tam_tam",
        "Piano": "piano",
        "Harp": "harp",
    }
    for label, expected in cases.items():
        canonical, _, _ = classifier.match_instrument(classifier.normalize(label), compiled)
        assert canonical == expected, f"{label!r} classified as {canonical!r}, expected {expected!r}"


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
    assert rec["instruments"][0]["canonical"] == "cornet"
    assert rec["instruments"][0]["part_index"] == 2
    assert rec["evidence_source"] == "filename"
    assert rec["confidence"] == 0.75


def test_classify_combined_doubling_part_lists_both_instruments():
    # A single physical part covering a chair plus a doubling (e.g. "Flute 1 & Piccolo")
    # must be classified as BOTH instruments so it can satisfy either expected slot.
    inv = {
        "pdf_path": "P/Song - Flute 1 & Piccolo.pdf",
        "pdf_filename": "Song - Flute 1 & Piccolo.pdf",
        "piece_folder": "Song",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    rec = _classify(inv)
    canonicals = {(f["canonical"], f["part_index"]) for f in rec["instruments"]}
    assert canonicals == {("flute", 1), ("piccolo", None)}
    # The composed label mentions both roles.
    assert "Flute" in rec["predicted_part"] and "Piccolo" in rec["predicted_part"]


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
    assert rec["instruments"][0]["canonical"] == "trumpet"
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
    assert rec["instruments"][0]["canonical"] == "flute"
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
    assert rec["instruments"] == []
    assert rec["evidence_source"] == "none"


def test_ocr_llm_expands_generic_percussion():
    # A generic "Percussion" filename part is replaced by the specific instruments named by the
    # document-level OCR->LLM consolidation, so each can satisfy its own expected slot.
    inv = {
        "pdf_path": "P/001 Mexican Hat Dance - Percussion.pdf",
        "pdf_filename": "001 Mexican Hat Dance - Percussion.pdf",
        "piece_folder": "001 Mexican Hat Dance",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    doc = {"ocr_llm_instruments": ["snare_drum", "bass_drum", "castanets", "tambourine"]}
    rec = _classify(inv, doc)
    canonicals = [f["canonical"] for f in rec["instruments"]]
    assert canonicals == ["snare_drum", "bass_drum", "castanets", "tambourine"]
    assert rec["evidence_source"] == "combined"
    assert rec["confidence"] >= 0.70


def test_ocr_llm_overrides_conflicting_filename():
    # Content-first: the in-file OCR->LLM instrument is authoritative and overrides a conflicting
    # filename. The filename is only a last resort, so it never overrides in-file content.
    inv = {
        "pdf_path": "P/Song - Trumpet 1.pdf",
        "pdf_filename": "Song - Trumpet 1.pdf",
        "piece_folder": "Song",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    doc = {"ocr_llm_instruments": ["flute"]}
    rec = _classify(inv, doc)
    canonicals = [f["canonical"] for f in rec["instruments"]]
    assert canonicals == ["flute"]
    # A cross-section override is surfaced for human review.
    assert rec["needs_review"] is True


def test_ocr_llm_ignores_unknown_tokens():
    inv = {
        "pdf_path": "P/mystery.pdf",
        "pdf_filename": "mystery.pdf",
        "piece_folder": "P",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    doc = {"ocr_llm_instruments": ["not_a_real_instrument"]}
    rec = _classify(inv, doc)
    assert rec["instruments"] == []
    assert rec["evidence_source"] == "none"


def test_page_text_footer_credit_overrides_filename_euphonium():
    # Regression (A Night On A Lonely Moor): the filename says "Baritone" but the printed part is a
    # Euphonium, named in the glyph-polluted footer credit block. In-file content must win over the
    # filename, and since Baritone/Euphonium share a section the result is confident (no review).
    inv = {
        "pdf_path": "681 A Night On A Lonely Moor/681 A Night On A Lonely Moor_Baritone (BC).pdf",
        "pdf_filename": "681 A Night On A Lonely Moor_Baritone (BC).pdf",
        "piece_folder": "681 A Night On A Lonely Moor",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    # Embedded text is ~half music-notation glyphs; the instrument name sits in the footer credit.
    doc = {
        "first_page_text": (
            "\ue100\ue234\ue300 Soli \ue001\ue002 72 \ue010\ue011\ue012 "
            "Sole Owner \ue020EUPHONIUM\ue021 Composed by CLIVE LONGHURST "
            "A Night On A Lonely Moor \ue030\ue031"
        ),
    }
    rec = _classify(inv, doc)
    assert [f["canonical"] for f in rec["instruments"]] == ["euphonium"]
    assert rec["evidence_source"] == "combined"
    assert rec["confidence"] == 0.80
    assert rec["needs_review"] is False


def test_page_text_footer_credit_overrides_filename_bass_trombone():
    # Regression: "Trombone 3 (Bass)" is actually a Bass Trombone part (named in the footer). The
    # filename part index (3) is preserved while the in-file instrument identity wins.
    inv = {
        "pdf_path": "681 A Night On A Lonely Moor/681 A Night On A Lonely Moor_Trombone 3 (Bass).pdf",
        "pdf_filename": "681 A Night On A Lonely Moor_Trombone 3 (Bass).pdf",
        "piece_folder": "681 A Night On A Lonely Moor",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    doc = {
        "first_page_text": (
            "\ue100\ue234 5 \ue001\ue002 Sole Owner \ue020BASS TROMBONE\ue021 "
            "Composed by CLIVE LONGHURST A Night On A Lonely Moor \ue030"
        ),
    }
    rec = _classify(inv, doc)
    assert [f["canonical"] for f in rec["instruments"]] == ["bass_trombone"]
    assert rec["instruments"][0]["part_index"] == 3
    assert rec["evidence_source"] == "combined"
    assert rec["needs_review"] is False


def test_page_text_footer_zone_overrides_filename():
    # The footer/credit instrument name can arrive via the page-1 bottom zone, not just the flat
    # first_page_text; the classifier must read that zone too.
    inv = {
        "pdf_path": "P/Song_Baritone.pdf",
        "pdf_filename": "Song_Baritone.pdf",
        "piece_folder": "Song",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    page1 = {"zone_bottom": "Sole Owner \ue020Euphonium\ue021 Composed by X"}
    rec = _classify(inv, doc=None, page1=page1)
    assert [f["canonical"] for f in rec["instruments"]] == ["euphonium"]


def test_combined_doubling_preserved_with_page_text():
    # A single content instrument read from the page must NOT collapse a combined/doubling part
    # captured structurally by the filename.
    inv = {
        "pdf_path": "P/Song - Flute 1 & Piccolo.pdf",
        "pdf_filename": "Song - Flute 1 & Piccolo.pdf",
        "piece_folder": "Song",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    doc = {"first_page_text": "Flute Piccolo solo passage"}
    rec = _classify(inv, doc)
    canonicals = {(f["canonical"], f["part_index"]) for f in rec["instruments"]}
    assert canonicals == {("flute", 1), ("piccolo", None)}
    assert rec["evidence_source"] == "combined"


def test_strip_cues_removes_cue_annotations():
    # "<instrument> cue" / "<instrument> cues" reference OTHER instruments and must be removed.
    assert classifier.strip_cues("1st bb clarinets oboe cue dusk") == "1st bb clarinets dusk"
    assert classifier.strip_cues("bsn. cue then flute cues") == "then"
    # Unrelated words that merely contain "cue" (e.g. "rescue") are left untouched.
    assert classifier.strip_cues("no annotations rescue here") == "no annotations rescue here"


def test_match_instrument_first_prefers_earliest_position():
    # The printed part label sits leftmost; earliest position wins even when a LONGER alias for a
    # different instrument appears later in the text.
    _, compiled = _lexicon_and_compiled()
    text = classifier.normalize("Oboe then Bass Clarinet")
    first, _, _ = classifier.match_instrument_first(text, compiled, min_alias_len=4)
    assert first == "oboe"
    longest, _, _ = classifier.match_instrument(text, compiled, min_alias_len=4)
    assert longest == "bass_clarinet"


def test_upper_left_label_overrides_cue_clarinet():
    # Regression (A Night On A Lonely Moor): a Bb Clarinet part prints an "Oboe cue" in its body.
    # The upper-left label ("1st Bb CLARINETS") is authoritative and the cue must be ignored, so the
    # part classifies as clarinet (confirming the filename) -- never oboe.
    inv = {
        "pdf_path": "681 A Night On A Lonely Moor/681 A Night On A Lonely Moor_Clarinet 1 (Lower).pdf",
        "pdf_filename": "681 A Night On A Lonely Moor_Clarinet 1 (Lower).pdf",
        "piece_folder": "681 A Night On A Lonely Moor",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    page1 = {
        "zone_top_left": (
            "1st Bb CLARINETS (Lower part) Dusk (slow, calmly) \ue0b1 Oboe cue "
            "\ue043 \ue043\ue043\ue043"
        ),
    }
    rec = _classify(inv, doc=None, page1=page1)
    assert [f["canonical"] for f in rec["instruments"]] == ["clarinet"]
    assert rec["instruments"][0]["part_index"] == 1
    assert rec["needs_review"] is False


def test_upper_left_label_overrides_filename_cross_section_flagged():
    # The upper-left printed label is authoritative and may override the filename even across
    # sections -- but a cross-section override is surfaced for human review.
    inv = {
        "pdf_path": "P/Song - Trumpet 1.pdf",
        "pdf_filename": "Song - Trumpet 1.pdf",
        "piece_folder": "Song",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    doc = {"first_page_header_candidates": ["Flute"], "first_page_text": "Flute solo line"}
    rec = _classify(inv, doc)
    assert [f["canonical"] for f in rec["instruments"]] == ["flute"]
    assert rec["needs_review"] is True


def test_upper_left_dual_bass_label_prefers_first_listed_string_bass():
    # Regression (Chick Corea Ole): the part header reads "String Bass or Electric Bass". Longest-
    # alias-wins would pick "electric bass" (bass_guitar) and report the string bass missing; the
    # upper-left earliest-match must take the first-listed instrument, confirming the filename.
    inv = {
        "pdf_path": "241 Chick Corea Ole/241 Chick Corea Ole - String Bass.pdf",
        "pdf_filename": "241 Chick Corea Ole - String Bass.pdf",
        "piece_folder": "241 Chick Corea Ole",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    page1 = {"zone_top_left": "String Bass or Electric Bass Maestoso - Freely arcoA f"}
    rec = _classify(inv, doc=None, page1=page1)
    assert [f["canonical"] for f in rec["instruments"]] == ["string_bass"]


def test_full_text_cross_section_reference_does_not_override_filename():
    # Without an upper-left label, a cross-section instrument mentioned only in the body/full text
    # must NOT override the filename baseline (that is where stray references and cues live).
    inv = {
        "pdf_path": "P/Song - Clarinet 1.pdf",
        "pdf_filename": "Song - Clarinet 1.pdf",
        "piece_folder": "Song",
        "piece_id": "abc",
        "file_fingerprint": "fp1",
    }
    doc = {"first_page_text": "Oboe"}
    rec = _classify(inv, doc)
    assert [f["canonical"] for f in rec["instruments"]] == ["clarinet"]
    assert rec["evidence_source"] == "filename"


def test_apply_ensemble_flags_duplicates():
    def _facet(canonical, idx):
        return {"canonical": canonical, "part_index": idx, "family": "other", "section": "x"}

    records = [
        {"piece_id": "p", "instruments": [_facet("trumpet", 1)],
         "clef": None, "is_score": False, "duplicate_in_piece": False},
        {"piece_id": "p", "instruments": [_facet("trumpet", 1)],
         "clef": None, "is_score": False, "duplicate_in_piece": False},
        {"piece_id": "p", "instruments": [_facet("flute", 1)],
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
    assert cornet["instruments"][0]["section"] == "cornets_trumpets"
    tuba = _classify(
        {"pdf_path": "P/Song - Tuba.pdf", "pdf_filename": "Song - Tuba.pdf",
         "piece_folder": "Song", "piece_id": "p", "file_fingerprint": "f"}
    )
    assert tuba["instruments"][0]["section"] == "tubas"
    # Score parts carry no instrument facets; the record is flagged as a score instead.
    score = _classify(
        {"pdf_path": "P/Song - Full Score.pdf", "pdf_filename": "Song - Full Score.pdf",
         "piece_folder": "Song", "piece_id": "p", "file_fingerprint": "f"}
    )
    assert score["is_score"] is True
    assert score["instruments"] == []


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
    cornet_entries = [
        p for p in piece["observed_parts"]
        if any(f["canonical"] == "cornet" for f in p["instruments"])
    ]
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
    assert cornet["instruments"][0]["canonical"] == "cornet"
    assert cornet["instruments"][0]["part_index"] == 1
    assert cornet["duplicate_in_piece"] is True  # two Cornet 1 in the piece

    baritone = by_path["Song/Song - Baritone (BC).pdf"]
    assert baritone["instruments"][0]["canonical"] == "baritone_horn"
    assert baritone["clef"] == "bass"
    assert baritone["predicted_part"] == "Baritone (BC)"

    score = by_path["Song/Song - Full Score.pdf"]
    assert score["is_score"] is True
    assert score["instruments"] == []

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
    cornet = [
        p for p in piece["observed_parts"]
        if any(f["canonical"] == "cornet" for f in p["instruments"])
    ]
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
