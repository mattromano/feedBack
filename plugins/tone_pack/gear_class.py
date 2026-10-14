"""Classify a Rocksmith tone definition into a Tone Automation category from its gear.

Used for tones whose *name* the Audio plugin's keyword classifier can't place ("Tone 1", "Default",
"George_Rhythm", "processed"...), and for every tone of a Bass arrangement. Pure stdlib (the
desktop app's bundled Python has no numpy).
"""

from __future__ import annotations

import re

DIST_PEDAL = re.compile(r"fuzz|distortion|buzz|shred|octavius|metal|hyper|dist\b", re.I)
OD_PEDAL = re.compile(r"drive|boost|screamer|overdrive|germanium", re.I)
MOD_PEDAL = re.compile(r"chorus|flang|phase|phaser|trem|wah|rotat|vibe|filter|omnimod|leslie|uni", re.I)
ACOUSTIC = re.compile(r"acoustic", re.I)
SOLO_NAME = re.compile(r"lead|solo", re.I)

# Amp gain (0-100) thresholds, fitted on tones whose names say clean / crunch / dist.
GAIN_DIST = 70.0   # named dist tones: median gain 82
GAIN_OD = 55.0     # named clean: median 32, crunch: 65 (clean 81% / dist 68% agreement)


def _pedal_keys(gear: dict) -> list[str]:
    out = []
    for slot, g in (gear or {}).items():
        if g and ("Pedal" in slot) and g.get("Key"):
            out.append(g["Key"])
    return out


def _amp(gear: dict) -> tuple[str, float | None]:
    a = (gear or {}).get("Amp") or {}
    key = a.get("Key") or ""
    gain = None
    for k, v in (a.get("KnobValues") or {}).items():
        if k.endswith("_Gain"):
            try:
                gain = float(v)
            except (TypeError, ValueError):
                pass
    return key, gain


def classify(definition: dict | None, name: str = "", bass_arrangement: bool = False) -> str | None:
    """'bass' | 'acoustic' | 'dist' | 'od' | 'mod' | 'clean' | 'solo', or None without gear."""
    if bass_arrangement:
        return "bass"
    if name and SOLO_NAME.search(name):
        return "solo"
    if not definition:
        return None
    gear = definition.get("GearList") or {}
    amp, gain = _amp(gear)
    if amp.startswith("Bass_"):
        return "bass"
    pedals = _pedal_keys(gear)
    if any(ACOUSTIC.search(p) for p in pedals):
        return "acoustic"
    if any(DIST_PEDAL.search(p) for p in pedals) or (gain is not None and gain >= GAIN_DIST):
        return "dist"
    if any(OD_PEDAL.search(p) for p in pedals) or (gain is not None and gain >= GAIN_OD):
        return "od"
    if any(MOD_PEDAL.search(p) for p in pedals):
        return "mod"
    return "clean"
