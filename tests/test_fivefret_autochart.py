"""lib/fivefret_autochart.py: pitch events and the beat grid."""

from __future__ import annotations

import pytest

import fivefret_autochart as ac
from drumchart import TempoMap
from song import Arrangement, Beat, Chord, Note


def _arr(notes=(), chords=(), **kw):
    kw.setdefault("name", "Lead")
    return Arrangement(notes=list(notes), chords=list(chords), **kw)


def test_pitch_from_tuning_and_capo():
    arr = _arr([Note(time=1.0, string=0, fret=3)], tuning=[-2, 0, 0, 0, 0, 0], capo=2)
    assert [e.pitches for e in ac.arrangement_events(arr)] == [(43,)]


def test_bass_uses_bass_base():
    arr = _arr([Note(time=1.0, string=0, fret=3)], name="Bass", tuning=[0, 0, 0, 0])
    assert ac.arrangement_events(arr)[0].pitches == (31,)       # 28 + 3, not 40 + 3


def test_note_inside_chord_merges():
    arr = _arr([Note(time=1.0004, string=3, fret=0)],
               [Chord(time=1.0, chord_id=0, notes=[Note(time=1.0, string=0, fret=0),
                                                   Note(time=1.0, string=1, fret=2),
                                                   Note(time=1.0, string=2, fret=2)])])
    events = ac.arrangement_events(arr)
    assert len(events) == 1
    assert events[0].pitches == (40, 47, 52)
    assert events[0].time == 1.0


def test_simplify_chord():
    assert ac.simplify_chord((40, 47, 52)) == (40, 47)
    assert ac.simplify_chord((40, 44, 47, 52)) == (40, 44, 47)
    assert ac.simplify_chord((40,)) == (40,)
    assert ac.simplify_chord((40, 40)) == (40,)
    assert ac.simplify_chord((40, 45)) == (40, 45)


def test_power_flag():
    power = _arr([Note(time=1.0, string=0, fret=0), Note(time=1.0, string=1, fret=2),
                  Note(time=1.0, string=2, fret=2)])
    ev = ac.arrangement_events(power)[0]
    assert ev.pitches == (40, 47) and ev.power is True
    third = _arr([Note(time=1.0, string=0, fret=0), Note(time=1.0, string=0, fret=4)])
    ev = ac.arrangement_events(third)[0]
    assert ev.pitches == (40, 44) and ev.power is False
    single = ac.arrangement_events(_arr([Note(time=1.0, string=0, fret=0)]))[0]
    assert single.power is False


def test_muted_chord_is_open_event():
    arr = _arr([Note(time=1.0, string=0, fret=0, mute=True),
                Note(time=1.0, string=1, fret=2, fret_hand_mute=True)])
    ev = ac.arrangement_events(arr)
    assert len(ev) == 1 and ev[0].pitches == ()


def test_muted_note_with_either_slide_stays_pitched():
    for kw in ({"slide_to": 7}, {"slide_unpitch_to": 7}):
        arr = _arr([Note(time=1.0, string=0, fret=5, mute=True, **kw)])
        ev = ac.arrangement_events(arr)[0]
        assert ev.pitches == (45,) and ev.slide_or_bend is True


def test_ignore_skipped():
    arr = _arr([Note(time=1.0, string=0, fret=0, ignore=True), Note(time=2.0, string=0, fret=1)])
    assert [e.time for e in ac.arrangement_events(arr)] == [2.0]


def test_sht_priority():
    both = _arr([Note(time=1.0, string=0, fret=0, hammer_on=True),
                 Note(time=1.0, string=1, fret=2, tap=True)])
    assert ac.arrangement_events(both)[0].sht == "hopo"
    pull = _arr([Note(time=1.0, string=0, fret=0, pull_off=True)])
    assert ac.arrangement_events(pull)[0].sht == "hopo"
    tap = _arr([Note(time=1.0, string=0, fret=0, tap=True)])
    assert ac.arrangement_events(tap)[0].sht == "tap"
    assert ac.arrangement_events(_arr([Note(time=1.0, string=0, fret=0)]))[0].sht == "strum"


def test_length_and_slide_or_bend():
    arr = _arr([Note(time=1.0, string=0, fret=0, sustain=0.5, bend=1.0),
                Note(time=1.0, string=1, fret=2, sustain=0.8)])
    ev = ac.arrangement_events(arr)[0]
    assert ev.length == pytest.approx(0.8) and ev.slide_or_bend is True
    plain = ac.arrangement_events(_arr([Note(time=1.0, string=0, fret=0, sustain=0.2)]))[0]
    assert plain.slide_or_bend is False


def _beats(n, spacing=0.5, per_measure=4):
    return [Beat(time=i * spacing, measure=(i // per_measure) if i % per_measure == 0 else -1)
            for i in range(n)]


def test_grid_from_beats_and_extrapolates():
    g = ac.BeatGrid.from_beats(_beats(9))              # beats 0..4 s, measures every 4
    assert g.downbeats[0] and g.downbeats[4] and not g.downbeats[1]
    assert g.beat_at(1.25) == pytest.approx(2.5)
    assert g.time_at(2.5) == pytest.approx(1.25)
    assert g.beat_at(5.0) == pytest.approx(10.0)       # past the last beat
    assert g.beat_at(-0.5) == pytest.approx(-1.0)      # before the first
    assert g.time_at(12.0) == pytest.approx(6.0)
    assert g.beat_len(1.3) == pytest.approx(0.5)
    assert g.beats_since_measure(1.25) == pytest.approx(2.5)
    assert g.beats_since_measure(2.25) == pytest.approx(0.5)


def test_grid_without_beats_is_120bpm():
    g = ac.BeatGrid.from_beats([])
    assert g.beat_at(1.0) == 2.0
    assert g.time_at(2.0) == 1.0
    assert g.beat_len(3.0) == 0.5
    assert g.beats_since_measure(2.25) == pytest.approx(0.5)
    one = ac.BeatGrid.from_beats([Beat(time=3.0, measure=0)])
    assert one.beat_at(1.0) == 2.0


def test_grid_from_tempo():
    g = ac.BeatGrid.from_tempo(TempoMap(480), end_time=6.0, offset=0.25)
    assert g.time_at(4) == pytest.approx(2.25)
    assert g.beats_since_measure(2.25) == pytest.approx(0)
    assert g.downbeats[:5] == [True, False, False, False, True]
