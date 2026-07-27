"""Script 03: classify each PDF's instrument/part and whether it is a score.

Consumes the Script 01 inventory and the Script 02 extraction datasets and writes one
prediction record per readable PDF to ``data/part_predictions.jsonl`` plus a Markdown report.

The classifier is rule-first and deterministic. The filename supplies a baseline part identity and
structure (its part segment is isolated by stripping the known ``piece_folder`` prefix), which
trustworthy in-file reads may override: the OCR->LLM consolidation, then the printed label in the
page's upper-left corner, then a same-section footer/credit instrument. ``<instrument> cue``
annotations are stripped so a cue naming another instrument never wins. An instrument lexicon lives
in ``config/regex_rules.yaml`` (with a built-in fallback so the script runs even without the file
or PyYAML).
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
    strip_music_glyphs,
    utc_now_iso,
)

RECORD_VERSION = "1.2"

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
        "castanets": "percussion", "tambourine": "percussion",
        "percussion": "percussion", "drum_set": "percussion",
        "violin": "strings", "viola": "strings", "cello": "strings",
        "guitar": "strings", "bass_guitar": "strings", "harp": "strings",
        "piano": "keyboard", "keyboard": "keyboard", "celesta": "keyboard",
        "organ": "keyboard", "voice": "voice",
        "alto_flute": "woodwind", "bass_flute": "woodwind",
        "contrabassoon": "woodwind", "sopranino_sax": "woodwind", "mellophone": "brass",
        "triangle": "percussion", "woodblock": "percussion", "temple_blocks": "percussion",
        "claves": "percussion", "maracas": "percussion", "cowbell": "percussion",
        "guiro": "percussion", "vibraslap": "percussion", "tom_toms": "percussion",
        "bongos": "percussion", "congas": "percussion", "timbales": "percussion",
        "tenor_drum": "percussion", "field_drum": "percussion", "sleigh_bells": "percussion",
        "wind_chimes": "percussion", "mark_tree": "percussion", "tam_tam": "percussion",
        "ratchet": "percussion", "slapstick": "percussion", "crotales": "percussion",
        "finger_cymbals": "percussion", "anvil": "percussion", "brake_drum": "percussion",
    },
    "instruments": {
        "piccolo": ["piccolo", "picc"],
        "flute": ["flute", "flutes", "fl", "c flute"],
        "oboe": ["oboe", "ob"],
        "english_horn": ["english horn", "cor anglais"],
        "bassoon": ["bassoon", "bsn", "fagotto"],
        "eb_clarinet": ["eb clarinet", "eb clarinets", "e flat clarinet", "clarinet in eb",
                        "clarinet in e flat", "clarinet eb"],
        "clarinet": ["bb clarinet", "bb clarinets", "b flat clarinet", "clarinet in bb",
                     "clarinets", "clarinet", "clar", "cl"],
        "alto_clarinet": ["alto clarinet", "alto clarinets", "eb alto clarinet", "clarinet eb alto",
                          "clarinet alto"],
        "bass_clarinet": ["bass clarinet", "bass clarinets", "b cl", "bass cl"],
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
        "castanets": ["castanets", "castanet", "castenets", "castenet"],
        "tambourine": ["tambourine", "tambourines", "tambo", "tamb"],
        "percussion": ["percussion", "perc", "battery", "aux percussion", "auxiliary percussion"],
        "drum_set": [
            "drum set", "drum kit", "drums", "trap set", "trap kit",
            "drumset", "drumkit", "trapset", "trapkit", "kit",
        ],
        "violin": ["violin", "violins", "vln", "vn"],
        "viola": ["viola", "violas", "vla"],
        "cello": ["cello", "cellos", "violoncello", "vc", "vcl"],
        "guitar": [
            "guitar", "gtr", "electric guitar", "acoustic guitar",
            "rhythm guitar", "classical guitar", "nylon string guitar",
        ],
        "bass_guitar": ["bass guitar", "electric bass", "elec bass", "bass gtr"],
        "harp": ["harp", "harps"],
        "piano": ["piano", "pianoforte", "pno"],
        "keyboard": ["keyboard", "electronic keyboard", "synthesizer", "synth"],
        "celesta": ["celesta", "celeste"],
        "organ": ["organ", "pipe organ", "electric organ", "hammond organ"],
        "voice": ["voice", "vocals", "vocal", "choir", "chorus", "solo voice"],
        "alto_flute": ["alto flute", "alt flute"],
        "bass_flute": ["bass flute"],
        "contrabassoon": ["contrabassoon", "contra bassoon", "double bassoon"],
        "sopranino_sax": ["sopranino saxophone", "sopranino sax"],
        "mellophone": ["mellophone", "mello"],
        "triangle": ["triangle", "triangles"],
        "woodblock": ["woodblock", "woodblocks", "wood block", "wood blocks"],
        "temple_blocks": ["temple blocks", "temple block"],
        "claves": ["claves", "clave"],
        "maracas": ["maracas", "maraca"],
        "cowbell": ["cowbell", "cowbells", "cow bell"],
        "guiro": ["guiro", "guiros"],
        "vibraslap": ["vibraslap", "vibra slap"],
        "tom_toms": [
            "tom toms", "tom-toms", "tomtoms", "toms",
            "concert toms", "roto toms", "roto-toms",
        ],
        "bongos": ["bongos", "bongo"],
        "congas": ["congas", "conga"],
        "timbales": ["timbales"],
        "tenor_drum": ["tenor drum", "tenor drums"],
        "field_drum": ["field drum", "field drums"],
        "sleigh_bells": ["sleigh bells", "sleigh bell", "sleighbells"],
        "wind_chimes": ["wind chimes", "wind chime"],
        "mark_tree": ["mark tree", "mark-tree", "marktree", "bell tree"],
        "tam_tam": ["tam tam", "tam-tam", "tamtam", "gong", "gongs"],
        "ratchet": ["ratchet", "ratchets"],
        "slapstick": ["slapstick", "slap stick", "whip"],
        "crotales": ["crotales", "antique cymbals"],
        "finger_cymbals": ["finger cymbals", "finger cymbal"],
        "anvil": ["anvil", "anvils"],
        "brake_drum": ["brake drum", "brake drums"],
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
        "flutes": ["piccolo", "flute", "alto_flute", "bass_flute"],
        "double_reeds": ["oboe", "english_horn", "bassoon", "contrabassoon"],
        "clarinets": [
            "eb_clarinet", "clarinet", "alto_clarinet", "bass_clarinet", "contrabass_clarinet",
        ],
        "saxophones": [
            "sopranino_sax", "soprano_sax", "alto_sax", "tenor_sax", "baritone_sax", "bass_sax",
        ],
        "cornets_trumpets": ["soprano_cornet", "cornet", "trumpet", "flugelhorn"],
        "horns": ["horn", "tenor_horn", "mellophone"],
        "low_brass": ["trombone", "bass_trombone", "baritone_horn", "euphonium"],
        "tubas": ["tuba"],
        "strings": ["string_bass", "violin", "viola", "cello", "guitar", "bass_guitar", "harp"],
        "keyboards": ["piano", "keyboard", "celesta", "organ"],
        "voices": ["voice"],
        "percussion": [
            "timpani", "mallet_percussion", "snare_drum", "bass_drum", "cymbals",
            "castanets", "tambourine", "percussion", "drum_set", "triangle", "woodblock",
            "temple_blocks", "claves", "maracas", "cowbell", "guiro", "vibraslap", "tom_toms",
            "bongos", "congas", "timbales", "tenor_drum", "field_drum", "sleigh_bells",
            "wind_chimes", "mark_tree", "tam_tam", "ratchet", "slapstick", "crotales",
            "finger_cymbals", "anvil", "brake_drum",
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
    "bass_drum": "Bass Drum", "cymbals": "Cymbals",
    "castanets": "Castanets", "tambourine": "Tambourine", "percussion": "Percussion",
    "drum_set": "Drum Set",
    "violin": "Violin", "viola": "Viola", "cello": "Cello", "guitar": "Guitar",
    "bass_guitar": "Bass Guitar", "harp": "Harp", "piano": "Piano", "keyboard": "Keyboard",
    "celesta": "Celesta", "organ": "Organ", "voice": "Voice", "alto_flute": "Alto Flute",
    "bass_flute": "Bass Flute", "contrabassoon": "Contrabassoon",
    "sopranino_sax": "Sopranino Saxophone", "mellophone": "Mellophone", "triangle": "Triangle",
    "woodblock": "Wood Block", "temple_blocks": "Temple Blocks", "claves": "Claves",
    "maracas": "Maracas", "cowbell": "Cowbell", "guiro": "Guiro", "vibraslap": "Vibraslap",
    "tom_toms": "Tom-Toms", "bongos": "Bongos", "congas": "Congas", "timbales": "Timbales",
    "tenor_drum": "Tenor Drum", "field_drum": "Field Drum", "sleigh_bells": "Sleigh Bells",
    "wind_chimes": "Wind Chimes", "mark_tree": "Mark Tree", "tam_tam": "Tam-Tam",
    "ratchet": "Ratchet", "slapstick": "Slapstick", "crotales": "Crotales",
    "finger_cymbals": "Finger Cymbals", "anvil": "Anvil", "brake_drum": "Brake Drum",
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


# Cue annotations (e.g. "Oboe cue" printed in a clarinet part) quote ANOTHER instrument for the
# player's reference. They are removed before scanning so they never masquerade as the part's own
# instrument. Matches "<word> cue" / "<word> cues" in already-normalized (lowercased) text.
_CUE_RE = re.compile(r"(?<![a-z0-9])[a-z][a-z.'&]*\s+cues?(?![a-z0-9])")


def strip_cues(text_norm: str) -> str:
    """Remove ``<instrument> cue``/``cues`` annotations from already-normalized text."""
    return re.sub(r"\s+", " ", _CUE_RE.sub(" ", text_norm)).strip()


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


def match_instrument_first(
    text_norm: str,
    compiled: list[tuple[str, str]],
    min_alias_len: int = 1,
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """Return the EARLIEST-positioned instrument match; ties broken by longest alias.

    The printed part label sits topmost/leftmost in the upper-left corner, above any body cue that
    names another instrument, so earliest-position wins there (unlike :func:`match_instrument`,
    which is longest-alias-wins and used where position is not meaningful).
    """
    found: list[tuple[int, int, str, str]] = []  # (start, -len(alias), canonical, alias)
    for canonical, alias in compiled:
        if len(alias) < min_alias_len:
            continue
        match = re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", text_norm)
        if match is not None:
            found.append((match.start(), -len(alias), canonical, alias))
    if not found:
        return None, None, []
    found.sort()
    best_canonical, best_alias = found[0][2], found[0][3]
    alternates: list[dict[str, Any]] = []
    seen = {best_canonical}
    for _start, _neg_len, canonical, alias in found:
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


# Connectors that mean one physical part covers MULTIPLE instruments -- either a doubling
# ("Flute 1 & Piccolo", "Oboe / English Horn") or a multi-chair part ("Horn 1 & 2"). Each named
# instrument becomes a co-equal facet so downstream coverage can satisfy any matching expected slot.
_COMBINED_SPLIT_RE = re.compile(r"\s*(?:&|/|\+|w/|\band\b|\bdoubling\b|\bdbl\b\.?)\s*")


def build_instrument_facets(
    seg_norm: str,
    compiled: list[tuple[str, str]],
    section_map: dict[str, str],
    lexicon: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a (possibly combined) part label into ordered, co-equal instrument facets.

    Returns ``(facets, primary_alternates)``. Each facet is
    ``{"canonical", "part_index", "family", "section"}``. A single-instrument label yields one
    facet (identical to the old behavior). A combined label yields one facet per named instrument.
    A sub-segment that carries only an index (e.g. the ``2`` in ``Horn 1 & 2``) extends the most
    recent instrument as an additional chair. ``primary_alternates`` are the ambiguity alternates of
    the first matched instrument (kept for the record's ``alternates`` field).
    """
    sub_segments = [s for s in _COMBINED_SPLIT_RE.split(seg_norm) if s.strip()]
    if not sub_segments:
        sub_segments = [seg_norm]

    facets: list[dict[str, Any]] = []
    seen: set[tuple[str, int | None]] = set()
    primary_alternates: list[dict[str, Any]] = []
    last_canonical: str | None = None
    for sub in sub_segments:
        canonical, _alias, alternates = match_instrument(sub, compiled)
        idx = extract_part_index(sub)
        if canonical is None:
            # A bare index extends the previous instrument (e.g. "Horn 1 & 2" -> horn 1, horn 2).
            if idx is not None and last_canonical is not None:
                canonical = last_canonical
            else:
                continue
        else:
            last_canonical = canonical
            if not primary_alternates:
                primary_alternates = alternates
        key = (canonical, idx)
        if key in seen:
            continue
        seen.add(key)
        family = lexicon["families"].get(canonical, "other")
        facets.append({
            "canonical": canonical,
            "part_index": idx,
            "family": family,
            "section": section_for(canonical, family, section_map),
        })
    return facets, primary_alternates


def llm_instruments(doc_record: dict[str, Any] | None, lexicon: dict[str, Any]) -> list[str]:
    """Ordered, de-duplicated canonical instrument tokens from Script 02's OCR->LLM consolidation.

    Only tokens that are known canonicals in the lexicon are kept, so a malformed or hallucinated
    LLM token is dropped rather than trusted.
    """
    if not doc_record:
        return []
    raw = doc_record.get("ocr_llm_instruments")
    if not isinstance(raw, list):
        return []
    families = lexicon.get("families", {})
    out: list[str] = []
    for value in raw:
        canonical = str(value).strip()
        if canonical and canonical in families and canonical not in out:
            out.append(canonical)
    return out


def make_facet(
    canonical: str, lexicon: dict[str, Any], section_map: dict[str, str]
) -> dict[str, Any]:
    """Build a single instrument facet dict for a canonical token."""
    family = lexicon["families"].get(canonical, "other")
    return {
        "canonical": canonical,
        "part_index": None,
        "family": family,
        "section": section_for(canonical, family, section_map),
    }


def primary_facet(rec: dict[str, Any]) -> dict[str, Any] | None:
    """The first instrument facet of a record (its representative instrument), or None."""
    facets = rec.get("instruments") or []
    return facets[0] if facets else None


def primary_canonical(rec: dict[str, Any]) -> str | None:
    facet = primary_facet(rec)
    return facet.get("canonical") if facet else None


def primary_family(rec: dict[str, Any]) -> str:
    if rec.get("is_score"):
        return "score"
    facet = primary_facet(rec)
    return facet.get("family") if facet else "unknown"


def primary_section(rec: dict[str, Any]) -> str:
    if rec.get("is_score"):
        return "score"
    facet = primary_facet(rec)
    return facet.get("section") if facet else "unknown"


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
) -> tuple[str, str]:
    """Return (upper_left_norm, full_norm): de-glyphed, cue-stripped, normalized in-file text.

    Both are stripped of Private Use Area music-font glyphs (which pollute born-digital embedded
    text) and of ``<instrument> cue`` annotations (which name OTHER instruments quoted for the
    player's reference and must never masquerade as the part's own instrument) before use.

    - ``upper_left_norm`` covers the page-1 header candidates and the top-left zone -- the corner
      where a part reliably prints its own instrument name. It is the authoritative region for the
      printed label (read earliest-match-first, since the label sits above any body cue).
    - ``full_norm`` covers the header, the top/bottom zones and the entire first-page text. It is
      used to confirm a filename match and to recover a same-section footer/credit instrument.
    """
    upper_left_fragments: list[str] = []
    other_fragments: list[str] = []
    if doc_record:
        header = doc_record.get("first_page_header_candidates") or []
        if isinstance(header, list):
            upper_left_fragments.extend(str(h) for h in header)
        other_fragments.append(str(doc_record.get("first_page_text") or ""))
    if page1_zones:
        top_left = page1_zones.get("zone_top_left")
        if top_left:
            upper_left_fragments.append(str(top_left))
        for key in ("zone_top_center", "zone_top_right", "zone_bottom"):
            value = page1_zones.get(key)
            if value:
                other_fragments.append(str(value))
    upper_left_norm = strip_cues(normalize(strip_music_glyphs(" ".join(upper_left_fragments))))
    full_norm = strip_cues(
        normalize(strip_music_glyphs(" ".join(upper_left_fragments + other_fragments)))
    )
    return upper_left_norm, full_norm



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
    upper_left_norm, full_norm = gather_text_signals(doc_record, page1_zones)

    clef = extract_clef(seg_norm, lexicon)
    transposition = extract_transposition(seg_norm, lexicon)

    instruments: list[dict[str, Any]] = []
    alternates: list[dict[str, Any]] = []
    matched_alias: str | None = None
    filename_match = False
    text_match = False
    is_score = False
    conflict = False

    if score_type is not None:
        is_score = True
        confidence = 0.90
        evidence = "filename"
        clef = None
        transposition = None
    else:
        # Filename-baseline classification with in-file modifiers. The filename supplies the
        # baseline part identity and structure (part index); trustworthy in-file reads may then
        # OVERRIDE it. Precedence: (1) the OCR->LLM consolidation, (2) the printed label in the
        # upper-left corner (may override across sections; flagged for review), (3) a footer/credit
        # instrument recovered from the full page text (relabels only WITHIN the same section, e.g.
        # Baritone -> Euphonium). "<instrument> cue" annotations are stripped upstream so they never
        # masquerade as the part's own instrument.
        filename_facets, filename_alternates = build_instrument_facets(
            seg_norm, compiled, section_map, lexicon
        )
        filename_canon_set = {f["canonical"] for f in filename_facets}
        filename_primary = filename_facets[0]["canonical"] if filename_facets else None
        filename_index = filename_facets[0]["part_index"] if len(filename_facets) == 1 else None
        filename_section = filename_facets[0]["section"] if filename_facets else None

        llm_canon = llm_instruments(doc_record, lexicon)
        # The printed part name sits in the upper-left corner and is read earliest-match-first so the
        # label wins over any surviving reference. The full page text is a weaker signal, used only
        # to confirm the filename or recover a same-section footer/credit instrument.
        ul_canon, _ul_alias, ul_alts = match_instrument_first(
            upper_left_norm, compiled, min_alias_len=4
        )
        full_canon, _full_alias, full_alts = match_instrument(
            full_norm, compiled, min_alias_len=4
        )
        if ul_canon is not None:
            content_canon, content_alts, content_cross = ul_canon, ul_alts, True
        else:
            content_canon, content_alts, content_cross = full_canon, full_alts, False

        # A full-text (footer/credit) instrument may only RELABEL within the same section; a
        # cross-section full-text hit is treated as noise (incidental references) and dropped so the
        # filename baseline stands. The upper-left label is allowed to cross sections.
        if (
            content_canon is not None
            and not content_cross
            and filename_primary is not None
            and filename_primary != content_canon
            and section_for(
                content_canon, lexicon["families"].get(content_canon, "other"), section_map
            )
            != filename_section
        ):
            content_canon = None

        if llm_canon:
            # (1) The document-level OCR->LLM consolidation is the strongest in-file signal. It is
            # authoritative for instrument identity and may expand one physical part into several
            # instruments (e.g. a "Percussion" book -> snare/bass/castanets).
            instruments = [make_facet(c, lexicon, section_map) for c in llm_canon]
            if len(instruments) == 1 and filename_index is not None:
                instruments[0]["part_index"] = filename_index
            matched_alias = llm_canon[0]
            alternates = []
            filename_match = bool(filename_facets)
            text_match = True
            if not filename_facets:
                confidence, evidence = 0.80, "ocr_llm"
            elif filename_primary in set(llm_canon):
                confidence, evidence = 0.90, "combined"
            else:
                same_section = filename_section == instruments[0]["section"]
                confidence = 0.80 if same_section else 0.70
                evidence = "combined"
                conflict = not same_section
        elif content_canon is not None and len(filename_facets) <= 1:
            # (2)/(3) A trustworthy in-file instrument read. The upper-left label may override a
            # single/empty filename across sections (flagged when it does); a footer/credit read
            # only relabels within a section (enforced above). The filename part index is kept.
            family = lexicon["families"].get(content_canon, "other")
            content_section = section_for(content_canon, family, section_map)
            instruments = [{
                "canonical": content_canon,
                "part_index": filename_index,
                "family": family,
                "section": content_section,
            }]
            matched_alias = content_canon
            alternates = content_alts
            text_match = True
            if filename_primary == content_canon:
                filename_match = True
                confidence = 0.90
                if clef or transposition:
                    confidence = min(0.98, confidence + 0.05)
                evidence = "combined"
            elif filename_primary is None:
                confidence = 0.50
                evidence = "text"
            else:
                # In-file label names a different instrument than the filename -> content wins.
                filename_match = True
                same_section = filename_section == content_section
                confidence = 0.80 if same_section else 0.70
                evidence = "combined"
                conflict = not same_section
        elif filename_facets:
            # (3) Last resort: the filename is the only signal (or a combined/doubling part whose
            # structure the flat page text cannot express). Confirm with page text when possible.
            instruments = filename_facets
            alternates = filename_alternates
            matched_alias = filename_primary
            filename_match = True
            text_match = any(
                _word_search(alias, full_norm)
                for c, alias in compiled
                if c in filename_canon_set
            )
            if text_match:
                confidence = 0.90
                if clef or transposition:
                    confidence = min(0.98, confidence + 0.05)
                evidence = "combined"
            else:
                confidence = 0.75
                evidence = "filename"
        else:
            confidence = 0.0
            evidence = "none"

    first = instruments[0] if instruments else None
    if is_score:
        predicted_part = compose_label(None, None, None, None, True, score_type)
    elif not instruments:
        predicted_part = None
    else:
        facet_labels = [
            compose_label(
                f["canonical"],
                transposition if i == 0 else None,
                f["part_index"],
                None,
                False,
                None,
            )
            for i, f in enumerate(instruments)
        ]
        predicted_part = " / ".join(label for label in facet_labels if label)
        if clef in CLEF_ABBREV:
            predicted_part = f"{predicted_part} ({CLEF_ABBREV[clef]})"

    part_sort_key = compute_part_sort_key(
        first["canonical"] if first else None,
        first["part_index"] if first else None,
        clef,
        is_score,
        score_type,
    )
    confidence = round(confidence, 3)
    tier = confidence_tier(confidence)
    # Base review flag; duplicate_in_piece is OR'd in during ensemble harmonization. A conflict
    # (in-file content contradicted the filename across sections) is surfaced for review too.
    needs_review = bool(
        (not is_score and not instruments) or confidence < LOW_CONFIDENCE or conflict
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
        "instruments": instruments,
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
        "instruments": [],
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
        if rec.get("is_score") or not rec.get("instruments"):
            continue
        facet_key = tuple(
            sorted((f.get("canonical"), f.get("part_index")) for f in rec["instruments"])
        )
        key = (
            rec.get("piece_id"),
            facet_key,
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

    Each observed-part entry carries the list of instruments the physical part represents
    (``instruments``); a combined/doubling part (e.g. "Flute 1 & Piccolo") therefore lists every
    instrument it covers, so downstream coverage in Script 04 can satisfy any matching expected
    slot. Entries are keyed by ``(instrument-set, clef)`` so identical parts collapse with a count.
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

        observed: dict[tuple[Any, Any], dict[str, Any]] = {}
        for d in parts:
            facets = d.get("instruments") or []
            key = (
                tuple(sorted((f.get("canonical"), f.get("part_index")) for f in facets)),
                d.get("clef"),
            )
            entry = observed.get(key)
            conf = d.get("confidence", 0.0)
            if entry is None:
                observed[key] = {
                    "instruments": [
                        {
                            "canonical": f.get("canonical"),
                            "part_index": f.get("part_index"),
                            "section": f.get("section", "unknown"),
                        }
                        for f in facets
                    ],
                    "clef": d.get("clef"),
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
        part_facets = [f for d in parts for f in (d.get("instruments") or [])]
        families = sorted({f.get("family") for f in part_facets if f.get("family")})
        sections = sorted({f.get("section") for f in part_facets if f.get("section")})
        distinct_instruments = len(
            {f.get("canonical") for f in part_facets if f.get("canonical")}
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
                    1 for d in parts if not d.get("instruments")
                ),
                "low_confidence_count": sum(
                    1
                    for d in parts
                    if d.get("instruments")
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
    if not rec.get("instruments"):
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
        evidence = rec.get("evidence_source") or "none"
        evidence_counts[evidence] = evidence_counts.get(evidence, 0) + 1
        if rec.get("is_score"):
            family_counts["score"] = family_counts.get("score", 0) + 1
            continue
        facets = rec.get("instruments") or []
        if not facets:
            instrument_counts["(unmatched)"] = instrument_counts.get("(unmatched)", 0) + 1
            family_counts["unknown"] = family_counts.get("unknown", 0) + 1
            continue
        for facet in facets:
            fam = facet.get("family") or "other"
            family_counts[fam] = family_counts.get(fam, 0) + 1
            canonical = facet.get("canonical") or "(unmatched)"
            instrument_counts[canonical] = instrument_counts.get(canonical, 0) + 1

    scores = [r for r in classified if r["is_score"]]
    parts = [r for r in classified if not r["is_score"]]
    unmatched = [r for r in parts if not r.get("instruments")]
    low_conf = [
        r for r in parts if r.get("instruments") and r["confidence"] < LOW_CONFIDENCE
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
            elif not rec.get("instruments"):
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
            f"{md_cell(primary_section(rec))} | {rec.get('confidence', 0.0):.2f} | "
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
