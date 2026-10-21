"""lib/fivefretchart.py with synthetic Rock Band / Clone Hero guitar, rhythm and bass charts."""

from __future__ import annotations

import pytest

import fivefret
import fivefretchart
from tests.drum_fixtures import TPQ, write_chart, write_ini, write_mid

G, R, Y, B, O = 96, 97, 98, 99, 100            # Expert frets
FORCE_HOPO, FORCE_STRUM, TAP = 101, 102, 104
OPEN_EXPERT = 95                                # with [ENHANCED_OPENS]


def _mid(path, notes, **kw):
    kw.setdefault("track_name", "PART GUITAR")
    return write_mid(path, [(t, n, 100, ln) for t, n, ln in notes], **kw)


def _tf(chart):
    return [(round(n.time, 3), n.frets) for n in chart.notes]


def _hopos(chart):
    return [n.hopo for n in chart.notes]


# ── MIDI ────────────────────────────────────────────────────────────────────

def test_mid_expert_frets_and_chords(tmp_path):
    c = fivefretchart.parse_mid(_mid(tmp_path / "n.mid", [(0, G, 10), (0, Y, 10), (480, O, 10)]))
    assert c.part == "guitar" and c.source_format == "mid"
    assert _tf(c) == [(0.0, 0b00101), (0.5, 0b10000)]


def test_mid_forced_hopo_and_strum(tmp_path):
    near = fivefretchart.parse_mid(_mid(tmp_path / "a.mid", [(0, G, 10), (120, R, 10)]))
    assert _hopos(near) == [False, True]          # natural: threshold 480/3 + 1 = 161 ticks
    far = fivefretchart.parse_mid(_mid(tmp_path / "b.mid", [(0, G, 10), (240, R, 10)]))
    assert _hopos(far) == [False, False]
    strum = fivefretchart.parse_mid(_mid(tmp_path / "c.mid", [(0, G, 10), (120, R, 10), (120, FORCE_STRUM, 10)]))
    assert _hopos(strum) == [False, False]
    chord = fivefretchart.parse_mid(_mid(tmp_path / "d.mid", [(0, G, 10), (120, R, 10), (120, Y, 10),
                                                              (120, FORCE_HOPO, 10)]))
    assert _hopos(chord) == [False, True]


def test_chord_never_natural_hopo(tmp_path):
    c = fivefretchart.parse_mid(_mid(tmp_path / "n.mid", [(0, G, 10), (120, R, 10), (120, Y, 10)]))
    assert _hopos(c) == [False, False]


def test_mid_same_fret_after_chord_is_not_hopo(tmp_path):
    # YARG's .mid subset exception: a single note already in the previous chord is a strum.
    c = fivefretchart.parse_mid(_mid(tmp_path / "n.mid", [(0, G, 10), (0, R, 10), (120, R, 10), (240, Y, 10)]))
    assert _hopos(c) == [False, False, True]


def test_open_plus_fret_keeps_fret(tmp_path):
    p = _mid(tmp_path / "n.mid", [(0, OPEN_EXPERT, 10), (0, R, 10), (480, OPEN_EXPERT, 10)],
             texts=[(0, "[ENHANCED_OPENS]")])
    assert _tf(fivefretchart.parse_mid(p)) == [(0.0, 0b00010), (0.5, 0)]
    plain = _mid(tmp_path / "plain.mid", [(0, OPEN_EXPERT, 10), (0, R, 10)])
    assert _tf(fivefretchart.parse_mid(plain)) == [(0.0, 0b00010)]


def test_mid_sysex_open_and_tap(tmp_path):
    import mido
    mid = mido.MidiFile(type=1, ticks_per_beat=TPQ)
    sync = mido.MidiTrack([mido.MetaMessage("set_tempo", tempo=500000, time=0)])
    mid.tracks.append(sync)

    def ps(diff, code, value, time):
        return mido.Message("sysex", data=[0x50, 0x53, 0x00, 0x00, diff, code, value], time=time)

    tr = mido.MidiTrack([mido.MetaMessage("track_name", name="PART GUITAR", time=0)])
    tr += [ps(3, 0x01, 1, 0),                                   # open phrase, Expert
           mido.Message("note_on", note=G, velocity=100, time=0),
           mido.Message("note_off", note=G, velocity=0, time=10),
           ps(3, 0x01, 0, 0),
           ps(0x7F, 0x04, 1, 470),                              # tap phrase, all difficulties
           mido.Message("note_on", note=R, velocity=100, time=0),
           mido.Message("note_off", note=R, velocity=0, time=10),
           ps(0x7F, 0x04, 0, 0)]
    mid.tracks.append(tr)
    mid.save(str(tmp_path / "n.mid"))
    # Phase Shift writes "all difficulties" as 0xFF, which mido refuses to encode: patch it in.
    raw = (tmp_path / "n.mid").read_bytes()
    assert raw.count(bytes([0x50, 0x53, 0, 0, 0x7F, 0x04])) == 2
    (tmp_path / "n.mid").write_bytes(raw.replace(bytes([0x50, 0x53, 0, 0, 0x7F, 0x04]),
                                                 bytes([0x50, 0x53, 0, 0, 0xFF, 0x04])))
    c = fivefretchart.parse_mid(tmp_path / "n.mid")
    assert _tf(c) == [(0.0, 0), (0.5, 0b00010)]
    assert [n.tap for n in c.notes] == [False, True]


def test_tap_note(tmp_path):
    c = fivefretchart.parse_mid(_mid(tmp_path / "n.mid", [(0, G, 10), (480, R, 10), (480, TAP, 10)]))
    assert [n.tap for n in c.notes] == [False, True]
    assert c.notes[1].hopo is False


def test_sustain_cutoff(tmp_path):
    c = fivefretchart.parse_mid(_mid(tmp_path / "n.mid", [(0, G, 100), (960, R, 480)]))
    assert [round(n.length, 3) for n in c.notes] == [0.0, 0.5]


def test_ini_hopo_and_sustain_overrides(tmp_path):
    notes = [(0, G, 200), (240, R, 10)]
    eighth = fivefretchart.parse_mid(_mid(tmp_path / "a.mid", notes), {"eighthnote_hopo": "1"})
    assert _hopos(eighth) == [False, True]        # 480/2 + 1 = 241 ticks
    freq = fivefretchart.parse_mid(_mid(tmp_path / "b.mid", notes), {"hopo_frequency": "100"})
    assert _hopos(freq) == [False, False]
    cut = fivefretchart.parse_mid(_mid(tmp_path / "c.mid", notes), {"sustain_cutoff_threshold": "300"})
    assert cut.notes[0].length == 0.0


def test_mid_hopo_frequency_adds_one_tick(tmp_path):
    # MidReader: threshold = hopo_frequency + 1, so 100 -> notes up to 101 ticks apart are HOPOs
    ini = {"hopo_frequency": "100"}
    at = fivefretchart.parse_mid(_mid(tmp_path / "a.mid", [(0, G, 10), (101, R, 10)]), ini)
    past = fivefretchart.parse_mid(_mid(tmp_path / "b.mid", [(0, G, 10), (102, R, 10)]), ini)
    assert _hopos(at) == [False, True]
    assert _hopos(past) == [False, False]


def test_legacy_hopofreq(tmp_path):
    # hopofreq 5 -> 4 * 480 / 4 = 480 ticks (+1 for .mid); out-of-range values fall back to 480/3
    def gap(ticks, ini):
        return _hopos(fivefretchart.parse_mid(_mid(tmp_path / f"{ticks}.mid", [(0, G, 10), (ticks, R, 10)]), ini))[1]
    assert gap(481, {"hopofreq": "5"}) is True
    assert gap(482, {"hopofreq": "5"}) is False
    assert gap(161, {"hopofreq": "9"}) is True
    assert gap(162, {"hopofreq": "9"}) is False
    assert fivefretchart.hopo_threshold({"hopofreq": "0"}, 480, "chart") == 4 * 480 // 24 + 2


def test_mid_sustain_cutoff_zero_means_one_tick(tmp_path):
    assert fivefretchart.sustain_cutoff({"sustain_cutoff_threshold": "0"}, 480, "mid") == 1
    assert fivefretchart.sustain_cutoff({"sustain_cutoff_threshold": "0"}, 480, "chart") == 0
    c = fivefretchart.parse_mid(_mid(tmp_path / "n.mid", [(0, G, 100)]), {"sustain_cutoff_threshold": "0"})
    assert c.notes[0].length > 0                   # not the 160-tick default


def test_mid_marker_precedence(tmp_path):
    tap_vs_hopo = fivefretchart.parse_mid(_mid(tmp_path / "a.mid", [(0, G, 10), (120, R, 10), (120, TAP, 10),
                                                                    (120, FORCE_HOPO, 10)]))
    assert (tap_vs_hopo.notes[1].tap, tap_vs_hopo.notes[1].hopo) == (True, False)
    hopo_vs_strum = fivefretchart.parse_mid(_mid(tmp_path / "b.mid", [(0, G, 10), (240, R, 10),
                                                                      (240, FORCE_STRUM, 10),
                                                                      (240, FORCE_HOPO, 10)]))
    assert _hopos(hopo_vs_strum) == [False, True]


def test_mid_marker_excludes_end_tick(tmp_path):
    # A force-HOPO marker on ticks 0..240 covers [0, 239]: the note at 240 keeps its natural (strum) state.
    c = fivefretchart.parse_mid(_mid(tmp_path / "n.mid", [(0, G, 10), (240, R, 10), (0, FORCE_HOPO, 240)]))
    assert _hopos(c) == [True, False]


def test_star_power_and_solo_ranges(tmp_path):
    c = fivefretchart.parse_mid(_mid(tmp_path / "n.mid", [(0, G, 10), (0, 116, 960), (960, 103, 480)]))
    assert c.star_power == [(0.0, 1.0)]
    assert c.solos == [(1.0, 1.5)]


def test_delay_applied(tmp_path):
    _mid(tmp_path / "notes.mid", [(0, G, 10), (480, R, 10), (0, 116, 480)])
    write_ini(tmp_path / "song.ini", name="S", artist="A", delay=250)
    parts, ini = fivefretchart.load_song_folder(tmp_path)
    c = parts["guitar"]
    assert _tf(c) == [(0.25, 0b00001), (0.75, 0b00010)]
    assert c.star_power == [(0.25, 0.75)]
    assert ini["name"] == "S"


def test_negative_delay_drops_early_notes(tmp_path, caplog):
    """A negative delay pushes early notes before 0: they are dropped (with a warning),
    ranges are clamped to start at 0 or dropped, and the payload still validates."""
    _mid(tmp_path / "notes.mid", [(0, G, 10), (480, R, 10), (960, Y, 10), (1440, B, 480),
                                  (0, 116, 1440), (0, 103, 480), (960, 103, 480)])
    write_ini(tmp_path / "song.ini", name="S", artist="A", delay=-750)
    with caplog.at_level("WARNING", logger="feedBack.lib.fivefretchart"):
        parts, _ = fivefretchart.load_song_folder(tmp_path)
    c = parts["guitar"]
    assert _tf(c) == [(0.25, 0b00100), (0.75, 0b01000)]               # later notes intact
    assert c.notes[1].length == pytest.approx(0.5)
    assert c.star_power == [(0.0, 0.75)]                               # clamped to 0
    assert c.solos == [(0.25, 0.75)]                                   # the pre-roll solo is dropped
    assert any("2 note(s)" in r.getMessage() for r in caplog.records)
    fivefretchart.ensure_levels(c)
    data = fivefretchart.five_fret_json(c, {"kind": "chart"})
    assert fivefret.validate_five_fret(data) == (True, "")


def test_negative_chart_offset_drops_early_notes(tmp_path):
    write_chart(tmp_path / "notes.chart", [(0, "N", 0, 0), (192, "N", 1, 0), (384, "N", 2, 0)],
                section="ExpertSingle", offset=-0.6)
    parts, _ = fivefretchart.load_song_folder(tmp_path)
    assert _tf(parts["guitar"]) == [(0.4, 0b00100)]


def test_missing_part_raises(tmp_path):
    p = _mid(tmp_path / "n.mid", [(0, G, 10)])
    with pytest.raises(ValueError):
        fivefretchart.parse_mid(p, part="bass")


def test_bass_only_folder(tmp_path):
    _mid(tmp_path / "notes.mid", [(0, G, 10)], track_name="PART BASS")
    parts, _ = fivefretchart.load_song_folder(tmp_path)
    assert set(parts) == {"bass"}
    assert parts["bass"].part == "bass"


def test_rhythm_coop_fallback(tmp_path):
    _mid(tmp_path / "notes.mid", [(0, G, 10)], track_name="PART GUITAR COOP")
    parts, _ = fivefretchart.load_song_folder(tmp_path)
    assert set(parts) == {"rhythm"}


# ── .chart ──────────────────────────────────────────────────────────────────

def test_chart_parts(tmp_path):
    guitar = [(0, "N", 0, 0), (192, "N", 1, 0), (192, "N", 5, 0),     # 192 > 65 ticks: N 5 flips to HOPO
              (384, "N", 2, 0), (384, "N", 6, 0),                      # tap
              (576, "N", 7, 0),                                        # open
              (0, "S", 2, 384), (0, "E", "solo"), (384, "E", "soloend")]
    write_chart(tmp_path / "notes.chart", guitar, section="ExpertSingle",
                extra_sections={"ExpertDoubleBass": [(0, "N", 4, 0)]})
    parts, ini = fivefretchart.load_song_folder(tmp_path)
    assert set(parts) == {"guitar", "bass"}
    g = parts["guitar"]
    assert g.source_format == "chart"
    assert [n.frets for n in g.notes] == [0b00001, 0b00010, 0b00100, 0]
    assert _hopos(g) == [False, True, False, False]
    assert [n.tap for n in g.notes] == [False, False, True, False]
    assert g.star_power == [(0.0, 1.0)] and g.solos == [(0.0, 1.0)]
    assert [n.frets for n in parts["bass"].notes] == [0b10000]
    assert ini["name"] == "Song"


def test_chart_hopo_threshold_and_no_default_sustain_cutoff(tmp_path):
    # .chart: natural HOPO within 192/3 + 192/192 = 65 ticks; no sustain cutoff unless song.ini sets one
    p = write_chart(tmp_path / "notes.chart", [(0, "N", 0, 10), (65, "N", 1, 0), (131, "N", 2, 0)],
                    section="ExpertSingle")
    c = fivefretchart.parse_chart(p)
    assert _hopos(c) == [False, True, False]
    assert c.notes[0].length > 0
    assert fivefretchart.parse_chart(p, {"sustain_cutoff_threshold": "64"}).notes[0].length == 0.0
    assert fivefretchart.hopo_threshold({}, 480, "chart") == 162          # 480/3 + 480//192, not 480/3 + 1


def test_chart_single_after_chord_is_hopo(tmp_path):
    # .chart has no subset exception: a single note within the threshold after a chord is a HOPO,
    # even when its fret was in that chord (it would be a strum in .mid).
    p = write_chart(tmp_path / "notes.chart", [(0, "N", 0, 0), (0, "N", 1, 0), (64, "N", 1, 0)],
                    section="ExpertSingle")
    assert _hopos(fivefretchart.parse_chart(p)) == [False, True]


def test_chart_tap_beats_forced(tmp_path):
    p = write_chart(tmp_path / "notes.chart", [(0, "N", 0, 0), (192, "N", 1, 0), (192, "N", 5, 0),
                                               (192, "N", 6, 0)], section="ExpertSingle")
    n = fivefretchart.parse_chart(p).notes[1]
    assert (n.tap, n.hopo) == (True, False)


def test_chart_missing_part_raises(tmp_path):
    p = write_chart(tmp_path / "notes.chart", [(0, "N", 0, 0)], section="ExpertSingle")
    with pytest.raises(ValueError):
        fivefretchart.parse_chart(p, part="rhythm")


# ── levels and JSON ─────────────────────────────────────────────────────────

def _level_folder(tmp_path):
    expert = [(0, G, 10), (0, Y, 10), (0, O, 10), (120, R, 10), (240, O, 10), (480, B, 10), (960, G, 10),
              (960, O, 10), (1440, Y, 10)]
    hard = [(0, 84, 10), (480, 86, 10)]
    _mid(tmp_path / "notes.mid", expert + hard)
    return fivefretchart.load_song_folder(tmp_path)


def test_lower_levels_read_and_generated(tmp_path):
    parts, _ = _level_folder(tmp_path)
    c = parts["guitar"]
    assert [n.frets for n in c.levels["hard"]] == [0b00001, 0b00100]
    assert "medium" not in c.levels and "easy" not in c.levels
    fivefretchart.ensure_levels(c)
    assert [n.frets for n in c.levels["hard"]] == [0b00001, 0b00100]    # the chart's own Hard is kept
    assert "hard" not in c.levels_generated
    assert sorted(c.levels_generated) == ["easy", "medium"]
    medium, easy = c.levels["medium"], c.levels["easy"]
    assert medium and all(not n.frets & 0b10000 for n in medium)
    assert all(bin(n.frets).count("1") <= 2 for n in medium)
    assert easy and all(bin(n.frets).count("1") <= 1 for n in easy)
    assert all(not n.frets & 0b11000 for n in easy)
    assert all(not n.hopo for n in medium + easy)
    gaps = [b.time - a.time for a, b in zip(easy, easy[1:])]
    assert all(g >= 0.35 - 1e-9 for g in gaps)


def test_ensure_levels_generates_hard(tmp_path):
    _mid(tmp_path / "notes.mid", [(0, G, 10), (0, Y, 10), (0, O, 10), (24, R, 10), (480, B, 10)])
    parts, _ = fivefretchart.load_song_folder(tmp_path)
    c = parts["guitar"]
    fivefretchart.ensure_levels(c)
    assert sorted(c.levels_generated) == ["easy", "hard", "medium"]
    assert [(round(n.time, 3), n.frets) for n in c.levels["hard"]] == [(0.0, 0b10001), (0.5, 0b01000)]


def test_five_fret_json_validates(tmp_path):
    parts, _ = _level_folder(tmp_path)
    c = parts["guitar"]
    c.notes[1].length = 0.5
    c.star_power, c.solos = [(0.0, 1.0)], [(1.0, 1.5)]
    fivefretchart.ensure_levels(c)
    data = fivefretchart.five_fret_json(c, {"kind": "chart", "origin": "local"})
    assert fivefret.validate_five_fret(data) == (True, "")
    assert data["version"] == fivefret.SCHEMA_VERSION and data["instrument"] == "guitar"
    assert data["name"] == "Guitar"
    assert data["notes"][0] == {"t": 0.0, "f": 0b10101}               # no l / h / tap when unset
    assert data["notes"][1] == {"t": 0.125, "f": 0b00010, "l": 0.5, "h": True}
    assert data["star_power"] == [{"start_time": 0.0, "end_time": 1.0}]
    assert data["solos"] == [{"start_time": 1.0, "end_time": 1.5}]
    assert "generated" not in data["levels"]["hard"]
    assert data["levels"]["medium"]["generated"] is True
    assert fivefretchart.five_fret_json(c, {"kind": "chart"}, name="Lead")["name"] == "Lead"


def test_drumchart_public_helpers():
    import drumchart
    assert drumchart.chart_sections is drumchart._chart_sections
    assert drumchart.chart_song_section is drumchart._chart_song_section
    assert drumchart.normalize_text is drumchart._normalize_text
