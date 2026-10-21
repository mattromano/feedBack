"""lib/fivefret_autochart.py: pitch events and the beat grid."""

from __future__ import annotations

import pytest

import fivefret
import fivefret_autochart as ac
from fivefretchart import FretNote, five_fret_json
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


# --- Task 3: Expert auto-chart ------------------------------------------------------------

def _grid(n=40):
    return ac.BeatGrid.from_beats(_beats(n))            # 120 bpm


def _chart(notes, **kw):
    return ac.autochart(_arr(notes, **kw), _grid(), "guitar")


def test_hammer_on_becomes_hopo():
    c = _chart([Note(time=1.0, string=0, fret=0), Note(time=2.0, string=0, fret=2, hammer_on=True)])
    assert [n.hopo for n in c.notes] == [False, True]
    assert c.source_format == "auto" and c.levels == {} and c.part == "guitar"


def test_tap_carries():
    c = _chart([Note(time=1.0, string=0, fret=0), Note(time=2.0, string=0, fret=2, tap=True)])
    assert [n.tap for n in c.notes] == [False, True] and not c.notes[1].hopo


def test_tap_then_hopo_becomes_tap():
    c = _chart([Note(time=1.0, string=0, fret=0, tap=True),
                Note(time=1.2, string=0, fret=2, hammer_on=True),
                Note(time=1.4, string=0, fret=3, pull_off=True)])
    assert [n.tap for n in c.notes] == [True, True, True]
    assert not any(n.hopo for n in c.notes)


def test_hopo_after_long_gap_before_tap_becomes_tap():
    c = _chart([Note(time=1.0, string=0, fret=0),
                Note(time=2.0, string=0, fret=2, hammer_on=True),
                Note(time=2.2, string=0, fret=3, tap=True)])
    assert [n.tap for n in c.notes] == [False, True, True]
    short = _chart([Note(time=1.0, string=0, fret=0),
                    Note(time=1.2, string=0, fret=2, hammer_on=True),
                    Note(time=1.4, string=0, fret=3, tap=True)])
    assert [n.tap for n in short.notes] == [False, False, True]
    assert short.notes[1].hopo


def test_fast_strum_becomes_hopo():
    c = _chart([Note(time=1.0, string=0, fret=0), Note(time=1.08, string=0, fret=2)])
    assert c.notes[0].frets != c.notes[1].frets
    assert [n.hopo for n in c.notes] == [False, True]
    slow = _chart([Note(time=1.0, string=0, fret=0), Note(time=1.2, string=0, fret=2)])
    assert not any(n.hopo for n in slow.notes)


def test_fast_strum_after_tap_or_into_open_stays_strum():
    after_tap = _chart([Note(time=1.0, string=0, fret=0, tap=True), Note(time=1.08, string=0, fret=2)])
    assert not after_tap.notes[1].hopo
    into_open = _chart([Note(time=1.0, string=0, fret=0), Note(time=1.08, string=1, fret=2, mute=True)])
    assert into_open.notes[1].frets == 0 and not into_open.notes[1].hopo


def test_chord_keeps_hopo():
    c = _chart([Note(time=1.0, string=0, fret=0),
                Note(time=2.0, string=0, fret=2, hammer_on=True), Note(time=2.0, string=1, fret=2)])
    assert bin(c.notes[1].frets).count("1") == 2 and c.notes[1].hopo


def test_short_sustain_dropped_long_kept():
    c = _chart([Note(time=1.0, string=0, fret=0, sustain=0.15),
                Note(time=3.0, string=0, fret=2, sustain=0.5),
                Note(time=5.0, string=0, fret=3)])
    assert c.notes[0].length == 0.0
    assert c.notes[1].length == pytest.approx(0.5)


def test_sustain_trimmed_before_next():
    c = _chart([Note(time=1.0, string=0, fret=0, sustain=1.0),
                Note(time=1.5, string=0, fret=2)])
    assert c.notes[0].length == pytest.approx(0.5 - 0.0625)       # 1/8 beat = 0.0625 s
    assert c.notes[0].time + c.notes[0].length <= 1.5 - 0.0625 + 1e-9


def test_bend_and_slide_keep_sustain():
    c = _chart([Note(time=1.0, string=0, fret=0, sustain=0.15, bend=1.0),
                Note(time=3.0, string=0, fret=2, sustain=0.15, slide_to=5),
                Note(time=5.0, string=0, fret=3, sustain=0.15)])
    assert c.notes[0].length == pytest.approx(0.15)
    assert c.notes[1].length == pytest.approx(0.15)
    assert c.notes[2].length == 0.0
    trimmed = _chart([Note(time=1.0, string=0, fret=0, sustain=2.0, bend=1.0),
                      Note(time=1.5, string=0, fret=2)])
    assert trimmed.notes[0].length == pytest.approx(0.4375)


def test_open_from_muted():
    c = _chart([Note(time=1.0, string=0, fret=3), Note(time=2.0, string=1, fret=2, fret_hand_mute=True)])
    assert c.notes[1].frets == 0 and c.notes[0].frets != 0


def test_no_overdrive():
    # Auto parts get synthesised star-power phrases from the five-fret session
    # (Plan 3 ruling 5), not from the auto-charter.
    c = _chart([Note(time=1.0, string=0, fret=0)])
    assert c.star_power == []
    assert "star_power" not in five_fret_json(c, {"kind": "auto"}, name="Guitar (auto)")


def test_autochart_json_validates():
    c = _chart([Note(time=1.0, string=0, fret=0, sustain=1.0),
                Note(time=2.0, string=1, fret=2, hammer_on=True),
                Note(time=3.0, string=0, fret=0, mute=True),
                Note(time=4.0, string=0, fret=0), Note(time=4.0, string=1, fret=2),
                Note(time=4.0, string=2, fret=2)])
    data = five_fret_json(c, {"kind": "auto", "generator": ac.GENERATOR,
                              "generator_version": ac.GENERATOR_VERSION,
                              "from_arrangement": "lead"}, name="Guitar (auto)")
    assert fivefret.validate_five_fret(data) == (True, "")


# --- Task 4: lower levels (gryboReduce port) and lane limits ---------------------------

G, R, Y, B, O = 0b00001, 0b00010, 0b00100, 0b01000, 0b10000


def _fn(beat, frets, length=0.0, hopo=False, tap=False):
    """A FretNote at `beat` on the 120 bpm test grid (0.5 s a beat); `length` in beats."""
    return FretNote(time=beat * 0.5, frets=frets, length=length * 0.5, hopo=hopo, tap=tap)


def _ff(notes, **kw):
    return ac.FiveFretChart(part="guitar", notes=notes, tempo=TempoMap(480), **kw)


def _tf_beats(notes):
    return [(round(n.time / 0.5, 3), n.frets) for n in notes]


def _dense_expert(measures=16):
    """8th notes over every lane, with a G+Y+O chord on each measure line."""
    pattern = [G, R, Y, B, O, B, Y, R]
    notes = []
    for i in range(measures * 8):
        frets = G | Y | O if i % 8 == 0 else pattern[i % 8]
        notes.append(_fn(i * 0.5, frets))
    return notes


def test_easy_three_lanes_medium_four():
    c = _ff(_dense_expert())
    ac.auto_levels(c, _grid(80))
    assert c.levels["medium"] and c.levels["easy"]
    assert all(n.frets & ~0b01111 == 0 for n in c.levels["medium"])
    assert all(n.frets & ~0b00111 == 0 for n in c.levels["easy"])
    assert {n.frets for n in c.levels["hard"]} & {O, R | O, Y | O}   # Hard keeps orange


def test_easy_single_notes_only():
    c = _ff([_fn(b, G | Y | O if b % 4 == 0 else R | B) for b in range(0, 32, 2)])
    ac.auto_levels(c, _grid(40))
    assert c.levels["easy"]
    assert all(bin(n.frets).count("1") == 1 for n in c.levels["easy"])


def test_hard_chord_pairs_green_orange_to_blue():
    notes = [_fn(0, G | Y | O), _fn(2, R | Y | B), _fn(4, G | O), _fn(6, R | O), _fn(8, Y)]
    hard = ac.reduce_level(notes, "hard", _grid())
    assert [n.frets for n in hard] == [G | B, R | B, G | B, R | O, Y]


def test_medium_chord_rules():
    notes = [_fn(0, G | B), _fn(4, R | O), _fn(8, Y | O), _fn(12, G | R), _fn(16, O),
             _fn(20, R | Y)]
    medium = ac.reduce_level(notes, "medium", _grid())
    assert [n.frets for n in medium] == [G | Y, R | B, Y | O, G | R, O, R | Y]


def test_density_falls_by_level():
    pattern = [G, R, Y, R]
    expert = [_fn(i * 0.25, pattern[i % 4]) for i in range(16 * 16)]
    c = _ff(expert)
    ac.auto_levels(c, _grid(80))
    counts = [len(c.notes)] + [len(c.levels[lv]) for lv in ("hard", "medium", "easy")]
    assert counts[0] > counts[1] > counts[2] > counts[3] > 0


def test_steady_eighths_survive_on_hard():
    expert = [_fn(i * 0.5, [G, R, Y][i % 3]) for i in range(32)]
    hard = ac.reduce_level(expert, "hard", _grid())
    assert [n.time for n in hard] == [n.time for n in expert]


def test_measure_line_notes_preferred():
    hard = ac.reduce_level([_fn(3.75, G), _fn(4.0, R)], "hard", _grid())
    assert _tf_beats(hard) == [(4.0, R)]
    # without the measure line the earlier note wins (ties go by time)
    hard = ac.reduce_level([_fn(4.75, G), _fn(4.875, R)], "hard", _grid())
    assert _tf_beats(hard) == [(4.75, G)]


def test_overdrive_and_sustain_notes_preferred():
    od = ac.reduce_level([_fn(3.75, G), _fn(4.0, R)], "hard", _grid(),
                         star_power=[(3.75 * 0.5, 3.9 * 0.5)])
    assert _tf_beats(od) == [(3.75, G)]
    sus = ac.reduce_level([_fn(4.0, R), _fn(4.25, G, length=2)], "hard", _grid())
    assert _tf_beats(sus) == [(4.25, G)]


def test_hopos_only_on_hard():
    expert = [_fn(0, G), _fn(2, Y, length=1, hopo=True), _fn(4, R, hopo=True),
              _fn(6, G | Y, hopo=True), _fn(8, B, tap=True)]
    hard = ac.reduce_level(expert, "hard", _grid())
    assert [(n.hopo, n.tap) for n in hard] == [
        (False, False), (True, False), (False, False), (True, False), (False, True)]
    medium = ac.reduce_level(hard, "medium", _grid())
    assert len(medium) == 5 and not any(n.hopo or n.tap for n in medium)


def test_chord_then_hopo_chord_and_same_top_hopo():
    hard = ac.reduce_level([_fn(0, R | B), _fn(1, G | Y, hopo=True)], "hard", _grid())
    assert [n.frets for n in hard] == [R, G | Y] and hard[1].hopo
    same_top = ac.reduce_level([_fn(0, Y), _fn(1, R | Y, hopo=True)], "hard", _grid())
    assert [n.frets for n in same_top] == [Y, R | Y] and not same_top[1].hopo
    other_top = ac.reduce_level([_fn(0, R), _fn(1, R | Y, hopo=True)], "hard", _grid())
    assert other_top[1].hopo


def test_grace_note_merges():
    hard = ac.reduce_level([_fn(1.0, G), _fn(1.25, R, length=1, hopo=True)], "hard", _grid())
    assert len(hard) == 1
    assert hard[0].frets == R and not hard[0].hopo
    assert hard[0].time == 0.5 and hard[0].length == pytest.approx(1.25 * 0.5)
    # first note off the 8th-note grid: no merge (the sustained HOPO is kept instead)
    off_beat = ac.reduce_level([_fn(1.25, G), _fn(1.5, R, length=1, hopo=True)], "hard", _grid())
    assert _tf_beats(off_beat) == [(1.5, R)] and off_beat[0].hopo


def test_medium_sustain_gap():
    hard = [_fn(0, G, length=3), _fn(3, R, length=1.5), _fn(5, Y, length=0.75), _fn(8, B)]
    medium = ac.reduce_level(hard, "medium", _grid())
    assert [round(n.length / 0.5, 3) for n in medium] == [2.0, 1.0, 0.0, 0.0]
    assert ac.reduce_level(hard, "hard", _grid())[1].length == pytest.approx(1.5 * 0.5)


def test_jump_limited():
    up = ac.reduce_level([_fn(0, G), _fn(0.5, O)], "hard", _grid())
    assert [n.frets for n in up] == [G, B]
    down = ac.reduce_level([_fn(0, O), _fn(0.5, G)], "hard", _grid())
    assert [n.frets for n in down] == [O, R]
    far = ac.reduce_level([_fn(0, G), _fn(2, O)], "hard", _grid())
    assert [n.frets for n in far] == [G, O]
    # the gap counts from the end of the previous sustain too
    held = ac.reduce_level([_fn(0, G, length=1.5), _fn(2, O)], "hard", _grid())
    assert [n.frets for n in held] == [G, B]
    medium = ac.reduce_level([_fn(0, G), _fn(1, B)], "medium", _grid())
    assert [n.frets for n in medium] == [G, Y]


def test_limit_lanes():
    fits = [_fn(0, G), _fn(1, B), _fn(2, 0)]
    assert [n.frets for n in ac.limit_lanes(fits, 4)] == [G, B, 0]
    line = [_fn(i, f) for i, f in enumerate([G, R, Y, B, O, 0])]
    limited = ac.limit_lanes(line, 3)
    assert all(n.frets and n.frets & ~0b00111 == 0 for n in limited)    # open -> a lane
    assert [n.time for n in limited] == [n.time for n in line]
    merged = ac.limit_lanes([_fn(0, G, length=2, hopo=True), _fn(0, O, tap=True), _fn(1, R)], 3)
    assert len(merged) == 2 and merged[0].tap and not merged[0].hopo
    assert merged[0].length == 0.0 and bin(merged[0].frets).count("1") == 2


def test_generated_marked():
    hard = [_fn(0, G), _fn(2, R)]
    c = _ff(_dense_expert(4), levels={"hard": hard})
    ac.auto_levels(c, _grid())
    assert c.levels["hard"] is hard
    assert sorted(c.levels_generated) == ["easy", "medium"]
    assert len(c.levels["medium"]) <= len(hard)                       # Medium comes from the real Hard
    ac.auto_levels(c, _grid(), only_missing=False)
    assert sorted(c.levels_generated) == ["easy", "hard", "medium"]
    assert c.levels["hard"] is not hard


def test_reduce_level_unknown_level():
    with pytest.raises(ValueError):
        ac.reduce_level([], "expert", _grid())
