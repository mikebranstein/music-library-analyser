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
- Emit `data/expected_parts.jsonl` (schema 2.1), a summary report (`data/expected_parts_report.md`), and a per-piece instrumentation report listing each piece's expected parts (`data/expected_instrumentation.md`); `--mode incremental` caches per-piece results by fingerprint to avoid re-spending AI credits or re-OCR

5. `05_quality_checks.py`
- Score scan quality
- Classify notation source (`printed_original`, `handwritten`, `mixed_or_uncertain`)

6. `06_piece_report.py`
- Generate per-piece JSON + Markdown reports

7. `07_collection_report.py`
- Aggregate metrics across all pieces

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
    quality_metrics.jsonl
    piece_reports/
    collection_reports/
  cache/
    ocr/
    llm/
    render/
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

Each document should include:

- `quality_score` (0-100)
- `quality_band` (`good`, `review`, `poor`)
- `notation_source_type` (`printed_original`, `handwritten`, `mixed_or_uncertain`)
- `notation_source_confidence` (0-1)
- `notation_source_evidence` (signals/page refs)

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
