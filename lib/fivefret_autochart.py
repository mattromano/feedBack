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

Lower levels (`reduce_level`, `limit_lanes`, `auto_levels`), where they differ from Onyx:
- Onyx removes open notes before reducing (`noOpenNotes`); here an open note stays open
  through `reduce_level` (it never takes part in a fast-jump shift) and only gets a lane
  when `limit_lanes` re-charts a level (open counts as Green there, as in `limitFrets`).
- Beat positions are integer ticks (1920 a beat); "on a measure line" and "on the grid
  unit" allow 1/128 beat for millisecond-rounded Rocksmith times.
- The padding test is a plain strict +-padding window (Onyx's saturating `posn -| padding`
  would let a note sit next to a kept note at beat 0). It and the other beat-length tests
  (grace gap, fast-jump reach, one-beat sustain floor) allow the same 1/128 beat, so
  steady notes at ms-rounded times are not dropped at non-round tempos.
- A fast-jump shift is clamped to the highway (Onyx's `pred` / `succ` would fail there).
- `auto_levels(only_missing=True)` keeps any level the chart has with notes, as Plan 1's
  `ensure_levels` does (Onyx's `orIfNull` regenerates a level with 5 or fewer notes).
- `limitFretsDifficulty` re-runs `noExtendedSustains'` before limiting; after
  `gryboReduce`'s one-beat sustain pull-back that changes nothing, so it is left out.

Additions that are not in Onyx: a power chord (a 2-pitch chord with at most 2 distinct
pitch classes and an outer interval of a perfect 4th, 5th or octave) is flagged on its
`PitchEvent` so later stages can keep it to adjacent frets, as the five-fret spec asks.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

import song
from drumchart import TempoMap
from fivefretchart import FiveFretChart, FretNote

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


# --- Expert chart: sustains, HOPO / tap carry-over, assembly ----------------------------

BLIP_BEATS = 3 / 8          # Guitar.hs standardBlipThreshold
SUSTAIN_GAP_BEATS = 1 / 8   # Guitar.hs standardSustainGap
_HST_LONG = 0.5             # Mode.hs adjustRocksmithHST timeLong (seconds)
_HST_SHORT = 0.1            # ... timeShort
_EPS = 1e-9


def apply_sustains(notes: list[FretNote], grid: BeatGrid, keep: list[bool]) -> None:
    """Onyx `noExtendedSustains'`: end each sustain at least 1/8 beat before the next note;
    one then shorter than 3/8 beat becomes 0, unless `keep[i]` (a slide or bend), which is
    only trimmed."""
    if len(keep) != len(notes):
        raise ValueError("notes and keep differ in length")
    for i, n in enumerate(notes):
        if n.length <= 0:
            n.length = 0.0
            continue
        start = grid.beat_at(n.time)
        length = grid.beat_at(n.time + n.length) - start
        if i + 1 < len(notes):
            gap = grid.beat_at(notes[i + 1].time) - start
            length = min(length, max(gap - SUSTAIN_GAP_BEATS, 0.0))
        if length <= _EPS or (length < BLIP_BEATS - _EPS and not keep[i]):
            n.length = 0.0
        else:
            n.length = grid.time_at(start + length) - n.time


def _is_single(n: FretNote) -> bool:
    return bin(n.frets).count("1") <= 1


def _sht(n: FretNote) -> str:
    return "tap" if n.tap else "hopo" if n.hopo else "strum"


def _set_sht(n: FretNote, sht: str) -> None:
    n.tap, n.hopo = sht == "tap", sht == "hopo"


def apply_hst(notes: list[FretNote]) -> None:
    """Onyx `adjustRocksmithHST` (seconds, in order): a HOPO right after a tap becomes a
    tap; so does a HOPO after a gap of 0.5 s or more that is followed by a tap; and a strummed
    single note within 0.1 s of a single note of another colour becomes a HOPO when the
    previous note is not a tap and this one is not open. The first note's gap runs from 0 s."""
    for i in range(len(notes) - 1):
        cur, nxt = notes[i], notes[i + 1]
        prev_t = notes[i - 1].time if i > 0 else 0.0
        if _sht(cur) == "tap" and _sht(nxt) == "hopo":
            _set_sht(nxt, "tap")
        elif _sht(cur) == "hopo" and cur.time - prev_t >= _HST_LONG \
                and _sht(nxt) == "tap":
            _set_sht(cur, "tap")
        elif _is_single(cur) and _is_single(nxt) \
                and _sht(nxt) == "strum" and nxt.time - cur.time <= _HST_SHORT \
                and cur.frets != nxt.frets and _sht(cur) != "tap" and nxt.frets != 0:
            _set_sht(nxt, "hopo")


def autochart(arr, grid: BeatGrid, part: str) -> FiveFretChart:
    """Expert-only 5-fret chart for an arrangement (`source_format == "auto"`, no levels)."""
    events = arrangement_events(arr)
    pitched = [e for e in events if e.pitches]
    lanes = assign_lanes([e.pitches for e in pitched], [e.time for e in pitched], 5,
                         grid=grid, power=[e.power for e in pitched])
    lanes = distinguish_lanes([e.pitches for e in pitched], lanes, 5,
                              power=[e.power for e in pitched])
    lane_iter = iter(lanes)
    notes = []
    for e in events:
        frets = sum(1 << lane for lane in next(lane_iter)) if e.pitches else 0
        notes.append(FretNote(time=e.time, frets=frets, length=e.length,
                              hopo=e.sht == "hopo", tap=e.sht == "tap"))
    apply_sustains(notes, grid, [e.slide_or_bend for e in events])
    apply_hst(notes)
    return FiveFretChart(part=part, notes=notes, tempo=TempoMap(resolution=480),
                         source_format="auto")


# --- Lower levels: Onyx `gryboReduce`, `limitFrets`, `gryboComplete` -------------------

TICKS_PER_BEAT = 1920        # beat positions are compared as integer ticks (1920 = 480 * 4)
_ALIGN_TOL = TICKS_PER_BEAT // 128   # 1/128 beat: absorbs millisecond-rounded note times
# level -> (grid unit / padding / jump time in ticks, jump limit in frets)
_REDUCE = {"hard": (TICKS_PER_BEAT // 2, 3),
           "medium": (TICKS_PER_BEAT, 2),
           "easy": (2 * TICKS_PER_BEAT, 2)}
_SHT_STRENGTH = {"strum": 0, "hopo": 1, "tap": 2}


def _lanes_of(frets: int) -> tuple[int, ...]:
    return tuple(i for i in range(5) if frets >> i & 1)


def _frets_of(lanes) -> int:
    return sum(1 << lane for lane in set(lanes))


class _Meter:
    """Tick positions on a beat grid, plus measure-line and alignment tests."""

    def __init__(self, grid: BeatGrid):
        self.grid = grid
        if grid._gridded():
            downs = [i for i, d in enumerate(grid.downbeats) if d] or [0]
            self.measure_ticks = [i * TICKS_PER_BEAT for i in downs]
        else:
            self.measure_ticks = None             # 120 bpm fallback: 4/4 from beat 0

    def tick(self, t: float) -> int:
        return round(self.grid.beat_at(t) * TICKS_PER_BEAT)

    def measure_pos(self, tick: int) -> int:
        probe = tick + _ALIGN_TOL                 # a note just before a bar line is on it
        if self.measure_ticks is None:
            bar = 4 * TICKS_PER_BEAT
            return tick - (probe // bar) * bar
        i = max(bisect.bisect_right(self.measure_ticks, probe) - 1, 0)
        return tick - self.measure_ticks[i]

    def aligned(self, tick: int, unit: int) -> bool:
        r = self.measure_pos(tick) % unit
        return min(r, unit - r) <= _ALIGN_TOL

    def on_measure(self, tick: int) -> bool:
        return abs(self.measure_pos(tick)) <= _ALIGN_TOL


def _overdrive_test(star_power):
    """Onyx `isOD`: true inside a (merged) phrase, from its start up to but not its end."""
    spans: list[list[float]] = []
    for a, b in sorted(star_power):
        if spans and a <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], b)
        else:
            spans.append([a, b])
    starts = [a for a, _ in spans]

    def is_od(t: float) -> bool:
        i = bisect.bisect_right(starts, t + _EPS) - 1
        return i >= 0 and t < spans[i][1] - _EPS
    return is_od


@dataclass
class _GNote:
    time: float
    tick: int
    lanes: tuple[int, ...]     # () = open
    sht: str
    length: float              # seconds; 0 = no sustain
    src_tick: int = 0


def _chord_rule(lanes: tuple[int, ...], level: str) -> tuple[int, ...]:
    if len(lanes) < 2:
        return lanes
    lo, hi = lanes[0], lanes[-1]
    if level == "hard":
        return (0, 3) if (lo, hi) == (0, 4) else (lo, hi)
    if level == "medium":
        if lo == 0:
            return (0, min(hi, 2))
        if lo == 1:
            return (1, min(hi, 3))
        return (lo, hi)
    return (lo,)


def reduce_level(notes: list[FretNote], level: str, grid: BeatGrid,
                 star_power=()) -> list[FretNote]:
    """Onyx `gryboReduce`: one level (`"hard"`, `"medium"` or `"easy"`) from the level
    above it. Steps, in Onyx's order: sustain clean-up, grace notes, chord shapes, kept
    notes by priority and padding, HOPOs, chord -> HOPO chord, fast jumps, HOPOs on the
    same top fret, and (Medium / Easy) sustains pulled back a beat from the next note."""
    if level not in _REDUCE:
        raise ValueError(f"unknown level {level!r}")
    unit, jump = _REDUCE[level]
    meter = _Meter(grid)
    is_od = _overdrive_test(star_power)

    # readGuitarNotes -> guitarify': noExtendedSustains' on the source level.
    src = [FretNote(time=n.time, frets=n.frets, length=n.length, hopo=n.hopo, tap=n.tap,
                    tick=n.tick) for n in sorted(notes, key=lambda n: n.time)]
    apply_sustains(src, grid, [False] * len(src))
    gs = [_GNote(n.time, meter.tick(n.time), _lanes_of(n.frets), _sht(n), n.length, n.tick)
          for n in src]

    def end_tick(g: _GNote) -> int:
        return meter.tick(g.time + g.length)

    # Grace notes: an unsustained strum, then within 1/4 beat a sustained HOPO.
    graced: list[_GNote] = []
    i = 0
    while i < len(gs):
        a = gs[i]
        if i + 1 < len(gs):
            b = gs[i + 1]
            if a.sht == "strum" and a.length <= 0 and b.sht == "hopo" and b.length > 0 \
                    and b.tick - a.tick <= TICKS_PER_BEAT // 4 + _ALIGN_TOL \
                    and meter.aligned(a.tick, TICKS_PER_BEAT // 2) \
                    and not (not is_od(a.time) and is_od(b.time)):
                graced.append(_GNote(a.time, a.tick, b.lanes, "strum",
                                     b.time + b.length - a.time, a.src_tick))
                i += 2
                continue
        graced.append(a)
        i += 1

    # Chord shapes.
    for g in graced:
        g.lanes = _chord_rule(g.lanes, level)

    # Kept notes: lowest priority first (ties by time); a note is kept unless a kept note
    # lies strictly within +- the padding (narrowed by _ALIGN_TOL for ms-rounded times).
    def priority(g: _GNote) -> int:
        return (0 if is_od(g.time) else 8) + (0 if g.length > 0 else 4) \
            + (0 if meter.on_measure(g.tick) else 2) + (0 if meter.aligned(g.tick, unit) else 1)
    order = sorted(range(len(graced)), key=lambda k: (priority(graced[k]), graced[k].tick, k))
    kept_ticks: list[int] = []
    keep = [False] * len(graced)
    for k in order:
        t = graced[k].tick
        j = bisect.bisect_right(kept_ticks, t - unit + _ALIGN_TOL)
        if j < len(kept_ticks) and kept_ticks[j] < t + unit - _ALIGN_TOL:
            continue
        bisect.insort(kept_ticks, t)
        keep[k] = True
    out = [g for g, kp in zip(graced, keep) if kp]

    # HOPOs: Hard strums only unsustained single-note HOPOs; Medium / Easy strum all.
    for g in out:
        if level != "hard" or (g.sht == "hopo" and len(g.lanes) <= 1 and g.length <= 0):
            g.sht = "strum"

    # A chord followed by a HOPO chord becomes its lowest note.
    for a, b in zip(out, out[1:]):
        if len(a.lanes) >= 2 and len(b.lanes) >= 2 and b.sht == "hopo":
            a.lanes = (a.lanes[0],)

    # Fast jumps (both directions; the gap also counts from the previous sustain's end):
    # the next note moves one fret toward this one, clamped to the highway.
    for a, b in zip(out, out[1:]):
        if not a.lanes or not b.lanes:
            continue
        reach = unit + _ALIGN_TOL
        near = b.tick - a.tick <= reach or (a.length > 0 and b.tick - end_tick(a) <= reach)
        if not near:
            continue
        if max(b.lanes) - min(a.lanes) > jump:
            b.lanes = tuple(sorted({max(lane - 1, 0) for lane in b.lanes}))
        elif max(a.lanes) - min(b.lanes) > jump:
            b.lanes = tuple(sorted({min(lane + 1, 4) for lane in b.lanes}))

    # A HOPO with the same top fret as the note before it becomes a strum.
    for a, b in zip(out, out[1:]):
        if b.sht == "hopo" and max(a.lanes, default=-1) == max(b.lanes, default=-1):
            b.sht = "strum"

    # Medium / Easy: a sustain ends at least a beat before the next note, else goes.
    if level != "hard":
        for a, b in zip(out, out[1:]):
            if a.length <= 0:
                continue
            cap = b.tick - a.tick - TICKS_PER_BEAT
            if min(end_tick(a) - a.tick, cap) < TICKS_PER_BEAT - _ALIGN_TOL:
                a.length = 0.0
            elif cap < end_tick(a) - a.tick:
                a.length = grid.time_at((a.tick + cap) / TICKS_PER_BEAT) - a.time

    return [FretNote(time=g.time, frets=_frets_of(g.lanes), length=max(g.length, 0.0),
                     hopo=g.sht == "hopo", tap=g.sht == "tap", tick=g.src_tick) for g in out]


def limit_lanes(notes: list[FretNote], lanes: int,
                grid: BeatGrid | None = None) -> list[FretNote]:
    """Onyx `limitFrets`: when a note uses a lane at or past `lanes`, re-chart the level
    with midi-ch (`assign_lanes` then `distinguish_lanes`) on the lane numbers as pitches
    (an open note counts as Green, and comes out on a lane). Notes at one time merge,
    keeping the strongest of tap > hopo > strum and the shortest sustain. A level that
    already fits is returned as is (copied)."""
    copies = [FretNote(time=n.time, frets=n.frets, length=n.length, hopo=n.hopo, tap=n.tap,
                       tick=n.tick) for n in sorted(notes, key=lambda n: n.time)]
    if max((max(_lanes_of(n.frets), default=0) for n in copies), default=0) < lanes:
        return copies
    positions: list[list[FretNote]] = []
    for n in copies:
        if positions and n.time == positions[-1][0].time:
            positions[-1].append(n)
        else:
            positions.append([n])
    groups = [tuple(sorted({lane for n in pos for lane in (_lanes_of(n.frets) or (0,))}))
              for pos in positions]
    times = [pos[0].time for pos in positions]
    out = distinguish_lanes(groups, assign_lanes(groups, times, lanes, grid=grid), lanes)
    result = []
    for pos, lane_set in zip(positions, out):
        sht = max((_sht(n) for n in pos), key=_SHT_STRENGTH.__getitem__)
        result.append(FretNote(time=pos[0].time, frets=_frets_of(lane_set),
                               length=min(n.length for n in pos),
                               hopo=sht == "hopo", tap=sht == "tap", tick=pos[0].tick))
    return result


_LEVEL_CHAIN = (("hard", None), ("medium", 4), ("easy", 3))


def auto_levels(chart: FiveFretChart, grid: BeatGrid, only_missing: bool = True) -> None:
    """Onyx `gryboComplete`: Hard from Expert, Medium (limited to 4 lanes) from Hard, Easy
    (limited to 3 lanes) from Medium. With `only_missing`, a level the chart already has is
    kept and the next level is reduced from it; each generated level is recorded in
    `levels_generated`."""
    above = chart.notes
    for level, max_lanes in _LEVEL_CHAIN:
        if only_missing and chart.levels.get(level):
            above = chart.levels[level]
            continue
        notes = reduce_level(above, level, grid, chart.star_power)
        if max_lanes is not None:
            notes = limit_lanes(notes, max_lanes, grid)
        chart.levels[level] = notes
        if level not in chart.levels_generated:
            chart.levels_generated.append(level)
        above = notes
