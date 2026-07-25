# Bootstrap and Script 01 Implementation Plan

## 1. Objective

Bootstrap the repository for a staged archive-analysis pipeline and implement Script 01 (`scripts/01_inventory.py`) to produce a deterministic, auditable PDF inventory.

This plan is derived from:

- `README.md`
- `docs/BAND_COLLECTION_ANALYSIS_PLAN.md`

## 2. Scope for This Phase

In scope:

- Create the baseline project structure required by the documented pipeline.
- Add initial configuration placeholders.
- Add Python project metadata and dependencies.
- Implement shared utilities for logging, hashing, and JSONL writes.
- Implement Script 01 CLI with full and incremental run modes.
- Validate Script 01 CLI behavior and output schema basics.

Out of scope:

- OCR extraction (Script 02)
- part classification (Script 03)
- authority lookup and expected-part inference (Script 04)
- quality metrics and notation-source classification (Script 05)
- report generation and review queue scripts (06-08)

## 3. Requirements Captured

Script 01 must:

- Recursively discover piece folders and PDF files under a library root.
- Emit one JSON record per PDF to `data/raw_inventory.jsonl`.
- Capture stable file and piece identity fields.
- Capture file metadata and PDF metadata.
- Detect health issues: unreadable, encrypted, malformed, and zero-page PDFs.
- Continue processing on per-file failures (do not crash full run).
- Support idempotent re-runs.
- Support incremental processing of changed/new PDFs.
- Log processing details for auditability.

## 4. Data Contract for `raw_inventory.jsonl`

Each record contains:

- `record_version` (string)
- `run_id` (string, UTC timestamp token)
- `piece_id` (deterministic hash of piece-relative path)
- `piece_folder` (string, normalized relative path under library root)
- `piece_folder_hash` (string, sha256)
- `pdf_path` (string, normalized relative path under library root)
- `pdf_filename` (string)
- `file_size_bytes` (integer)
- `modified_timestamp` (ISO-8601 UTC)
- `file_fingerprint` (string, sha256 over path+size+mtime)
- `page_count` (integer, `-1` on read failure)
- `pdf_readable` (boolean)
- `is_encrypted` (boolean)
- `pdf_metadata` (object with title/author/subject/creator/producer/creation_date/modification_date)
- `health_flags`:
  - `is_zero_page` (boolean)
  - `is_malformed` (boolean)
  - `is_corrupted` (boolean)
  - `warnings` (array of strings)
- `processing_status` (`success`, `success_with_warnings`, `error`)
- `error_message` (string or null)
- `processing_timestamp` (ISO-8601 UTC)

## 5. Design Decisions

1. Deterministic IDs:
- `piece_id` is derived from normalized piece-relative path.
- Relative paths are preferred to keep IDs stable across machines.

2. Incremental strategy:
- Persist a checkpoint in `data/.inventory_checkpoint.json`.
- Use `file_fingerprint` to detect unchanged PDFs.
- Reuse unchanged records from prior output; rebuild output file atomically.

3. Idempotent writes:
- Build final records in memory for this phase and write sorted output atomically.
- Sorting key: `pdf_path` for stable diffs.

4. PDF parser strategy:
- Primary: `pymupdf` (`fitz`) for page count and metadata.
- Fallback: graceful failure with explicit health flags.

5. Failure policy:
- A bad PDF never aborts the run.
- Failures are represented in output and logs.

## 6. Implementation Sequence

1. Create directories and placeholder files from the documented structure.
2. Add project metadata (`pyproject.toml`) and baseline dependencies.
3. Implement shared helpers (`scripts/_common.py`).
4. Implement Script 01 (`scripts/01_inventory.py`).
5. Add script stubs for 02-08 to establish pipeline skeleton.
6. Run CLI help and a local dry run against the repository root for sanity checks.

## 7. Validation Plan

Minimum validation in this phase:

1. CLI starts and shows `--help`.
2. Full mode runs and generates `data/raw_inventory.jsonl`.
3. Re-run in full mode is deterministic (same record ordering and stable identifiers).
4. Incremental mode runs without duplicating unchanged records.
5. Error cases produce `processing_status` and health flags instead of crashes.

## 8. Risks and Mitigations

Risk: PDF metadata inconsistency across files.
Mitigation: nullable metadata fields and strict exception handling.

Risk: path instability across OS conventions.
Mitigation: normalize to POSIX-like relative paths and hash those normalized values.

Risk: checkpoint drift.
Mitigation: checkpoint is rewritten after successful atomic output write.

## 9. Acceptance Criteria

This phase is complete when:

- Repository scaffold exists for all major pipeline areas.
- Script 01 is runnable as a CLI and writes valid JSONL inventory output.
- Full and incremental modes are both implemented.
- Output includes deterministic IDs, health flags, and PDF metadata fields.
- Implementation notes are documented in this plan file.

## 10. Implementation Notes Log

This section is updated during execution.

- Initial plan authored: 2026-07-25.
- Deep research pass completed: 2026-07-26.
  - Used the `Explore` coding subagent in thorough mode to audit implementation for failure modes.
  - Priority findings addressed in implementation:
    - malformed JSON/JSONL tolerance
    - incremental reuse logic simplification
    - orphaned-path logging in incremental mode
    - file race handling (deleted/permission-denied during processing)
    - checkpoint path tied to output directory
    - checkpoint version mismatch warning/ignore behavior
    - mtime precision retained in fingerprint construction
    - atomic write temp-file cleanup on failures

- Bootstrap execution completed: 2026-07-26.
  - Project scaffold present for scripts/config/data/cache/logs/tests.
  - Config placeholders created (`authority_sources.yaml`, `regex_rules.yaml`, `quality_thresholds.yaml`, prompt stubs).
  - Python project metadata added and corrected for editable installs in flat layout.

- Script 01 implementation completed: 2026-07-26.
  - Deterministic piece identity based on normalized relative folder path.
  - Inventory schema fields implemented per Section 4.
  - Full and incremental modes implemented with idempotent JSONL output ordering.
  - Per-file errors captured into output records instead of aborting run.

- Validation completed: 2026-07-26.
  - Full run command executed successfully.
  - Incremental run command executed successfully.
  - Added automated tests for:
    - full+incremental deterministic reuse path
    - malformed/non-object JSONL line handling
