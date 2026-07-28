from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import fitz
from typer.testing import CliRunner

from scripts._common import read_jsonl


runner = CliRunner()


def load_inventory_module():
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "01_inventory.py"
    spec = importlib.util.spec_from_file_location("inventory_script", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load inventory script module")

    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_one_page_pdf(pdf_path: Path) -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    doc.new_page()
    doc.set_metadata(
        {
            "title": "Test Piece",
            "author": "Composer",
            "creator": "pytest",
            "producer": "pymupdf",
        }
    )
    doc.save(pdf_path)
    doc.close()


def test_inventory_full_and_incremental_reuse(tmp_path: Path) -> None:
    inventory = load_inventory_module()

    library_root = tmp_path / "library"
    piece_folder = library_root / "Piece A"
    pdf_path = piece_folder / "Flute 1.pdf"
    make_one_page_pdf(pdf_path)

    output_path = tmp_path / "data" / "raw_inventory.jsonl"

    full_result = runner.invoke(
        inventory.app,
        [
            "--library-root",
            str(library_root),
            "--output",
            str(output_path),
            "--mode",
            "full",
        ],
    )
    assert full_result.exit_code == 0, full_result.stdout

    records = read_jsonl(output_path)
    assert len(records) == 1
    record = records[0]
    assert record["pdf_path"] == "Piece A/Flute 1.pdf"
    assert record["piece_folder"] == "Piece A"
    assert record["page_count"] == 1
    assert record["pdf_readable"] is True
    assert record["processing_status"] == "success"

    full_output_text = output_path.read_text(encoding="utf-8")

    incremental_result = runner.invoke(
        inventory.app,
        [
            "--library-root",
            str(library_root),
            "--output",
            str(output_path),
            "--mode",
            "incremental",
        ],
    )
    assert incremental_result.exit_code == 0, incremental_result.stdout

    incremental_output_text = output_path.read_text(encoding="utf-8")
    assert incremental_output_text == full_output_text
    assert (output_path.parent / ".inventory_checkpoint.json").exists()


def test_subfolders_collapse_into_top_level_piece(tmp_path: Path) -> None:
    """PDFs in nested subfolders belong to their top-level folder's piece, not a new one."""
    inventory = load_inventory_module()

    library_root = tmp_path / "library"
    make_one_page_pdf(library_root / "368 Czardas" / "Solo Clarinet.pdf")
    make_one_page_pdf(library_root / "368 Czardas" / "Solo Alternatives" / "Alto Sax.pdf")
    make_one_page_pdf(library_root / "368 Czardas" / "Score" / "Full Score.pdf")

    output_path = tmp_path / "data" / "raw_inventory.jsonl"
    result = runner.invoke(
        inventory.app,
        ["--library-root", str(library_root), "--output", str(output_path), "--mode", "full"],
    )
    assert result.exit_code == 0, result.stdout

    records = read_jsonl(output_path)
    assert len(records) == 3
    # Every PDF (including those in subfolders) shares one piece_folder and one piece_id.
    assert {r["piece_folder"] for r in records} == {"368 Czardas"}
    assert len({r["piece_id"] for r in records}) == 1
    # The original nested location is preserved in the per-file path.
    paths = {r["pdf_path"] for r in records}
    assert "368 Czardas/Solo Alternatives/Alto Sax.pdf" in paths
    assert "368 Czardas/Score/Full Score.pdf" in paths


def test_inventory_persists_incrementally_during_run(tmp_path: Path, monkeypatch) -> None:
    """Records + checkpoint are written per PDF, not only once at the end."""
    inventory = load_inventory_module()

    library_root = tmp_path / "library"
    make_one_page_pdf(library_root / "Piece A" / "Flute 1.pdf")
    make_one_page_pdf(library_root / "Piece B" / "Oboe 1.pdf")

    output_path = tmp_path / "data" / "raw_inventory.jsonl"

    jsonl_writes = {"count": 0}
    real_write_jsonl = inventory.atomic_write_jsonl

    def counting_write_jsonl(path, records):
        if Path(path).name == "raw_inventory.jsonl":
            jsonl_writes["count"] += 1
        return real_write_jsonl(path, records)

    monkeypatch.setattr(inventory, "atomic_write_jsonl", counting_write_jsonl)

    result = runner.invoke(
        inventory.app,
        [
            "--library-root",
            str(library_root),
            "--output",
            str(output_path),
            "--mode",
            "full",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert len(read_jsonl(output_path)) == 2
    # One write per PDF (2) plus the final canonical write (>= 3): output is not written once.
    assert jsonl_writes["count"] >= 3


def test_inventory_resumes_from_partial_output(tmp_path: Path) -> None:
    """An interrupted run leaves a partial output that --mode incremental reuses."""
    inventory = load_inventory_module()

    library_root = tmp_path / "library"
    make_one_page_pdf(library_root / "Piece A" / "Flute 1.pdf")
    make_one_page_pdf(library_root / "Piece B" / "Oboe 1.pdf")

    output_path = tmp_path / "data" / "raw_inventory.jsonl"

    # Simulate a crash after only the first PDF was persisted: a one-record output file.
    full = runner.invoke(
        inventory.app,
        ["--library-root", str(library_root), "--output", str(output_path), "--mode", "full"],
    )
    assert full.exit_code == 0, full.stdout
    all_records = read_jsonl(output_path)
    inventory.atomic_write_jsonl(output_path, all_records[:1])

    # Resuming in incremental mode reuses the surviving record and fills in the rest.
    resumed = runner.invoke(
        inventory.app,
        [
            "--library-root",
            str(library_root),
            "--output",
            str(output_path),
            "--mode",
            "incremental",
        ],
    )
    assert resumed.exit_code == 0, resumed.stdout
    resumed_records = read_jsonl(output_path)
    assert {r["pdf_path"] for r in resumed_records} == {
        "Piece A/Flute 1.pdf",
        "Piece B/Oboe 1.pdf",
    }


def test_only_piece_recomputes_target_and_preserves_others(tmp_path: Path) -> None:
    """--only-piece force-recomputes the targeted catalogue and leaves other pieces untouched."""
    inventory = load_inventory_module()

    library_root = tmp_path / "library"
    make_one_page_pdf(library_root / "368 Czardas" / "Solo Clarinet.pdf")
    make_one_page_pdf(library_root / "500 Waltz" / "Flute 1.pdf")

    output_path = tmp_path / "data" / "raw_inventory.jsonl"
    first = runner.invoke(
        inventory.app,
        ["--library-root", str(library_root), "--output", str(output_path), "--mode", "full"],
    )
    assert first.exit_code == 0, first.stdout

    # Tag every prior record with a sentinel so we can tell reuse (kept) from recompute (dropped).
    records = read_jsonl(output_path)
    for rec in records:
        rec["_sentinel"] = True
    inventory.atomic_write_jsonl(output_path, records)

    scoped = runner.invoke(
        inventory.app,
        [
            "--library-root",
            str(library_root),
            "--output",
            str(output_path),
            "--mode",
            "full",
            "--only-piece",
            "368",
        ],
    )
    assert scoped.exit_code == 0, scoped.stdout

    by_path = {r["pdf_path"]: r for r in read_jsonl(output_path)}
    # The targeted piece was recomputed from scratch, so the sentinel is gone.
    assert "_sentinel" not in by_path["368 Czardas/Solo Clarinet.pdf"]
    # The untouched piece was preserved verbatim, sentinel and all.
    assert by_path["500 Waltz/Flute 1.pdf"].get("_sentinel") is True

