# Concert Band Library Analysis Plan

## 1. Goal

Build a repeatable pipeline that scans every piece folder, analyzes all PDF parts, and produces:

- A per-piece report:
  - Which parts were detected
  - Which parts are likely missing
  - Whether a score is present
  - Scan quality warnings
  - Confidence levels and manual-review flags
- A collection-wide report:
  - Coverage statistics
  - Most common missing parts
  - Pieces with highest risk / lowest confidence
  - Work queue for manual follow-up

This plan assumes you currently have only directories + PDF files and no existing metadata.

## 2. Reality Check and Constraints

Your problem has two hard unknowns:

1. You do not always know what the expected instrumentation is for a given piece.
2. Filenames and scan quality may be inconsistent.

That means the system should not pretend to be perfectly deterministic. The right design is:

- Deterministic extraction where possible (file names, page counts, PDF metadata)
- Heuristic + model-based inference where needed (part classification, expected parts)
- Confidence scoring for every decision
- A clear manual-review queue

## 3. Recommended Architecture

Use a staged pipeline with persistent intermediate artifacts. Avoid one giant script.

## 3.1 Directory layout

Suggested structure:

```text
project-root/
  scripts/
    01_inventory.py
    02_extract_text_and_images.py
    03_part_classifier.py
    04_expected_parts_inference.py
    05_quality_checks.py
    06_piece_report.py
    07_collection_report.py
    08_manual_review_pack.py
  config/
    authority_sources.yaml
    score_lookup.yaml
    regex_rules.yaml
    quality_thresholds.yaml
    llm_prompts/
      classify_part.txt
      lookup_instrumentation.txt
      verify_low_confidence.txt
  data/
    raw_inventory.jsonl
    extracted_text.jsonl
    pages.jsonl
    documents.jsonl
    part_predictions.jsonl
    observed_parts_by_piece.jsonl
    expected_parts.jsonl
    expected_parts_report.md
    expected_instrumentation.md
    expected_instrumentation/
    quality_metrics.jsonl
    quality_report.md
    piece_reports/
    collection_reports/
  cache/
    ocr/
    llm/
    render/
  logs/
  README.md
```

## 3.2 Data flow

```text
Folders+PDFs
  -> inventory pass
  -> text/image extraction pass
  -> part classification pass
  -> expected-parts inference pass
  -> quality analysis pass
  -> per-piece report
  -> collection aggregation report
  -> manual-review package
```

## 4. Script-by-Script Plan

> Output format decision (as built): Scripts 01 and 02 emit newline-delimited JSON
> (`.jsonl`) for streaming writes, human-auditable diffs, and parity across passes. All
> downstream passes (03-08) should read and write `.jsonl` as well. Parquet migration is
> deferred and optional; treat the `.parquet` names in older drafts as `.jsonl`.

## 4.1 Script 01: Inventory Pass (`01_inventory.py`)

Status: implemented and tested (see `docs/BOOTSTRAP_AND_SCRIPT01_IMPLEMENTATION_PLAN.md`).

Purpose:

- Discover all piece folders
- Enumerate PDFs
- Capture basic file facts

Outputs:

- `data/raw_inventory.jsonl` (one record per PDF)
- `data/.inventory_checkpoint.json` (fingerprints for incremental mode)

Fields (as built):

- record_version, run_id
- piece_id (stable hash key)
- piece_folder, piece_folder_hash
- pdf_path (relative to library root), pdf_filename
- file_size_bytes, modified_timestamp
- file_fingerprint (used for incremental reuse)
- page_count
- pdf_readable, is_encrypted
- pdf_metadata (title, author, producer, etc. when present)
- health_flags (includes is_malformed)
- processing_status, error_message, processing_timestamp

Checks:

- unreadable PDFs
- encrypted PDFs
- zero-page or malformed documents

Downstream note: Script 02 consumes this file and filters to readable records
(`pdf_readable`, non-encrypted, `page_count >= 1`, not `is_malformed`).

## 4.2 Script 02: Extraction Pass (`02_extract_text_and_images.py`)

Status: implemented and tested (see `docs/SCRIPT02_EXTRACTION_IMPLEMENTATION_PLAN.md`).
Record schema version `2.0`.

Purpose:

- Pull embedded text per page and derive text-quality signals
- Capture prominent/header text candidates for part/title detection
- Render low-res thumbnails for visual QA and LLM prompts
- Compute page geometry, born-digital vs scanned signals, and image-quality metrics
- OCR scanned/no-text pages with Tesseract (OSD auto-rotate first) and record recovered text
- Consolidate each PDF's OCR text into a normalized instrument list via one Copilot CLI call per file
- Roll up per-page results into a per-document summary

OCR is implemented via `pytesseract` + the Tesseract system binary and runs by default on pages
flagged `needs_ocr` or `is_image_based`. It degrades gracefully (auto-disabled with a warning)
when the toolchain is unavailable; those pages keep `needs_ocr = true` and null OCR fields.

Multi-pass OCR (`--ocr-multipass`, default on) runs several passes at varied PSM + DPI, keeps
every non-empty pass in `ocr_candidates`, and selects the highest-scoring text as `ocr_text`
(`--single-pass-ocr` runs a single pass). A per-file OCR-LLM step (`--ocr-llm`, default on)
sends the consolidated OCR candidates to the Copilot CLI once per PDF and records a normalized
instrument list on the document record (`ocr_llm_status`, `ocr_llm_instruments`) for Script 03.

Processing is pipelined for throughput. OCR Tesseract passes run across a thread pool while page
rendering stays on the main thread (PyMuPDF is not thread-safe); once a file is OCR'd its Copilot
classification is spun off asynchronously so the next PDF begins OCR while prior LLM calls finish,
and the async results are collected at the end of the run. `--ocr-workers` sets the worker count
(`0` = auto `min(8, CPU count)`, `1` = serial) and also bounds concurrent OCR-LLM calls.

Libraries (as built):

- `pymupdf` for PDF structure, text, and page rendering
- `numpy` + `opencv-python(-headless)` for image-quality metrics (optional; degrade to `null`)
- `pytesseract` + `Pillow` + the Tesseract binary for OCR/OSD (optional; degrade to `null`)

Outputs (three JSONL datasets + checkpoint):

- `data/extracted_text.jsonl` (one record per page: text + text-quality signals)
- `data/pages.jsonl` (one record per page: render + geometry + image-quality metrics)
- `data/documents.jsonl` (one record per PDF: rollup for identity/quality/reporting stages)
- `data/.extraction_checkpoint.json`

Key `extracted_text.jsonl` fields for downstream use:

- embedded_text, embedded_text_length, extraction_method
- text_is_searchable, needs_ocr, ocr_text, ocr_confidence, ocr_word_count
- ocr_applied, ocr_status, ocr_engine
- ocr_candidates (per-pass `{dpi, psm, text, confidence, word_count}` from multi-pass OCR)
- text_source (`embedded`/`ocr`/`none`) — downstream should use embedded_text when
  `text_source == embedded`, else fall back to ocr_text
- word_count, alnum_ratio, page_text_hash (normalized-text dedupe key)
- header_text_candidates (largest-font strings), top_lines (top-of-page reading order)

Key `pages.jsonl` fields for downstream use:

- thumbnail_path, thumbnail_hash, render_dpi, render_width_px, render_height_px
- text_density, black_white_ratio
- page_width_pt, page_height_pt, rotation, orientation, aspect_ratio
- image_count, largest_image_coverage, is_image_based, estimated_dpi
- contrast_std, blur_variance, skew_angle_deg (null when numpy/opencv absent or disabled)
- osd_rotation, osd_orientation_conf, osd_script (OSD orientation; null when OCR disabled)

Key `documents.jsonl` fields for downstream use:

- piece_id, piece_folder, pdf_filename, page_count
- pages_with_text, pages_needing_ocr, ocr_fraction
- pages_ocr_applied, pages_ocr_recovered, ocr_char_count, pages_rotated
- ocr_llm_status, ocr_llm_instruments (per-file OCR-LLM instrument consolidation for Script 03)
- total_text_length, total_word_count
- pages_image_based, image_based_fraction
- first_page_text, first_page_header_candidates
- processing_status (`success` / `partial_error`)

## 4.3 Script 03: Part Classification (`03_part_classifier.py`)

Status: implemented (`record_version 1.0`). See
`docs/SCRIPT03_PART_CLASSIFICATION_IMPLEMENTATION_PLAN.md` for the full design.

Purpose:

Identify what each PDF likely is:

- Part name (Trumpet 1, Clarinet 2, Flute, Timpani, etc.)
- Whether document is likely score, condensed score, or single part

Method (rule-first, deterministic; LLM fallback deferred):

Inputs (from Scripts 01/02):

- `data/raw_inventory.jsonl` for `pdf_filename`, `piece_folder`, `piece_id`, `file_fingerprint`,
  and `pdf_readable`
- `data/documents.jsonl` for `first_page_text` and `first_page_header_candidates`
- `data/pages.jsonl` for page-1 `zone_top_left`/`zone_top_center`/`zone_top_right`
- Script 02 datasets are optional; when absent the classifier runs filename-only

Rules and vocabulary live in `config/regex_rules.yaml` (instrument lexicon, clef/transposition
markers, score keywords), with an identical built-in fallback baked into the script so it runs
even without the file or PyYAML.

1. Filename-first (primary):
- Isolate the part segment by stripping the known `piece_folder` prefix (handles dashes inside a
  part such as `Basses - Tuba` and both `-`/`_` separators).
- Longest-alias-wins instrument match with word-boundary regex (so `Bass Trombone`, `Bass
  Clarinet`, `Alto Saxophone` are not shadowed, and short aliases like `cl`/`fl` never match
  inside words).
- Extract clef (`(BC)`/`(TC)`/`(Bass)`), transposition (`in F/Bb/Eb`), part index, and score type.

2. Text confirmation/recovery (secondary):
- Confirm the filename instrument against page-1 text (boosts confidence to `combined`); recover a
  label from page text when the filename is ambiguous (`text` evidence, lower confidence).

3. Ensemble harmonization:
- Within a piece, documents sharing an `(instrument, part_index, clef)` signature are flagged
  `duplicate_in_piece` (a signal for Scripts 04/05, not an automatic edit).

4. Model fallback (deferred):
- A disabled-by-default `--use-llm` hook exists but no provider is wired in this offline repo;
  enabling it logs a warning and changes nothing (no fabricated predictions).

Outputs:

- `data/part_predictions.jsonl` (one record per readable PDF)
- `data/observed_parts_by_piece.jsonl` (one record per `piece_id`: observed-parts rollup for
  Scripts 04/06/07 — added in schema 1.1)
- `data/part_classification_report.md` (Markdown summary)
- `data/.part_classifier_checkpoint.json` (checkpoint; supports `--mode incremental`)

Key per-document fields (`part_predictions.jsonl`):

- predicted_part (human label, e.g. `Horn in F 2`, `Baritone (BC)`, `Full Score`; nullable)
- canonical_instrument (snake_case key, e.g. `baritone_horn`, `tuba`; nullable)
- family (woodwind/brass/percussion/strings/score/unknown)
- section (coarser lexicon-driven grouping, e.g. `trumpets`, `low_brass`, `tubas`;
  `score`/`unknown` fallbacks) — schema 1.1
- catalog_number, piece_title_guess (work-identity seeds parsed from the folder) — schema 1.1
- part_index (e.g., 1,2,3; nullable)
- clef (`bass`/`treble`; nullable), transposition (`F`/`Bb`/`Eb`; nullable)
- is_score, score_type (`full`/`condensed`/`short`/`conductor`; nullable)
- confidence (0..1), confidence_tier (`high`/`medium`/`low`/`none`) — schema 1.1
- evidence_source (`filename`/`text`/`combined`/`none`/`llm`)
- needs_review (bool; true when unmatched, below threshold, or a duplicate) — schema 1.1
- part_sort_key (string; conventional score order) — schema 1.1
- alternates (top-2 other instrument candidates), match_details, duplicate_in_piece

Key per-piece fields (`observed_parts_by_piece.jsonl`, schema 1.1):

- piece_id, piece_folder, catalog_number, piece_title_guess
- document_count, classified_count, has_score, score_types
- distinct_instruments, families, sections
- needs_review_count, unmatched_count, low_confidence_count, duplicate_count
- observed_parts (list, sorted by part_sort_key): each carries canonical_instrument, part_index,
  clef, section, predicted_part, count, min/max_confidence, needs_review, duplicate


## 4.4 Script 04: Expected Parts Inference (`04_expected_parts_inference.py`)

Purpose:

Estimate which parts should exist for each piece so missing parts can be flagged.

Inputs:

- `data/observed_parts_by_piece.jsonl` (Script 03 rollup, schema 1.1) — **preferred** observed-parts
  source: already grouped by `piece_id` with the `(canonical_instrument, part_index, clef)` key,
  `has_score`, per-part `count`/`needs_review`/`duplicate`, and `catalog_number` /
  `piece_title_guess` identity seeds. Use this instead of re-grouping the per-document file.
- `data/documents.jsonl` for additional work-identity seeds (`first_page_text`,
  `first_page_header_candidates`, `identity_candidates`)
- `data/part_predictions.jsonl` (Script 03) — used to **locate a piece's local score** document(s):
  records where `is_score` is true supply `pdf_path`, `score_type`, `file_fingerprint`, and
  `piece_folder`. The best score per piece is chosen by `score_type` preference
  (`full` > `conductor` > `condensed` > `short`).
- `data/extracted_text.jsonl` (Script 02) — the per-page text (`embedded_text` / `ocr_text` chosen
  by `text_source`, plus `header_text_candidates`) for the local score PDFs; the leading
  `max_score_pages` pages are the Stage A text source, reused instead of re-OCR when adequate.
- `--library-root` (optional) — filesystem root used to resolve a score `pdf_path` when Stage A must
  re-render and re-OCR pages (only when the reused Script 02 text is thinner than
  `min_score_text_chars`).

Approach:

Status: implemented (Script 04, schema `2.1`) as a **three-stage inference engine**. Rather than
assuming an ensemble template, it establishes each piece's *actual published edition* instrumentation
from the most authoritative source available, degrading conservatively when nothing usable is found.
The ensemble type is inferred from whatever score is found. All AI/OCR touchpoints are injectable so
tests run fully offline. See
`docs/SCRIPT04_EXPECTED_PARTS_INFERENCE_IMPLEMENTATION_PLAN.md` for the full design.

The stages run in order; the first stage to produce a confident instrumentation contract
(match found, non-empty `expected_parts`, and `identity_match_confidence >= confidence_threshold`)
wins, otherwise the piece degrades to the conservative fallback:

**Stage A — local score OCR (`detection_method: local_score_ocr`):**
- Find the piece's best local score from `part_predictions.jsonl` (score-type preference above).
- Gather its leading-page text from `extracted_text.jsonl` (reusing Script 02 embedded/OCR text).
  If that text is thinner than `min_score_text_chars`, re-render and re-OCR the first
  `max_score_pages` at `reocr_dpi` via PyMuPDF + Tesseract (resolving the PDF under `--library-root`;
  skipped gracefully when the toolchain or file is unavailable).
- Send the score text to the Copilot CLI with the **summarize** prompt template
  (`config/llm_prompts/summarize_score_instrumentation.txt`), which cleans/normalizes the text into
  the same instrumentation contract JSON (no web search). A confident contract ends inference here.

**Stage B — online authority lookup (`detection_method: authority_lookup`):**
- Build a work-identity query from observed instruments, `catalog_number`, `piece_title_guess`, and
  Script 02 `identity_candidates`, then render `config/llm_prompts/lookup_instrumentation.txt`.
- Invoke the Copilot CLI headlessly (`copilot -p <prompt> --allow-all-tools -s --no-ask-user ...`).
  The model searches authoritative web sources and returns one JSON object between the
  `<<<SCORE_JSON>>>` / `<<<END_SCORE_JSON>>>` sentinels, providing `ensemble_type`,
  `ensemble_display_name`, `work_identity`, `evidence_sources`, `expected_parts[...]` (only from
  authoritative text sources), and — when it cannot read instrumentation from text — a list of
  `candidate_score_images[{url, kind, source_url, notes}]`. The model does **not** OCR images itself.

**Stage C — remote image OCR (`detection_method: score_image_ocr`):**
- Runs only when Stage B matched an edition confidently but returned no `expected_parts` alongside
  one or more `candidate_score_images`.
- Each image URL is downloaded into a per-piece temp folder (no domain/size caps) and OCR'd with
  Tesseract; the concatenated text is fed to the same **summarize** prompt as Stage A. A confident
  contract ends inference here.

For every winning stage, the looked-up part slots are reconciled against the piece's observed parts.
Matching is count-based per canonical instrument (robust to null `part_index`), preferring explicit
index matches, then consuming slots in order — producing `present` / `missing` / `unexpected` sets,
a required-part completeness score + tier, and `needs_review` when required parts are missing, the
score is missing, unexpected parts appear, or the Script 03 rollup already flagged the piece.

Conservative fallback (per-piece degradation):
- `--no-lookup` (or `enabled: false` in `config/score_lookup.yaml`) with no local-score result →
  `lookup_status: disabled`.
- The Copilot CLI is not on `PATH`, times out, or exits non-zero → `lookup_status: error`.
- The model reports `match_found: false` → `lookup_status: no_match`.
- Match confidence below `confidence_threshold` → `lookup_status: low_confidence`.
- Matched an edition but no stage produced usable parts (no text parts, and image OCR yielded
  nothing) → `lookup_status: no_match`.
- In every fallback case the record declares no expected/missing parts, sets
  `completeness_score: null` and `completeness_tier: unknown`, and is flagged `needs_review: true`.
  Any evidence, notes, and resolved identity returned before the fallback are retained.

Stage toggles: `--local-score/--no-local-score` (Stage A), `--lookup/--no-lookup` (Stage B), and
`--image-ocr/--no-image-ocr` (Stage C); each also respects its `*_enabled` flag in
`config/score_lookup.yaml`. Report toggle: `--split-instrumentation/--no-split-instrumentation`
(default on) controls whether the instrumentation report is split into per-piece files with a
linking index (see §4.10).

Note: lookup accuracy is bounded by what the model can find online (Stage B) and by scan/OCR quality
(Stages A/C), and Stages B/C are network- and AI-credit-dependent and nondeterministic. `--mode
incremental` caches per-piece results by fingerprint (observed parts + `has_score` + best local
score identity (`pdf_path` + `file_fingerprint`) + model + confidence threshold + stage flags +
`max_score_pages`/`min_score_text_chars`/`reocr_dpi` + lookup- and summarize-template hashes) to
avoid re-spending credits or re-OCR on unchanged pieces.


Outputs:

- `data/expected_parts.jsonl` (one record per piece; rewritten atomically as each piece completes,
  so an interrupted run keeps the pieces already finished)
- `data/expected_parts_report.md` (Markdown summary; written at end of run)
- `data/expected_instrumentation.md` (per-piece expected instrumentation). By default this is an
  **index** that links to one Markdown file per piece under `data/expected_instrumentation/`, each
  written as its piece finishes; `--no-split-instrumentation` restores a single monolithic file.
- `data/expected_instrumentation/` (per-piece instrumentation Markdown when splitting is enabled)
- `data/.expected_parts_checkpoint.json` (checkpoint; rewritten as each piece completes; supports
  `--mode incremental` resume after an interrupted run)

Fields (`expected_parts.jsonl`, schema 2.1):

- `ensemble_type`, `ensemble_display_name` (inferred per piece from the found score)
- `detection_method` (`local_score_ocr` | `authority_lookup` | `score_image_ocr` |
  `conservative_fallback` | `error`)
- `inference_method` (`local_score_ocr` | `authority_lookup` | `score_image_ocr` |
  `fallback_conservative`)
- `ocr_source` (`local_score` | `score_image` | `null`) and `local_score_path` (the score PDF used
  for Stage A, or `null`)
- `lookup_status` (`matched` | `low_confidence` | `no_match` | `disabled` | `error`)
- `lookup_model`, `lookup_notes`, `identity_match_confidence`
- `authority_coverage` (`full` when evidence sources were returned, else `none`),
  `evidence_sources` (list of `{url, title, snippet, retrieved}`)
- `candidate_score_images` (list of `{url, kind, source_url, notes}` the Stage B lookup returned)
- `expected_parts` (list of `{canonical_instrument, part_index, label, section, required, present}`)
- `missing_parts`, `missing_required_parts`, `missing_optional_parts`
- `unexpected_parts` (observed parts with no expected slot)
- `has_score`, `score_expected`, `score_missing`
- `completeness_score` (0..1, or `null` on fallback), `completeness_tier`
  (`complete`/`near_complete`/`incomplete`/`severely_incomplete`/`unknown`)
- `needs_review`, plus counts: `observed_instrument_count`, `expected_part_count`,
  `missing_required_count`, `unexpected_part_count`
- `work_identity` (`title_guess`, `catalog_number`, `identity_candidates` from Script 02, plus the
  `resolved` edition identity returned by the lookup)
- `catalog_number`, `piece_title_guess`, `piece_id`, `piece_folder`

Deferred fields (populated once the authority path is wired): `source_match_diagnostics`
(candidate/accepted counts, rejected candidates, identity_match_score), richer
`instrumentation_structure` (optional/alternate substitutions, doubles/cues, transposition
requirements), and normalized multi-field `work_identity` (title_variants, composer, publisher,
edition, series, publication_year).

## 4.5 Script 05: Quality Checks (`05_quality_checks.py`) — implemented (schema 1.0)

Purpose:

Flag poor scan quality / likely-unusable pages and classify each document's notation source
(printed/engraved vs handwritten). The engine is deterministic and threshold-driven: it **reuses**
the objective per-page metrics Script 02 already computed rather than re-rendering pages, and it
never flags an issue from a missing (null) metric.

Inputs:

- `data/pages.jsonl` — core objective metrics from Script 02: `blur_variance` (Laplacian
  variance), `skew_angle_deg`, `contrast_std`, `text_density`, `estimated_dpi`,
  `render_width_px` / `render_height_px`, `is_image_based`, `is_blank`, `thumbnail_hash`.
- `data/extracted_text.jsonl` — legibility signals: `ocr_confidence`, `ocr_word_count`,
  `alnum_ratio`, `word_count`, `text_is_searchable`, `page_text_hash`.
- `data/documents.jsonl` — optional per-document rollups supplying `piece_folder` / `pdf_filename`.

Thresholds live in `config/quality_thresholds.yaml` (`quality_checks` + `notation_source`
sections); built-in defaults are used when the file or PyYAML is absent, and YAML values are merged
one level deep over those defaults.

Per-page issue codes (each derived from a single Script 02 metric; a null metric is never flagged):

- `low_resolution` — `estimated_dpi` below `min_estimated_dpi`, else render dimensions below
  `min_render_width_px` / `min_render_height_px`.
- `excessive_skew` — `abs(skew_angle_deg)` above `max_skew_angle_deg`.
- `low_contrast` — `contrast_std` below `min_contrast_std`.
- `heavy_blur` — `blur_variance` below `min_blur_variance`.
- `blank_page` — `is_blank` true, or very low `text_density` with zero words.
- `noise_page` — high `text_density` with almost no recognized words.
- `ocr_illegible` — only when the page was expected to carry text: low `ocr_confidence` where
  OCR actually found words (`ocr_word_count > 0`), or low `alnum_ratio` when `word_count > 0`
  (interim proxy when OCR is disabled). Pure-notation pages (no text) are not penalized, and blank
  pages suppress this check.

`Cropping margin loss` from earlier drafts is **deferred**: Script 02 exposes no margin/crop metric,
so it is intentionally not implemented rather than fabricated.

Scoring:

- Each issue penalizes `quality_score` by its configured weight scaled by the fraction of analyzed
  pages it affects (`score = max(0, 100 - Σ weightᵢ × affected_fractionᵢ)`), so a defect on every
  page hurts more than a one-page defect.
- `quality_band` maps the score via `bands` (`good` ≥ `good_min_score`, `review` ≥
  `review_min_score`, else `poor`). Documents with **no scoreable pages** (all pages errored) get
  `quality_band = unknown`, `quality_score = null`, and `needs_review = true`.

Notation-source classification (`classify_notation_source`):

- Strongest signal: a real embedded/searchable text layer (`searchable_fraction ≥
  searchable_fraction_printed`) ⇒ `printed_original`.
- Otherwise OCR confidence + alphanumeric ratio discriminate engraved print
  (`printed_min_ocr_confidence` / `printed_min_alnum_ratio`) from handwriting
  (`handwritten_max_ocr_confidence` / `handwritten_max_alnum_ratio`); ambiguous ⇒
  `mixed_or_uncertain`.
- `notation_source_evidence` records the feature values used (e.g. `searchable_fraction=0.80`,
  `mean_ocr_confidence=88.2`); `notation_source_confidence` is 0..1 (0.0 only when there is no
  usable text signal).

Optional model-assisted checks (`--use-vision`): a hook is reserved for a future vision pass but is
**not wired to a provider**; requesting it logs a warning and falls back to the heuristic checks
(mirrors Script 03's `--use-llm`).

Shared infrastructure (reuse from `scripts._common`; see §4.9):

- `setup_logging`; `RECORD_VERSION = "1.0"` + `CHECKPOINT_FILENAME = ".quality_checks_checkpoint.json"`
  with `make_checkpoint_path` / `load_checkpoint` / `build_checkpoint` for `--mode incremental`.
  Incremental reuse keys on a per-document fingerprint (config fingerprint + per-page
  `page_text_hash` / `thumbnail_hash` / `processing_status`).
- Build each record from `new_record_envelope(run_id, RECORD_VERSION)`; set `ProcessingStatus.ERROR`
  on unexpected per-document failures.
- Documents are scored through `run_with_progress`, so `-j/--concurrency` works like Script 04
  (scoring is CPU-light; concurrency mainly benefits the future vision pass).

Outputs:

- `data/quality_metrics.jsonl` — one record per document.
- `data/quality_report.md` — Markdown summary (config table, quality-band distribution,
  notation-source distribution, collection-wide issue counts, and a per-document detail table
  ordered worst-score-first, limited by `--report-detail-limit`).

Per-document record (schema 1.0) fields: envelope (`record_version`, `run_id`, `processing_status`,
`processing_timestamp`) plus `pdf_path`, `piece_id`, `piece_folder`, `pdf_filename`, `page_count`,
`analyzed_page_count`, `quality_score` (0-100 float, or null), `quality_band`
(`good` / `review` / `poor` / `unknown`), `needs_review`, `page_issue_count`,
`issue_summary` (code → affected-page count), `top_issues`, `worst_page`,
`page_findings` (`[{page_num, issues}]`), `notation_source_type`
(`printed_original` / `handwritten` / `mixed_or_uncertain`), `notation_source_confidence` (0-1),
`notation_source_evidence`, and a `metrics` block (`median_estimated_dpi`, `mean_contrast_std`,
`mean_blur_variance`, `max_abs_skew_deg`, `mean_alnum_ratio`, `mean_ocr_confidence`,
`image_based_fraction`, `blank_page_count`).

> Thresholds in `config/quality_thresholds.yaml` are initial heuristics and are **unverified against
> real scan data**; they should be calibrated once a representative sample has been run.

## 4.6 Script 06: Piece Report Generator (`06_piece_report.py`)

Purpose:

Create one report per piece folder in Markdown or JSON.

Inputs (join on `piece_id`):

- `data/documents.jsonl`, `data/part_predictions.jsonl`, `data/observed_parts_by_piece.jsonl`,
  `data/expected_parts.jsonl`, `data/quality_metrics.jsonl`, and `data/pages.jsonl` (for thumbnail
  references). List detected parts in conventional score order using Script 03's `part_sort_key`,
  and surface `needs_review` / `confidence_tier` in the confidence summary rather than re-deriving
  them from the raw `confidence` float.

Report sections:

- Piece identity
- Detected documents and predicted parts
- Expected parts and missing parts
- Score presence/absence
- Quality findings
- Confidence summary
- Manual actions recommended

Shared infrastructure (reuse from `scripts._common`; see §4.9):

- Iterate/sort pieces with `piece_sort_key`; escape Markdown table cells with `md_cell` and compute
  rates with `pct`.
- When reading `expected_parts.jsonl`, branch on `LookupStatus.*` and `CompletenessTier.*` constants
  (never bare strings), and present completeness using `COMPLETENESS_TIER_ORDER`.
- Render many piece reports concurrently via `run_with_progress`.

Outputs:

- `data/piece_reports/<piece_id>.md`
- `data/piece_reports/<piece_id>.json`

## 4.7 Script 07: Collection Report (`07_collection_report.py`)

Purpose:

Aggregate all piece reports.

Metrics:

- total pieces processed
- pieces with complete sets
- pieces with missing critical parts
- pieces with no score (from Script 03 `has_score` in the per-piece rollup)
- distribution of quality bands
- top missing instruments overall (aggregate by Script 03 `section` and `canonical_instrument`)
- confidence distribution (aggregate Script 03 `confidence_tier` / `needs_review_count`)

Shared infrastructure (reuse from `scripts._common`; see §4.9):

- Aggregate Script 04 results by iterating `LOOKUP_STATUS_ORDER` / `COMPLETENESS_TIER_ORDER` so
  every table shares one canonical ordering; compare against `LookupStatus.*` / `CompletenessTier.*`
  constants rather than literals.
- Use `pct` for coverage percentages and `md_cell` for all Markdown table cells; order any
  piece-level rows with `piece_sort_key`.

Outputs:

- `data/collection_reports/summary.md`
- `data/collection_reports/summary.json`
- `data/collection_reports/manual_review_queue.csv`

## 4.8 Script 08: Manual Review Pack (`08_manual_review_pack.py`)

Purpose:

Generate a prioritized queue so your manual effort targets the highest-value fixes.

Queue priority formula (example):

`priority = missing_critical_weight + quality_penalty + low_confidence_penalty`

The `low_confidence_penalty` is driven directly by Script 03's `needs_review` flag and
`confidence_tier` (and the per-piece `needs_review_count` / `low_confidence_count` /
`duplicate_count` rollup counts), so no re-thresholding of the raw confidence float is required.

Include:

- piece folder
- likely missing parts
- sample page thumbnails
- reason codes
- recommended next action

Shared infrastructure (reuse from `scripts._common`; see §4.9):

- Drive priority off `CompletenessTier.*` / `LookupStatus.*` and Script 03's `needs_review`
  rollup counts (compare against the shared constants, not strings).
- Order the queue with `piece_sort_key` as a stable tie-breaker; escape any Markdown/HTML table
  cells with `md_cell`.

Output:

- CSV and optional lightweight HTML dashboard

## 4.9 Shared Pipeline Infrastructure (`scripts/_common.py`) and Gap Analysis

Auditing Script 04 alongside Scripts 01-03 surfaced infrastructure that had been copy-pasted into
every script. Because Scripts 05-08 are not written yet, that duplication is now the single largest
risk to the pipeline staying consistent: each new script would otherwise re-implement logging,
checkpointing, percentage/Markdown helpers, concurrency, and — most dangerously — re-type the
status/tier string literals that consumers filter on. The audit therefore extracted six pieces of
shared infrastructure into `scripts/_common.py`. Scripts 01-04 were refactored onto them (all 67
tests still pass), and Scripts 05-08 are expected to build on them from day one.

### Gap analysis (what was missing, now filled)

| # | Gap before | Shared API in `scripts/_common.py` | Why it matters for 05-08 |
|---|------------|------------------------------------|--------------------------|
| 1 | Each script re-declared `setup_logging`, an ad-hoc checkpoint-path builder, and private `_pct` / `_md_cell` report helpers | `LOG_FORMAT`, `setup_logging(log_level)`, `make_checkpoint_path(output, filename)`, `pct(part, whole)`, `md_cell(value)` | New scripts get identical log lines, checkpoint placement, and safe Markdown tables for free |
| 2 | Each script hand-rolled checkpoint load (with its own version-mismatch check) and checkpoint assembly | `load_checkpoint(path, record_version, logger=None)` (returns `{}` on missing/stale), `build_checkpoint(record_version, run_id, fingerprints, **extra)` | `--mode incremental` behaves identically everywhere; a schema bump auto-invalidates stale checkpoints |
| 3 | Each record's common header (`record_version`, `run_id`, `processing_status`, `processing_timestamp`) was inlined per script | `new_record_envelope(run_id, record_version)` | Every downstream record carries the same envelope; consumers can rely on it existing |
| 4 | Only Script 04 had concurrency, wired directly to `ThreadPoolExecutor` | `run_with_progress(items, worker, max_workers=1)` (order-preserving; sequential for 1 worker/item) | Any slow stage (05 vision checks, 06 report rendering) gets a `-j/--concurrency` option with one call |
| 5 | Status/tier strings (`"matched"`, `"complete"`, `"success"`, …) were bare literals scattered across scripts | `ProcessingStatus`, `LookupStatus`, `CompletenessTier` classes + `LOOKUP_STATUS_ORDER` / `COMPLETENESS_TIER_ORDER` tuples | A rename can no longer silently break a consumer's filter; report tables share one canonical ordering |
| 6 | Piece ordering was a private `_sort_key` in Script 04 only | `piece_sort_key(record)` → `(catalog_number or "~", piece_folder or "")` | Piece-level output/reports sort identically across 04, 06, 07, 08 |

### Shared API surface (import from `scripts._common`)

- Serialization / IO (pre-existing): `read_jsonl`, `read_json`, `atomic_write_jsonl`,
  `atomic_write_json`, `atomic_write_text`, `utc_now_iso`, `sha256_text`, `file_fingerprint`,
  `normalize_rel_path`.
- Scaffolding (this audit): `setup_logging`, `make_checkpoint_path`, `load_checkpoint`,
  `build_checkpoint`, `new_record_envelope`, `run_with_progress`.
- Report helpers (this audit): `pct`, `md_cell`, `piece_sort_key`.
- Vocabulary constants (this audit): `ProcessingStatus`, `LookupStatus`, `CompletenessTier`,
  `LOOKUP_STATUS_ORDER`, `COMPLETENESS_TIER_ORDER`.

### Conventions every new script (05-08) should follow

1. Define a module-level `RECORD_VERSION` and a `CHECKPOINT_FILENAME` dotfile constant (e.g.
   `".quality_checks_checkpoint.json"`) so `make_checkpoint_path` keeps checkpoints beside outputs.
2. Start `main()` with `setup_logging(log_level)`; log per-item progress inside the `worker`
   passed to `run_with_progress`, not in the caller.
3. Build every output record from `new_record_envelope(run_id, RECORD_VERSION)` and add domain
   fields on top; set `processing_status` from `ProcessingStatus` on error paths.
4. For incremental mode, load with `load_checkpoint(path, RECORD_VERSION, logger)` and persist with
   `build_checkpoint(RECORD_VERSION, run_id, fingerprints, **stage_specific_fields)` via
   `atomic_write_json`.
5. When reading Script 04 output, filter on `LookupStatus.*` / `CompletenessTier.*` constants — never
   bare strings — and iterate report groups using the `*_ORDER` tuples.
6. Sort piece-level records/reports with `piece_sort_key`; escape Markdown cells with `md_cell` and
   compute rates with `pct`.

> Note: Script 02's per-page/per-document record builders were intentionally left on their inline
> envelope fields (rather than retrofitted to `new_record_envelope`) to avoid churning a stable,
> already-shipped schema. New scripts have no such constraint and should use the envelope helper.

## 4.10 Incremental streaming output and per-piece report split (Script 04)

A follow-up audit of Script 04's write path (aimed at large libraries, e.g. 700 pieces) surfaced a
durability gap and a report-scaling problem. Both are now addressed; the design below is the
convention Scripts 05-08 should follow whenever a run is long, expensive, or interruptible.

### Audit findings

| # | Finding (before) | Impact | Fix |
|---|------------------|--------|-----|
| A | **Nothing was persisted until the run finished.** `expected_parts.jsonl`, both Markdown reports, and `.expected_parts_checkpoint.json` were all written only after the full processing loop. | Stopping mid-run (e.g. 400/700) lost every structured record **and** the checkpoint. `--mode incremental` could not resume because `previous_records` is read from the end-only JSONL, so a re-run recomputed everything and re-spent AI credits. Only raw lookup JSON (`--save-lookups`) was written incrementally, and it is not structured enough to rebuild reports or drive resume. | Records, the checkpoint, and (when split) per-piece Markdown are now persisted **as each piece completes**, so an interrupted run resumes and never recomputes finished pieces. |
| B | **`expected_instrumentation.md` was one monolithic file** rendered only at the end. | At ~1-3 KB/piece it grows to 1-2 MB for 700 pieces: unwieldy to navigate, noisy diffs, and unavailable until completion. | The report is split into one file per piece under `data/expected_instrumentation/`, written as each piece finishes; `expected_instrumentation.md` becomes a lightweight index that links to them. |
| C | **`run_with_progress` returned only after all items completed** (`ThreadPoolExecutor.map`), giving no per-completion hook. | There was no safe place to persist incrementally under concurrency. | `run_with_progress` gained an optional `on_result(item, result)` callback invoked in the **caller's thread** as each item finishes (order-preserving return is unchanged), so incremental writes need no locking. |
| D | **Checkpoint fingerprints were computed for all pieces upfront** but written once at the end. | A partial checkpoint could otherwise claim unprocessed pieces were done. | Incremental checkpoint writes include only fingerprints for pieces actually persisted so far; the existing dual guard (record present in output **and** fingerprint match) keeps partial checkpoints correct. |

### Design (as built)

- **`scripts/_common.py` — `run_with_progress(items, worker, max_workers=1, on_result=None)`:**
  when `on_result` is supplied it is called once per completed item, in the caller's thread, for
  both the sequential and parallel paths. Return order still matches input order. This is the shared
  hook any streaming stage (05-08) should use for incremental persistence.
- **Incremental persistence (Script 04):** as each piece completes, the run (a) appends the record
  to the in-memory set and atomically rewrites `expected_parts.jsonl` (sorted via `piece_sort_key`),
  (b) records the piece's fingerprint and atomically rewrites the checkpoint, and (c) when splitting,
  atomically writes that piece's per-piece Markdown file. All writes go through the existing
  `atomic_write_*` helpers (temp-file + `os.replace`), so a crash never corrupts an existing file.
- **Per-piece report split:** controlled by `--split-instrumentation/--no-split-instrumentation`
  (default **on**). Split files live in a subfolder named after the instrumentation report's stem —
  `data/expected_instrumentation/` for the default `data/expected_instrumentation.md`. Each file is
  named `"{catalog}_{title}_{piece_id}.md"` (slugged) so it is deterministic across runs and sorts
  by catalog. `expected_instrumentation.md` is then an index: run metadata plus a table
  (Catalog / Piece / Ensemble / Lookup / Completeness / Missing required / Details) whose Details
  column links to each per-piece file. `--no-split-instrumentation` restores the single-file report.
- **Orphan cleanup:** at the end of a run the split folder is reconciled — Markdown files that no
  longer correspond to a current piece are removed. Only `*.md` files inside the managed subfolder
  are touched.

### Extension to Scripts 01-02 (as built)

The same durability gap (finding A) existed in Scripts 01 and 03 (write output + checkpoint only
after the full loop) and in Script 02, the most expensive stage (render + OCR + image metrics per
PDF). Incremental persistence has been extended to **Scripts 01 and 02** (the ones whose runs are
long enough for an interruption to be costly). Script 03 was intentionally left as end-only: its
per-record work is trivial regex classification, and `observed_parts_by_piece.jsonl` plus
`apply_ensemble` are cross-record aggregates that can only be finalized at the end.

- **Script 01 (`01_inventory.py`):** after each PDF is recorded (processed, reused, or errored) it
  atomically rewrites the sorted `raw_inventory.jsonl` and `.inventory_checkpoint.json` via a
  local `_flush_progress()` helper. A final flush guarantees a canonical write even when no PDFs
  are found. Resume works from the partial `raw_inventory.jsonl` (incremental reuse is driven by
  per-record `file_fingerprint`).
- **Script 02 (`02_extract_text_and_images.py`):** after each PDF completes (processed or reused)
  it atomically rewrites all three outputs (`extracted_text.jsonl`, `pages.jsonl`,
  `documents.jsonl`) and `.extraction_checkpoint.json`. The checkpoint now carries **only completed
  fingerprints** (finding D fix — it previously wrote fingerprints for *all* inventory items
  upfront), so a resumed `--mode incremental` run reuses finished PDFs (the dual guard: fingerprint
  match **and** record present in the output) and retries the rest. The expensive per-page OCR was
  already cached under `cache/ocr/`; incremental persistence adds durable resume for the assembled
  records and checkpoint on top of that.
- Both loops are single-threaded, so persistence is inline at the end of each iteration rather than
  via `run_with_progress`'s `on_result` hook. No report splitting applies: Script 01 has no report
  and Script 02's report is an end-of-run aggregate summary, not per-piece detail.

### Implications for Scripts 05-08

- Script 05 (quality checks) already scores documents through `run_with_progress`; if its runs get
  long it should adopt the same `on_result` incremental-persistence pattern rather than writing
  `quality_metrics.jsonl` and its report only at the end.
- Scripts 06-08 read `expected_parts.jsonl` (unchanged schema) as their source of truth — **not** the
  Markdown reports — so the split does not change their inputs. If Script 06 renders many per-piece
  files, it should mirror this split + streaming convention (it already targets
  `data/piece_reports/<piece_id>.md`).
- The instrumentation split establishes the folder convention (`data/<report_stem>/` for per-piece
  Markdown, with the top-level `.md` as an index). Reuse this shape for any future per-piece report.

## 5. Core Decision Model

Use confidence tiers everywhere:

- High: deterministic evidence (clear filename + OCR agreement)
- Medium: partial agreement (rule + weak OCR)
- Low: model-only or conflicting signals

Flag low confidence as `needs_review = true` rather than forcing hard conclusions.

## 6. Handling the Specific Problems You Listed

## 6.1 Missing parts

Solved by combining:

- Detected observed parts
- Inferred expected parts
- Gap set difference

Return both:

- strict missing (high confidence)
- possible missing (medium/low confidence)

## 6.2 Unknown expected instrumentation

Use layered inference:

- authoritative listing lookup first (publisher/catalog/distributor)
- multi-source reconciliation second
- conservative fallback only when no authority source is available

Keep method + confidence in report.

## 6.3 Missing score

Classify docs as score/non-score using:

- filename patterns (`score`, `full score`, `condensed`)
- first-page layout cues
- OCR text hints

If no score detected with high confidence, flag as missing score.

## 6.4 Poor scan quality

Use objective image metrics first.
Use model review only for edge cases.

## 7. Suggested Tech Stack

Language:

- Python 3.11+

Libraries:

- `pymupdf` (PDF parsing + rendering) — in use (Scripts 01/02)
- `numpy` + `opencv-python(-headless)` (image quality metrics) — in use (Script 02)
- `typer` (CLI) — in use
- `pytesseract` + `Pillow` (OCR/OSD) — in use (Script 02; needs the Tesseract system binary)
- `PyYAML` (instrument lexicon / rule config) — in use (Script 03; degrades to a built-in lexicon)
- `rapidfuzz` (name/title fuzzy matching) — for Script 04
- `pydantic` (structured outputs) — for Script 04 (Script 03 uses plain dict records)
- `jinja2` (report templates) — for Scripts 06/07
- `pandas`, `pyarrow` — only if/when a Parquet migration is adopted; JSONL is the current format

Data format: newline-delimited JSON (`.jsonl`) across all passes, read/written with the
shared helpers in `scripts/_common.py`.

LLM integration:

- Any API that supports JSON-mode structured outputs
- Cache all calls by content hash to control cost

## 8. Implementation Roadmap (From Zero to Working)

## Phase 1: Foundation (1-2 days)

Status: done. Repo, config layout, and Script 01 inventory implemented and tested.

- Initialize repo and folder structure
- Build config files
- Implement Script 01 inventory
- Validate against 10 sample piece folders

Deliverable:

- Reliable PDF inventory with page counts

## Phase 2: Extraction + Baseline Classification (2-4 days)

Status: extraction done (Script 02, schema `2.2`, with OCR/OSD); rule-based classification done
(Script 03, schema `1.1` — adds `section`, `confidence_tier`, `needs_review`, `catalog_number`,
`part_sort_key`, and a per-piece `observed_parts_by_piece.jsonl` rollup for Scripts 04/06/07).

- Implement Script 02 extraction (done: text, headers, geometry, scanned/DPI, image metrics,
  OCR/OSD via Tesseract, and a per-document rollup)
- Implement rule-based section of Script 03 (done: filename-first instrument/part/score
  classification with page-text confirmation, per-piece duplicate flagging, and a report)
- Produce first part labels without LLM (done: all 26 documents in the sample library classified)

Deliverable:

- First pass per-piece observed parts list

## Phase 3: Expected Parts + Missing Detection (2-4 days)

Status: implemented (Script 04, schema `2.0`) as an online score-lookup engine driven by the GitHub
Copilot CLI: per piece it looks up the actual published edition, infers the ensemble type from that
score, and reconciles the edition's real parts against the observed parts (present/missing/
unexpected + completeness scoring + `needs_review`). Lookup is the primary path; disabled/failed/
no-match/low-confidence pieces degrade to a conservative observed-only record flagged for review.

- Implemented Script 04 lookup-based expected-parts inference + missing detection (done)
- Copilot-CLI online lookup of the published edition + evidence sources (done)
- First missing-parts outputs (done: `data/expected_parts.jsonl` + report)

Deliverable:

- Preliminary missing-parts report

## Phase 4: Quality Engine (2-3 days)

- Implement Script 05 metrics
- Tune thresholds on sample set

Deliverable:

- Quality scores + issue flags

## Phase 5: Reporting + Aggregation (1-2 days)

- Implement Scripts 06 and 07
- Create collection summary and queue

Deliverable:

- End-to-end reports for all pieces

## Phase 6: LLM Refinement Loop (ongoing)

- Add LLM fallback for low-confidence items
- Add caching and retry policies
- Track confidence lift and false positives

Deliverable:

- Better precision with controlled cost

## 9. Operational Practices You Should Include

- Idempotent scripts: re-running should not corrupt outputs
- Incremental mode: process only changed files unless full rebuild requested
- Deterministic IDs: piece/document keys stable across runs
- Full logging: errors and warnings captured per piece
- Audit trail: every inferred decision stores evidence and method

## 10. Cost and Performance Strategy

- Process local deterministic steps first; use LLM only for uncertain records
- Batch OCR and page rendering with multiprocessing
- Cap LLM calls per piece and use cache keys by document hash
- Keep thumbnails low-resolution for classification to reduce payload size

## 11. Risks and Mitigations

Risk: inconsistent naming conventions
Mitigation: robust regex dictionary + fuzzy normalization + model fallback

Risk: OCR failures on bad scans
Mitigation: quality gating + image preprocessing (deskew, contrast)

Risk: overconfident missing-part claims
Mitigation: confidence tiers + explicit "possible missing" category

Risk: API cost explosion
Mitigation: cache aggressively; LLM only on unresolved cases

## 12. Minimum Viable Output Definition

A piece is "analyzed" when you can produce:

- observed parts list with confidence
- inferred expected parts with method and confidence
- missing parts classification
- score presence flag
- quality score with top issue reasons
- manual-review recommendation

## 13. Recommended First Build Target

Start with 25 representative folders (easy, medium, hard quality). Do not begin with all 700.

Success criterion for pilot:

- At least 85% correct part labeling on sample set
- At least 80% useful missing-part flags after manual verification
- Manual queue is short, clear, and prioritized

After pilot threshold is met, run full collection.

## 14. Next Immediate Steps

1. Create the project structure and install dependencies.
2. Implement inventory + extraction scripts first.
3. Build a small validation notebook or script to inspect 25-folder pilot outputs.
4. Tune regex rules and quality thresholds before introducing LLM fallback.
5. Add reporting and run full-batch analysis.

---

If you want, the next step can be a concrete scaffold of these scripts (CLI entry points, config schemas, and starter code) so you can run a pilot immediately.

## 15. Consolidated Decisions and Notes

These notes capture final direction choices made during planning so the implementation stays aligned.

Project naming:

- Preferred plain name: `Band Parts Audit`
- Other plain options:
  - `Band Library Analysis`
  - `Concert Band Archive Analysis`
  - `Band Score Inventory`

Language/runtime:

- Primary language: Python 3.11+
- Reason: strongest practical ecosystem for PDF processing, OCR, image quality analysis, LLM orchestration, and reporting.

Expected instrumentation policy (Script 04):

- Do not rely on generic concert-band profile templates as the primary source of truth.
- Determine expected instrumentation from authoritative external sources tied to the specific work identity.
- Require evidence-backed outputs (URL, snippet, timestamp, source rank).
- Use LLM for extraction/normalization/adjudication only, constrained to retrieved evidence.

Authoritative source ranking:

1. Official publisher product page
2. Official publisher catalog page/PDF
3. Major distributor listing (when instrumentation field is explicit)
4. Secondary library/catalog record

Notation source classification policy (Script 05):

- Each document must be labeled as one of:
  - `printed_original`
  - `handwritten`
  - `mixed_or_uncertain`
- Each label must include `notation_source_confidence` in the range 0-1.
- Include top evidence signals for traceability.

Confidence policy (recommended thresholds):

- `>= 0.85`: auto-accept classification/inference
- `0.60 - 0.84`: review queue (human spot-check)
- `< 0.60`: manual review required before downstream decisions

Missing-part reporting policy:

- Distinguish clearly between:
  - `missing_required_parts`
  - `missing_optional_parts`
  - `ambiguous_equivalency_parts`
- Never collapse ambiguous equivalencies into hard missing claims.

Operational policy:

- Cache OCR and LLM calls by content hash.
- Keep all script passes idempotent and incremental.
- Preserve all evidence used for inference so outputs are auditable.
- Prefer a conservative result with review flag over an overconfident claim.

## 16. New Repository Bootstrap Checklist

When moving this plan into a new repository, create the following first:

1. Folder structure from Section 3.1.
2. Placeholder configs:
   - `config/authority_sources.yaml`
   - `config/regex_rules.yaml`
   - `config/quality_thresholds.yaml`
3. Script stubs for `01` to `08` with CLI entry points.
4. SQLite schema migration for core entities and evidence tables.
5. Pilot runner command targeting 25 representative piece folders.

Definition of done for bootstrap:

- One command can run Scripts 01-06 end to end on pilot data.
- Per-piece JSON/Markdown reports are generated.
- Collection summary and manual review queue are generated.
