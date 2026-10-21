"""Five-fret (Rock Band / Clone Hero style) guitar/bass part helpers.

Core's reader-side validator and WebSocket wire helpers for the feedpak
`five_fret_<id>.json` file (schema: feedpak-spec `schemas/five-fret.schema.json`).

Note: `{t, f, l?, h?, tap?}` where `f` is a 0-31 fret bitmask (bit0 green ..
bit4 orange; 0 = open).
"""

from __future__ import annotations

import math

from drums import ranges_to_wire  # noqa: F401  (re-exported; same range shape)

SCHEMA_VERSION = 1
PARTS = ("guitar", "rhythm", "bass")
LEVELS = ("easy", "medium", "hard")

_ORIGINS = ("chorus", "local", "rb")
_SOURCE_KINDS = ("chart", "auto")
_SOURCE_STRINGS = ("md5", "charter", "generator", "generator_version", "from_arrangement")


def _num(v: object) -> float | None:
    """Finite JSON number (bool excluded) as float, else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    f = float(v)
    return f if math.isfinite(f) else None


def _note_error(n: object) -> str:
    """Reason a note violates the schema, or "" if valid."""
    if not isinstance(n, dict):
        return "must be an object"
    if "t" not in n or "f" not in n:
        return "requires t and f"
    t = _num(n["t"])
    if t is None or t < 0:
        return "t must be a number >= 0"
    f = n["f"]
    if isinstance(f, bool) or not isinstance(f, (int, float)) or not math.isfinite(f) \
            or f != int(f) or not 0 <= f <= 31:
        return "f must be an integer 0-31"
    if "l" in n:
        ln = _num(n["l"])
        if ln is None or ln <= 0:
            return "l must be a number > 0"
    for k in ("h", "tap"):
        if k in n and not isinstance(n[k], bool):
            return f"{k} must be a boolean"
    return ""


def _notes_error(notes: object, where: str) -> str:
    if not isinstance(notes, list):
        return f"{where} must be a list"
    for i, n in enumerate(notes):
        why = _note_error(n)
        if why:
            return f"{where}[{i}]: {why}"
    return ""


def _ranges_error(ranges: object, where: str) -> str:
    if not isinstance(ranges, list):
        return f"{where} must be a list"
    for i, r in enumerate(ranges):
        if not isinstance(r, dict) or _num(r.get("start_time")) is None \
                or _num(r.get("end_time")) is None:
            return f"{where}[{i}] needs numeric start_time and end_time"
    return ""


def validate_five_fret(data: object) -> tuple[bool, str]:
    """Strict check of a parsed `five_fret_<id>.json` payload.

    Returns `(ok, reason)`; mirrors five-fret.schema.json (unknown keys are
    allowed, as in the schema). Unlike `drums.validate_drum_tab`, individual
    notes ARE validated: the schema requires every note to be well formed.
    """
    if not isinstance(data, dict):
        return False, "five_fret payload must be a JSON object"
    ver = data.get("version")
    if isinstance(ver, bool) or not isinstance(ver, (int, float)) or ver != SCHEMA_VERSION:
        return False, f"five_fret.version must be {SCHEMA_VERSION}"
    if "name" in data and not isinstance(data["name"], str):
        return False, "five_fret.name must be a string"
    if "instrument" in data and data["instrument"] not in PARTS:
        return False, f"five_fret.instrument must be one of {', '.join(PARTS)}"
    src = data.get("source")
    if not isinstance(src, dict):
        return False, "five_fret.source must be an object"
    if src.get("kind") not in _SOURCE_KINDS:
        return False, "five_fret.source.kind must be 'chart' or 'auto'"
    if "origin" in src and src["origin"] not in _ORIGINS:
        return False, "five_fret.source.origin must be one of chorus, local, rb"
    for k in _SOURCE_STRINGS:
        if k in src and not isinstance(src[k], str):
            return False, f"five_fret.source.{k} must be a string"
    if "notes" not in data:
        return False, "five_fret.notes is required"
    why = _notes_error(data["notes"], "five_fret.notes")
    if why:
        return False, why
    if "levels" in data:
        levels = data["levels"]
        if not isinstance(levels, dict):
            return False, "five_fret.levels must be an object"
        for lid in LEVELS:
            if lid not in levels:
                continue
            lv = levels[lid]
            where = f"five_fret.levels.{lid}"
            if not isinstance(lv, dict) or "notes" not in lv:
                return False, f"{where} must be an object with notes"
            why = _notes_error(lv["notes"], f"{where}.notes")
            if why:
                return False, why
            if "generated" in lv and not isinstance(lv["generated"], bool):
                return False, f"{where}.generated must be a boolean"
    for key in ("star_power", "solos"):
        if key in data:
            why = _ranges_error(data[key], f"five_fret.{key}")
            if why:
                return False, why
    return True, ""


def notes_to_wire(notes: list) -> list[dict]:
    """Normalise notes to the wire form, dropping invalid ones, sorted by (t, f).

    Wire keys: `t` (seconds, 3dp), `f` (mask); `l` (sustain seconds), `h`
    (HOPO), `tap` only when set.
    """
    out: list[dict] = []
    if not isinstance(notes, list):
        return out
    for n in notes:
        if _note_error(n):
            continue
        w: dict = {"t": round(float(n["t"]), 3), "f": int(n["f"])}
        if "l" in n:
            w["l"] = round(float(n["l"]), 3)
        if n.get("h") is True:
            w["h"] = True
        if n.get("tap") is True:
            w["tap"] = True
        out.append(w)
    out.sort(key=lambda w: (w["t"], w["f"]))
    return out


def levels_to_wire(levels: dict | None) -> dict:
    """`{level: {"total": n, "generated": bool}}` for the closed easy/medium/hard ids.

    A level with no valid notes is dropped (nothing to play), as in
    `drums.levels_to_wire`. Note bodies are streamed separately.
    """
    out: dict = {}
    if not isinstance(levels, dict):
        return out
    for lid in LEVELS:
        lv = levels.get(lid)
        if not isinstance(lv, dict):
            continue
        total = len(notes_to_wire(lv.get("notes")))
        if not total:
            continue
        out[lid] = {"total": total, "generated": lv.get("generated") is True}
    return out
