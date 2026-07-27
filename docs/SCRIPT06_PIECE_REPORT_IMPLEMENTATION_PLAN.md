# Script 06 — Piece Report Generator: Implementation Plan

Status: **planned → implemented** (schema `1.0`). This document is the design of record for
`scripts/06_piece_report.py`; the master plan (`docs/BAND_COLLECTION_ANALYSIS_PLAN.md` §4.6) links
here.

## 1. Purpose

Produce one **human-readable Markdown report** and one **machine-readable JSON record** per piece
folder, joining every upstream signal on `piece_id` into a single "what do we know about this
piece, and what should a human do about it" view. Script 06 is the first *reporting* stage: it
computes nothing new about the music: it **joins, orders, and narrates** the deterministic facts
Scripts 01–05 already produced, and derives a prioritized list of recommended manual actions.

It is the per-piece counterpart to Script 07 (collection aggregate) and the input a human uses when
working the Script 08 review queue.

## 2. Design principles

1. **Join, don't recompute.** Every fact comes from an upstream record. Script 06 never re-derives
   confidence, completeness, or quality — it surfaces `confidence_tier` / `completeness_tier` /
   `quality_band` and the `needs_review` flags exactly as upstream set them (per the standing rule
   in the master plan §4.6/§4.9).
2. **Resilient to missing stages.** A piece is reportable if it appears in *any* per-piece source.
   Missing inputs (e.g. Script 04 not yet run) degrade a section to "not available" rather than
   failing the piece.
3. **Deterministic + offline.** No AI, no network, no rendering. Pure function of the input JSONL
   files + `RECORD_VERSION`. This makes the whole stage cheap, fast, and fully unit-testable.
4. **Mirror the established conventions.** Reuse `scripts/_common.py` scaffolding, the
   `run_with_progress` + `on_result` incremental-streaming pattern, and the per-piece split +
   index + orphan-cleanup shape that Script 04 established (master plan §4.10).

## 3. Inputs (all joined on `piece_id`)

| Input | Role | Key fields used |
|-------|------|-----------------|
| `data/expected_parts.jsonl` (Script 04) | Expected instrumentation, missing/unexpected parts, completeness, lookup status, ensemble, work identity, evidence | `expected_parts[]`, `missing_parts[]`, `missing_required_parts`, `unexpected_parts[]`, `completeness_score/tier`, `lookup_status`, `ensemble_type/display_name`, `work_identity`, `score_expected/missing`, `needs_review` |
| `data/observed_parts_by_piece.jsonl` (Script 03) | Observed-parts rollup, score presence, review counts | `observed_parts[]` (sorted by `part_sort_key`), `has_score`, `score_types`, `distinct_instruments`, `families`, `sections`, `needs_review_count`, `unmatched_count`, `low_confidence_count`, `duplicate_count`, `catalog_number`, `piece_title_guess` |
| `data/part_predictions.jsonl` (Script 03) | Per-document detected parts | grouped by `piece_id`: `pdf_path`, `pdf_filename`, `predicted_part`, `confidence_tier`, `needs_review`, `is_score`, `score_type`, `duplicate_in_piece`, `part_sort_key` |
| `data/quality_metrics.jsonl` (Script 05) | Per-document scan quality + notation source | grouped by `piece_id`, joined to documents by `pdf_path`: `quality_band`, `quality_score`, `notation_source_type`, `top_issues`, `needs_review`, `worst_page` |
| `data/documents.jsonl` (Script 02) | Per-document page counts / identity (optional) | `pdf_path`, `page_count` |
| `data/pages.jsonl` (Script 02) | Thumbnail references (optional) | page-1 `thumbnail_path` per `pdf_path` |

**Piece universe:** the union of `piece_id`s across `expected_parts`, `observed_parts_by_piece`,
`part_predictions`, and `quality_metrics`. Identity fields (`catalog_number`, `piece_title_guess`,
`piece_folder`) are resolved with a fixed precedence: expected_parts → observed rollup →
part_predictions → documents.

## 4. Outputs

Following the master plan §4.10 per-piece split convention:

- `data/piece_reports/<piece_id>.md` — the human report for one piece.
- `data/piece_reports/<piece_id>.json` — the machine record for one piece (carries the standard
  `new_record_envelope` header + schema `record_version`).
- `data/piece_reports.md` — a lightweight **index** (run metadata + a table sorted by catalog via
  `piece_sort_key`, one row per piece linking to its `.md`).
- `data/.piece_report_checkpoint.json` — checkpoint for `--mode incremental`.

`<piece_id>.md` / `<piece_id>.json` filenames honor the explicit master-plan §4.6 contract (the
files are looked up by `piece_id`). The index rows sort by catalog for human navigation. Files for
pieces that no longer exist are pruned at end of run (orphan cleanup; only `*.md`/`*.json` inside
`data/piece_reports/` are touched).

## 5. Per-piece Markdown report sections

1. **Piece identity** — catalog number, title, folder; ensemble type + display name; resolved
   edition identity + lookup status/confidence (from Script 04). Degrades to "instrumentation not
   looked up" when Script 04 is absent or fell back.
2. **Detected documents & predicted parts** — one row per PDF (ordered by `part_sort_key`):
   filename, predicted part, confidence tier, score?/score type, quality band, notation source,
   review flag, optional thumbnail link. Joins part_predictions + quality_metrics by `pdf_path`.
3. **Expected parts & missing parts** — the Script 04 expected-instrumentation table
   (`# / instrument / label / section / required / observed`), then missing-required,
   missing-optional, and unexpected-parts call-outs. "Not available" when Script 04 is absent.
4. **Score presence** — `has_score`, `score_types`, and (when Script 04 ran) `score_expected` /
   `score_missing`.
5. **Quality findings** — per-document quality bands + top issues + notation source, plus a
   piece-level worst-band summary; born-digital vs handwritten note.
6. **Confidence summary** — the observed-rollup review counts (`needs_review_count`,
   `low_confidence_count`, `unmatched_count`, `duplicate_count`) and Script 04 `needs_review`,
   surfaced as-is (no re-thresholding of raw confidence floats).
7. **Recommended manual actions** — the derived, prioritized reason codes (see §6).

## 6. Derived fields (the only new information Script 06 produces)

These are deterministic roll-ups of upstream flags, recorded in the JSON and rendered as the
"Recommended manual actions" section:

- `reason_codes` (ordered): a subset of
  `missing_score`, `missing_required_parts`, `unexpected_parts`, `low_quality_scans`,
  `handwritten_or_illegible`, `low_confidence_parts`, `duplicate_parts`,
  `instrumentation_unresolved`. Each maps to a fixed human-readable recommended action string.
- `needs_review` (piece-level bool): true when **any** contributing signal is set — Script 04
  `needs_review`, observed-rollup review/low-confidence/unmatched/duplicate counts > 0, any
  per-document quality `needs_review`, a missing score, missing required parts, or an unresolved
  lookup. Mirrors upstream flags; never invents a "pass".
- `severity` (`ok` / `review` / `high`): `high` when a required part or the score is missing or any
  scan is `poor`; `review` when any softer flag is set; else `ok`. This is a *hint*; the
  authoritative work-queue priority is computed by Script 08.

> Per the standing engagement rules, no verdict/QA language ("passed", "production-ready") is used;
> the report states findings and open items only.

## 7. Shared infrastructure (from `scripts/_common.py`)

- `RECORD_VERSION = "1.0"`; `CHECKPOINT_FILENAME = ".piece_report_checkpoint.json"`;
  `make_checkpoint_path` / `load_checkpoint` / `build_checkpoint`.
- `new_record_envelope(run_id, RECORD_VERSION)` for every JSON record.
- `read_jsonl`, `atomic_write_text`, `atomic_write_json`, `piece_sort_key`, `pct`, `md_cell`.
- `LookupStatus` / `CompletenessTier` constants + `COMPLETENESS_TIER_ORDER` (branch on constants,
  never bare strings).
- `run_with_progress(items, worker, max_workers, on_result=...)` — render pieces (optionally
  concurrently) and persist each `.md` + `.json` + checkpoint as it completes; write the index and
  run orphan cleanup at the end.

## 8. Incremental mode

Per-piece fingerprint = `sha256` of the sorted-JSON join slice for the piece (its expected_parts
record + observed rollup + sorted part_predictions + sorted quality_metrics + document page counts
+ thumbnails), combined with a config fingerprint (currently just `RECORD_VERSION`, so a schema
bump invalidates all). In `--mode incremental`, a piece is reused when its prior `.json` exists,
its `record_version` matches, and the checkpoint fingerprint matches; otherwise it is re-rendered.
Previous records are read back from `data/piece_reports/*.json`.

## 9. CLI

```
06_piece_report.py
  --expected-parts       data/expected_parts.jsonl
  --observed-parts       data/observed_parts_by_piece.jsonl
  --part-predictions     data/part_predictions.jsonl
  --quality-metrics      data/quality_metrics.jsonl
  --documents            data/documents.jsonl
  --pages                data/pages.jsonl
  --output-dir           data/piece_reports
  --output-index         data/piece_reports.md
  --write-index/--no-index        (default on)
  --thumbnails/--no-thumbnails     (default on; include page-1 thumbnail links)
  --mode                 full | incremental   (default full)
  --concurrency / -j     1
  --log-level            INFO
```

## 10. Testing

Fully offline unit tests in `tests/test_piece_report.py` (module loaded via the shared
`importlib` helper), covering: piece-universe union across sources; identity precedence; the
detected-documents join (part_predictions × quality by `pdf_path`); missing/unexpected rendering;
reason-code + severity + `needs_review` derivation for representative pieces (complete, missing
required, missing score, low quality, low confidence, unresolved lookup); index generation;
orphan cleanup; and incremental reuse.

## 11. Downstream implications (Scripts 07/08)

- Script 07 aggregates across pieces. It reads `expected_parts.jsonl` (source of truth) and MAY
  read the Script 06 `data/piece_reports/*.json` records for the pre-derived `reason_codes` /
  `severity` / `needs_review` rather than re-deriving them. Either way the join keys and vocabulary
  are shared via `_common.py`.
- Script 08 builds the prioritized work queue. It owns the authoritative priority formula; Script
  06's `severity` is only a hint. Script 08 can consume the same per-piece JSON records for reason
  codes and thumbnail references.
- The per-piece JSON schema (`record_version 1.0`) is the stable contract for 07/08; the Markdown
  is human-facing only.
