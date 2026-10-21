"""Metrics and pass line for the five-fret auto-charter benchmark.

Compares an auto chart with a real (hand-charted) chart of the same song part.
Pure functions over `FretNote`; no I/O. Licensed AGPL-3.0 like the rest of core
(no code from Onyx is used here).

Conventions:
- A lane is the mean set-bit index of the note's `frets` mask (green 0 .. orange 4);
  an open note (`frets == 0`) is -1.0.
- `score_levels` scores a level only when it is a key of `real` (the benchmark passes
  only hand-charted levels there). A level absent from `real` keeps `None` metrics and
  `has_real: False`; its `fret_limit_ok` is still computed from the auto chart.
- `verdict` reports, per lower level, how many paired holdout rows were left out of the
  density median for lack of a hand-charted real level (`excluded` in that check).
"""
from __future__ import annotations

import hashlib
from statistics import median

from fivefretchart import FretNote

TOL = 0.07
MIN_MATCHED = 20
MIN_SONG_PARTS = 20
MIN_ONSET_F1 = 0.50

PASS_LINE = {
    "expert_direction": 0.70,
    "expert_lane_closeness": 1.0,
    "expert_chord_size": 0.60,
    "density_low": 0.80,
    "density_high": 1.20,
}

LEVELS = ("expert", "hard", "medium", "easy")
_LIMIT_MASK = {"easy": 0b00111, "medium": 0b01111}


def lane_center(frets: int) -> float:
    """Mean set-bit index of a fret mask; open (0) is -1.0."""
    if not frets:
        return -1.0
    idx = [i for i in range(5) if frets >> i & 1]
    return sum(idx) / len(idx)


def match_notes(real, auto, tol=TOL):
    """Greedy one-to-one matching by smallest |dt| within `tol`, in time order."""
    cands = []
    j0 = 0
    for i, r in enumerate(real):
        while j0 < len(auto) and auto[j0].time < r.time - tol:
            j0 += 1
        j = j0
        while j < len(auto) and auto[j].time <= r.time + tol:
            cands.append((abs(auto[j].time - r.time), i, j))
            j += 1
    cands.sort()
    used_r, used_a, out = set(), set(), []
    for _, i, j in cands:
        if i in used_r or j in used_a:
            continue
        used_r.add(i)
        used_a.add(j)
        out.append((i, j))
    out.sort()
    return [(real[i], auto[j]) for i, j in out]


def onset_f1(real, auto, tol=TOL) -> float:
    m = len(match_notes(real, auto, tol))
    if not m:
        return 0.0
    p, r = m / len(auto), m / len(real)
    return 2 * p * r / (p + r)


def _sign(x: float) -> int:
    return (x > 0) - (x < 0)


def direction_agreement(pairs):
    """Share of consecutive matched pairs whose lane move has the same sign (0 = same)."""
    if len(pairs) < 2:
        return None
    hits = 0
    for (r0, a0), (r1, a1) in zip(pairs, pairs[1:]):
        dr = lane_center(r1.frets) - lane_center(r0.frets)
        da = lane_center(a1.frets) - lane_center(a0.frets)
        hits += _sign(dr) == _sign(da)
    return hits / (len(pairs) - 1)


def lane_closeness(pairs):
    """Min over integer shifts -4..4 of mean |auto - real + shift| (lower is closer)."""
    if not pairs:
        return None
    diffs = [lane_center(a.frets) - lane_center(r.frets) for r, a in pairs]
    return min(sum(abs(d + s) for d in diffs) / len(diffs) for s in range(-4, 5))


def _size(frets: int) -> int:
    return max(1, bin(frets).count("1"))


def chord_size_agreement(pairs):
    if not pairs:
        return None
    return sum(_size(r.frets) == _size(a.frets) for r, a in pairs) / len(pairs)


def hopo_agreement(pairs):
    if not pairs:
        return None
    return sum((r.hopo or r.tap) == (a.hopo or a.tap) for r, a in pairs) / len(pairs)


def density_ratio(real, auto):
    """auto / real notes per second over the overlap window [max(first), min(last)]."""
    if not real or not auto:
        return None
    lo = max(real[0].time, auto[0].time)
    hi = min(real[-1].time, auto[-1].time)
    if hi <= lo:
        return None
    n_real = sum(lo <= n.time <= hi for n in real)
    n_auto = sum(lo <= n.time <= hi for n in auto)
    if not n_real:
        return None
    return n_auto / n_real  # same window, so the per-second rates divide to counts


def fret_limit_ok(notes, level: str) -> bool:
    mask = _LIMIT_MASK.get(level)
    if mask is None:
        return True
    return all(n.frets & ~mask == 0 for n in notes)


def score_levels(real, auto):
    """Per-level metrics; only levels that are keys of `real` get scored (see module doc)."""
    out = {}
    for lv in LEVELS:
        a = auto.get(lv) or []
        entry = {"has_real": lv in real, "matched": None, "onset_f1": None,
                 "direction": None, "lane_closeness": None, "chord_size": None,
                 "hopo": None, "density_ratio": None,
                 "fret_limit_ok": fret_limit_ok(a, lv)}
        if lv in real:
            r = real[lv]
            pairs = match_notes(r, a)
            entry.update(matched=len(pairs), onset_f1=onset_f1(r, a),
                         direction=direction_agreement(pairs),
                         lane_closeness=lane_closeness(pairs),
                         chord_size=chord_size_agreement(pairs),
                         hopo=hopo_agreement(pairs),
                         density_ratio=density_ratio(r, a))
        out[lv] = entry
    return out


def split(name: str) -> str:
    return "tune" if hashlib.sha1(name.encode()).digest()[0] % 2 == 0 else "holdout"


def _med(values):
    vals = [v for v in values if v is not None]
    return median(vals) if vals else None


def verdict(rows):
    """Pass/fail on the paired holdout rows; see PASS_LINE. `n` counts those rows."""
    used = [r for r in rows if r.get("paired") and r.get("split") == "holdout"]
    n = len(used)
    checks = [{"name": "enough_songs", "level": None, "value": n,
               "line": MIN_SONG_PARTS, "ok": n >= MIN_SONG_PARTS}]

    exp_rows = [r["levels"]["expert"] for r in used
                if (r["levels"]["expert"].get("matched") or 0) >= MIN_MATCHED]
    for name, key, line, higher in (
            ("direction", "direction", PASS_LINE["expert_direction"], True),
            ("lane_closeness", "lane_closeness", PASS_LINE["expert_lane_closeness"], False),
            ("chord_size", "chord_size", PASS_LINE["expert_chord_size"], True)):
        v = _med(e[key] for e in exp_rows)
        ok = v is not None and (v >= line if higher else v <= line)
        checks.append({"name": name, "level": "expert", "value": v, "line": line,
                       "ok": ok, "rows": len(exp_rows)})

    for lv in ("hard", "medium", "easy"):
        entries = [r["levels"][lv] for r in used]
        have = [e for e in entries if e.get("has_real")]
        v = _med(e["density_ratio"] for e in have)
        ok = v is not None and PASS_LINE["density_low"] <= v <= PASS_LINE["density_high"]
        checks.append({"name": "density_ratio", "level": lv, "value": v,
                       "line": (PASS_LINE["density_low"], PASS_LINE["density_high"]),
                       "ok": ok, "excluded": len(entries) - len(have)})

    for lv in ("medium", "easy"):
        bad = sum(not r["levels"][lv]["fret_limit_ok"] for r in used)
        checks.append({"name": "fret_limit", "level": lv, "value": bad, "line": 0,
                       "ok": bad == 0})

    return {"pass": all(c["ok"] for c in checks), "n": n, "checks": checks}
