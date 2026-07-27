# Vision Notation-Source Review: Gap Analysis & Implementation Plan (Scripts 02 & 05)

## 1. Objective

Add an optional, model-assisted **vision** pass that reads the rendered page image and reports
whether a part is **printed_original**, **handwritten**, or **mixed_or_uncertain**, plus a
human-**legibility** judgment. The raw signal is produced in **Script 02** (alongside the existing
OCR-LLM consolidation) and **adjudicated** in **Script 05** (quality checks), where it overrides
the deterministic `notation_source_type` and caps the `quality_band` when a document is handwritten
and/or poorly legible.

This plan is derived from:

- `scripts/02_extract_text_and_images.py` (OCR-LLM machinery to mirror; page/document schema)
- `scripts/05_quality_checks.py` (`classify_notation_source`, `build_quality_record`, fingerprint)
- `config/quality_thresholds.yaml`
- `README.md` (Pipeline Overview §2 and §5)
- Feasibility spike (see §3)

## 2. Gap Analysis

### 2.1 The problem deterministic signals cannot solve

Script 05's `classify_notation_source` relies on OCR confidence, alphanumeric ratio, and the
presence of an embedded text layer. Probing all 33 Mexican Hat Dance parts showed **no
deterministic signal** separates the genuinely handwritten `Eb Horn (written (badly))` part from
readable **printed** photocopies:

| Signal | Handwritten Eb Horn | Readable printed parts |
| --- | --- | --- |
| `osd_orientation_conf` | 0.91 / 0.55 | 0.37 (String Bass), 0.88 (Clarinet 2) |
| `osd_script` | Cyrillic (misread) | Cyrillic (misread) |
| `ocr_confidence` | 0.39 / 0.37 | 0.42 / 0.39 |
| `contrast_std` | 78 | comparable |

OCR confidence over music notation reflects **notation density**, not human legibility. Any
threshold catching the Eb Horn also misclassifies readable printed parts. As a result the Eb Horn
scored `quality 100.0` / `notation mixed_or_uncertain` — which is wrong; it is clearly handwritten.

### 2.2 What the current pipeline does with notation source

- **Script 02** renders a page-1 thumbnail per PDF (`thumbnail_path`, `thumbnail_hash`) and stores
  OCR/geometry/image metrics — but has **no** notation-source signal.
- **Script 05** derives `notation_source_type` deterministically and (post-fix) correctly refuses
  to guess handwriting from low OCR alone, defaulting ambiguous image scans to
  `mixed_or_uncertain`. It exposes an **unwired** `--use-vision` flag that only logs a warning, and
  `build_report` already renders a `Vision review: enabled/disabled` row.

### 2.3 Feasibility (resolved by spike — see §3)

The Copilot CLI **can** read a local image file when the absolute path is embedded in the prompt
and `--allow-all-tools` is passed. This makes the existing OCR-LLM subprocess pattern directly
reusable for vision — no new transport, auth, or SDK is required.

## 3. Feasibility Spike (completed)

Ran the Copilot CLI headlessly against two page-1 thumbnails from the same piece:

- Readable **printed** part → `{"notation_source":"printed_original","legibility":"good","confidence":0.97}`
- Handwritten **Eb Horn** part → `{"notation_source":"handwritten","legibility":"fair","confidence":0.85}`

Command shape (identical flags to `run_ocr_llm`):

```
copilot -p <prompt-with-absolute-image-path> --allow-all-tools --allow-all-urls \
  --no-color --no-ask-user -s --log-level none [--model <model>]
```

The model returns exactly one JSON object between sentinel lines. Vision **does** discriminate
handwritten from printed where deterministic metrics cannot. Feasibility: **confirmed**.

## 4. Design

### 4.1 Where each responsibility lives

- **Script 02 — raw signal (no verdict).** Mirror the OCR-LLM machinery: a `VisionConfig`
  dataclass, an external prompt file with a built-in fallback + prompt-version constant, sentinel
  parsing, a `subprocess.run` invoker, a disk cache keyed by **thumbnail hash** (not OCR text), and
  an async executor. Store the raw fields on the per-document record. Fire **only for scanned /
  image-based documents** (born-digital text PDFs are already, correctly, printed originals — no
  vision needed), and only when `--use-vision` is passed.
- **Script 05 — adjudication.** Read the vision fields from `documents.jsonl`, override
  `notation_source_type`/`confidence` when a confident vision verdict exists, and **cap the
  `quality_band`** for handwritten / low-legibility documents. All adjudication is driven by a new
  `vision` section in `config/quality_thresholds.yaml`.

### 4.2 Caching separation (cost discipline)

Vision is cached **separately** from OCR-LLM under `cache/vision/`, keyed by
`pdf_path | file_fingerprint | model | page_scope | VISION_PROMPT_VERSION | thumbnail_hash`. A
vision-prompt change (bump `VISION_PROMPT_VERSION`) never forces a re-OCR, and re-rendering an
unchanged page never re-invokes vision. Enabling `--use-vision` on an already-extracted library
runs **only** the vision calls (OCR/render results are reused).

## 5. Script 02 Changes (raw signal) — RECORD_VERSION `2.3` → `2.4`

New module-level constants/machinery (mirrors the OCR-LLM block):

- `_VISION_PROMPT_PATH = config/llm_prompts/classify_notation_source.txt`
- `VISION_RESULT_START = "<<<VISION_JSON>>>"`, `VISION_RESULT_END = "<<<END_VISION_JSON>>>"`
- `VISION_PROMPT_VERSION = "1"`
- `DEFAULT_VISION_PROMPT` (built-in fallback)
- `@dataclass(frozen=True) VisionConfig(enabled=False, command="copilot", model="", timeout_seconds=300.0, page_scope="first")`
- `_vision_template()` — file override else built-in
- `build_vision_prompt(template, item, image_abs_paths)` — fills `pdf_filename`, `piece_folder`, `image_paths`
- `parse_vision_response(stdout)` — sentinel parse (falls back to first/last brace)
- `run_vision_llm(prompt, cfg)` — `subprocess.run` with the spike's flag set; raises on non-zero
- `vision_cache_path(cache_dir, item, cfg, thumb_hash)` — under `cache/vision/`
- `_empty_vision_result(status)` — `{vision_status, vision_notation_source, vision_legibility, vision_confidence, vision_notes}`
- `classify_notation_vision(item, page_records, cache_dir, workspace_root, cfg, llm_fn=run_vision_llm)`
  — picks the page-1 thumbnail (or all when `page_scope="all"`), resolves absolute path(s), checks
  cache, invokes the LLM, validates enums, caches on success. Returns `None` when no thumbnail is
  available; never raises.
- `apply_vision_fields(doc_record, vision)` — patches the five `vision_*` fields.

Document record (`build_document_record`): add defaults
`vision_status="not_applied"`, `vision_notation_source=None`, `vision_legibility=None`,
`vision_confidence=None`, `vision_notes=""`.

`main()` new flags:

- `--use-vision/--no-vision` (default **on**)
- `--vision-command` (default `copilot`)
- `--vision-model` (default `""` = CLI default)
- `--vision-page-scope` (`first`|`all`, default `first`)
- `--vision-timeout` (default `300.0`)

Guards: disable vision (with a warning) when the CLI is not on PATH, or when rendering is disabled
(vision needs thumbnails). Executor: a dedicated `vision_executor` (bounded by `--ocr-workers`)
independent of OCR/OCR-LLM, so vision can run even when OCR is off. Submit
`classify_notation_vision` per **scanned** document (in both the fresh-processing and
incremental-reuse branches, so enabling vision later needs no re-OCR); drain and
`apply_vision_fields` after the loop.

New prompt file `config/llm_prompts/classify_notation_source.txt` (see §8 for placeholders).

## 6. Script 05 Changes (adjudication) — RECORD_VERSION `1.0` → `1.1`

- New `vision` section in `DEFAULT_THRESHOLDS` + `config/quality_thresholds.yaml`:
  - `enabled: true` — honor vision fields when present (independent of Script 02's flag)
  - `min_confidence: 0.50` — ignore vision verdicts below this
  - `override_notation_source: true`
  - `handwritten_caps_band_at: review` — a handwritten doc is never `good`
  - `poor_legibility_caps_band_at: poor`
  - `fair_legibility_caps_band_at: review`
- New issue codes: `ISSUE_HANDWRITTEN = "handwritten_notation"`, `ISSUE_LOW_LEGIBILITY = "low_legibility"`.
- `adjudicate_with_vision(record, doc_meta, thresholds)`: when a confident vision result exists,
  override `notation_source_type`/`confidence`, append evidence (`vision: handwritten (0.85)`), add
  the relevant issue code(s), cap the band (worse-of current vs cap), and set `needs_review`
  accordingly. Records the applied vision fields (`vision_notation_source`, `vision_legibility`,
  `vision_confidence`, `vision_notes`, `vision_applied`) on the quality record for transparency.
- `document_fingerprint(...)` gains a `doc_meta` argument and folds the vision fields in, so
  enabling vision (which rewrites `documents.jsonl`) invalidates the Script 05 cache.
- `main()` passes `use_vision` through; the existing warning is replaced by adjudication.

Band ordering for caps (worst → best): `POOR < REVIEW < GOOD` (`UNKNOWN` untouched). "Cap at X"
means the band may not be better than X.

## 7. Downstream Impact

- `data/documents.jsonl` gains five `vision_*` fields (RECORD_VERSION `2.4`). Consumers reading
  document rollups (Scripts 03/04/06/07/08) must treat them as optional/nullable.
- `data/quality_metrics.jsonl` gains `vision_applied` + echoed vision fields and may now report
  `handwritten`/lower bands for scanned manuscripts (RECORD_VERSION `1.1`). Scripts 06/07/08 that
  read `notation_source_type`, `quality_band`, or `needs_review` will see corrected values.
- README Pipeline Overview §2 and §5 and Recommended Repository Structure (new prompt file) updated.

## 8. Prompt Contract (`classify_notation_source.txt`)

Placeholders filled by `build_vision_prompt`: `{pdf_filename}`, `{piece_folder}`, `{image_paths}`.
The model must return exactly one JSON object between the sentinels:

```
<<<VISION_JSON>>>
{"notation_source":"printed_original|handwritten|mixed_or_uncertain","legibility":"good|fair|poor","confidence":0.0,"notes":""}
<<<END_VISION_JSON>>>
```

## 9. Test Plan

- **Script 02**: `classify_notation_vision` with a mock `llm_fn` → parses/validates enums, caches,
  and returns `None` without a thumbnail; `apply_vision_fields` patches the record; born-digital /
  no-thumbnail docs are skipped.
- **Script 05**: `adjudicate_with_vision` overrides notation source, caps band for
  handwritten/poor-legibility, ignores low-confidence and disabled-config cases; `document_fingerprint`
  changes when vision fields change.
- Run full suite + ruff; report raw output.

## 10. Rollout / Non-Goals

- Vision is **on by default** (`--use-vision`); pass `--no-vision` to skip it. The deterministic
  pipeline is unchanged when disabled.
- Non-goals: transcribing notation, per-note legibility, or web lookups. Vision reports a
  document-level source + legibility judgment only.
