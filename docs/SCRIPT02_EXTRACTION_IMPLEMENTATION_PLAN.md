# Script 02 Extraction Implementation Plan

## 1. Objective

Implement Script 02 (`scripts/02_extract_text_and_images.py`): a deterministic, incremental,
page-level extraction pass that consumes the Script 01 inventory and produces per-page text
and per-page render/feature datasets.

This plan is derived from:

- `README.md`
- `docs/BAND_COLLECTION_ANALYSIS_PLAN.md` (Section 4.2)
- `docs/BOOTSTRAP_AND_SCRIPT01_IMPLEMENTATION_PLAN.md`
- `scripts/01_inventory.py` (input contract)
- `scripts/_common.py` (shared helpers)

## 2. Phased Scope

Phase 1 (this implementation) uses `pymupdf` only:

- Embedded text extraction per page.
- Page rendering to PNG thumbnails in `cache/render/`.
- Lightweight page features computable from the rendered pixmap: `text_density` and
  `black_white_ratio`.
- Two JSONL outputs plus a checkpoint, mirroring Script 01 conventions.

Deferred to Phase 2 (out of scope now):

- OCR via `pytesseract` (requires the Tesseract system binary; on Windows it is not added to
  PATH automatically).
- Advanced image metrics (blur/skew/contrast) via `opencv-python` / `numpy`.
- Migration of outputs from JSONL to Parquet via `pandas` / `pyarrow`.

Format decision: use JSONL in Phase 1 for parity with Script 01, streaming writes, and
human-auditable output. Parquet migration is a Phase 2 concern.

## 3. Input Contract

Source: `data/raw_inventory.jsonl` (Script 01 output).

A record is processed only when all of the following hold:

- `pdf_readable` is true
- `is_encrypted` is false
- `page_count >= 1`
- `health_flags.is_zero_page` is false
- `health_flags.is_malformed` is false
- `processing_status` is not `error`

Otherwise the record is skipped and logged as skipped.

Consumed fields: `pdf_path`, `piece_id`, `piece_folder`, `pdf_filename`, `page_count`,
`pdf_readable`, `is_encrypted`, `health_flags`, `processing_status`, `file_fingerprint`.

The library root is not stored in the inventory (paths are relative), so Script 02 accepts a
`--library-root` option and resolves each `pdf_path` against it.

## 4. Output Contracts

### 4.1 `data/extracted_text.jsonl` (one record per page)

Fields:

- `record_version`
- `run_id`
- `pdf_path`
- `piece_id`
- `page_num` (1-indexed)
- `page_index_zero` (0-indexed)
- `embedded_text` (nullable)
- `embedded_text_length`
- `extraction_method` (`pymupdf_embedded`)
- `text_is_searchable`
- `needs_ocr`
- `ocr_text` (nullable; Phase 2)
- `ocr_confidence` (nullable; Phase 2)
- `processing_timestamp`
- `processing_status` (`success` or `error`)
- `error_message` (nullable)

`text_is_searchable` is true when `embedded_text.strip()` is non-empty and contains at least one
alphanumeric character. `needs_ocr` is the negation of `text_is_searchable`.

### 4.2 `data/pages.jsonl` (one record per page)

Fields:

- `record_version`
- `run_id`
- `pdf_path`
- `piece_id`
- `page_num`
- `page_index_zero`
- `thumbnail_path` (nullable, relative)
- `thumbnail_hash` (nullable, sha256 of PNG bytes)
- `render_dpi`
- `render_width_px`
- `render_height_px`
- `render_file_size_bytes`
- `text_density` (nullable float 0..1)
- `black_white_ratio` (nullable float 0..1)
- `render_timestamp`
- `processing_status` (`success` or `error`)
- `error_message` (nullable)

Both outputs are sorted by `(pdf_path, page_num)` for deterministic diffs.

### 4.3 Checkpoint `data/.extraction_checkpoint.json`

Fields: `record_version`, `last_run_id`, `last_run_timestamp`, `inventory_input`,
`extracted_text_output`, `pages_output`, `library_root`, `fingerprints`
(map of `pdf_path` to `file_fingerprint`), `pdf_count_processed`, `page_count_processed`,
`reused_pdf_count`.

## 5. Feature Computation (pymupdf only)

Render each page with `page.get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72))`. Convert to grayscale
samples and compute:

- `text_density`: fraction of pixels with grayscale value `< 192`.
- `black_white_ratio`: fraction of pixels equal to `0` or `255`.

To bound memory/CPU on huge pages, features are computed from the grayscale pixmap bytes directly.

## 6. Caching Strategy

Render cache directory: `cache/render/`.

Cache key per page:

```
content_hash = sha256(f"{pdf_path}|{file_fingerprint}|{page_num}")
filename = f"{piece_id}_p{page_num:04d}_{content_hash_prefix12}.png"
```

If the cache file already exists (matching key), reuse it instead of re-rendering. Because the
key includes `file_fingerprint`, a changed PDF invalidates its cached renders automatically.

## 7. Idempotency & Incremental Mode

Mirror Script 01:

- `--mode full`: reprocess every readable PDF and rewrite outputs.
- `--mode incremental`: for each PDF, if the previous checkpoint fingerprint matches the current
  inventory fingerprint, reuse the prior per-page records from the existing output files; only
  changed/new PDFs are reprocessed. Orphaned PDFs (in prior output but no longer in inventory) are
  logged.

Outputs are rebuilt in memory, sorted, and written atomically via `atomic_write_jsonl`. The
checkpoint is written atomically after a successful output write.

## 8. CLI Design

| Option | Type | Default | Purpose |
|--------|------|---------|---------|
| `--inventory` | Path | `data/raw_inventory.jsonl` | Script 01 output to consume |
| `--library-root` | Path | required | Root used to resolve relative `pdf_path` |
| `--output-text` | Path | `data/extracted_text.jsonl` | Per-page text output |
| `--output-pages` | Path | `data/pages.jsonl` | Per-page feature output |
| `--cache-dir` | Path | `cache` | Base cache directory |
| `--mode` | str | `full` | `full` or `incremental` |
| `--render-dpi` | int | `150` | Thumbnail render DPI |
| `--enable-rendering / --no-rendering` | flag | enabled | Toggle rendering + features |
| `--log-level` | str | `INFO` | Logging level |

OCR options are intentionally omitted in Phase 1.

## 9. Failure Policy

- Per-PDF open failure: skip the PDF, log an error, continue.
- Per-page failure: emit an error record for that page (text and/or pages output), log a warning,
  continue with the next page.
- Render failure on an otherwise-good page: still emit the text record; emit a pages record with
  null render fields and `processing_status="error"`.
- A single bad page or PDF never aborts the run.

## 10. Validation Plan

1. `--help` renders.
2. Full mode over a fixture library produces both JSONL outputs and a checkpoint.
3. Re-running full mode is deterministic (identical output bytes).
4. Incremental mode reuses unchanged PDFs (records identical, reused count > 0).
5. Unreadable/encrypted/zero-page inventory records are skipped.
6. Automated tests cover text extraction, feature calc, full/incremental parity, and skipping.

## 11. Acceptance Criteria

- Script 02 runs as a CLI and writes valid `extracted_text.jsonl` and `pages.jsonl`.
- Full and incremental modes both implemented and deterministic.
- Rendering cache is populated and reused across runs.
- Per-page and per-PDF failures are captured, not fatal.
- Tests pass and the implementation notes below are updated.

## 12. Implementation Notes Log

- Plan authored: 2026-07-26.
- Implementation completed: 2026-07-26.
  - Implemented `scripts/02_extract_text_and_images.py` (Phase 1, pymupdf only).
  - Per-page embedded text extraction with `text_is_searchable` / `needs_ocr` flags.
  - Page rendering to `cache/render/` with content-hash cache keys and reuse.
  - Features `text_density` and `black_white_ratio` computed from a grayscale pixmap.
  - Full and incremental modes with checkpoint at `data/.extraction_checkpoint.json`.
  - Per-page and per-PDF failures captured as records/logs without aborting the run.
- Validation completed: 2026-07-26.
  - Added `tests/test_extraction.py`; full suite passes (5 tests).
  - Smoke run on a 2-page fixture: page 1 text extracted, blank page 2 flagged `needs_ocr`,
    both thumbnails rendered; incremental rerun reused the PDF (reused=1, processed=0).
  - Added `.gitignore` rules for generated pipeline artifacts and cache renders.
- Deferred (Phase 2): OCR via pytesseract (+ Tesseract system binary on Windows),
  opencv-based blur/skew/contrast metrics, and optional Parquet migration.
