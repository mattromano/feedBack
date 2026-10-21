"""Rock Band / Clone Hero / YARG 5-fret charts → feedpak `five_fret_<id>.json`.

Reads the guitar, rhythm and bass parts of a ``notes.mid`` (PART GUITAR, PART
RHYTHM / PART GUITAR COOP, PART BASS) or ``notes.chart`` ([ExpertSingle],
[ExpertDoubleRhythm], [ExpertDoubleBass] and the easier difficulties) plus
``song.ini``, and turns each part into fret-mask notes with absolute (audio)
times, sustains, HOPO / tap flags, star-power phrases and solos.
`five_fret_json()` encodes a part as a `five_fret_<id>.json` payload (checked by
lib/fivefret.py). The MIDI / .chart / song.ini helpers are shared with the
drums importer in lib/drumchart.py.

The chart-parsing rules are ported from YARG.Core (MoonscraperChartParser:
MidReader / MidReader.ProcessLists / MidIOHelper / PhaseShiftSysEx /
ChartReader / ChartReader.ProcessLists / NoteFlagPriority / MoonNote;
Song/Entries/Ini/SongEntry.IniBase.cs):

    Copyright (c) 2016-2020 Alexander Ong (Moonscraper)
    Copyright (c) YARC (YARG) contributors
    Licensed under the GNU Lesser General Public License v3.0
    (https://github.com/YARC-Official/YARG.Core/blob/master/LICENSE).

Ported to Python and modified (5-fret guitar rules only, no GHL / pro guitar).
Distributed here as part of feedBack under AGPL-3.0, which LGPL-3.0 permits:
LGPL-3.0 is GPL-3.0 plus additional permissions, and GPL-3.0 section 13 allows
combining with AGPL-3.0 code.

Rules implemented (YARG behaviour in brackets):
- MIDI, per difficulty base b = 60/72/84/96: frets b..b+4 (green..orange),
  force HOPO b+5, force strum b+6; tap 104, solo 103, overdrive 116 for all
  difficulties. With the ``[ENHANCED_OPENS]`` text event, b-1 is an open note.
  Phase Shift sysex phrases ``F0 50 53 00 00 <diff> <code> <01|00> F7`` (diff
  0..3 = Easy..Expert, 0xFF = all; code 0x01 = open, 0x04 = tap) turn the
  notes they cover into opens / taps.
- Marker ranges (forcing, tap, sysex) cover [start, end-1] ticks. Tap beats
  everything; forced HOPO beats forced strum [ProcessEventAsGuitarForcedTypePostDelay].
- .chart: ``N 0..4`` frets, ``N 7`` open, ``N 5`` flips that tick's natural
  HOPO state, ``N 6`` tap; ``S 2`` star power; ``E solo`` / ``E soloend``.
- Natural HOPO [MoonNote.IsNaturalHopo]: a single note, at most the threshold
  after the previous note, whose fret differs from the previous single note.
  After a chord: .mid needs the fret to be outside that chord; .chart always counts.
- Threshold [SongEntry.IniBase]: ``hopo_frequency`` ticks if set; else
  ``eighthnote_hopo`` (res/2 when true, res/3 when false); else legacy
  ``hopofreq`` 0..5 (4*res / 24,16,12,8,6,4; any other value falls back to
  res/3, where YARG throws); else res/3. When it was not
  ``hopo_frequency``, .chart adds res//192. .mid always adds 1 [MidReader].
- Sustains shorter than ``sustain_cutoff_threshold`` become 0. Default: res/3
  for .mid (0 in song.ini means 1 tick); no cutoff for .chart.
- Notes on one tick merge into one note with OR-ed frets and the longest
  sustain. An open plus frets keeps the frets, but still counts as a chord
  (open bit included) when working out HOPOs, as in YARG.
- ``song.ini`` ``delay`` (ms; .chart ``Offset`` as fallback) is added to every time.
  A negative one can push notes before 0: those are dropped (with a warning), and
  star-power / solo ranges are clamped to start at 0 (dropped if they end by 0), so
  every payload keeps the schema's ``t >= 0``.

Where these differ from the original plan (task 3 brief), YARG wins:
- .mid ``hopo_frequency`` gets +1 like the default; .chart's default is
  res/3 + res//192 (65 ticks at 192, 162 at 480), not res/3 + 1.
- .chart has no default sustain cutoff; a .mid cutoff of 0 means 1 tick.
- A single note after a chord: rule above, not "fret differs".
- ``hopofreq`` (legacy) is read too.

Simplified or left out: ``[ENHANCED_OPENS]`` applies to the whole track (YARG
switches from the event onward; charts put it at tick 0); chord notes share one
sustain (the longest); no legacy solo→star-power fixup, ``multiplier_note``,
note snapping, or coda / Big Rock Ending flags.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import drumchart
import fivefret
from drumchart import (DIFF_START, LOWER_LEVELS, TempoMap, chart_sections, chart_song_section,
                       chart_tempo, find_chart_file, ini_bool, ini_delay_seconds, load_song_ini,
                       normalize_text, read_mid, track_events)

log = logging.getLogger("feedBack.lib.fivefretchart")

PARTS = fivefret.PARTS
PART_NAMES = {"guitar": "Guitar", "rhythm": "Rhythm", "bass": "Bass"}
MID_TRACKS = {"guitar": ("PART GUITAR",), "rhythm": ("PART RHYTHM", "PART GUITAR COOP"), "bass": ("PART BASS",)}
CHART_SECTIONS = {"guitar": "Single", "rhythm": "DoubleRhythm", "bass": "DoubleBass"}

# MIDI (MidIOHelper.cs)
MIDI_FORCE_HOPO, MIDI_FORCE_STRUM = 5, 6        # offsets from the difficulty base
MIDI_TAP, MIDI_SOLO, MIDI_STARPOWER = 104, 103, 116
ENHANCED_OPENS_TEXT = "ENHANCED_OPENS"

# Phase Shift sysex (PhaseShiftSysEx.cs); mido strips F0/F7 and clips 0xFF to 0x7F
PS_HEADER = (0x50, 0x53, 0x00)
PS_TYPE_PHRASE = 0x00
PS_OPEN, PS_TAP = 0x01, 0x04
PS_DIFF = {"easy": 0, "medium": 1, "hard": 2, "expert": 3}
PS_ALL = (0xFF, 0x7F)

# .chart (ChartReader.ProcessLists.cs)
CHART_FORCED, CHART_TAP, CHART_OPEN = 5, 6, 7
CHART_STARPOWER = 2
CHART_DEFAULT_RESOLUTION = 192

OPEN = 5                       # raw fret index for an open note; bit 5 only while parsing
FRET_BITS = 0b11111

# ensure_levels reduction: minimum gap (seconds) between kept notes
LEVEL_GAPS = {"hard": 0.10, "medium": 0.20, "easy": 0.35}


@dataclass
class FretNote:
    time: float           # seconds, audio time (song.ini delay applied)
    frets: int            # bit0 green .. bit4 orange; 0 = open
    length: float = 0.0   # sustain, seconds
    hopo: bool = False
    tap: bool = False
    tick: int = 0


@dataclass
class FiveFretChart:
    part: str
    notes: list[FretNote]
    tempo: TempoMap
    offset: float = 0.0
    star_power: list = field(default_factory=list)        # (start, end) seconds
    solos: list = field(default_factory=list)             # (start, end) seconds
    levels: dict = field(default_factory=dict)            # "easy"/"medium"/"hard" -> [FretNote]
    levels_generated: list = field(default_factory=list)  # which of those were reduced by software
    source_format: str = ""


# ── song.ini settings (SongEntry.IniBase) ───────────────────────────────────

def _ini_int(ini: dict, key: str) -> int | None:
    try:
        return int(float(ini[key]))
    except (KeyError, TypeError, ValueError):
        return None


def hopo_threshold(ini: dict, resolution: int, fmt: str) -> int:
    """Natural-HOPO window in ticks (a note at most this far after the previous one)."""
    freq = _ini_int(ini, "hopo_frequency")
    if freq is not None and freq > 0:
        ticks = freq
    else:
        legacy = _ini_int(ini, "hopofreq")
        if "eighthnote_hopo" in ini:
            ticks = resolution // (2 if ini_bool(ini["eighthnote_hopo"]) else 3)
        elif legacy in (0, 1, 2, 3, 4, 5):
            ticks = 4 * resolution // (24, 16, 12, 8, 6, 4)[legacy]
        else:
            ticks = resolution // 3
        if fmt == "chart":
            ticks += resolution // CHART_DEFAULT_RESOLUTION
    return ticks + 1 if fmt == "mid" else ticks


def sustain_cutoff(ini: dict, resolution: int, fmt: str) -> int:
    """Sustains shorter than this many ticks are dropped."""
    cut = _ini_int(ini, "sustain_cutoff_threshold")
    if cut is None:
        return resolution // 3 if fmt == "mid" else 0
    return max(cut, 1) if fmt == "mid" else cut


# ── shared post-processing ──────────────────────────────────────────────────

def _in_ranges(tick: int, ranges) -> bool:
    return any(a <= tick <= b for a, b in ranges)


def _marker(start: int, end: int) -> tuple[int, int]:
    """A marker's tick range, excluding its last tick (MidReader)."""
    return start, end - 1 if end > start else end


def _build(part, raw, resolve, tempo: TempoMap, offset, threshold, cutoff, midi_subset, sp, solos, fmt):
    """raw [(tick, fret 0..5, length ticks)] -> FiveFretChart. ``resolve(tick, natural)``
    gives (hopo, tap) for the note at ``tick`` from its natural-HOPO state."""
    chords: dict[int, list[int]] = {}
    for tick, fret, length in raw:
        c = chords.setdefault(tick, [0, 0])
        c[0] |= 1 << fret
        c[1] = max(c[1], length if length >= cutoff else 0)
    notes, prev = [], None
    for tick in sorted(chords):
        mask, length = chords[tick]
        single = bin(mask).count("1") == 1
        natural = False
        if single and prev is not None and tick - prev[0] <= threshold:
            pmask = prev[1]
            if bin(pmask).count("1") == 1:
                natural = mask != pmask
            else:
                natural = not midi_subset or not mask & pmask
        hopo, tap = resolve(tick, natural)
        t0 = tempo.tick_to_time(tick)
        notes.append(FretNote(time=round(t0 + offset, 4), frets=mask & FRET_BITS,
                              length=round(tempo.tick_to_time(tick + length) - t0, 4) if length else 0.0,
                              hopo=hopo, tap=tap, tick=tick))
        prev = (tick, mask)

    early = sum(n.time < 0 for n in notes)
    if early:
        log.warning("fivefretchart: %s (%s): dropped %d note(s) before 0 s (negative delay / Offset)",
                    part, fmt, early)
        notes = [n for n in notes if n.time >= 0]

    def span(rng):
        spans = ((round(tempo.tick_to_time(a) + offset, 4), round(tempo.tick_to_time(b) + offset, 4))
                 for a, b in rng)
        return sorted({(max(a, 0.0), b) for a, b in spans if b > 0})
    return FiveFretChart(part=part, notes=notes, tempo=tempo, offset=offset, star_power=span(sp),
                         solos=span(solos), source_format=fmt)


# ── notes.mid ───────────────────────────────────────────────────────────────

def _sysex_ranges(track, difficulty: str) -> dict[int, list[tuple[int, int]]]:
    """Phase Shift sysex phrases for this difficulty as {code: [(start, last tick)]}."""
    out: dict[int, list] = {}
    pending: dict[tuple, int] = {}
    tick = 0
    for m in track:
        tick += m.time
        if m.type != "sysex":
            continue
        d = tuple(m.data)
        if len(d) != 7 or d[:3] != PS_HEADER or d[3] != PS_TYPE_PHRASE:
            continue
        diff, code, value = d[4], d[5], d[6]
        key = (diff, code)
        if value == 1:
            pending.setdefault(key, tick)
        elif value == 0 and key in pending:
            start = pending.pop(key)
            if diff in PS_ALL or diff == PS_DIFF[difficulty]:
                out.setdefault(code, []).append(_marker(start, tick))
    return out


def parse_mid(path: Path, ini: dict | None = None, part: str = "guitar", difficulty: str = "expert") -> FiveFretChart:
    ini = ini or {}
    tempo, _sections, tracks = read_mid(path)
    track = next((tracks[n] for n in MID_TRACKS[part] if n in tracks), None)
    if track is None:
        raise ValueError(f"{Path(path).name}: no {' / '.join(MID_TRACKS[part])} track")
    base = DIFF_START[difficulty]
    texts, events = track_events(track)
    enhanced_opens = any(normalize_text(t) == ENHANCED_OPENS_TEXT for _, t in texts)
    raw, force_hopo, force_strum, taps, sp, solos = [], [], [], [], [], []
    for st, en, note, _vel in events:
        off = note - base
        if 0 <= off <= 4:
            raw.append((st, off, en - st))
        elif off == -1 and enhanced_opens:
            raw.append((st, OPEN, en - st))
        elif off == MIDI_FORCE_HOPO:
            force_hopo.append(_marker(st, en))
        elif off == MIDI_FORCE_STRUM:
            force_strum.append(_marker(st, en))
        elif note == MIDI_TAP:
            taps.append(_marker(st, en))
        elif note == MIDI_STARPOWER:
            sp.append((st, en))
        elif note == MIDI_SOLO:
            solos.append((st, en))
    sysex = _sysex_ranges(track, difficulty)
    opens = sysex.get(PS_OPEN, [])
    raw = [(t, OPEN if _in_ranges(t, opens) else f, ln) for t, f, ln in raw]
    taps += sysex.get(PS_TAP, [])

    def resolve(tick, natural):
        if _in_ranges(tick, taps):
            return False, True
        if _in_ranges(tick, force_hopo):
            return True, False
        if _in_ranges(tick, force_strum):
            return False, False
        return natural, False

    res = tempo.resolution
    return _build(part, raw, resolve, tempo, ini_delay_seconds(ini), hopo_threshold(ini, res, "mid"),
                  sustain_cutoff(ini, res, "mid"), True, sp, solos, "mid")


# ── notes.chart ─────────────────────────────────────────────────────────────

def parse_chart(path: Path, ini: dict | None = None, part: str = "guitar",
                difficulty: str = "expert") -> FiveFretChart:
    ini = ini or {}
    secs = chart_sections(Path(path).read_text(encoding="utf-8-sig", errors="replace"))
    song = chart_song_section(secs)
    res = int(float(song.get("Resolution", CHART_DEFAULT_RESOLUTION)))
    name = difficulty.capitalize() + CHART_SECTIONS[part]
    track = secs.get(name)
    if track is None:
        raise ValueError(f"{Path(path).name}: no [{name}] section")
    raw, forced, taps, sp, solos, solo_starts = [], set(), set(), [], [], []
    for line in track:
        k, _, v = line.partition("=")
        parts = v.split()
        if len(parts) < 2:
            continue
        t = int(k.strip())
        if parts[0] == "N":
            n, length = int(parts[1]), int(parts[2]) if len(parts) > 2 else 0
            if 0 <= n <= 4:
                raw.append((t, n, length))
            elif n == CHART_OPEN:
                raw.append((t, OPEN, length))
            elif n == CHART_FORCED:
                forced.add(t)
            elif n == CHART_TAP:
                taps.add(t)
        elif parts[0] == "S":
            if int(parts[1]) == CHART_STARPOWER:
                sp.append((t, t + (int(parts[2]) if len(parts) > 2 else 0)))
        elif parts[0] == "E":
            text = normalize_text(" ".join(parts[1:]).strip('"'))
            if text == "solo":
                solo_starts.append(t)
            elif text == "soloend" and solo_starts:
                solos.append((solo_starts.pop(), t))

    def resolve(tick, natural):
        if tick in taps:
            return False, True
        return natural != (tick in forced), False

    try:
        chart_off = float(song.get("Offset", 0) or 0)
    except ValueError:
        chart_off = 0.0
    return _build(part, raw, resolve, chart_tempo(secs, res), ini_delay_seconds(ini, chart_off),
                  hopo_threshold(ini, res, "chart"), sustain_cutoff(ini, res, "chart"), False, sp, solos,
                  "chart")


# ── song folders ────────────────────────────────────────────────────────────

def load_song_folder(folder: Path) -> tuple[dict[str, FiveFretChart], dict]:
    """Parse a CH/YARG song folder: {part: chart} for every part with Expert notes (the
    chart's own Easy/Medium/Hard read into ``chart.levels`` where they have notes), plus
    the song.ini."""
    folder = Path(folder)
    ini = load_song_ini(folder / "song.ini")
    chart_path = find_chart_file(folder)
    if chart_path is None:
        raise FileNotFoundError(f"{folder.name}: no notes.mid or notes.chart")
    is_chart = chart_path.suffix.lower() == ".chart"
    parse = parse_chart if is_chart else parse_mid
    if is_chart and (not ini.get("name") or not ini.get("artist")):
        song = chart_song_section(chart_sections(chart_path.read_text(encoding="utf-8-sig", errors="replace")))
        for key, ini_key in (("Name", "name"), ("Artist", "artist"), ("Album", "album")):
            if song.get(key):
                ini.setdefault(ini_key, song[key])
        if song.get("Year"):
            ini.setdefault("year", song["Year"].strip(", "))
    out = {}
    for part in PARTS:
        try:
            chart = parse(chart_path, ini, part)
        except ValueError:
            continue
        if not chart.notes:
            continue
        for lv in LOWER_LEVELS:
            try:
                lower = parse(chart_path, ini, part, lv)
            except ValueError:
                continue
            if lower.notes:
                chart.levels[lv] = lower.notes
        out[part] = chart
    return out, ini


# ── generated levels ────────────────────────────────────────────────────────

def _reduce_frets(frets: int, level: str) -> int:
    if level == "medium" and frets & 0b10000:          # orange folds to blue
        frets = (frets & ~0b10000) | 0b01000
    bits = [i for i in range(5) if frets >> i & 1]
    if not bits:
        return frets                                    # open
    if level == "easy":
        return 1 << min(bits[0], 2)                     # lowest fret; blue / orange fold to yellow
    if len(bits) >= (3 if level == "hard" else 2):
        return (1 << bits[0]) | (1 << bits[-1])
    return frets


def _reduce(notes: list[FretNote], level: str) -> list[FretNote]:
    gap, out, last = LEVEL_GAPS[level], [], None
    for n in notes:
        if last is not None and n.time - last < gap - 1e-6:
            continue
        out.append(FretNote(time=n.time, frets=_reduce_frets(n.frets, level), length=n.length,
                            hopo=n.hopo and level == "hard", tap=n.tap, tick=n.tick))
        last = n.time
    return out


def ensure_levels(chart: FiveFretChart) -> None:
    """Fill missing Easy/Medium/Hard by reducing Expert; mark them in ``levels_generated``."""
    for lv in LOWER_LEVELS:
        if chart.levels.get(lv):
            continue
        chart.levels[lv] = _reduce(chart.notes, lv)
        if lv not in chart.levels_generated:
            chart.levels_generated.append(lv)


# ── feedpak encoding (five_fret_<id>.json) ──────────────────────────────────

def note_json(n: FretNote) -> dict:
    out: dict = {"t": round(n.time, 3), "f": n.frets}
    length = round(n.length, 3)
    if length > 0:
        out["l"] = length
    if n.hopo:
        out["h"] = True
    if n.tap:
        out["tap"] = True
    return out


def _notes_json(notes) -> list[dict]:
    return sorted((note_json(n) for n in notes), key=lambda d: (d["t"], d["f"]))


def five_fret_json(chart: FiveFretChart, source: dict, name: str | None = None) -> dict:
    """The part as a feedpak `five_fret_<id>.json` payload."""
    data: dict = {"version": fivefret.SCHEMA_VERSION, "name": name or PART_NAMES[chart.part], "instrument": chart.part,
                  "source": dict(source), "notes": _notes_json(chart.notes)}
    levels = {}
    for lv in LOWER_LEVELS:
        if chart.levels.get(lv):
            levels[lv] = {"notes": _notes_json(chart.levels[lv])}
            if lv in chart.levels_generated:
                levels[lv]["generated"] = True
    if levels:
        data["levels"] = levels
    for key, spans in (("star_power", chart.star_power), ("solos", chart.solos)):
        if spans:
            data[key] = drumchart._ranges(spans)
    return data
