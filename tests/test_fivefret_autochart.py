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


def test_perfect_fourth_double_stop_is_power():
    arr = _arr([Note(time=1.0, string=0, fret=0), Note(time=1.0, string=0, fret=5)])
    ev = ac.arrangement_events(arr)[0]
    assert ev.pitches == (40, 45) and ev.power is True


# --- lane assignment (midi-ch) and distinguish_lanes ---------------------------------

def _line(pitches, start=0.0, step=0.5):
    return [(p,) for p in pitches], [start + i * step for i in range(len(pitches))]


def _flat(lanes):
    assert all(len(t) == 1 for t in lanes)
    return [t[0] for t in lanes]


def test_rising_line_moves_right():
    groups, times = _line([40, 42, 43, 45, 47])
    assert _flat(ac.assign_lanes(groups, times)) == [0, 1, 2, 3, 4]


def test_falling_line_moves_left():
    groups, times = _line([47, 45, 43, 42, 40])
    assert _flat(ac.assign_lanes(groups, times)) == [4, 3, 2, 1, 0]


def test_repeated_pitch_same_lane():
    groups, times = _line([40, 42, 42, 45, 42])
    lanes = _flat(ac.assign_lanes(groups, times))
    assert lanes[1] == lanes[2] == lanes[4]
    assert lanes[0] < lanes[1] < lanes[3]


def test_lane_is_rank_within_group():
    groups, times = _line([40, 41, 47])
    assert _flat(ac.assign_lanes(groups, times)) == [0, 1, 2]


def test_one_pitch_riff_stays_put():
    a, ta = _line([40, 42, 43, 45, 47])
    riff, tr = _line([50, 50, 50, 50], start=10.0, step=0.25)
    c, tc = _line([40, 42, 43, 45, 47], start=20.0)
    lanes = _flat(ac.assign_lanes(a + riff + c, ta + tr + tc))
    assert len(set(lanes[5:9])) == 1
    groups, times = _line([45] * 6)
    assert _flat(ac.assign_lanes(groups, times)) == [0] * 6
    assert ac.assign_lanes([(45,)], [1.0]) == [(0,)]
    assert ac.assign_lanes([], []) == []


def test_window_reanchors_after_gap():
    a, ta = _line([40, 42, 43, 45, 47])
    b, tb = _line([52, 54, 55, 57, 59], start=ta[-1] + 4.0)
    assert _flat(ac.assign_lanes(a + b, ta + tb)) == [0, 1, 2, 3, 4] * 2


def test_chords_two_or_three_lanes_follow_pitch():
    out = ac.assign_lanes([(40, 47), (43, 50)], [0.0, 0.5])
    assert out == [(0, 2), (1, 3)]
    out = ac.assign_lanes([(40, 44, 47), (45, 49, 52)], [0.0, 0.5])
    assert [len(t) for t in out] == [3, 3]
    assert out[1][0] > out[0][0]


def test_padding_borrows_from_time_neighbours():
    # Groups (time order): {40,41,42,43,44}, {50}, {60,61,62,63,64}. The middle one-note
    # group borrows the previous group's last notes and the next group's first notes.
    a, ta = _line([40, 41, 42, 43, 44])
    b, tb = _line([50], start=10.0)
    c, tc = _line([60, 61, 62, 63, 64], start=20.0)
    lanes = _flat(ac.assign_lanes(a + b + c, ta + tb + tc))
    # padded set: 50 + 44, 60, 43, 61 -> sorted 43, 44, 50, 60, 61 -> 50 is lane 2
    assert lanes[5] == 2


def test_padding_borrows_from_second_to_last_neighbour():
    # Groups (time order): {50,51,52}, {40,41,42}, {60,...,64}; none can merge (> 5 ranks).
    # The first group needs two more ranks. midi-ch's l.140 off-by-one moved on from the
    # second-to-last group after one note (taking 40, then 60); fixed, it takes 40 and 41.
    a, ta = _line([50, 51, 52])
    b, tb = _line([40, 41, 42], start=10.0)
    c, tc = _line([60, 61, 62, 63, 64], start=22.0)
    lanes = _flat(ac.assign_lanes(a + b + c, ta + tb + tc))
    # padded set for the first group: 40, 41, 50, 51, 52
    assert lanes[:3] == [2, 3, 4]


def test_gaps_quantised_to_grid_ties_by_index():
    # Two lanes: whichever gap is visited first decides the grouping. In seconds the second
    # gap (0.4999) is smaller; on the grid both are 1 beat at 1/32 resolution, so the tie
    # goes to the first gap by index.
    groups = [(40,), (41,), (42,)]
    times = [0.0, 0.5001, 1.0]
    assert _flat(ac.assign_lanes(groups, times, lanes=2)) == [0, 0, 1]
    grid = ac.BeatGrid.from_beats([])
    assert _flat(ac.assign_lanes(groups, times, lanes=2, grid=grid)) == [0, 1, 1]


def test_power_chord_adjacent():
    groups = [(40,), (40, 47), (45, 52)]
    times = [0.0, 0.5, 1.0]
    plain = ac.assign_lanes(groups, times)
    assert any(t[1] - t[0] > 1 for t in plain[1:])
    out = ac.assign_lanes(groups, times, power=[False, True, True])
    assert all(t[1] - t[0] == 1 for t in out[1:])
    assert out[2][0] >= out[1][0]


def test_major_third_double_stop_not_adjacent():
    groups = [(40,), (42,), (40, 44)]
    out = ac.assign_lanes(groups, [0.0, 0.5, 1.0], power=[False, False, False])
    assert out[2] == (0, 2)


def test_distinguish_moves_collision():
    # rising pitch -> second event moves up
    assert ac.distinguish_lanes([(40,), (45,)], [(2,), (2,)]) == [(2,), (3,)]
    # falling pitch -> second event moves down
    assert ac.distinguish_lanes([(45,), (40,)], [(2,), (2,)]) == [(2,), (1,)]
    # the second can't move up from the top lane -> the first moves down instead
    assert ac.distinguish_lanes([(40,), (45,)], [(4,), (4,)]) == [(3,), (4,)]
    # the moved event avoids its next neighbour too
    assert ac.distinguish_lanes([(40,), (45,), (47,)], [(2,), (2,), (3,)]) == \
        [(2,), (4,), (3,)]


def test_distinguish_moves_whole_run():
    # AutoChart.hs l.43-45: repeats of the second pitch set move together
    out = ac.distinguish_lanes([(40,), (45,), (45,), (45,)], [(2,)] * 4)
    assert out == [(2,), (3,), (3,), (3,)]


def test_distinguish_power_moves_to_adjacent_pair():
    groups = [(40, 47), (42, 49)]
    out = ac.distinguish_lanes(groups, [(1, 2), (1, 2)], power=[True, True])
    assert out == [(1, 2), (2, 3)]
    # Without the power flag Onyx's fixOne takes {0,3}: same average, so it counts as
    # "in the aimed direction" at distance 0.
    assert ac.distinguish_lanes(groups, [(1, 2), (1, 2)]) == [(1, 2), (0, 3)]


def test_distinguish_keeps_same_pitch_same_lane():
    groups = [(40,), (40,), (42,), (42,)]
    out = [(1,), (1,), (2,), (2,)]
    assert ac.distinguish_lanes(groups, out) == out
    assert ac.distinguish_lanes([(40,)] * 3, [(0,)] * 3) == [(0,)] * 3
