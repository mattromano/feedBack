#!/usr/bin/env python3
"""Clone Hero / YARG / Rock Band song folder (or .sng) -> a drums / 5-fret feedpak.

Usage:
    python scripts/ch_to_sloppak.py SONG_FOLDER_OR_SNG OUT.sloppak
           [--title T --artist A --album X --year N] [--dir] [-v]

The conversion lives in lib/ch_convert.py (what goes into the pack is documented
there); the app uses the same code for its Clone Hero songs folder.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "lib"))

from ch_convert import (  # noqa: E402,F401  (re-exported for older callers)
    FEEDPAK_VERSION, audio_seconds, build_stems, convert, encode_stem, find_cover, five_fret_name,
    parse_year, prepare_drums, prepare_five_fret, zip_dir,
)

log = logging.getLogger("feedBack.scripts.ch_to_sloppak")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path, help="CH/YARG song folder or .sng package")
    ap.add_argument("out", type=Path)
    ap.add_argument("--title")
    ap.add_argument("--artist")
    ap.add_argument("--album")
    ap.add_argument("--year", type=int)
    ap.add_argument("--dir", action="store_true", help="write the directory form instead of a zip")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(levelname)s %(message)s")
    try:
        s = convert(a.src, a.out, a.title, a.artist, a.album, a.year, a.dir)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as e:
        log.error("%s", e)
        return 1
    log.info("wrote %s: %d drum notes (%s), %d star power, %d fills, %d solos, %d sections, "
             "delay %+.3fs, generated levels %s, stems %s; 5-fret: %s; vocals: %d syllables, %d pitched, "
             "%d phrases", s["out"], s["notes"], s["format"], s["star_power"], s["fills"], s["solos"],
             s["sections"], s["delay"], ", ".join(s["levels_generated"]) or "none", ", ".join(s["stems"]),
             ", ".join(f"{p} {n} notes" for p, n in s["five_fret"].items()) or "none",
             s["lyrics"], s["vocal_pitch"], s["vocal_phrases"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
