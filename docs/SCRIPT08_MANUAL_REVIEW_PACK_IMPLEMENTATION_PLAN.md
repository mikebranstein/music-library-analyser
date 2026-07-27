# Script 08 — Manual Review Pack: Implementation Plan

Status: **planned → implemented** (schema `1.0`). This document is the design of record for
`scripts/08_manual_review_pack.py`; the master plan (`docs/BAND_COLLECTION_ANALYSIS_PLAN.md` §4.8)
links here.

## 1. Purpose

Turn the deterministic per-piece findings into a **prioritized manual-review queue** so a
librarian's limited time targets the highest-value fixes first. Scripts 06/07 are *reporting*
stages that only count/group/order; Script 08 is the first stage whose job **is** to compute
something new — a **priority ordering** — from those facts. It never re-derives completeness,
quality, severity, or reason codes; it only *weights and orders* pieces the earlier stages already
described.

It is the actionable counterpart to Script 07's descriptive aggregate: Script 07 answers "what is
the state of the library?"; Script 08 answers "what should I fix first, and exactly what is wrong?"

## 2. Design principles

1. **Weight and order, don't recompute.** Every input number is a field already present in the
   Script 06 per-piece records. Script 08 combines them into a transparent, auditable priority
   score; it does not re-open PDFs, call an LLM, or touch the network.
2. **Transparent, deterministic priority.** The score is a weighted sum of named magnitude weights
   (module constants), plus a severity base. Each piece carries a `priority_breakdown` so every
   point in its score is explainable and unit-testable. Same inputs ⇒ same queue, every time.
3. **Reuse Script 06's vocabulary verbatim.** `reason_codes`, `recommended_actions`, and
   `action_items` are copied straight through — Script 08 does not invent new advice, it *orders*
   the existing advice. Grouping/ordering uses the shared `_common.py` constants
   (`Severity`/`SEVERITY_ORDER`, `CompletenessTier`, `REASON_CODE_ORDER`, `piece_sort_key`).
4. **Single authoritative input.** Script 08 reads `data/piece_reports/*.json` (Script 06's
   schema-1.1 records) — the same authoritative source Script 07 reads. The per-piece reports are
   required because the queue's line items (`action_items` with named target documents/parts, the
   likely-missing part labels, and a sample page thumbnail) live only there, not in Script 07's
   `pieces[]` projection. If the directory is missing/empty, Script 08 fails with a clear "run
   Script 06 first" message.
5. **Mirror the established conventions.** Reuse `_common.py` scaffolding (`setup_logging`,
   `new_record_envelope`, `atomic_write_json/text`, `pct`, `md_cell`, `piece_sort_key`) and the
   Script 07 data-health callout pattern.

> **Input choice (documented):** master plan §4.8 says Script 08 *MAY* read Script 07's
> `summary.json` `pieces[]` index *instead of* re-scanning the per-piece reports. That index is a
> lightweight projection and deliberately omits `action_items`, `recommended_actions`,
> `expected_parts`, and per-document `thumbnail_path`. Because the review pack's line items need all
> four, Script 08 reads the per-piece reports directly (exactly as Script 07 does). This keeps a
> single authoritative input and avoids coupling the pack to Script 07's run freshness.

## 3. Input

| Input | Role | Key fields used |
|-------|------|-----------------|
| `data/piece_reports/*.json` (Script 06, schema 1.1) | Per-piece joined facts + derived findings | `piece_id`, `catalog_number`, `piece_title_guess`, `piece_folder`, `severity`, `completeness_tier`, `completeness_score`, `score_missing`, `missing_required_count`, `unexpected_part_count`, `document_count`, `worst_quality_band`, `quality_summary` (`low_quality_doc_count`/`handwritten_doc_count`), `review_counts` (`low_confidence_count`/`unmatched_count`/`duplicate_count`), `reason_codes`, `recommended_actions`, `action_items[]`, `expected_parts[]` (`label`/`canonical_instrument`/`section`/`required`/`present`), `documents[0].thumbnail_path` |

The checkpoint dotfile (`.piece_report_checkpoint.json`) is ignored — only `*.json` whose payload
carries a `piece_id` are read. Unreadable/malformed/non-piece files are skipped and counted for a
data-health callout (same loader contract as Script 07).

## 4. Priority model

`priority_score` (integer) = `severity_base` + Σ (weighted magnitude components):

| Component | Source field | Default weight |
|-----------|--------------|---------------:|
| Missing conductor score | `score_missing` (bool) | `40` |
| Missing required part | `missing_required_count` | `10` each |
| Low-quality document | `quality_summary.low_quality_doc_count` | `6` each |
| Handwritten/uncertain document | `quality_summary.handwritten_doc_count` | `4` each |
| Low-confidence part label | `review_counts.low_confidence_count` | `5` each |
| Unmatched part | `review_counts.unmatched_count` | `5` each |
| Duplicate part | `review_counts.duplicate_count` | `2` each |
| Unexpected observed part | `unexpected_part_count` | `1` each |
| Severity base | `severity` | high `20`, review `8`, ok `0` |

Notes:

- Weights are **named module constants** (`WEIGHTS`, `SEVERITY_BASE`) and are echoed into
  `manual_review_queue.json` (and the Markdown pack) so the ranking is fully transparent and can be
  retuned without guesswork.
- `review_counts.needs_review_count` is deliberately **not** added: it is a superset flag count that
  overlaps the low-confidence / unmatched / duplicate components, so counting it would double-count.
- Magnitudes (not booleans) drive the score, so a piece missing five required parts outranks one
  missing a single part, matching how a librarian triages.
- Ordering: `priority_score` **descending**, tie-broken by `piece_sort_key` (catalog then folder)
  for a stable queue.

## 5. Queue record (`manual_review_queue.json`, schema 1.0)

On top of `new_record_envelope`:

- `piece_count`, `pieces_skipped`, `source_dir`.
- `record_version_distribution`: count of each Script 06 schema version (data-health guard; mirrors
  Script 07).
- `weights`: the `WEIGHTS` + `severity_base` map actually applied (transparency / reproducibility).
- `queue`: the prioritized list, one entry per piece, ordered by `priority_score` desc then
  `piece_sort_key`. Each entry carries:
  - `rank` (1-based), `piece_id`, `catalog_number`, `piece_title_guess`, `piece_folder`
  - `priority_score`, `priority_breakdown` (per-component point contributions; components at 0 are
    included so the row is self-describing)
  - `severity`, `completeness_tier`, `completeness_score`, `score_missing`, `worst_quality_band`
  - magnitude counts: `missing_required_count`, `unexpected_part_count`, `low_quality_doc_count`,
    `handwritten_doc_count`, `document_count`, and the three scored `review_counts`
  - `reason_codes[]`, `recommended_actions[]`, `action_items[]` (copied verbatim from Script 06)
  - `missing_required_parts[]`: the likely-missing parts — `{label, canonical_instrument, section}`
    projected from `expected_parts[]` where `required` and not `present`
  - `sample_thumbnail`: the first document's `thumbnail_path` (a page-1 sample), or `null`
  - `report_path`: relative link to the per-piece Markdown report

`--limit N` (N > 0) truncates `queue` to the top-N pieces after ranking; the totals/counts still
reflect the full input set.

## 6. Outputs

- `data/review_pack/manual_review_queue.json` — the machine-readable prioritized queue (schema 1.0;
  the stable contract).
- `data/review_pack/manual_review_queue.csv` — a flat, one-row-per-piece prioritized export for
  sorting/filtering in a spreadsheet.
- `data/review_pack/review_pack.md` — the human-readable prioritized pack: a **Data health** callout
  (only when needed), the applied priority weights, a ranked summary table, and a per-piece detail
  section (priority breakdown, likely-missing parts, recommended actions, action-item targets,
  sample thumbnail link) for the top pieces.

> **Deviation from the original §4.8 output list (documented):** §4.8 lists "CSV and optional
> lightweight HTML dashboard." To stay consistent with Scripts 06/07 (which emit Markdown that
> renders in GitHub/VS Code without a browser and needs no extra tooling), Script 08 emits a
> Markdown pack instead of HTML, alongside the JSON contract and the flat CSV.

## 7. Markdown pack (`review_pack.md`) sections

1. **Header + run metadata** — generated timestamp, run id, piece count, source directory. A **Data
   health** callout appears only when files were skipped or an unexpected schema version is present.
2. **Priority weights** — the applied `WEIGHTS` + severity base, so the ranking is auditable.
3. **Review queue** — ranked table: Rank | Priority | Catalog | Piece | Severity | Missing req. |
   Reason codes | Report.
4. **Top pieces (detail)** — for the top pieces (bounded by `--detail-limit`), a per-piece block
   with the priority breakdown, likely-missing parts, recommended actions, action-item targets, and
   the sample thumbnail link.

All tables escape cells with `md_cell`; no verdict/QA language.

## 8. CLI

```
08_manual_review_pack.py
  --piece-reports-dir   data/piece_reports
  --output-dir          data/review_pack
  --csv / --no-csv      (default on; write manual_review_queue.csv)
  --limit               0   (0 = full queue; N>0 keeps only the top-N ranked pieces in all outputs)
  --detail-limit        20  (how many top pieces get a detail block in review_pack.md)
  --mode                full | incremental   (accepted for pipeline uniformity; always a full
                        recompute — priority is cheap and has no per-item cache)
  --log-level           INFO
```

Output paths (`manual_review_queue.json`, `manual_review_queue.csv`, `review_pack.md`) are derived
from `--output-dir`.

## 9. Shared infrastructure / reuse

- Reuse from `_common.py`: `setup_logging`, `new_record_envelope`, `atomic_write_json`,
  `atomic_write_text`, `pct`, `md_cell`, `piece_sort_key`, and the `Severity` / `SEVERITY_ORDER` /
  `CompletenessTier` / `REASON_CODE_ORDER` vocabulary (compared against the shared constants, never
  string literals).
- The per-piece loader mirrors Script 07's `load_piece_records` contract (skip the checkpoint
  dotfile, keep only payloads with a `piece_id`, count skips for the data-health callout).
- CSV is written with the stdlib `csv` module into a string buffer, then persisted via
  `atomic_write_text` (atomic, consistent with the other outputs).

## 10. Testing

Fully offline unit tests in `tests/test_manual_review_pack.py` (module loaded via the shared
`importlib` helper), covering: the priority score and `priority_breakdown` for a seeded piece;
`needs_review_count` is not double-counted; queue ordering (priority desc, `piece_sort_key`
tie-break) and `rank` assignment; `missing_required_parts[]` projection from `expected_parts`;
`action_items` / `recommended_actions` / `sample_thumbnail` pass-through; `--limit` truncation; CSV
row shape; the data-health skip accounting; the "no piece reports → clear error" path; and the CLI
end-to-end writing all three outputs.

## 11. Downstream / boundaries

Script 08 is the pipeline's terminal stage: it produces the artifact a human acts on. It owns the
authoritative priority formula; Scripts 06/07 stay descriptive and never encode ordering. The
`manual_review_queue.json` schema (`record_version 1.0`) is the stable contract; the CSV and
Markdown are presentation layers (the CSV is a flat projection of `queue[]`). Retuning priority is a
weights-only change (bump the schema minor version if the queue record shape changes).
