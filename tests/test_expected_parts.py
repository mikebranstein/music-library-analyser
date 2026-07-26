"""Tests for Script 04 (expected-parts inference via online score lookup).

The Copilot CLI is never invoked here: tests inject a fake ``lookup_fn`` or monkeypatch the
module-level ``run_copilot_lookup`` so nothing touches the network or spends AI credits.
"""

from __future__ import annotations

import importlib.util
import json
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
        "canonical_instrument": canonical,
        "part_index": part_index,
        "clef": "treble",
        "section": None,
        "predicted_part": f"{canonical}_{part_index}",
        "count": count,
        "min_confidence": 0.9,
        "max_confidence": 0.95,
        "needs_review": False,
        "duplicate": False,
        "part_sort_key": f"{canonical}:{part_index}",
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
    return {
        "piece_id": piece_id,
        "piece_folder": f"folder_{piece_id}",
        "catalog_number": "100",
        "piece_title_guess": f"Title {piece_id}",
        "has_score": has_score,
        "score_types": ["full_score"] if has_score else [],
        "distinct_instruments": len({o["canonical_instrument"] for o in obs}),
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
    ensemble_type: str = "british_brass_band",
) -> dict[str, Any]:
    return {
        "match_found": match_found,
        "identity_match_confidence": confidence,
        "ensemble_type": ensemble_type,
        "ensemble_display_name": "British Brass Band",
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
    assert any(u["canonical_instrument"] == "trombone" for u in unexpected)


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
    assert unexpected[0]["canonical_instrument"] == "cornet"


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
    assert rec["ensemble_type"] == "british_brass_band"


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
    assert "British Brass Band" in report
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

    def fake_run(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
        calls["count"] += 1
        # Match on title text embedded in the prompt.
        for piece_id, result in results_by_piece.items():
            if f"Title {piece_id}" in prompt:
                return result
        raise AssertionError("no canned result matched prompt")

    monkeypatch.setattr(expected, "run_copilot_lookup", fake_run)

    args = [
        "--pieces", str(pieces_path),
        "--documents", str(docs_path),
        "--output", str(out_path),
        "--output-report", str(report_path),
        "--output-instrumentation", str(instr_path),
        "--config", str(tmp_path / "no_config.yaml"),
    ]
    if extra:
        args += extra
    result = runner.invoke(expected.app, args)
    return result, out_path, report_path, calls


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
    assert "Cornet 1" in instr
    # p2 is missing euphonium, so it must be flagged MISSING in the per-piece table.
    assert "MISSING" in instr


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
