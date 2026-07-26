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
    quality_metrics.jsonl
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
- Roll up per-page results into a per-document summary

OCR is implemented via `pytesseract` + the Tesseract system binary and runs by default on pages
flagged `needs_ocr` or `is_image_based`. It degrades gracefully (auto-disabled with a warning)
when the toolchain is unavailable; those pages keep `needs_ocr = true` and null OCR fields.

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
- section (coarser lexicon-driven grouping, e.g. `cornets_trumpets`, `low_brass`, `tubas`;
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
- `data/part_predictions.jsonl` only when per-document detail is needed (e.g. `confidence_tier`,
  `evidence_source`, `match_details`); the observed-part key and `confidence`/`duplicate_in_piece`
  are already rolled up per piece

Approach:

Status: implemented (Script 04, schema `2.0`) as an **online score-lookup engine driven by the
GitHub Copilot CLI**. Rather than assuming an ensemble template, it looks up the *actual published
edition* per piece and reports that edition's real instrumentation; the ensemble type is inferred
from whatever score is found. When lookup is disabled or fails, the piece degrades to a
conservative, observed-only record flagged for review — it never fabricates a "missing" part
without an authoritative source. See
`docs/SCRIPT04_EXPECTED_PARTS_INFERENCE_IMPLEMENTATION_PLAN.md` for the full design.

1. Online score lookup (implemented primary path):
- For each piece, build a work-identity query from the observed instruments, `catalog_number`,
  `piece_title_guess`, and Script 02 `identity_candidates`, then render the prompt template at
  `config/llm_prompts/lookup_instrumentation.txt`.
- Invoke the GitHub Copilot CLI headlessly (`copilot -p <prompt> --allow-all-tools -s
  --no-ask-user ...`, with `--allow-all-urls` or a restricted `--allow-url` list from
  `config/score_lookup.yaml`). The model searches authoritative web sources (publisher, distributor,
  library catalog) and returns exactly one JSON object between the
  `<<<SCORE_JSON>>>` / `<<<END_SCORE_JSON>>>` sentinels.
- The returned edition provides `ensemble_type`, `ensemble_display_name`, `work_identity`,
  `evidence_sources`, and `expected_parts[{canonical_instrument, part_index, label, section,
  required}]`. No instrumentation families are hardcoded; the parts come from the found score.
- Reconcile the looked-up part slots against the piece's observed parts. Matching is count-based per
  canonical instrument (robust to null `part_index`), preferring explicit index matches, then
  consuming slots in order. Produces `present` / `missing` / `unexpected` sets.
- Compute a required-part completeness score + tier, and `needs_review` when required parts are
  missing, the score is missing, unexpected parts appear, or the Script 03 rollup already flagged
  the piece.

2. Conservative fallback (per-piece degradation):
- `--no-lookup` (or `enabled: false` in `config/score_lookup.yaml`) → `lookup_status: disabled`.
- The Copilot CLI is not on `PATH`, times out, or exits non-zero → `lookup_status: error`.
- The model reports `match_found: false` → `lookup_status: no_match`.
- Match confidence below `confidence_threshold` → `lookup_status: low_confidence`.
- In every fallback case the record declares no expected/missing parts, sets
  `completeness_score: null` and `completeness_tier: unknown`, and is flagged `needs_review: true`.
  Any evidence, notes, and resolved identity returned before the fallback are retained.

Note: lookup accuracy is bounded by what the model can find online and is network- and
AI-credit-dependent and nondeterministic. `--mode incremental` caches per-piece results by
fingerprint (observed parts + `has_score` + model + confidence threshold + prompt-template hash) to
avoid re-spending credits on unchanged pieces.

Outputs:

- `data/expected_parts.jsonl` (one record per piece)
- `data/expected_parts_report.md` (Markdown summary)
- `data/expected_instrumentation.md` (per-piece expected instrumentation from the lookups)
- `data/.expected_parts_checkpoint.json` (checkpoint; supports `--mode incremental`)

Fields (`expected_parts.jsonl`, schema 2.0):

- `ensemble_type`, `ensemble_display_name` (inferred per piece from the found score)
- `detection_method` (`authority_lookup` | `conservative_fallback` | `error`)
- `inference_method` (`authority_lookup` | `fallback_conservative`)
- `lookup_status` (`matched` | `low_confidence` | `no_match` | `disabled` | `error`)
- `lookup_model`, `lookup_notes`, `identity_match_confidence`
- `authority_coverage` (`full` when evidence sources were returned, else `none`),
  `evidence_sources` (list of `{url, title, snippet, retrieved}`)
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

## 4.5 Script 05: Quality Checks (`05_quality_checks.py`)

Purpose:

Flag poor scan quality and likely unusable pages.

Inputs:

- `data/pages.jsonl` already carries the core objective metrics from Script 02:
  `blur_variance` (Laplacian variance), `skew_angle_deg`, `contrast_std`, `text_density`,
  `black_white_ratio`, `estimated_dpi`, `is_image_based`, `image_count`, and geometry.
- `data/extracted_text.jsonl` provides `alnum_ratio` and `word_count` as legibility signals.

Script 05 should consume and threshold these existing metrics rather than recompute them;
re-rendering is only needed for optional model-assisted vision checks. Thresholds live in
`config/quality_thresholds.yaml`. Script 03's `needs_review` flag can additionally prioritize which
documents warrant a manual/visual quality pass.

Checks (mostly derived from Script 02 metrics):

- Low effective resolution (`estimated_dpi`, render dimensions)
- Excessive skew (`skew_angle_deg`)
- Extreme low contrast / washed pages (`contrast_std`)
- Heavy blur (`blur_variance`)
- OCR illegibility score (from Script 02 `ocr_confidence`; interim proxy: `alnum_ratio` when OCR disabled)
- Cropping margin loss
- Page anomalies (blank page, mostly noise)
- Document style classification:
  - scanned printed/engraved original
  - hand-written manuscript part
  - mixed/uncertain

Handwritten-vs-printed detection signals:

- OCR character confidence and stability across pages
- Stroke regularity/consistency (engraved notation is usually more uniform)
- Text baseline variance and letterform irregularity in headers/markings
- Symbol contour variance and spacing entropy
- Presence of pen-like pressure artifacts or inconsistent line weight

Optional model-assisted checks:

- LLM vision model reviews sampled pages and labels
  - readability
  - handwritten vs engraved
  - severe artifact presence
  - confidence rationale for classification

Outputs:

- `data/quality_metrics.jsonl`

Per-document quality summary:

- quality_score (0-100)
- quality_band (`good`, `review`, `poor`)
- top_issues (list)
- page_issue_count
- notation_source_type (`printed_original`, `handwritten`, `mixed_or_uncertain`)
- notation_source_confidence (0-1)
- notation_source_evidence (top 3-5 feature signals or page references)

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

Output:

- CSV and optional lightweight HTML dashboard

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
