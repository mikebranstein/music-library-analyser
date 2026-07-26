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


def test_extract_identity_candidates():
    extract = load_module("02_extract_text_and_images.py", "extract_identity")
    page1 = (
        "Sample March\n"
        "by John Composer\n"
        "Arr. Jane Arranger\n"
        "\u00a9 2019 Acme Music Publishing\n"
        "All Rights Reserved\n"
    )
    identity = extract.extract_identity_candidates([page1])
    assert identity["copyright_year"] == 2019
    assert identity["arranger"] == "Jane Arranger"
    assert identity["composer"] == "John Composer"
    assert identity["publisher"] == "Acme Music Publishing"
    assert "\u00a9" in identity["copyright_line"]

    # No identity signals => all None, never raises.
    empty = extract.extract_identity_candidates(["Just some page text."])
    assert empty["copyright_line"] is None
    assert empty["copyright_year"] is None


def test_extraction_persists_incrementally_during_run(tmp_path: Path, monkeypatch):
    """The three JSONLs + checkpoint are written per PDF, not only once at the end."""
    monkeypatch.chdir(tmp_path)
    extract = load_module("02_extract_text_and_images.py", "extract_incremental_persist")

    library_root = tmp_path / "library"
    make_text_pdf(library_root / "Piece A" / "Flute 1.pdf", "FLUTE 1")
    make_text_pdf(library_root / "Piece B" / "Oboe 1.pdf", "OBOE 1")

    inventory_path = tmp_path / "data" / "raw_inventory.jsonl"
    build_inventory(library_root, inventory_path)

    output_text = tmp_path / "data" / "extracted_text.jsonl"

    text_writes = {"count": 0}
    real_write_jsonl = extract.atomic_write_jsonl

    def counting_write_jsonl(path, records):
        if Path(path).name == "extracted_text.jsonl":
            text_writes["count"] += 1
        return real_write_jsonl(path, records)

    monkeypatch.setattr(extract, "atomic_write_jsonl", counting_write_jsonl)

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
            str(tmp_path / "data" / "pages.jsonl"),
            "--output-documents",
            str(tmp_path / "data" / "documents.jsonl"),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--mode",
            "full",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert len(read_jsonl(output_text)) == 2
    # One write per PDF (2) plus the final canonical write (>= 3).
    assert text_writes["count"] >= 3


def test_extraction_resumes_after_partial_run(tmp_path: Path, monkeypatch):
    """A run interrupted after one PDF resumes: the done PDF is reused, the rest processed."""
    monkeypatch.chdir(tmp_path)
    extract = load_module("02_extract_text_and_images.py", "extract_resume")

    library_root = tmp_path / "library"
    make_text_pdf(library_root / "Piece A" / "Flute 1.pdf", "FLUTE 1")
    make_text_pdf(library_root / "Piece B" / "Oboe 1.pdf", "OBOE 1")

    inventory_path = tmp_path / "data" / "raw_inventory.jsonl"
    build_inventory(library_root, inventory_path)

    output_text = tmp_path / "data" / "extracted_text.jsonl"
    output_pages = tmp_path / "data" / "pages.jsonl"
    output_documents = tmp_path / "data" / "documents.jsonl"
    cache_dir = tmp_path / "cache"
    args = [
        "--library-root", str(library_root),
        "--inventory", str(inventory_path),
        "--output-text", str(output_text),
        "--output-pages", str(output_pages),
        "--output-documents", str(output_documents),
        "--cache-dir", str(cache_dir),
    ]

    # Full run produces both PDFs; then simulate a crash after only "Piece A" was persisted by
    # trimming every output + the checkpoint fingerprints down to that first PDF.
    full = runner.invoke(extract.app, [*args, "--mode", "full"])
    assert full.exit_code == 0, full.stdout
    keep = "Piece A/Flute 1.pdf"
    for path in (output_text, output_pages, output_documents):
        extract.atomic_write_jsonl(
            path, [r for r in read_jsonl(path) if r.get("pdf_path") == keep]
        )
    ckpt_path = output_text.parent / ".extraction_checkpoint.json"
    checkpoint = read_json(ckpt_path)
    checkpoint["fingerprints"] = {
        k: v for k, v in checkpoint["fingerprints"].items() if k == keep
    }
    extract.atomic_write_json(ckpt_path, checkpoint)

    resumed = runner.invoke(extract.app, [*args, "--mode", "incremental"])
    assert resumed.exit_code == 0, resumed.stdout

    after = read_json(ckpt_path)
    assert after["reused_pdf_count"] == 1
    assert after["pdf_count_processed"] == 1
    assert {r["pdf_path"] for r in read_jsonl(output_documents)} == {
        "Piece A/Flute 1.pdf",
        "Piece B/Oboe 1.pdf",
    }



def test_detect_staves_projection():
    extract = load_module("02_extract_text_and_images.py", "extract_staves")
    if extract.np is None:
        import pytest

        pytest.skip("numpy unavailable; staff detection degrades to None")
    width, height = 200, 100
    buf = bytearray([255] * (width * height))
    for row in (10, 20, 30, 40, 50):
        start = row * width
        buf[start : start + width] = bytes([0] * width)
    result = extract.detect_staves(bytes(buf), width, height)
    assert result["staff_line_count"] == 5
    assert result["has_staves"] is True

    blank = extract.detect_staves(bytes([255] * (width * height)), width, height)
    assert blank["staff_line_count"] == 0
    assert blank["has_staves"] is False


def test_detect_staves_degrades_without_shape():
    extract = load_module("02_extract_text_and_images.py", "extract_staves_none")
    # Mismatched dimensions cannot reshape => None, never raises.
    result = extract.detect_staves(bytes([0] * 10), -1, -1)
    assert result["has_staves"] is None
    assert result["staff_line_count"] is None


def test_zone_blank_and_staff_full_run(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    extract = load_module("02_extract_text_and_images.py", "extract_wave2")

    library_root = tmp_path / "library"
    pdf_path = library_root / "Piece M" / "Score.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "MARCHING SONG", fontsize=24)
    page.insert_text((72, 760), "\u00a9 2019 Acme Music Publishing", fontsize=10)
    for y in range(200, 260, 10):
        page.draw_line(fitz.Point(60, y), fitz.Point(552, y), width=1.5)
    doc.new_page(width=612, height=792)  # blank second page
    doc.save(pdf_path)
    doc.close()

    inventory_path = tmp_path / "data" / "raw_inventory.jsonl"
    build_inventory(library_root, inventory_path)

    output_text = tmp_path / "data" / "extracted_text.jsonl"
    output_pages = tmp_path / "data" / "pages.jsonl"
    output_documents = tmp_path / "data" / "documents.jsonl"
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
            "--output-documents",
            str(output_documents),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--mode",
            "full",
            "--no-ocr",
        ],
    )
    assert result.exit_code == 0, result.stdout

    text_records = read_jsonl(output_text)
    page_records = read_jsonl(output_pages)
    doc_rec = read_jsonl(output_documents)[0]

    page1_text = next(r for r in text_records if r["page_num"] == 1)
    # Zone fields exist; the title sits in the top band.
    for key in (
        "zone_top_left",
        "zone_top_center",
        "zone_top_right",
        "zone_bottom",
    ):
        assert key in page1_text
    top_text = " ".join(
        v or ""
        for v in (
            page1_text["zone_top_left"],
            page1_text["zone_top_center"],
            page1_text["zone_top_right"],
        )
    )
    assert "MARCHING SONG" in top_text

    page_recs = {r["page_num"]: r for r in page_records}
    # Blank second page is flagged; music page has staves detected.
    assert page_recs[2]["is_blank"] is True
    if extract.np is not None:
        assert page_recs[1]["has_staves"] is True
        assert page_recs[1]["staff_line_count"] >= 5
        assert doc_rec["music_page_count"] >= 1
    assert doc_rec["blank_page_count"] >= 1

    # Identity candidate parsed from the copyright line.
    identity = doc_rec["identity_candidates"]
    assert identity["copyright_year"] == 2019
    assert doc_rec["record_version"] == "2.2"


def _tesseract_or_skip(extract):
    """Resolve Tesseract for OCR tests, skipping when it (or pytesseract) is absent."""
    import pytest

    if extract.pytesseract is None or extract.Image is None:
        pytest.skip("pytesseract/Pillow not installed")
    cmd = extract.resolve_tesseract_cmd(None)
    if cmd is None:
        pytest.skip("Tesseract binary not available")
    extract.pytesseract.pytesseract.tesseract_cmd = cmd
    return cmd


def test_resolve_tesseract_cmd_explicit(tmp_path: Path):
    extract = load_module("02_extract_text_and_images.py", "extract_resolve")
    fake = tmp_path / "tesseract.exe"
    fake.write_bytes(b"stub")
    assert extract.resolve_tesseract_cmd(str(fake)) == str(fake)
    # A non-existent explicit path falls back to PATH/common dirs (or None).
    result = extract.resolve_tesseract_cmd(str(tmp_path / "missing.exe"))
    assert result is None or Path(result).exists()


def test_run_ocr_reads_text():
    extract = load_module("02_extract_text_and_images.py", "extract_run_ocr")
    _tesseract_or_skip(extract)
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 120), "HELLO OCR WORLD", fontsize=36)
    cfg = extract.OcrConfig(enabled=True, dpi=300, lang="eng")
    result = extract.run_ocr(page, cfg)
    doc.close()
    assert result["ocr_status"] == "success"
    assert "HELLO" in (result["ocr_text"] or "").upper()
    assert result["ocr_word_count"] and result["ocr_word_count"] >= 3
    assert result["ocr_confidence"] is None or 0.0 <= result["ocr_confidence"] <= 1.0


def test_ocr_full_run_recovers_scanned_text(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    extract = load_module("02_extract_text_and_images.py", "extract_ocr_full")
    _tesseract_or_skip(extract)
    from PIL import Image, ImageDraw, ImageFont

    # Build a rasterized "scanned" page: an image of text with no text layer.
    img = Image.new("RGB", (1600, 400), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default(size=120)
    except TypeError:  # pragma: no cover - older Pillow
        font = ImageFont.load_default()
    draw.text((40, 120), "TROMBONE PART", fill="black", font=font)
    img_path = tmp_path / "scan.png"
    img.save(img_path)

    library_root = tmp_path / "library"
    pdf_path = library_root / "Piece S" / "Scan.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_image(fitz.Rect(40, 40, 572, 200), filename=str(img_path))
    doc.save(pdf_path)
    doc.close()

    inventory_path = tmp_path / "data" / "raw_inventory.jsonl"
    build_inventory(library_root, inventory_path)

    output_text = tmp_path / "data" / "extracted_text.jsonl"
    output_pages = tmp_path / "data" / "pages.jsonl"
    output_documents = tmp_path / "data" / "documents.jsonl"
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
            "--output-documents",
            str(output_documents),
            "--cache-dir",
            str(tmp_path / "cache"),
            "--mode",
            "full",
            "--ocr",
        ],
    )
    assert result.exit_code == 0, result.stdout

    text_rec = read_jsonl(output_text)[0]
    doc_rec = read_jsonl(output_documents)[0]
    assert text_rec["ocr_applied"] is True
    assert text_rec["text_source"] == "ocr"
    assert "TROMBONE" in (text_rec["ocr_text"] or "").upper()
    assert doc_rec["pages_ocr_recovered"] >= 1
    assert doc_rec["ocr_char_count"] > 0

    # OCR result is cached; a rerun should reuse it without a live OCR call.
    cache_files = list((tmp_path / "cache" / "ocr").glob("*.json"))
    assert cache_files