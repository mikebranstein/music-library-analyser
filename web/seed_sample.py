#!/usr/bin/env python3
"""Seed web/data/ with the committed made-up sample so the scaffold renders offline.

web/data/ is intentionally NOT source-controlled (real runs derive from copyrighted
sheet music). This copies the fully fictional fixture from web/data-sample/ into
web/data/ so you can preview the site without running the full pipeline.

    python web/seed_sample.py           # copy sample -> web/data/ (skips existing)
    python web/seed_sample.py --force   # overwrite web/data/ with the sample
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent
SAMPLE_DIR = WEB_DIR / "data-sample"
DATA_DIR = WEB_DIR / "data"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="overwrite existing web/data/ files")
    args = parser.parse_args()

    if not SAMPLE_DIR.is_dir():
        print(f"Sample directory not found: {SAMPLE_DIR}")
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    for src in sorted(SAMPLE_DIR.glob("*.js")):
        dst = DATA_DIR / src.name
        if dst.exists() and not args.force:
            skipped += 1
            continue
        shutil.copy2(src, dst)
        copied += 1

    print(f"Seeded {copied} file(s) into {DATA_DIR}" + (f" ({skipped} skipped)" if skipped else ""))
    if skipped and not args.force:
        print("Some files already existed; re-run with --force to overwrite.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
