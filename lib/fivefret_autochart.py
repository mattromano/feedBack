"""Rocksmith arrangement -> 5-fret auto-charter (feedBack `source.kind: "auto"` charts).

Ported from Onyx (Michael Tolly, mtolly/onyx, commit be402a777cb668fb9133471030c055c5e6956e3e,
GPL-3.0), whose autochart is a port of Edward Haas's midi-ch (EFHIII/midi-ch, GPL-3.0).
Licensed GPL-3.0-or-later as a separate file; core feedBack is AGPL-3.0, which GPL-3.0 §13
allows combining with. Nothing else in core copies Onyx code.

Onyx files and functions this module ports (the later pieces arrive task by task):
- Mode.hs: the Rocksmith -> five-fret conversion (pitch events, fret-hand-mute notes
  left out unless they slide, `simplifyChord`, hammer-on / tap / strum selection).
- AutoChart.hs: `autoChart`, `distinguishLanes`, the lane limit for the lower levels.
- midi-ch.cpp (via Onyx's port): grouping, padding and lane assignment.
- Guitar.hs / Reductions.hs: `gryboReduce`, the reduction used for Hard / Medium / Easy.
- adjustRocksmithHST: HOPO and tap carry-over.

Port the algorithm, not the bugs:
- midi-ch.cpp l.149 dereferences `distinct[g].end()`; it is ported as `rbegin()` (the
  last pitch).
- midi-ch.cpp padding bounds `g - gd > 0` / `g + gd < groups.size() - 1` (and the
  off-by-one at l.140) become `g - gd >= 0` / `g + gd <= len(groups) - 1`.
- midi-ch pads from its groups in creation order; here groups and their notes are
  time-sorted first, so the borrowed notes are the nearest in time.
- `std::sort` on the gaps is unstable; here gaps (quantised to 1/32 beat when a beat grid
  is given) are visited in (gap, index) order.

Left out: Onyx splits a tremolo into 1/4-beat notes; here a tremolo stays a sustain.

Additions that are not in Onyx: a power chord (a 2-pitch chord with at most 2 distinct
pitch classes and an outer interval of a perfect 4th, 5th or octave) is flagged on its
`PitchEvent` so later stages can keep it to adjacent frets, as the five-fret spec asks.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import song
from drumchart import TempoMap

GENERATOR = "feedback-fivefret-autochart"
GENERATOR_VERSION = "1"

_MERGE_WINDOW = 0.001        # notes this close (seconds) form one event


@dataclass
class BeatGrid:
    """Beat times with fractional-beat conversion; falls back to 120 bpm from 0 s."""
    times: list[float] = field(default_factory=list)
    downbeats: list[bool] = field(default_factory=list)

    @classmethod
    def from_beats(cls, beats: list) -> "BeatGrid":
        bs = sorted(beats, key=lambda b: b.time)
        return cls([float(b.time) for b in bs], [b.measure >= 0 for b in bs])

    @classmethod
    def from_tempo(cls, tempo: TempoMap, end_time: float, offset: float = 0.0) -> "BeatGrid":
        end_tick = max(0.0, tempo.time_to_tick(max(0.0, end_time - offset)))
        pairs = tempo.beats(end_tick)
        return cls([tempo.tick_to_time(tick) + offset for tick, _ in pairs],
                   [measure >= 0 for _, measure in pairs])

    def _gridded(self) -> bool:
        return len(self.times) >= 2

    def _edge_len(self, i: int) -> float:
        """Seconds of the beat starting at index i (the edge beat's length past either end)."""
        n = len(self.times)
        i = min(max(i, 0), n - 2)
        return max(self.times[i + 1] - self.times[i], 1e-9)

    def beat_at(self, t: float) -> float:
        if not self._gridded():
            return t * 2.0
        i = bisect.bisect_right(self.times, t) - 1
        return i + (t - self.times[i]) / self._edge_len(i) if i >= 0 \
            else (t - self.times[0]) / self._edge_len(0)

    def time_at(self, b: float) -> float:
        if not self._gridded():
            return b * 0.5
        n = len(self.times)
        if b < 0:
            return self.times[0] + b * self._edge_len(0)
        i = min(int(b // 1), n - 1)
        return self.times[i] + (b - i) * self._edge_len(i)

    def beat_len(self, t: float) -> float:
        if not self._gridded():
            return 0.5
        return self._edge_len(bisect.bisect_right(self.times, t) - 1)

    def beats_since_measure(self, t: float) -> float:
        b = self.beat_at(t)
        if not self._gridded():
            return b % 4.0
        i = min(max(int(b // 1), 0), len(self.times) - 1)
        while i > 0 and not self.downbeats[i]:
            i -= 1
        return b - i


@dataclass
class PitchEvent:
    time: float
    pitches: tuple[int, ...]          # sorted ascending; () = muted (open)
    length: float
    sht: str                          # "strum" | "hopo" | "tap"
    slide_or_bend: bool = False
    power: bool = False               # power chord / octave, kept to adjacent frets later


def simplify_chord(pitches) -> tuple[int, ...]:
    """Onyx `simplifyChord`: keep 1-2 distinct pitches; otherwise the 2 lowest when there
    are at most 2 pitch classes, else the 3 lowest."""
    distinct = sorted(set(pitches))
    if len(distinct) <= 2:
        return tuple(distinct)
    if len({p % 12 for p in distinct}) <= 2:
        return tuple(distinct[:2])
    return tuple(distinct[:3])


def _is_power(distinct: list[int]) -> bool:
    if len(distinct) < 2 or len({p % 12 for p in distinct}) > 2:
        return False
    interval = (distinct[-1] - distinct[0]) % 12
    return interval in (5, 7) or (interval == 0 and distinct[-1] > distinct[0])


def _has_slide(n) -> bool:
    return n.slide_to >= 0 or n.slide_unpitch_to >= 0


def arrangement_events(arr) -> list[PitchEvent]:
    """Pitch events for an arrangement: notes and chord notes within 1 ms merge."""
    is_bass = song.arrangement_is_bass(arr)
    base = song.base_open_string_midis(song.arrangement_string_count(arr), is_bass)
    capo = int(getattr(arr, "capo", 0) or 0)
    tuning = arr.tuning or []

    notes = [n for n in arr.notes if not n.ignore]
    for c in arr.chords:
        notes.extend(n for n in c.notes if not n.ignore)
    notes.sort(key=lambda n: n.time)

    groups: list[list] = []
    for n in notes:
        if groups and n.time - groups[-1][0].time <= _MERGE_WINDOW:
            groups[-1].append(n)
        else:
            groups.append([n])

    events = []
    for g in groups:
        pitches = []
        for n in g:
            if (n.mute or n.fret_hand_mute) and not _has_slide(n):
                continue
            p = song.pitch_from_base(base, capo, tuning, n.string, n.fret)
            if p is not None:
                pitches.append(p)
        distinct = sorted(set(pitches))
        simplified = simplify_chord(distinct)
        if any(n.hammer_on or n.pull_off for n in g):
            sht = "hopo"
        elif any(n.tap for n in g):
            sht = "tap"
        else:
            sht = "strum"
        events.append(PitchEvent(
            time=g[0].time,
            pitches=simplified,
            length=max(max(n.sustain for n in g), 0.0),
            sht=sht,
            slide_or_bend=any(_has_slide(n) or n.bend > 0 for n in g),
            power=len(simplified) == 2 and _is_power(distinct),
        ))
    return events


# --- lane assignment: midi-ch `midich_compute` and Onyx `distinguishFrets` -------------

_GAP_RESOLUTION = 32         # gaps are quantised to 1/32 beat on a beat grid


def _power_pair(lanes: tuple[int, ...], n_lanes: int) -> tuple[int, ...]:
    """A power chord's two lanes become adjacent: (lo, lo + 1), or the top pair."""
    if len(lanes) != 2:
        return lanes
    lo = min(lanes[0], n_lanes - 2)
    return (lo, lo + 1)


def assign_lanes(groups: list[tuple[int, ...]], times: list[float], lanes: int = 5,
                 grid: BeatGrid | None = None,
                 power: list[bool] | None = None) -> list[tuple[int, ...]]:
    """midi-ch lane assignment: one sorted lane tuple (0..lanes-1) per pitch tuple.

    Notes are joined into groups of at most `lanes` distinct pitches, smallest time gap
    first; a note's lane is its pitch's rank inside its (padded) group."""
    if len(groups) != len(times):
        raise ValueError("groups and times differ in length")
    if any(not g for g in groups):
        raise ValueError("assign_lanes needs a non-empty pitch tuple per event")
    if grid is not None:
        keys = [round(grid.beat_at(t) * _GAP_RESOLUTION) for t in times]
    else:
        keys = list(times)

    # Flatten to notes sorted by time, then pitch (midi-ch's input order).
    notes = sorted(((keys[e], p, e) for e, g in enumerate(groups) for p in sorted(set(g))),
                   key=lambda n: (n[0], n[1]))
    n = len(notes)
    if n == 0:
        return []
    lane_of = [0] * n
    if n > 1:
        distinct_pitches = sorted({p for _, p, _ in notes})
        rank_index = {p: i for i, p in enumerate(distinct_pitches)}
        rank = [rank_index[p] for _, p, _ in notes]

        def ranks_of(members):
            return {rank[m] for m in members}

        group_of: list[list[int] | None] = [None] * n
        gaps = sorted(range(1, n), key=lambda i: (notes[i][0] - notes[i - 1][0], i))
        for i in gaps:
            a, b = group_of[i - 1], group_of[i]
            if a is None and b is None:
                grp = [i - 1, i]
                group_of[i - 1] = group_of[i] = grp
            elif a is None:
                if len(ranks_of(b) | {rank[i - 1]}) <= lanes:
                    b.append(i - 1)
                    group_of[i - 1] = b
                else:
                    group_of[i - 1] = [i - 1]
            elif b is None:
                if len(ranks_of(a) | {rank[i]}) <= lanes:
                    a.append(i)
                    group_of[i] = a
                else:
                    group_of[i] = [i]
            elif a is not b and len(ranks_of(a) | ranks_of(b)) <= lanes:
                a.extend(b)
                for m in b:
                    group_of[m] = a

        # Distinct groups, time-sorted, each with its notes time-sorted.
        seen: dict[int, list[int]] = {}
        for grp in group_of:
            seen.setdefault(id(grp), grp)
        ordered = sorted((sorted(grp) for grp in seen.values()), key=lambda g: g[0])

        # Pad each group to `lanes` ranks, borrowing by position from its neighbours.
        last = len(ordered) - 1
        for g, members in enumerate(ordered):
            distinct = ranks_of(members)
            d, gd = 0, 1
            while len(distinct) < lanes:
                if g - gd >= 0 and d < len(ordered[g - gd]):
                    prev = ordered[g - gd]
                    distinct.add(rank[prev[len(prev) - 1 - d]])
                if g + gd <= last and len(distinct) < lanes and d < len(ordered[g + gd]):
                    distinct.add(rank[ordered[g + gd][d]])
                d += 1
                if (g - gd < 0 or d >= len(ordered[g - gd])) and \
                        (g + gd > last or d >= len(ordered[g + gd])):
                    gd += 1
                    d = 0
                if g - gd < 0 and g + gd > last:
                    distinct.add(9999 + max(distinct))
            padded = sorted(distinct)
            for m in members:
                lane_of[m] = padded.index(rank[m])

    per_event: list[set[int]] = [set() for _ in groups]
    for (_, _, e), lane in zip(notes, lane_of):
        per_event[e].add(lane)
    out = [tuple(sorted(s)) for s in per_event]
    if power is not None:
        out = [_power_pair(t, lanes) if pw else t for t, pw in zip(out, power)]
    return out


def distinguish_lanes(groups: list[tuple[int, ...]], out: list[tuple[int, ...]],
                      lanes: int = 5,
                      power: list[bool] | None = None) -> list[tuple[int, ...]]:
    """Onyx `distinguishFrets`: when two consecutive different pitch sets came out on the
    same lanes, move one of them (a whole run of its repeats) to the nearest other lane set
    of the same size, aiming in the direction the pitch moved."""
    if len(groups) != len(out):
        raise ValueError("groups and out differ in length")
    pw = power if power is not None else [False] * len(groups)

    # Runs of consecutive events whose pitch set and lane set both repeat.
    runs: list[list[int]] = []
    for i in range(len(groups)):
        if runs and set(groups[i]) == set(groups[runs[-1][0]]) and \
                set(out[i]) == set(out[runs[-1][0]]):
            runs[-1].append(i)
        else:
            runs.append([i])
    orig = [frozenset(groups[r[0]]) for r in runs]
    auto = [frozenset(out[r[0]]) for r in runs]
    is_power = [any(pw[i] for i in r) for r in runs]

    def average(xs) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    def is_bottom(x) -> bool:
        return x == frozenset(range(len(x)))

    def is_top(x) -> bool:
        return x == frozenset(range(lanes - len(x), lanes))

    def fix_one(neighbours, aim_up: bool, first_try, adjacent: bool):
        base = average(first_try)
        choices = []
        for mask in range(1 << lanes):
            fc = frozenset(k for k in range(lanes) if mask >> k & 1)
            if len(fc) != len(first_try) or fc in neighbours:
                continue
            if adjacent and len(fc) == 2 and max(fc) - min(fc) != 1:
                continue
            distance = base - average(fc)
            wrong_way = distance > 0 if aim_up else distance < 0
            choices.append((wrong_way, abs(distance), sorted(fc), fc))
        return min(choices)[3] if choices else first_try

    prev = None
    for k in range(len(runs) - 1):
        a1, a2 = auto[k], auto[k + 1]
        if a1 == a2:
            up = average(orig[k]) <= average(orig[k + 1])
            if (not is_top(a2)) if up else (not is_bottom(a2)):
                neighbours = [a1] + ([auto[k + 2]] if k + 2 < len(runs) else [])
                auto[k + 1] = fix_one(neighbours, up, a2, is_power[k + 1])
            else:
                neighbours = [a2] + ([prev] if prev is not None else [])
                auto[k] = fix_one(neighbours, not up, a1, is_power[k])
        prev = auto[k]

    result = list(out)
    for r, lane_set in zip(runs, auto):
        for i in r:
            result[i] = tuple(sorted(lane_set))
    return result
