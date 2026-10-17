"""Rock Band / Clone Hero / YARG vocal charts → feedpak `lyrics.json` + `vocal_pitch.json`.

Reads the ``PART VOCALS`` track of a ``notes.mid`` (``HARM1`` when there is no lead part) and
turns it into hand-authored karaoke data: one syllable per vocal note with absolute (audio)
times, the note's MIDI pitch, and line breaks from the chart's phrase markers. The MIDI reading
(tempo map, note on/off pairing, song.ini ``delay``) is shared with the drums importer in
lib/drumchart.py.

Rules (Rock Band authoring conventions, as YARG reads them):
- Notes 36..84 are sung notes; each takes the lyric event (``lyrics`` or ``text`` meta, ``[..]``
  animation events skipped) at its start tick. Other notes (0/1 range and lyric shifts, 96/97
  percussion) are ignored.
- Lyric suffixes: ``-`` joins to the next syllable, ``=`` joins with a visible hyphen, ``#`` /
  ``^`` / ``*`` mark an unpitched (spoken, "talkie") syllable, ``%`` / ``/`` / ``$`` are display
  hints and are dropped, ``§`` joins two syllables on one note (shown as ``‿``), ``_`` is a space.
- A ``+`` lyric is a pitch slide: the previous syllable carries on to this note's pitch.
- Phrases are note 105 (106, the RB2 player-2 marker, when there are none); overdrive is 116.

Encoding (feedpak spec §7.1 / §7.2): the last syllable of each phrase gets the ``+`` line-end
suffix. A slide becomes its own wordless syllable (``w`` ``""``) joined on to the syllable it
continues, so the slide's pitch is scored over its own span. Talkies stay in ``lyrics.json`` and
are left out of ``vocal_pitch.json`` (unpitched). Both files use identical rounded times, so a
reader can pair them by ``t``.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from pathlib import Path

import drumchart

VOCAL_TRACK_NAMES = ("PART VOCALS", "HARM1")
PITCH_MIN, PITCH_MAX = 36, 84
MIDI_PHRASE, MIDI_PHRASE_P2, MIDI_OVERDRIVE = 105, 106, 116

_TALKIE = "#^*"
_DROPPED = "%/$"


@dataclass
class VocalNote:
    time: float           # seconds, audio time (song.ini delay applied)
    dur: float
    midi: int | None      # None = unpitched (talkie)
    text: str             # display text, with a trailing "-" when it joins to the next syllable
    talkie: bool = False
    slide: bool = False
    tick: int = 0


@dataclass
class VocalPhrase:
    start: float
    end: float
    overdrive: bool = False


@dataclass
class VocalChart:
    notes: list[VocalNote]
    phrases: list[VocalPhrase] = field(default_factory=list)
    offset: float = 0.0
    track: str = ""


def clean_syllable(raw: str) -> tuple[str, bool, bool]:
    """A Rock Band lyric -> (display text with any join suffix, talkie, slide)."""
    s = raw.strip()
    talkie = False
    while s and (s[-1] in _TALKIE or s[-1] in _DROPPED):
        talkie |= s[-1] in _TALKIE
        s = s[:-1]
    s = s.lstrip("$")
    if s.startswith("+"):
        return "", False, True
    if s.endswith("="):
        s = s[:-1] + "--"           # joined, and the hyphen stays visible
    return s.replace("§", "‿").replace("_", " "), talkie, False


def _lyric_events(texts) -> list[tuple[int, str]]:
    return sorted((t, x) for t, x in texts if x.strip() and not x.strip().startswith("["))


def _spans(events, note) -> list[tuple[int, int]]:
    return [(st, en) for st, en, n, _ in events if n == note]


def parse_mid(path: Path, ini: dict | None = None) -> VocalChart:
    ini = ini or {}
    tempo, _sections, tracks = drumchart.read_mid(path)
    name = next((n for n in VOCAL_TRACK_NAMES if n in tracks), None)
    if name is None:
        raise ValueError(f"{Path(path).name}: no PART VOCALS track")
    texts, events = drumchart.track_events(tracks[name])
    offset = drumchart.ini_delay_seconds(ini)

    def sec(tick):
        return tempo.tick_to_time(tick) + offset

    lyrics = _lyric_events(texts)
    lyric_ticks = [t for t, _ in lyrics]
    used: set[int] = set()
    tol = max(1, tempo.resolution // 64)

    def lyric_at(tick):
        i = bisect.bisect_left(lyric_ticks, tick - tol)
        while i < len(lyrics) and lyric_ticks[i] <= tick + tol:
            if i not in used:
                used.add(i)
                return lyrics[i][1]
            i += 1
        return None

    notes: list[VocalNote] = []
    for st, en, n, _vel in events:
        if not PITCH_MIN <= n <= PITCH_MAX:
            continue
        raw = lyric_at(st)
        text, talkie, slide = clean_syllable(raw) if raw is not None else ("", False, True)
        if slide and notes:
            prev = notes[-1]
            joins = prev.text.endswith("-")
            if not joins:
                prev.text += "-"
            text = "-" if joins else ""
        notes.append(VocalNote(sec(st), sec(en) - sec(st), None if talkie else n, text,
                               talkie=talkie, slide=slide, tick=st))

    phrase_spans = _spans(events, MIDI_PHRASE) or _spans(events, MIDI_PHRASE_P2)
    od = _spans(events, MIDI_OVERDRIVE)
    phrases = []
    for st, en in sorted(set(phrase_spans)):
        phrases.append(VocalPhrase(sec(st), sec(en), any(o0 < en and o1 > st for o0, o1 in od)))
        inside = [v for v in notes if st <= v.tick < en]
        if inside:
            last = inside[-1]
            last.text = (last.text[:-1] if last.text.endswith("-") else last.text) + "+"
    return VocalChart(notes, phrases, offset, name)


def load_song_folder(folder: Path) -> VocalChart | None:
    """The vocals of a CH/YARG song folder, or None when it has no ``notes.mid`` vocal part."""
    folder = Path(folder)
    chart_path = drumchart.find_chart_file(folder)
    if chart_path is None or chart_path.suffix.lower() == ".chart":
        return None
    try:
        chart = parse_mid(chart_path, drumchart.load_song_ini(folder / "song.ini"))
    except ValueError:
        return None
    return chart if chart.notes else None


# ── feedpak encoding ────────────────────────────────────────────────────────

def _t(x: float) -> float:
    return round(x, 3)


def lyrics_json(chart: VocalChart) -> list[dict]:
    """The `lyrics.json` payload (spec §7.1)."""
    return [{"t": _t(n.time), "d": _t(n.dur), "w": n.text} for n in chart.notes]


def vocal_pitch_json(chart: VocalChart) -> dict:
    """The `vocal_pitch.json` payload (spec §7.2): pitched syllables only, same `t` as lyrics."""
    return {"version": 1,
            "notes": [{"t": _t(n.time), "d": _t(n.dur), "midi": n.midi} for n in chart.notes
                      if n.midi is not None]}
