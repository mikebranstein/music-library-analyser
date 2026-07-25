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

## 2. Scope

This implementation produces rich, page- and document-level signals for downstream stages
(Scripts 03-05, 07-08). It uses `pymupdf` for extraction/rendering and `numpy` + `opencv-python`
for image-quality metrics. Heavy image metrics degrade gracefully to `null` when those optional
libraries are unavailable, so the core extraction path still runs with `pymupdf` alone.

In scope (enhancement items 1-8):

1. Document-level rollup output (`data/documents.jsonl`).
2. Prominent/header text extraction via `page.get_text("dict")` (feeds part/title detection).
3. Born-digital vs scanned classification (`is_image_based`, image coverage).
4. Page geometry signals (points size, rotation, orientation, aspect ratio).
5. Text-quality counts (`word_count`, `alnum_ratio`).
6. Per-page normalized text hash (`page_text_hash`) for dedupe.
7. Estimated scan DPI from embedded images (pymupdf only).
8. Image-quality metrics: blur (Laplacian variance), skew angle, contrast (numpy/opencv).

Deferred to a later phase:

- OCR via `pytesseract` (requires the Tesseract system binary; on Windows it is not added to
  PATH automatically).
- Migration of outputs from JSONL to Parquet via `pandas` / `pyarrow`.

Format decision: use JSONL for parity with Script 01, streaming writes, and human-auditable
output. Parquet migration is a later concern.

### 2.1 Dependency and Degradation Policy

- `numpy` present: vectorized `text_density`/`black_white_ratio`, plus `contrast_std` and
  `blur_variance`.
- `numpy` absent: pure-python `text_density`/`black_white_ratio`; `contrast_std`/`blur_variance`
  are `null`.
- `opencv-python` present: `skew_angle_deg` computed; absent: `skew_angle_deg` is `null`.
- `--no-image-metrics` disables blur/skew/contrast regardless of library availability.
- Items 2-7 depend on `pymupdf` only and are always computed when the page loads.

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
- `ocr_text` (nullable; future OCR phase)
- `ocr_confidence` (nullable; future OCR phase)
- `word_count` (item 5)
- `alnum_ratio` (item 5; alnum chars / non-space chars)
- `page_text_hash` (item 6; sha256 of whitespace-normalized lowercase text)
- `header_text_candidates` (item 2; prominent largest-font strings)
- `top_lines` (item 2; top-of-page text lines in reading order)
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
- `page_width_pt` (item 4)
- `page_height_pt` (item 4)
- `rotation` (item 4; degrees)
- `orientation` (item 4; `portrait` or `landscape`)
- `aspect_ratio` (item 4; nullable)
- `image_count` (item 3)
- `largest_image_coverage` (item 3; 0..1)
- `is_image_based` (item 3; scanned-page heuristic)
- `estimated_dpi` (item 7; nullable)
- `contrast_std` (item 8; nullable, numpy)
- `blur_variance` (item 8; nullable, numpy Laplacian variance)
- `skew_angle_deg` (item 8; nullable, opencv)
- `render_timestamp`
- `processing_status` (`success` or `error`)
- `error_message` (nullable)

### 4.3 `data/documents.jsonl` (item 1; one record per PDF)

Fields:

- `record_version`, `run_id`, `pdf_path`, `piece_id`, `piece_folder`, `pdf_filename`
- `page_count` (pages processed)
- `pages_with_text`
- `pages_needing_ocr`
- `ocr_fraction`
- `total_text_length`
- `total_word_count`
- `pages_image_based`
- `image_based_fraction`
- `first_page_text` (nullable)
- `first_page_header_candidates` (list)
- `processing_status` (`success` or `partial_error`)
- `processing_timestamp`

All three outputs are sorted deterministically: page/text outputs by `(pdf_path, page_num)` and
the document output by `pdf_path`.

### 4.4 Checkpoint `data/.extraction_checkpoint.json`

Fields: `record_version`, `last_run_id`, `last_run_timestamp`, `inventory_input`,
`extracted_text_output`, `pages_output`, `documents_output`, `library_root`, `fingerprints`
(map of `pdf_path` to `file_fingerprint`), `pdf_count_processed`, `page_count_processed`,
`reused_pdf_count`. The schema version is bumped to `2.0` for the enhanced record shape.

## 5. Feature Computation

### 5.1 Pixel metrics (from the grayscale render)

Render each page with `page.get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72))`, convert to grayscale,
and compute:

- `text_density`: fraction of pixels with grayscale value `< 192`.
- `black_white_ratio`: fraction of pixels equal to `0` or `255`.
- `contrast_std` (item 8): standard deviation of grayscale values (numpy).
- `blur_variance` (item 8): variance of a discrete Laplacian of the grayscale image (numpy). Low
  values indicate blur.
- `skew_angle_deg` (item 8): estimated deskew angle via Otsu threshold + `cv2.minAreaRect` over
  dark pixels (opencv).

When `numpy` is available, pixel metrics are vectorized; otherwise a pure-python fallback computes
`text_density`/`black_white_ratio` and leaves the advanced metrics `null`.

### 5.2 Page-structure metrics (from pymupdf, item 2/3/4/7)

- Header/title candidates via `page.get_text("dict")`: collect spans, take the largest-font
  strings (`header_text_candidates`) and top-of-page lines (`top_lines`).
- Geometry via `page.rect` and `page.rotation`: `page_width_pt`, `page_height_pt`, `rotation`,
  `orientation`, `aspect_ratio`.
- Image analysis via `page.get_images(full=True)` + `page.get_image_rects`: `image_count`,
  `largest_image_coverage`, `is_image_based`, and `estimated_dpi` from the largest image's pixel
  width divided by its placed width in inches.

`is_image_based` heuristic: `image_count > 0` and `largest_image_coverage >= 0.6` and page text
length `< 30` characters.

### 5.3 Text-quality signals (item 5/6)

- `word_count`: whitespace-split token count.
- `alnum_ratio`: alphanumeric chars divided by non-space chars.
- `page_text_hash`: sha256 of the whitespace-normalized, lowercased page text.

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
| `--output-documents` | Path | `data/documents.jsonl` | Per-document rollup output |
| `--cache-dir` | Path | `cache` | Base cache directory |
| `--mode` | str | `full` | `full` or `incremental` |
| `--render-dpi` | int | `150` | Thumbnail render DPI |
| `--enable-rendering / --no-rendering` | flag | enabled | Toggle rendering + pixel features |
| `--enable-image-metrics / --no-image-metrics` | flag | enabled | Toggle blur/skew/contrast (item 8) |
| `--log-level` | str | `INFO` | Logging level |

OCR options are intentionally omitted for now. If `--enable-image-metrics` is set but `numpy` is
unavailable, the flag is auto-disabled with a warning.

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

- Script 02 runs as a CLI and writes valid `extracted_text.jsonl`, `pages.jsonl`, and
  `documents.jsonl`.
- Full and incremental modes both implemented and deterministic (all three outputs reused).
- Rendering cache is populated and reused across runs.
- Per-page and per-PDF failures are captured, not fatal.
- Image metrics degrade gracefully when `numpy`/`opencv-python` are absent.
- Tests pass and the implementation notes below are updated.

## 12. Gap Analysis (pre-enhancement audit)

The following gaps were identified between the originally shipped Script 02 and the enhanced plan
(items 1-8), and were closed in this pass:

| # | Gap in shipped Script 02 | Item | Resolution |
|---|--------------------------|------|------------|
| G1 | No document-level output | 1 | Added `data/documents.jsonl` rollup with reuse |
| G2 | No prominent/header text | 2 | Added `header_text_candidates` + `top_lines` via text dict |
| G3 | No scanned/born-digital signal | 3 | Added `is_image_based`, `image_count`, `largest_image_coverage` |
| G4 | No page geometry | 4 | Added points size, `rotation`, `orientation`, `aspect_ratio` |
| G5 | No text-quality counts | 5 | Added `word_count`, `alnum_ratio` |
| G6 | No dedupe signal | 6 | Added `page_text_hash` |
| G7 | No scan DPI estimate | 7 | Added `estimated_dpi` from embedded image placement |
| G8 | No quality metrics | 8 | Added `blur_variance`, `skew_angle_deg`, `contrast_std` |
| G9 | Pure-python pixel loop (slow) | 8 | Vectorized with numpy; pure-python fallback retained |
| G10 | Incremental reuse ignored docs | 1 | Document records reused alongside text/pages |
| G11 | No dependency guards | 8 | Optional `numpy`/`cv2` imports + `--no-image-metrics` |

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
- Deferred: OCR via pytesseract (+ Tesseract system binary on Windows) and optional Parquet
  migration.
- Enhancement pass (items 1-8) integrated: 2026-07-26.
  - Added `data/documents.jsonl` rollup, header/title candidates, scanned/born-digital signals,
    page geometry, text-quality counts, per-page text hash, estimated DPI, and image-quality
    metrics (blur/skew/contrast).
  - Added `numpy` + `opencv-python` dependencies with graceful degradation and a
    `--no-image-metrics` toggle; bumped record schema to `2.0`.
  - See Section 12 for the gap analysis that drove this pass.
