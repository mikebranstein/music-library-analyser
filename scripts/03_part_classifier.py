"""Script 03: classify each PDF's instrument/part and whether it is a score.

Consumes the Script 01 inventory and the Script 02 extraction datasets and writes one
prediction record per readable PDF to ``data/part_predictions.jsonl`` plus a Markdown report.

The classifier is rule-first and deterministic: the filename is the primary signal (its part
segment is isolated by stripping the known ``piece_folder`` prefix), and Script 02 page text is
used to confirm or recover a label. An instrument lexicon lives in ``config/regex_rules.yaml``
(with a built-in fallback so the script runs even without the file or PyYAML).
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None

from scripts._common import (
    atomic_write_json,
    atomic_write_jsonl,
    atomic_write_text,
    build_checkpoint,
    load_checkpoint,
    make_checkpoint_path,
    md_cell,
    pct,
    read_jsonl,
    setup_logging,
    utc_now_iso,
)

RECORD_VERSION = "1.1"

CHECKPOINT_FILENAME = ".part_classifier_checkpoint.json"

app = typer.Typer(add_completion=False)

logger = logging.getLogger("script03.classify")

# --- Built-in lexicon (authoritative default; YAML overrides/extends it) ---------------------

DEFAULT_LEXICON: dict[str, Any] = {
    "families": {
        "piccolo": "woodwind", "flute": "woodwind", "oboe": "woodwind",
        "english_horn": "woodwind", "bassoon": "woodwind", "eb_clarinet": "woodwind",
        "clarinet": "woodwind", "alto_clarinet": "woodwind", "bass_clarinet": "woodwind",
        "contrabass_clarinet": "woodwind", "soprano_sax": "woodwind", "alto_sax": "woodwind",
        "tenor_sax": "woodwind", "baritone_sax": "woodwind", "bass_sax": "woodwind",
        "soprano_cornet": "brass", "cornet": "brass", "trumpet": "brass",
        "flugelhorn": "brass", "horn": "brass", "tenor_horn": "brass", "trombone": "brass",
        "bass_trombone": "brass", "baritone_horn": "brass", "euphonium": "brass", "tuba": "brass",
        "string_bass": "strings", "timpani": "percussion", "mallet_percussion": "percussion",
        "snare_drum": "percussion", "bass_drum": "percussion", "cymbals": "percussion",
        "percussion": "percussion", "drum_set": "percussion",
    },
    "instruments": {
        "piccolo": ["piccolo", "picc"],
        "flute": ["flute", "flutes", "fl", "c flute"],
        "oboe": ["oboe", "ob"],
        "english_horn": ["english horn", "cor anglais"],
        "bassoon": ["bassoon", "bsn", "fagotto"],
        "eb_clarinet": ["eb clarinet", "e flat clarinet", "clarinet in eb", "clarinet in e flat",
                        "clarinet eb"],
        "clarinet": ["bb clarinet", "b flat clarinet", "clarinet in bb", "clarinet", "clar", "cl"],
        "alto_clarinet": ["alto clarinet", "eb alto clarinet", "clarinet eb alto", "clarinet alto"],
        "bass_clarinet": ["bass clarinet", "b cl", "bass cl"],
        "contrabass_clarinet": [
            "contrabass clarinet", "contra bass clarinet",
            "contra alto clarinet", "contralto clarinet",
        ],
        "soprano_sax": ["soprano saxophone", "soprano sax", "sop sax"],
        "alto_sax": ["alto saxophone", "alto sax", "eb alto sax", "e flat alto saxophone"],
        "tenor_sax": ["tenor saxophone", "tenor sax", "bb tenor sax"],
        "baritone_sax": [
            "baritone saxophone", "baritone sax", "bari sax", "eb baritone saxophone",
        ],
        "bass_sax": ["bass saxophone", "bass sax"],
        "soprano_cornet": ["soprano cornet", "eb soprano cornet", "sop cornet", "eb cornet"],
        "cornet": ["solo cornet", "repiano cornet", "ripieno cornet", "bb cornet", "cornet", "cornets"],
        "trumpet": ["bb trumpet", "b flat trumpet", "trumpet", "trumpets", "tpt", "tpts"],
        "flugelhorn": ["flugelhorn", "flugel horn", "flugel", "fluegelhorn"],
        "horn": ["horn in f", "f horn", "french horn", "horn", "horns", "hn"],
        "tenor_horn": ["tenor horn", "eb tenor horn", "eb horn", "alto horn"],
        "trombone": ["tenor trombone", "trombone", "trombones", "tbn", "tbns", "tbne"],
        "bass_trombone": ["bass trombone", "bass tbn"],
        "baritone_horn": ["baritone horn", "baritone", "bari horn"],
        "euphonium": ["euphonium", "euph", "eupho"],
        "tuba": [
            "tuba", "tubas", "basses", "bass tuba", "eb bass", "bb bass",
            "bbb", "sousaphone", "contrabass tuba",
        ],
        "string_bass": [
            "string bass", "double bass", "contrabass", "acoustic bass", "upright bass",
        ],
        "timpani": ["timpani", "timp", "timpano", "kettle drums"],
        "mallet_percussion": [
            "mallet percussion", "mallets", "xylophone", "glockenspiel", "bells",
            "orchestra bells", "vibraphone", "vibes", "marimba", "chimes", "tubular bells",
        ],
        "snare_drum": ["snare drum", "snare"],
        "bass_drum": ["bass drum"],
        "cymbals": ["cymbals", "crash cymbals", "suspended cymbal"],
        "percussion": ["percussion", "perc", "battery", "aux percussion", "auxiliary percussion"],
        "drum_set": ["drum set", "drum kit", "drums", "trap set"],
    },
    "clef_markers": {
        "(bc)": "bass", "(tc)": "treble", "(bass)": "bass", "(treble)": "treble",
        "bass clef": "bass", "treble clef": "treble",
    },
    "transposition_markers": {
        "in f": "F", "in bb": "Bb", "in b flat": "Bb", "in eb": "Eb",
        "in e flat": "Eb", "in c": "C", "in a": "A", "in d": "D",
    },
    "score_keywords": [
        ["full score", "full"], ["condensed score", "condensed"], ["short score", "short"],
        ["conductor score", "conductor"], ["conductor", "conductor"], ["score", "full"],
    ],
    "separators": ["-", "_"],
    # Coarser grouping than family; overridable via YAML. section -> [canonical instruments].
    "sections": {
        "flutes": ["piccolo", "flute"],
        "double_reeds": ["oboe", "english_horn", "bassoon"],
        "clarinets": [
            "eb_clarinet", "clarinet", "alto_clarinet", "bass_clarinet", "contrabass_clarinet",
        ],
        "saxophones": [
            "soprano_sax", "alto_sax", "tenor_sax", "baritone_sax", "bass_sax",
        ],
        "cornets_trumpets": ["soprano_cornet", "cornet", "trumpet", "flugelhorn"],
        "horns": ["horn", "tenor_horn"],
        "low_brass": ["trombone", "bass_trombone", "baritone_horn", "euphonium"],
        "tubas": ["tuba"],
        "strings": ["string_bass"],
        "percussion": [
            "timpani", "mallet_percussion", "snare_drum", "bass_drum", "cymbals",
            "percussion", "drum_set",
        ],
    },
}

# Display names used to compose the human-readable ``predicted_part`` label.
INSTRUMENT_DISPLAY: dict[str, str] = {
    "piccolo": "Piccolo", "flute": "Flute", "oboe": "Oboe", "english_horn": "English Horn",
    "bassoon": "Bassoon", "eb_clarinet": "Eb Clarinet", "clarinet": "Clarinet",
    "alto_clarinet": "Alto Clarinet", "bass_clarinet": "Bass Clarinet",
    "contrabass_clarinet": "Contrabass Clarinet", "soprano_sax": "Soprano Saxophone",
    "alto_sax": "Alto Saxophone", "tenor_sax": "Tenor Saxophone",
    "baritone_sax": "Baritone Saxophone", "bass_sax": "Bass Saxophone",
    "soprano_cornet": "Soprano Cornet", "cornet": "Cornet", "trumpet": "Trumpet",
    "flugelhorn": "Flugelhorn", "horn": "Horn", "tenor_horn": "Tenor Horn",
    "trombone": "Trombone", "bass_trombone": "Bass Trombone", "baritone_horn": "Baritone",
    "euphonium": "Euphonium", "tuba": "Tuba", "string_bass": "String Bass",
    "timpani": "Timpani", "mallet_percussion": "Mallet Percussion", "snare_drum": "Snare Drum",
    "bass_drum": "Bass Drum", "cymbals": "Cymbals", "percussion": "Percussion",
    "drum_set": "Drum Set",
}

SCORE_DISPLAY: dict[str, str] = {
    "full": "Full Score", "condensed": "Condensed Score",
    "short": "Short Score", "conductor": "Conductor",
}

CLEF_ABBREV: dict[str, str] = {"bass": "BC", "treble": "TC"}

HIGH_CONFIDENCE = 0.90
LOW_CONFIDENCE = 0.75

# Deterministic ordering used to build ``part_sort_key`` (conventional score order).
INSTRUMENT_ORDER: dict[str, int] = {
    canonical: i for i, canonical in enumerate(DEFAULT_LEXICON["families"])
}
SCORE_TYPE_ORDER: dict[str, int] = {"full": 0, "condensed": 1, "short": 2, "conductor": 3}
CLEF_ORDER: dict[str, int] = {"treble": 1, "bass": 2}


# --- Lexicon loading -------------------------------------------------------------------------


def load_lexicon(rules_path: Path) -> tuple[dict[str, Any], str]:
    """Return (lexicon, source) merging YAML overrides over the built-in defaults."""
    lexicon = {
        "families": dict(DEFAULT_LEXICON["families"]),
        "instruments": {k: list(v) for k, v in DEFAULT_LEXICON["instruments"].items()},
        "clef_markers": dict(DEFAULT_LEXICON["clef_markers"]),
        "transposition_markers": dict(DEFAULT_LEXICON["transposition_markers"]),
        "score_keywords": [list(pair) for pair in DEFAULT_LEXICON["score_keywords"]],
        "separators": list(DEFAULT_LEXICON["separators"]),
        "sections": {k: list(v) for k, v in DEFAULT_LEXICON["sections"].items()},
    }
    if yaml is None:
        logger.warning("PyYAML unavailable; using built-in instrument lexicon.")
        return lexicon, "builtin"
    if not rules_path.exists():
        logger.warning("Rules file %s not found; using built-in lexicon.", rules_path)
        return lexicon, "builtin"
    try:
        with rules_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Failed to parse %s (%s); using built-in lexicon.", rules_path, exc)
        return lexicon, "builtin"

    if isinstance(data.get("families"), dict):
        lexicon["families"].update(data["families"])
    if isinstance(data.get("instruments"), dict):
        for canonical, aliases in data["instruments"].items():
            if isinstance(aliases, list):
                lexicon["instruments"][canonical] = list(aliases)
    if isinstance(data.get("clef_markers"), dict):
        lexicon["clef_markers"].update(data["clef_markers"])
    if isinstance(data.get("transposition_markers"), dict):
        lexicon["transposition_markers"].update(data["transposition_markers"])
    if isinstance(data.get("score_keywords"), list) and data["score_keywords"]:
        lexicon["score_keywords"] = [list(pair) for pair in data["score_keywords"]]
    if isinstance(data.get("separators"), list) and data["separators"]:
        lexicon["separators"] = list(data["separators"])
    if isinstance(data.get("sections"), dict) and data["sections"]:
        for section, canonicals in data["sections"].items():
            if isinstance(canonicals, list):
                lexicon["sections"][section] = list(canonicals)
    return lexicon, "yaml"


def compile_aliases(lexicon: dict[str, Any]) -> list[tuple[str, str]]:
    """Return (canonical, normalized_alias) pairs sorted by alias length descending."""
    entries: list[tuple[str, str]] = []
    for canonical, aliases in lexicon["instruments"].items():
        for alias in aliases:
            entries.append((canonical, normalize(alias)))
    entries.sort(key=lambda e: len(e[1]), reverse=True)
    return entries


def build_section_map(lexicon: dict[str, Any]) -> dict[str, str]:
    """Invert the lexicon ``sections`` mapping into canonical_instrument -> section."""
    mapping: dict[str, str] = {}
    for section, canonicals in (lexicon.get("sections") or {}).items():
        if isinstance(canonicals, list):
            for canonical in canonicals:
                mapping[canonical] = section
    return mapping


def section_for(canonical: str | None, family: str, section_map: dict[str, str]) -> str:
    """Coarser grouping than family; falls back to family, then score/unknown."""
    if canonical is None:
        return "score" if family == "score" else "unknown"
    return section_map.get(canonical, family)


def confidence_tier(confidence: float) -> str:
    """Map the deterministic confidence float onto the master-plan decision tiers."""
    if confidence >= HIGH_CONFIDENCE:
        return "high"
    if confidence >= LOW_CONFIDENCE:
        return "medium"
    if confidence > 0.0:
        return "low"
    return "none"


def compute_part_sort_key(
    canonical: str | None,
    part_index: int | None,
    clef: str | None,
    is_score: bool,
    score_type: str | None,
) -> str:
    """Stable lexically-sortable key for conventional score order (scores first)."""
    if is_score:
        return f"0-{SCORE_TYPE_ORDER.get(score_type or 'full', 9):02d}"
    instr_rank = INSTRUMENT_ORDER.get(canonical, 999) if canonical else 999
    idx = part_index if part_index is not None else 0
    clef_rank = CLEF_ORDER.get(clef or "", 0)
    return f"1-{instr_rank:03d}-{idx:02d}-{clef_rank}"


def parse_piece_identity(
    piece_folder: str, pdf_filename: str
) -> tuple[str | None, str | None]:
    """Extract a catalog number and best-effort piece title from the folder/filename."""
    base = (piece_folder or "").split("/")[-1].strip()
    if not base:
        base = Path(pdf_filename or "").stem.strip()
    match = re.match(r"^(\d{1,5})[\s._-]+(.*)$", base)
    if match:
        title = match.group(2).strip().strip("-_").strip() or None
        return match.group(1), title
    return None, (base or None)


# --- Text helpers ----------------------------------------------------------------------------


def normalize(text: str) -> str:
    lowered = (text or "").lower()
    lowered = re.sub(r"[-_]", " ", lowered)
    lowered = re.sub(r"\s+", " ", lowered)
    return lowered.strip()


def _word_search(needle: str, haystack: str) -> bool:
    if not needle:
        return False
    pattern = r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])"
    return re.search(pattern, haystack) is not None


def isolate_part_segment(pdf_filename: str, piece_folder: str) -> str:
    """Strip the piece_folder (and any leading catalog number) to isolate the part text."""
    stem = Path(pdf_filename).stem
    folder = piece_folder.split("/")[-1] if piece_folder else ""
    matched = False
    remainder = stem
    if folder and stem.lower().startswith(folder.lower()):
        remainder = stem[len(folder):]
        matched = True
    remainder = remainder.strip().strip("-_").strip()
    if not matched:
        remainder = re.sub(r"^\d{1,5}[\s._-]+", "", remainder).strip().strip("-_").strip()
    if not remainder:
        remainder = stem.strip()
    return remainder


# --- Matching --------------------------------------------------------------------------------


def match_instrument(
    text_norm: str,
    compiled: list[tuple[str, str]],
    min_alias_len: int = 1,
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """Return (canonical, matched_alias, alternates) using longest-alias-wins."""
    matches: list[tuple[str, str]] = []
    for canonical, alias in compiled:
        if len(alias) < min_alias_len:
            continue
        if _word_search(alias, text_norm):
            matches.append((canonical, alias))
    if not matches:
        return None, None, []
    best_canonical, best_alias = matches[0]
    alternates: list[dict[str, Any]] = []
    seen = {best_canonical}
    for canonical, alias in matches:
        if canonical in seen:
            continue
        seen.add(canonical)
        alternates.append(
            {
                "canonical_instrument": canonical,
                "score": round(len(alias) / max(len(best_alias), 1), 3),
            }
        )
        if len(alternates) >= 2:
            break
    return best_canonical, best_alias, alternates


def detect_score(text_norm: str, lexicon: dict[str, Any]) -> str | None:
    for keyword, score_type in lexicon["score_keywords"]:
        if _word_search(normalize(keyword), text_norm):
            return score_type
    return None


def extract_clef(text_norm: str, lexicon: dict[str, Any]) -> str | None:
    for marker, clef in lexicon["clef_markers"].items():
        if marker in text_norm:
            return clef
    return None


def extract_transposition(text_norm: str, lexicon: dict[str, Any]) -> str | None:
    for marker, pitch in lexicon["transposition_markers"].items():
        if _word_search(marker, text_norm):
            return pitch
    return None


def extract_part_index(text_norm: str) -> int | None:
    found = re.findall(r"(?<![a-z0-9])(\d{1,2})(?![a-z0-9])", text_norm)
    if found:
        return int(found[0])
    return None


def compose_label(
    canonical: str | None,
    transposition: str | None,
    part_index: int | None,
    clef: str | None,
    is_score: bool,
    score_type: str | None,
) -> str | None:
    if is_score:
        return SCORE_DISPLAY.get(score_type or "full", "Score")
    if canonical is None:
        return None
    parts = [INSTRUMENT_DISPLAY.get(canonical, canonical.replace("_", " ").title())]
    if transposition and transposition.lower() not in parts[0].lower():
        parts.append(f"in {transposition}")
    if part_index is not None:
        parts.append(str(part_index))
    label = " ".join(parts)
    if clef in CLEF_ABBREV:
        label = f"{label} ({CLEF_ABBREV[clef]})"
    return label


# --- Text signal gathering -------------------------------------------------------------------


def gather_text_signals(
    doc_record: dict[str, Any] | None,
    page1_zones: dict[str, Any] | None,
) -> str:
    """Concatenate the most reliable page-1 text sources for confirmation/recovery."""
    fragments: list[str] = []
    if doc_record:
        header = doc_record.get("first_page_header_candidates") or []
        if isinstance(header, list):
            fragments.extend(str(h) for h in header)
        first_text = doc_record.get("first_page_text") or ""
        # Only the head of the (noisy) page text, where the part label usually sits.
        fragments.append(str(first_text)[:200])
    if page1_zones:
        for key in ("zone_top_left", "zone_top_center", "zone_top_right"):
            value = page1_zones.get(key)
            if value:
                fragments.append(str(value))
    return normalize(" ".join(fragments))


# --- Classification --------------------------------------------------------------------------


def classify_document(
    inv_record: dict[str, Any],
    doc_record: dict[str, Any] | None,
    page1_zones: dict[str, Any] | None,
    lexicon: dict[str, Any],
    compiled: list[tuple[str, str]],
    section_map: dict[str, str],
    run_id: str,
) -> dict[str, Any]:
    pdf_filename = inv_record.get("pdf_filename", "")
    piece_folder = inv_record.get("piece_folder", "")
    catalog_number, piece_title_guess = parse_piece_identity(piece_folder, pdf_filename)
    part_segment = isolate_part_segment(pdf_filename, piece_folder)
    seg_norm = normalize(part_segment)

    score_type = detect_score(seg_norm, lexicon)
    text_norm = gather_text_signals(doc_record, page1_zones)

    clef = extract_clef(seg_norm, lexicon)
    transposition = extract_transposition(seg_norm, lexicon)

    canonical: str | None = None
    matched_alias: str | None = None
    alternates: list[dict[str, Any]] = []
    filename_match = False
    text_match = False
    is_score = False

    if score_type is not None:
        is_score = True
        family = "score"
        confidence = 0.90
        evidence = "filename"
        part_index = None
        clef = None
        transposition = None
    else:
        canonical, matched_alias, alternates = match_instrument(seg_norm, compiled)
        part_index = extract_part_index(seg_norm)
        if canonical is not None:
            filename_match = True
            # Confirm with page text: does any alias of this canonical appear?
            text_match = any(
                _word_search(alias, text_norm)
                for c, alias in compiled
                if c == canonical
            )
            if text_match:
                confidence = 0.90
                if clef or transposition:
                    confidence = min(0.98, confidence + 0.05)
                evidence = "combined"
            else:
                confidence = 0.75
                evidence = "filename"
            family = lexicon["families"].get(canonical, "other")
        else:
            # Filename gave nothing usable; try to recover from page text.
            t_canonical, t_alias, t_alts = match_instrument(
                text_norm, compiled, min_alias_len=4
            )
            if t_canonical is not None:
                canonical = t_canonical
                matched_alias = t_alias
                alternates = t_alts
                text_match = True
                confidence = 0.50
                evidence = "text"
                family = lexicon["families"].get(canonical, "other")
            else:
                confidence = 0.0
                evidence = "none"
                family = "unknown"

    predicted_part = compose_label(
        canonical, transposition, part_index, clef, is_score, score_type
    )
    section = section_for(canonical, family, section_map)
    part_sort_key = compute_part_sort_key(
        canonical, part_index, clef, is_score, score_type
    )
    confidence = round(confidence, 3)
    tier = confidence_tier(confidence)
    # Base review flag; duplicate_in_piece is OR'd in during ensemble harmonization.
    needs_review = bool(
        (not is_score and canonical is None) or confidence < LOW_CONFIDENCE
    )

    return {
        "record_version": RECORD_VERSION,
        "run_id": run_id,
        "pdf_path": inv_record.get("pdf_path"),
        "piece_id": inv_record.get("piece_id"),
        "piece_folder": piece_folder,
        "pdf_filename": pdf_filename,
        "catalog_number": catalog_number,
        "piece_title_guess": piece_title_guess,
        "predicted_part": predicted_part,
        "canonical_instrument": canonical,
        "family": family,
        "section": section,
        "part_index": part_index,
        "clef": clef,
        "transposition": transposition,
        "is_score": is_score,
        "score_type": score_type,
        "confidence": confidence,
        "confidence_tier": tier,
        "evidence_source": evidence,
        "alternates": alternates,
        "match_details": {
            "part_segment": part_segment,
            "matched_alias": matched_alias,
            "filename_match": filename_match,
            "text_match": text_match,
        },
        "duplicate_in_piece": False,
        "needs_review": needs_review,
        "part_sort_key": part_sort_key,
        "file_fingerprint": inv_record.get("file_fingerprint"),
        "processing_status": "success",
        "processing_timestamp": utc_now_iso(),
    }


def build_skipped_record(inv_record: dict[str, Any], run_id: str) -> dict[str, Any]:
    piece_folder = inv_record.get("piece_folder", "")
    catalog_number, piece_title_guess = parse_piece_identity(
        piece_folder, inv_record.get("pdf_filename", "")
    )
    return {
        "record_version": RECORD_VERSION,
        "run_id": run_id,
        "pdf_path": inv_record.get("pdf_path"),
        "piece_id": inv_record.get("piece_id"),
        "piece_folder": piece_folder,
        "pdf_filename": inv_record.get("pdf_filename", ""),
        "catalog_number": catalog_number,
        "piece_title_guess": piece_title_guess,
        "predicted_part": None,
        "canonical_instrument": None,
        "family": "unknown",
        "section": "unknown",
        "part_index": None,
        "clef": None,
        "transposition": None,
        "is_score": False,
        "score_type": None,
        "confidence": 0.0,
        "confidence_tier": "none",
        "evidence_source": "none",
        "alternates": [],
        "match_details": {
            "part_segment": None,
            "matched_alias": None,
            "filename_match": False,
            "text_match": False,
        },
        "duplicate_in_piece": False,
        "needs_review": True,
        "part_sort_key": compute_part_sort_key(None, None, None, False, None),
        "file_fingerprint": inv_record.get("file_fingerprint"),
        "processing_status": "skipped_unreadable",
        "processing_timestamp": utc_now_iso(),
    }


def apply_ensemble(records: list[dict[str, Any]]) -> None:
    """Flag documents that share an instrument signature within the same piece."""
    signatures: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for rec in records:
        if rec.get("is_score") or rec.get("canonical_instrument") is None:
            continue
        key = (
            rec.get("piece_id"),
            rec.get("canonical_instrument"),
            rec.get("part_index"),
            rec.get("clef"),
        )
        signatures.setdefault(key, []).append(rec)
    for group in signatures.values():
        if len(group) > 1:
            for rec in group:
                rec["duplicate_in_piece"] = True
                rec["needs_review"] = True


def build_piece_rollups(
    records: list[dict[str, Any]], run_id: str
) -> list[dict[str, Any]]:
    """Aggregate per-document predictions into one observed-parts record per piece.

    The observed-part key is ``(canonical_instrument, part_index, clef)`` \u2014 the exact shape
    Scripts 04/06 compare against \u2014 so downstream gap analysis never drifts on key shape.
    """
    pieces: dict[Any, dict[str, Any]] = {}
    for rec in records:
        piece_id = rec.get("piece_id")
        piece = pieces.get(piece_id)
        if piece is None:
            piece = {
                "piece_id": piece_id,
                "piece_folder": rec.get("piece_folder", ""),
                "catalog_number": rec.get("catalog_number"),
                "piece_title_guess": rec.get("piece_title_guess"),
                "documents": [],
            }
            pieces[piece_id] = piece
        # Prefer the first non-null identity seed seen for the piece.
        if not piece["catalog_number"] and rec.get("catalog_number"):
            piece["catalog_number"] = rec["catalog_number"]
        if not piece["piece_title_guess"] and rec.get("piece_title_guess"):
            piece["piece_title_guess"] = rec["piece_title_guess"]
        piece["documents"].append(rec)

    rollups: list[dict[str, Any]] = []
    for piece in pieces.values():
        docs = piece["documents"]
        classified = [d for d in docs if d.get("processing_status") == "success"]
        scores = [d for d in classified if d.get("is_score")]
        parts = [d for d in classified if not d.get("is_score")]

        observed: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
        for d in parts:
            key = (
                d.get("canonical_instrument"),
                d.get("part_index"),
                d.get("clef"),
            )
            entry = observed.get(key)
            conf = d.get("confidence", 0.0)
            if entry is None:
                observed[key] = {
                    "canonical_instrument": key[0],
                    "part_index": key[1],
                    "clef": key[2],
                    "section": d.get("section", "unknown"),
                    "predicted_part": d.get("predicted_part"),
                    "count": 1,
                    "min_confidence": conf,
                    "max_confidence": conf,
                    "needs_review": bool(d.get("needs_review")),
                    "duplicate": bool(d.get("duplicate_in_piece")),
                    "part_sort_key": d.get("part_sort_key", ""),
                }
            else:
                entry["count"] += 1
                entry["min_confidence"] = min(entry["min_confidence"], conf)
                entry["max_confidence"] = max(entry["max_confidence"], conf)
                entry["needs_review"] = entry["needs_review"] or bool(d.get("needs_review"))
                entry["duplicate"] = entry["duplicate"] or bool(d.get("duplicate_in_piece"))

        observed_parts = sorted(observed.values(), key=lambda e: e["part_sort_key"])
        families = sorted({d.get("family") for d in classified if d.get("family")})
        sections = sorted({d.get("section") for d in classified if d.get("section")})
        distinct_instruments = len(
            {d.get("canonical_instrument") for d in parts if d.get("canonical_instrument")}
        )

        rollups.append(
            {
                "record_version": RECORD_VERSION,
                "run_id": run_id,
                "piece_id": piece["piece_id"],
                "piece_folder": piece["piece_folder"],
                "catalog_number": piece["catalog_number"],
                "piece_title_guess": piece["piece_title_guess"],
                "document_count": len(docs),
                "classified_count": len(classified),
                "has_score": bool(scores),
                "score_types": sorted({s.get("score_type") for s in scores if s.get("score_type")}),
                "distinct_instruments": distinct_instruments,
                "families": families,
                "sections": sections,
                "needs_review_count": sum(1 for d in docs if d.get("needs_review")),
                "unmatched_count": sum(
                    1 for d in parts if d.get("canonical_instrument") is None
                ),
                "low_confidence_count": sum(
                    1
                    for d in parts
                    if d.get("canonical_instrument") is not None
                    and d.get("confidence", 0.0) < LOW_CONFIDENCE
                ),
                "duplicate_count": sum(1 for d in docs if d.get("duplicate_in_piece")),
                "observed_parts": observed_parts,
            }
        )

    rollups.sort(key=lambda p: (p.get("catalog_number") or "", p.get("piece_folder") or ""))
    return rollups


# --- Reporting -------------------------------------------------------------------------------


def _part_status_icon(rec: dict[str, Any]) -> str:
    """One-glyph status for the per-document detail table."""
    status = rec.get("processing_status")
    if status == "error":
        return "❌"
    if status != "success":
        return "⏭️"
    if rec.get("is_score"):
        return "🎼"
    if rec.get("canonical_instrument") is None:
        return "⚠️"
    if rec.get("confidence", 0.0) < LOW_CONFIDENCE:
        return "⚠️"
    return "✅"


def build_report(
    records: list[dict[str, Any]],
    meta: dict[str, Any],
) -> str:
    """Render a human-readable Markdown summary of the classification outputs."""
    classified = [r for r in records if r["processing_status"] == "success"]
    skipped = [r for r in records if r["processing_status"] == "skipped_unreadable"]
    errors = [r for r in records if r["processing_status"] == "error"]
    total = len(records)

    family_counts: dict[str, int] = {}
    instrument_counts: dict[str, int] = {}
    evidence_counts: dict[str, int] = {}
    for rec in classified:
        family_counts[rec["family"]] = family_counts.get(rec["family"], 0) + 1
        canonical = rec["canonical_instrument"] or "(unmatched)"
        instrument_counts[canonical] = instrument_counts.get(canonical, 0) + 1
        evidence = rec.get("evidence_source") or "none"
        evidence_counts[evidence] = evidence_counts.get(evidence, 0) + 1

    scores = [r for r in classified if r["is_score"]]
    parts = [r for r in classified if not r["is_score"]]
    unmatched = [r for r in parts if r["canonical_instrument"] is None]
    low_conf = [
        r for r in parts if r["canonical_instrument"] is not None and r["confidence"] < LOW_CONFIDENCE
    ]
    duplicates = [r for r in classified if r["duplicate_in_piece"]]

    high_conf = sum(1 for r in classified if r["confidence"] >= 0.90)
    med_conf = sum(1 for r in classified if LOW_CONFIDENCE <= r["confidence"] < 0.90)
    lo_conf_all = sum(1 for r in classified if 0.0 < r["confidence"] < LOW_CONFIDENCE)
    zero_conf = sum(1 for r in classified if r["confidence"] <= 0.0)

    overall = "✅ Healthy"
    if errors:
        overall = "❌ Errors present"
    elif unmatched or low_conf or duplicates:
        overall = "⚠️ Review recommended"

    out: list[str] = []
    out.append("# Part Classification Report")
    out.append("")
    out.append(
        f"_Generated {meta['generated_at']} • run `{meta['run_id']}` • "
        f"mode **{meta['mode']}** • {meta['elapsed_seconds']:.1f}s_"
    )
    out.append("")
    out.append(f"**Status:** {overall}")
    out.append("")

    # Navigation
    out.append("## Contents")
    out.append("")
    out.append("- [At a Glance](#at-a-glance)")
    out.append("- [Instrument Coverage](#instrument-coverage)")
    out.append("- [Confidence and Evidence](#confidence-and-evidence)")
    out.append("- [Attention Needed](#attention-needed)")
    out.append("- [Per-Piece Breakdown](#per-piece-breakdown)")
    out.append("- [Per-Document Detail](#per-document-detail)")
    out.append("- [Configuration and Environment](#configuration-and-environment)")
    out.append("")

    # At a Glance
    out.append("## At a Glance")
    out.append("")
    out.append("| Metric | Value |")
    out.append("| --- | --- |")
    out.append(f"| Documents in output | {total} |")
    out.append(
        f"| Processed this run | {meta['processed']} (reused {meta['reused']}) |"
    )
    out.append(f"| Skipped (unreadable in inventory) | {len(skipped)} |")
    out.append(f"| Classification errors | {len(errors)} |")
    out.append(
        f"| Parts classified | {len(parts)} "
        f"({pct(len(parts), len(classified)):.1f}% of classified) |"
    )
    out.append(f"| Scores detected | {len(scores)} |")
    out.append(f"| Distinct instruments | {len(instrument_counts)} |")
    out.append(f"| Distinct families | {len(family_counts)} |")
    out.append(f"| Unmatched parts | {len(unmatched)} |")
    out.append(f"| Low-confidence parts (< {LOW_CONFIDENCE:.2f}) | {len(low_conf)} |")
    out.append(f"| Duplicate labels within a piece | {len(duplicates)} |")
    out.append(f"| Documents needing review | {sum(1 for r in records if r.get('needs_review'))} |")
    out.append("")

    # Instrument coverage
    out.append("## Instrument Coverage")
    out.append("")
    if family_counts:
        out.append("### By family")
        out.append("")
        out.append("| Family | Count |")
        out.append("| --- | --- |")
        for family in sorted(family_counts, key=lambda k: (-family_counts[k], k)):
            out.append(f"| {family} | {family_counts[family]} |")
        out.append("")
        out.append("### By instrument")
        out.append("")
        out.append("| Instrument | Count |")
        out.append("| --- | --- |")
        for canonical in sorted(instrument_counts, key=lambda k: (-instrument_counts[k], k)):
            display = INSTRUMENT_DISPLAY.get(canonical, canonical)
            out.append(f"| {md_cell(display)} | {instrument_counts[canonical]} |")
        out.append("")
    else:
        out.append("No documents were classified this run.")
        out.append("")

    # Confidence & evidence
    out.append("## Confidence and Evidence")
    out.append("")
    out.append("| Confidence band | Documents |")
    out.append("| --- | --- |")
    out.append(f"| High (≥ 0.90) | {high_conf} |")
    out.append(f"| Medium ({LOW_CONFIDENCE:.2f} – 0.90) | {med_conf} |")
    out.append(f"| Low (< {LOW_CONFIDENCE:.2f}) | {lo_conf_all} |")
    out.append(f"| None (0.00) | {zero_conf} |")
    out.append("")
    if evidence_counts:
        out.append("| Evidence source | Documents |")
        out.append("| --- | --- |")
        for source in sorted(evidence_counts, key=lambda k: (-evidence_counts[k], k)):
            out.append(f"| {source} | {evidence_counts[source]} |")
        out.append("")

    # Attention needed
    out.append("## Attention Needed")
    out.append("")
    attention_added = False

    if errors:
        attention_added = True
        out.append(f"### ❌ Classification errors ({len(errors)})")
        out.append("")
        out.append("| Document | Detail |")
        out.append("| --- | --- |")
        for rec in errors[:15]:
            detail = (rec.get("match_details") or {}).get("matched_alias") or ""
            out.append(f"| {md_cell(rec.get('pdf_path'))} | {md_cell(detail)} |")
        if len(errors) > 15:
            out.append(f"| … and {len(errors) - 15} more | |")
        out.append("")

    if unmatched:
        attention_added = True
        out.append(f"### ⚠️ Unmatched parts ({len(unmatched)})")
        out.append("")
        out.append("| Document | Part segment | Evidence |")
        out.append("| --- | --- | --- |")
        for rec in unmatched[:20]:
            segment = (rec.get("match_details") or {}).get("part_segment") or ""
            out.append(
                f"| {md_cell(rec.get('pdf_filename'))} | {md_cell(segment)} | "
                f"{md_cell(rec.get('evidence_source'))} |"
            )
        if len(unmatched) > 20:
            out.append(f"| … and {len(unmatched) - 20} more | | |")
        out.append("")

    if low_conf:
        attention_added = True
        out.append(f"### ⚠️ Low-confidence parts ({len(low_conf)})")
        out.append("")
        out.append("| Document | Predicted | Confidence | Evidence |")
        out.append("| --- | --- | --- | --- |")
        for rec in sorted(low_conf, key=lambda r: r["confidence"])[:20]:
            out.append(
                f"| {md_cell(rec.get('pdf_filename'))} | "
                f"{md_cell(rec.get('predicted_part'))} | "
                f"{rec['confidence']:.2f} | {md_cell(rec.get('evidence_source'))} |"
            )
        if len(low_conf) > 20:
            out.append(f"| … and {len(low_conf) - 20} more | | | |")
        out.append("")

    if duplicates:
        attention_added = True
        out.append(f"### ⚠️ Duplicate labels within a piece ({len(duplicates)})")
        out.append("")
        out.append("| Piece | Document | Predicted |")
        out.append("| --- | --- | --- |")
        for rec in sorted(duplicates, key=lambda r: (r.get("piece_folder") or "", r.get("predicted_part") or "")):
            out.append(
                f"| {md_cell(rec.get('piece_folder'))} | "
                f"{md_cell(rec.get('pdf_filename'))} | "
                f"{md_cell(rec.get('predicted_part'))} |"
            )
        out.append("")

    if not attention_added:
        out.append("✅ Nothing flagged. Every readable document matched an instrument/part "
                   "at or above the confidence threshold, with no duplicate labels.")
        out.append("")

    # Per-piece breakdown
    out.append("## Per-Piece Breakdown")
    out.append("")
    pieces: dict[str, dict[str, Any]] = {}
    for rec in records:
        p = pieces.setdefault(
            rec.get("piece_folder") or "",
            {"catalog": rec.get("catalog_number") or "", "docs": 0, "classified": 0,
             "scores": 0, "unmatched": 0, "low": 0, "dupes": 0, "review": 0},
        )
        if not p["catalog"] and rec.get("catalog_number"):
            p["catalog"] = rec["catalog_number"]
        p["docs"] += 1
        if rec.get("needs_review"):
            p["review"] += 1
        if rec["processing_status"] == "success":
            p["classified"] += 1
            if rec["is_score"]:
                p["scores"] += 1
            elif rec["canonical_instrument"] is None:
                p["unmatched"] += 1
            elif rec["confidence"] < LOW_CONFIDENCE:
                p["low"] += 1
            if rec["duplicate_in_piece"]:
                p["dupes"] += 1
    out.append(
        "| Catalog | Piece | Docs | Classified | Scores | Unmatched | Low-conf | "
        "Duplicates | Review |"
    )
    out.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for name in sorted(pieces, key=lambda k: (pieces[k]["catalog"], k)):
        p = pieces[name]
        out.append(
            f"| {md_cell(p['catalog'])} | {md_cell(name) or '(root)'} | {p['docs']} | "
            f"{p['classified']} | {p['scores']} | {p['unmatched']} | {p['low']} | "
            f"{p['dupes']} | {p['review']} |"
        )
    out.append("")

    # Per-document detail
    out.append("## Per-Document Detail")
    out.append("")
    limit = int(meta["detail_limit"])
    shown = records[:limit]
    out.append(f"<details><summary>Show {len(shown)} of {total} documents</summary>")
    out.append("")
    out.append("| | Document | Piece | Predicted | Section | Conf | Evidence |")
    out.append("| --- | --- | --- | --- | --- | --- | --- |")
    for rec in shown:
        out.append(
            f"| {_part_status_icon(rec)} | {md_cell(rec.get('pdf_filename'))} | "
            f"{md_cell(rec.get('piece_folder'))} | "
            f"{md_cell(rec.get('predicted_part'))} | "
            f"{md_cell(rec.get('section'))} | {rec.get('confidence', 0.0):.2f} | "
            f"{md_cell(rec.get('evidence_source'))} |"
        )
    out.append("")
    out.append("</details>")
    out.append("")
    if total > limit:
        out.append(
            f"> Showing first {limit} of {total} documents. "
            "See `part_predictions.jsonl` for the complete dataset."
        )
        out.append("")

    # Configuration
    out.append("## Configuration and Environment")
    out.append("")
    out.append("| Setting | Value |")
    out.append("| --- | --- |")
    out.append(f"| Record schema version | {RECORD_VERSION} |")
    out.append(f"| Mode | {meta['mode']} |")
    out.append(f"| Inventory input | `{meta['inventory']}` |")
    out.append(f"| Documents input (Script 02) | `{meta['documents']}` |")
    out.append(f"| Pages input (Script 02) | `{meta['pages']}` |")
    out.append(f"| Script 02 data present | {'yes' if meta['have_script02'] else 'no'} |")
    out.append(f"| Lexicon source | {meta['rules_source']} |")
    out.append(f"| LLM fallback | {'enabled' if meta['llm_enabled'] else 'disabled'} |")
    out.append(f"| Predictions output | `{meta['output']}` |")
    out.append(f"| Observed-parts output | `{meta['pieces_output']}` |")
    out.append("")
    out.append("Status legend: ✅ matched (≥ threshold) • 🎼 score • ⚠️ unmatched/low-confidence "
               "• ❌ error • ⏭️ skipped (unreadable).")
    out.append("")

    return "\n".join(out) + "\n"


# --- Incremental helpers ---------------------------------------------------------------------


def load_previous_record_map(output: Path) -> dict[str, dict[str, Any]]:
    previous = read_jsonl(output)
    return {rec["pdf_path"]: rec for rec in previous if "pdf_path" in rec}


def should_reuse_record(
    prior_record: dict[str, Any] | None,
    current_fingerprint: str | None,
) -> bool:
    if prior_record is None or current_fingerprint is None:
        return False
    return prior_record.get("file_fingerprint") == current_fingerprint


@app.command()
def main(
    inventory: Path = typer.Option(
        Path("data/raw_inventory.jsonl"), help="Script 01 inventory JSONL input"
    ),
    documents: Path = typer.Option(
        Path("data/documents.jsonl"), help="Script 02 per-document rollups (optional)"
    ),
    pages: Path = typer.Option(
        Path("data/pages.jsonl"), help="Script 02 per-page features (optional)"
    ),
    rules: Path = typer.Option(
        Path("config/regex_rules.yaml"), help="Instrument lexicon YAML (optional)"
    ),
    output: Path = typer.Option(
        Path("data/part_predictions.jsonl"), help="Prediction output JSONL"
    ),
    output_pieces: Path = typer.Option(
        Path("data/observed_parts_by_piece.jsonl"),
        help="Per-piece observed-parts rollup output JSONL",
    ),
    output_report: Path = typer.Option(
        Path("data/part_classification_report.md"), help="Markdown summary output"
    ),
    write_report: bool = typer.Option(
        True, "--report/--no-report", help="Write the Markdown summary report"
    ),
    report_detail_limit: int = typer.Option(
        200, help="Max rows in the per-document detail table of the report"
    ),
    mode: str = typer.Option("full", help="Processing mode: full or incremental"),
    use_llm: bool = typer.Option(
        False, "--use-llm/--no-llm", help="Enable the (unwired) LLM fallback hook"
    ),
    log_level: str = typer.Option("INFO", help="DEBUG, INFO, WARNING, ERROR"),
) -> None:
    """Classify each PDF's instrument/part and whether it is a score."""
    mode = mode.lower().strip()
    if mode not in {"full", "incremental"}:
        raise typer.BadParameter("mode must be 'full' or 'incremental'")

    setup_logging(log_level)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    start_time = time.perf_counter()

    inventory = inventory.resolve()
    output = output.resolve()

    inv_records = read_jsonl(inventory)
    if not inv_records:
        raise typer.BadParameter(f"No inventory records found at {inventory}")

    if use_llm:
        logger.warning(
            "LLM fallback requested but no provider is wired; "
            "rule-based predictions are emitted unchanged."
        )

    lexicon, rules_source = load_lexicon(rules.resolve())
    compiled = compile_aliases(lexicon)
    section_map = build_section_map(lexicon)

    # Optional Script 02 datasets, indexed by pdf_path.
    doc_map = {r["pdf_path"]: r for r in read_jsonl(documents.resolve()) if "pdf_path" in r}
    page1_map: dict[str, dict[str, Any]] = {}
    for rec in read_jsonl(pages.resolve()):
        if rec.get("page_num") == 1 and "pdf_path" in rec:
            page1_map.setdefault(rec["pdf_path"], rec)
    if not doc_map:
        logger.warning("No Script 02 documents found; classifying filename-only.")

    checkpoint_path = make_checkpoint_path(output, CHECKPOINT_FILENAME)
    # Loaded only to warn on (and ignore) a stale-version checkpoint; incremental reuse below is
    # driven by per-record fingerprints, not the checkpoint payload.
    load_checkpoint(checkpoint_path, RECORD_VERSION, logger)
    previous_records_map = load_previous_record_map(output)

    rebuilt: list[dict[str, Any]] = []
    classified_count = 0
    reused_count = 0
    skipped_count = 0

    for inv in inv_records:
        pdf_path = inv.get("pdf_path")
        if not pdf_path:
            continue
        if not inv.get("pdf_readable", True):
            rebuilt.append(build_skipped_record(inv, run_id))
            skipped_count += 1
            continue

        current_fingerprint = inv.get("file_fingerprint")
        prior = previous_records_map.get(pdf_path)
        if mode == "incremental" and should_reuse_record(prior, current_fingerprint):
            rebuilt.append(prior)
            reused_count += 1
            continue

        try:
            record = classify_document(
                inv, doc_map.get(pdf_path), page1_map.get(pdf_path),
                lexicon, compiled, section_map, run_id,
            )
        except Exception as exc:
            logger.exception("Unexpected classification error on %s", pdf_path)
            record = build_skipped_record(inv, run_id)
            record["processing_status"] = "error"
            record["match_details"]["matched_alias"] = f"error: {exc}"
        rebuilt.append(record)
        classified_count += 1

    apply_ensemble(rebuilt)
    rebuilt.sort(key=lambda rec: rec.get("pdf_path") or "")
    atomic_write_jsonl(output, rebuilt)

    output_pieces = output_pieces.resolve()
    piece_rollups = build_piece_rollups(rebuilt, run_id)
    atomic_write_jsonl(output_pieces, piece_rollups)
    logger.info(
        "Wrote per-piece observed-parts rollup (%d pieces): %s",
        len(piece_rollups),
        output_pieces,
    )

    if write_report:
        meta = {
            "generated_at": utc_now_iso(),
            "run_id": run_id,
            "mode": mode,
            "elapsed_seconds": time.perf_counter() - start_time,
            "processed": classified_count,
            "reused": reused_count,
            "rules_source": rules_source,
            "llm_enabled": use_llm,
            "have_script02": bool(doc_map),
            "inventory": inventory.as_posix(),
            "documents": documents.resolve().as_posix(),
            "pages": pages.resolve().as_posix(),
            "output": output.as_posix(),
            "pieces_output": output_pieces.as_posix(),
            "detail_limit": report_detail_limit,
        }
        report = build_report(rebuilt, meta)
        atomic_write_text(output_report.resolve(), report)
        logger.info("Wrote Markdown report: %s", output_report.resolve())

    new_checkpoint = build_checkpoint(
        RECORD_VERSION,
        run_id,
        {
            rec["pdf_path"]: rec.get("file_fingerprint")
            for rec in rebuilt
            if rec.get("pdf_path")
        },
        inventory_input=inventory.as_posix(),
        output=output.as_posix(),
        pieces_output=output_pieces.as_posix(),
        rules_source=rules_source,
        llm_enabled=use_llm,
        record_count=len(rebuilt),
    )
    atomic_write_json(checkpoint_path, new_checkpoint)

    logger.info(
        "Part classification completed: total=%d classified=%d reused=%d skipped=%d output=%s",
        len(rebuilt),
        classified_count,
        reused_count,
        skipped_count,
        output,
    )


if __name__ == "__main__":
    app()
