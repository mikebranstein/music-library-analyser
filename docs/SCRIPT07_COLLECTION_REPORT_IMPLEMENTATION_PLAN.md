# Script 07 — Collection Report: Implementation Plan

Status: **planned → implemented** (schema `1.0`). This document is the design of record for
`scripts/07_collection_report.py`; the master plan (`docs/BAND_COLLECTION_ANALYSIS_PLAN.md` §4.7)
links here.

## 1. Purpose

Aggregate every per-piece report into one **collection-wide view**: how big the library is, how
complete it is, where the quality problems concentrate, and which instruments are most often
missing. Like Script 06, this stage is a *reporting* stage — it **computes nothing new about the
music**. It counts, groups, and orders the deterministic facts Scripts 01–06 already produced.

It is the collection-level counterpart to Script 06 (per-piece) and gives the librarian the "state
of the whole library at a glance" plus the raw material Script 08 turns into a prioritized queue.

## 2. Design principles

1. **Aggregate, don't recompute.** Every number is a count/sum/group of a field already present in
   the Script 06 per-piece records. Script 07 never re-derives completeness, quality, severity, or
   reason codes.
2. **Single, well-defined input.** Script 07 aggregates `data/piece_reports/*.json` (Script 06's
   schema-1.1 records). Those records are already the faithful join of Scripts 01–05, so reading
   them — rather than re-joining the raw stage outputs — is what keeps 06 and 07 from ever
   diverging. If the directory is missing/empty, Script 07 fails with a clear "run Script 06 first"
   message.
3. **Deterministic + offline.** No AI, no network. Pure function of the input JSON files +
   `RECORD_VERSION`. Fully unit-testable.
4. **Shared vocabulary, never literals.** Group/order by the shared constants in
   `scripts/_common.py` (`COMPLETENESS_TIER_ORDER`, `LOOKUP_STATUS_ORDER`, `SEVERITY_ORDER`,
   `REASON_CODE_ORDER`). This plan **promotes** `ReasonCode` / `REASON_CODE_ORDER` /
   `REASON_ACTIONS` / `Severity` from Script 06 into `_common.py` (adding `SEVERITY_ORDER`) so both
   Scripts 06 and 07 (and later 08) import one definition — exactly the anti-duplication rule in
   master plan §4.9.
5. **Mirror the established conventions.** Reuse `_common.py` scaffolding (`setup_logging`,
   `new_record_envelope`, `read_jsonl`, `atomic_write_json/text`, `pct`, `md_cell`,
   `piece_sort_key`).

## 3. Input

| Input | Role | Key fields used |
|-------|------|-----------------|
| `data/piece_reports/*.json` (Script 06, schema 1.1) | Per-piece joined facts + derived findings | `piece_id`, `catalog_number`, `piece_title_guess`, `piece_folder`, `processing_status`, `has_score`, `score_missing`, `completeness_tier`, `lookup_status`, `severity`, `needs_review`, `reason_codes`, `missing_required_count`, `unexpected_part_count`, `expected_part_count`, `observed_instrument_count`, `expected_parts[]` (`canonical_instrument`/`section`/`required`/`present`), `quality_summary` (`band_counts`/`low_quality_doc_count`/`handwritten_doc_count`), `review_counts`, `document_count` |

The checkpoint dotfile (`.piece_report_checkpoint.json`) is ignored — only `*.json` whose payload
carries a `piece_id` are aggregated.

## 4. Outputs

- `data/collection_reports/summary.md` — the human-readable collection dashboard.
- `data/collection_reports/summary.json` — the machine-readable aggregate record (schema 1.0).
- `data/collection_reports/pieces.csv` — a flat, **non-prioritized** one-row-per-piece export
  (catalog, title, severity, completeness tier, missing-required count, worst quality band,
  needs-review, reason codes) for sorting/filtering in a spreadsheet.

> **Deviation from the original §4.7 output list (documented):** the original plan listed a
> `manual_review_queue.csv` under Script 07. The *prioritized* review queue is owned by Script 08
> (§4.8), which holds the authoritative priority formula. To avoid two scripts producing competing
> queues, Script 07 emits only the flat `pieces.csv` collection export; Script 08 consumes the
> per-piece records (and this CSV if useful) to build the prioritized `manual_review_queue`.

## 5. Aggregate record (`summary.json`, schema 1.0)

On top of `new_record_envelope`:

- `piece_count`, `source_dir`.
- `totals`: `pieces`, `pieces_complete`, `pieces_with_missing_required`, `pieces_missing_score`,
  `pieces_needs_review`, `pieces_high_severity`, `pieces_with_errors`, `documents`,
  `documents_low_quality`, `documents_handwritten`.
- `completeness_distribution`: count per tier in `COMPLETENESS_TIER_ORDER`.
- `lookup_status_distribution`: count per status in `LOOKUP_STATUS_ORDER`.
- `severity_distribution`: count per severity in `SEVERITY_ORDER` (`high` → `review` → `ok`).
- `quality_band_distribution`: document-level `good`/`review`/`poor`/`unknown` summed across pieces.
- `reason_code_frequency`: number of **pieces** exhibiting each reason code, in `REASON_CODE_ORDER`.
- `review_totals`: summed `needs_review_count` / `low_confidence_count` / `unmatched_count` /
  `duplicate_count`.
- `top_missing_instruments`: list of `{canonical_instrument, section, missing_piece_count}` for
  **required** parts that are absent, sorted by count desc then name — "what am I most often missing
  across the whole library."
- `attention_pieces`: the high-severity pieces (catalog, title, `piece_id`, reason codes) for the
  dashboard's call-out list, ordered by `piece_sort_key`.

## 6. Markdown report (`summary.md`) sections

1. **Header + run metadata** — generated timestamp, run id, piece count, source directory.
2. **Headline totals** — complete / missing-required / missing-score / needs-review / high-severity
   counts with percentages (`pct`).
3. **Completeness distribution** — table over `COMPLETENESS_TIER_ORDER`.
4. **Instrumentation lookup distribution** — table over `LOOKUP_STATUS_ORDER`.
5. **Severity distribution** — table over `SEVERITY_ORDER`.
6. **Scan quality** — document-level band distribution + total low-quality / handwritten documents.
7. **Reason-code frequency** — how many pieces hit each reason code (with the shared action string).
8. **Top missing instruments** — the most-often-missing required instruments across the library.
9. **Pieces needing attention** — high-severity pieces, each linking to its per-piece report
   (`../piece_reports/<piece_id>.md`).

All tables escape cells with `md_cell` and use `pct` for percentages; no verdict/QA language.

## 7. CLI

```
07_collection_report.py
  --piece-reports-dir   data/piece_reports
  --output-dir          data/collection_reports
  --csv / --no-csv      (default on; write pieces.csv)
  --mode                full | incremental   (accepted for pipeline uniformity; the collection
                        aggregate is always a full recompute — it is cheap and has no per-item cache)
  --log-level           INFO
```

Output paths (`summary.md`, `summary.json`, `pieces.csv`) are derived from `--output-dir`.

## 8. Shared infrastructure / refactor

- **Promote to `_common.py`:** `ReasonCode`, `REASON_CODE_ORDER`, `REASON_ACTIONS`, `Severity`, and
  a new `SEVERITY_ORDER = (Severity.HIGH, Severity.REVIEW, Severity.OK)`. Script 06 imports these
  (its public names `ReasonCode` / `REASON_CODE_ORDER` / `Severity` remain valid attributes, so its
  tests are unaffected). Script 07 imports them for grouping/ordering.
- Reuse `setup_logging`, `new_record_envelope`, `read_jsonl`, `atomic_write_json`,
  `atomic_write_text`, `pct`, `md_cell`, `piece_sort_key`, `utc_now_iso`.
- CSV is written with the stdlib `csv` module into a string buffer, then persisted via
  `atomic_write_text` (atomic, consistent with the other outputs).

## 9. Testing

Fully offline unit tests in `tests/test_collection_report.py` (module loaded via the shared
`importlib` helper), covering: totals and each distribution; reason-code frequency counts pieces
(not occurrences); `top_missing_instruments` aggregation and ordering; `attention_pieces`
selection; CSV row shape; the "no piece reports → clear error" path; and the CLI end-to-end writing
all three outputs.

## 10. Downstream implications (Script 08)

- Script 08 (Manual Review Pack) builds the **prioritized** queue. It reads the per-piece records
  (schema 1.1) for `reason_codes` / `action_items` / magnitude counts and MAY read Script 07's
  `summary.json` for collection context (e.g. to rank against library-wide frequencies) and
  `pieces.csv` as a starting grid. Script 08 owns the authoritative priority formula; Script 07's
  aggregates are descriptive, not prescriptive.
- The `summary.json` schema (`record_version 1.0`) is the stable contract; the Markdown/CSV are
  presentation layers.
