from __future__ import annotations

from scripts._common import catalog_int, only_piece_decision


def test_catalog_int_parses_leading_number() -> None:
    assert catalog_int("368 Czardas") == 368
    assert catalog_int("368") == 368
    assert catalog_int("007 From Russia") == 7  # leading zeros are stripped
    assert catalog_int("12_Some-Piece") == 12
    assert catalog_int(368) == 368  # ints pass through unchanged


def test_catalog_int_without_number_is_none() -> None:
    assert catalog_int("Untitled Piece") is None
    assert catalog_int("") is None
    assert catalog_int(None) is None


def test_only_piece_decision_unscoped_is_normal() -> None:
    # No --only-piece: every record uses the script's usual full/incremental logic.
    assert only_piece_decision("368 Czardas", None, True) == "normal"
    assert only_piece_decision("368 Czardas", None, False) == "normal"


def test_only_piece_decision_targets_matching_catalogue() -> None:
    # The targeted piece is always force-recomputed, whether or not it has a prior record.
    assert only_piece_decision("368 Czardas", 368, True) == "process"
    assert only_piece_decision("368 Czardas", 368, False) == "process"
    assert only_piece_decision("368", 368, True) == "process"


def test_only_piece_decision_preserves_other_pieces() -> None:
    # A different piece with a prior record is preserved verbatim...
    assert only_piece_decision("500 Other", 368, True) == "reuse"
    # ...but a different piece with no prior record falls back to normal processing.
    assert only_piece_decision("500 Other", 368, False) == "normal"
    # A non-numeric folder never matches a catalogue number.
    assert only_piece_decision("Untitled", 368, True) == "reuse"
    assert only_piece_decision("Untitled", 368, False) == "normal"
