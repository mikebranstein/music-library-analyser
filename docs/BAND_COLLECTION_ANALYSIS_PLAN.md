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
    regex_rules.yaml
    quality_thresholds.yaml
    llm_prompts/
      classify_part.txt
      infer_expected_parts.txt
      verify_low_confidence.txt
  data/
    raw_inventory.jsonl
    extracted_text.jsonl
    pages.jsonl
    documents.jsonl
    part_predictions.jsonl
    expected_parts.jsonl
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
- Roll up per-page results into a per-document summary

OCR is deferred to a later stage (needs the Tesseract system binary). Pages with no
searchable text are flagged `needs_ocr = true` for that future pass.

Libraries (as built):

- `pymupdf` for PDF structure, text, and page rendering
- `numpy` + `opencv-python(-headless)` for image-quality metrics (optional; degrade to `null`)

Outputs (three JSONL datasets + checkpoint):

- `data/extracted_text.jsonl` (one record per page: text + text-quality signals)
- `data/pages.jsonl` (one record per page: render + geometry + image-quality metrics)
- `data/documents.jsonl` (one record per PDF: rollup for identity/quality/reporting stages)
- `data/.extraction_checkpoint.json`

Key `extracted_text.jsonl` fields for downstream use:

- embedded_text, embedded_text_length, extraction_method
- text_is_searchable, needs_ocr, ocr_text (null), ocr_confidence (null)
- word_count, alnum_ratio, page_text_hash (normalized-text dedupe key)
- header_text_candidates (largest-font strings), top_lines (top-of-page reading order)

Key `pages.jsonl` fields for downstream use:

- thumbnail_path, thumbnail_hash, render_dpi, render_width_px, render_height_px
- text_density, black_white_ratio
- page_width_pt, page_height_pt, rotation, orientation, aspect_ratio
- image_count, largest_image_coverage, is_image_based, estimated_dpi
- contrast_std, blur_variance, skew_angle_deg (null when numpy/opencv absent or disabled)

Key `documents.jsonl` fields for downstream use:

- piece_id, piece_folder, pdf_filename, page_count
- pages_with_text, pages_needing_ocr, ocr_fraction
- total_text_length, total_word_count
- pages_image_based, image_based_fraction
- first_page_text, first_page_header_candidates
- processing_status (`success` / `partial_error`)

## 4.3 Script 03: Part Classification (`03_part_classifier.py`)

Purpose:

Identify what each PDF likely is:

- Part name (Trumpet 1, Clarinet 2, Flute, Timpani, etc.)
- Whether document is likely score, condensed score, or single part

Method (hybrid):

Inputs (from Scripts 01/02):

- `data/raw_inventory.jsonl` for `pdf_filename` and `piece_folder`
- `data/extracted_text.jsonl` for per-page `embedded_text`, `header_text_candidates`, and
  `top_lines`
- `data/documents.jsonl` for `first_page_text` and `first_page_header_candidates`
- `data/pages.jsonl` for `is_image_based` (route scanned pages to the future OCR/vision path)

1. Rule-first:
- Filename regex rules (`regex_rules.yaml`) against `pdf_filename`
- First-page text regex patterns against `first_page_header_candidates` / `header_text_candidates`

2. Model fallback:
- LLM prompt with extracted snippets + filename + optional thumbnail (`thumbnail_path`)
- Return structured JSON

3. Ensemble harmonization:
- If two docs both claim "Trumpet 1", resolve conflicts with confidence logic

Outputs:

- `data/part_predictions.jsonl`

Key fields:

- predicted_part
- family (woodwind/brass/percussion/score/other)
- part_index (e.g., 1,2,3)
- is_score
- confidence
- evidence_source (`filename`, `ocr`, `llm`, `combined`)

## 4.4 Script 04: Expected Parts Inference (`04_expected_parts_inference.py`)

Purpose:

Estimate which parts should exist for each piece so missing parts can be flagged.

Inputs:

- `data/documents.jsonl` for work-identity seeds (`first_page_text`,
  `first_page_header_candidates`, `piece_folder`, `pdf_filename`)
- `data/part_predictions.jsonl` for observed parts per piece

Approach:

1. Authority-first metadata lookup (primary path):
- Build a search query from normalized work identity fields:
  - piece title
  - composer
  - arranger
  - publisher
  - edition/revision label
  - catalog number / item number / SKU
  - series name (for publisher series-based catalogs)
  - publication year or copyright year
  - subtitle / movement name (when folder naming is partial)
  - alternate title spellings and punctuation-normalized variants
  - known duration and grade level (if printed on cover/score)
- Retrieve candidate source pages from authoritative catalogs/listings first:
  - publisher product pages
  - publisher catalog PDFs
  - distributor listings that reproduce publisher instrumentation fields
  - library records with explicit instrumentation notes
- Parse and normalize instrumentation text from those sources into canonical part keys.

Identity hardening before lookup:
- Build a `work_identity_fingerprint` using weighted fields (title, composer, publisher, catalog no., year).
- Use fuzzy matching for OCR noise and typographical variants.
- Reject low-similarity candidates unless catalog number or publisher item code matches.

2. Multi-source reconciliation:
- Compare instrumentation extracted from multiple sources.
- Prefer sources in this order when conflicts occur:
  - official publisher page
  - official publisher catalog
  - major distributor listing
  - secondary library/catalog listing
- Keep source URLs, snippets, and retrieval timestamps as evidence.

3. LLM as extraction and adjudication layer (not primary truth source):
- Use LLM to:
  - extract structured instrumentation from messy listing text
  - resolve aliasing and shorthand into canonical part names
  - explain conflict resolution when two sources disagree
  - distinguish optional/doubled parts from required core parts
  - detect transposition-specific variants (e.g., clarinet in Bb vs A)
  - preserve publisher wording alongside normalized labels
- Constrain the prompt to only use retrieved evidence.
- Require structured output with evidence references for each expected part.

4. Fallback path when no authoritative listing is found:
- Mark piece as `authority_not_found`.
- Use observed parts plus conservative inference for a temporary expected set.
- Downgrade confidence and force manual review.

Outputs:

- `data/expected_parts.jsonl`

Fields:

- expected_parts (list)
- missing_parts (list)
- inference_method (`authority_lookup`, `authority_plus_llm`, `fallback_conservative`)
- confidence
- evidence_sources (list of URL/title/date/snippet)
- authority_coverage (`full`, `partial`, `none`)
- work_identity:
  - title_normalized
  - title_variants
  - composer
  - arranger
  - publisher
  - catalog_number
  - edition
  - series
  - publication_year
- source_match_diagnostics:
  - candidate_count
  - accepted_source_count
  - rejected_candidates (with rejection reason)
  - identity_match_score
- instrumentation_structure:
  - required_parts
  - optional_parts
  - alternate_substitutions (e.g., bassoon optional if bass clarinet present)
  - doubles_and_cues
  - transposition_requirements
- completeness_flags:
  - missing_required_parts
  - missing_optional_parts
  - ambiguous_equivalency_parts

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
`config/quality_thresholds.yaml`.

Checks (mostly derived from Script 02 metrics):

- Low effective resolution (`estimated_dpi`, render dimensions)
- Excessive skew (`skew_angle_deg`)
- Extreme low contrast / washed pages (`contrast_std`)
- Heavy blur (`blur_variance`)
- OCR illegibility score (future OCR pass; interim proxy: `alnum_ratio`)
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

- `data/documents.jsonl`, `data/part_predictions.jsonl`, `data/expected_parts.jsonl`,
  `data/quality_metrics.jsonl`, and `data/pages.jsonl` (for thumbnail references)

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
- pieces with no score
- distribution of quality bands
- top missing instruments overall
- confidence distribution

Outputs:

- `data/collection_reports/summary.md`
- `data/collection_reports/summary.json`
- `data/collection_reports/manual_review_queue.csv`

## 4.8 Script 08: Manual Review Pack (`08_manual_review_pack.py`)

Purpose:

Generate a prioritized queue so your manual effort targets the highest-value fixes.

Queue priority formula (example):

`priority = missing_critical_weight + quality_penalty + low_confidence_penalty`

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
- `pytesseract` or PaddleOCR (OCR) — deferred (needs Tesseract system binary)
- `rapidfuzz` (name/title fuzzy matching) — for Script 04
- `pydantic` (structured outputs) — for Scripts 03/04
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

Status: extraction done (Script 02, schema `2.0`, without OCR); baseline classification
(Script 03 rules) pending.

- Implement Script 02 extraction (done: text, headers, geometry, scanned/DPI, image metrics,
  and a per-document rollup; OCR deferred)
- Implement rule-based section of Script 03
- Produce first part labels without LLM

Deliverable:

- First pass per-piece observed parts list

## Phase 3: Expected Parts + Missing Detection (2-4 days)

- Implement Script 04 with authority-first source lookup
- Add multi-source reconciliation and evidence scoring
- Create first missing-parts outputs

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
