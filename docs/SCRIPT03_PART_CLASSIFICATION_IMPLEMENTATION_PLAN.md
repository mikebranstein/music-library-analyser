# Script 03 Part Classification Implementation Plan

## 1. Objective

Implement Script 03 (`scripts/03_part_classifier.py`): a deterministic, incremental,
document-level classification pass that consumes the Script 01 inventory and the Script 02
extraction datasets and predicts, for every PDF, **what instrument/part it is** and **whether it
is a score**. The result is one prediction record per PDF in `data/part_predictions.jsonl`, plus a
human-readable Markdown report.

This plan is derived from:

- `README.md`
- `docs/BAND_COLLECTION_ANALYSIS_PLAN.md` (Section 4.3)
- `docs/SCRIPT02_EXTRACTION_IMPLEMENTATION_PLAN.md` (upstream output contract)
- `scripts/01_inventory.py` + `scripts/02_extract_text_and_images.py` (input contracts)
- `scripts/_common.py` (shared helpers)
- Direct inspection of the real library outputs (`data/documents.jsonl`, `data/pages.jsonl`,
  `data/extracted_text.jsonl`).

## 2. Research: what the data actually looks like

The current library is a **brass-band** collection. Inspecting `data/documents.jsonl`
(26 documents, 2 pieces) shows a highly regular filename convention:

```
<catalog#> <PieceName>{ - | _}<Part>[ (<Clef>)].pdf
```

Concrete observed filenames:

- `241 Chick Corea Ole - Baritone (BC).pdf`      (space-dash-space separator, bass-clef baritone)
- `241 Chick Corea Ole - Baritone (TC).pdf`      (treble-clef baritone)
- `241 Chick Corea Ole - Basses - Tuba.pdf`      (part name itself contains a dash)
- `241 Chick Corea Ole - Cornet 1/2/3.pdf`       (numbered parts)
- `241 Chick Corea Ole - Horn 1 in F.pdf`        (number + transposition)
- `681 A Night On A Lonely Moor_Horn in F 1.pdf` (underscore separator, transposition + number)
- `681 A Night On A Lonely Moor_Trombone 3 (Bass).pdf` (bass-clef qualifier)

### 2.1 Findings that drive the design

1. **The filename is the strongest, cleanest signal.** For engraved/scanned band parts the
   embedded/OCR page text is musically noisy (`", CHICK COREA OLE ... 2= 1 t ¥ J~ J J"`) and a poor
   primary source. The filename is authoritative in this collection.
2. **The part segment must be isolated by stripping the `piece_folder` prefix**, not by naively
   splitting on the last separator. `241 Chick Corea Ole - Basses - Tuba.pdf` contains a dash
   inside the part; stripping the known `piece_folder` (`241 Chick Corea Ole`) leaves the true
   part segment `Basses - Tuba`.
3. **Both `-` and `_` are used as the title/part separator**, sometimes within the same library.
4. **Clef and transposition are encoded in parentheses / trailing tokens**: `(BC)` = bass clef,
   `(TC)` = treble clef, `(Bass)` = bass clef, `in F`/`in Bb`/`in Eb` = transposition.
5. **Part numbers are trailing or embedded** (`Cornet 2`, `Horn 1 in F`, `Horn in F 1`).
6. **Longest-alias-wins matching is required** so `Bass Trombone`, `Bass Clarinet`, and
   `Alto Saxophone` are not shadowed by `Trombone`/`Clarinet`/`Saxophone`.
7. **Page text is a good confirmation signal, not a primary one.** The part name frequently appears
   near the top of page 1 (`first_page_text` / `zone_top_left` / `zone_top_center`), so it is used
   to *boost* confidence and to *recover* a label when the filename is ambiguous.

## 3. Scope

In scope:

1. Rule-first classification driven by an instrument lexicon in `config/regex_rules.yaml`
   (with a built-in fallback lexicon so the script runs even if the YAML file or PyYAML is
   missing).
2. Filename parsing: `piece_folder`-aware part-segment isolation, instrument match (longest alias
   wins), clef, transposition, part index, and score detection.
3. Text confirmation from Script 02 outputs (`first_page_text`, `first_page_header_candidates`,
   page-1 `zone_top_*`) to boost/recover predictions.
4. Deterministic confidence scoring and an explicit `evidence_source`.
5. Per-piece ensemble harmonization: flag duplicate labels within a piece.
6. Full and incremental modes with a checkpoint, mirroring Scripts 01/02.
7. A Markdown summary report.

Out of scope (deferred / later):

- Live LLM classification. A clean, disabled-by-default `--use-llm` hook is provided, but no
  network provider is wired in this offline repo; enabling it without a provider logs a warning
  and changes nothing (no fabricated predictions).
- Vision/thumbnail model checks.
- Parquet output migration.

## 4. Dependency and Degradation Policy

- `PyYAML` present: `config/regex_rules.yaml` is loaded and merged over the built-in lexicon.
- `PyYAML` absent, or the file missing/empty/unparseable: the built-in lexicon is used and a
  debug/warning line is logged. Classification still runs.
- `--use-llm` set but no provider wired: warn and skip; rule-based results are emitted unchanged.

## 5. Input Contract

Inputs (all optional except the inventory; missing datasets degrade gracefully):

- `data/raw_inventory.jsonl` (Script 01): `pdf_path`, `pdf_filename`, `piece_id`, `piece_folder`,
  `file_fingerprint`, `pdf_readable`. Only `pdf_readable == true` records are classified.
- `data/documents.jsonl` (Script 02): `first_page_text`, `first_page_header_candidates`,
  `page_count`, `pages_image_based`, `identity_candidates`.
- `data/pages.jsonl` (Script 02): page-1 `zone_top_left`/`zone_top_center`/`zone_top_right` used
  for text confirmation.

Records are joined by `pdf_path` (canonical key). When Script 02 outputs are absent the classifier
runs filename-only.

## 6. Instrument Lexicon (`config/regex_rules.yaml`)

The lexicon is the source of the classification vocabulary. Top-level keys:

- `families`: canonical instrument key -> family (`woodwind`/`brass`/`percussion`/`strings`).
- `instruments`: canonical instrument key -> ordered list of aliases (matched case-insensitively,
  longest alias first). Canonical keys use `snake_case` (e.g., `bass_clarinet`, `horn`,
  `baritone_horn`, `tuba`).
- `clef_markers`: token -> clef (`(bc)`/`bass` -> `bass`, `(tc)` -> `treble`).
- `transposition_markers`: token -> pitch (`in f` -> `F`, `in bb` -> `Bb`, `in eb` -> `Eb`).
- `score_keywords`: ordered list mapping keywords to `score_type` (`full`/`condensed`/
  `conductor`/`short`).
- `separators`: characters that split title from part (`-`, `_`).

The Python module ships an identical built-in default so the behavior is well-defined without the
file. `config/regex_rules.yaml` overrides/extends the defaults when present.

## 7. Classification Algorithm (per PDF)

1. **Isolate the part segment.** Take the filename stem; strip a leading `piece_folder` (case- and
   separator-insensitive); strip a leading catalog number; strip surrounding separators. The
   remainder is `part_segment` (e.g., `Basses - Tuba`, `Horn 1 in F`, `Trombone 3 (Bass)`).
2. **Score detection.** If `part_segment` (or filename) matches a `score_keywords` entry, set
   `is_score = true`, `family = "score"`, `score_type`, and `predicted_part` (e.g., `Full Score`).
3. **Instrument match (filename).** Normalize `part_segment`; scan the lexicon and select the
   longest alias that appears as a token/substring. Record `canonical_instrument`, `family`, and
   `matched_alias`.
4. **Modifiers.** Extract `clef` (`(BC)`/`(TC)`/`(Bass)`), `transposition` (`in F/Bb/Eb`), and
   `part_index` (number adjacent to the instrument or trailing).
5. **Text confirmation.** Normalize the page-1 text sources; if the same canonical instrument's
   alias appears, set `filename+text` agreement. If the filename gave no instrument but text does,
   recover a text-only prediction.
6. **Confidence + evidence.** Deterministic scoring (Section 8).
7. **Human label.** Compose `predicted_part` from canonical instrument + transposition + number +
   clef (e.g., `Horn in F 2`, `Baritone (BC)`, `Cornet 1`).

## 8. Confidence Scoring (deterministic, 0..1)

| Situation | Score | evidence_source |
|-----------|-------|-----------------|
| Score keyword match | 0.90 | `filename` |
| Filename instrument match confirmed by page text | 0.90 (+0.05 if clef/transposition present, cap 0.98) | `combined` |
| Filename instrument match, no text confirmation | 0.75 | `filename` |
| No filename instrument, but page-text instrument match | 0.50 | `text` |
| No match anywhere | 0.0 (`predicted_part = null`, `family = "unknown"`) | `none` |

`--llm` predictions (when a provider is wired) would report `evidence_source = "llm"`; not enabled
by default.

## 9. Per-Piece Ensemble Harmonization

Within each `piece_id`, compute a signature `(canonical_instrument, part_index, clef, is_score)`
per document. When two or more documents share a non-null, non-score signature, each is flagged
`duplicate_in_piece = true`. This is a *signal* for Scripts 04/05 (possible mislabel or genuine
duplicate), not an automatic edit to the prediction.

## 10. Output Contract

`data/part_predictions.jsonl` — one record per readable PDF:

- `record_version`, `run_id`
- `pdf_path`, `piece_id`, `piece_folder`, `pdf_filename`
- `predicted_part` (human label; nullable)
- `canonical_instrument` (snake_case key; nullable)
- `family` (`woodwind`/`brass`/`percussion`/`strings`/`score`/`unknown`)
- `part_index` (int; nullable)
- `clef` (`bass`/`treble`; nullable)
- `transposition` (`F`/`Bb`/`Eb`/...; nullable)
- `is_score` (bool), `score_type` (`full`/`condensed`/`conductor`/`short`; nullable)
- `confidence` (float 0..1)
- `evidence_source` (`filename`/`text`/`combined`/`none`/`llm`)
- `alternates` (list of `{canonical_instrument, score}`, top 2)
- `match_details` (`{part_segment, matched_alias, filename_match, text_match}`)
- `duplicate_in_piece` (bool)
- `processing_status` (`success`/`skipped_unreadable`), `processing_timestamp`

Checkpoint: `data/.part_classifier_checkpoint.json` with `record_version`, `last_run_id`,
timestamps, IO paths, `fingerprints` (pdf_path -> file_fingerprint), and counts.

## 11. Idempotency & Incremental Mode

Mirror Scripts 01/02:

- `--mode full`: reclassify every readable PDF and rewrite outputs.
- `--mode incremental`: reuse prior prediction records for PDFs whose inventory
  `file_fingerprint` matches the checkpoint; only changed/new PDFs are reclassified. Orphans are
  logged. Ensemble harmonization is recomputed over the merged set so duplicate flags stay
  correct.

Outputs are rebuilt in memory, sorted by `pdf_path`, and written atomically.

## 12. CLI Design

| Option | Type | Default | Purpose |
|--------|------|---------|---------|
| `--inventory` | Path | `data/raw_inventory.jsonl` | Script 01 inventory input |
| `--documents` | Path | `data/documents.jsonl` | Script 02 document rollups (optional) |
| `--pages` | Path | `data/pages.jsonl` | Script 02 page features (optional) |
| `--rules` | Path | `config/regex_rules.yaml` | Instrument lexicon (optional) |
| `--output` | Path | `data/part_predictions.jsonl` | Prediction output |
| `--output-report` | Path | `data/part_classification_report.md` | Markdown summary |
| `--report / --no-report` | flag | enabled | Toggle the report |
| `--mode` | str | `full` | `full` or `incremental` |
| `--use-llm / --no-llm` | flag | disabled | Enable the (unwired) LLM fallback hook |
| `--log-level` | str | `INFO` | Logging level |

## 13. Failure Policy

- Missing inventory: hard error (nothing to classify).
- Missing Script 02 outputs: warn and run filename-only.
- Unreadable/`pdf_readable == false` inventory rows: emit a `skipped_unreadable` record.
- Per-record classification never raises; on unexpected error the record is emitted with
  `family = "unknown"`, `confidence = 0.0`, and the error logged.

## 14. Testing Strategy

`tests/test_part_classifier.py`:

- Part-segment isolation (prefix strip, dash-in-part, underscore separator).
- Instrument match longest-alias-wins (`Bass Trombone` vs `Trombone`).
- Clef/transposition/part-index extraction.
- Score detection.
- Text-only recovery + confidence tiers.
- Duplicate-in-piece flagging.
- Full end-to-end run over a tiny synthetic inventory + documents fixture, asserting the output
  JSONL, report, and checkpoint.

## 15. Implementation Notes Log

- Plan authored: 2026-07-26.
- Implementation completed: 2026-07-26.
  - Implemented `scripts/03_part_classifier.py` (rule-first, deterministic).
  - Built-in lexicon baked into the module; `config/regex_rules.yaml` populated with the same
    vocabulary and loaded via PyYAML (added `PyYAML>=6.0` dependency; degrades to the built-in
    lexicon when PyYAML/file are unavailable).
  - Part-segment isolation strips the `piece_folder` prefix (handles dash-in-part like
    `Basses - Tuba` and both `-`/`_` separators). Longest-alias-wins instrument matching with
    word-boundary regex so short aliases (`cl`, `fl`, `hn`) never match inside words.
  - Clef (`(BC)`/`(TC)`/`(Bass)`), transposition (`in F/Bb/Eb`), part index, and score detection.
  - Page-text confirmation/recovery, deterministic confidence tiers, per-piece duplicate flagging,
    full/incremental modes, checkpoint, and a Markdown report.
  - LLM fallback hook present but disabled by default and unwired (no fabricated predictions).
- Validation completed: 2026-07-26.
  - `tests/test_part_classifier.py`: 16 tests pass (unit + end-to-end + incremental reuse).
  - Live run over the real 26-document library: all 26 classified with 0 unmatched, 0
    low-confidence, 0 duplicates. 13 filename-only (0.75) and 13 text-confirmed (`combined`,
    0.90-0.95). Correctly handled `Basses - Tuba` -> Tuba, `Horn 1 in F` -> `Horn in F 1`,
    `Trombone 3 (Bass)` -> `Trombone 3 (BC)`, and `Baritone (BC)/(TC)` clefs.

</content>
</invoke>
