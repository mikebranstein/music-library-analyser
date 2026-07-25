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
    output_documents = tmp_path / "data" / "documents.jsonl"
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
            "--output-documents",
            str(output_documents),
            "--cache-dir",
            str(cache_dir),
            "--mode",
            "full",
        ],
    )
    assert full_result.exit_code == 0, full_result.stdout

    text_records = read_jsonl(output_text)
    page_records = read_jsonl(output_pages)
    document_records = read_jsonl(output_documents)
    assert len(text_records) == 1
    assert len(page_records) == 1
    assert len(document_records) == 1

    text_rec = text_records[0]
    assert text_rec["pdf_path"] == "Piece A/Flute 1.pdf"
    assert text_rec["page_num"] == 1
    assert "FLUTE" in (text_rec["embedded_text"] or "")
    assert text_rec["text_is_searchable"] is True
    assert text_rec["needs_ocr"] is False
    assert text_rec["word_count"] >= 2
    assert 0.0 < text_rec["alnum_ratio"] <= 1.0
    assert isinstance(text_rec["page_text_hash"], str) and text_rec["page_text_hash"]
    assert isinstance(text_rec["header_text_candidates"], list)
    assert "FLUTE 1" in " ".join(text_rec["header_text_candidates"])

    page_rec = page_records[0]
    assert page_rec["render_width_px"] > 0
    assert page_rec["render_height_px"] > 0
    assert page_rec["thumbnail_path"] is not None
    assert page_rec["processing_status"] == "success"
    assert page_rec["page_width_pt"] > 0
    assert page_rec["page_height_pt"] > 0
    assert page_rec["orientation"] in {"portrait", "landscape"}
    assert page_rec["is_image_based"] is False

    doc_rec = document_records[0]
    assert doc_rec["pdf_path"] == "Piece A/Flute 1.pdf"
    assert doc_rec["page_count"] == 1
    assert doc_rec["pages_with_text"] == 1
    assert doc_rec["pages_needing_ocr"] == 0
    assert doc_rec["processing_status"] == "success"

    thumb = tmp_path / page_rec["thumbnail_path"]
    assert thumb.exists()

    checkpoint = read_json(output_text.parent / ".extraction_checkpoint.json")
    assert checkpoint is not None
    assert checkpoint["pdf_count_processed"] == 1
    assert checkpoint["documents_output"] is not None

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
            "--output-documents",
            str(output_documents),
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
    assert len(read_jsonl(output_documents)) == 1


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


def test_text_signals_and_hash():
    extract = load_module("02_extract_text_and_images.py", "extract_signals")
    signals = extract.compute_text_signals("Flute 1  Solo\n\n")
    assert signals["word_count"] == 3
    assert 0.0 < signals["alnum_ratio"] <= 1.0
    # Whitespace-normalized, lowercased hashing is stable.
    assert signals["page_text_hash"] == extract.compute_text_signals("flute 1 solo")[
        "page_text_hash"
    ]
    empty = extract.compute_text_signals(None)
    assert empty["word_count"] == 0
    assert empty["alnum_ratio"] == 0.0


def test_gray_metrics_degrade_without_numpy():
    extract = load_module("02_extract_text_and_images.py", "extract_metrics")
    metrics = extract.compute_gray_metrics(bytes([0] * 16), 4, 4, enable_image_metrics=True)
    assert 0.0 <= metrics["text_density"] <= 1.0
    assert 0.0 <= metrics["black_white_ratio"] <= 1.0
    # Metrics are either computed (numpy present) or None (fallback); never raise.
    assert metrics["contrast_std"] is None or metrics["contrast_std"] >= 0.0
    assert metrics["blur_variance"] is None or metrics["blur_variance"] >= 0.0


def test_markdown_report_written(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    extract = load_module("02_extract_text_and_images.py", "extract_report")

    library_root = tmp_path / "library"
    make_text_pdf(library_root / "Piece A" / "Flute 1.pdf", "FLUTE 1")

    inventory_path = tmp_path / "data" / "raw_inventory.jsonl"
    build_inventory(library_root, inventory_path)

    output_report = tmp_path / "data" / "extraction_report.md"
    result = runner.invoke(
        extract.app,
        [
            "--library-root",
            str(library_root),
            "--inventory",
            str(inventory_path),
            "--output-text",
            str(tmp_path / "data" / "extracted_text.jsonl"),
            "--output-pages",
            str(tmp_path / "data" / "pages.jsonl"),
            "--output-documents",
            str(tmp_path / "data" / "documents.jsonl"),
            "--output-report",
            str(output_report),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--mode",
            "full",
        ],
    )
    assert result.exit_code == 0, result.stdout

    assert output_report.exists()
    report = output_report.read_text(encoding="utf-8")
    assert report.startswith("# Extraction Report")
    for heading in (
        "## At a Glance",
        "## Text Coverage",
        "## Attention Needed",
        "## Per-Folder Breakdown",
        "## Per-Document Detail",
        "## Configuration and Environment",
    ):
        assert heading in report
    assert "Piece A/Flute 1.pdf" in report


def test_no_report_flag(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    extract = load_module("02_extract_text_and_images.py", "extract_noreport")

    library_root = tmp_path / "library"
    make_text_pdf(library_root / "Piece A" / "Flute 1.pdf", "FLUTE 1")

    inventory_path = tmp_path / "data" / "raw_inventory.jsonl"
    build_inventory(library_root, inventory_path)

    output_report = tmp_path / "data" / "extraction_report.md"
    result = runner.invoke(
        extract.app,
        [
            "--library-root",
            str(library_root),
            "--inventory",
            str(inventory_path),
            "--output-text",
            str(tmp_path / "data" / "extracted_text.jsonl"),
            "--output-pages",
            str(tmp_path / "data" / "pages.jsonl"),
            "--output-documents",
            str(tmp_path / "data" / "documents.jsonl"),
            "--output-report",
            str(output_report),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--mode",
            "full",
            "--no-report",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert not output_report.exists()