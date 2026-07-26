"""Script 04: infer the expected parts for each piece and flag missing parts.

Consumes the Script 03 per-piece observed-parts rollup (``data/observed_parts_by_piece.jsonl``)
and writes one expected-parts record per piece to ``data/expected_parts.jsonl`` plus a Markdown
report.

The primary inference path is an online lookup of the *actual published score*: for each piece the
script builds a work-identity query and asks the GitHub Copilot CLI (``copilot -p ...``) to search
authoritative web sources and return that edition's real instrumentation. The ensemble type is not
hardcoded; it is whatever the found score is. When lookup is disabled, finds no confident match, or
fails, the piece degrades to a conservative, observed-only record flagged for review -- it never
fabricates a "missing" part without an authoritative source.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None

from scripts._common import (
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    read_json,
    read_jsonl,
    sha256_text,
    utc_now_iso,
)

RECORD_VERSION = "2.0"

app = typer.Typer(add_completion=False)

logger = logging.getLogger("script04.expected")

# Completeness tier thresholds (fraction of required parts present).
NEAR_COMPLETE = 0.85
INCOMPLETE = 0.5

# Sentinels the lookup prompt wraps its JSON result in.
RESULT_START = "<<<SCORE_JSON>>>"
RESULT_END = "<<<END_SCORE_JSON>>>"

# --- Built-in lookup config (YAML overrides/extends it) --------------------------------------

DEFAULT_LOOKUP_CONFIG: dict[str, Any] = {
    "enabled": True,
    "command": "copilot",
    "model": "",
    "timeout_seconds": 300,
    "confidence_threshold": 0.5,
    "allowed_domains": [],
    "prompt_template_path": "config/llm_prompts/lookup_instrumentation.txt",
    "stream_output": True,
    "extra_args": [],
}

DEFAULT_PROMPT_TEMPLATE = (
    "You are a music librarian assistant. Identify the ACTUAL published score for one piece and\n"
    "report its real instrumentation by searching authoritative online sources.\n\n"
    "Piece:\n"
    "- Title guess: {title_guess}\n"
    "- Catalog / item number: {catalog_number}\n"
    "- Source folder name: {piece_folder}\n"
    "- Identity candidates: {identity_candidates}\n"
    "- Observed instruments: {observed_summary}\n\n"
    "Infer the ensemble type from the edition you find (do not assume it). Report one entry per\n"
    "named printed part with a lowercase snake_case canonical_instrument, a part_index (or null),\n"
    "a label, a section, and a required flag. If you cannot confidently identify the edition, set\n"
    "match_found to false and explain in notes. Never invent an edition or a source URL.\n\n"
    "Respond with exactly one JSON object between the sentinel lines and nothing else:\n"
    f"{RESULT_START}\n"
    '{{"match_found": true, "identity_match_confidence": 0.0, "ensemble_type": "",\n'
    '  "ensemble_display_name": "", "score_expected": true,\n'
    '  "work_identity": {{"title": null, "composer": null, "arranger": null,\n'
    '    "publisher": null, "catalog_number": null, "year": null}},\n'
    '  "expected_parts": [], "evidence_sources": [], "notes": ""}}\n'
    f"{RESULT_END}\n"
)


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def get_checkpoint_path(output: Path) -> Path:
    return output.parent / ".expected_parts_checkpoint.json"


# --- Config + prompt loading -----------------------------------------------------------------


def load_lookup_config(config_path: Path) -> tuple[dict[str, Any], str]:
    """Return (config, source) merging YAML overrides over the built-in defaults."""
    config = dict(DEFAULT_LOOKUP_CONFIG)
    config["allowed_domains"] = list(DEFAULT_LOOKUP_CONFIG["allowed_domains"])
    config["extra_args"] = list(DEFAULT_LOOKUP_CONFIG["extra_args"])
    if yaml is None:
        logger.warning("PyYAML unavailable; using built-in lookup config.")
        return config, "builtin"
    if not config_path.exists():
        logger.warning("Config file %s not found; using built-in lookup config.", config_path)
        return config, "builtin"
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to parse %s (%s); using built-in config.", config_path, exc)
        return config, "builtin"

    for key in DEFAULT_LOOKUP_CONFIG:
        if key in data and data[key] is not None:
            config[key] = data[key]
    config["allowed_domains"] = list(config.get("allowed_domains") or [])
    config["extra_args"] = list(config.get("extra_args") or [])
    return config, "yaml"


def load_prompt_template(template_path: Path) -> tuple[str, str]:
    """Return (template, source); falls back to a built-in prompt when the file is missing."""
    if not template_path.exists():
        logger.warning(
            "Prompt template %s not found; using built-in prompt.", template_path
        )
        return DEFAULT_PROMPT_TEMPLATE, "builtin"
    try:
        return template_path.read_text(encoding="utf-8"), "file"
    except Exception as exc:
        logger.warning("Failed to read %s (%s); using built-in prompt.", template_path, exc)
        return DEFAULT_PROMPT_TEMPLATE, "builtin"


# --- Query + prompt building -----------------------------------------------------------------


def observed_canonicals(piece: dict[str, Any]) -> set[str]:
    """Distinct non-null canonical instruments observed in a piece rollup."""
    result: set[str] = set()
    for obs in piece.get("observed_parts", []):
        canonical = obs.get("canonical_instrument")
        if canonical:
            result.add(canonical)
    return result


def _observed_summary(piece: dict[str, Any]) -> str:
    """Compact human-readable summary of the observed instruments and counts."""
    parts: list[str] = []
    for obs in piece.get("observed_parts", []):
        canonical = obs.get("canonical_instrument")
        if not canonical:
            continue
        idx = obs.get("part_index")
        count = obs.get("count", 1)
        label = canonical if idx is None else f"{canonical} {idx}"
        parts.append(f"{label} (x{count})" if count and count != 1 else label)
    return ", ".join(parts) if parts else "none detected"


def build_lookup_query(piece: dict[str, Any], doc: dict[str, Any] | None) -> dict[str, str]:
    """Assemble the fields injected into the lookup prompt."""
    candidates: dict[str, Any] = {}
    if doc and isinstance(doc.get("identity_candidates"), dict):
        candidates = doc["identity_candidates"]
    return {
        "title_guess": str(piece.get("piece_title_guess") or "unknown"),
        "catalog_number": str(piece.get("catalog_number") or "unknown"),
        "piece_folder": str(piece.get("piece_folder") or "unknown"),
        "identity_candidates": json.dumps(candidates, ensure_ascii=False) if candidates else "none",
        "observed_summary": _observed_summary(piece),
    }


def render_prompt(template: str, query: dict[str, str]) -> str:
    """Fill ``{placeholder}`` fields in the template, leaving JSON braces (``{{``) intact."""
    result = template
    for key, value in query.items():
        result = result.replace("{" + key + "}", value)
    return result


# --- Copilot CLI invocation ------------------------------------------------------------------


def build_cli_args(config: dict[str, Any], prompt: str) -> list[str]:
    """Build the ``copilot`` argument vector for a headless lookup."""
    command = shutil.which(config.get("command", "copilot")) or config.get("command", "copilot")
    args = [
        command,
        "-p", prompt,
        "--allow-all-tools",
        "--no-color",
        "--no-ask-user",
    ]
    # When streaming, leave the CLI verbose so its progress is visible; otherwise run silent so
    # only the final response reaches stdout.
    if not config.get("stream_output", True):
        args += ["-s", "--log-level", "none"]
    domains = config.get("allowed_domains") or []
    if domains:
        args.append("--allow-url=" + ",".join(str(d) for d in domains))
    else:
        args.append("--allow-all-urls")
    model = str(config.get("model") or "").strip()
    if model:
        args += ["--model", model]
    extra = config.get("extra_args") or []
    args += [str(a) for a in extra]
    return args


def parse_lookup_response(stdout: str) -> dict[str, Any]:
    """Extract and parse the single JSON result object from the CLI stdout.

    Prefers the sentinel-delimited block; falls back to the largest ``{...}`` span. Raises
    ``ValueError`` when no valid JSON object can be recovered.
    """
    text = stdout or ""
    if RESULT_START in text and RESULT_END in text:
        start = text.index(RESULT_START) + len(RESULT_START)
        end = text.index(RESULT_END, start)
        candidate = text[start:end].strip()
        candidate = candidate.strip("`").strip()
        return json.loads(candidate)

    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last <= first:
        raise ValueError("No JSON object found in lookup response.")
    return json.loads(text[first:last + 1])


def run_copilot_lookup(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
    """Invoke the Copilot CLI headlessly and return the parsed JSON result.

    Streams the CLI's output live to the log so a long-running lookup does not look frozen, while
    still capturing the full text for parsing. Isolated so tests can monkeypatch it; never called
    when lookup is disabled. Raises on a missing binary, non-zero exit, timeout, or unparseable
    output.
    """
    command = config.get("command", "copilot")
    if not shutil.which(command):
        raise FileNotFoundError(f"Copilot CLI '{command}' not found on PATH.")
    args = build_cli_args(config, prompt)
    timeout = float(config.get("timeout_seconds", 300) or 300)
    stream = bool(config.get("stream_output", True))

    logger.info(
        "Invoking Copilot CLI (model=%s); this can take up to %.0fs...",
        config.get("model") or "CLI default",
        timeout,
    )
    start = time.perf_counter()

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, _kill_on_timeout)
    watchdog.start()

    captured: list[str] = []
    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            captured.append(raw_line)
            line = raw_line.rstrip()
            if stream and line:
                logger.info("  copilot | %s", line)
            elif not stream:
                logger.debug("  copilot | %s", line)
        proc.wait()
    finally:
        watchdog.cancel()

    elapsed = time.perf_counter() - start
    stdout = "".join(captured)

    if timed_out.is_set():
        raise TimeoutError(f"Copilot CLI timed out after {timeout:.0f}s.")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Copilot CLI exited {proc.returncode} after {elapsed:.1f}s: "
            f"{stdout.strip()[-500:]}"
        )

    logger.info("Copilot CLI completed in %.1fs (%d chars captured).", elapsed, len(stdout))
    return parse_lookup_response(stdout)


# --- Expected-part normalization + reconciliation --------------------------------------------


def _coerce_index(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def normalize_expected_parts(raw_parts: Any) -> list[dict[str, Any]]:
    """Coerce lookup ``expected_parts`` into internal slot dicts.

    Output slots use the same shape as ``reconcile_parts`` expects: ``canonical``, ``part_index``,
    ``label``, ``section``, ``required``. Entries without a canonical instrument are dropped.
    """
    slots: list[dict[str, Any]] = []
    if not isinstance(raw_parts, list):
        return slots
    for entry in raw_parts:
        if not isinstance(entry, dict):
            continue
        canonical = entry.get("canonical_instrument") or entry.get("canonical")
        if not canonical or not isinstance(canonical, str):
            continue
        canonical = canonical.strip().lower()
        if not canonical:
            continue
        required = entry.get("required")
        slots.append({
            "canonical": canonical,
            "part_index": _coerce_index(entry.get("part_index")),
            "label": str(entry.get("label") or canonical),
            "section": entry.get("section"),
            "required": True if required is None else bool(required),
        })
    return slots


def _unexpected_entry(obs: dict[str, Any]) -> dict[str, Any]:
    return {
        "canonical_instrument": obs.get("canonical_instrument"),
        "part_index": obs.get("part_index"),
        "predicted_part": obs.get("predicted_part"),
        "count": obs.get("count", 1),
    }


def reconcile_parts(
    template_parts: list[dict[str, Any]],
    observed_parts: list[dict[str, Any]],
    equivalents: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match observed parts against expected slots.

    Returns (expected_parts, unexpected_parts). ``expected_parts`` preserves slot order and carries
    a ``present`` flag. Matching is count-based per canonical instrument (robust to null
    ``part_index``), preferring explicit index matches before consuming slots in order.
    ``equivalents`` maps a slot canonical to observed canonicals that should satisfy it.
    """
    alias_to_slot: dict[str, str] = {}
    for slot_canonical, aliases in (equivalents or {}).items():
        for alias in aliases:
            alias_to_slot[alias] = slot_canonical

    expected_idx_by_instr: dict[str, list[int]] = defaultdict(list)
    for i, slot in enumerate(template_parts):
        expected_idx_by_instr[slot["canonical"]].append(i)

    observed_by_instr: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for obs in observed_parts:
        canonical = obs.get("canonical_instrument")
        if canonical is not None:
            observed_by_instr[alias_to_slot.get(canonical, canonical)].append(obs)

    present_ids: set[int] = set()
    unexpected: list[dict[str, Any]] = []

    for canonical, slot_ids in expected_idx_by_instr.items():
        obs_list = observed_by_instr.get(canonical, [])
        consumed = {sid: False for sid in slot_ids}
        used = [False] * len(obs_list)

        # Pass 1: match by explicit part_index.
        for oi, obs in enumerate(obs_list):
            oidx = obs.get("part_index")
            if oidx is None:
                continue
            for sid in slot_ids:
                if not consumed[sid] and template_parts[sid].get("part_index") == oidx:
                    consumed[sid] = True
                    used[oi] = True
                    break

        # Pass 2: consume remaining observed against remaining slots in order.
        for oi in range(len(obs_list)):
            if used[oi]:
                continue
            for sid in slot_ids:
                if not consumed[sid]:
                    consumed[sid] = True
                    used[oi] = True
                    break

        for sid in slot_ids:
            if consumed[sid]:
                present_ids.add(sid)
        for oi, obs in enumerate(obs_list):
            if not used[oi]:
                unexpected.append(_unexpected_entry(obs))

    # Observed instruments with no expected slots at all.
    for canonical, obs_list in observed_by_instr.items():
        if canonical not in expected_idx_by_instr:
            unexpected.extend(_unexpected_entry(obs) for obs in obs_list)

    expected: list[dict[str, Any]] = []
    for i, slot in enumerate(template_parts):
        expected.append({
            "canonical_instrument": slot["canonical"],
            "part_index": slot.get("part_index"),
            "label": slot["label"],
            "section": slot.get("section"),
            "required": bool(slot.get("required")),
            "present": i in present_ids,
        })
    return expected, unexpected


def completeness_tier(score: float, missing_required: int, score_missing: bool) -> str:
    """Map the required-part completeness fraction onto a tier label."""
    if missing_required == 0 and not score_missing:
        return "complete"
    if score >= NEAR_COMPLETE:
        return "near_complete"
    if score >= INCOMPLETE:
        return "incomplete"
    return "severely_incomplete"


# --- Work identity ---------------------------------------------------------------------------


def build_work_identity(
    piece: dict[str, Any],
    doc: dict[str, Any] | None,
    lookup_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity: dict[str, Any] = {
        "title_guess": piece.get("piece_title_guess"),
        "catalog_number": piece.get("catalog_number"),
        "identity_candidates": {},
        "resolved": {},
    }
    if doc and isinstance(doc.get("identity_candidates"), dict):
        identity["identity_candidates"] = doc["identity_candidates"]
    if isinstance(lookup_identity, dict):
        identity["resolved"] = {
            k: v for k, v in lookup_identity.items() if v not in (None, "")
        }
    return identity


# --- Per-piece inference ---------------------------------------------------------------------


def _base_record(piece: dict[str, Any], run_id: str) -> dict[str, Any]:
    return {
        "record_version": RECORD_VERSION,
        "run_id": run_id,
        "piece_id": piece.get("piece_id"),
        "piece_folder": piece.get("piece_folder"),
        "catalog_number": piece.get("catalog_number"),
        "piece_title_guess": piece.get("piece_title_guess"),
        "has_score": bool(piece.get("has_score")),
        "processing_status": "success",
        "processing_timestamp": utc_now_iso(),
    }


def conservative_record(
    piece: dict[str, Any],
    doc: dict[str, Any] | None,
    run_id: str,
    lookup_status: str,
    *,
    evidence: list[dict[str, Any]] | None = None,
    identity_match_confidence: float = 0.0,
    lookup_notes: str = "",
    lookup_model: str = "",
    ensemble_type: str = "unknown",
    ensemble_display_name: str = "Unknown",
    lookup_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Observed-only record: asserts no missing parts; always flagged for review."""
    rec = _base_record(piece, run_id)
    rec.update({
        "ensemble_type": ensemble_type,
        "ensemble_display_name": ensemble_display_name,
        "detection_method": "conservative_fallback",
        "inference_method": "fallback_conservative",
        "lookup_status": lookup_status,
        "lookup_model": lookup_model,
        "lookup_notes": lookup_notes,
        "identity_match_confidence": round(float(identity_match_confidence or 0.0), 3),
        "authority_coverage": "none",
        "evidence_sources": evidence or [],
        "work_identity": build_work_identity(piece, doc, lookup_identity),
        "expected_parts": [],
        "missing_parts": [],
        "missing_required_parts": [],
        "missing_optional_parts": [],
        "unexpected_parts": [],
        "score_expected": False,
        "score_missing": False,
        "completeness_score": None,
        "completeness_tier": "unknown",
        "observed_instrument_count": len(observed_canonicals(piece)),
        "expected_part_count": 0,
        "missing_required_count": 0,
        "unexpected_part_count": 0,
        "needs_review": True,
    })
    return rec


def infer_piece_from_lookup(
    piece: dict[str, Any],
    doc: dict[str, Any] | None,
    run_id: str,
    result: dict[str, Any],
    lookup_model: str,
) -> dict[str, Any]:
    """Build a matched record by reconciling the looked-up parts against observed parts."""
    slots = normalize_expected_parts(result.get("expected_parts"))
    expected, unexpected = reconcile_parts(slots, piece.get("observed_parts", []))

    has_score = bool(piece.get("has_score"))
    score_expected = bool(result.get("score_expected"))
    score_missing = score_expected and not has_score
    rollup_needs_review = int(piece.get("needs_review_count", 0) or 0) > 0

    missing = [e for e in expected if not e["present"]]
    missing_required = [e["label"] for e in missing if e["required"]]
    missing_optional = [e["label"] for e in missing if not e["required"]]

    required_slots = [e for e in expected if e["required"]]
    required_total = len(required_slots) + (1 if score_expected else 0)
    required_present = sum(1 for e in required_slots if e["present"])
    required_present += 1 if (score_expected and has_score) else 0
    completeness_score = round(required_present / required_total, 3) if required_total else 1.0

    tier = completeness_tier(completeness_score, len(missing_required), score_missing)
    needs_review = bool(
        missing_required or unexpected or score_missing or rollup_needs_review
    )
    evidence = result.get("evidence_sources")
    evidence = evidence if isinstance(evidence, list) else []

    rec = _base_record(piece, run_id)
    rec.update({
        "ensemble_type": str(result.get("ensemble_type") or "unknown"),
        "ensemble_display_name": str(result.get("ensemble_display_name") or "Unknown"),
        "detection_method": "authority_lookup",
        "inference_method": "authority_lookup",
        "lookup_status": "matched",
        "lookup_model": lookup_model,
        "lookup_notes": str(result.get("notes") or ""),
        "identity_match_confidence": round(
            float(result.get("identity_match_confidence") or 0.0), 3
        ),
        "authority_coverage": "full" if evidence else "none",
        "evidence_sources": evidence,
        "work_identity": build_work_identity(piece, doc, result.get("work_identity")),
        "expected_parts": expected,
        "missing_parts": missing,
        "missing_required_parts": missing_required,
        "missing_optional_parts": missing_optional,
        "unexpected_parts": unexpected,
        "score_expected": score_expected,
        "score_missing": score_missing,
        "completeness_score": completeness_score,
        "completeness_tier": tier,
        "observed_instrument_count": len(observed_canonicals(piece)),
        "expected_part_count": len(expected),
        "missing_required_count": len(missing_required),
        "unexpected_part_count": len(unexpected),
        "needs_review": needs_review,
    })
    return rec


def infer_piece(
    piece: dict[str, Any],
    doc: dict[str, Any] | None,
    run_id: str,
    *,
    config: dict[str, Any],
    prompt_template: str,
    lookup_enabled: bool,
    lookup_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Infer expected parts for one piece via online lookup, degrading conservatively."""
    model = str(config.get("model") or "")
    if not lookup_enabled:
        return conservative_record(piece, doc, run_id, "disabled", lookup_model=model)

    query = build_lookup_query(piece, doc)
    prompt = render_prompt(prompt_template, query)
    try:
        result = lookup_fn(prompt, config)
    except Exception as exc:
        logger.warning("Lookup failed for piece %s: %s", piece.get("piece_id"), exc)
        return conservative_record(
            piece, doc, run_id, "error", lookup_notes=str(exc), lookup_model=model
        )

    if not isinstance(result, dict) or not result.get("match_found"):
        return conservative_record(
            piece, doc, run_id, "no_match",
            evidence=result.get("evidence_sources") if isinstance(result, dict) else None,
            lookup_notes=str(result.get("notes") or "") if isinstance(result, dict) else "",
            lookup_model=model,
            lookup_identity=result.get("work_identity") if isinstance(result, dict) else None,
        )

    confidence = float(result.get("identity_match_confidence") or 0.0)
    threshold = float(config.get("confidence_threshold", 0.5) or 0.0)
    if confidence < threshold:
        return conservative_record(
            piece, doc, run_id, "low_confidence",
            evidence=result.get("evidence_sources"),
            identity_match_confidence=confidence,
            lookup_notes=str(result.get("notes") or ""),
            lookup_model=model,
            ensemble_type=str(result.get("ensemble_type") or "unknown"),
            ensemble_display_name=str(result.get("ensemble_display_name") or "Unknown"),
            lookup_identity=result.get("work_identity"),
        )

    return infer_piece_from_lookup(piece, doc, run_id, result, model)


def build_error_record(piece: dict[str, Any], run_id: str, message: str) -> dict[str, Any]:
    rec = conservative_record(piece, None, run_id, "error", lookup_notes=message)
    rec["processing_status"] = "error"
    rec["detection_method"] = "error"
    rec["error_detail"] = message
    return rec


# --- Fingerprint + ordering ------------------------------------------------------------------


def piece_fingerprint(piece: dict[str, Any], config_fingerprint: str) -> str:
    """Stable fingerprint of the inputs that affect a piece's inference."""
    keys = sorted(
        f"{o.get('canonical_instrument')}|{o.get('part_index')}|{o.get('clef')}"
        for o in piece.get("observed_parts", [])
    )
    basis = f"{config_fingerprint}|{bool(piece.get('has_score'))}|" + ";".join(keys)
    return sha256_text(basis)


def config_fingerprint(config: dict[str, Any], prompt_template: str, lookup_enabled: bool) -> str:
    """Fingerprint of the lookup-affecting configuration (invalidates stale reuse)."""
    basis = "|".join([
        str(lookup_enabled),
        str(config.get("model") or ""),
        str(config.get("confidence_threshold")),
        sha256_text(prompt_template),
    ])
    return sha256_text(basis)


def _sort_key(record: dict[str, Any]) -> tuple[str, str]:
    catalog = record.get("catalog_number") or "~"
    return (catalog, record.get("piece_folder") or "")


# --- Reporting -------------------------------------------------------------------------------


def _pct(part: int, whole: int) -> float:
    return (100.0 * part / whole) if whole else 0.0


def _md_cell(value: str | None) -> str:
    return (value or "").replace("|", "\\|")


def build_report(records: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    """Render a human-readable Markdown summary of the expected-parts inference."""
    total = len(records)
    errors = [r for r in records if r["processing_status"] == "error"]

    tier_counts: dict[str, int] = {}
    ensemble_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    missing_part_counts: dict[str, int] = {}
    for rec in records:
        tier_counts[rec["completeness_tier"]] = tier_counts.get(rec["completeness_tier"], 0) + 1
        ensemble_counts[rec["ensemble_type"]] = ensemble_counts.get(rec["ensemble_type"], 0) + 1
        status = rec.get("lookup_status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        for label in rec.get("missing_required_parts", []):
            missing_part_counts[label] = missing_part_counts.get(label, 0) + 1

    needs_review = sum(1 for r in records if r.get("needs_review"))
    matched = status_counts.get("matched", 0)
    complete = tier_counts.get("complete", 0)

    overall = "OK"
    if errors:
        overall = "Errors present"
    elif needs_review:
        overall = "Review recommended"

    out: list[str] = []
    out.append("# Expected Parts Report")
    out.append("")
    out.append(
        f"_Generated {meta['generated_at']} - run `{meta['run_id']}` - "
        f"mode **{meta['mode']}** - {meta['elapsed_seconds']:.1f}s_"
    )
    out.append("")
    out.append(f"**Status:** {overall}")
    out.append("")

    out.append("## At a Glance")
    out.append("")
    out.append("| Metric | Value |")
    out.append("| --- | --- |")
    out.append(f"| Pieces in output | {total} |")
    out.append(f"| Processed this run | {meta['processed']} (reused {meta['reused']}) |")
    out.append(f"| Confident lookups | {matched} ({_pct(matched, total):.1f}%) |")
    out.append(f"| Complete pieces | {complete} ({_pct(complete, total):.1f}%) |")
    out.append(f"| Pieces needing review | {needs_review} |")
    out.append(f"| Processing errors | {len(errors)} |")
    out.append("")

    out.append("## Lookup Status")
    out.append("")
    out.append("| Status | Pieces |")
    out.append("| --- | --- |")
    for status in ("matched", "low_confidence", "no_match", "disabled", "error"):
        if status in status_counts:
            out.append(f"| {status} | {status_counts[status]} |")
    out.append("")

    out.append("## Completeness")
    out.append("")
    out.append("| Tier | Pieces |")
    out.append("| --- | --- |")
    for tier in ("complete", "near_complete", "incomplete", "severely_incomplete", "unknown"):
        if tier in tier_counts:
            out.append(f"| {tier} | {tier_counts[tier]} |")
    out.append("")

    out.append("## Ensembles")
    out.append("")
    out.append("| Ensemble | Pieces |")
    out.append("| --- | --- |")
    for name in sorted(ensemble_counts, key=lambda k: (-ensemble_counts[k], k)):
        out.append(f"| {_md_cell(name)} | {ensemble_counts[name]} |")
    out.append("")

    out.append("## Most Commonly Missing Parts")
    out.append("")
    if missing_part_counts:
        out.append("| Part | Pieces missing it |")
        out.append("| --- | --- |")
        ranked = sorted(missing_part_counts, key=lambda k: (-missing_part_counts[k], k))
        for label in ranked[:25]:
            out.append(f"| {_md_cell(label)} | {missing_part_counts[label]} |")
    else:
        out.append("No required parts are missing across the collection.")
    out.append("")

    out.append("## Per-Piece Breakdown")
    out.append("")
    limit = meta.get("detail_limit", 200)
    shown = sorted(records, key=_sort_key)[:limit]
    out.append("| Catalog | Piece | Ensemble | Lookup | Completeness | Missing required | Review |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for rec in shown:
        catalog = rec.get("catalog_number") or ""
        name = _md_cell(rec.get("piece_title_guess") or rec.get("piece_folder"))
        ensemble = _md_cell(rec.get("ensemble_type"))
        status = rec.get("lookup_status", "")
        score = rec.get("completeness_score")
        pct = "-" if score is None else f"{score * 100:.0f}% ({rec.get('completeness_tier')})"
        missing = _md_cell(", ".join(rec.get("missing_required_parts", [])) or "-")
        review = "yes" if rec.get("needs_review") else ""
        out.append(
            f"| {catalog} | {name} | {ensemble} | {status} | {pct} | {missing} | {review} |"
        )
    if len(records) > limit:
        out.append("")
        out.append(f"_Showing {limit} of {len(records)} pieces._")
    out.append("")

    out.append("## Configuration and Environment")
    out.append("")
    out.append("| Setting | Value |")
    out.append("| --- | --- |")
    out.append(f"| Rollup input | `{meta['pieces']}` |")
    out.append(f"| Config source | {meta['config_source']} |")
    out.append(f"| Prompt source | {meta['prompt_source']} |")
    out.append(f"| Lookup | {'enabled' if meta['lookup_enabled'] else 'disabled'} |")
    out.append(f"| Model | {meta['model'] or '(CLI default)'} |")
    out.append(f"| Output | `{meta['output']}` |")
    out.append("")

    return "\n".join(out) + "\n"


def _format_identity(resolved: dict[str, Any]) -> str:
    """One-line summary of the resolved edition identity, or empty when nothing is known."""
    if not isinstance(resolved, dict):
        return ""
    order = ["composer", "arranger", "publisher", "catalog_number", "year"]
    labels = {
        "composer": "composer",
        "arranger": "arr.",
        "publisher": "publisher",
        "catalog_number": "cat.",
        "year": "year",
    }
    parts = [
        f"{labels[key]} {resolved[key]}"
        for key in order
        if resolved.get(key) not in (None, "")
    ]
    return "; ".join(parts)


def build_instrumentation_report(records: list[dict[str, Any]], meta: dict[str, Any]) -> str:
    """Render a per-piece report of the expected instrumentation found by the LLM lookups.

    Unlike the summary report, this lists every expected part for each piece (canonical
    instrument, printed label, section, required/optional, and whether it was observed), plus the
    resolved edition identity and the evidence sources the lookup consulted. Pieces that fell back
    to a conservative record are listed with a note that no authoritative instrumentation was
    found.
    """
    matched = [r for r in records if r.get("lookup_status") == "matched"]

    out: list[str] = []
    out.append("# Expected Instrumentation by Piece")
    out.append("")
    out.append(
        f"_Generated {meta['generated_at']} - run `{meta['run_id']}` - "
        f"mode **{meta['mode']}** - model {meta['model'] or '(CLI default)'}_"
    )
    out.append("")
    out.append(
        f"Instrumentation below comes from online score lookups. "
        f"{len(matched)} of {len(records)} piece(s) have an authoritative instrumentation list; "
        f"the rest fell back to a conservative record (no expected parts asserted)."
    )
    out.append("")

    for rec in sorted(records, key=_sort_key):
        catalog = rec.get("catalog_number") or "?"
        title = rec.get("piece_title_guess") or rec.get("piece_folder") or rec.get("piece_id")
        out.append(f"## {catalog} - {title}")
        out.append("")

        ensemble = rec.get("ensemble_display_name") or "Unknown"
        ensemble_type = rec.get("ensemble_type") or "unknown"
        status = rec.get("lookup_status", "unknown")
        confidence = rec.get("identity_match_confidence")
        score = rec.get("completeness_score")
        pct = "-" if score is None else f"{score * 100:.0f}% ({rec.get('completeness_tier')})"

        out.append(f"- **Ensemble:** {_md_cell(ensemble)} (`{ensemble_type}`)")
        out.append(
            f"- **Lookup:** {status} - confidence {confidence} - completeness {pct}"
        )
        identity = _format_identity((rec.get("work_identity") or {}).get("resolved", {}))
        if identity:
            out.append(f"- **Edition:** {_md_cell(identity)}")
        if rec.get("lookup_notes"):
            out.append(f"- **Notes:** {_md_cell(rec['lookup_notes'])}")
        out.append("")

        expected_parts = rec.get("expected_parts") or []
        if expected_parts:
            out.append("| # | Instrument | Label | Section | Required | Observed |")
            out.append("| --- | --- | --- | --- | --- | --- |")
            for part in expected_parts:
                idx = part.get("part_index")
                idx_cell = "-" if idx is None else str(idx)
                required = "required" if part.get("required") else "optional"
                observed = "yes" if part.get("present") else "MISSING"
                out.append(
                    f"| {idx_cell} | {_md_cell(part.get('canonical_instrument'))} "
                    f"| {_md_cell(part.get('label'))} | {_md_cell(part.get('section'))} "
                    f"| {required} | {observed} |"
                )
            out.append("")
            unexpected = rec.get("unexpected_parts") or []
            if unexpected:
                labels = ", ".join(
                    _md_cell(u.get("predicted_part") or u.get("canonical_instrument"))
                    for u in unexpected
                )
                out.append(f"_Observed but not expected: {labels}_")
                out.append("")
        else:
            out.append(
                "_No authoritative instrumentation found "
                f"(lookup {status}); {rec.get('observed_instrument_count', 0)} "
                "instrument(s) observed in the library copy._"
            )
            out.append("")

        evidence = rec.get("evidence_sources") or []
        if evidence:
            out.append("**Sources:**")
            out.append("")
            for src in evidence:
                if not isinstance(src, dict):
                    continue
                stitle = _md_cell(src.get("title") or src.get("url") or "source")
                url = src.get("url") or ""
                snippet = _md_cell(src.get("snippet") or "")
                line = f"- [{stitle}]({url})" if url else f"- {stitle}"
                if snippet:
                    line += f' - "{snippet}"'
                out.append(line)
            out.append("")

    return "\n".join(out) + "\n"


# --- CLI -------------------------------------------------------------------------------------


@app.command()
def main(
    pieces: Path = typer.Option(
        Path("data/observed_parts_by_piece.jsonl"),
        help="Script 03 per-piece observed-parts rollup JSONL input",
    ),
    documents: Path = typer.Option(
        Path("data/documents.jsonl"), help="Script 02 per-document rollups (optional identity)"
    ),
    config_path: Path = typer.Option(
        Path("config/score_lookup.yaml"), "--config", help="Lookup engine config YAML (optional)"
    ),
    output: Path = typer.Option(
        Path("data/expected_parts.jsonl"), help="Expected-parts output JSONL"
    ),
    output_report: Path = typer.Option(
        Path("data/expected_parts_report.md"), help="Markdown summary output"
    ),
    output_instrumentation: Path = typer.Option(
        Path("data/expected_instrumentation.md"),
        help="Per-piece expected-instrumentation report output",
    ),
    write_report: bool = typer.Option(
        True, "--report/--no-report", help="Write the Markdown summary report"
    ),
    report_detail_limit: int = typer.Option(
        200, help="Max rows in the per-piece detail table of the report"
    ),
    mode: str = typer.Option("full", help="Processing mode: full or incremental"),
    lookup_enabled: bool = typer.Option(
        True, "--lookup/--no-lookup", help="Enable online score lookup (primary path)"
    ),
    model: str = typer.Option("", help="Override the Copilot CLI model (blank = config/default)"),
    timeout: int = typer.Option(0, help="Override per-piece subprocess timeout in seconds (0=cfg)"),
    stream_lookup: bool = typer.Option(
        True, "--stream-lookup/--no-stream-lookup",
        help="Stream the Copilot CLI's output live so long lookups don't look frozen",
    ),
    log_level: str = typer.Option("INFO", help="DEBUG, INFO, WARNING, ERROR"),
) -> None:
    """Infer expected parts per piece via online score lookup and flag missing parts."""
    mode = mode.lower().strip()
    if mode not in {"full", "incremental"}:
        raise typer.BadParameter("mode must be 'full' or 'incremental'")

    setup_logging(log_level)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start_time = time.perf_counter()

    pieces = pieces.resolve()
    output = output.resolve()

    piece_records = read_jsonl(pieces)
    if not piece_records:
        raise typer.BadParameter(f"No piece rollup records found at {pieces}")

    config, config_source = load_lookup_config(config_path.resolve())
    if not config.get("enabled", True):
        lookup_enabled = False
    if model.strip():
        config["model"] = model.strip()
    if timeout > 0:
        config["timeout_seconds"] = timeout
    config["stream_output"] = stream_lookup

    template_path = Path(config.get("prompt_template_path", "")).resolve()
    prompt_template, prompt_source = load_prompt_template(template_path)

    if lookup_enabled and not shutil.which(config.get("command", "copilot")):
        logger.warning(
            "Lookup enabled but '%s' is not on PATH; all pieces will use the conservative "
            "fallback.", config.get("command", "copilot"),
        )

    doc_map: dict[str, dict[str, Any]] = {}
    for rec in read_jsonl(documents.resolve()):
        piece_id = rec.get("piece_id")
        if piece_id and piece_id not in doc_map:
            doc_map[piece_id] = rec

    cfg_fp = config_fingerprint(config, prompt_template, lookup_enabled)

    checkpoint_path = get_checkpoint_path(output)
    checkpoint = read_json(checkpoint_path) or {}
    if checkpoint and checkpoint.get("record_version") != RECORD_VERSION:
        logger.warning(
            "Checkpoint version mismatch (found=%s expected=%s). Ignoring checkpoint.",
            checkpoint.get("record_version"),
            RECORD_VERSION,
        )
        checkpoint = {}
    previous_records = {
        rec["piece_id"]: rec
        for rec in read_jsonl(output)
        if rec.get("piece_id")
    }
    prior_fingerprints = checkpoint.get("fingerprints", {}) if mode == "incremental" else {}

    rebuilt: list[dict[str, Any]] = []
    processed = 0
    reused = 0
    fingerprints: dict[str, str] = {}

    total_pieces = sum(1 for p in piece_records if p.get("piece_id"))
    logger.info(
        "Processing %d piece(s) in %s mode (lookup %s).",
        total_pieces,
        mode,
        "enabled" if lookup_enabled else "disabled",
    )

    seen = 0
    for piece in piece_records:
        piece_id = piece.get("piece_id")
        if not piece_id:
            continue
        seen += 1
        fingerprint = piece_fingerprint(piece, cfg_fp)
        fingerprints[piece_id] = fingerprint

        title = piece.get("piece_title_guess") or piece.get("piece_folder") or piece_id
        catalog = piece.get("catalog_number") or "?"

        prior = previous_records.get(piece_id)
        if (
            mode == "incremental"
            and prior is not None
            and prior.get("record_version") == RECORD_VERSION
            and prior_fingerprints.get(piece_id) == fingerprint
        ):
            rebuilt.append(prior)
            reused += 1
            logger.info("[%d/%d] Reusing cached result: %s (cat %s)", seen, total_pieces,
                        title, catalog)
            continue

        logger.info("[%d/%d] %s: %s (cat %s)", seen, total_pieces,
                    "Looking up" if lookup_enabled else "Recording (no lookup)", title, catalog)
        piece_start = time.perf_counter()
        try:
            record = infer_piece(
                piece,
                doc_map.get(piece_id),
                run_id,
                config=config,
                prompt_template=prompt_template,
                lookup_enabled=lookup_enabled,
                lookup_fn=run_copilot_lookup,
            )
        except Exception as exc:
            logger.exception("Unexpected inference error on piece %s", piece_id)
            record = build_error_record(piece, run_id, str(exc))
        logger.info(
            "[%d/%d] Done: %s -> status=%s tier=%s (%.1fs)",
            seen, total_pieces, title,
            record.get("lookup_status"), record.get("completeness_tier"),
            time.perf_counter() - piece_start,
        )
        rebuilt.append(record)
        processed += 1

    rebuilt.sort(key=_sort_key)
    atomic_write_jsonl(output, rebuilt)

    if write_report:
        meta = {
            "generated_at": utc_now_iso(),
            "run_id": run_id,
            "mode": mode,
            "elapsed_seconds": time.perf_counter() - start_time,
            "processed": processed,
            "reused": reused,
            "pieces": pieces.as_posix(),
            "config_source": config_source,
            "prompt_source": prompt_source,
            "lookup_enabled": lookup_enabled,
            "model": config.get("model") or "",
            "output": output.as_posix(),
            "detail_limit": report_detail_limit,
        }
        report = build_report(rebuilt, meta)
        atomic_write_text(output_report.resolve(), report)
        logger.info("Wrote Markdown report: %s", output_report.resolve())

        instrumentation = build_instrumentation_report(rebuilt, meta)
        atomic_write_text(output_instrumentation.resolve(), instrumentation)
        logger.info("Wrote instrumentation report: %s", output_instrumentation.resolve())

    new_checkpoint = {
        "record_version": RECORD_VERSION,
        "last_run_id": run_id,
        "last_run_timestamp": utc_now_iso(),
        "pieces_input": pieces.as_posix(),
        "output": output.as_posix(),
        "config_source": config_source,
        "config_fingerprint": cfg_fp,
        "lookup_enabled": lookup_enabled,
        "fingerprints": fingerprints,
        "record_count": len(rebuilt),
    }
    atomic_write_json(checkpoint_path, new_checkpoint)

    logger.info(
        "Expected-parts inference completed: total=%d processed=%d reused=%d output=%s",
        len(rebuilt),
        processed,
        reused,
        output,
    )


if __name__ == "__main__":
    app()
