# Music Library Analyser

Analyze a large concert band PDF library and produce evidence-based completeness and quality reports at both piece and collection levels.

## Purpose

This project is designed for archive-scale review (hundreds of piece folders, thousands of PDFs) where instrumentation expectations and scan quality are often unknown.

Primary outputs:

- Per-piece report:
  - detected parts
  - expected parts (authority-backed)
  - missing required/optional parts
  - score presence
  - quality and notation-source classification
  - confidence + review flags
- Collection report:
  - completeness and quality trends
  - top risk pieces
  - prioritized manual review queue

## Core Decisions

- Language: Python 3.11+
- Expected instrumentation: authority-first lookup by work identity (publisher/catalog/distributor/library evidence)
- LLM usage: extraction/adjudication constrained to retrieved evidence only
- Quality classification: includes printed-original vs handwritten vs mixed/uncertain with confidence
- Pipeline behavior: idempotent, incremental, and auditable
- Shared infrastructure: every script builds on `scripts/_common.py` for logging, checkpointing,
  record envelopes, concurrency, report helpers, and the status/tier vocabulary constants
  (`LookupStatus`, `CompletenessTier`, `ProcessingStatus`) — see §4.9 of the plan document

## Pipeline Overview

1. `01_inventory.py`
- Enumerate piece folders and PDFs
- Capture file metadata and PDF health

2. `02_extract_text_and_images.py`
- Extract embedded text per page (plus word count, alnum ratio, and a normalized text hash)
- Capture prominent/header text candidates for part/title detection
- Extract zoned corner/bottom text (top-left/center/right, bottom) for part-name and identity detection
- Render page thumbnails and compute page features (text density, black/white ratio)
- Detect page geometry (size, rotation, orientation) and born-digital vs scanned pages
- Flag blank/near-blank pages and detect music staves (per-page `has_staves`/`staff_line_count`)
- Parse best-effort copyright/identity candidates (publisher, year, arranger, composer)
- Compute image-quality metrics (blur, skew, contrast) when `numpy`/`opencv-python` are available
- Emit a per-document rollup (`data/documents.jsonl`) alongside `extracted_text.jsonl` and `pages.jsonl`
- Write a human-readable Markdown summary (`data/extraction_report.md`) with coverage, quality, and per-folder/per-document breakdowns
- OCR scanned/no-text pages with Tesseract (OSD auto-rotate first); runs by default and degrades gracefully when the toolchain is unavailable
- Multi-pass OCR (`--ocr-multipass`, default on): run several passes at varied PSM + DPI, keep every non-empty pass in `ocr_candidates`, and select the highest-scoring text (`--single-pass-ocr` for one pass)
- Consolidate each PDF's OCR candidates into a normalized instrument list via one Copilot CLI call per file (`--ocr-llm`, default on), recording `ocr_llm_status`/`ocr_llm_instruments` on the document record for Script 03
- Optionally classify notation source from the rendered page image via one Copilot CLI call per file (`--use-vision`, default on; scanned documents only) — records `vision_notation_source` (`printed_original`/`handwritten`/`mixed_or_uncertain`), `vision_legibility` (`good`/`fair`/`poor`), `vision_confidence`, and `vision_notes` on the document record for Script 05 to adjudicate. Cached separately from OCR (keyed by thumbnail hash) so a vision-prompt change never re-runs OCR and re-rendering never re-runs vision; prompt in `config/llm_prompts/classify_notation_source.txt`
- Parallel pipeline: OCR passes run across a thread pool (page rendering stays on the main thread since PyMuPDF is not thread-safe), and each file's LLM classification is spun off asynchronously so the next PDF starts OCR while prior Copilot calls finish; `--ocr-workers` sets the worker count (`0` = auto `min(8, CPU)`, `1` = serial) and also bounds concurrent OCR-LLM calls

3. `03_part_classifier.py`
- Classify each PDF's instrument/part and whether it is a score (filename-first, deterministic)
- Isolate the part segment from the filename, match against an instrument lexicon (`config/regex_rules.yaml`), and confirm/recover with Script 02 page text
- Extract clef, transposition, and part index; assign a `section` and a conventional-score-order `part_sort_key`; flag duplicate labels within a piece
- Emit downstream-ready signals: `confidence_tier`, `needs_review`, and parsed `catalog_number` / `piece_title_guess` work-identity seeds
- Write `data/part_predictions.jsonl`, a per-piece `data/observed_parts_by_piece.jsonl` rollup, and a Markdown report (`data/part_classification_report.md`); an LLM fallback hook exists but is disabled/unwired

4. `04_expected_parts_inference.py`
- Establish each piece's *actual published edition* instrumentation via a three-stage engine that stops at the first confident result: **Stage A** OCR a local score (from `part_predictions.jsonl` + `extracted_text.jsonl`, re-OCR via PyMuPDF/Tesseract only if the reused text is thin) and summarize it with the Copilot CLI; **Stage B** online authority lookup via the Copilot CLI (`copilot -p ...`, config in `config/score_lookup.yaml`, prompt in `config/llm_prompts/lookup_instrumentation.txt`); **Stage C** download + OCR any score images the lookup returns, then summarize (prompt in `config/llm_prompts/summarize_score_instrumentation.txt`). The ensemble type is inferred per piece from the found score rather than assumed
- Reconcile the resulting part slots against observed parts (count-based, robust to null part indices) into present / missing / unexpected sets
- Score required-part completeness + tier, flag `score_missing` and `needs_review`, and record `detection_method`, `ocr_source`, `local_score_path`, `evidence_sources`, `candidate_score_images`, and resolved work identity
- Toggle stages with `--local-score/--no-local-score`, `--lookup/--no-lookup`, and `--image-ocr/--no-image-ocr`; degrade conservatively (all stages off/failed, CLI missing/error, `no_match`, or `low_confidence`): declare no missing parts, set `completeness_score: null`, and flag for review — never fabricate a missing part
- Each lookup logs a one-line Stage B result summary (`match_found`, confidence, `expected_parts`, `candidate_score_images` counts) so it is clear when/why Stage C image download runs; `--save-lookups` (default on) also persists each raw lookup result (prompt + parsed JSON) under `cache/llm/lookups/` for auditing
- Persist results **incrementally**: `data/expected_parts.jsonl` and the checkpoint are rewritten atomically as each piece completes, so stopping mid-run (e.g. 400/700 pieces) never loses finished work and `--mode incremental` resumes without re-spending credits
- Emit `data/expected_parts.jsonl` (schema 2.1) and a summary report (`data/expected_parts_report.md`). The per-piece instrumentation report is split by default (`--split-instrumentation`) into one file per piece under `data/expected_instrumentation/`, written as each piece finishes, with `data/expected_instrumentation.md` as a linking index; `--no-split-instrumentation` writes a single combined file. `--mode incremental` caches per-piece results by fingerprint to avoid re-spending AI credits or re-OCR

5. `05_quality_checks.py`
- Score per-document scan quality (0-100) into a `quality_band` (`good` / `review` / `poor`, plus `unknown` when a document has no scoreable pages) by thresholding the objective per-page metrics Script 02 already computed (resolution, skew, contrast, blur, OCR confidence, blankness/noise) — pages are never re-rendered, and a missing (null) metric never raises an issue
- Detect per-page issues (`low_resolution`, `excessive_skew`, `low_contrast`, `heavy_blur`, `ocr_illegible`, `blank_page`, `noise_page`), roll them into a document score weighted by affected-page fraction, and record `top_issues`, `worst_page`, and `needs_review`
- Classify notation source (`printed_original`, `handwritten`, `mixed_or_uncertain`) with a 0-1 confidence and evidence from searchable-text fraction, OCR confidence, and alphanumeric ratio
- Adjudicate the optional Script 02 vision signal (`--use-vision`, default on when the fields are present): a confident vision verdict overrides `notation_source_type` and caps `quality_band` (handwritten is never `good`; poor legibility is forced to `poor`), because deterministic metrics cannot separate handwritten manuscript from a readable printed photocopy. Adds `handwritten_notation` / `low_legibility` issue codes and echoes `vision_*` fields onto the record
- Thresholds live in `config/quality_thresholds.yaml` (built-in defaults when absent), including a `vision:` section (enable/disable, `min_confidence`, band caps); `--mode incremental` caches per-document results by fingerprint (which now folds in the vision signal). The *cropping margin loss* check remains deferred (Script 02 exposes no margin metric)
- Emit `data/quality_metrics.jsonl` (schema 1.1) plus a Markdown summary `data/quality_report.md`

6. `06_piece_report.py`
- Join every upstream per-piece and per-document signal on `piece_id` (expected parts, observed parts, part predictions, quality metrics, documents, page thumbnails) into one report per piece; the stage is deterministic and fully offline (computes nothing new about the music)
- Piece universe is the union of `piece_id`s across the expected / observed / predictions / quality sources, so a piece missing an upstream stage still gets a report; detected parts are ordered by Script 03's `part_sort_key` and joined to quality by `pdf_path`
- Derive `reason_codes` (`missing_score`, `missing_required_parts`, `low_quality_scans`, `handwritten_or_illegible`, `unexpected_parts`, `low_confidence_parts`, `duplicate_parts`, `instrumentation_unresolved`), matching `recommended_actions`, a `severity` hint (`ok` / `review` / `high`), and a piece-level `needs_review` roll-up
- Roll up magnitudes for downstream priority/aggregation: `quality_summary` (band distribution + low-quality / handwritten document counts), echoed Script 04 scalar counts (`expected_part_count`, `missing_required_count`, `unexpected_part_count`, `observed_instrument_count`), and `action_items` naming the specific offending documents/parts per reason code
- Stream per-piece `.md` + `.json` as each completes; `--mode incremental` reuses cached reports by joined-input fingerprint, and orphan cleanup prunes only managed reports (never the checkpoint)
- Emit `data/piece_reports/<piece_id>.{md,json}` (schema 1.1) plus a top-level index `data/piece_reports.md`

7. `07_collection_report.py`
- Aggregate every per-piece report (`data/piece_reports/*.json`, schema 1.1) into one collection-wide view; deterministic and fully offline (recomputes nothing — only counts / groups / orders Script 06's facts). Errors with a "run Script 06 first" message if there are no piece reports
- Headline totals (complete sets, missing required parts, missing score, needs-review, high-severity, processing errors, total / low-quality / handwritten documents) plus distributions over completeness, lookup status, and severity, and a document quality-band distribution summed from each piece's `quality_summary.band_counts`
- Reason-code frequency (pieces exhibiting each code, with its action string), top missing *required* instruments across the library (by `canonical_instrument` + `section`), summed review counts, and a high-severity `attention_pieces` list linking each piece's report
- Emit a structured `pieces[]` index in `summary.json` (per-piece magnitude counts + `reason_codes[]`) so Script 08 can prioritize from one stable file; a `completeness_score_summary` KPI (mean / median / range); and a `record_version_distribution` + `pieces_skipped` data-health guard that warns on mixed/stale schema versions or skipped files
- Emit `data/collection_reports/summary.md` (dashboard), `summary.json` (schema 1.1 aggregate, the stable contract), and `pieces.csv` (flat projection of `pieces[]`); prioritization is deferred to Script 08

8. `08_manual_review_pack.py`
- Generate prioritized review queue

## Recommended Repository Structure

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
      classify_notation_source.txt
      lookup_instrumentation.txt
      summarize_score_instrumentation.txt
      verify_low_confidence.txt
  data/
    raw_inventory.jsonl
    extracted_text.jsonl
    pages.jsonl
    documents.jsonl
    extraction_report.md
    part_predictions.jsonl
    observed_parts_by_piece.jsonl
    expected_parts.jsonl
    expected_parts_report.md
    expected_instrumentation.md
    expected_instrumentation/
    quality_metrics.jsonl
    quality_report.md
    piece_reports/
    piece_reports.md
    collection_reports/
      summary.md
      summary.json
      pieces.csv
  cache/
    ocr/
    llm/
    render/
    vision/
  logs/
  outputs/
    analysis.db
```

## Data and Confidence Policy

Expected-parts inference methods:

- `local_score_ocr`
- `authority_lookup`
- `score_image_ocr`
- `fallback_conservative`

Confidence threshold guidance:

- `>= 0.85`: auto-accept
- `0.60 - 0.84`: send to review queue
- `< 0.60`: manual review required

Always separate:

- `missing_required_parts`
- `missing_optional_parts`
- `ambiguous_equivalency_parts`

## Authority-First Source Ranking

1. Official publisher product page
2. Official publisher catalog page/PDF
3. Major distributor listing with explicit instrumentation
4. Secondary library/catalog record

All inferred expected parts should preserve:

- source URL
- source title
- retrieval timestamp
- evidence snippet
- source rank

## Quality and Notation Source Classification

Each document record (`data/quality_metrics.jsonl`, schema 1.1) includes:

- `quality_score` (0-100 float, or `null` when no pages were scoreable)
- `quality_band` (`good`, `review`, `poor`, or `unknown`)
- `needs_review`, `page_issue_count`, `issue_summary`, `top_issues`, `worst_page`,
  `page_findings`
- `notation_source_type` (`printed_original`, `handwritten`, `mixed_or_uncertain`)
- `notation_source_confidence` (0-1)
- `notation_source_evidence` (feature signals such as `searchable_fraction=0.80`)
- `vision_applied` plus echoed `vision_notation_source` / `vision_legibility` /
  `vision_confidence` / `vision_notes` when the Script 02 vision signal was adjudicated
- a `metrics` block (median DPI, mean contrast/blur, max skew, mean alnum/OCR confidence,
  image-based fraction, blank-page count)

When Script 02 runs with vision enabled (the default; pass `--no-vision` to skip it), the
per-document rollup (`data/documents.jsonl`, schema 2.4) also carries the raw `vision_status`,
`vision_notation_source`, `vision_legibility`, `vision_confidence`, and `vision_notes` fields
(defaulting to `not_applied`/`null` otherwise); downstream consumers should treat them as
optional/nullable.

> The thresholds in `config/quality_thresholds.yaml` are initial heuristics and are not yet
> calibrated against real scan data.

## Suggested Tech Stack

- PDF: `pymupdf`, `pypdf`
- OCR: `pytesseract` (or PaddleOCR)
- Imaging/features: `opencv-python`, `numpy`, `Pillow`
- Data: `pandas`, `pyarrow`, `sqlite3`
- Matching: `rapidfuzz`
- Validation/models: `pydantic`
- CLI: `typer`
- Reporting: `jinja2`
- LLM orchestration: provider SDK or `litellm`

## OCR Prerequisite: Tesseract

OCR of scanned pages relies on the **Tesseract OCR engine**, a native binary that must be
installed separately — `pip install pytesseract` only provides the Python wrapper, not the engine
itself. Tesseract is only required when OCR is enabled; the rest of the pipeline (Scripts 01-02
embedded-text extraction, rendering, and image metrics) runs without it.

### Install the engine

- **Windows**: install the UB-Mannheim build from
  <https://github.com/UB-Mannheim/tesseract/wiki> (`tesseract-ocr-w64-setup-*.exe`). During
  setup, select any additional language packs you need (English `eng` is included by default).
- **macOS**: `brew install tesseract`
- **Debian/Ubuntu**: `sudo apt-get install tesseract-ocr`

### Make it discoverable

The code needs to find the `tesseract` executable. Either:

- add the install directory to your `PATH` (on Windows, typically
  `C:\Program Files\Tesseract-OCR`), or
- point `pytesseract` at it explicitly in code:

  ```python
  import pytesseract
  pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
  ```

### Verify the install

```powershell
tesseract --version
```

If that prints a version (e.g. `tesseract v5.x`), the engine is ready. From Python you can also
confirm the wrapper sees it with `pytesseract.get_tesseract_version()`.

### Notes

- Higher-DPI renders (~300 DPI) markedly improve OCR accuracy over the default 150-DPI thumbnails.
- Non-English or symbol-heavy scans require the matching `traineddata` language pack installed
  above, selected via the OCR `lang` option.
- Orientation/script detection (OSD, for auto-rotating sideways scans) uses the same engine via
  `pytesseract.image_to_osd`.

## Pilot-First Execution Plan

Start with a pilot sample of 25 representative folders.

Success targets:

- >=85% correct part labeling on sample
- >=80% useful missing-part flags after manual verification
- clear and bounded manual review queue

After pilot calibration, scale to the full collection.

## Bootstrap Checklist

1. Create project structure and config placeholders.
2. Implement Scripts 01-03 for deterministic baseline.
3. Add Script 04 authority lookup and evidence persistence.
4. Add Script 05 quality + notation-source classification.
5. Generate per-piece and collection reports.
6. Tune thresholds and source ranking on pilot set.

## Notes for Next Session

If you spin up a new repo and resume there, start by implementing:

- script stubs with CLI entry points
- SQLite schema for piece/document/page/evidence/finding
- a single command that runs Scripts 01-06 on pilot data

The full planning document is available in `BAND_COLLECTION_ANALYSIS_PLAN.md`.
