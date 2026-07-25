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
