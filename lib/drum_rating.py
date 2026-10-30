"""Overall drum difficulty for the library, on Rock Band / Clone Hero's 0-6 tier
scale (song.ini ``diff_drums``), and whether a drum chart's easier levels were made
by software.

A pack carries no authored tier, so ``estimate_tier`` rates the Expert chart (the
drum tab's top-level ``hits``) from three numbers: how busy its busiest stretches are,
how often the hands play and how often they move between pads. The weights are a
least-squares fit to 120 hand-tiered Rock Band charts (Green Day, The Beatles);
left out of the fit one at a time, 71% landed within one tier of the authored one
(mean error 1.1 tiers). Tiers are only comparable within a game, so the UI marks
an estimate as one. Nothing here is written into a pack: it is library metadata.
"""

from __future__ import annotations

import math

TIER_NAMES = ("Warmup", "Apprentice", "Solid", "Moderate", "Challenging", "Nightmare", "Impossible")
MAX_TIER = len(TIER_NAMES) - 1

# tier = PEAK * peak + HANDS * hand_onsets_per_s + MOVES * pad_changes_per_s + BIAS
_PEAK, _HANDS, _MOVES, _BIAS = 0.5351, 0.346, -0.6106, -1.1145
_WINDOW_S = 4           # peak density: hits per second over 4 s windows ...
_PEAK_PCT = 0.95        # ... at the 95th percentile (a fill alone doesn't make a song hard)


def _percentile(vals: list[float], q: float) -> float:
    s = sorted(vals)
    if not s:
        return 0.0
    k = (len(s) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def chart_features(hits: list) -> dict | None:
    """The three rating inputs for drum-tab hits (``[{"t", "p", ...}]``), or None
    when there are none to rate."""
    pts = sorted((float(h["t"]), str(h["p"])) for h in hits or []
                 if isinstance(h, dict) and isinstance(h.get("t"), (int, float)) and h.get("p"))
    if not pts:
        return None
    span = max(pts[-1][0] - pts[0][0], 1.0)
    onsets: dict[float, set] = {}
    for t, p in pts:
        onsets.setdefault(round(t, 3), set()).add(p)
    hand = [frozenset(ps - {"kick"}) for _t, ps in sorted(onsets.items()) if ps - {"kick"}]
    moves = sum(1 for a, b in zip(hand, hand[1:]) if a != b)
    bins = [0] * (int(span) + 1)
    for t, _p in pts:
        bins[min(int(t - pts[0][0]), len(bins) - 1)] += 1
    if len(bins) >= _WINDOW_S:
        dens = [sum(bins[i:i + _WINDOW_S]) / _WINDOW_S for i in range(len(bins) - _WINDOW_S + 1)]
    else:
        dens = [float(b) for b in bins]
    return {"peak": _percentile(dens, _PEAK_PCT), "hands": len(hand) / span, "moves": moves / span}


def estimate_tier(hits: list) -> int | None:
    """0-6 for an Expert drum chart's hits, None when it has none."""
    f = chart_features(hits)
    if f is None:
        return None
    raw = _PEAK * f["peak"] + _HANDS * f["hands"] + _MOVES * f["moves"] + _BIAS
    return max(0, min(MAX_TIER, int(math.floor(raw + 0.5))))


def levels_generated(tab: dict) -> bool:
    """True when any of the tab's easier ``levels`` is marked ``generated`` (reduced
    from Expert by software rather than charted by hand)."""
    levels = tab.get("levels") if isinstance(tab, dict) else None
    if not isinstance(levels, dict):
        return False
    return any(isinstance(lv, dict) and lv.get("generated") is True for lv in levels.values())


def ini_tier(ini: dict) -> int | None:
    """The authored tier from a song.ini (``diff_drums``, else the pro-drums keys);
    None when it has none."""
    for key in ("diff_drums", "diff_drums_real", "diff_drums_real_ps"):
        try:
            v = int(float(str(ini.get(key, "")).strip()))
        except ValueError:
            continue
        if v >= 0:
            return min(v, MAX_TIER)
    return None
