"""Script 04: infer the expected parts for each piece and flag missing parts.

Consumes the Script 03 per-piece observed-parts rollup (``data/observed_parts_by_piece.jsonl``)
and writes one expected-parts record per piece to ``data/expected_parts.jsonl`` plus a Markdown
report.

Inference runs in up to three stages, stopping at the first that yields a confident
instrumentation contract:

1. **Local score OCR** (``local_score_ocr``): if Script 03 flagged a local score PDF for the
   piece, read that score's text (reusing Script 02's extracted/OCR text, re-OCRing the leading
   pages only when that text is too thin) and ask the LLM to summarize it into the instrumentation
   contract.
2. **Online authority lookup** (``authority_lookup``): ask the GitHub Copilot CLI to find the
   *actual published score* online and return its real instrumentation from authoritative text
   sources, plus candidate score-image URLs. This is the previous behavior.
3. **Remote image OCR** (``score_image_ocr``): when the online lookup returns candidate score
   images but no instrumentation, download those images into a per-piece temp folder, OCR them
   locally, and summarize the OCR text into the contract.

When every stage is disabled, finds no confident match, or fails, the piece degrades to a
conservative, observed-only record flagged for review -- it never fabricates a "missing" part
without an authoritative source.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
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

# Optional imaging/OCR deps used only by the local-score re-OCR and remote-image OCR fallbacks.
# They degrade gracefully: when unavailable, those stages are skipped with a logged warning.
try:  # pragma: no cover - exercised only when the toolchain is installed
    import fitz  # type: ignore  # PyMuPDF, for rendering score PDF pages
except Exception:  # pragma: no cover - optional dependency
    fitz = None

try:  # pragma: no cover - exercised only when the toolchain is installed
    import pytesseract  # type: ignore
    from PIL import Image  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    pytesseract = None
    Image = None

from scripts._common import (
    COMPLETENESS_TIER_ORDER as TIER_ORDER,
)
from scripts._common import (
    LOOKUP_STATUS_ORDER as STATUS_ORDER,
)
from scripts._common import (
    CompletenessTier,
    LookupStatus,
    ProcessingStatus,
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    build_checkpoint,
    canonicalize_instrument,
    instrument_display_name,
    load_checkpoint,
    load_instrument_taxonomy,
    make_checkpoint_path,
    md_cell,
    new_record_envelope,
    normalize_instrument_name,
    only_piece_decision,
    pct,
    piece_sort_key,
    read_jsonl,
    run_with_progress,
    setup_logging,
    sha256_text,
    utc_now_iso,
)

RECORD_VERSION = "2.1"

CHECKPOINT_FILENAME = ".expected_parts_checkpoint.json"

app = typer.Typer(add_completion=False)

logger = logging.getLogger("script04.expected")

# Completeness tier thresholds (fraction of required parts present).
NEAR_COMPLETE = 0.85
INCOMPLETE = 0.5

# detection_method / inference_method values for the resolution paths.
METHOD_LOCAL_SCORE = "local_score_ocr"
METHOD_WINDREP = "windrep_lookup"
METHOD_AUTHORITY = "authority_lookup"
METHOD_SCORE_IMAGE = "score_image_ocr"
METHOD_FALLBACK = "conservative_fallback"

# Human-readable labels for each resolution path, surfaced on the web dashboard as the
# "where did this instrumentation come from?" provenance.
METHOD_LABELS = {
    METHOD_LOCAL_SCORE: "OCR of local score",
    METHOD_WINDREP: "Web lookup (Wind Repertory Project)",
    METHOD_AUTHORITY: "Web lookup (authority source)",
    METHOD_SCORE_IMAGE: "OCR of score image from web",
    METHOD_FALLBACK: "No authoritative source (observed parts only)",
}

# Order Script 03 score types are preferred when picking a piece's best local score to OCR.
SCORE_TYPE_PREFERENCE = {"full": 0, "conductor": 1, "condensed": 2, "short": 3}

# Shared instrument taxonomy (canonical tokens + section map), loaded once from the same YAML
# lexicon Script 03 uses so LLM-derived expected parts canonicalize identically to observed parts.
_RULES_PATH = Path(__file__).resolve().parent.parent / "config" / "regex_rules.yaml"
_INSTRUMENT_TAXONOMY: dict[str, Any] | None = None


def _instrument_taxonomy() -> dict[str, Any]:
    """Return the cached instrument taxonomy (alias->canonical and canonical->section maps)."""
    global _INSTRUMENT_TAXONOMY
    if _INSTRUMENT_TAXONOMY is None:
        _INSTRUMENT_TAXONOMY = load_instrument_taxonomy(_RULES_PATH)
    return _INSTRUMENT_TAXONOMY


# Sentinels the lookup prompt wraps its JSON result in.
RESULT_START = "<<<SCORE_JSON>>>"
RESULT_END = "<<<END_SCORE_JSON>>>"

# Cap on the assembled multi-pass score OCR text handed to the score-OCR summarize prompt.
SCORE_OCR_MAX_PROMPT_CHARS = 16000

# --- Built-in lookup config (YAML overrides/extends it) --------------------------------------

DEFAULT_LOOKUP_CONFIG: dict[str, Any] = {
    "enabled": True,
    "command": "copilot",
    "model": "",
    "timeout_seconds": 600,
    # Emit a "still processing" heartbeat to the log every N seconds during a lookup so a long,
    # silent CLI call does not look frozen. Set to 0 to disable.
    "heartbeat_seconds": 30,
    "confidence_threshold": 0.5,
    "allowed_domains": [],
    "prompt_template_path": "config/llm_prompts/lookup_instrumentation.txt",
    # Phase 2 of the external lookup: fetch the identified edition's instrumentation WITHOUT any
    # library-holdings context, so `expected_parts` reflect the published edition, never the parts
    # this library happens to own. Phase 1 (identify) uses `prompt_template` above; this template is
    # a clean second pass seeded only with the resolved identity + source URLs.
    "instrumentation_prompt_template_path": "config/llm_prompts/lookup_instrumentation_parts.txt",
    "stream_output": True,
    "extra_args": [],
    # Local score OCR (Stage A). When the leading pages were OCR'd, Script 02's multi-pass OCR
    # candidates are reused and all passes are handed to the LLM (via the score-OCR prompt) to
    # reconcile, instead of trusting a single word-count-weighted pass or re-OCRing from scratch.
    "local_score_enabled": True,
    "summarize_prompt_template_path": "config/llm_prompts/summarize_score_instrumentation.txt",
    "summarize_score_ocr_prompt_template_path":
        "config/llm_prompts/summarize_score_ocr_instrumentation.txt",
    "max_score_pages": 2,
    "min_score_text_chars": 200,
    "reocr_dpi": 300,
    # Stage A quality gates. Every local score type (full, conductor, condensed, short) is used as
    # an instrumentation source: even an abridged edition almost always prints the full
    # instrumentation list in its front matter, and a full score is the most authoritative source
    # of all. Score type only governs whether a conductor score appears in the *displayed* full
    # instrumentation elsewhere -- a separate concern from discovering instrumentation here -- so
    # nothing is excluded by default. The knob stays available for callers that want to suppress a
    # type. A scanned score is skipped only when even its *best* OCR pass over the read pages is
    # below this floor (mean confidence, 0..1 scale); such pages are treated as unreadable and fall
    # through to the online lookup.
    "local_score_types_excluded": [],
    "min_score_ocr_confidence": 0.4,
    # Remote image OCR (Stage C).
    "image_ocr_enabled": True,
    # WindRep stage (runs between local score OCR and the general online lookup). Always tried;
    # not gated on ensemble type. W1 is a direct MediaWiki fetch bounded by windrep_timeout_seconds
    # (graceful fallback); W2 is a dedicated LLM lookup constrained to windrep.org.
    "windrep_enabled": True,
    "windrep_direct_enabled": True,
    "windrep_lookup_enabled": True,
    "windrep_timeout_seconds": 10,
    "windrep_api_url": "https://www.windrep.org/api.php",
    "windrep_prompt_template_path": "config/llm_prompts/windrep_lookup_instrumentation.txt",
    "windrep_cache_dir": "cache/windrep",
    "llm_cache_enabled": True,
    "llm_cache_dir": "cache/llm/results",
    "llm_cache_version": 1,
    # When live windrep.org is unreachable (e.g. IP-blocked), let the W2 LLM lookup also consult
    # the Wayback Machine snapshot of the work page. Adds the archive hosts to the W2 URL allowlist
    # and instructs the prompt to fall back to the latest archived copy.
    "windrep_wayback_enabled": True,
    "windrep_wayback_domains": ["web.archive.org", "archive.org"],
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
    "Report score/instrumentation image URLs (direct .jpg/.png or full-resolution image-endpoint\n"
    "URLs) in candidate_score_images (do NOT OCR them yourself; a local step reads them). Only\n"
    "populate expected_parts from authoritative text. If you match an edition but cannot find its\n"
    "instrumentation in text, you MUST still return candidate_score_images (title, instrumentation,\n"
    "contents, or score-first-page images) so the local OCR step can recover the parts -- do not\n"
    "return match_found=true with both expected_parts and candidate_score_images empty.\n\n"
    "A dedicated earlier stage already checked the Wind Repertory Project (windrep.org); do not\n"
    "depend on it here -- prefer publisher, distributor, and library-catalog sources.\n\n"
    "Respond with exactly one JSON object between the sentinel lines and nothing else:\n"
    f"{RESULT_START}\n"
    '{{"match_found": true, "identity_match_confidence": 0.0, "ensemble_type": "",\n'
    '  "ensemble_display_name": "", "score_expected": true,\n'
    '  "work_identity": {{"title": null, "composer": null, "arranger": null,\n'
    '    "publisher": null, "catalog_number": null, "year": null, "summary": null}},\n'
    '  "expected_parts": [], "candidate_score_images": [], "evidence_sources": [], "notes": ""}}\n'
    f"{RESULT_END}\n"
)


DEFAULT_INSTRUMENTATION_TEMPLATE = (
    "You are a music librarian assistant. The published edition of one piece has ALREADY been\n"
    "identified. Report ONLY its real, edition-specific instrumentation from authoritative online\n"
    "text sources.\n\n"
    "Identified edition:\n"
    "- Title guess: {title_guess}\n"
    "- Catalog / item number: {catalog_number}\n"
    "- Resolved work identity: {work_identity}\n"
    "- Identity candidates (composer/arranger/publisher/year): {identity_candidates}\n"
    "- Ensemble type: {ensemble_type}\n"
    "- Source URLs already found: {source_urls}\n\n"
    "Report one entry per named printed part with a lowercase snake_case canonical_instrument, a\n"
    "part_index (or null), a label, a section, and a required flag. Set required=false only for\n"
    "parts a source explicitly marks optional/ad lib/substitute/alternative/cue-only/doubling;\n"
    "otherwise true. Never invent an edition, instrument, source, or URL.\n\n"
    "IMPORTANT: base the instrumentation ONLY on the published edition and authoritative sources.\n"
    "No information about which parts any particular library happens to hold is provided or should\n"
    "be assumed; the instrumentation must reflect the published score alone.\n\n"
    "If you cannot find the instrumentation in authoritative text, return expected_parts empty (a\n"
    "later step reads score images); do not guess.\n\n"
    "Respond with exactly one JSON object between the sentinel lines and nothing else:\n"
    f"{RESULT_START}\n"
    '{{"match_found": true, "identity_match_confidence": 0.0, "ensemble_type": "",\n'
    '  "ensemble_display_name": "", "score_expected": true,\n'
    '  "expected_parts": [], "evidence_sources": [], "notes": ""}}\n'
    f"{RESULT_END}\n"
)


DEFAULT_SUMMARIZE_TEMPLATE = (
    "You are a music librarian assistant. Read the score text below (OCR or embedded text from a\n"
    "score's first page(s)) and report the piece's instrumentation as one JSON object. Do not\n"
    "search the web; treat the supplied text as the authoritative source.\n\n"
    "Piece context (disambiguation only):\n"
    "- Title guess: {title_guess}\n"
    "- Catalog / item number: {catalog_number}\n"
    "- Source folder name: {piece_folder}\n\n"
    "Score text:\n"
    "{score_text}\n\n"
    "Report one expected_parts entry per named printed part with a lowercase snake_case\n"
    "canonical_instrument, a part_index (or null), a label, a section, and a required flag. Infer\n"
    "ensemble_type from the instrumentation you read. If the text does not clearly show\n"
    "instrumentation, set match_found to false and explain in notes. Never invent instruments.\n\n"
    "Respond with exactly one JSON object between the sentinel lines and nothing else:\n"
    f"{RESULT_START}\n"
    '{{"match_found": true, "identity_match_confidence": 0.0, "ensemble_type": "",\n'
    '  "ensemble_display_name": "", "score_expected": true,\n'
    '  "work_identity": {{"title": null, "composer": null, "arranger": null,\n'
    '    "publisher": null, "catalog_number": null, "year": null, "summary": null}},\n'
    '  "expected_parts": [], "evidence_sources": [], "notes": ""}}\n'
    f"{RESULT_END}\n"
)


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


def load_summarize_template(template_path: Path) -> tuple[str, str]:
    """Return (template, source) for the score-text summarization prompt.

    Falls back to the built-in summarize template when the file is missing or unreadable.
    """
    if not template_path.exists():
        logger.warning(
            "Summarize template %s not found; using built-in summarize prompt.", template_path
        )
        return DEFAULT_SUMMARIZE_TEMPLATE, "builtin"
    try:
        return template_path.read_text(encoding="utf-8"), "file"
    except Exception as exc:
        logger.warning(
            "Failed to read %s (%s); using built-in summarize prompt.", template_path, exc
        )
        return DEFAULT_SUMMARIZE_TEMPLATE, "builtin"


# --- Query + prompt building -----------------------------------------------------------------


def observed_canonicals(piece: dict[str, Any]) -> set[str]:
    """Distinct non-null canonical instruments observed in a piece rollup."""
    result: set[str] = set()
    for obs in piece.get("observed_parts", []):
        for facet in obs.get("instruments", []):
            canonical = facet.get("canonical")
            if canonical:
                result.add(canonical)
    return result


def _observed_summary(piece: dict[str, Any]) -> str:
    """Compact human-readable summary of the observed instruments and counts."""
    parts: list[str] = []
    for obs in piece.get("observed_parts", []):
        facets = obs.get("instruments", [])
        if not facets:
            continue
        labels: list[str] = []
        for facet in facets:
            canonical = facet.get("canonical")
            if not canonical:
                continue
            idx = facet.get("part_index")
            labels.append(canonical if idx is None else f"{canonical} {idx}")
        if not labels:
            continue
        label = " / ".join(labels)
        count = obs.get("count", 1)
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


def build_instrumentation_query(
    identity_result: dict[str, Any], piece: dict[str, Any], doc: dict[str, Any] | None = None
) -> dict[str, str]:
    """Assemble the phase-2 (instrumentation) prompt fields from a resolved identity.

    Deliberately carries NO library-holdings information (`observed_summary`): the second pass sees
    only the edition identity and the source/image URLs found in phase 1, so the returned
    instrumentation reflects the published edition and can never be seeded by the parts this library
    holds. The document's ``identity_candidates`` (composer/arranger/publisher/year scraped from the
    score) ARE passed through as a disambiguation fallback -- they are work-identity metadata, not
    holdings -- so phase 2 still knows the composer/arranger even if phase 1 did not resolve them.
    """
    identity = identity_result.get("work_identity")
    identity = identity if isinstance(identity, dict) else {}
    sources = identity_result.get("evidence_sources")
    urls = [
        str(s.get("url")).strip()
        for s in (sources if isinstance(sources, list) else [])
        if isinstance(s, dict) and str(s.get("url") or "").strip()
    ]
    candidates: dict[str, Any] = {}
    if doc and isinstance(doc.get("identity_candidates"), dict):
        candidates = doc["identity_candidates"]
    return {
        "title_guess": str(piece.get("piece_title_guess") or "unknown"),
        "catalog_number": str(piece.get("catalog_number") or "unknown"),
        "piece_folder": str(piece.get("piece_folder") or "unknown"),
        "work_identity": json.dumps(identity, ensure_ascii=False) if identity else "unknown",
        "identity_candidates": json.dumps(candidates, ensure_ascii=False) if candidates else "none",
        "ensemble_type": str(identity_result.get("ensemble_type") or "unknown"),
        "source_urls": json.dumps(urls, ensure_ascii=False) if urls else "none",
    }


def render_prompt(template: str, query: dict[str, str]) -> str:
    """Fill ``{placeholder}`` fields in the template, leaving JSON braces (``{{``) intact."""
    result = template
    for key, value in query.items():
        result = result.replace("{" + key + "}", value)
    return result


# --- Copilot CLI invocation ------------------------------------------------------------------


def _resolve_cli_command(configured: str) -> str:
    """Resolve the copilot executable, preferring the real binary over a cmd.exe batch shim.

    On Windows ``shutil.which('copilot')`` resolves to a ``.bat`` shim whose directory sits early on
    ``PATH``. Launching a ``.bat`` runs it through ``cmd.exe``, whose command line is capped at
    ~8191 characters, so a large prompt (with embedded score/OCR text) fails outright with
    "The command line is too long". Re-resolving with the shim's own directory removed from ``PATH``
    finds the real executable -- exactly what the shim's bootstrapper does internally -- which is
    then launched directly via ``CreateProcess`` (limit ~32767) and comfortably fits these prompts.
    Falls back to the shim (or the configured name) when no alternative is found, so behaviour is
    unchanged wherever the batch limit is not a problem.
    """
    resolved = shutil.which(configured)
    if not resolved:
        return configured
    if not resolved.lower().endswith((".bat", ".cmd")):
        return resolved
    shim_dir = os.path.normcase(os.path.dirname(resolved).rstrip("\\/"))
    remaining = [
        d
        for d in os.environ.get("PATH", "").split(os.pathsep)
        if d and os.path.normcase(d.rstrip("\\/")) != shim_dir
    ]
    return shutil.which(configured, path=os.pathsep.join(remaining)) or resolved


def build_cli_args(config: dict[str, Any], prompt: str) -> list[str]:
    """Build the ``copilot`` argument vector for a headless lookup."""
    command = _resolve_cli_command(config.get("command", "copilot"))
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


def _loads_json_lenient(candidate: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating trailing commas the model sometimes emits.

    Tries strict parsing first; on failure retries once after stripping trailing commas before
    ``}``/``]`` (a safe, common LLM glitch). Other malformations (e.g. unescaped quotes inside a
    string value) are not repaired -- they re-raise so the caller can log and degrade. Raises
    ``ValueError`` with a payload snippet so the failure is diagnosable from the logs.
    """
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        repaired = re.sub(r",(\s*[}\]])", r"\1", candidate)
        if repaired != candidate:
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                pass
        snippet = candidate.replace("\n", "\\n")
        if len(snippet) > 400:
            snippet = snippet[:400] + "..."
        raise ValueError(f"{exc}; payload was: {snippet}") from exc


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
        return _loads_json_lenient(candidate)

    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last <= first:
        raise ValueError("No JSON object found in lookup response.")
    return _loads_json_lenient(text[first:last + 1])


class LLMResultCache:
    """Reusable LLM result cache for prompt-based lookups.

    The cache is intentionally keyed to the actual prompt plus relevant config values, so a piece
    whose observation set changes gets a different prompt and bypasses stale results automatically.
    Each cache payload is versioned so the script can safely ignore older or incompatible cache
    files without risking stale LLM responses. This makes the layer easy to extract into another
    module or disable entirely by setting ``llm_cache_enabled`` to ``False``.
    """

    DEFAULT_VERSION = 1

    def __init__(self, base_dir: str | Path = "cache/llm/results") -> None:
        self.base_dir = Path(base_dir)

    @staticmethod
    def _config_fingerprint(config: dict[str, Any]) -> str:
        payload = {
            "piece_id": config.get("piece_id"),
            "command": config.get("command", "copilot"),
            "model": config.get("model") or "",
            "allowed_domains": list(config.get("allowed_domains") or []),
            "llm_cache_version": config.get("llm_cache_version", LLMResultCache.DEFAULT_VERSION),
        }
        return sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    @classmethod
    def _cache_version(cls, config: dict[str, Any]) -> int:
        try:
            return int(config.get("llm_cache_version", cls.DEFAULT_VERSION) or cls.DEFAULT_VERSION)
        except (TypeError, ValueError):
            return cls.DEFAULT_VERSION

    def cache_path(self, prompt: str, config: dict[str, Any]) -> Path:
        payload = {
            "piece_id": config.get("piece_id"),
            "command": config.get("command", "copilot"),
            "model": config.get("model") or "",
            "allowed_domains": list(config.get("allowed_domains") or []),
            "llm_cache_version": self._cache_version(config),
            "config_fingerprint": self._config_fingerprint(config),
            "prompt": prompt,
        }
        digest = sha256_text(json.dumps(payload, ensure_ascii=False, sort_keys=True)).replace(":", "_")
        return self.base_dir / f"{digest}.json"

    def load(self, prompt: str, config: dict[str, Any]) -> dict[str, Any] | None:
        if not bool(config.get("llm_cache_enabled", True)):
            return None
        if bool(config.get("force_refresh", False)):
            return None
        path = self.cache_path(prompt, config)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("cache_version") != self._cache_version(config):
            return None
        expected_fingerprint = self._config_fingerprint(config)
        if payload.get("config_fingerprint") not in (None, expected_fingerprint):
            return None
        result = payload.get("result")
        return result if isinstance(result, dict) else None

    def save(self, prompt: str, config: dict[str, Any], result: dict[str, Any]) -> None:
        if not bool(config.get("llm_cache_enabled", True)):
            return
        path = self.cache_path(prompt, config)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_json(
                path,
                {
                    "cache_version": self._cache_version(config),
                    "config_fingerprint": self._config_fingerprint(config),
                    "piece_id": config.get("piece_id"),
                    "cached_at": utc_now_iso(),
                    "result": result,
                },
            )
        except OSError:
            logger.debug("Lookup cache write failed for %s", path)


def lookup_cache_path(prompt: str, config: dict[str, Any]) -> Path:
    """Backward-compatible wrapper for the concrete cache path used by Script 04."""
    return LLMResultCache(config.get("llm_cache_dir") or "cache/llm/results").cache_path(
        prompt, config
    )


def load_cached_lookup_result(prompt: str, config: dict[str, Any]) -> dict[str, Any] | None:
    """Backward-compatible wrapper for loading a cached result."""
    return LLMResultCache(config.get("llm_cache_dir") or "cache/llm/results").load(
        prompt, config
    )


def save_cached_lookup_result(prompt: str, config: dict[str, Any], result: dict[str, Any]) -> None:
    """Backward-compatible wrapper for persisting a cached result."""
    LLMResultCache(config.get("llm_cache_dir") or "cache/llm/results").save(prompt, config, result)


def run_copilot_lookup(prompt: str, config: dict[str, Any]) -> dict[str, Any]:
    """Invoke the Copilot CLI headlessly and return the parsed JSON result.

    Streams the CLI's output live to the log so a long-running lookup does not look frozen, while
    still capturing the full text for parsing. Isolated so tests can monkeypatch it; never called
    when lookup is disabled. Raises on a missing binary, non-zero exit, timeout, or unparseable
    output.
    """
    cache = LLMResultCache(config.get("llm_cache_dir") or "cache/llm/results")
    cached = cache.load(prompt, config)
    if cached is not None:
        logger.info("[%s] Using cached Copilot lookup result for prompt hash %s.",
                    config.get("piece_id"), sha256_text(prompt))
        return cached

    command = config.get("command", "copilot")
    if not shutil.which(command):
        raise FileNotFoundError(f"Copilot CLI '{command}' not found on PATH.")
    args = build_cli_args(config, prompt)
    timeout = float(config.get("timeout_seconds", 600) or 600)
    stream = bool(config.get("stream_output", True))
    piece_id = config.get("piece_id")
    prefix = f"[{piece_id}] " if piece_id else ""

    logger.info(
        "%sInvoking Copilot CLI (model=%s); this can take up to %.0fs...",
        prefix,
        config.get("model") or "CLI default",
        timeout,
    )
    start = time.perf_counter()

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )

    timed_out = threading.Event()

    def _kill_on_timeout() -> None:
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout, _kill_on_timeout)
    watchdog.start()

    # Heartbeat: a daemon thread that logs elapsed time on an interval so a long, silent lookup
    # does not look frozen. It runs in parallel with the blocking stdout read below and is stopped
    # the instant the call completes. ``Event.wait`` sleeps in interruptible chunks, so completion
    # never has to wait out a full interval.
    heartbeat_interval = float(config.get("heartbeat_seconds", 30) or 0)
    stop_heartbeat = threading.Event()

    def _heartbeat() -> None:
        while not stop_heartbeat.wait(heartbeat_interval):
            waited = time.perf_counter() - start
            logger.info(
                "%sstill processing... Copilot CLI running for %.0fs (limit %.0fs).",
                prefix,
                waited,
                timeout,
            )

    heartbeat_thread: threading.Thread | None = None
    if heartbeat_interval > 0:
        heartbeat_thread = threading.Thread(
            target=_heartbeat, name="copilot-heartbeat", daemon=True
        )
        heartbeat_thread.start()

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
        stop_heartbeat.set()
        if heartbeat_thread is not None:
            heartbeat_thread.join(timeout=1.0)

    elapsed = time.perf_counter() - start
    stdout = "".join(captured)

    if timed_out.is_set():
        raise TimeoutError(f"Copilot CLI timed out after {timeout:.0f}s.")
    if proc.returncode != 0:
        raise RuntimeError(
            f"Copilot CLI exited {proc.returncode} after {elapsed:.1f}s: "
            f"{stdout.strip()[-500:]}"
        )

    logger.info(
        "%sCopilot CLI completed in %.1fs (%d chars captured).", prefix, elapsed, len(stdout)
    )
    result = parse_lookup_response(stdout)
    cache.save(prompt, config, result)
    return result


def _log_lookup_summary(piece_id: Any, result: Any) -> None:
    """Log a one-line summary of the lookup result so Stage C decisions are explainable.

    This surfaces *why* the remote-image stage does or does not run: it reports whether the model
    matched an edition, its confidence, how many parts it returned, and how many candidate score
    images it offered for download.
    """
    if not isinstance(result, dict):
        logger.info("[%s] Stage B result: lookup returned no JSON object", piece_id)
        return
    parts = result.get("expected_parts")
    part_count = len(parts) if isinstance(parts, list) else 0
    image_count = len(extract_image_urls(result))
    logger.info(
        "[%s] Stage B result: match_found=%s confidence=%.2f expected_parts=%d "
        "candidate_score_images=%d",
        piece_id,
        bool(result.get("match_found")),
        float(result.get("identity_match_confidence") or 0.0),
        part_count,
        image_count,
    )


def _safe_filename(value: Any, fallback: str = "piece") -> str:
    """Return a filesystem-safe slug for use in a debug filename."""
    slug = "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(value or ""))
    return slug or fallback


def _persist_lookup_result(config: dict[str, Any], piece_id: Any, prompt: str, result: Any) -> None:
    """Persist the parsed lookup result (with the prompt) for auditing, if a debug dir is set.

    Writes one JSON file per piece so a run can be inspected after the fact -- in particular to see
    exactly what ``candidate_score_images`` (if any) the model returned. Best-effort: never fails
    the inference.
    """
    debug_dir = config.get("lookup_debug_dir")
    if not debug_dir:
        return
    try:
        out_dir = Path(debug_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "piece_id": piece_id,
            "captured_at": utc_now_iso(),
            "prompt": prompt,
            "result": result,
        }
        dest = out_dir / f"lookup_{_safe_filename(piece_id)}.json"
        atomic_write_json(dest, payload)
        logger.info("[%s] Saved raw lookup result: %s", piece_id, dest)
    except Exception as exc:
        logger.debug("Could not persist lookup result for %s: %s", piece_id, exc)


# --- WindRep stage helpers (Stage W: between local score OCR and the general online lookup) ---

WINDREP_USER_AGENT = "music-library-analyser (concert/wind band library tooling)"


def _windrep_title_variations(query: dict[str, Any]) -> list[str]:
    """Candidate search terms for a piece, most-specific first.

    WindRep pages are titled by work, so we try the parsed title, a leading-article-stripped
    variant, and the source folder name with any leading catalog number removed.
    """
    variants: list[str] = []
    title = str(query.get("title_guess") or "").strip()
    if title and title.lower() != "unknown":
        variants.append(title)
        if title.lower().startswith("the "):
            variants.append(title[4:].strip())
    folder = str(query.get("piece_folder") or "").strip()
    if folder and folder.lower() != "unknown":
        stripped = re.sub(r"^\d+\s+", "", folder).strip()
        variants.append(stripped or folder)
    seen: list[str] = []
    for variant in variants:
        if variant and variant not in seen:
            seen.append(variant)
    return seen


def _windrep_api_get(api_url: str, params: dict[str, str], deadline: float) -> Any:
    """One MediaWiki API call, bounded by the remaining time budget.

    ``deadline`` is a ``time.monotonic()`` timestamp. Raises ``TimeoutError`` when too little time
    remains, and uses the remaining budget as the socket timeout so a hung/unreachable WindRep
    cannot exceed the overall budget.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0.5:
        raise TimeoutError("WindRep time budget exhausted")
    url = api_url + "?" + urllib.parse.urlencode({**params, "format": "json"})
    req = urllib.request.Request(url, headers={"User-Agent": WINDREP_USER_AGENT})
    with urllib.request.urlopen(req, timeout=remaining) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _windrep_cache_path(config: dict[str, Any], query: dict[str, Any]) -> Path | None:
    cache_dir = config.get("windrep_cache_dir")
    if not cache_dir:
        return None
    key = _safe_filename(query.get("piece_folder") or query.get("title_guess") or "piece")
    return Path(cache_dir) / f"{key}.json"


def _windrep_cache_read(config: dict[str, Any], query: dict[str, Any]) -> str | None:
    path = _windrep_cache_path(config, query)
    if not path or not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    text = data.get("text") if isinstance(data, dict) else None
    return text if isinstance(text, str) and text.strip() else None


def _windrep_cache_write(
    config: dict[str, Any], query: dict[str, Any], page: str, text: str
) -> None:
    path = _windrep_cache_path(config, query)
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            path,
            {"query": query, "matched_page": page, "text": text, "fetched_at": utc_now_iso()},
        )
    except OSError as exc:
        logger.debug("WindRep cache write failed: %s", exc)


def fetch_windrep_instrumentation(query: dict[str, Any], config: dict[str, Any]) -> str | None:
    """Resolve a piece to a WindRep work page and return its instrumentation text, or None.

    Tries a few title variations against the WindRep MediaWiki API (opensearch, then full-text
    search), then extracts the page's Instrumentation section (falling back to the whole page). The
    whole attempt is bounded by ``windrep_timeout_seconds`` (default 10s) so an unreachable or
    geoblocked WindRep degrades gracefully to the next stage instead of blocking the run; on any
    failure this returns None rather than raising. Successful fetches are cached under
    ``windrep_cache_dir``.
    """
    cached = _windrep_cache_read(config, query)
    if cached is not None:
        logger.info("WindRep: using cached page text")
        return cached

    api_url = str(config.get("windrep_api_url") or "https://www.windrep.org/api.php")
    budget = float(config.get("windrep_timeout_seconds", 10) or 10)
    deadline = time.monotonic() + budget
    try:
        page: str | None = None
        for variant in _windrep_title_variations(query):
            data = _windrep_api_get(
                api_url, {"action": "opensearch", "limit": "5", "search": variant}, deadline
            )
            candidates = data[1] if isinstance(data, list) and len(data) > 1 else []
            if not candidates:
                data = _windrep_api_get(
                    api_url,
                    {"action": "query", "list": "search", "srlimit": "5", "srsearch": variant},
                    deadline,
                )
                hits = (((data or {}).get("query") or {}).get("search")) or []
                candidates = [h.get("title", "") for h in hits if isinstance(h, dict)]
            candidates = [c for c in candidates if c]
            if candidates:
                page = candidates[0]
                break
        if not page:
            logger.info("WindRep: no matching work page for query")
            return None

        sections = _windrep_api_get(
            api_url, {"action": "parse", "page": page, "prop": "sections"}, deadline
        )
        section_index: str | None = None
        for sec in (((sections or {}).get("parse") or {}).get("sections")) or []:
            if isinstance(sec, dict) and re.search(
                r"instrument", str(sec.get("line", "")), re.IGNORECASE
            ):
                section_index = str(sec.get("index"))
                break
        parse_params = {"action": "parse", "page": page, "prop": "wikitext"}
        if section_index is not None:
            parse_params["section"] = section_index
        parsed = _windrep_api_get(api_url, parse_params, deadline)
        wikitext = (((parsed or {}).get("parse") or {}).get("wikitext")) or {}
        body = wikitext.get("*", "") if isinstance(wikitext, dict) else ""
        if not isinstance(body, str) or not body.strip():
            return None
        text = f"WindRep work page: {page}\n\n{body}"
        _windrep_cache_write(config, query, page, text)
        return text
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as exc:
        logger.info("WindRep direct fetch unavailable (%s); falling through to next stage", exc)
        return None


# --- Local score text (Stage A) --------------------------------------------------------------


def find_best_score_doc(
    piece_id: str, score_docs_by_piece: dict[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """Return the piece's most-informative local score document, or None.

    Prefers a full score, then conductor/condensed/short (see ``SCORE_TYPE_PREFERENCE``); ties
    break on ``pdf_path`` for determinism.
    """
    docs = score_docs_by_piece.get(piece_id) or []
    if not docs:
        return None
    return min(
        docs,
        key=lambda d: (
            SCORE_TYPE_PREFERENCE.get(str(d.get("score_type") or "full"), 9),
            str(d.get("pdf_path") or ""),
        ),
    )


def _score_type_usable(score_type: Any, config: dict[str, Any]) -> bool:
    """True when a local score's type is allowed as a Stage A instrumentation source.

    Every score type is usable by default: even a condensed or conductor score almost always prints
    the full instrumentation list in its front matter, and a full score is the most authoritative
    source of all. (Whether a conductor score appears in the *displayed* full instrumentation is a
    separate, downstream concern -- not this discovery gate.) The excluded set stays configurable
    via ``local_score_types_excluded`` for callers that want to suppress a type; an empty/absent
    list (the default) allows every type.
    """
    excluded = {
        str(t).strip().lower() for t in (config.get("local_score_types_excluded") or ())
    }
    return str(score_type or "full").strip().lower() not in excluded


def get_extracted_score_text(
    pdf_path: str, text_by_pdf: dict[str, list[dict[str, Any]]], max_pages: int
) -> str:
    """Concatenate the leading pages' text (embedded or OCR) for a score PDF from Script 02 output.

    Uses ``text_source`` to pick embedded vs OCR text per page and keeps only the first
    ``max_pages`` pages, where a score's staff labels / instrumentation list appear.
    """
    pages = text_by_pdf.get(pdf_path) or []
    ordered = sorted(pages, key=lambda r: r.get("page_num", 0))[: max(1, max_pages)]
    chunks: list[str] = []
    for rec in ordered:
        source = rec.get("text_source")
        text = rec.get("ocr_text") if source == "ocr" else rec.get("embedded_text")
        text = text or rec.get("embedded_text") or rec.get("ocr_text") or ""
        headers = rec.get("header_text_candidates") or []
        if headers:
            chunks.append(" ".join(str(h) for h in headers))
        if text.strip():
            chunks.append(text.strip())
    return "\n".join(chunks).strip()


def mean_score_ocr_confidence(
    pdf_path: str, text_by_pdf: dict[str, list[dict[str, Any]]], max_pages: int
) -> float | None:
    """Mean OCR confidence over the leading OCR-sourced pages of a score, or None.

    Only pages whose text came from OCR (``text_source == "ocr"``) with a numeric ``ocr_confidence``
    count; born-digital/embedded pages have reliable text and no OCR confidence, so they return
    None and the quality gate does not apply to them.
    """
    pages = text_by_pdf.get(pdf_path) or []
    ordered = sorted(pages, key=lambda r: r.get("page_num", 0))[: max(1, max_pages)]
    confidences = [
        float(rec["ocr_confidence"])
        for rec in ordered
        if rec.get("text_source") == "ocr"
        and isinstance(rec.get("ocr_confidence"), (int, float))
    ]
    if not confidences:
        return None
    return sum(confidences) / len(confidences)


def best_score_ocr_confidence(
    pdf_path: str, text_by_pdf: dict[str, list[dict[str, Any]]], max_pages: int
) -> float | None:
    """Highest single OCR-pass confidence across the leading OCR pages of a score (0..1), or None.

    Reads every multi-pass OCR candidate's confidence (falling back to the page-level
    ``ocr_confidence`` when a page has no candidates) and returns the maximum -- the best legible
    pass available for the LLM to reconcile. This avoids hiding a clean low-PSM/high-DPI pass behind
    the word-count-weighted pass that the page-level scalar happened to select. Returns None when no
    leading page came from OCR (embedded pages have reliable text and no OCR confidence).
    """
    pages = text_by_pdf.get(pdf_path) or []
    ordered = sorted(pages, key=lambda r: r.get("page_num", 0))[: max(1, max_pages)]
    best: list[float] = []
    for rec in ordered:
        if rec.get("text_source") != "ocr":
            continue
        pass_confs = [
            float(c["confidence"])
            for c in (rec.get("ocr_candidates") or [])
            if isinstance(c.get("confidence"), (int, float))
        ]
        if pass_confs:
            best.append(max(pass_confs))
        elif isinstance(rec.get("ocr_confidence"), (int, float)):
            best.append(float(rec["ocr_confidence"]))
    if not best:
        return None
    return max(best)


def gather_score_ocr_candidates_text(
    pdf_path: str, text_by_pdf: dict[str, list[dict[str, Any]]], max_pages: int
) -> str:
    """Assemble the multi-pass OCR candidates for a score's leading pages into one labelled blob.

    Mirrors Script 02's part-level consolidation: every OCR pass (dpi/psm/confidence) is shown so
    the LLM can reconcile the noisy passes rather than trusting the single word-count-weighted pass
    that populated ``ocr_text``. Any detected header text is prepended per page. Returns "" when no
    leading page carries OCR candidates (the caller then uses the embedded/single-pass text path).
    """
    pages = text_by_pdf.get(pdf_path) or []
    ordered = sorted(pages, key=lambda r: r.get("page_num", 0))[: max(1, max_pages)]
    chunks: list[str] = []
    for rec in ordered:
        candidates = rec.get("ocr_candidates") or []
        if not candidates:
            continue
        headers = rec.get("header_text_candidates") or []
        chunks.append(f"--- Page {rec.get('page_num')} ---")
        if headers:
            chunks.append("[header] " + " ".join(str(h) for h in headers))
        for c in candidates:
            chunks.append(
                f"[dpi={c.get('dpi')} psm={c.get('psm')} conf={c.get('confidence')}] "
                f"{c.get('text', '')}"
            )
    return "\n".join(chunks).strip()[:SCORE_OCR_MAX_PROMPT_CHARS]


def reocr_score_pages(
    abs_pdf_path: Path, max_pages: int, dpi: int, dest_dir: Path
) -> str:
    """Re-render and OCR the leading pages of a score PDF; returns concatenated OCR text.

    Returns an empty string when PyMuPDF/pytesseract are unavailable, the file is missing, or
    rendering/OCR fails -- callers then keep whatever (thin) reused text they had.
    """
    if fitz is None or pytesseract is None or Image is None:
        logger.debug("Re-OCR skipped: pymupdf/pytesseract/Pillow not available.")
        return ""
    if not abs_pdf_path.exists():
        logger.debug("Re-OCR skipped: score PDF not found at %s", abs_pdf_path)
        return ""
    dest_dir.mkdir(parents=True, exist_ok=True)
    chunks: list[str] = []
    try:
        doc = fitz.open(abs_pdf_path)  # type: ignore[attr-defined]
    except Exception as exc:  # pragma: no cover - depends on file/toolchain
        logger.warning("Re-OCR failed to open %s: %s", abs_pdf_path, exc)
        return ""
    try:
        page_count = min(max(1, max_pages), doc.page_count)
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)  # type: ignore[attr-defined]
        logger.info("Re-OCRing first %d page(s) of score %s at %d DPI",
                    page_count, abs_pdf_path.name, dpi)
        for page_index in range(page_count):
            try:
                logger.info("Re-OCR: rendering + OCRing page %d/%d of %s",
                            page_index + 1, page_count, abs_pdf_path.name)
                page = doc.load_page(page_index)
                pix = page.get_pixmap(matrix=matrix)  # type: ignore[attr-defined]
                img_path = dest_dir / f"reocr_page{page_index + 1}.png"
                pix.save(img_path)
                text = ocr_image_file(img_path)
                if text.strip():
                    chunks.append(text.strip())
            except Exception as exc:  # pragma: no cover - depends on file/toolchain
                logger.warning("Re-OCR failed on page %d of %s: %s", page_index + 1,
                               abs_pdf_path, exc)
    finally:
        doc.close()
    return "\n".join(chunks).strip()


# --- Remote image OCR (Stage C) --------------------------------------------------------------


def download_image(url: str, dest_dir: Path, index: int) -> Path | None:
    """Download an image URL into ``dest_dir``; returns the saved path or None on failure.

    No domain or size restriction is applied (per configuration): any URL the lookup returns is
    fetched. Files land in the per-piece temp directory the caller created.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(urllib.parse.urlparse(url).path).suffix
    if not suffix or len(suffix) > 5:
        suffix = ".img"
    dest = dest_dir / f"image_{index:02d}{suffix}"
    logger.info("Downloading candidate score image #%d: %s -> %s", index, url, dest.name)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "music-library-analyser/04"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
    except Exception as exc:
        logger.warning("Failed to download image %s: %s", url, exc)
        return None
    logger.info("Downloaded image #%d (%d bytes): %s", index, dest.stat().st_size, dest.name)
    return dest


def ocr_image_file(image_path: Path) -> str:
    """OCR a single image file with Tesseract; returns "" when OCR is unavailable or fails."""
    if pytesseract is None or Image is None:
        logger.debug("Image OCR skipped: pytesseract/Pillow not available.")
        return ""
    logger.info("OCRing image %s", image_path.name)
    try:
        with Image.open(image_path) as img:  # type: ignore[union-attr]
            text = str(pytesseract.image_to_string(img) or "").strip()  # type: ignore[union-attr]
    except Exception as exc:
        logger.warning("Image OCR failed for %s: %s", image_path, exc)
        return ""
    logger.info("OCR of %s produced %d character(s)", image_path.name, len(text))
    return text


def extract_image_urls(result: dict[str, Any]) -> list[str]:
    """Pull the candidate score-image URLs out of a lookup result, in order, de-duplicated."""
    raw = result.get("candidate_score_images")
    urls: list[str] = []
    seen: set[str] = set()
    if isinstance(raw, list):
        for entry in raw:
            url = entry.get("url") if isinstance(entry, dict) else entry
            if isinstance(url, str) and url.strip() and url not in seen:
                seen.add(url)
                urls.append(url.strip())
    return urls


def download_and_ocr_images(
    urls: list[str],
    piece_id: str,
    fetch_fn: Callable[[str, Path, int], Path | None],
    ocr_fn: Callable[[Path], str],
) -> tuple[str, int]:
    """Download each image into a per-piece temp folder and OCR it; returns (text, ocr_count).

    A dedicated temp directory is created for the piece so downloaded images are isolated and can
    be inspected during a run; it is left in place for auditing.
    """
    if not urls:
        return "", 0
    temp_dir = Path(tempfile.mkdtemp(prefix=f"score_imgs_{piece_id}_"))
    logger.info("Downloading %d candidate score image(s) for %s into %s",
                len(urls), piece_id, temp_dir)
    chunks: list[str] = []
    ocr_count = 0
    for i, url in enumerate(urls, start=1):
        path = fetch_fn(url, temp_dir, i)
        if path is None:
            continue
        text = ocr_fn(path)
        if text.strip():
            chunks.append(text.strip())
            ocr_count += 1
    logger.info("OCR'd %d of %d candidate score image(s) for %s (%d chars total)",
                ocr_count, len(urls), piece_id, len("\n".join(chunks).strip()))
    return "\n".join(chunks).strip(), ocr_count


# --- Summarize score text into the instrumentation contract ----------------------------------


def build_summarize_prompt(template: str, piece: dict[str, Any], score_text: str) -> str:
    """Render the summarize template with the piece context and the supplied score text.

    Library holdings are deliberately excluded so the summarized instrumentation reflects the score
    text alone.
    """
    query = {
        "title_guess": str(piece.get("piece_title_guess") or "unknown"),
        "catalog_number": str(piece.get("catalog_number") or "unknown"),
        "piece_folder": str(piece.get("piece_folder") or "unknown"),
        "score_text": score_text,
    }
    return render_prompt(template, query)


def derive_contract_from_text(
    score_text: str,
    piece: dict[str, Any],
    summarize_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    summarize_template: str,
    config: dict[str, Any],
) -> dict[str, Any] | None:
    """Summarize score/OCR text into the instrumentation contract via the LLM.

    Returns the parsed contract dict, or None when the call fails or returns unusable output.
    """
    prompt = build_summarize_prompt(summarize_template, piece, score_text)
    try:
        result = summarize_fn(prompt, config)
    except Exception as exc:
        logger.warning("Summarize call failed for piece %s: %s", piece.get("piece_id"), exc)
        return None
    return result if isinstance(result, dict) else None


# --- Deterministic percussion backfill (Lever 2) ---------------------------------------------
# A summary stage sometimes collapses several named percussion instruments into a single generic
# "percussion" line (e.g. a WindRep "Percussion, including: Bass Drum, Castanets, Snare Drum,
# Tambourine, Xylophone" block). When the raw source text carries an explicit instrumentation
# section that names those instruments, enumerate them deterministically so the richer breakdown is
# not discarded by whichever summarize stage wins -- no extra LLM call required.

_INSTRUMENTATION_HEADING_RE = re.compile(
    r"==+\s*(?:instrumentation|scoring|besetzung)\s*==+", re.IGNORECASE
)
_INSTRUMENTATION_PLAIN_RE = re.compile(
    r"^[ \t]*(?:instrumentation|scoring|besetzung)[ \t]*:?[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_LABEL_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")
_ALTERNATE_DELIMITER_RE = re.compile(r"/|\bor\b", re.IGNORECASE)
_DOUBLING_DELIMITER_RE = re.compile(r"&|\+|\band\b|\bdoubling\b|\bdbl\b", re.IGNORECASE)


def _instrumentation_region(text: str) -> str | None:
    """Return the instrumentation section of a source text, or None when none is present.

    Only an explicitly labelled instrumentation/scoring/Besetzung section is returned, so the
    percussion backfill never scans program-note prose (which may mention instruments in passing).
    Handles both MediaWiki-style ``== Instrumentation ==`` headings (WindRep) and a plain
    ``Instrumentation`` heading line.
    """
    if not text:
        return None
    m = _INSTRUMENTATION_HEADING_RE.search(text)
    if m:
        start = m.end()
        nxt = re.search(r"\n==+[^=]", text[start:])
        end = start + nxt.start() if nxt else len(text)
        return text[start:end]
    m = _INSTRUMENTATION_PLAIN_RE.search(text)
    if m:
        return text[m.end():]
    return None


def _normalize_label_for_alias_match(label: Any) -> str:
    """Normalize a part label for alias matching against taxonomy phrases."""
    if not isinstance(label, str):
        return ""
    lowered = label.lower().replace("♭", " flat ").replace("♯", " sharp ")
    cleaned = _LABEL_NORMALIZE_RE.sub(" ", lowered)
    return normalize_instrument_name(cleaned)


def _label_canonical_matches(label: Any, alias_to_canonical: dict[str, str]) -> list[str]:
    """Canonical instruments mentioned in ``label`` (ordered by first match position)."""
    text = _normalize_label_for_alias_match(label)
    if not text:
        return []
    alias_items = sorted(alias_to_canonical.items(), key=lambda kv: len(kv[0]), reverse=True)
    occupied: list[tuple[int, int]] = []
    hits: list[tuple[int, str]] = []
    for alias, canonical in alias_items:
        if not alias:
            continue
        pattern = r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])"
        for match in re.finditer(pattern, text):
            start, end = match.span()
            overlaps = any(not (end <= left or start >= right) for left, right in occupied)
            if overlaps:
                continue
            occupied.append((start, end))
            hits.append((start, canonical))
    if not hits:
        return []
    hits.sort(key=lambda pair: pair[0])
    ordered: list[str] = []
    for _start, canonical in hits:
        if canonical not in ordered:
            ordered.append(canonical)
    return ordered


def _canonical_from_label(
    fallback_canonical: str,
    label: Any,
    *,
    alias_to_canonical: dict[str, str],
    canonical_to_section: dict[str, str],
) -> tuple[str, list[str]]:
    """Refine a slot's canonical instrument from its printed label.

    Returns ``(primary_canonical, equivalent_canonicals)`` where
    ``equivalent_canonicals`` are alternate instruments in an explicit either/or label
    (e.g. ``"Bass Clarinet / Contrabass Clarinet"``). Alternates are emitted only for
    explicit alternatives (``/`` or ``or``) and only within the same section.
    """
    matches = _label_canonical_matches(label, alias_to_canonical)
    if not matches:
        return fallback_canonical, []
    primary = matches[0]
    if fallback_canonical in matches:
        primary = fallback_canonical
    # Prefer a more specific label-derived canonical when the fallback is generic.
    if fallback_canonical == "clarinet":
        for candidate in matches:
            if candidate != "clarinet":
                primary = candidate
                break
    elif fallback_canonical == "horn":
        for candidate in matches:
            if candidate in {"tenor_horn", "mellophone"}:
                primary = candidate
                break
    elif fallback_canonical in {"trumpet", "cornet"}:
        for candidate in matches:
            if candidate in {"trumpet", "cornet", "flugelhorn", "soprano_cornet"}:
                primary = candidate
                break
    raw_label = str(label or "")
    has_alternatives = bool(_ALTERNATE_DELIMITER_RE.search(raw_label))
    has_doubling = bool(_DOUBLING_DELIMITER_RE.search(raw_label))
    equivalents: list[str] = []
    if has_alternatives and not has_doubling:
        primary_section = canonical_to_section.get(primary)
        for candidate in matches:
            if candidate == primary or candidate in equivalents:
                continue
            if primary_section and canonical_to_section.get(candidate) != primary_section:
                continue
            equivalents.append(candidate)
    return primary, equivalents


def _percussion_alias_index() -> list[tuple[str, str]]:
    """Return ``(alias, canonical)`` pairs for percussion-section instruments, longest alias first.

    Longer aliases are matched first so a specific phrase (``orchestra bells``) is preferred over a
    shorter substring. Aliases are normalized to spaced lowercase, matching ``regex_rules.yaml``.
    """
    taxonomy = _instrument_taxonomy()
    alias_to_canonical: dict[str, str] = taxonomy["alias_to_canonical"]
    section_map: dict[str, str] = taxonomy["canonical_to_section"]
    pairs = [
        (alias, canonical)
        for alias, canonical in alias_to_canonical.items()
        if section_map.get(canonical) == "percussion"
    ]
    pairs.sort(key=lambda p: len(p[0]), reverse=True)
    return pairs


def backfill_percussion_from_text(
    contract: dict[str, Any] | None,
    source_text: str,
) -> dict[str, Any] | None:
    """Enumerate named percussion a summary collapsed into one generic ``percussion`` line.

    Deterministic, LLM-free safety net for the summarize stages (local score, WindRep direct fetch,
    remote image OCR). When ``source_text`` has an explicit instrumentation section naming specific
    percussion instruments (snare drum, bass drum, xylophone, ...), each one not already present in
    the contract's ``expected_parts`` is appended as its own entry, and a lone generic ``percussion``
    entry is dropped once at least one specific percussion instrument is added. Returns the contract
    unchanged when there is no instrumentation section or nothing new to add.
    """
    if not isinstance(contract, dict):
        return contract
    parts = contract.get("expected_parts")
    if not isinstance(parts, list):
        return contract
    region = _instrumentation_region(source_text)
    if not region:
        return contract
    taxonomy = _instrument_taxonomy()
    alias_to_canonical = taxonomy["alias_to_canonical"]
    section_map = taxonomy["canonical_to_section"]

    def _canon(entry: dict[str, Any]) -> str:
        raw = entry.get("canonical_instrument") or entry.get("canonical")
        return canonicalize_instrument(raw, alias_to_canonical) if isinstance(raw, str) else ""

    present = {_canon(e) for e in parts if isinstance(e, dict)}

    found: dict[str, str] = {}
    for alias, canonical in _percussion_alias_index():
        if canonical == "percussion" or canonical in present or canonical in found:
            continue
        if re.search(rf"\b{re.escape(alias)}\b", region, re.IGNORECASE):
            found[canonical] = alias.title()
    if not found:
        return contract

    kept = [e for e in parts if not (isinstance(e, dict) and _canon(e) == "percussion")]
    for canonical, label in found.items():
        kept.append({
            "canonical_instrument": canonical,
            "part_index": None,
            "label": label,
            "section": section_map.get(canonical, "percussion"),
            "required": True,
        })
    updated = dict(contract)
    updated["expected_parts"] = kept
    return updated


# --- Expected-part normalization + reconciliation --------------------------------------------


def _coerce_index(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _is_score_slot(canonical: str, section: Any) -> bool:
    """True when a lookup part entry describes the score itself, not a playable instrument.

    Authority sources routinely list the score edition (e.g. "Condensed Score", "Full Score") as
    the first line of the instrumentation. Those are not reconcilable instrument parts -- observed
    score documents are tracked separately via ``score_expected`` / ``has_score`` -- so they must
    not become expected instrument slots (which could never be matched and would surface as a false
    "missing Condensed Score" required part).
    """
    if "score" in canonical.lower():
        return True
    return isinstance(section, str) and section.strip().lower() == "score"


def _drop_unqualified_generic_slot(canonical: str, label: Any, part_index: int | None) -> bool:
    """Reject generic umbrella labels that are not tied to a subtype or part number.

    A bare ``Clarinet`` or ``Trombone`` line is too vague to become a required expected-part slot by
    itself; if the score actually names a subtype (``Eb Clarinet``) or identifies a numbered chair
    (``Clarinet 1``) we keep it. This prevents spurious "missing clarinet/trombone" reports from
    generic fallback entries that are not source-backed by a more specific label.
    """
    if part_index is not None:
        return False
    if not isinstance(label, str):
        return False
    text = label.strip().lower().replace("_", " ")
    if not text:
        return False
    generic_canonicals = {"clarinet", "trombone", "horn", "trumpet", "cornet"}
    if canonical not in generic_canonicals:
        return False
    variants = {canonical, canonical.replace("_", " "), canonical.replace("_", " ") + "s"}
    if text not in variants:
        return False
    return True


def normalize_expected_parts(raw_parts: Any) -> list[dict[str, Any]]:
    """Coerce lookup ``expected_parts`` into internal slot dicts.

    Output slots use the same shape as ``reconcile_parts`` expects: ``canonical``, ``part_index``,
    ``label``, ``section``, ``required``. Entries without a canonical instrument are dropped, as are
    score-edition entries (see ``_is_score_slot``) since score presence is tracked separately. Each
    ``canonical_instrument`` from the lookup is mapped onto the shared taxonomy token (e.g.
    ``alto_saxophone`` -> ``alto_sax``) so it reconciles against Script 03's observed parts, and the
    ``section`` is taken from the taxonomy for that canonical (falling back to the lookup's section)
    so expected and observed parts are grouped identically.
    """
    slots: list[dict[str, Any]] = []
    if not isinstance(raw_parts, list):
        return slots
    taxonomy = _instrument_taxonomy()
    alias_to_canonical = taxonomy["alias_to_canonical"]
    section_map = taxonomy["canonical_to_section"]
    for entry in raw_parts:
        if not isinstance(entry, dict):
            continue
        raw_canonical = entry.get("canonical_instrument") or entry.get("canonical")
        if not raw_canonical or not isinstance(raw_canonical, str):
            continue
        fallback_canonical = canonicalize_instrument(raw_canonical, alias_to_canonical)
        canonical, equivalent_canonicals = _canonical_from_label(
            fallback_canonical,
            entry.get("label"),
            alias_to_canonical=alias_to_canonical,
            canonical_to_section=section_map,
        )
        if not canonical:
            continue
        if _is_score_slot(canonical, entry.get("section")):
            continue
        part_index = _coerce_index(entry.get("part_index"))
        if _drop_unqualified_generic_slot(canonical, entry.get("label"), part_index):
            continue
        required = entry.get("required")
        slot = {
            "canonical": canonical,
            "part_index": part_index,
            "label": str(entry.get("label") or canonical),
            "section": section_map.get(canonical, entry.get("section")),
            "required": True if required is None else bool(required),
        }
        if equivalent_canonicals:
            slot["equivalent_canonicals"] = equivalent_canonicals
        slots.append(slot)
    return slots


def _unexpected_entry(obs: dict[str, Any]) -> dict[str, Any]:
    return {
        "predicted_part": obs.get("predicted_part"),
        "instruments": [
            {"canonical": f.get("canonical"), "part_index": f.get("part_index")}
            for f in obs.get("instruments", [])
        ],
        "part_role": obs.get("part_role", "section"),
        "count": obs.get("count", 1),
        "observed_clefs": list(obs.get("observed_clefs", [])),
    }


def _unexpected_label(entry: dict[str, Any]) -> str:
    """Human-readable label for an unexpected observed part."""
    predicted = entry.get("predicted_part")
    if predicted:
        return str(predicted)
    labels: list[str] = []
    for facet in entry.get("instruments", []):
        canonical = facet.get("canonical")
        if not canonical:
            continue
        idx = facet.get("part_index")
        labels.append(canonical if idx is None else f"{canonical} {idx}")
    return " / ".join(labels) if labels else "unknown"


# Display abbreviations for the clef editions a part may be published in.
_CLEF_DISPLAY: dict[str, str] = {"bass": "BC", "treble": "TC"}

# Prettified transposition editions (e.g. an F horn also held as its E-flat alternate).
_TRANSPOSITION_DISPLAY: dict[str, str] = {
    "Bb": "B\u266d", "Eb": "E\u266d", "Ab": "A\u266d", "Db": "D\u266d", "Gb": "G\u266d",
}


def _transposition_display(value: Any) -> str | None:
    if not value:
        return None
    return _TRANSPOSITION_DISPLAY.get(value, str(value))


# Default transposition conventions: a bare (unkeyed) part for these instruments is, by long-
# standing band/orchestral convention, in the key given here. This lets an unmarked "Trumpet 1"
# reconcile against a "Bb Trumpet" slot, and -- crucially -- lets an explicitly *different* key
# (e.g. an E-flat Horn) be recognised as a genuinely distinct instrument rather than an alternate
# of the conventional one (Horn in F). Concert-pitch instruments (flutes, oboes, bassoons,
# trombones, tuba, strings, keyboards, percussion) and instruments whose written key is a clef
# choice rather than a transposition (euphonium/baritone B.C. vs T.C.) are omitted: they carry no
# transposition identity and match any key.
DEFAULT_TRANSPOSITIONS: dict[str, str] = {
    # Clarinets (the plain "clarinet" is the B-flat soprano; sizes are their own canonicals).
    "clarinet": "Bb",
    "eb_clarinet": "Eb",
    "alto_clarinet": "Eb",
    "bass_clarinet": "Bb",
    "contrabass_clarinet": "Bb",
    # Saxophones.
    "sopranino_sax": "Eb",
    "soprano_sax": "Bb",
    "alto_sax": "Eb",
    "tenor_sax": "Bb",
    "baritone_sax": "Eb",
    "bass_sax": "Bb",
    # Double reeds / flutes that transpose.
    "english_horn": "F",
    "alto_flute": "G",
    # Cornets / trumpets / flugelhorn (unkeyed cornet or trumpet is a B-flat by convention).
    "soprano_cornet": "Eb",
    "cornet": "Bb",
    "trumpet": "Bb",
    "flugelhorn": "Bb",
    # Horns (unkeyed horn is a Horn in F; the E-flat horn/alto is a distinct instrument).
    "horn": "F",
    "tenor_horn": "Eb",
    "mellophone": "F",
}

# Ordered label patterns for reading a transposition out of an expected-slot label such as
# "Horn in F 3", "E-flat Horn or Alto II", or "Bb Trumpet". Flat-key patterns are checked before
# the plain "in X" patterns so "E-flat" wins over a stray "in F" elsewhere in the label.
_LABEL_TRANSPOSITION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\be[-\s]?flat\b|\bin\s+eb\b|\beb\b", re.IGNORECASE), "Eb"),
    (re.compile(r"\bb[-\s]?flat\b|\bin\s+bb\b|\bbb\b", re.IGNORECASE), "Bb"),
    (re.compile(r"\ba[-\s]?flat\b|\bin\s+ab\b|\bab\b", re.IGNORECASE), "Ab"),
    (re.compile(r"\bd[-\s]?flat\b|\bin\s+db\b|\bdb\b", re.IGNORECASE), "Db"),
    (re.compile(r"\bin\s+f\b", re.IGNORECASE), "F"),
    (re.compile(r"\bin\s+c\b", re.IGNORECASE), "C"),
    (re.compile(r"\bin\s+a\b", re.IGNORECASE), "A"),
    (re.compile(r"\bin\s+d\b", re.IGNORECASE), "D"),
    (re.compile(r"\bin\s+g\b", re.IGNORECASE), "G"),
]


def _parse_label_transposition(label: Any) -> str | None:
    """Read an explicit transposition key from an expected-slot label, or ``None`` if unstated."""
    if not label or not isinstance(label, str):
        return None
    for pattern, key in _LABEL_TRANSPOSITION_PATTERNS:
        if pattern.search(label):
            return key
    return None


def _norm_transposition(value: Any) -> str | None:
    """Canonical comparison form for a transposition key (case-insensitive), or ``None``."""
    if not value:
        return None
    return str(value).strip().casefold()


def _conventional_transposition(canonical: Any) -> str | None:
    """Default transposition for a bare part of ``canonical``, or ``None`` if no convention."""
    return DEFAULT_TRANSPOSITIONS.get(canonical)


def _slot_transposition(slot: dict[str, Any]) -> str | None:
    """Transposition identity of an expected slot: explicit label key, else convention default."""
    return _parse_label_transposition(slot.get("label")) or _conventional_transposition(
        slot.get("canonical")
    )


def _slot_role(slot: dict[str, Any]) -> str:
    """Expected slot role inferred from its label."""
    label = str(slot.get("label") or "")
    return "solo" if re.search(r"(?<![a-z0-9])solo(?![a-z0-9])", label, re.IGNORECASE) else "section"


def _role_compatible(slot: dict[str, Any], observed_role: Any) -> bool:
    """Whether an observed part role may satisfy an expected slot role."""
    slot_role = _slot_role(slot)
    role = str(observed_role or "section")
    if slot_role == "solo":
        return role in {"solo", "solo_alternative"}
    return role == "section"


def _transposition_compatible(
    slot: dict[str, Any], facet_canonical: Any, obs_transposition: Any
) -> bool:
    """Whether an observed instance may fill an expected slot given their transpositions.

    An *unkeyed* observed part (no transposition marker) is flexible and fits any slot -- we do not
    guess it into a mismatch. A *keyed* observed part must match the slot's transposition identity
    (the slot's explicit label key, or its conventional default); if the slot carries no
    transposition identity at all, any key is accepted. This is what keeps an E-flat Horn from
    filling a Horn in F chair while still letting an unmarked horn part do so.
    """
    if not obs_transposition:
        return True
    slot_key = _slot_transposition(slot)
    if not slot_key:
        return True
    return _norm_transposition(obs_transposition) == _norm_transposition(slot_key)


def _facet_set_key(obs: dict[str, Any]) -> tuple[tuple[Any, Any], ...]:
    """Order-independent identity of the instrument set a part represents."""
    return tuple(
        sorted((f.get("canonical"), f.get("part_index")) for f in obs.get("instruments", []))
    )


def collapse_clef_editions(
    observed_parts: list[dict[str, Any]],
    slot_demand: dict[tuple[tuple[Any, Any], ...], int] | None = None,
) -> list[dict[str, Any]]:
    """Merge observed parts that are the same musical part in different clef editions.

    The *same* part is sometimes published in both a bass-clef (BC, concert pitch) and treble-clef
    (TC, transposing) edition. Script 03 keeps those as separate observed entries because they are
    distinct physical items in the library, but for expected-parts reconciliation they represent
    one part. Collapsing them here prevents the alternate edition from
    (a) double-filling an expected slot (which would inflate completeness) or (b) being reported as
    an "unexpected" surplus part.

    Entries are grouped by their instrument set (the sorted ``(canonical, part_index)`` facets), so
    a combined/doubling part collapses only with another part covering the exact same instruments.
    Within a group each *distinct* clef is treated as one edition of the same part; the set of clefs
    seen is recorded on the merged entry as ``observed_clefs`` (sorted BC/TC display codes). When the
    same clef appears more than once in a group (genuinely separate copies, not editions) the extra
    copies are kept as separate observed instances so they still consume their own slots. Insertion
    order is preserved for deterministic downstream matching.

    ``slot_demand`` maps a facet-set key to how many expected slots share that exact
    ``(canonical, part_index)`` identity. When the score enumerates more co-equal slots for a part
    than we hold same-clef copies, the clef editions are *not* merged: each is a distinct physical
    part able to fill its own slot (e.g. a Baritone printed in both B.C. and T.C. against two
    separate baritone chairs). Absent (or unit) demand, the historical merge behaviour is unchanged.
    """
    groups: dict[tuple[tuple[Any, Any], ...], list[dict[str, Any]]] = {}
    order: list[tuple[tuple[Any, Any], ...]] = []
    for obs in observed_parts:
        key = _facet_set_key(obs)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(obs)

    collapsed: list[dict[str, Any]] = []
    for key in order:
        entries = groups[key]
        demand = slot_demand.get(key, 1) if slot_demand else 1
        # A held part in an explicitly different key (e.g. an E-flat Horn alongside a Horn in F) is
        # a genuinely distinct instrument, not a clef/edition alternate. When a facet set holds two
        # or more explicit keys, split it into per-key partitions so each key collapses on its own
        # and can be reconciled (or reported as unlisted) independently. A single key -- with or
        # without unmarked copies -- keeps the historical clef-edition merge behaviour.
        for partition in _partition_by_transposition(entries):
            collapsed.extend(_collapse_group(partition, demand))
    return collapsed


def _partition_by_transposition(
    entries: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Split a facet-set group into partitions that are distinct instruments by transposition.

    Entries sharing an explicit key form one partition. Unmarked (unkeyed) entries are flexible:
    they join the partition of the group's conventional default key when it is present, otherwise
    they form their own partition. When fewer than two explicit keys are present the whole group is
    returned unsplit so single-key clef-edition merging is unchanged.
    """
    explicit_keys = {e.get("transposition") for e in entries if e.get("transposition")}
    if len(explicit_keys) <= 1:
        return [entries]

    buckets: dict[Any, list[dict[str, Any]]] = {}
    order: list[Any] = []

    def _add(bucket_key: Any, entry: dict[str, Any]) -> None:
        if bucket_key not in buckets:
            buckets[bucket_key] = []
            order.append(bucket_key)
        buckets[bucket_key].append(entry)

    for entry in entries:
        trans = entry.get("transposition")
        if trans:
            _add(_norm_transposition(trans), entry)
            continue
        conv = _conventional_transposition(_primary_canonical(entry))
        conv_key = _norm_transposition(conv)
        if conv_key in {_norm_transposition(k) for k in explicit_keys}:
            _add(conv_key, entry)
        else:
            _add(conv_key if conv_key else (None, id(entry)), entry)
    return [buckets[k] for k in order]


def _primary_canonical(entry: dict[str, Any]) -> Any:
    """Canonical of the first instrument facet of an observed part, or ``None``."""
    for facet in entry.get("instruments", []):
        canonical = facet.get("canonical")
        if canonical is not None:
            return canonical
    return None


def _collapse_group(
    entries: list[dict[str, Any]], demand: int
) -> list[dict[str, Any]]:
    """Collapse clef editions within one instrument-set / transposition partition."""
    collapsed: list[dict[str, Any]] = []
    by_clef: dict[Any, list[dict[str, Any]]] = {}
    clef_order: list[Any] = []
    for entry in entries:
        # An edition is a (clef, transposition) pair: the same musical part published in a
        # different written form (BC/TC clef, or an unmarked copy of a keyed part). Same-edition
        # copies are genuine duplicates; different editions may fill separate slots.
        edition = (entry.get("clef"), entry.get("transposition"))
        if edition not in by_clef:
            by_clef[edition] = []
            clef_order.append(edition)
        by_clef[edition].append(entry)

    instances = max(len(bucket) for bucket in by_clef.values())
    if instances < demand and len(entries) > instances:
        # The score lists more co-equal slots for this exact part than we have same-edition
        # copies, yet we hold multiple editions. Each edition is its own physical part able to
        # fill a distinct slot, so surface them individually (up to demand) rather than merging.
        for entry in entries[:demand]:
            clef = entry.get("clef")
            display = _CLEF_DISPLAY.get(clef, clef) if clef else None
            trans = _transposition_display(entry.get("transposition"))
            merged = dict(entry)
            merged["count"] = int(entry.get("count", 1) or 1)
            merged["observed_clefs"] = [display] if display else []
            merged["observed_transpositions"] = [trans] if trans else []
            collapsed.append(merged)
        return collapsed
    for i in range(instances):
        base: dict[str, Any] | None = None
        total = 0
        clefs: list[str] = []
        transps: list[str] = []
        for edition in clef_order:
            bucket = by_clef[edition]
            if i >= len(bucket):
                continue
            entry = bucket[i]
            if base is None:
                base = entry
            total += int(entry.get("count", 1) or 1)
            clef = entry.get("clef")
            display = _CLEF_DISPLAY.get(clef, clef) if clef else None
            if display and display not in clefs:
                clefs.append(display)
            trans = _transposition_display(entry.get("transposition"))
            if trans and trans not in transps:
                transps.append(trans)
        merged = dict(base) if base is not None else {}
        merged["count"] = total
        merged["observed_clefs"] = sorted(clefs)
        merged["observed_transpositions"] = sorted(transps)
        collapsed.append(merged)
    return collapsed


def build_equivalents(
    slots: list[dict[str, Any]],
    groups: list[list[str]] | None = None,
) -> dict[str, list[str]]:
    """Map each expected slot canonical to the observed canonicals that may also satisfy it.

    ``groups`` lists sets of interchangeable instruments (from the taxonomy's
    ``interchangeable_instruments`` config). Within each group, a part written for one member covers
    the chair of the others, so we let an observed member satisfy an expected slot for any other
    member -- e.g. an expected ``euphonium`` slot is filled by an observed ``baritone_horn`` and vice
    versa. This bridging is DISABLED for a group the moment the score lists more than one of its
    members as separate expected slots: then each slot must be filled by its own instrument (no
    cross-matching), so a genuinely missing member is still reported. Returns a ``{slot_canonical:
    [other_canonicals]}`` map suitable for ``reconcile_parts``.
    """
    if not groups:
        return {}
    expected_canonicals = {slot["canonical"] for slot in slots}
    equivalents: dict[str, list[str]] = {}
    for group in groups:
        members_in_score = [m for m in group if m in expected_canonicals]
        # Only bridge when the score calls for exactly one member of the group.
        if len(members_in_score) != 1:
            continue
        slot_canonical = members_in_score[0]
        others = [m for m in group if m != slot_canonical]
        if others:
            equivalents[slot_canonical] = others
    return equivalents


def reconcile_parts(
    template_parts: list[dict[str, Any]],
    observed_parts: list[dict[str, Any]],
    equivalents: dict[str, list[str]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Match observed parts against expected slots using instrument-set coverage.

    Returns (expected_parts, unexpected_parts). ``expected_parts`` preserves slot order and carries
    a ``present`` flag plus ``observed_clefs`` (the clef editions that satisfied the slot).

    Each observed part lists every instrument it covers (``instruments``): a combined or doubling
    part such as "Flute 1 & Piccolo" contributes one *instance* per instrument, so a single physical
    part can satisfy multiple expected slots. Matching is count-based per canonical instrument
    (robust to null ``part_index``), preferring explicit index matches before consuming slots in
    order. Observed parts that differ only by clef are collapsed first so an alternate edition never
    double-fills a slot. ``equivalents`` maps a slot canonical to observed canonicals that should
    satisfy it.

    An observed part is reported as *unexpected* only if **none** of its instruments matched any
    expected slot. When a part is anchored by at least one matched instrument, its remaining
    (doubling) instruments are silently accepted rather than flagged \u2014 an extra doubling the score
    happens not to enumerate is informational at most, never a surplus.
    """
    alias_to_slot: dict[str, str] = {}
    for slot_canonical, aliases in (equivalents or {}).items():
        for alias in aliases:
            alias_to_slot[alias] = slot_canonical
    # Slot-local alternatives from explicit label either/or forms
    # (e.g. "Bass Clarinet / Contrabass Clarinet"): treat alternates as satisfying
    # the same expected slot unless they are also explicitly listed as primary slots.
    explicit_primary = {slot["canonical"] for slot in template_parts}
    for slot in template_parts:
        slot_canonical = slot["canonical"]
        for alias in slot.get("equivalent_canonicals", []):
            if not alias or alias == slot_canonical:
                continue
            if alias in explicit_primary:
                continue
            alias_to_slot.setdefault(alias, slot_canonical)

    expected_idx_by_instr: dict[str, list[int]] = defaultdict(list)
    slots_by_key: dict[tuple[Any, Any], int] = defaultdict(int)
    for i, slot in enumerate(template_parts):
        expected_idx_by_instr[slot["canonical"]].append(i)
        slots_by_key[(slot["canonical"], slot.get("part_index"))] += 1

    # How many expected slots share a part's exact identity, so a clef-edition pair only stays split
    # when the score genuinely enumerates that many co-equal chairs (keyed by observed facet set).
    slot_demand: dict[tuple[tuple[Any, Any], ...], int] = {}
    for obs in observed_parts:
        fkey = _facet_set_key(obs)
        if fkey in slot_demand:
            continue
        demand = 0
        for facet in obs.get("instruments", []):
            canon = facet.get("canonical")
            if canon is None:
                continue
            target = alias_to_slot.get(canon, canon)
            demand = max(demand, slots_by_key.get((target, facet.get("part_index")), 0))
        slot_demand[fkey] = demand

    collapsed = collapse_clef_editions(observed_parts, slot_demand)
    # Flatten every observed part into one instance per instrument facet, tagged with the owning
    # observed-part id so we can tell whether a part anchored at least one slot. Prefer a literal
    # same-canonical match for an expected slot before falling back to an equivalent alternative
    # (e.g. an observed Euphonium satisfying an expected Euphonium slot ahead of a held Baritone
    # that only counts as an interchangeable substitute).
    exact_instances_by_instr: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    equivalent_instances_by_instr: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for obs_id, obs in enumerate(collapsed):
        for facet in obs.get("instruments", []):
            canonical = facet.get("canonical")
            if canonical is None:
                continue
            slot_canonical = alias_to_slot.get(canonical, canonical)
            exact_instances_by_instr[canonical].append((obs_id, facet))
            if slot_canonical != canonical:
                equivalent_instances_by_instr[slot_canonical].append((obs_id, facet))

    present_ids: set[int] = set()
    slot_obs: dict[int, dict[str, Any]] = {}
    matched_obs_ids: set[int] = set()

    for canonical, slot_ids in expected_idx_by_instr.items():
        exact_matches = exact_instances_by_instr.get(canonical, [])
        equivalent_matches = [
            pair
            for pair in equivalent_instances_by_instr.get(canonical, [])
            if pair[0] not in {obs_id for obs_id, _facet in exact_matches}
        ]
        inst_list = exact_matches + equivalent_matches
        consumed = {sid: False for sid in slot_ids}
        used = [False] * len(inst_list)

        # Pass 1: match by explicit part_index.
        for ii, (obs_id, facet) in enumerate(inst_list):
            oidx = facet.get("part_index")
            if oidx is None:
                continue
            obs_trans = collapsed[obs_id].get("transposition")
            for sid in slot_ids:
                if (
                    not consumed[sid]
                    and template_parts[sid].get("part_index") == oidx
                    and _role_compatible(template_parts[sid], collapsed[obs_id].get("part_role"))
                    and _transposition_compatible(
                        template_parts[sid], facet.get("canonical"), obs_trans
                    )
                ):
                    consumed[sid] = True
                    used[ii] = True
                    slot_obs[sid] = collapsed[obs_id]
                    matched_obs_ids.add(obs_id)
                    break

        # Pass 2: fill remaining slots with the best remaining instance.
        for sid in slot_ids:
            if consumed[sid]:
                continue
            slot = template_parts[sid]
            target_idx = slot.get("part_index")
            best_ii: int | None = None
            best_rank: tuple[int, int] | None = None
            for ii, (obs_id, facet) in enumerate(inst_list):
                if used[ii]:
                    continue
                obs = collapsed[obs_id]
                if not _role_compatible(slot, obs.get("part_role")):
                    continue
                if not _transposition_compatible(slot, facet.get("canonical"), obs.get("transposition")):
                    continue
                oidx = facet.get("part_index")
                if target_idx is None:
                    # Prefer an unnumbered instance for an unnumbered slot.
                    rank = (0, ii) if oidx is None else (1, ii)
                else:
                    # Explicit index should already be filled in pass 1; keep deterministic fallback.
                    rank = (0, ii) if oidx == target_idx else (1, ii)
                if best_rank is None or rank < best_rank:
                    best_rank = rank
                    best_ii = ii
            if best_ii is None:
                continue
            obs_id, _facet = inst_list[best_ii]
            consumed[sid] = True
            used[best_ii] = True
            slot_obs[sid] = collapsed[obs_id]
            matched_obs_ids.add(obs_id)

        for sid in slot_ids:
            if consumed[sid]:
                present_ids.add(sid)

    # A part is unexpected only if none of its instruments anchored an expected slot.
    has_unindexed_section_slot: set[str] = {
        slot["canonical"]
        for slot in template_parts
        if slot.get("part_index") is None and _slot_role(slot) == "section"
    }
    unexpected: list[dict[str, Any]] = []
    for obs_id, obs in enumerate(collapsed):
        if not obs.get("instruments"):
            continue
        if obs_id not in matched_obs_ids:
            role = str(obs.get("part_role") or "section")
            if role in {"solo", "solo_alternative"}:
                continue
            # A split numbered section part against an unnumbered section slot (e.g. Alto Sax 1/2
            # vs. expected "Alto Sax") is a variant, not an actionable surprise.
            indexed_variants = [
                f for f in obs.get("instruments", [])
                if f.get("part_index") is not None and f.get("canonical") in has_unindexed_section_slot
            ]
            if indexed_variants and len(indexed_variants) == len(obs.get("instruments", [])):
                continue
            unexpected.append(_unexpected_entry(obs))

    expected: list[dict[str, Any]] = []
    for i, slot in enumerate(template_parts):
        obs = slot_obs.get(i)
        expected.append({
            "canonical_instrument": slot["canonical"],
            "part_index": slot.get("part_index"),
            "label": slot["label"],
            "section": slot.get("section"),
            "required": bool(slot.get("required")),
            "present": i in present_ids,
            "observed_clefs": list(obs.get("observed_clefs", [])) if obs else [],
            "observed_transpositions": list(obs.get("observed_transpositions", [])) if obs else [],
        })
    return expected, unexpected


def completeness_tier(score: float, missing_required: int, score_missing: bool) -> str:
    """Map the required-part completeness fraction onto a tier label."""
    if missing_required == 0 and not score_missing:
        return CompletenessTier.COMPLETE
    if score >= NEAR_COMPLETE:
        return CompletenessTier.NEAR_COMPLETE
    if score >= INCOMPLETE:
        return CompletenessTier.INCOMPLETE
    return CompletenessTier.SEVERELY_INCOMPLETE


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


# --- Instrumentation provenance --------------------------------------------------------------


def _normalize_sources(evidence: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Project raw ``evidence_sources`` into compact ``{title, url, snippet}`` rows.

    Keeps only entries carrying at least one non-empty field so the dashboard never renders empty
    source rows.
    """
    out: list[dict[str, Any]] = []
    for src in evidence or []:
        if not isinstance(src, dict):
            continue
        url = str(src.get("url") or "").strip()
        title = str(src.get("title") or "").strip()
        snippet = str(src.get("snippet") or "").strip()
        if not (url or title or snippet):
            continue
        out.append({"title": title or url or "source", "url": url, "snippet": snippet})
    return out


def build_instrumentation_provenance(
    *,
    detection_method: str,
    lookup_status: str,
    identity_match_confidence: float,
    lookup_model: str,
    lookup_notes: str,
    evidence: list[dict[str, Any]] | None,
    local_score_path: str | None = None,
    ocr_source: str | None = None,
) -> dict[str, Any]:
    """Summarize where a piece's instrumentation came from.

    Folds the resolution path, confidence, LLM context/notes, and any web evidence into one
    self-contained object that Scripts 06/09 carry forward verbatim so the dashboard can answer
    "where did we get this instrumentation from?" (OCR of the score, web search + which URLs, or an
    LLM authority lookup).
    """
    label = METHOD_LABELS.get(detection_method, detection_method.replace("_", " ").title())
    if detection_method == METHOD_LOCAL_SCORE:
        summary = "Read from the OCR'd text of the piece's own score."
    elif detection_method == METHOD_SCORE_IMAGE:
        summary = "Read from OCR of a score image found via web search."
    elif detection_method in (METHOD_WINDREP, METHOD_AUTHORITY):
        summary = "Identified by an LLM web search of authoritative publisher/catalog sources."
    else:
        summary = (
            "No authoritative instrumentation found; only the parts observed in the library copy "
            "are known."
        )
    return {
        "method": detection_method,
        "method_label": label,
        "summary": summary,
        "status": lookup_status,
        "confidence": round(float(identity_match_confidence or 0.0), 3),
        "model": lookup_model or None,
        "notes": (lookup_notes or "").strip(),
        "local_score_path": local_score_path,
        "ocr_source": ocr_source,
        "sources": _normalize_sources(evidence),
    }


# --- Per-piece inference ---------------------------------------------------------------------


def _base_record(piece: dict[str, Any], run_id: str) -> dict[str, Any]:
    rec = new_record_envelope(run_id, RECORD_VERSION)
    rec.update({
        "piece_id": piece.get("piece_id"),
        "piece_folder": piece.get("piece_folder"),
        "catalog_number": piece.get("catalog_number"),
        "piece_title_guess": piece.get("piece_title_guess"),
        "has_score": bool(piece.get("has_score")),
    })
    return rec


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
        "ocr_source": None,
        "local_score_path": None,
        "lookup_status": lookup_status,
        "lookup_model": lookup_model,
        "lookup_notes": lookup_notes,
        "identity_match_confidence": round(float(identity_match_confidence or 0.0), 3),
        "authority_coverage": "none",
        "evidence_sources": evidence or [],
        "instrumentation_provenance": build_instrumentation_provenance(
            detection_method="conservative_fallback",
            lookup_status=lookup_status,
            identity_match_confidence=identity_match_confidence,
            lookup_model=lookup_model,
            lookup_notes=lookup_notes,
            evidence=evidence,
        ),
        "work_identity": build_work_identity(piece, doc, lookup_identity),
        "expected_parts": [],
        "missing_parts": [],
        "missing_required_parts": [],
        "missing_optional_parts": [],
        "unexpected_parts": [],
        "score_expected": False,
        "score_missing": False,
        "completeness_score": None,
        "completeness_tier": CompletenessTier.UNKNOWN,
        "observed_instrument_count": len(observed_canonicals(piece)),
        "expected_part_count": 0,
        "missing_required_count": 0,
        "unexpected_part_count": 0,
        "needs_review": True,
    })
    return rec


def build_matched_record(
    piece: dict[str, Any],
    doc: dict[str, Any] | None,
    run_id: str,
    result: dict[str, Any],
    lookup_model: str,
    *,
    detection_method: str = METHOD_AUTHORITY,
    inference_method: str = METHOD_AUTHORITY,
    ocr_source: str | None = None,
    local_score_path: str | None = None,
) -> dict[str, Any]:
    """Build a matched record by reconciling the resolved parts against observed parts.

    Shared by all three resolution paths (local score OCR, online authority lookup, remote image
    OCR); ``detection_method`` / ``inference_method`` and the optional ``ocr_source`` /
    ``local_score_path`` record which path produced the contract.
    """
    slots = normalize_expected_parts(result.get("expected_parts"))
    equivalents = build_equivalents(slots, _instrument_taxonomy().get("interchangeable_groups"))
    expected, unexpected = reconcile_parts(slots, piece.get("observed_parts", []), equivalents)

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
    # Unexpected/extra parts are informational only \u2014 a part the score doesn't enumerate is a
    # warning, not a gap that blocks review.
    needs_review = bool(missing_required or score_missing or rollup_needs_review)
    evidence = result.get("evidence_sources")
    evidence = evidence if isinstance(evidence, list) else []

    rec = _base_record(piece, run_id)
    rec.update({
        "ensemble_type": str(result.get("ensemble_type") or "unknown"),
        "ensemble_display_name": str(result.get("ensemble_display_name") or "Unknown"),
        "detection_method": detection_method,
        "inference_method": inference_method,
        "ocr_source": ocr_source,
        "local_score_path": local_score_path,
        "lookup_status": LookupStatus.MATCHED,
        "lookup_model": lookup_model,
        "lookup_notes": str(result.get("notes") or ""),
        "identity_match_confidence": round(
            float(result.get("identity_match_confidence") or 0.0), 3
        ),
        "authority_coverage": "full" if evidence else "none",
        "evidence_sources": evidence,
        "instrumentation_provenance": build_instrumentation_provenance(
            detection_method=detection_method,
            lookup_status=LookupStatus.MATCHED,
            identity_match_confidence=float(result.get("identity_match_confidence") or 0.0),
            lookup_model=lookup_model,
            lookup_notes=str(result.get("notes") or ""),
            evidence=evidence,
            local_score_path=local_score_path,
            ocr_source=ocr_source,
        ),
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


def _try_contract_from_text(
    piece: dict[str, Any],
    doc: dict[str, Any] | None,
    run_id: str,
    score_text: str,
    *,
    config: dict[str, Any],
    summarize_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    summarize_template: str,
    detection_method: str,
    ocr_source: str,
    local_score_path: str | None = None,
) -> dict[str, Any] | None:
    """Summarize OCR/score text and, if confident, return a matched record; else None.

    Used by both the local-score stage and the remote-image stage. Returns None when the text is
    too thin, the summarize call fails, no match is reported, no parts come back, or the reported
    confidence is below the configured threshold -- so the caller can fall through to the next
    stage.
    """
    min_chars = int(config.get("min_score_text_chars", 200) or 0)
    if len(score_text.strip()) < max(1, min_chars):
        return None
    contract = derive_contract_from_text(
        score_text, piece, summarize_fn, summarize_template, config
    )
    if not contract or not contract.get("match_found") or not contract.get("expected_parts"):
        return None
    contract = backfill_percussion_from_text(contract, score_text)
    confidence = float(contract.get("identity_match_confidence") or 0.0)
    threshold = float(config.get("confidence_threshold", 0.5) or 0.0)
    if confidence < threshold:
        return None
    return build_matched_record(
        piece, doc, run_id, contract, str(config.get("model") or ""),
        detection_method=detection_method,
        inference_method=detection_method,
        ocr_source=ocr_source,
        local_score_path=local_score_path,
    )


def fetch_clean_instrumentation(
    identity_result: dict[str, Any],
    piece: dict[str, Any],
    config: dict[str, Any],
    lookup_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    instrumentation_template: str,
    *,
    piece_id: Any = None,
    doc: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Phase 2 of an external lookup: fetch the identified edition's instrumentation cleanly.

    The second pass is seeded ONLY with the identity resolved in phase 1 (work identity, ensemble,
    source URLs) plus the document's work-identity candidates (composer/arranger/publisher/year) --
    never with library holdings -- so ``expected_parts`` reflect the published edition and can never
    be echoed back from the parts this library happens to own. Returns a merged result (phase-1
    identity + phase-2 parts, with evidence combined) ready for ``build_matched_record``, or None
    when the clean pass yields no parts.
    """
    query = build_instrumentation_query(identity_result, piece, doc)
    prompt = render_prompt(instrumentation_template, query)
    try:
        parts_result = lookup_fn(prompt, {**config, "piece_id": piece_id})
    except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
        logger.info("[%s] Instrumentation phase failed (%s); falling through", piece_id, exc)
        return None
    _persist_lookup_result(config, f"{piece_id}.instrumentation", prompt, parts_result)
    if not isinstance(parts_result, dict) or not parts_result.get("expected_parts"):
        return None
    merged = dict(identity_result)
    merged["expected_parts"] = parts_result.get("expected_parts")
    id_evidence = identity_result.get("evidence_sources")
    combined = list(id_evidence) if isinstance(id_evidence, list) else []
    for src in parts_result.get("evidence_sources") or []:
        if src not in combined:
            combined.append(src)
    if combined:
        merged["evidence_sources"] = combined
    if not merged.get("notes") and parts_result.get("notes"):
        merged["notes"] = parts_result.get("notes")
    return merged


def infer_piece(
    piece: dict[str, Any],
    doc: dict[str, Any] | None,
    run_id: str,
    *,
    config: dict[str, Any],
    prompt_template: str,
    lookup_enabled: bool,
    lookup_fn: Callable[[str, dict[str, Any]], dict[str, Any]],
    summarize_fn: Callable[[str, dict[str, Any]], dict[str, Any]] | None = None,
    summarize_template: str = "",
    score_ocr_summarize_template: str = "",
    score_text_provider: Callable[[dict[str, Any]], tuple[str | None, dict[str, Any] | None]]
    | None = None,
    image_fetch_fn: Callable[[str, Path, int], Path | None] | None = None,
    image_ocr_fn: Callable[[Path], str] | None = None,
    windrep_fetch_fn: Callable[[dict[str, Any], dict[str, Any]], str | None] | None = None,
    windrep_prompt_template: str = "",
    instrumentation_template: str = "",
) -> dict[str, Any]:
    """Infer expected parts for one piece across up to four stages, degrading conservatively.

    Stage A (local score OCR) runs first when a ``score_text_provider`` yields score text; Stage W
    (WindRep -- W1 direct MediaWiki fetch, then W2 dedicated LLM lookup) runs next and is always
    attempted (never gated on ensemble type); Stage B (general online authority lookup, which
    excludes WindRep) runs after; Stage C (remote image OCR) runs when the online lookup returns
    candidate images but no instrumentation. The first stage to produce a confident contract wins;
    otherwise the piece degrades to a conservative record. Stage W1's fetch is time-bounded and
    fails gracefully, so an unreachable WindRep simply falls through to W2/Stage B.

    The external LLM lookups (Stages W2 and B) are two-phase: phase 1 identifies the edition (and
    may use library holdings for disambiguation), then ``fetch_clean_instrumentation`` runs a
    holdings-free phase 2 to obtain ``expected_parts`` -- so the reported instrumentation reflects
    the published edition, never the parts this library happens to own.
    """
    instr_template = instrumentation_template or DEFAULT_INSTRUMENTATION_TEMPLATE
    model = str(config.get("model") or "")
    summarize = summarize_fn or lookup_fn
    piece_id = piece.get("piece_id")

    # --- Stage A: local score OCR --------------------------------------------------------
    if config.get("local_score_enabled", True) and score_text_provider is not None:
        logger.info("[%s] Stage A: checking for a local score to OCR", piece_id)
        score_text, score_doc = score_text_provider(piece)
        if score_text:
            score_path = (score_doc or {}).get("pdf_path") if score_doc else None
            # Reconcile noisy multi-pass OCR with the dedicated score-OCR prompt; clean embedded
            # text uses the standard summarize prompt.
            is_multipass_ocr = bool((score_doc or {}).get("score_text_is_multipass_ocr"))
            stage_a_template = (
                score_ocr_summarize_template
                if (is_multipass_ocr and score_ocr_summarize_template)
                else summarize_template
            )
            logger.info(
                "[%s] Stage A: found local score %s (%d chars of %s text); summarizing into a "
                "contract", piece_id, score_path or "?", len(score_text),
                "multi-pass OCR" if is_multipass_ocr else "score",
            )
            record = _try_contract_from_text(
                piece, doc, run_id, score_text,
                config=config, summarize_fn=summarize, summarize_template=stage_a_template,
                detection_method=METHOD_LOCAL_SCORE, ocr_source="local_score",
                local_score_path=score_path,
            )
            if record is not None:
                logger.info("[%s] Stage A succeeded: instrumentation from local score %s",
                            piece_id, score_path or "?")
                return record
            logger.info(
                "[%s] Stage A: local score did not yield a confident contract; trying "
                "online lookup", piece_id,
            )
        else:
            logger.info("[%s] Stage A: no usable local score found; trying online lookup", piece_id)

    # --- Stage W: WindRep (always tried, between local score and the general lookup) ------
    if config.get("windrep_enabled", True):
        windrep_query = build_lookup_query(piece, doc)
        # W1: direct MediaWiki fetch, time-bounded with a graceful fallback. The fetch function
        # is contracted to return None (never raise) on any failure, so no guard is needed here.
        if config.get("windrep_direct_enabled", True) and windrep_fetch_fn is not None:
            logger.info("[%s] Stage W1: trying direct WindRep fetch", piece_id)
            windrep_text = windrep_fetch_fn(windrep_query, config)
            if windrep_text:
                record = _try_contract_from_text(
                    piece, doc, run_id, windrep_text,
                    config=config, summarize_fn=summarize, summarize_template=summarize_template,
                    detection_method=METHOD_WINDREP, ocr_source="windrep_fetch",
                )
                if record is not None:
                    logger.info("[%s] Stage W1 succeeded: instrumentation from WindRep page",
                                piece_id)
                    return record
                logger.info("[%s] Stage W1: WindRep page did not yield a confident contract",
                            piece_id)
            else:
                logger.info("[%s] Stage W1: no usable WindRep page (unreachable or no match)",
                            piece_id)
        # W2: dedicated LLM lookup constrained to windrep.org.
        if (
            lookup_enabled
            and config.get("windrep_lookup_enabled", True)
            and windrep_prompt_template
        ):
            logger.info("[%s] Stage W2: querying LLM WindRep lookup", piece_id)
            windrep_domains = ["windrep.org"]
            if config.get("windrep_wayback_enabled", True):
                windrep_domains += [
                    str(d) for d in (config.get("windrep_wayback_domains") or [])
                ]
            windrep_config = {**config, "allowed_domains": windrep_domains, "piece_id": piece_id}
            windrep_prompt = render_prompt(windrep_prompt_template, windrep_query)
            try:
                windrep_result = lookup_fn(windrep_prompt, windrep_config)
            except (RuntimeError, TimeoutError, OSError, ValueError) as exc:
                logger.info("[%s] Stage W2: WindRep lookup failed (%s); falling through",
                            piece_id, exc)
                windrep_result = None
            _log_lookup_summary(piece_id, windrep_result)
            _persist_lookup_result(windrep_config, piece_id, windrep_prompt, windrep_result)
            if (
                isinstance(windrep_result, dict)
                and windrep_result.get("match_found")
            ):
                windrep_conf = float(windrep_result.get("identity_match_confidence") or 0.0)
                threshold = float(config.get("confidence_threshold", 0.5) or 0.0)
                if windrep_conf >= threshold:
                    clean = fetch_clean_instrumentation(
                        windrep_result, piece, windrep_config, lookup_fn, instr_template,
                        piece_id=piece_id, doc=doc,
                    )
                    if clean is not None:
                        logger.info(
                            "[%s] Stage W2 matched via WindRep; instrumentation from clean "
                            "phase-2 lookup (confidence %.2f)", piece_id, windrep_conf,
                        )
                        return build_matched_record(
                            piece, doc, run_id, clean, model,
                            detection_method=METHOD_WINDREP, inference_method=METHOD_WINDREP,
                        )
            logger.info("[%s] Stage W2: no confident WindRep match; falling through to general "
                        "lookup", piece_id)

    # --- Stage B: online authority lookup ------------------------------------------------
    if not lookup_enabled:
        logger.info("[%s] Online lookup disabled; recording conservative fallback", piece_id)
        return conservative_record(piece, doc, run_id, LookupStatus.DISABLED, lookup_model=model)

    logger.info("[%s] Stage B: querying online authority lookup", piece_id)
    query = build_lookup_query(piece, doc)
    prompt = render_prompt(prompt_template, query)
    try:
        result = lookup_fn(prompt, {**config, "piece_id": piece_id})
    except Exception as exc:
        logger.warning("Lookup failed for piece %s: %s", piece.get("piece_id"), exc)
        return conservative_record(
            piece, doc, run_id, LookupStatus.ERROR, lookup_notes=str(exc), lookup_model=model
        )

    _log_lookup_summary(piece_id, result)
    _persist_lookup_result(config, piece_id, prompt, result)

    if not isinstance(result, dict) or not result.get("match_found"):
        logger.info("[%s] Stage B: no authoritative match found; recording conservative fallback",
                    piece_id)
        return conservative_record(
            piece, doc, run_id, LookupStatus.NO_MATCH,
            evidence=result.get("evidence_sources") if isinstance(result, dict) else None,
            lookup_notes=str(result.get("notes") or "") if isinstance(result, dict) else "",
            lookup_model=model,
            lookup_identity=result.get("work_identity") if isinstance(result, dict) else None,
        )

    confidence = float(result.get("identity_match_confidence") or 0.0)
    threshold = float(config.get("confidence_threshold", 0.5) or 0.0)

    if confidence >= threshold:
        # Phase 2: fetch the identified edition's instrumentation WITHOUT library-holdings context.
        clean = fetch_clean_instrumentation(
            result, piece, config, lookup_fn, instr_template, piece_id=piece_id, doc=doc
        )
        if clean is not None:
            logger.info("[%s] Stage B matched an edition; instrumentation from clean phase-2 "
                        "lookup (confidence %.2f)", piece_id, confidence)
            return build_matched_record(
                piece, doc, run_id, clean, model,
                detection_method=METHOD_AUTHORITY, inference_method=METHOD_AUTHORITY,
            )

    # --- Stage C: remote image OCR (identity matched but no text instrumentation) ---------
    if confidence >= threshold:
        stage_c_ready = (
            config.get("image_ocr_enabled", True)
            and image_fetch_fn is not None
            and image_ocr_fn is not None
        )
        image_urls = extract_image_urls(result) if stage_c_ready else []
        if not stage_c_ready:
            logger.info("[%s] Stage C skipped: image OCR is disabled", piece_id)
        elif not image_urls:
            logger.info(
                "[%s] Stage C skipped: lookup matched an edition but returned no "
                "candidate_score_images to download", piece_id,
            )
        else:
            logger.info(
                "[%s] Stage C: edition matched (confidence %.2f) but no parts returned; "
                "OCRing %d candidate score image(s)", piece_id, confidence, len(image_urls),
            )
            ocr_text, ocr_count = download_and_ocr_images(
                image_urls, str(piece.get("piece_id") or "piece"), image_fetch_fn, image_ocr_fn
            )
            if ocr_count:
                logger.info("[%s] Stage C: summarizing OCR'd image text into a contract", piece_id)
                record = _try_contract_from_text(
                    piece, doc, run_id, ocr_text,
                    config=config, summarize_fn=summarize,
                    summarize_template=summarize_template,
                    detection_method=METHOD_SCORE_IMAGE, ocr_source="score_image",
                )
                if record is not None:
                    logger.info("[%s] Stage C succeeded: instrumentation from OCR'd score images",
                                piece_id)
                    return record
            else:
                logger.info(
                    "[%s] Stage C: no candidate image produced usable OCR text", piece_id
                )

    if confidence < threshold:
        logger.info(
            "[%s] Stage B match below confidence threshold (%.2f < %.2f); conservative fallback",
            piece_id, confidence, threshold,
        )
        return conservative_record(
            piece, doc, run_id, LookupStatus.LOW_CONFIDENCE,
            evidence=result.get("evidence_sources"),
            identity_match_confidence=confidence,
            lookup_notes=str(result.get("notes") or ""),
            lookup_model=model,
            ensemble_type=str(result.get("ensemble_type") or "unknown"),
            ensemble_display_name=str(result.get("ensemble_display_name") or "Unknown"),
            lookup_identity=result.get("work_identity"),
        )

    # Matched an edition, but neither text nor image OCR produced usable parts.
    logger.info("[%s] Matched an edition but no stage produced usable parts; conservative fallback",
                piece_id)
    return conservative_record(
        piece, doc, run_id, LookupStatus.NO_MATCH,
        evidence=result.get("evidence_sources"),
        identity_match_confidence=confidence,
        lookup_notes=str(result.get("notes") or ""),
        lookup_model=model,
        ensemble_type=str(result.get("ensemble_type") or "unknown"),
        ensemble_display_name=str(result.get("ensemble_display_name") or "Unknown"),
        lookup_identity=result.get("work_identity"),
    )


def build_error_record(piece: dict[str, Any], run_id: str, message: str) -> dict[str, Any]:
    rec = conservative_record(piece, None, run_id, LookupStatus.ERROR, lookup_notes=message)
    rec["processing_status"] = ProcessingStatus.ERROR
    rec["detection_method"] = "error"
    rec["error_detail"] = message
    return rec


# --- Fingerprint + ordering ------------------------------------------------------------------


def piece_fingerprint(
    piece: dict[str, Any], config_fingerprint: str, score_fingerprint: str = ""
) -> str:
    """Stable fingerprint of the inputs that affect a piece's inference.

    ``score_fingerprint`` folds in the identity of the local score PDF (path + file fingerprint) so
    incremental reuse re-runs a piece when its score changes.
    """
    keys = sorted(
        f"{o.get('canonical_instrument')}|{o.get('part_index')}|{o.get('clef')}"
        for o in piece.get("observed_parts", [])
    )
    basis = (
        f"{config_fingerprint}|{bool(piece.get('has_score'))}|{score_fingerprint}|"
        + ";".join(keys)
    )
    return sha256_text(basis)


def config_fingerprint(
    config: dict[str, Any],
    prompt_template: str,
    lookup_enabled: bool,
    summarize_template: str = "",
    local_score_enabled: bool = True,
    image_ocr_enabled: bool = True,
    windrep_enabled: bool = True,
    windrep_prompt_template: str = "",
    score_ocr_summarize_template: str = "",
    instrumentation_template: str = "",
) -> str:
    """Fingerprint of the inference-affecting configuration (invalidates stale reuse)."""
    basis = "|".join([
        str(lookup_enabled),
        str(local_score_enabled),
        str(image_ocr_enabled),
        str(windrep_enabled),
        str(config.get("model") or ""),
        str(config.get("confidence_threshold")),
        str(config.get("max_score_pages")),
        str(config.get("min_score_text_chars")),
        str(config.get("min_score_ocr_confidence")),
        ",".join(str(t) for t in (config.get("local_score_types_excluded") or ())),
        str(config.get("reocr_dpi")),
        str(config.get("windrep_wayback_enabled")),
        ",".join(str(d) for d in (config.get("windrep_wayback_domains") or ())),
        sha256_text(prompt_template),
        sha256_text(summarize_template),
        sha256_text(score_ocr_summarize_template),
        sha256_text(windrep_prompt_template),
        sha256_text(instrumentation_template),
    ])
    return sha256_text(basis)


# --- Reporting -------------------------------------------------------------------------------


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
    out.append(f"| Confident lookups | {matched} ({pct(matched, total):.1f}%) |")
    out.append(f"| Complete pieces | {complete} ({pct(complete, total):.1f}%) |")
    out.append(f"| Pieces needing review | {needs_review} |")
    out.append(f"| Processing errors | {len(errors)} |")
    out.append("")

    out.append("## Lookup Status")
    out.append("")
    out.append("| Status | Pieces |")
    out.append("| --- | --- |")
    for status in STATUS_ORDER:
        if status in status_counts:
            out.append(f"| {status} | {status_counts[status]} |")
    out.append("")

    out.append("## Completeness")
    out.append("")
    out.append("| Tier | Pieces |")
    out.append("| --- | --- |")
    for tier in TIER_ORDER:
        if tier in tier_counts:
            out.append(f"| {tier} | {tier_counts[tier]} |")
    out.append("")

    out.append("## Ensembles")
    out.append("")
    out.append("| Ensemble | Pieces |")
    out.append("| --- | --- |")
    for name in sorted(ensemble_counts, key=lambda k: (-ensemble_counts[k], k)):
        out.append(f"| {md_cell(name)} | {ensemble_counts[name]} |")
    out.append("")

    out.append("## Most Commonly Missing Parts")
    out.append("")
    if missing_part_counts:
        out.append("| Part | Pieces missing it |")
        out.append("| --- | --- |")
        ranked = sorted(missing_part_counts, key=lambda k: (-missing_part_counts[k], k))
        for label in ranked[:25]:
            out.append(f"| {md_cell(label)} | {missing_part_counts[label]} |")
    else:
        out.append("No required parts are missing across the collection.")
    out.append("")

    out.append("## Per-Piece Breakdown")
    out.append("")
    limit = meta.get("detail_limit", 200)
    shown = sorted(records, key=piece_sort_key)[:limit]
    out.append("| Catalog | Piece | Ensemble | Lookup | Completeness | Missing required | Review |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for rec in shown:
        catalog = rec.get("catalog_number") or ""
        name = md_cell(rec.get("piece_title_guess") or rec.get("piece_folder"))
        ensemble = md_cell(rec.get("ensemble_type"))
        status = rec.get("lookup_status", "")
        score = rec.get("completeness_score")
        completeness = "-" if score is None else f"{score * 100:.0f}% ({rec.get('completeness_tier')})"
        missing = md_cell(", ".join(rec.get("missing_required_parts", [])) or "-")
        review = "yes" if rec.get("needs_review") else ""
        out.append(
            f"| {catalog} | {name} | {ensemble} | {status} | {completeness} | {missing} | {review} |"
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
    if meta.get("summarize_source"):
        out.append(f"| Summarize prompt source | {meta['summarize_source']} |")
    if meta.get("score_ocr_summarize_source"):
        out.append(f"| Score-OCR prompt source | {meta['score_ocr_summarize_source']} |")
    out.append(f"| Local score OCR | {'enabled' if meta.get('local_score_enabled') else 'disabled'} |")
    out.append(f"| Lookup | {'enabled' if meta['lookup_enabled'] else 'disabled'} |")
    out.append(f"| Image OCR | {'enabled' if meta.get('image_ocr_enabled') else 'disabled'} |")
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

    for rec in sorted(records, key=piece_sort_key):
        catalog = rec.get("catalog_number") or "?"
        title = rec.get("piece_title_guess") or rec.get("piece_folder") or rec.get("piece_id")
        out.append(f"## {catalog} - {title}")
        out.append("")
        out.extend(render_piece_instrumentation_body(rec))

    return "\n".join(out) + "\n"


def piece_instrumentation_filename(rec: dict[str, Any]) -> str:
    """Deterministic per-piece Markdown filename: ``<catalog>_<title>_<piece_id>.md`` (slugged).

    Includes ``piece_id`` so the name is unique and stable across runs (so incremental runs never
    orphan a renamed file), and leads with the catalog number so files sort like the reports.
    """
    catalog = rec.get("catalog_number") or "000"
    title = rec.get("piece_title_guess") or rec.get("piece_folder") or "piece"
    piece_id = rec.get("piece_id") or "piece"
    return _safe_filename(f"{catalog}_{title}_{piece_id}") + ".md"


def render_piece_instrumentation_body(rec: dict[str, Any]) -> list[str]:
    """Render one piece's instrumentation body (identity bullets, parts table, sources).

    The caller supplies the heading; this returns everything under it, so it is shared by both the
    single-file report and the per-piece split files.
    """
    out: list[str] = []
    ensemble = rec.get("ensemble_display_name") or "Unknown"
    ensemble_type = rec.get("ensemble_type") or "unknown"
    status = rec.get("lookup_status", "unknown")
    confidence = rec.get("identity_match_confidence")
    score = rec.get("completeness_score")
    completeness = "-" if score is None else f"{score * 100:.0f}% ({rec.get('completeness_tier')})"

    out.append(f"- **Ensemble:** {md_cell(ensemble)} (`{ensemble_type}`)")
    out.append(
        f"- **Lookup:** {status} - confidence {confidence} - completeness {completeness}"
    )
    identity = _format_identity((rec.get("work_identity") or {}).get("resolved", {}))
    if identity:
        out.append(f"- **Edition:** {md_cell(identity)}")
    if rec.get("lookup_notes"):
        out.append(f"- **Notes:** {md_cell(rec['lookup_notes'])}")
    out.append("")

    expected_parts = rec.get("expected_parts") or []
    if expected_parts:
        out.append("| # | Instrument | Score Label | Section | Required | Observed |")
        out.append("| --- | --- | --- | --- | --- | --- |")
        for part in expected_parts:
            idx = part.get("part_index")
            idx_cell = "-" if idx is None else str(idx)
            required = "required" if part.get("required") else "optional"
            if part.get("present"):
                # Transposition is now part of instrument identity (its own row), so the Observed
                # column annotates only the clef edition(s) that satisfied the slot.
                editions = part.get("observed_clefs") or []
                observed = f"yes ({', '.join(editions)})" if editions else "yes"
            else:
                observed = "MISSING"
            out.append(
                f"| {idx_cell} | {md_cell(instrument_display_name(part.get('canonical_instrument')))} "
                f"| {md_cell(part.get('label'))} | {md_cell(part.get('section'))} "
                f"| {required} | {observed} |"
            )
        out.append("")
        unexpected = rec.get("unexpected_parts") or []
        if unexpected:
            labels = ", ".join(md_cell(_unexpected_label(u)) for u in unexpected)
            out.append(f"_Observed but not expected (informational): {labels}_")
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
            stitle = md_cell(src.get("title") or src.get("url") or "source")
            url = src.get("url") or ""
            snippet = md_cell(src.get("snippet") or "")
            line = f"- [{stitle}]({url})" if url else f"- {stitle}"
            if snippet:
                line += f' - "{snippet}"'
            out.append(line)
        out.append("")

    return out


def render_piece_instrumentation_doc(rec: dict[str, Any], meta: dict[str, Any]) -> str:
    """Render a standalone per-piece instrumentation document (for the split-report files)."""
    catalog = rec.get("catalog_number") or "?"
    title = rec.get("piece_title_guess") or rec.get("piece_folder") or rec.get("piece_id")
    out: list[str] = []
    out.append(f"# {catalog} - {title}")
    out.append("")
    out.append(
        f"_Generated {meta['generated_at']} - run `{meta['run_id']}` - "
        f"model {meta['model'] or '(CLI default)'}_"
    )
    out.append("")
    out.extend(render_piece_instrumentation_body(rec))
    return "\n".join(out) + "\n"


def build_instrumentation_index(
    records: list[dict[str, Any]], meta: dict[str, Any], split_dirname: str
) -> str:
    """Render the instrumentation index: run metadata plus a table linking to per-piece files."""
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
        f"Instrumentation comes from online score lookups. "
        f"{len(matched)} of {len(records)} piece(s) have an authoritative instrumentation list; "
        f"the rest fell back to a conservative record (no expected parts asserted). "
        f"Each piece links to its own file under `{split_dirname}/`."
    )
    out.append("")
    out.append("| Catalog | Piece | Ensemble | Lookup | Completeness | Missing required | Details |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for rec in sorted(records, key=piece_sort_key):
        catalog = rec.get("catalog_number") or ""
        name = md_cell(rec.get("piece_title_guess") or rec.get("piece_folder"))
        ensemble = md_cell(rec.get("ensemble_type"))
        status = rec.get("lookup_status", "")
        score = rec.get("completeness_score")
        completeness = "-" if score is None else f"{score * 100:.0f}% ({rec.get('completeness_tier')})"
        missing = md_cell(", ".join(rec.get("missing_required_parts", [])) or "-")
        link = f"[details]({split_dirname}/{piece_instrumentation_filename(rec)})"
        out.append(
            f"| {catalog} | {name} | {ensemble} | {status} | {completeness} | {missing} | {link} |"
        )
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
    part_predictions: Path = typer.Option(
        Path("data/part_predictions.jsonl"),
        help="Script 03 per-document predictions (used to locate a piece's local score)",
    ),
    extracted_text: Path = typer.Option(
        Path("data/extracted_text.jsonl"),
        help="Script 02 per-page text (source of local-score text for Stage A)",
    ),
    library_root: Path = typer.Option(
        None,
        help="Library root for resolving score PDFs when re-OCR is needed (optional)",
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
    only_piece: int = typer.Option(
        None,
        help=(
            "Catalogue number of a single piece to force-recompute (re-runs its lookup/OCR); "
            "every other piece's prior record is preserved verbatim."
        ),
    ),
    lookup_enabled: bool = typer.Option(
        True, "--lookup/--no-lookup", help="Enable online score lookup (Stage B)"
    ),
    local_score_enabled: bool = typer.Option(
        True, "--local-score/--no-local-score",
        help="Enable local-score OCR before the online lookup (Stage A)",
    ),
    image_ocr_enabled: bool = typer.Option(
        True, "--image-ocr/--no-image-ocr",
        help="Enable local OCR of images the online lookup returns (Stage C)",
    ),
    windrep_enabled: bool = typer.Option(
        True, "--windrep/--no-windrep",
        help="Enable the WindRep stage (direct fetch + LLM lookup) before the general lookup",
    ),
    model: str = typer.Option("", help="Override the Copilot CLI model (blank = config/default)"),
    timeout: int = typer.Option(0, help="Override per-piece subprocess timeout in seconds (0=cfg)"),
    stream_lookup: bool = typer.Option(
        True, "--stream-lookup/--no-stream-lookup",
        help="Stream the Copilot CLI's output live so long lookups don't look frozen",
    ),
    save_lookups: bool = typer.Option(
        True, "--save-lookups/--no-save-lookups",
        help="Save each raw lookup result (prompt + parsed JSON) under cache/llm/lookups",
    ),
    concurrency: int = typer.Option(
        4, "--concurrency", "-j",
        help="Number of pieces to look up in parallel (I/O-bound). 1 = sequential; 3-4 recommended",
    ),
    split_instrumentation: bool = typer.Option(
        True, "--split-instrumentation/--no-split-instrumentation",
        help="Write one instrumentation file per piece (under a subfolder named after the report) "
             "as each finishes, with the main report as a linking index. Off = single file.",
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
    if not config.get("local_score_enabled", True):
        local_score_enabled = False
    if not config.get("image_ocr_enabled", True):
        image_ocr_enabled = False
    config["local_score_enabled"] = local_score_enabled
    config["image_ocr_enabled"] = image_ocr_enabled
    if not config.get("windrep_enabled", True):
        windrep_enabled = False
    config["windrep_enabled"] = windrep_enabled
    config["windrep_cache_dir"] = str(
        Path(config.get("windrep_cache_dir") or "cache/windrep").resolve()
    )
    if model.strip():
        config["model"] = model.strip()
    if timeout > 0:
        config["timeout_seconds"] = timeout
    config["stream_output"] = stream_lookup
    config["lookup_debug_dir"] = (
        str(Path("cache/llm/lookups").resolve()) if save_lookups else None
    )

    template_path = Path(config.get("prompt_template_path", "")).resolve()
    prompt_template, prompt_source = load_prompt_template(template_path)

    instrumentation_path = Path(config.get("instrumentation_prompt_template_path", "")).resolve()
    instrumentation_template, instrumentation_source = load_prompt_template(instrumentation_path)
    if instrumentation_template is DEFAULT_PROMPT_TEMPLATE:
        # load_prompt_template falls back to the identify template when the file is missing; the
        # phase-2 pass needs the holdings-free instrumentation template instead.
        instrumentation_template, instrumentation_source = DEFAULT_INSTRUMENTATION_TEMPLATE, "builtin"
    logger.info("Instrumentation (phase 2) prompt: %s (%s)", instrumentation_path,
                instrumentation_source)

    summarize_path = Path(config.get("summarize_prompt_template_path", "")).resolve()
    summarize_template, summarize_source = load_summarize_template(summarize_path)

    score_ocr_summarize_path = Path(
        config.get("summarize_score_ocr_prompt_template_path", "")
    ).resolve()
    score_ocr_summarize_template, score_ocr_summarize_source = load_summarize_template(
        score_ocr_summarize_path
    )

    windrep_prompt_template = ""
    if windrep_enabled:
        windrep_path = Path(config.get("windrep_prompt_template_path", "")).resolve()
        windrep_prompt_template, windrep_source = load_prompt_template(windrep_path)
        logger.info("WindRep lookup prompt: %s (%s)", windrep_path, windrep_source)

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

    # Local-score inputs (Stage A): map each piece to its score document(s) from Script 03, and
    # index Script 02 per-page text by the score PDFs only (bounding memory on large libraries).
    score_docs_by_piece: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if local_score_enabled:
        for rec in read_jsonl(part_predictions.resolve()):
            if rec.get("is_score") and rec.get("piece_id") and rec.get("pdf_path"):
                if not _score_type_usable(rec.get("score_type"), config):
                    logger.info(
                        "[%s] Skipping abridged local score %s (score_type=%s); not a reliable "
                        "instrumentation source",
                        rec.get("piece_id"), rec.get("pdf_path"), rec.get("score_type") or "?",
                    )
                    continue
                score_docs_by_piece[rec["piece_id"]].append(rec)

    score_pdf_paths = {
        d.get("pdf_path") for docs in score_docs_by_piece.values() for d in docs
    }
    text_by_pdf: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if local_score_enabled and score_pdf_paths:
        for rec in read_jsonl(extracted_text.resolve()):
            pdf_path = rec.get("pdf_path")
            if pdf_path in score_pdf_paths:
                text_by_pdf[pdf_path].append(rec)

    lib_root = library_root.resolve() if library_root else None
    max_score_pages = int(config.get("max_score_pages", 2) or 2)
    min_score_text = int(config.get("min_score_text_chars", 200) or 0)
    min_score_ocr_conf = float(config.get("min_score_ocr_confidence", 0) or 0)
    reocr_dpi = int(config.get("reocr_dpi", 300) or 300)

    def _score_fingerprint(piece: dict[str, Any]) -> str:
        best = find_best_score_doc(str(piece.get("piece_id") or ""), score_docs_by_piece)
        if not best:
            return ""
        return f"{best.get('pdf_path')}|{best.get('file_fingerprint') or ''}"

    def _score_text_provider(
        piece: dict[str, Any],
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Return (score_text, score_doc) for a piece, reusing Script 02 text.

        When the leading pages were OCR'd, every multi-pass OCR candidate Script 02 already computed
        is handed to the LLM (flagged so Stage A uses the dedicated score-OCR prompt to reconcile
        the noisy passes) -- gated only when even the best pass is unreadably low. Otherwise the
        embedded/single-pass text is used, re-OCRing from scratch when it is thin.
        """
        pid = piece.get("piece_id")
        best = find_best_score_doc(str(pid or ""), score_docs_by_piece)
        if not best:
            logger.info("[%s] No local score document found in part predictions", pid)
            return None, None
        pdf_path = str(best.get("pdf_path") or "")
        logger.info("[%s] Selected local score %s (score_type=%s)",
                    pid, pdf_path, best.get("score_type") or "?")

        # Preferred path: reuse Script 02's multi-pass OCR candidates and let the LLM reconcile.
        multipass = gather_score_ocr_candidates_text(pdf_path, text_by_pdf, max_score_pages)
        if multipass:
            best_conf = best_score_ocr_confidence(pdf_path, text_by_pdf, max_score_pages)
            if best_conf is not None and best_conf < min_score_ocr_conf:
                logger.info(
                    "[%s] Local score OCR quality too low (best pass confidence %.2f < %.2f); "
                    "skipping local score and using online sources",
                    pid, best_conf, min_score_ocr_conf,
                )
                return None, best
            logger.info(
                "[%s] Reusing %d chars of multi-pass score OCR from Script 02 (best pass %.2f)",
                pid, len(multipass), best_conf if best_conf is not None else -1.0,
            )
            return multipass, {**best, "score_text_is_multipass_ocr": True}

        # Fallback: embedded text (born-digital score) or legacy data without OCR candidates.
        text = get_extracted_score_text(pdf_path, text_by_pdf, max_score_pages)
        if len(text.strip()) < max(1, min_score_text):
            abs_path = (lib_root / pdf_path) if lib_root else Path(pdf_path)
            logger.info(
                "[%s] Reused text is thin (%d < %d chars); re-OCRing %s",
                pid, len(text.strip()), min_score_text, abs_path,
            )
            reocr_dir = Path(tempfile.mkdtemp(prefix=f"reocr_{pid}_"))
            reocr_text = reocr_score_pages(abs_path, max_score_pages, reocr_dpi, reocr_dir)
            if len(reocr_text.strip()) > len(text.strip()):
                text = reocr_text
        else:
            logger.info("[%s] Reusing %d chars of extracted score text from Script 02",
                        pid, len(text.strip()))
        return (text or None), best

    cfg_fp = config_fingerprint(
        config, prompt_template, lookup_enabled,
        summarize_template, local_score_enabled, image_ocr_enabled,
        windrep_enabled, windrep_prompt_template,
        score_ocr_summarize_template,
        instrumentation_template,
    )

    ckpt_path = make_checkpoint_path(output, CHECKPOINT_FILENAME)
    checkpoint = load_checkpoint(ckpt_path, RECORD_VERSION, logger)
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
    persisted_fingerprints: dict[str, str] = {}

    # Per-piece instrumentation split: files live in a subfolder named after the report's stem
    # (e.g. data/expected_instrumentation/) and are written as each piece finishes.
    instrumentation_path = output_instrumentation.resolve()
    split_dir = instrumentation_path.parent / instrumentation_path.stem
    split_dirname = instrumentation_path.stem
    do_split = write_report and split_instrumentation
    if do_split:
        split_dir.mkdir(parents=True, exist_ok=True)
    stream_meta = {
        "generated_at": utc_now_iso(),
        "run_id": run_id,
        "model": config.get("model") or "",
    }

    def _write_piece_doc(record: dict[str, Any]) -> None:
        if not do_split:
            return
        atomic_write_text(
            split_dir / piece_instrumentation_filename(record),
            render_piece_instrumentation_doc(record, stream_meta),
        )

    def _write_stream_checkpoint(fp_dict: dict[str, str], record_count: int) -> None:
        atomic_write_json(
            ckpt_path,
            build_checkpoint(
                RECORD_VERSION, run_id, dict(fp_dict),
                pieces_input=pieces.as_posix(),
                output=output.as_posix(),
                config_source=config_source,
                config_fingerprint=cfg_fp,
                lookup_enabled=lookup_enabled,
                local_score_enabled=local_score_enabled,
                image_ocr_enabled=image_ocr_enabled,
                record_count=record_count,
            ),
        )

    def _persist(_item: tuple[int, dict[str, Any], str, str], record: dict[str, Any]) -> None:
        # Runs in the caller's thread as each piece completes: append, then durably persist the
        # record, the checkpoint, and (when splitting) the per-piece file, so an interrupted run
        # keeps everything finished so far and can resume via --mode incremental.
        rebuilt.append(record)
        pid = record.get("piece_id")
        if pid:
            persisted_fingerprints[pid] = fingerprints.get(pid, "")
        atomic_write_jsonl(output, sorted(rebuilt, key=piece_sort_key))
        _write_stream_checkpoint(persisted_fingerprints, len(rebuilt))
        _write_piece_doc(record)

    total_pieces = sum(1 for p in piece_records if p.get("piece_id"))
    logger.info(
        "Processing %d piece(s) in %s mode (local-score %s, lookup %s, image-OCR %s).",
        total_pieces,
        mode,
        "on" if local_score_enabled else "off",
        "on" if lookup_enabled else "off",
        "on" if image_ocr_enabled else "off",
    )

    seen = 0
    to_process: list[tuple[int, dict[str, Any], str, str]] = []
    for piece in piece_records:
        piece_id = piece.get("piece_id")
        if not piece_id:
            continue
        seen += 1
        fingerprint = piece_fingerprint(piece, cfg_fp, _score_fingerprint(piece))
        fingerprints[piece_id] = fingerprint

        title = piece.get("piece_title_guess") or piece.get("piece_folder") or piece_id
        catalog = piece.get("catalog_number") or "?"

        prior = previous_records.get(piece_id)
        decision = only_piece_decision(
            piece.get("catalog_number") or piece.get("piece_folder"),
            only_piece,
            prior is not None,
        )
        normal_reuse = (
            mode == "incremental"
            and prior is not None
            and prior.get("record_version") == RECORD_VERSION
            and prior_fingerprints.get(piece_id) == fingerprint
        )
        if decision == "reuse" or (decision == "normal" and normal_reuse):
            rebuilt.append(prior)
            reused += 1
            persisted_fingerprints[piece_id] = fingerprint
            _write_piece_doc(prior)
            logger.info("[%d/%d] Reusing cached result: %s (cat %s)", seen, total_pieces,
                        title, catalog)
            continue

        to_process.append((seen, piece, str(title), str(catalog)))

    # Parallel lookups are I/O-bound (each waits on the Copilot subprocess + network), so a small
    # thread pool overlaps the waiting without loading the CPU. Live streaming is disabled when
    # running in parallel so multiple CLIs don't interleave into unreadable output.
    workers = max(1, concurrency)
    if workers > 1 and config.get("stream_output"):
        logger.info(
            "Disabling live CLI streaming for parallel lookups (concurrency=%d).", workers
        )
        config = {**config, "stream_output": False}

    def _process(item: tuple[int, dict[str, Any], str, str]) -> dict[str, Any]:
        idx, piece, title, catalog = item
        logger.info("[%d/%d] %s: %s (cat %s)", idx, total_pieces,
                    "Inferring" if (lookup_enabled or local_score_enabled)
                    else "Recording (no lookup)", title, catalog)
        piece_start = time.perf_counter()
        try:
            record = infer_piece(
                piece,
                doc_map.get(piece["piece_id"]),
                run_id,
                config=config,
                prompt_template=prompt_template,
                lookup_enabled=lookup_enabled,
                lookup_fn=run_copilot_lookup,
                summarize_fn=run_copilot_lookup,
                summarize_template=summarize_template,
                score_ocr_summarize_template=score_ocr_summarize_template,
                score_text_provider=_score_text_provider if local_score_enabled else None,
                image_fetch_fn=download_image if image_ocr_enabled else None,
                image_ocr_fn=ocr_image_file if image_ocr_enabled else None,
                windrep_fetch_fn=(
                    fetch_windrep_instrumentation
                    if (windrep_enabled and config.get("windrep_direct_enabled", True))
                    else None
                ),
                windrep_prompt_template=windrep_prompt_template,
                instrumentation_template=instrumentation_template,
            )
        except Exception as exc:
            logger.exception("Unexpected inference error on piece %s", piece.get("piece_id"))
            record = build_error_record(piece, run_id, str(exc))
        logger.info(
            "[%d/%d] Done: %s -> status=%s method=%s tier=%s (%.1fs)",
            idx, total_pieces, title,
            record.get("lookup_status"), record.get("detection_method"),
            record.get("completeness_tier"),
            time.perf_counter() - piece_start,
        )
        return record

    if to_process:
        logger.info("Running %d lookup(s) with concurrency %d.", len(to_process), workers)
        run_with_progress(to_process, _process, workers, on_result=_persist)
    processed = len(to_process)

    rebuilt.sort(key=piece_sort_key)
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
            "summarize_source": summarize_source,
            "score_ocr_summarize_source": score_ocr_summarize_source,
            "lookup_enabled": lookup_enabled,
            "local_score_enabled": local_score_enabled,
            "image_ocr_enabled": image_ocr_enabled,
            "model": config.get("model") or "",
            "output": output.as_posix(),
            "detail_limit": report_detail_limit,
        }
        report = build_report(rebuilt, meta)
        atomic_write_text(output_report.resolve(), report)
        logger.info("Wrote Markdown report: %s", output_report.resolve())

        if split_instrumentation:
            split_dir.mkdir(parents=True, exist_ok=True)
            valid_names: set[str] = set()
            for rec in rebuilt:
                name = piece_instrumentation_filename(rec)
                valid_names.add(name)
                atomic_write_text(
                    split_dir / name, render_piece_instrumentation_doc(rec, meta)
                )
            # Prune per-piece files that no longer correspond to a current piece (only *.md in the
            # managed subfolder are touched).
            for existing in split_dir.glob("*.md"):
                if existing.name not in valid_names:
                    try:
                        existing.unlink()
                    except OSError:
                        pass
            index = build_instrumentation_index(rebuilt, meta, split_dirname)
            atomic_write_text(instrumentation_path, index)
            logger.info(
                "Wrote instrumentation index: %s (%d per-piece file(s) under %s)",
                instrumentation_path, len(rebuilt), split_dir,
            )
        else:
            instrumentation = build_instrumentation_report(rebuilt, meta)
            atomic_write_text(instrumentation_path, instrumentation)
            logger.info("Wrote instrumentation report: %s", instrumentation_path)

    new_checkpoint = build_checkpoint(
        RECORD_VERSION,
        run_id,
        fingerprints,
        pieces_input=pieces.as_posix(),
        output=output.as_posix(),
        config_source=config_source,
        config_fingerprint=cfg_fp,
        lookup_enabled=lookup_enabled,
        local_score_enabled=local_score_enabled,
        image_ocr_enabled=image_ocr_enabled,
        record_count=len(rebuilt),
    )
    atomic_write_json(ckpt_path, new_checkpoint)

    logger.info(
        "Expected-parts inference completed: total=%d processed=%d reused=%d output=%s",
        len(rebuilt),
        processed,
        reused,
        output,
    )


if __name__ == "__main__":
    app()
