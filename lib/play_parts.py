"""Play parts — the instrument/part dimension of scored stats.

A *part key* names what was played in a scored run:

    ''                 real guitar / bass / keys via note detection (the
                       original song_stats path, keyed by arrangement)
    'drums'            drums (MIDI kit or drums controller)
    'five_fret:guitar' a 5-fret controller on the guitar part
    'five_fret:rhythm' / 'five_fret:bass' / ...
    'vocals'           sung parts

The frontend recorder builds the key from a ``play:result`` player
(``instrument`` + ``part``, see the play-layer contract §3); the server only
validates its shape. Pure + flat-importable (constitution Principle V).
"""

from __future__ import annotations

import re

__all__ = ["DIFFICULTIES", "normalize_part", "normalize_difficulty", "instrument_for_part"]

DIFFICULTIES = ("easy", "medium", "hard", "expert")

_PART_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}(?::[a-z][a-z0-9_]{0,23})?$")


def normalize_part(value) -> str | None:
    """'' for missing/empty (real guitar), the lower-cased key when it is
    well formed, else None (reject)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v:
        return ""
    return v if _PART_RE.fullmatch(v) else None


def normalize_difficulty(value) -> str | None:
    """'' for missing/empty, one of DIFFICULTIES, else None (reject)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    if not v:
        return ""
    return v if v in DIFFICULTIES else None


def instrument_for_part(part: str) -> str | None:
    """Progression/career instrument for a part key: the part before ':'
    ('drums' -> 'drums', 'five_fret:bass' -> 'five_fret'). None for ''
    (real guitar resolves its instrument from the arrangement instead)."""
    if not part:
        return None
    return part.split(":", 1)[0]
