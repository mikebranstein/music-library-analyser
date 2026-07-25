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

Second enhancement wave (items 1, 2, 4, 5 of the follow-up audit; RECORD_VERSION bumped to `2.1`):

1. Zone/corner text extraction: bucket first-class spans into `zone_top_left`, `zone_top_center`,
   `zone_top_right`, and `zone_bottom` using span bounding boxes (feeds part-name/composer/
   copyright detection downstream).
2. Copyright/identity extraction: regex over page text for `©`/`Copyright`/`(c)`, a 4-digit
   year, `Arr.`/`arranged by`, and `by`/`music by`/`composed by`, emitted as an
   `identity_candidates` object on the document rollup.
4. Blank/near-blank page flag: an `is_blank` boolean per page derived from the existing
   ink-coverage measure (`text_density`) plus embedded-text emptiness, rolled up to
   `blank_page_count`.
5. Staff (music notation) detection: horizontal-line projection over the grayscale render yields
   `has_staves` and `staff_line_count` per page, rolled up to `music_page_count`.

(Item 3 — fully structured title record — was intentionally not scoped in this wave.)

Third enhancement wave (OCR/OSD via Tesseract; RECORD_VERSION bumped to `2.2`):

- OCR of scanned/no-text pages with `pytesseract` + the Tesseract binary. Pages where
  `needs_ocr` is true **or** `is_image_based` is true are rendered at a dedicated OCR DPI
  (default 300) and OCR'd; results populate `ocr_text`, `ocr_confidence`, `ocr_word_count`,
  `ocr_applied`, `ocr_status`, `ocr_engine`, and a convenience `text_source`
  (`embedded`/`ocr`/`none`).
- OSD (orientation/script detection) runs before OCR, auto-rotating sideways/upside-down scans
  and recording `osd_rotation`, `osd_orientation_conf`, and `osd_script` on the page record.
- OCR results are cached per page under `cache/ocr/` (keyed by content + DPI + language +
  engine version) so reruns and re-processing skip live OCR calls.
- OCR is on by default (`--ocr/--no-ocr`) and degrades gracefully: when `pytesseract`/`Pillow`
  or the Tesseract binary are missing, OCR is disabled with a warning and pages stay flagged
  `needs_ocr` with null OCR fields.

Deferred to a later phase:

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
- `pytesseract` + `Pillow` present **and** the Tesseract binary resolvable (via `--tesseract-cmd`,
  `PATH`, or common install dirs such as `C:\Program Files\Tesseract-OCR`): OCR/OSD run on
  eligible pages. If any of these are missing, or `--no-ocr` is passed, OCR is disabled and
  `ocr_*`/`osd_*` fields are `null` (pages remain `needs_ocr`). OCR never aborts the run.
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
- `ocr_text` (nullable; populated when OCR ran and recovered text)
- `ocr_confidence` (nullable float 0..1; mean per-word Tesseract confidence / 100)
- `ocr_word_count` (wave-3; nullable int)
- `ocr_applied` (wave-3; bool; whether OCR was attempted on this page)
- `ocr_status` (wave-3; `not_applied`/`success`/`unavailable`/`render failed: ...`/`ocr failed: ...`)
- `ocr_engine` (wave-3; `tesseract` when applied, else null)
- `text_source` (wave-3; `embedded`/`ocr`/`none` — which text a downstream stage should prefer)
- `word_count` (item 5)
- `alnum_ratio` (item 5; alnum chars / non-space chars)
- `page_text_hash` (item 6; sha256 of whitespace-normalized lowercase text)
- `header_text_candidates` (item 2; prominent largest-font strings)
- `top_lines` (item 2; top-of-page text lines in reading order)
- `zone_top_left` (wave-2 item 1; nullable; text in the top-left region)
- `zone_top_center` (wave-2 item 1; nullable; text in the top-center region)
- `zone_top_right` (wave-2 item 1; nullable; text in the top-right region)
- `zone_bottom` (wave-2 item 1; nullable; text in the bottom region)
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
- `is_blank` (wave-2 item 4; nullable bool; low ink coverage + no embedded text)
- `has_staves` (wave-2 item 5; nullable bool; music-notation staff lines detected)
- `staff_line_count` (wave-2 item 5; nullable int; long horizontal lines detected)
- `osd_rotation` (wave-3; nullable int; OSD-detected rotation in degrees, 0/90/180/270)
- `osd_orientation_conf` (wave-3; nullable float; Tesseract OSD orientation confidence)
- `osd_script` (wave-3; nullable string; OSD-detected script, e.g. `Latin`)
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
- `blank_page_count` (wave-2 item 4; count of pages with `is_blank` true)
- `music_page_count` (wave-2 item 5; count of pages with `has_staves` true)
- `identity_candidates` (wave-2 item 2; object with `publisher`, `copyright_year`,
  `arranger`, `composer`, `copyright_line`; each field nullable)
- `pages_ocr_applied` (wave-3; pages OCR was attempted on)
- `pages_ocr_recovered` (wave-3; pages where OCR produced non-empty text)
- `ocr_char_count` (wave-3; total characters of recovered OCR text)
- `pages_rotated` (wave-3; pages OSD auto-rotated before OCR)
- `processing_status` (`success` or `partial_error`)
- `processing_timestamp`

All three outputs are sorted deterministically: page/text outputs by `(pdf_path, page_num)` and
the document output by `pdf_path`.

### 4.4 Checkpoint `data/.extraction_checkpoint.json`

Fields: `record_version`, `last_run_id`, `last_run_timestamp`, `inventory_input`,
`extracted_text_output`, `pages_output`, `documents_output`, `library_root`, `fingerprints`
(map of `pdf_path` to `file_fingerprint`), `pdf_count_processed`, `page_count_processed`,
`reused_pdf_count`, `ocr_enabled`, `ocr_engine_version`. The schema version is bumped to `2.2`
for the OCR/OSD record shape; the bump invalidates prior 2.0/2.1 checkpoints so all PDFs are
reprocessed once to populate the new fields (OCR results are still served from `cache/ocr/`).

### 4.5 Report `data/extraction_report.md`

A human-readable Markdown summary written after the JSONL outputs (skippable via
`--no-report`). It is derived entirely from the in-memory records, so it never diverges from
the data files. Sections: an anchor-linked table of contents, At-a-Glance metrics, Text
Coverage, Document Types (born-digital vs scanned), Image Quality, Attention Needed
(errors / needs-OCR / blurry / low-contrast / skewed), a Per-Folder breakdown, a collapsible
Per-Document detail table (capped by `--report-detail-limit`), and a Configuration and
Environment section. Emoji status markers (✅ 🔍 🖼️ ❌) stand in for color.

Quality caveat: blur/skew/contrast/DPI flags are computed only over scanned (image-based)
pages. Born-digital pages are vector-rendered and crisp by construction, and sheet music is
mostly white space, so absolute thresholds there would produce false positives.

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

### 5.4 Zone/corner text (wave-2 item 1)

Using the same `page.get_text("dict")` spans, each span's bounding-box center is bucketed by page
fraction: the top band is `y_center <= 0.22 * page_height`, the bottom band is
`y_center >= 0.82 * page_height`. Within the top band, `x_center < 0.38 * W` is left,
`x_center > 0.62 * W` is right, otherwise center. Spans in each bucket are joined in reading order
into `zone_top_left` / `zone_top_center` / `zone_top_right` / `zone_bottom` (nullable when empty).

### 5.5 Copyright/identity candidates (wave-2 item 2)

`extract_identity_candidates(page_texts)` scans page text (page 1 first, then later pages for a
copyright line if page 1 lacks one) for:

- `copyright_line`: first line containing `©`, `(c)`, or `copyright` (case-insensitive).
- `copyright_year`: first 4-digit `19xx`/`20xx` in the copyright line (or page-1 text).
- `publisher`: best-effort remainder of the copyright line after the year, trimmed of boilerplate
  such as `all rights reserved`.
- `arranger`: text after `arr.`/`arranged by`/`arr by`.
- `composer`: text after `by`/`music by`/`composed by` on the first page.

All fields are nullable and clearly best-effort (heuristic, for downstream ranking, not ground
truth).

### 5.6 Blank flag and ink coverage (wave-2 item 4)

`text_density` (fraction of pixels `< 192`) already serves as the normalized ink-coverage measure.
A page is `is_blank` when the render succeeded with `text_density < 0.004` and the stripped
embedded text is empty and the page is not image-based. When rendering is disabled, `is_blank`
falls back to embedded-text emptiness only; when the render errored, `is_blank` is `null`.

### 5.7 Staff (music-notation) detection (wave-2 item 5)

`detect_staves(gray_bytes, width, height)` (numpy required) computes, per image row, the fraction
of dark pixels (`< 160`). Rows whose dark fraction exceeds `0.40` are candidate staff lines; runs
of consecutive candidate rows are merged into single lines. `staff_line_count` is the number of
merged long horizontal lines and `has_staves` is `staff_line_count >= 5` (at least one 5-line
staff). Both are `null` when numpy is unavailable, rendering is disabled, or the render errored.
The measure works on both born-digital and scanned music because it operates on the render.

### 5.8 OCR and OSD (wave-3, Tesseract)

Run only when OCR is enabled and the page qualifies (`needs_ocr` **or** `is_image_based`).
`run_ocr(page, cfg)`:

1. Renders the page to a fresh pixmap at the OCR DPI (default 300, independent of the 150-DPI
   thumbnail render) and opens it as a `PIL.Image`.
2. OSD: `pytesseract.image_to_osd` yields `osd_rotation`, `osd_orientation_conf`, `osd_script`;
   when the rotation is 90/180/270 the image is rotated upright before OCR. OSD failures (common
   on sparse/low-content pages) are tolerated and leave the page unrotated.
3. OCR: `pytesseract.image_to_data` yields per-word text and confidence. Non-empty tokens are
   joined into `ocr_text`; `ocr_word_count` is the token count; `ocr_confidence` is the mean of
   non-negative word confidences divided by 100 (0..1). `text_source` becomes `embedded` when the
   page already had searchable text, else `ocr` when OCR recovered text, else `none`.

The Tesseract binary is located via `--tesseract-cmd`, then `PATH`, then common install
directories. If `pytesseract`/`Pillow` or the binary are unavailable, OCR is disabled for the run
with a warning; `run_ocr` itself never raises.

## 6. Caching Strategy

Render cache directory: `cache/render/`.

Cache key per page:

```
content_hash = sha256(f"{pdf_path}|{file_fingerprint}|{page_num}")
filename = f"{piece_id}_p{page_num:04d}_{content_hash_prefix12}.png"
```

If the cache file already exists (matching key), reuse it instead of re-rendering. Because the
key includes `file_fingerprint`, a changed PDF invalidates its cached renders automatically.

OCR cache directory: `cache/ocr/`.

```
key = sha256(f"{pdf_path}|{file_fingerprint}|{page_num}|{ocr_dpi}|{ocr_lang}|{engine_version}")
filename = f"{piece_id}_p{page_num:04d}_{key_prefix12}.json"
```

A per-page OCR result (text, confidence, word count, OSD fields) is written as JSON on first
compute and reused on subsequent runs, so schema bumps or re-renders do not trigger a fresh
(expensive) OCR call. The key includes DPI, language, and Tesseract version, so changing any of
them produces a new cache entry. Only successful OCR results are cached; transient states
(`unavailable`/errors) are retried next run.

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
| `--output-report` | Path | `data/extraction_report.md` | Human-readable Markdown summary |
| `--report / --no-report` | flag | enabled | Toggle the Markdown summary report |
| `--report-detail-limit` | int | `200` | Max rows in the per-document detail table |
| `--cache-dir` | Path | `cache` | Base cache directory |
| `--mode` | str | `full` | `full` or `incremental` |
| `--render-dpi` | int | `150` | Thumbnail render DPI |
| `--enable-rendering / --no-rendering` | flag | enabled | Toggle rendering + pixel features |
| `--enable-image-metrics / --no-image-metrics` | flag | enabled | Toggle blur/skew/contrast (item 8) |
| `--ocr / --no-ocr` | flag | enabled | Toggle OCR/OSD on eligible pages (wave-3) |
| `--ocr-dpi` | int | `300` | Dedicated OCR render DPI (validated 72..1200) |
| `--ocr-lang` | str | `eng` | Tesseract language(s) passed to OCR/OSD |
| `--tesseract-cmd` | str | `""` | Explicit path to the Tesseract binary (else auto-detect) |
| `--log-level` | str | `INFO` | Logging level |

If `--enable-image-metrics` is set but `numpy` is unavailable, the flag is auto-disabled with a
warning. Likewise, if `--ocr` is set but `pytesseract`/`Pillow` or the Tesseract binary cannot be
resolved (or the binary is not runnable), OCR is auto-disabled with a warning and the run
continues with null `ocr_*`/`osd_*` fields.

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

### 12.1 Second-wave gap analysis (follow-up audit)

These gaps were identified against the shipped RECORD_VERSION 2.0 and closed by bumping to `2.1`:

| # | Gap in RECORD_VERSION 2.0 | Item | Resolution |
|---|---------------------------|------|------------|
| G12 | No zone/corner text (x-coords discarded) | 1 | Added `zone_top_left/_center/_right` + `zone_bottom` from span bboxes |
| G13 | No copyright/identity parsing | 2 | Added `identity_candidates` (publisher/year/arranger/composer/line) |
| G14 | No blank-page flag or blank rollup | 4 | Added `is_blank` per page + `blank_page_count` (ink coverage = `text_density`) |
| G15 | No music-notation signal | 5 | Added `has_staves`/`staff_line_count` per page + `music_page_count` |

### 12.2 Third-wave gap analysis (OCR/OSD audit)

Audit of RECORD_VERSION 2.1 against the newly available Tesseract binary. The `ocr_text` /
`ocr_confidence` placeholders and the `needs_ocr` flag existed, but no code ever populated them.
Closed by bumping to `2.2`:

| # | Gap in RECORD_VERSION 2.1 | Resolution |
|---|---------------------------|------------|
| G16 | `pytesseract` never imported; OCR placeholders always null even for scanned pages | Optional `pytesseract`/`Pillow` imports + `run_ocr` populate `ocr_text`/`ocr_confidence`/`ocr_word_count`/`ocr_status`/`text_source` |
| G17 | No orientation handling; sideways/upside-down scans OCR poorly | Added OSD (`image_to_osd`) with auto-rotate + `osd_rotation`/`osd_orientation_conf`/`osd_script` on pages |
| G18 | No OCR trigger logic or CLI surface | `_should_ocr` (needs_ocr OR image-based) gated by `--ocr/--no-ocr`, `--ocr-dpi`, `--ocr-lang`, `--tesseract-cmd`; binary auto-resolved |
| G19 | No OCR caching or rollups; every run would re-OCR | Per-page JSON cache under `cache/ocr/` + document rollups `pages_ocr_applied`/`pages_ocr_recovered`/`ocr_char_count`/`pages_rotated` |

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
- Second enhancement wave (items 1, 2, 4, 5) integrated:
  - Item 1: `extract_header_candidates` replaced by `extract_text_structure`, which additionally
    buckets span bounding boxes into `zone_top_left/_center/_right` and `zone_bottom`.
  - Item 2: `extract_identity_candidates` parses publisher/year/arranger/composer/copyright-line
    from page text (page 1 first) into `identity_candidates` on `documents.jsonl`.
  - Item 4: `_compute_is_blank` adds a per-page `is_blank` flag (ink coverage = `text_density`),
    rolled up to `blank_page_count`.
  - Item 5: `detect_staves` adds per-page `has_staves`/`staff_line_count` via row-projection over
    the grayscale render, rolled up to `music_page_count` (numpy required; degrades to `null`).
  - Report surfaces blank pages, music pages, and identity-candidate counts.
  - Schema bumped to `2.1` (invalidates prior 2.0 checkpoints). See Section 12.1 (G12-G15).
  - Validation: `pytest -q` => `13 passed`; `ruff check` reports only the pre-accepted codes
    (N999, BLE001, B008, SIM103, UP017). Added tests: `test_extract_identity_candidates`,
    `test_detect_staves_projection`, `test_detect_staves_degrades_without_shape`,
    `test_zone_blank_and_staff_full_run`.
- Third enhancement wave (OCR/OSD via Tesseract) integrated:
  - Added optional `pytesseract` + `Pillow` imports and `pytesseract>=0.3.10` / `Pillow>=10.0.0`
    dependencies; installed into `.venv`. Verified Tesseract v5.5.3 at
    `C:\Program Files\Tesseract-OCR\tesseract.exe` (not on PATH) via `resolve_tesseract_cmd`.
  - `run_ocr` renders eligible pages at `--ocr-dpi` (default 300, separate from the 150-DPI
    thumbnail), runs OSD (auto-rotate + `osd_*` fields) then `image_to_data` OCR; populates
    `ocr_text`/`ocr_confidence`/`ocr_word_count`/`ocr_applied`/`ocr_status`/`ocr_engine`/
    `text_source`. It never raises.
  - `_should_ocr` triggers on `needs_ocr` OR `is_image_based`; gated by `--ocr/--no-ocr`
    (default on) with `--ocr-lang` and `--tesseract-cmd`. OCR auto-disables with a warning when
    the toolchain is missing.
  - Per-page OCR results cached under `cache/ocr/` (keyed by content + DPI + lang + engine
    version); document rollups `pages_ocr_applied`/`pages_ocr_recovered`/`ocr_char_count`/
    `pages_rotated`; checkpoint gains `ocr_enabled`/`ocr_engine_version`. Report surfaces OCR'd,
    recovered, and auto-rotated page counts plus an OCR configuration block.
  - Schema bumped to `2.2` (invalidates prior 2.1 checkpoints). See Section 12.2 (G16-G19).
  - Validation: `pytest -q` => `16 passed`; `ruff check` reports only the pre-accepted codes
    (N999, BLE001, B008, SIM103, UP017). Added tests: `test_resolve_tesseract_cmd_explicit`,
    `test_run_ocr_reads_text`, `test_ocr_full_run_recovers_scanned_text`.
- Deferred (still open): Parquet output migration via `pandas`/`pyarrow`.
