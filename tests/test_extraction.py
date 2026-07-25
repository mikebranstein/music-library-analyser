from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import fitz
from typer.testing import CliRunner

from scripts._common import read_json, read_jsonl


runner = CliRunner()


def load_module(script_name: str, module_name: str):
    script_path = Path(__file__).resolve().parents[1] / "scripts" / script_name
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load {script_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_text_pdf(pdf_path: Path, text: str = "FLUTE 1") -> None:
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=24)
    doc.save(pdf_path)
    doc.close()


def build_inventory(library_root: Path, output_path: Path) -> None:
    """Run Script 01 to produce a real inventory for Script 02 to consume."""
    inventory = load_module("01_inventory.py", "inventory_for_extract")
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


def test_compute_features_extremes():
    extract = load_module("02_extract_text_and_images.py", "extract_features")
    black_density, black_bw = extract.compute_features_from_gray(bytes([0] * 100))
    white_density, white_bw = extract.compute_features_from_gray(bytes([255] * 100))
    assert black_density == 1.0
    assert black_bw == 1.0
    assert white_density == 0.0
    assert white_bw == 1.0


def test_extraction_full_and_incremental(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    extract = load_module("02_extract_text_and_images.py", "extract_run")

    library_root = tmp_path / "library"
    make_text_pdf(library_root / "Piece A" / "Flute 1.pdf", "FLUTE 1")

    inventory_path = tmp_path / "data" / "raw_inventory.jsonl"
    build_inventory(library_root, inventory_path)

    output_text = tmp_path / "data" / "extracted_text.jsonl"
    output_pages = tmp_path / "data" / "pages.jsonl"
    cache_dir = tmp_path / "cache"

    full_result = runner.invoke(
        extract.app,
        [
            "--library-root",
            str(library_root),
            "--inventory",
            str(inventory_path),
            "--output-text",
            str(output_text),
            "--output-pages",
            str(output_pages),
            "--cache-dir",
            str(cache_dir),
            "--mode",
            "full",
        ],
    )
    assert full_result.exit_code == 0, full_result.stdout

    text_records = read_jsonl(output_text)
    page_records = read_jsonl(output_pages)
    assert len(text_records) == 1
    assert len(page_records) == 1

    text_rec = text_records[0]
    assert text_rec["pdf_path"] == "Piece A/Flute 1.pdf"
    assert text_rec["page_num"] == 1
    assert "FLUTE" in (text_rec["embedded_text"] or "")
    assert text_rec["text_is_searchable"] is True
    assert text_rec["needs_ocr"] is False

    page_rec = page_records[0]
    assert page_rec["render_width_px"] > 0
    assert page_rec["render_height_px"] > 0
    assert page_rec["thumbnail_path"] is not None
    assert page_rec["processing_status"] == "success"

    thumb = tmp_path / page_rec["thumbnail_path"]
    assert thumb.exists()

    checkpoint = read_json(output_text.parent / ".extraction_checkpoint.json")
    assert checkpoint is not None
    assert checkpoint["pdf_count_processed"] == 1

    incremental_result = runner.invoke(
        extract.app,
        [
            "--library-root",
            str(library_root),
            "--inventory",
            str(inventory_path),
            "--output-text",
            str(output_text),
            "--output-pages",
            str(output_pages),
            "--cache-dir",
            str(cache_dir),
            "--mode",
            "incremental",
        ],
    )
    assert incremental_result.exit_code == 0, incremental_result.stdout

    checkpoint_after = read_json(output_text.parent / ".extraction_checkpoint.json")
    assert checkpoint_after["reused_pdf_count"] == 1
    assert checkpoint_after["pdf_count_processed"] == 0

    # Output content should be stable across the reuse path.
    assert len(read_jsonl(output_text)) == 1
    assert len(read_jsonl(output_pages)) == 1


def test_no_rendering_flag(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    extract = load_module("02_extract_text_and_images.py", "extract_norender")

    library_root = tmp_path / "library"
    make_text_pdf(library_root / "Piece B" / "Oboe.pdf", "OBOE")

    inventory_path = tmp_path / "data" / "raw_inventory.jsonl"
    build_inventory(library_root, inventory_path)

    output_text = tmp_path / "data" / "extracted_text.jsonl"
    output_pages = tmp_path / "data" / "pages.jsonl"

    result = runner.invoke(
        extract.app,
        [
            "--library-root",
            str(library_root),
            "--inventory",
            str(inventory_path),
            "--output-text",
            str(output_text),
            "--output-pages",
            str(output_pages),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--mode",
            "full",
            "--no-rendering",
        ],
    )
    assert result.exit_code == 0, result.stdout

    page_rec = read_jsonl(output_pages)[0]
    assert page_rec["thumbnail_path"] is None
    assert page_rec["render_width_px"] == -1
