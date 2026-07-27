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

## 5. Aggregate record (`summary.json`, schema 1.2)

On top of `new_record_envelope`:

- `piece_count`, `pieces_skipped`, `source_dir`.
- `record_version_distribution`: count of each Script 06 schema version across the input records
  (v1.1 data-health guard; mixed/stale versions => some aggregates may be incomplete).
- `totals`: `pieces`, `pieces_complete`, `pieces_with_missing_required`, `pieces_missing_score`,
  `pieces_needs_review`, `pieces_high_severity`, `pieces_with_errors`, `documents`,
  `documents_low_quality`, `documents_handwritten`.
- `completeness_distribution`: count per tier in `COMPLETENESS_TIER_ORDER`.
- `completeness_score_summary`: `{count, mean, median, min, max}` over the per-piece
  `completeness_score` floats (v1.1 numeric KPI; `None` values when no piece is scored).
- `lookup_status_distribution`: count per status in `LOOKUP_STATUS_ORDER`.
- `severity_distribution`: count per severity in `SEVERITY_ORDER` (`high` → `review` → `ok`).
- `quality_band_distribution`: document-level `good`/`review`/`poor`/`unknown` summed across pieces.
- `reason_code_frequency`: number of **pieces** exhibiting each reason code, in `REASON_CODE_ORDER`.
- `review_totals`: summed `needs_review_count` / `low_confidence_count` / `unmatched_count` /
  `duplicate_count`.
- `top_missing_instruments`: list of `{canonical_instrument, section, missing_piece_count}` for
  **required** parts that are absent, sorted by count desc then name — "what am I most often missing
  across the whole library."
- `top_missing_sections`: coarser `{section, missing_piece_count}` companion (v1.2), counting distinct
  pieces missing at least one required part in each section — how librarians often think ("short on
  percussion").
- `attention_pieces`: the pieces needing attention (v1.2: high **then** review severity), each
  enriched with `severity`, `completeness_score`, and the magnitude counts
  (`missing_required_count`, `low_quality_doc_count`, `handwritten_doc_count`) plus `reason_codes`,
  ordered by severity rank (`SEVERITY_ORDER`) then `piece_sort_key`, for the dashboard call-out list.
- `pieces`: the v1.1 **structured per-piece index** (see §11) — the stable Script 08 contract and the
  source that `pieces.csv` projects.

## 6. Markdown report (`summary.md`) sections

1. **Header + run metadata** — generated timestamp, run id, piece count, source directory.
   A **Data health** callout appears only when files were skipped or a schema version other than the
   expected one is present.
2. **Headline totals** — complete / missing-required / missing-score / needs-review / high-severity
   counts with percentages (`pct`).
3. **Collection completeness** — one-line numeric KPI (mean / median / range) from
   `completeness_score_summary`, shown when at least one piece is scored.
4. **Completeness distribution** — table over `COMPLETENESS_TIER_ORDER`.
5. **Instrumentation lookup distribution** — table over `LOOKUP_STATUS_ORDER`.
6. **Severity distribution** — table over `SEVERITY_ORDER`.
7. **Scan quality** — document-level band distribution + total low-quality / handwritten documents.
8. **Reason-code frequency** — how many pieces hit each reason code (with the shared action string).
9. **Top missing sections** — the most-often-missing required *sections* across the library (v1.2).
10. **Top missing instruments** — the most-often-missing required instruments across the library.
11. **Pieces needing attention** — high then review severity pieces with severity + magnitude columns
    (missing-required / low-quality), each linking to its per-piece report
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

- Script 08 (Manual Review Pack) builds the **prioritized** queue. As of schema 1.1 it can read a
  **single file** \u2014 `summary.json` \u2014 whose `pieces[]` index carries every per-piece magnitude count
  and `reason_codes` array it needs, instead of re-opening each per-piece report. It MAY still open
  the individual records for the richer `action_items` (named target documents/parts) and page
  thumbnails, and MAY read the collection distributions to rank a piece against library-wide
  frequencies. Script 08 owns the authoritative priority formula; Script 07's aggregates are
  descriptive, not prescriptive.
- The `summary.json` schema (`record_version 1.2`) is the stable contract; the Markdown/CSV are
  presentation layers (`pieces.csv` is a flat projection of `pieces[]`).

## 11. v1.1 enhancements (gap analysis)

Auditing the v1.0 output against what Script 08 and a maintainer actually need surfaced three gaps.
All three additions stay descriptive (no priority logic) and keep Script 07 a pure offline aggregate.

| # | Gap in v1.0 | v1.1 addition | Why it matters |
|---|-------------|---------------|----------------|
| 1 | The only per-piece data in the stable JSON contract was `attention_pieces` (high-severity only); the full grid lived only in `pieces.csv`, a presentation layer | `pieces[]` structured index in `summary.json` (severity, tiers, `completeness_score`, magnitude counts, `review_counts`, `reason_codes[]`), ordered by `piece_sort_key`; `pieces.csv` is now a projection of it | Script 08 reads one stable JSON file to prioritize instead of re-opening every per-piece report or parsing a CSV |
| 2 | Records were aggregated blindly; an older Script 06 schema (e.g. a partial re-run) would silently read newer fields as 0 and under-count | `record_version_distribution` + `pieces_skipped`, a run-time warning when versions are mixed/stale or files are skipped, and a **Data health** callout in `summary.md` | Prevents silently-wrong collection numbers; makes stale/partial state visible |
| 3 | Completeness was only bucketed into tiers; the underlying `completeness_score` float was never surfaced | `completeness_score_summary` (`count` / `mean` / `median` / `min` / `max`) + a one-line KPI in `summary.md` | Gives a single trackable \"how complete is the library\" number for maintainers and downstream ranking |

`RECORD_VERSION` bumped `1.0` \u2192 `1.1`; `EXPECTED_PIECE_SCHEMA = "1.1"` gates the data-health guard.
Tests extended in `tests/test_collection_report.py` (load skip accounting, `pieces[]` index,
`record_version_distribution`, `completeness_score_summary`, `pieces_skipped`, and the Data-health
Markdown note).
## 12. v1.2 enhancements (gap analysis)

Two further gaps, both descriptive (no priority logic), keeping Script 07 a pure offline aggregate.

| # | Gap in v1.1 | v1.2 addition | Why it matters |
|---|-------------|---------------|----------------|
| 1 | Missing-parts rollup was only per-instrument; a librarian's coarser "which sections am I short on" question needed manual grouping | `top_missing_sections` (`{section, missing_piece_count}`, distinct pieces per section) + a "Top missing sections" table in `summary.md` | Matches how sets are managed/shelved; a one-glance coarse view above the instrument detail |
| 2 | `attention_pieces` was high-severity only and carried no magnitudes, so a consumer could not sort within the list or see review-tier pieces without re-opening records | `attention_pieces` now spans high **then** review severity and carries `severity` + magnitude counts (`missing_required_count`, `low_quality_doc_count`, `handwritten_doc_count`, `completeness_score`); `summary.md` gains Severity / Missing-req. / Low-qual columns | Script 08 (and the dashboard) can rank the call-out list directly; review-tier pieces are no longer invisible |

`RECORD_VERSION` bumped `1.1` → `1.2`. Tests updated: `attention_pieces` high-then-review ordering
with enriched counts, and `top_missing_sections` aggregation/ordering.