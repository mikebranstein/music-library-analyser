# Script 04 — Expected Parts Inference Implementation Plan

## 1. Objective

Estimate the set of parts that *should* exist for each piece so that Scripts 06/07/08 can flag
missing parts, score completeness, and prioritize manual review. Script 04 consumes the Script 03
per-piece observed-parts rollup (`data/observed_parts_by_piece.jsonl`, schema 1.1) and writes one
expected-parts record per piece to `data/expected_parts.jsonl`, plus a Markdown summary.

The **primary** inference path is an **online lookup of the actual published score**: for each
piece, Script 04 builds a work-identity query from the metadata it already has and asks an LLM
(via the **GitHub Copilot CLI**) to search authoritative web sources (publisher, distributor, and
library catalog listings) and return that specific edition's real instrumentation. The ensemble
type is **not hardcoded** — it is whatever the found score is (concert/wind band, wind ensemble,
orchestra, etc.). When the lookup is disabled, finds no
confident match, or fails, Script 04 falls back to a **conservative, observed-only** record and
flags the piece for review; it never invents a "missing" part without an authoritative source.

## 2. Why lookup-primary (no hardcoded templates)

Earlier drafts matched each piece to a single hardcoded ensemble template. That is wrong
for a mixed library: the same folder set can contain concert/wind band, wind ensemble, or orchestral
arrangements, and editions differ in their exact seatings. Rather than
enumerate every ensemble's canonical instrumentation ourselves (brittle, and still only an
approximation of any *specific* published edition), we look up the real edition online and use its
actual part list. Deterministic templates are removed entirely; the only non-lookup path is the
conservative fallback, which asserts nothing about missing parts.

## 3. Runtime engine: GitHub Copilot CLI

The lookup is executed by shelling out to the installed **GitHub Copilot CLI** (`copilot`,
non-interactive prompt mode). This reuses the user's existing Copilot entitlement and web-capable
agent instead of wiring a bespoke search/LLM stack into the repo.

Invocation shape (built by `build_cli_args`):

```text
copilot -p "<PROMPT>" --allow-all-tools --allow-all-urls -s --no-color \
        --log-level none --no-ask-user [--model <MODEL>] [extra_args...]
```

- `-p/--prompt` runs headless and exits after completion.
- `--allow-all-tools` is required for non-interactive use; it enables the built-in `web_fetch`
  tool used for research.
- `--allow-all-urls` (or a configured `--allow-url=<domains>` allowlist) grants the web access the
  research needs; `web_fetch` still enforces SSRF protection (no localhost/private IPs).
- `-s` prints only the agent's final response (no usage stats), `--no-color` + `--log-level none`
  keep stdout clean for parsing.
- Auth uses the CLI's stored credentials or `COPILOT_GITHUB_TOKEN`/`GH_TOKEN`/`GITHUB_TOKEN`.

The subprocess call is isolated in `run_copilot_lookup(prompt, config)` so tests monkeypatch it and
never touch the network.

### Input contract (into every prompt)

The prompt (from `config/llm_prompts/lookup_instrumentation.txt`) is rendered with a per-piece
query built by `build_lookup_query`:

- `title_guess`, `catalog_number`, `piece_folder`
- identity candidates joined from Script 02 documents (composer, arranger, publisher, series, year)
- a compact summary of the observed instruments/part counts (so the model can disambiguate and
  sanity-check the edition it finds)

### Exit contract (out of every prompt)

The prompt instructs the model to emit **exactly one JSON object between sentinels**:

```text
<<<SCORE_JSON>>>
{ ...single JSON object... }
<<<END_SCORE_JSON>>>
```

Schema the model must follow:

```json
{
  "match_found": true,
  "identity_match_confidence": 0.0,
  "ensemble_type": "concert_band | wind_ensemble | orchestra | ... (inferred, free-form)",
  "ensemble_display_name": "human label",
  "score_expected": true,
  "work_identity": {
    "title": null, "composer": null, "arranger": null,
    "publisher": null, "catalog_number": null, "year": null
  },
  "expected_parts": [
    {"canonical_instrument": "cornet", "part_index": 1,
     "label": "Solo Cornet", "section": "cornets_trumpets", "required": true}
  ],
  "evidence_sources": [
    {"url": "https://...", "title": "...", "snippet": "...", "retrieved": "date"}
  ],
  "notes": "free text; conflicts, assumptions, or why no match"
}
```

`parse_lookup_response` extracts the sentinel block (falling back to the largest `{...}` JSON
object), then validates it. Malformed/absent JSON raises and the piece degrades to the fallback.

## 4. Dependency and Degradation Policy

- Pure standard library + `typer` + optional `PyYAML` for config; **plus** the external `copilot`
  binary at runtime (only when lookup is enabled).
- If `copilot` is not on `PATH`, lookup is disabled, the subprocess fails, or the JSON is invalid,
  the piece degrades to `fallback_conservative` (never aborts the run, never fabricates sources).
- `--no-lookup` forces the deterministic, offline, observed-only path for every piece (useful for
  CI and reproducible dry runs).

## 5. Input Contract

- `--pieces data/observed_parts_by_piece.jsonl` (required): Script 03 rollup, schema 1.1.
- `--documents data/documents.jsonl` (optional): work-identity seeds joined by `piece_id`.
- `--config config/score_lookup.yaml` (optional): lookup engine settings (see §6).

If the rollup is empty/missing the script exits with a `typer.BadParameter`.

## 6. Lookup config (`config/score_lookup.yaml`)

```yaml
enabled: true                 # master switch (also toggled by --lookup/--no-lookup)
command: copilot              # CLI binary name/path
model: ""                     # "" = CLI default model
timeout_seconds: 300          # per-piece subprocess timeout
confidence_threshold: 0.5     # below this, a match degrades to conservative (evidence kept)
allowed_domains: []           # [] = --allow-all-urls; else --allow-url=<domains>
prompt_template_path: config/llm_prompts/lookup_instrumentation.txt
extra_args: []                # appended verbatim to the copilot invocation
```

A built-in default mirrors this, so the script runs without the YAML or PyYAML.

## 7. Per-piece flow

1. Build the work-identity query from the piece + joined document.
2. If lookup disabled → `fallback_conservative` (`lookup_status = "disabled"`).
3. Else render the prompt, call `run_copilot_lookup`, and parse the JSON:
   - subprocess/parse error → conservative (`lookup_status = "error"`, `needs_review`).
   - `match_found == false` → conservative (`lookup_status = "no_match"`, evidence/notes kept).
   - `identity_match_confidence < threshold` → conservative (`lookup_status = "low_confidence"`,
     evidence kept, expected parts intentionally not asserted).
   - otherwise → `authority_lookup` (`lookup_status = "matched"`).
4. On a confident match, normalize the returned `expected_parts` into slots and reconcile them
   against the observed parts.

## 8. Observed-vs-Expected Reconciliation

`reconcile_parts` is reused unchanged and is **count-based per canonical instrument** (robust to
null/inconsistent `part_index`):

1. Group expected slots (from the lookup result) by `canonical_instrument`.
2. Group observed parts by `canonical_instrument`.
3. Match by explicit `part_index` first, then consume remaining observed against remaining slots in
   order.
4. Expected slots left unconsumed → `missing` (retain `required` flag + label).
5. Observed instruments/parts with no matching slot → `unexpected_parts` (surfaced, not "missing").

## 9. Completeness Scoring (deterministic, 0..1)

Applies only to confident matches (fallback records use `completeness_score = null`,
`completeness_tier = "unknown"`):

- `required_total` = number of required slots (+1 if `score_expected`).
- `required_present` = required slots present (+ score if expected & present).
- `completeness_score = required_present / required_total` (1.0 when `required_total == 0`).
- `completeness_tier`: `complete` (no missing required and score ok) / `near_complete` (≥0.85) /
  `incomplete` (≥0.5) / `severely_incomplete`.
- `needs_review = True` when: any missing required part, any `unexpected_parts`, `score_missing`,
  the rollup reported `needs_review_count > 0`, or the record used any fallback path.

## 10. Output Contract

`data/expected_parts.jsonl` — one record per piece (schema **2.0**):

- `record_version`, `run_id`, `piece_id`, `piece_folder`, `catalog_number`, `piece_title_guess`
- `ensemble_type` (free-form, from lookup, or `"unknown"`), `ensemble_display_name`
- `detection_method` (`authority_lookup` | `conservative_fallback`)
- `inference_method` (`authority_lookup` | `fallback_conservative`)
- `lookup_status` (`matched` | `low_confidence` | `no_match` | `disabled` | `error`)
- `lookup_model`, `lookup_notes`
- `identity_match_confidence`, `authority_coverage` (`full` | `none`)
- `evidence_sources` (list of `{url, title, snippet, retrieved}`)
- `work_identity` (local seeds merged with lookup-provided fields)
- `expected_parts` (list of `{canonical_instrument, part_index, label, section, required, present}`)
- `missing_parts`, `missing_required_parts`, `missing_optional_parts`
- `unexpected_parts` (list of `{canonical_instrument, part_index, predicted_part, count}`)
- `has_score`, `score_expected`, `score_missing`
- `completeness_score` (null on fallback), `completeness_tier`, `needs_review`
- counts: `observed_instrument_count`, `expected_part_count`, `missing_required_count`,
  `unexpected_part_count`
- `processing_status`, `processing_timestamp`

`data/expected_parts_report.md` — At-a-Glance metrics, lookup-status distribution, completeness
tiers, ensemble distribution, most-commonly-missing parts, and a per-piece breakdown.

`data/.expected_parts_checkpoint.json` — checkpoint for incremental mode.

## 11. Idempotency & Incremental Mode

- Records sorted by `(catalog_number, piece_folder)` for stable output.
- Per-piece fingerprint = sha256 of observed-part keys + `has_score` + lookup-affecting config
  (`enabled`, `model`, `confidence_threshold`, prompt-template hash).
- `--mode incremental` reuses a prior record when the fingerprint and `RECORD_VERSION` match. This
  is important: it avoids re-spending Copilot credits and re-querying the web for unchanged pieces.
- Checkpoint version mismatch forces a full rebuild (benign warning).

## 12. CLI Design

| Option | Default | Purpose |
| --- | --- | --- |
| `--pieces` | `data/observed_parts_by_piece.jsonl` | Script 03 rollup input |
| `--documents` | `data/documents.jsonl` | Optional identity seeds |
| `--config` | `config/score_lookup.yaml` | Lookup engine config |
| `--output` | `data/expected_parts.jsonl` | Expected-parts output |
| `--output-report` | `data/expected_parts_report.md` | Markdown summary |
| `--report / --no-report` | `--report` | Toggle the Markdown report |
| `--report-detail-limit` | `200` | Max rows in the per-piece detail table |
| `--mode` | `full` | `full` or `incremental` |
| `--lookup / --no-lookup` | `--lookup` | Enable online score lookup (primary path) |
| `--model` | (config) | Override the Copilot CLI model |
| `--timeout` | (config) | Override per-piece subprocess timeout (seconds) |
| `--log-level` | `INFO` | Logging verbosity |

## 13. Failure Policy

- A piece that raises during processing is written with `processing_status = "error"`,
  `needs_review = True`, and empty expected/missing lists (never aborts the run).
- Disabled/no-match/low-confidence lookups are normal outcomes (`fallback_conservative`), not
  errors.

## 14. Testing Strategy

All tests inject a fake lookup callable or monkeypatch `run_copilot_lookup`; **no network or CLI is
invoked**.

- Unit: config + prompt-template builtin fallback; `build_lookup_query`; `render_prompt`
  (placeholders filled); `build_cli_args` (flags, model, allow-url vs allow-all-urls);
  `parse_lookup_response` (sentinel extraction, last-JSON fallback, invalid→raise);
  `normalize_expected_parts`; `reconcile_parts` (present/missing/unexpected, null/numeric index);
  `completeness_tier` bands.
- Inference: confident match → complete / missing-required; low-confidence, no-match, disabled, and
  lookup-error → conservative with correct `lookup_status` and `needs_review`.
- End-to-end via `CliRunner` with a monkeypatched lookup: full run writes records + report +
  checkpoint; incremental reuse does not re-invoke the lookup for unchanged pieces.

## 15. Implementation Notes Log

- `RECORD_VERSION = "2.0"` (breaking change from the template-based 1.0).
- Module logger `logging.getLogger("script04.expected")`.
- Reuses `scripts/_common.py` atomic writers, `read_json`, `read_jsonl`, `utc_now_iso`,
  `sha256_text`.
- `config/instrumentation_templates.yaml` is removed; there are no hardcoded ensemble templates.
- The lookup is nondeterministic and network-dependent; accuracy is bounded by what the model finds
  online. Records surface evidence + confidence so downstream review can verify, and incremental
  mode caches results per fingerprint.
