"""Rocksmith arrangement -> 5-fret auto-charter (feedBack `source.kind: "auto"` charts).

Ported from Onyx (Michael Tolly, mtolly/onyx, commit be402a777cb668fb9133471030c055c5e6956e3e,
GPL-3.0), whose autochart is a port of Edward Haas's midi-ch (EFHIII/midi-ch, GPL-3.0).
Licensed GPL-3.0-or-later as a separate file; core feedBack is AGPL-3.0, which GPL-3.0
section 13 allows combining with. Nothing else in core copies Onyx code.

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
