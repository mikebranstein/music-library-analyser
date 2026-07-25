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

## Pipeline Overview

1. `01_inventory.py`
- Enumerate piece folders and PDFs
- Capture file metadata and PDF health

2. `02_extract_text_and_images.py`
- Extract embedded text
- OCR scanned pages
- Render thumbnails and compute page features

3. `03_part_classifier.py`
- Classify part labels and score candidates
- Combine regex/rules with model fallback

4. `04_expected_parts_inference.py`
- Build work identity fingerprint
- Retrieve authoritative instrumentation sources
- Reconcile sources and infer expected parts with evidence

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
  outputs/
    analysis.db
```

## Data and Confidence Policy

Expected-parts inference methods:

- `authority_lookup`
- `authority_plus_llm`
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
- Imaging/features: `opencv-python`, `Pillow`
- Data: `pandas`, `pyarrow`, `sqlite3`
- Matching: `rapidfuzz`
- Validation/models: `pydantic`
- CLI: `typer`
- Reporting: `jinja2`
- LLM orchestration: provider SDK or `litellm`

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
