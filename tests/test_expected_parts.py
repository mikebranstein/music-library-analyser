"""Tests for Script 04 (expected-parts inference via online score lookup).

The Copilot CLI is never invoked here: tests inject a fake ``lookup_fn`` or monkeypatch the
module-level ``run_copilot_lookup`` so nothing touches the network or spends AI credits.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

runner = CliRunner()


def load_module():
    path = Path(__file__).resolve().parent.parent / "scripts" / "04_expected_parts_inference.py"
    spec = importlib.util.spec_from_file_location("expected_parts_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


expected = load_module()


# --- Fixtures --------------------------------------------------------------------------------


def _observed(canonical: str, part_index: int | None, count: int = 1) -> dict[str, Any]:
    return {
        "instruments": [
            {"canonical": canonical, "part_index": part_index, "section": None}
        ],
        "clef": "treble",
        "predicted_part": f"{canonical}_{part_index}",
        "count": count,
        "min_confidence": 0.9,
        "max_confidence": 0.95,
        "needs_review": False,
        "duplicate": False,
        "part_sort_key": f"{canonical}:{part_index}",
    }


def _observed_combined(
    facets: list[tuple[str, int | None]], count: int = 1
) -> dict[str, Any]:
    """An observed part covering multiple instruments (a doubling/combined part)."""
    label = " / ".join(f"{c}_{i}" for c, i in facets)
    return {
        "instruments": [
            {"canonical": c, "part_index": i, "section": None} for c, i in facets
        ],
        "clef": "treble",
        "predicted_part": label,
        "count": count,
        "min_confidence": 0.9,
        "max_confidence": 0.95,
        "needs_review": False,
        "duplicate": False,
        "part_sort_key": label,
    }


def _piece(
    piece_id: str = "p1",
    observed: list[dict[str, Any]] | None = None,
    has_score: bool = True,
    needs_review_count: int = 0,
) -> dict[str, Any]:
    obs = observed if observed is not None else [
        _observed("cornet", 1),
        _observed("euphonium", 1),
    ]
    distinct = {
        f["canonical"] for o in obs for f in o.get("instruments", []) if f.get("canonical")
    }
    return {
        "piece_id": piece_id,
        "piece_folder": f"folder_{piece_id}",
        "catalog_number": "100",
        "piece_title_guess": f"Title {piece_id}",
        "has_score": has_score,
        "score_types": ["full_score"] if has_score else [],
        "distinct_instruments": len(distinct),
        "families": [],
        "sections": [],
        "needs_review_count": needs_review_count,
        "observed_parts": obs,
    }


def _score_result(
    parts: list[dict[str, Any]],
    *,
    match_found: bool = True,
    confidence: float = 0.9,
    score_expected: bool = True,
    ensemble_type: str = "concert_band",
) -> dict[str, Any]:
    return {
        "match_found": match_found,
        "identity_match_confidence": confidence,
        "ensemble_type": ensemble_type,
        "ensemble_display_name": "Concert Band",
        "score_expected": score_expected,
        "work_identity": {"title": "Found Title", "publisher": "ACME"},
        "expected_parts": parts,
        "evidence_sources": [{"url": "https://example.com", "title": "Pub"}],
        "notes": "",
    }


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


# --- Config + prompt loading -----------------------------------------------------------------


def test_load_lookup_config_builtin_when_missing(tmp_path: Path):
    config, source = expected.load_lookup_config(tmp_path / "nope.yaml")
    assert source == "builtin"
    assert config["enabled"] is True
    assert config["command"] == "copilot"


def test_load_lookup_config_yaml_override(tmp_path: Path):
    if expected.yaml is None:
        return
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "enabled: false\nmodel: gpt-x\nconfidence_threshold: 0.75\nallowed_domains: [a.com]\n",
        encoding="utf-8",
    )
    config, source = expected.load_lookup_config(cfg)
    assert source == "yaml"
    assert config["enabled"] is False
    assert config["model"] == "gpt-x"
    assert config["confidence_threshold"] == 0.75
    assert config["allowed_domains"] == ["a.com"]


def test_load_prompt_template_builtin_when_missing(tmp_path: Path):
    template, source = expected.load_prompt_template(tmp_path / "nope.txt")
    assert source == "builtin"
    assert "{title_guess}" in template


def test_load_prompt_template_from_file(tmp_path: Path):
    p = tmp_path / "prompt.txt"
    p.write_text("hello {title_guess}", encoding="utf-8")
    template, source = expected.load_prompt_template(p)
    assert source == "file"
    assert template == "hello {title_guess}"


# --- Query + prompt building -----------------------------------------------------------------


def test_build_lookup_query():
    piece = _piece()
    doc = {"identity_candidates": {"composer": "Bach"}}
    query = expected.build_lookup_query(piece, doc)
    assert query["title_guess"] == "Title p1"
    assert query["catalog_number"] == "100"
    assert "cornet" in query["observed_summary"]
    assert "Bach" in query["identity_candidates"]


def test_render_prompt_no_leftover_placeholders():
    template, _ = expected.load_prompt_template(Path("does-not-exist"))
    query = expected.build_lookup_query(_piece(), None)
    rendered = expected.render_prompt(template, query)
    for key in query:
        assert "{" + key + "}" not in rendered
    # JSON braces preserved via {{ }} in the template.
    assert "match_found" in rendered


# --- CLI arg building ------------------------------------------------------------------------


def test_build_cli_args_allow_all_urls():
    config = dict(expected.DEFAULT_LOOKUP_CONFIG)
    args = expected.build_cli_args(config, "PROMPT")
    assert "-p" in args
    assert args[args.index("-p") + 1] == "PROMPT"
    assert "--allow-all-tools" in args
    assert "--no-ask-user" in args
    assert "--allow-all-urls" in args
    assert not any(a.startswith("--allow-url") for a in args)
    # Streaming is the default, so the CLI is not run in silent mode.
    assert "-s" not in args


def test_build_cli_args_silent_when_not_streaming():
    config = {**expected.DEFAULT_LOOKUP_CONFIG, "stream_output": False}
    args = expected.build_cli_args(config, "PROMPT")
    assert "-s" in args
    assert "--log-level" in args and args[args.index("--log-level") + 1] == "none"


def test_build_cli_args_allow_specific_urls_and_model():
    config = dict(expected.DEFAULT_LOOKUP_CONFIG)
    config["allowed_domains"] = ["a.com", "b.com"]
    config["model"] = "gpt-x"
    args = expected.build_cli_args(config, "P")
    assert "--allow-url=a.com,b.com" in args
    assert "--allow-all-urls" not in args
    assert "--model" in args and args[args.index("--model") + 1] == "gpt-x"


# --- Response parsing ------------------------------------------------------------------------


def test_parse_lookup_response_sentinels():
    payload = {"match_found": True, "expected_parts": []}
    stdout = f"chatter\n{expected.RESULT_START}\n{json.dumps(payload)}\n{expected.RESULT_END}\ntail"
    result = expected.parse_lookup_response(stdout)
    assert result["match_found"] is True


def test_parse_lookup_response_fallback_last_json():
    payload = {"match_found": False}
    stdout = f"some prose {json.dumps(payload)} trailing words"
    result = expected.parse_lookup_response(stdout)
    assert result["match_found"] is False


def test_parse_lookup_response_tolerates_trailing_commas():
    stdout = (
        f"{expected.RESULT_START}\n"
        '{"match_found": true, "expected_parts": ["flute", "oboe",],}\n'
        f"{expected.RESULT_END}\n"
    )
    result = expected.parse_lookup_response(stdout)
    assert result["match_found"] is True
    assert result["expected_parts"] == ["flute", "oboe"]


def test_parse_lookup_response_invalid_reports_payload():
    # Unescaped inner quote is not auto-repaired; the error must surface the payload snippet.
    bad = (
        f"{expected.RESULT_START}\n"
        '{"match_found": true, "notes": "uses a "special" flute"}\n'
        f"{expected.RESULT_END}\n"
    )
    try:
        expected.parse_lookup_response(bad)
    except ValueError as exc:
        assert "payload was:" in str(exc)
        return
    raise AssertionError("expected ValueError")


def test_parse_lookup_response_invalid_raises():
    try:
        expected.parse_lookup_response("no json at all")
    except ValueError:
        return
    raise AssertionError("expected ValueError")


# --- Normalization ---------------------------------------------------------------------------


def test_normalize_expected_parts():
    raw = [
        {"canonical_instrument": "Cornet", "part_index": "1", "label": "Solo Cornet"},
        {"canonical_instrument": "tuba", "part_index": None, "required": False},
        {"label": "no canonical"},
        "not a dict",
    ]
    slots = expected.normalize_expected_parts(raw)
    assert len(slots) == 2
    assert slots[0]["canonical"] == "cornet"
    assert slots[0]["part_index"] == 1
    assert slots[0]["required"] is True
    assert slots[1]["required"] is False
    assert slots[1]["part_index"] is None


def test_normalize_expected_parts_canonicalizes_to_taxonomy():
    # The lookup LLM emits full instrument names; they must be mapped onto the same canonical
    # tokens Script 03 uses for observed parts, and the section derived from the taxonomy.
    raw = [
        {"canonical_instrument": "Alto Saxophone", "part_index": 1, "label": "Alto Saxophone I"},
        {"canonical_instrument": "alto_saxophone", "part_index": 2, "label": "Alto Saxophone II"},
        {"canonical_instrument": "drum kit", "part_index": 1, "label": "Drum kit"},
    ]
    slots = expected.normalize_expected_parts(raw)
    assert [s["canonical"] for s in slots] == ["alto_sax", "alto_sax", "drum_set"]
    assert slots[0]["section"] == "saxophones"
    assert slots[0]["label"] == "Alto Saxophone I"


def test_normalize_expected_parts_drops_score_entries():
    # Authority instrumentation lists routinely start with the score edition (e.g. "Condensed
    # Score"). That is not a playable instrument part -- score presence is tracked separately --
    # so it must not become an expected slot that would falsely read as a missing required part.
    raw = [
        {"canonical_instrument": "Condensed Score", "label": "Condensed Score",
         "section": "score", "required": True},
        {"canonical_instrument": "Full Score", "label": "Full Score", "required": True},
        {"canonical_instrument": "Flute", "part_index": 1, "label": "Flute 1"},
    ]
    slots = expected.normalize_expected_parts(raw)
    assert [s["canonical"] for s in slots] == ["flute"]


def test_infer_piece_condensed_score_not_reported_missing():
    # Regression (001 Mexican Hat Dance): a conductor score exists (has_score True) and the lookup
    # lists "Condensed Score" first. The score edition must not surface as a missing required part.
    piece = _piece(observed=[_observed("flute", 1)], has_score=True)
    parts = [
        {"canonical_instrument": "Condensed Score", "label": "Condensed Score",
         "section": "score", "required": True},
        {"canonical_instrument": "flute", "part_index": 1, "label": "Flute 1", "required": True},
    ]
    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts),
    )
    assert "Condensed Score" not in rec["missing_required_parts"]
    assert rec["missing_required_count"] == 0
    assert rec["score_missing"] is False
    assert rec["needs_review"] is False


# --- Reconciliation --------------------------------------------------------------------------


def test_reconcile_present_missing_unexpected():
    slots = [
        {"canonical": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
        {"canonical": "cornet", "part_index": 2, "label": "Cornet 2", "required": True},
        {"canonical": "tuba", "part_index": None, "label": "Tuba", "required": False},
    ]
    observed = [_observed("cornet", 1), _observed("trombone", 1)]
    expected_parts, unexpected = expected.reconcile_parts(slots, observed)
    present = {(e["canonical_instrument"], e["part_index"]): e["present"] for e in expected_parts}
    assert present[("cornet", 1)] is True
    assert present[("cornet", 2)] is False
    assert present[("tuba", None)] is False
    assert any(
        any(f["canonical"] == "trombone" for f in u["instruments"]) for u in unexpected
    )


def test_reconcile_null_index_consumes_in_order():
    slots = [
        {"canonical": "cornet", "part_index": None, "label": "Cornet A", "required": True},
        {"canonical": "cornet", "part_index": None, "label": "Cornet B", "required": True},
    ]
    observed = [_observed("cornet", None), _observed("cornet", None)]
    expected_parts, unexpected = expected.reconcile_parts(slots, observed)
    assert all(e["present"] for e in expected_parts)
    assert unexpected == []


def test_reconcile_extra_same_instrument_is_unexpected():
    slots = [{"canonical": "cornet", "part_index": 1, "label": "Cornet 1", "required": True}]
    observed = [_observed("cornet", 1), _observed("cornet", 2)]
    expected_parts, unexpected = expected.reconcile_parts(slots, observed)
    assert expected_parts[0]["present"] is True
    assert len(unexpected) == 1
    assert unexpected[0]["instruments"][0]["canonical"] == "cornet"


def test_reconcile_combined_part_satisfies_multiple_slots():
    # "Flute 1 & Piccolo" is one physical part that covers TWO expected slots.
    slots = [
        {"canonical": "flute", "part_index": 1, "label": "Flute 1", "required": True},
        {"canonical": "piccolo", "part_index": None, "label": "Piccolo", "required": True},
    ]
    observed = [_observed_combined([("flute", 1), ("piccolo", None)])]
    expected_parts, unexpected = expected.reconcile_parts(slots, observed)
    present = {(e["canonical_instrument"], e["part_index"]): e["present"] for e in expected_parts}
    assert present[("flute", 1)] is True
    assert present[("piccolo", None)] is True
    assert unexpected == []


def test_reconcile_anchored_doubling_not_flagged_unexpected():
    # Only flute is expected; the piccolo doubling on the same part is silently accepted
    # because the part is anchored by a matched instrument.
    slots = [
        {"canonical": "flute", "part_index": 1, "label": "Flute 1", "required": True},
    ]
    observed = [_observed_combined([("flute", 1), ("piccolo", None)])]
    expected_parts, unexpected = expected.reconcile_parts(slots, observed)
    assert expected_parts[0]["present"] is True
    assert unexpected == []


def test_reconcile_fully_unmatched_combined_part_is_unexpected():
    # Neither instrument on the combined part is expected -> the whole part is unexpected.
    slots = [{"canonical": "cornet", "part_index": 1, "label": "Cornet 1", "required": True}]
    observed = [_observed_combined([("oboe", None), ("english_horn", None)])]
    expected_parts, unexpected = expected.reconcile_parts(slots, observed)
    assert expected_parts[0]["present"] is False
    assert len(unexpected) == 1
    canonicals = {f["canonical"] for f in unexpected[0]["instruments"]}
    assert canonicals == {"oboe", "english_horn"}


def test_collapse_clef_editions_merges_bc_and_tc():
    observed = [
        {**_observed("euphonium", 1), "clef": "bass"},
        {**_observed("euphonium", 1), "clef": "treble"},
    ]
    collapsed = expected.collapse_clef_editions(observed)
    assert len(collapsed) == 1
    assert collapsed[0]["observed_clefs"] == ["BC", "TC"]
    assert collapsed[0]["count"] == 2


def test_collapse_clef_editions_keeps_distinct_indices():
    observed = [
        {**_observed("euphonium", 1), "clef": "bass"},
        {**_observed("euphonium", 2), "clef": "treble"},
    ]
    collapsed = expected.collapse_clef_editions(observed)
    assert len(collapsed) == 2


def test_collapse_clef_editions_keeps_same_clef_copies_separate():
    # Two same-clef copies are distinct physical parts, not clef editions: keep both.
    observed = [
        {**_observed("cornet", None), "clef": "treble"},
        {**_observed("cornet", None), "clef": "treble"},
    ]
    collapsed = expected.collapse_clef_editions(observed)
    assert len(collapsed) == 2


def test_reconcile_clef_editions_fill_one_slot():
    slots = [
        {"canonical": "euphonium", "part_index": 1, "label": "Euphonium", "required": True},
    ]
    observed = [
        {**_observed("euphonium", 1), "clef": "bass"},
        {**_observed("euphonium", 1), "clef": "treble"},
    ]
    expected_parts, unexpected = expected.reconcile_parts(slots, observed)
    assert expected_parts[0]["present"] is True
    assert expected_parts[0]["observed_clefs"] == ["BC", "TC"]
    assert unexpected == []


def test_reconcile_clef_editions_no_completeness_inflation():
    slots = [
        {"canonical": "euphonium", "part_index": 1, "label": "Euphonium 1", "required": True},
        {"canonical": "euphonium", "part_index": 2, "label": "Euphonium 2", "required": True},
    ]
    observed = [
        {**_observed("euphonium", 1), "clef": "bass"},
        {**_observed("euphonium", 1), "clef": "treble"},
    ]
    expected_parts, unexpected = expected.reconcile_parts(slots, observed)
    present = {(e["canonical_instrument"], e["part_index"]): e["present"] for e in expected_parts}
    assert present[("euphonium", 1)] is True
    assert present[("euphonium", 2)] is False
    assert unexpected == []


# --- Completeness tiers ----------------------------------------------------------------------


def test_completeness_tier_bands():
    assert expected.completeness_tier(1.0, 0, False) == "complete"
    assert expected.completeness_tier(0.9, 1, False) == "near_complete"
    assert expected.completeness_tier(0.6, 2, False) == "incomplete"
    assert expected.completeness_tier(0.2, 5, False) == "severely_incomplete"
    assert expected.completeness_tier(1.0, 0, True) == "near_complete"


# --- infer_piece paths -----------------------------------------------------------------------


def test_infer_piece_confident_complete():
    piece = _piece(observed=[_observed("cornet", 1), _observed("euphonium", 1)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
        {"canonical_instrument": "euphonium", "part_index": 1, "label": "Euph", "required": True},
    ]

    def fake_lookup(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
        return _score_result(parts)

    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=fake_lookup,
    )
    assert rec["lookup_status"] == "matched"
    assert rec["inference_method"] == "authority_lookup"
    assert rec["completeness_tier"] == "complete"
    assert rec["needs_review"] is False
    assert rec["ensemble_type"] == "concert_band"


def test_infer_piece_lookup_full_names_reconcile_with_observed_abbreviations():
    # Regression: the lookup LLM emits full instrument names (e.g. "Alto Saxophone") while Script
    # 03 emits abbreviated canonical tokens (e.g. "alto_sax"). Without canonicalization the two
    # never match, producing phantom missing_required + unexpected parts for the same instrument.
    piece = _piece(observed=[_observed("alto_sax", 1), _observed("alto_sax", 2)])
    parts = [
        {"canonical_instrument": "Alto Saxophone", "part_index": 1,
         "label": "Alto Saxophone I", "required": True},
        {"canonical_instrument": "alto_saxophone", "part_index": 2,
         "label": "Alto Saxophone II", "required": True},
    ]
    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts),
    )
    assert rec["missing_required_count"] == 0
    assert rec["missing_required_parts"] == []
    assert rec["unexpected_part_count"] == 0
    assert rec["completeness_tier"] == "complete"
    assert rec["needs_review"] is False


def test_infer_piece_clef_editions_not_unexpected_and_shown_in_report():
    piece = _piece(observed=[
        {**_observed("euphonium", 1), "clef": "bass"},
        {**_observed("euphonium", 1), "clef": "treble"},
    ])
    parts = [
        {"canonical_instrument": "euphonium", "part_index": 1, "label": "Euphonium",
         "required": True},
    ]
    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts),
    )
    assert rec["unexpected_part_count"] == 0
    assert rec["needs_review"] is False
    assert rec["completeness_tier"] == "complete"
    body = "\n".join(expected.render_piece_instrumentation_body(rec))
    assert "yes (BC, TC)" in body
    assert "Observed but not expected" not in body


def test_infer_piece_missing_required_flags_review():
    piece = _piece(observed=[_observed("cornet", 1)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
        {"canonical_instrument": "euphonium", "part_index": 1, "label": "Euph", "required": True},
    ]
    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts),
    )
    assert rec["missing_required_count"] == 1
    assert "Euph" in rec["missing_required_parts"]
    assert rec["needs_review"] is True


def test_infer_piece_unexpected_part_is_warning_not_review():
    # An extra part the score doesn't enumerate is informational: it is reported but must NOT
    # trigger needs_review when everything expected is present.
    piece = _piece(observed=[_observed("cornet", 1), _observed("tuba", None)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]
    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts),
    )
    assert rec["unexpected_part_count"] == 1
    assert rec["missing_required_count"] == 0
    assert rec["completeness_tier"] == "complete"
    assert rec["needs_review"] is False
    body = "\n".join(expected.render_piece_instrumentation_body(rec))
    assert "Observed but not expected" in body


def test_infer_piece_low_confidence_conservative():
    piece = _piece()
    parts = [{"canonical_instrument": "cornet", "part_index": 1, "label": "C", "required": True}]
    rec = expected.infer_piece(
        piece, None, "run1",
        config={**expected.DEFAULT_LOOKUP_CONFIG, "confidence_threshold": 0.8},
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts, confidence=0.4),
    )
    assert rec["lookup_status"] == "low_confidence"
    assert rec["inference_method"] == "fallback_conservative"
    assert rec["expected_parts"] == []
    assert rec["completeness_score"] is None
    assert rec["needs_review"] is True


def test_infer_piece_no_match_conservative():
    piece = _piece()
    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result([], match_found=False),
    )
    assert rec["lookup_status"] == "no_match"
    assert rec["completeness_tier"] == "unknown"
    assert rec["needs_review"] is True


def test_infer_piece_disabled_conservative():
    rec = expected.infer_piece(
        _piece(), None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=False,
        lookup_fn=lambda p, c: _score_result([]),
    )
    assert rec["lookup_status"] == "disabled"
    assert rec["expected_parts"] == []


def test_infer_piece_lookup_error_conservative():
    def boom(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("cli exploded")

    rec = expected.infer_piece(
        _piece(), None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=boom,
    )
    assert rec["lookup_status"] == "error"
    assert "cli exploded" in rec["lookup_notes"]
    assert rec["needs_review"] is True


# --- Stage A: local score OCR ----------------------------------------------------------------


_LONG_SCORE_TEXT = (
    "Full Score - Solo Cornet in Bb - Euphonium - "
    + ("staff labels and instrumentation list. " * 20)
)


def test_infer_piece_stage_a_local_score_ocr():
    piece = _piece(observed=[_observed("cornet", 1), _observed("euphonium", 1)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
        {"canonical_instrument": "euphonium", "part_index": 1, "label": "Euph", "required": True},
    ]

    def summarize(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
        return _score_result(parts)

    def lookup(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("online lookup must not run when local score OCR succeeds")

    def score_provider(p: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        return _LONG_SCORE_TEXT, {"pdf_path": "band/p1/full_score.pdf", "score_type": "full"}

    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lookup,
        summarize_fn=summarize,
        summarize_template="{score_text}",
        score_text_provider=score_provider,
    )
    assert rec["lookup_status"] == "matched"
    assert rec["detection_method"] == "local_score_ocr"
    assert rec["inference_method"] == "local_score_ocr"
    assert rec["ocr_source"] == "local_score"
    assert rec["local_score_path"] == "band/p1/full_score.pdf"
    assert rec["completeness_tier"] == "complete"


def test_infer_piece_stage_a_thin_text_falls_through_to_lookup():
    piece = _piece(observed=[_observed("cornet", 1)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]

    def score_provider(p: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        # Below min_score_text_chars, so Stage A must skip and defer to the online lookup.
        return "tiny", {"pdf_path": "band/p1/full_score.pdf", "score_type": "full"}

    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts),
        summarize_fn=lambda p, c: _score_result(parts),
        summarize_template="{score_text}",
        score_text_provider=score_provider,
    )
    assert rec["lookup_status"] == "matched"
    assert rec["detection_method"] == "authority_lookup"
    assert rec["ocr_source"] is None


def test_infer_piece_stage_a_no_score_defers_to_lookup():
    piece = _piece(observed=[_observed("cornet", 1)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]

    def score_provider(p: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        return None, None  # no local score found for this piece

    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts),
        summarize_fn=lambda p, c: _score_result(parts),
        summarize_template="{score_text}",
        score_text_provider=score_provider,
    )
    assert rec["lookup_status"] == "matched"
    assert rec["detection_method"] == "authority_lookup"


# --- Stage C: remote image OCR ---------------------------------------------------------------


def test_infer_piece_stage_c_image_ocr():
    piece = _piece(observed=[_observed("cornet", 1), _observed("euphonium", 1)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
        {"canonical_instrument": "euphonium", "part_index": 1, "label": "Euph", "required": True},
    ]
    # Online lookup matches an edition but returns no expected parts, only candidate images.
    lookup_result = _score_result([], match_found=True, confidence=0.9)
    lookup_result["candidate_score_images"] = [
        {"url": "https://libris.kb.se/score_page1.jpg", "kind": "score_page"},
    ]

    fetched: list[str] = []

    def image_fetch(url: str, dest_dir: Path, index: int) -> Path:
        fetched.append(url)
        p = dest_dir / f"image_{index:02d}.jpg"
        dest_dir.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fake")
        return p

    def image_ocr(path: Path) -> str:
        return _LONG_SCORE_TEXT

    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: lookup_result,
        summarize_fn=lambda p, c: _score_result(parts),
        summarize_template="{score_text}",
        image_fetch_fn=image_fetch,
        image_ocr_fn=image_ocr,
    )
    assert rec["lookup_status"] == "matched"
    assert rec["detection_method"] == "score_image_ocr"
    assert rec["inference_method"] == "score_image_ocr"
    assert rec["ocr_source"] == "score_image"
    assert fetched == ["https://libris.kb.se/score_page1.jpg"]


def test_infer_piece_stage_c_no_images_stays_conservative():
    piece = _piece(observed=[_observed("cornet", 1)])
    # Matched edition, no parts, and no candidate images -> conservative NO_MATCH.
    lookup_result = _score_result([], match_found=True, confidence=0.9)

    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: lookup_result,
        summarize_fn=lambda p, c: _score_result([]),
        summarize_template="{score_text}",
        image_fetch_fn=lambda u, d, i: None,
        image_ocr_fn=lambda p: "",
    )
    assert rec["lookup_status"] == "no_match"
    assert rec["expected_parts"] == []
    assert rec["ocr_source"] is None


# --- Stage W: WindRep (W1 direct fetch, W2 dedicated LLM lookup) ------------------------------


_LONG_WINDREP_TEXT = (
    "WindRep work page: Some Piece\n\n== Instrumentation ==\n"
    + ("Flute 1 - Oboe - Clarinet 1 - Bassoon - Trumpet 1 - Horn - Percussion. " * 10)
)


def test_windrep_title_variations_most_specific_first():
    variations = expected._windrep_title_variations(
        {"title_guess": "The Blue Ridge", "piece_folder": "734 Blue Ridge Saga"}
    )
    assert variations[0] == "The Blue Ridge"
    assert "Blue Ridge" in variations  # leading article dropped
    assert "Blue Ridge Saga" in variations  # leading catalog number stripped from folder
    # No duplicates and no empty entries.
    assert len(variations) == len(set(variations))
    assert all(v for v in variations)


def test_windrep_title_variations_ignores_unknown():
    assert expected._windrep_title_variations(
        {"title_guess": "unknown", "piece_folder": "unknown"}
    ) == []


def test_infer_piece_windrep_direct_fetch_matches():
    piece = _piece(observed=[_observed("cornet", 1), _observed("euphonium", 1)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
        {"canonical_instrument": "euphonium", "part_index": 1, "label": "Euph", "required": True},
    ]

    def windrep_fetch(query: dict[str, Any], config: dict[str, Any]) -> str | None:
        return _LONG_WINDREP_TEXT

    def summarize(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
        return _score_result(parts)

    def lookup(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("general lookup must not run when Stage W1 succeeds")

    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lookup,
        summarize_fn=summarize,
        summarize_template="{score_text}",
        windrep_fetch_fn=windrep_fetch,
    )
    assert rec["lookup_status"] == "matched"
    assert rec["detection_method"] == "windrep_lookup"
    assert rec["inference_method"] == "windrep_lookup"
    assert rec["ocr_source"] == "windrep_fetch"


def test_infer_piece_windrep_fetch_none_falls_through_to_lookup():
    piece = _piece(observed=[_observed("cornet", 1)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]

    def windrep_fetch(query: dict[str, Any], config: dict[str, Any]) -> str | None:
        return None  # unreachable / no match -> graceful fallback

    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts),
        windrep_fetch_fn=windrep_fetch,
    )
    assert rec["lookup_status"] == "matched"
    assert rec["detection_method"] == "authority_lookup"


def test_infer_piece_windrep_llm_lookup_matches():
    piece = _piece(observed=[_observed("cornet", 1)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]

    seen_domains: list[Any] = []

    def lookup(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
        seen_domains.append(config.get("allowed_domains"))
        return _score_result(parts)

    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lookup,
        windrep_fetch_fn=lambda q, c: None,  # W1 misses, W2 wins
        windrep_prompt_template="{title_guess}",
    )
    assert rec["lookup_status"] == "matched"
    assert rec["detection_method"] == "windrep_lookup"
    assert rec["inference_method"] == "windrep_lookup"
    # W2 constrains the lookup to windrep.org.
    assert seen_domains[0] == ["windrep.org"]


def test_infer_piece_windrep_disabled_skips_stage_w():
    piece = _piece(observed=[_observed("cornet", 1)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]

    def windrep_fetch(query: dict[str, Any], config: dict[str, Any]) -> str | None:
        raise AssertionError("Stage W must not run when windrep is disabled")

    rec = expected.infer_piece(
        piece, None, "run1",
        config={**expected.DEFAULT_LOOKUP_CONFIG, "windrep_enabled": False},
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts),
        windrep_fetch_fn=windrep_fetch,
        windrep_prompt_template="{title_guess}",
    )
    assert rec["lookup_status"] == "matched"
    assert rec["detection_method"] == "authority_lookup"


def test_fetch_windrep_returns_none_on_timeout(monkeypatch, tmp_path: Path):
    def boom(api_url: str, params: dict[str, str], deadline: float):
        raise TimeoutError("connect timed out")

    monkeypatch.setattr(expected, "_windrep_api_get", boom)
    result = expected.fetch_windrep_instrumentation(
        {"title_guess": "Some Piece", "piece_folder": "123 Some Piece"},
        {**expected.DEFAULT_LOOKUP_CONFIG, "windrep_cache_dir": str(tmp_path / "wr")},
    )
    assert result is None


def test_fetch_windrep_returns_none_on_urlerror(monkeypatch, tmp_path: Path):
    def boom(api_url: str, params: dict[str, str], deadline: float):
        raise expected.urllib.error.URLError("geoblocked")

    monkeypatch.setattr(expected, "_windrep_api_get", boom)
    result = expected.fetch_windrep_instrumentation(
        {"title_guess": "Some Piece", "piece_folder": "123 Some Piece"},
        {**expected.DEFAULT_LOOKUP_CONFIG, "windrep_cache_dir": str(tmp_path / "wr")},
    )
    assert result is None


def test_fetch_windrep_parses_instrumentation_and_caches(monkeypatch, tmp_path: Path):
    cache_dir = tmp_path / "wr"

    def fake_api_get(api_url: str, params: dict[str, str], deadline: float):
        action = params.get("action")
        if action == "opensearch":
            return ["Lincolnshire Posy", ["Lincolnshire Posy"], [""], [""]]
        if action == "parse" and params.get("prop") == "sections":
            return {"parse": {"sections": [
                {"line": "Program Notes", "index": "1"},
                {"line": "Instrumentation", "index": "3"},
            ]}}
        if action == "parse" and params.get("prop") == "wikitext":
            assert params.get("section") == "3"
            return {"parse": {"wikitext": {"*": "Flute 1\nOboe\nClarinet in Bb 1"}}}
        raise AssertionError(f"unexpected API call: {params}")

    monkeypatch.setattr(expected, "_windrep_api_get", fake_api_get)
    query = {"title_guess": "Lincolnshire Posy", "piece_folder": "200 Lincolnshire Posy"}
    config = {**expected.DEFAULT_LOOKUP_CONFIG, "windrep_cache_dir": str(cache_dir)}

    result = expected.fetch_windrep_instrumentation(query, config)
    assert result is not None
    assert "WindRep work page: Lincolnshire Posy" in result
    assert "Flute 1" in result

    # Cached on success and reused without hitting the API again.
    cache_files = list(cache_dir.glob("*.json"))
    assert len(cache_files) == 1

    def explode(api_url: str, params: dict[str, str], deadline: float):
        raise AssertionError("cache should be used; API must not be called again")

    monkeypatch.setattr(expected, "_windrep_api_get", explode)
    cached = expected.fetch_windrep_instrumentation(query, config)
    assert cached == result


# --- Lookup diagnostics: summary log + raw-result persistence --------------------------------


def test_log_lookup_summary_handles_dict_and_non_dict():
    # Should never raise, regardless of the result shape.
    expected._log_lookup_summary("p1", {"match_found": True, "identity_match_confidence": 0.8,
                                         "expected_parts": [{}], "candidate_score_images": []})
    expected._log_lookup_summary("p1", "not a dict")
    expected._log_lookup_summary("p1", None)


def test_persist_lookup_result_writes_file_when_dir_set(tmp_path: Path):
    debug_dir = tmp_path / "lookups"
    config = {"lookup_debug_dir": str(debug_dir)}
    result = _score_result([], match_found=True, confidence=0.9)
    result["candidate_score_images"] = [{"url": "https://x/y.jpg", "kind": "title_page"}]

    expected._persist_lookup_result(config, "p1", "the prompt", result)

    dest = debug_dir / "lookup_p1.json"
    assert dest.exists()
    payload = json.loads(dest.read_text(encoding="utf-8"))
    assert payload["piece_id"] == "p1"
    assert payload["prompt"] == "the prompt"
    assert payload["result"]["candidate_score_images"][0]["url"] == "https://x/y.jpg"


def test_persist_lookup_result_noop_without_dir(tmp_path: Path):
    # No lookup_debug_dir configured -> nothing written, no error.
    expected._persist_lookup_result({}, "p1", "prompt", {"match_found": False})
    assert list(tmp_path.iterdir()) == []


def test_persist_lookup_result_sanitizes_piece_id(tmp_path: Path):
    debug_dir = tmp_path / "lookups"
    config = {"lookup_debug_dir": str(debug_dir)}
    expected._persist_lookup_result(config, "a/b:c d", "prompt", {"match_found": False})
    written = list(debug_dir.glob("lookup_*.json"))
    assert len(written) == 1
    assert "/" not in written[0].name and ":" not in written[0].name


def test_infer_piece_persists_lookup_result(tmp_path: Path):
    debug_dir = tmp_path / "lookups"
    config = dict(expected.DEFAULT_LOOKUP_CONFIG)
    config["lookup_debug_dir"] = str(debug_dir)
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]
    rec = expected.infer_piece(
        _piece(observed=[_observed("cornet", 1)]), None, "run1",
        config=config,
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts, match_found=True, confidence=0.9),
        summarize_template="{score_text}",
    )
    assert rec["lookup_status"] == "matched"
    assert (debug_dir / "lookup_p1.json").exists()


# --- Instrumentation report ------------------------------------------------------------------


def _meta() -> dict[str, Any]:
    return {
        "generated_at": "2026-01-01T00:00:00Z",
        "run_id": "run1",
        "mode": "full",
        "model": "gpt-x",
    }


def test_build_instrumentation_report_matched_piece():
    piece = _piece("p1", observed=[_observed("cornet", 1)])
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
        {"canonical_instrument": "euphonium", "part_index": 1, "label": "Euph", "required": True},
    ]
    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts),
    )
    report = expected.build_instrumentation_report([rec], _meta())
    assert "Expected Instrumentation by Piece" in report
    assert "Concert Band" in report
    assert "Cornet 1" in report
    assert "Euph" in report
    # cornet observed -> yes; euphonium not observed -> MISSING
    assert "MISSING" in report
    assert "https://example.com" in report


def test_build_instrumentation_report_fallback_piece():
    piece = _piece("p1")
    rec = expected.infer_piece(
        piece, None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=False,
        lookup_fn=lambda p, c: _score_result([]),
    )
    report = expected.build_instrumentation_report([rec], _meta())
    assert "No authoritative instrumentation found" in report


# --- End to end ------------------------------------------------------------------------------


def _run_cli(tmp_path: Path, monkeypatch, results_by_piece: dict[str, dict[str, Any]], extra=None):
    pieces_path = tmp_path / "pieces.jsonl"
    docs_path = tmp_path / "documents.jsonl"
    out_path = tmp_path / "expected_parts.jsonl"
    report_path = tmp_path / "report.md"
    instr_path = tmp_path / "instrumentation.md"

    calls = {"count": 0}
    calls_lock = threading.Lock()

    def fake_run(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
        with calls_lock:
            calls["count"] += 1
        # Match on title text embedded in the prompt.
        for piece_id, result in results_by_piece.items():
            if f"Title {piece_id}" in prompt:
                return result
        raise AssertionError("no canned result matched prompt")

    monkeypatch.setattr(expected, "run_copilot_lookup", fake_run)
    # Keep the CLI end-to-end tests offline/deterministic: the WindRep direct fetch never touches
    # the network here. Individual tests opt into the WindRep stage with an explicit "--windrep".
    monkeypatch.setattr(expected, "fetch_windrep_instrumentation", lambda query, config: None)

    args = [
        "--pieces", str(pieces_path),
        "--documents", str(docs_path),
        "--output", str(out_path),
        "--output-report", str(report_path),
        "--output-instrumentation", str(instr_path),
        "--config", str(tmp_path / "no_config.yaml"),
        "--no-windrep",
    ]
    if extra:
        args += extra
    result = runner.invoke(expected.app, args)
    return result, out_path, report_path, calls


def test_e2e_local_score_stage_a(tmp_path: Path, monkeypatch):
    """End-to-end: a piece with a local score is inferred via Stage A (no online lookup)."""
    pieces = [_piece("p1", observed=[_observed("cornet", 1), _observed("euphonium", 1)])]
    _write_jsonl(tmp_path / "pieces.jsonl", pieces)
    _write_jsonl(tmp_path / "documents.jsonl", [])
    _write_jsonl(tmp_path / "part_predictions.jsonl", [
        {
            "pdf_path": "band/p1/full_score.pdf",
            "piece_id": "p1",
            "is_score": True,
            "score_type": "full",
            "file_fingerprint": "abc123",
            "piece_folder": "folder_p1",
        },
    ])
    _write_jsonl(tmp_path / "extracted_text.jsonl", [
        {
            "pdf_path": "band/p1/full_score.pdf",
            "piece_id": "p1",
            "page_num": 1,
            "embedded_text": _LONG_SCORE_TEXT,
            "ocr_text": "",
            "text_source": "embedded",
            "header_text_candidates": ["Full Score"],
            "word_count": 120,
        },
    ])

    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
        {"canonical_instrument": "euphonium", "part_index": 1, "label": "Euph", "required": True},
    ]
    results = {"p1": _score_result(parts)}

    result, out_path, _report, calls = _run_cli(
        tmp_path, monkeypatch, results,
        extra=[
            "--part-predictions", str(tmp_path / "part_predictions.jsonl"),
            "--extracted-text", str(tmp_path / "extracted_text.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
    assert len(records) == 1
    rec = records[0]
    assert rec["detection_method"] == "local_score_ocr"
    assert rec["ocr_source"] == "local_score"
    assert rec["local_score_path"] == "band/p1/full_score.pdf"
    # Stage A succeeded, so the summarize call ran but the online lookup (Stage B) did not; both
    # go through the same monkeypatched fake, so exactly one call is expected.
    assert calls["count"] == 1


def test_e2e_no_local_score_flag_skips_stage_a(tmp_path: Path, monkeypatch):
    """With --no-local-score, a local score is ignored and the online lookup is used instead."""
    pieces = [_piece("p1", observed=[_observed("cornet", 1)])]
    _write_jsonl(tmp_path / "pieces.jsonl", pieces)
    _write_jsonl(tmp_path / "documents.jsonl", [])
    _write_jsonl(tmp_path / "part_predictions.jsonl", [
        {
            "pdf_path": "band/p1/full_score.pdf",
            "piece_id": "p1",
            "is_score": True,
            "score_type": "full",
            "file_fingerprint": "abc123",
            "piece_folder": "folder_p1",
        },
    ])
    _write_jsonl(tmp_path / "extracted_text.jsonl", [
        {
            "pdf_path": "band/p1/full_score.pdf",
            "piece_id": "p1",
            "page_num": 1,
            "embedded_text": _LONG_SCORE_TEXT,
            "ocr_text": "",
            "text_source": "embedded",
            "header_text_candidates": [],
            "word_count": 120,
        },
    ])

    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]
    results = {"p1": _score_result(parts)}

    result, out_path, _report, _calls = _run_cli(
        tmp_path, monkeypatch, results,
        extra=[
            "--no-local-score",
            "--part-predictions", str(tmp_path / "part_predictions.jsonl"),
            "--extracted-text", str(tmp_path / "extracted_text.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
    assert records[0]["detection_method"] == "authority_lookup"


def test_e2e_windrep_stage_matches_via_cli(tmp_path: Path, monkeypatch):
    """End-to-end: with --windrep, the WindRep stage runs before the general lookup."""
    pieces = [_piece("p1", observed=[_observed("cornet", 1)])]
    _write_jsonl(tmp_path / "pieces.jsonl", pieces)
    _write_jsonl(tmp_path / "documents.jsonl", [])

    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]
    results = {"p1": _score_result(parts)}

    # WindRep direct fetch (W1) is stubbed to miss by _run_cli, so the dedicated WindRep LLM
    # lookup (W2) resolves the piece and wins before the general authority lookup would run.
    result, out_path, _report, _calls = _run_cli(
        tmp_path, monkeypatch, results,
        extra=["--no-local-score", "--windrep"],
    )
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
    assert records[0]["detection_method"] == "windrep_lookup"
    assert records[0]["lookup_status"] == "matched"


def test_e2e_full_run(tmp_path: Path, monkeypatch):
    pieces = [
        _piece("p1", observed=[_observed("cornet", 1), _observed("euphonium", 1)]),
        _piece("p2", observed=[_observed("cornet", 1)]),
    ]
    _write_jsonl(tmp_path / "pieces.jsonl", pieces)
    _write_jsonl(tmp_path / "documents.jsonl", [])

    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
        {"canonical_instrument": "euphonium", "part_index": 1, "label": "Euph", "required": True},
    ]
    results = {"p1": _score_result(parts), "p2": _score_result(parts)}

    result, out_path, report_path, calls = _run_cli(tmp_path, monkeypatch, results)
    assert result.exit_code == 0, result.output

    records = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
    assert len(records) == 2
    by_id = {r["piece_id"]: r for r in records}
    assert by_id["p1"]["completeness_tier"] == "complete"
    assert by_id["p2"]["missing_required_count"] == 1
    assert report_path.exists()
    assert "Expected Parts Report" in report_path.read_text()
    assert (out_path.parent / ".expected_parts_checkpoint.json").exists()
    assert calls["count"] == 2

    instr = (out_path.parent / "instrumentation.md").read_text()
    assert "Expected Instrumentation by Piece" in instr
    # Split is on by default: instrumentation.md is an index that links to per-piece files.
    assert "(instrumentation/" in instr
    split_dir = out_path.parent / "instrumentation"
    assert split_dir.is_dir()
    combined = "\n".join(p.read_text() for p in sorted(split_dir.glob("*.md")))
    assert "Cornet 1" in combined
    # p2 is missing euphonium, so it must be flagged MISSING in its per-piece table.
    assert "MISSING" in combined


def test_e2e_incremental_reuse(tmp_path: Path, monkeypatch):
    pieces = [_piece("p1", observed=[_observed("cornet", 1)])]
    _write_jsonl(tmp_path / "pieces.jsonl", pieces)
    _write_jsonl(tmp_path / "documents.jsonl", [])

    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]
    results = {"p1": _score_result(parts)}

    r1, _out, _, calls1 = _run_cli(tmp_path, monkeypatch, results)
    assert r1.exit_code == 0, r1.output
    assert calls1["count"] == 1

    r2, _, _, calls2 = _run_cli(
        tmp_path, monkeypatch, results, extra=["--mode", "incremental"]
    )
    assert r2.exit_code == 0, r2.output
    # Unchanged piece must be reused, not looked up again.
    assert calls2["count"] == 0


def test_e2e_parallel_lookups(tmp_path: Path, monkeypatch):
    pieces = [
        _piece("p1", observed=[_observed("cornet", 1)]),
        _piece("p2", observed=[_observed("cornet", 1)]),
        _piece("p3", observed=[_observed("cornet", 1)]),
    ]
    _write_jsonl(tmp_path / "pieces.jsonl", pieces)
    _write_jsonl(tmp_path / "documents.jsonl", [])

    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]
    results = {pid: _score_result(parts) for pid in ("p1", "p2", "p3")}

    result, out_path, _, calls = _run_cli(
        tmp_path, monkeypatch, results, extra=["--concurrency", "3"]
    )
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
    assert {r["piece_id"] for r in records} == {"p1", "p2", "p3"}
    assert calls["count"] == 3
    assert all(r["lookup_status"] == "matched" for r in records)


# --- Per-piece instrumentation split ---------------------------------------------------------


def _matched_record(piece_id: str = "p1") -> dict[str, Any]:
    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
        {"canonical_instrument": "euphonium", "part_index": 1, "label": "Euph", "required": True},
    ]
    return expected.infer_piece(
        _piece(piece_id, observed=[_observed("cornet", 1)]), None, "run1",
        config=dict(expected.DEFAULT_LOOKUP_CONFIG),
        prompt_template="{title_guess}",
        lookup_enabled=True,
        lookup_fn=lambda p, c: _score_result(parts),
    )


def test_piece_instrumentation_filename_deterministic():
    rec = _matched_record("p1")
    name = expected.piece_instrumentation_filename(rec)
    # Stable across calls, ends in .md, leads with the catalog number, includes the piece id.
    assert name == expected.piece_instrumentation_filename(rec)
    assert name.endswith(".md")
    assert name.startswith("100_")
    assert "p1" in name
    # Filesystem-safe: no path separators or spaces.
    assert "/" not in name and "\\" not in name and " " not in name


def test_render_piece_instrumentation_doc_standalone():
    rec = _matched_record("p1")
    doc = expected.render_piece_instrumentation_doc(rec, _meta())
    assert doc.startswith("# ")  # standalone H1 heading
    assert "Cornet 1" in doc
    assert "Euph" in doc
    assert "MISSING" in doc  # euphonium not observed


def test_build_instrumentation_index_links_to_files():
    rec = _matched_record("p1")
    index = expected.build_instrumentation_index([rec], _meta(), "expected_instrumentation")
    assert "Expected Instrumentation by Piece" in index
    fname = expected.piece_instrumentation_filename(rec)
    assert f"(expected_instrumentation/{fname})" in index
    # The index is a summary table, not the full per-piece detail.
    assert "Cornet 1" not in index


def test_e2e_no_split_instrumentation(tmp_path: Path, monkeypatch):
    pieces = [_piece("p1", observed=[_observed("cornet", 1)])]
    _write_jsonl(tmp_path / "pieces.jsonl", pieces)
    _write_jsonl(tmp_path / "documents.jsonl", [])

    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]
    results = {"p1": _score_result(parts)}

    result, out_path, _, _calls = _run_cli(
        tmp_path, monkeypatch, results, extra=["--no-split-instrumentation"]
    )
    assert result.exit_code == 0, result.output

    instr = (out_path.parent / "instrumentation.md").read_text()
    # Single combined file: details are inline, and no split folder is created.
    assert "Cornet 1" in instr
    assert not (out_path.parent / "instrumentation").exists()


def test_e2e_split_writes_per_piece_files(tmp_path: Path, monkeypatch):
    pieces = [
        _piece("p1", observed=[_observed("cornet", 1)]),
        _piece("p2", observed=[_observed("cornet", 1)]),
    ]
    _write_jsonl(tmp_path / "pieces.jsonl", pieces)
    _write_jsonl(tmp_path / "documents.jsonl", [])

    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]
    results = {"p1": _score_result(parts), "p2": _score_result(parts)}

    result, out_path, _, _calls = _run_cli(tmp_path, monkeypatch, results)
    assert result.exit_code == 0, result.output

    split_dir = out_path.parent / "instrumentation"
    files = sorted(split_dir.glob("*.md"))
    assert len(files) == 2
    assert all(f.read_text().startswith("# ") for f in files)


def test_e2e_incremental_persistence_writes_during_run(tmp_path: Path, monkeypatch):
    """Records + checkpoint must be persisted per piece, not only once at the end."""
    pieces = [
        _piece("p1", observed=[_observed("cornet", 1)]),
        _piece("p2", observed=[_observed("cornet", 1)]),
    ]
    _write_jsonl(tmp_path / "pieces.jsonl", pieces)
    _write_jsonl(tmp_path / "documents.jsonl", [])

    parts = [
        {"canonical_instrument": "cornet", "part_index": 1, "label": "Cornet 1", "required": True},
    ]
    results = {"p1": _score_result(parts), "p2": _score_result(parts)}

    out_path = tmp_path / "expected_parts.jsonl"
    jsonl_writes = {"count": 0}
    real_write_jsonl = expected.atomic_write_jsonl

    def counting_write_jsonl(path, records):
        if Path(path).name == "expected_parts.jsonl":
            jsonl_writes["count"] += 1
        return real_write_jsonl(path, records)

    monkeypatch.setattr(expected, "atomic_write_jsonl", counting_write_jsonl)

    result, out_path2, _, _calls = _run_cli(
        tmp_path, monkeypatch, results, extra=["--concurrency", "1"]
    )
    assert result.exit_code == 0, result.output
    assert out_path2 == out_path
    # One write per completed piece (2) plus the final canonical write (>= 3), proving the output
    # is not written only once at the end.
    assert jsonl_writes["count"] >= 3
