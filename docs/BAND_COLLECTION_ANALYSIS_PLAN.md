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
    pages.parquet
    extracted_text.parquet
    part_predictions.parquet
    expected_parts.parquet
    quality_metrics.parquet
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

## 4.1 Script 01: Inventory Pass (`01_inventory.py`)

Purpose:

- Discover all piece folders
- Enumerate PDFs
- Capture basic file facts

Outputs:

- `data/raw_inventory.jsonl` (one record per PDF)

Fields:

- piece_id (stable hash or folder path key)
- piece_folder
- pdf_path
- file_size
- modified_time
- page_count
- pdf_metadata (title, author, producer if present)

Checks:

- unreadable PDFs
- encrypted PDFs
- zero-page or malformed documents

## 4.2 Script 02: Extraction Pass (`02_extract_text_and_images.py`)

Purpose:

- Pull text where embedded text exists
- OCR where text is absent
- Render low-res thumbnails for visual QA and LLM prompts

Libraries:

- `pypdf` or `pymupdf` for PDF structure
- `pdf2image` or `pymupdf` for page rendering
- `pytesseract` or PaddleOCR for OCR

Outputs:

- `data/extracted_text.parquet` (page-level text)
- `data/pages.parquet` (render + page features)

Useful page-level features:

- OCR text density
- average contrast
- blur estimate (Laplacian variance)
- skew estimate
- black/white ratio

## 4.3 Script 03: Part Classification (`03_part_classifier.py`)

Purpose:

Identify what each PDF likely is:

- Part name (Trumpet 1, Clarinet 2, Flute, Timpani, etc.)
- Whether document is likely score, condensed score, or single part

Method (hybrid):

1. Rule-first:
- Filename regex rules (`regex_rules.yaml`)
- First-page text regex patterns

2. Model fallback:
- LLM prompt with extracted snippets + filename + optional thumbnail
- Return structured JSON

3. Ensemble harmonization:
- If two docs both claim "Trumpet 1", resolve conflicts with confidence logic

Outputs:

- `data/part_predictions.parquet`

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

- `data/expected_parts.parquet`

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

Checks:

- Low effective resolution
- Excessive skew
- Extreme low contrast / washed pages
- Heavy blur
- OCR illegibility score
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

- `data/quality_metrics.parquet`

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

- `pymupdf` (PDF parsing + rendering)
- `pypdf` (metadata fallback)
- `pandas`, `pyarrow` (tabular data)
- `opencv-python` (image quality metrics)
- `pytesseract` or PaddleOCR (OCR)
- `rapidfuzz` (name/title fuzzy matching)
- `pydantic` (structured outputs)
- `typer` (CLI)
- `jinja2` (report templates)

LLM integration:

- Any API that supports JSON-mode structured outputs
- Cache all calls by content hash to control cost

## 8. Implementation Roadmap (From Zero to Working)

## Phase 1: Foundation (1-2 days)

- Initialize repo and folder structure
- Build config files
- Implement Script 01 inventory
- Validate against 10 sample piece folders

Deliverable:

- Reliable PDF inventory with page counts

## Phase 2: Extraction + Baseline Classification (2-4 days)

- Implement Script 02 extraction
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
