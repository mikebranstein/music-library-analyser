from __future__ import annotations

from pathlib import Path

from scripts._common import (
    canonicalize_instrument,
    has_readable_text,
    instrument_display_name,
    load_instrument_taxonomy,
    normalize_instrument_name,
    read_jsonl,
    run_with_progress,
    strip_music_glyphs,
)

_RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "regex_rules.yaml"


def test_strip_music_glyphs_removes_private_use_area() -> None:
    # Music-notation fonts embed glyphs in the PUA (U+E000-U+F8FF); they must be stripped so the
    # readable instrument name survives. Each glyph run collapses to a single space.
    polluted = "Sole Owner \ue100\ue234EUPHONIUM\uf001 Composed by"
    cleaned = strip_music_glyphs(polluted)
    assert "\ue100" not in cleaned and "\uf001" not in cleaned
    assert "EUPHONIUM" in cleaned
    # Plain text is returned unchanged.
    assert strip_music_glyphs("Trumpet in Bb") == "Trumpet in Bb"
    assert strip_music_glyphs("") == ""
    assert strip_music_glyphs(None) == ""


def test_has_readable_text_ignores_glyph_soup() -> None:
    # A page whose embedded text is essentially all notation glyphs has no readable content.
    assert has_readable_text("\ue000\ue001\ue002\uf8ff") is False
    assert has_readable_text("   ") is False
    assert has_readable_text(None) is False
    # Real letters/digits (even amid glyphs) count as readable.
    assert has_readable_text("\ue000FLUTE\ue001") is True
    assert has_readable_text("Trumpet 1") is True


def test_read_jsonl_skips_malformed_and_non_object_lines(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "sample.jsonl"
    jsonl_path.write_text(
        "\n".join(
            [
                '{"ok": true}',
                "not-json",
                "[1, 2, 3]",
                '{"another": 1}',
                "",
            ]
        ),
        encoding="utf-8",
    )

    records = read_jsonl(jsonl_path)
    assert records == [{"ok": True}, {"another": 1}]


def test_normalize_instrument_name_collapses_separators() -> None:
    assert normalize_instrument_name("Alto_Saxophone") == "alto saxophone"
    assert normalize_instrument_name("  French   Horn ") == "french horn"
    assert normalize_instrument_name("bass-clarinet") == "bass clarinet"
    assert normalize_instrument_name("") == ""


def test_instrument_display_name_titlecases_snake_case_fallback() -> None:
    assert instrument_display_name("flute") == "Flute"
    assert instrument_display_name("baritone_horn") == "Baritone Horn"
    assert instrument_display_name("mallet_percussion") == "Mallet Percussion"
    assert instrument_display_name("string_bass") == "String Bass"


def test_instrument_display_name_applies_overrides() -> None:
    assert instrument_display_name("eb_clarinet") == "E-flat Clarinet"
    assert instrument_display_name("contra_alto_clarinet") == "Contra-alto Clarinet"
    assert instrument_display_name("alto_sax") == "Alto Saxophone"
    assert instrument_display_name("horn") == "Horn in F"
    assert instrument_display_name("tom_toms") == "Tom-toms"


def test_instrument_display_name_handles_blank_and_case() -> None:
    assert instrument_display_name(None) == ""
    assert instrument_display_name("") == ""
    assert instrument_display_name("  Baritone_Horn ") == "Baritone Horn"


def test_load_instrument_taxonomy_from_yaml() -> None:
    taxonomy = load_instrument_taxonomy(_RULES_PATH)
    alias_map = taxonomy["alias_to_canonical"]
    section_map = taxonomy["canonical_to_section"]
    # Aliases and canonical keys both resolve to the canonical token.
    assert alias_map["alto saxophone"] == "alto_sax"
    assert alias_map["alto sax"] == "alto_sax"
    assert alias_map["drum kit"] == "drum_set"
    # Canonical -> section mapping is available for consistent grouping.
    assert section_map["alto_sax"] == "saxophones"


def test_load_instrument_taxonomy_interchangeable_groups() -> None:
    taxonomy = load_instrument_taxonomy(_RULES_PATH)
    groups = taxonomy["interchangeable_groups"]
    assert any(set(g) == {"euphonium", "baritone_horn"} for g in groups)
    assert any(set(g) == {"cornet", "trumpet"} for g in groups)


def test_load_instrument_taxonomy_missing_file_is_empty(tmp_path: Path) -> None:
    taxonomy = load_instrument_taxonomy(tmp_path / "nope.yaml")
    assert taxonomy == {
        "alias_to_canonical": {},
        "canonical_to_section": {},
        "interchangeable_groups": [],
    }


def test_canonicalize_instrument_maps_synonyms_to_taxonomy() -> None:
    alias_map = load_instrument_taxonomy(_RULES_PATH)["alias_to_canonical"]
    assert canonicalize_instrument("alto_saxophone", alias_map) == "alto_sax"
    assert canonicalize_instrument("Tenor Saxophone", alias_map) == "tenor_sax"
    assert canonicalize_instrument("baritone_saxophone", alias_map) == "baritone_sax"
    assert canonicalize_instrument("drum_kit", alias_map) == "drum_set"
    # Already-canonical tokens are stable.
    assert canonicalize_instrument("alto_sax", alias_map) == "alto_sax"
    # Unknown instruments fall back to a uniform snake_case form.
    assert canonicalize_instrument("Theremin Deluxe", alias_map) == "theremin_deluxe"
    assert canonicalize_instrument("", alias_map) == ""


def test_run_with_progress_on_result_sequential() -> None:
    items = [1, 2, 3]
    seen: list[tuple[int, int]] = []

    results = run_with_progress(
        items, lambda x: x * 10, max_workers=1, on_result=lambda item, res: seen.append((item, res))
    )

    assert results == [10, 20, 30]
    # on_result is called once per item, in order, with (item, result).
    assert seen == [(1, 10), (2, 20), (3, 30)]


def test_run_with_progress_on_result_parallel_preserves_input_order() -> None:
    items = [1, 2, 3, 4]
    seen: list[tuple[int, int]] = []

    results = run_with_progress(
        items,
        lambda x: x * 10,
        max_workers=4,
        on_result=lambda item, res: seen.append((item, res)),
    )

    # Returned results stay in input order regardless of completion order.
    assert results == [10, 20, 30, 40]
    # Every item is reported exactly once via on_result.
    assert sorted(seen) == [(1, 10), (2, 20), (3, 30), (4, 40)]
