"""lib/vocalchart.py (Rock Band / Clone Hero PART VOCALS -> lyrics.json + vocal_pitch.json) and
its use in scripts/ch_to_sloppak.py, with synthetic charts (no ffmpeg or real audio needed)."""

from __future__ import annotations

import json
import zipfile

import pytest
import yaml

import vocalchart
from tests.drum_fixtures import FAKE_OGG, TPQ, write_ini, write_mid

PHRASE, PHRASE_P2, OVERDRIVE = 105, 106, 116


def _mid(tmp_path, notes, lyrics, *, track="PART VOCALS", text_type="text", tempos=((0, 120.0),)):
    """A notes.mid with only a vocals track. notes: (tick, midi, length); lyrics: (tick, text)."""
    return write_mid(tmp_path / "notes.mid", [], tempos=tempos, track_name="",
                     extra_tracks=[(track, [(t, n, 100, ln) for t, n, ln in notes], lyrics, text_type)])


def _tokens(chart):
    return [(t["t"], t["d"], t["w"]) for t in vocalchart.lyrics_json(chart)]


def _pitch(chart):
    return [(n["t"], n["d"], n["midi"]) for n in vocalchart.vocal_pitch_json(chart)["notes"]]


# ── parsing ─────────────────────────────────────────────────────────────────

def test_syllables_pair_with_notes_at_their_tick(tmp_path):
    p = _mid(tmp_path, [(0, 60, 240), (480, 62, 480)], [(0, "Hel-"), (480, "lo")])
    c = vocalchart.parse_mid(p)
    assert _tokens(c) == [(0.0, 0.25, "Hel-"), (0.5, 0.5, "lo")]
    assert _pitch(c) == [(0.0, 0.25, 60), (0.5, 0.5, 62)]
    assert vocalchart.vocal_pitch_json(c)["version"] == 1


def test_lyrics_meta_events_and_bracketed_text(tmp_path):
    p = _mid(tmp_path, [(0, 60, 240)], [(0, "[play]"), (0, "Hey"), (960, "[idle]")], text_type="lyrics")
    assert _tokens(vocalchart.parse_mid(p)) == [(0.0, 0.25, "Hey")]


def test_tempo_map_and_song_ini_delay(tmp_path):
    p = _mid(tmp_path, [(1920, 60, 480)], [(1920, "late")], tempos=[(0, 120.0), (960, 60.0)])
    c = vocalchart.parse_mid(p, {"delay": "500"})
    # 960 ticks at 120 bpm = 1 s, 960 more at 60 bpm = 2 s, + 0.5 s delay; 480 ticks at 60 bpm = 1 s
    assert _tokens(c) == [(3.5, 1.0, "late")]


def test_talkies_have_text_but_no_pitch(tmp_path):
    p = _mid(tmp_path, [(0, 60, 240), (480, 50, 240), (960, 50, 240), (1440, 64, 240)],
             [(0, "Say#"), (480, "hey^"), (960, "yo*"), (1440, "sing")])
    c = vocalchart.parse_mid(p)
    assert _tokens(c) == [(0.0, 0.25, "Say"), (0.5, 0.25, "hey"), (1.0, 0.25, "yo"), (1.5, 0.25, "sing")]
    assert [n.talkie for n in c.notes] == [True, True, True, False]
    assert all(set(t) == {"t", "d", "w"} for t in vocalchart.lyrics_json(c))    # spec §7.1 fields only
    assert _pitch(c) == [(1.5, 0.25, 64)]


def test_pitch_slide_is_a_joined_wordless_syllable(tmp_path):
    # "fall-" "in'" "+" "rain": the slide continues "in'" to a new pitch
    p = _mid(tmp_path, [(0, 60, 240), (240, 62, 120), (360, 59, 120), (480, 60, 240)],
             [(0, "fall-"), (240, "in'"), (360, "+"), (480, "rain")])
    c = vocalchart.parse_mid(p)
    assert [w for _, _, w in _tokens(c)] == ["fall-", "in'-", "", "rain"]
    assert [m for _, _, m in _pitch(c)] == [60, 62, 59, 60]


def test_slide_keeps_the_word_joined_to_the_next_syllable(tmp_path):
    # "ged-" "+" "don": the slide sits inside the word, so it joins on to "don"
    p = _mid(tmp_path, [(0, 60, 120), (120, 62, 120), (240, 60, 240)], [(0, "ged-"), (120, "+"), (240, "don")])
    assert [w for _, _, w in _tokens(vocalchart.parse_mid(p))] == ["ged-", "-", "don"]


def test_rock_band_lyric_symbols(tmp_path):
    notes = [(i * 480, 60, 240) for i in range(5)]
    p = _mid(tmp_path, notes, [(0, "$hid"), (480, "den%"), (960, "e="), (1440, "mail§a"), (1920, "a_b/")])
    # "=" joins with a hyphen that stays visible (the reader strips one trailing "-")
    assert [w for _, _, w in _tokens(vocalchart.parse_mid(p))] == ["hid", "den", "e--", "mail‿a", "a b"]


def test_phrase_markers_end_lines_and_carry_overdrive(tmp_path):
    notes = [(0, 60, 240), (480, 62, 240), (960, 64, 240), (1920, 60, 240), (2400, 62, 240)]
    notes += [(0, PHRASE, 1440), (1920, PHRASE, 960), (1920, OVERDRIVE, 960)]
    p = _mid(tmp_path, notes, [(0, "one"), (480, "two"), (960, "thr-"), (1920, "four"), (2400, "five")])
    c = vocalchart.parse_mid(p)
    # a word cut by a phrase end loses its join and ends the line
    assert [w for _, _, w in _tokens(c)] == ["one", "two", "thr+", "four", "five+"]
    assert [(ph.start, ph.end, ph.overdrive) for ph in c.phrases] == [(0.0, 1.5, False), (2.0, 3.0, True)]


def test_player_two_phrases_are_the_fallback(tmp_path):
    notes = [(0, 60, 240), (480, 62, 240), (0, PHRASE_P2, 960)]
    c = vocalchart.parse_mid(_mid(tmp_path, notes, [(0, "a"), (480, "b")]))
    assert [w for _, _, w in _tokens(c)] == ["a", "b+"]


def test_notes_outside_the_vocal_range_are_ignored(tmp_path):
    # 0/1 = range/lyric shift, 96/97 = percussion
    p = _mid(tmp_path, [(0, 1, 240), (0, 60, 240), (480, 96, 10)], [(0, "la")])
    assert _pitch(vocalchart.parse_mid(p)) == [(0.0, 0.25, 60)]


def test_harm1_when_there_is_no_part_vocals(tmp_path):
    p = _mid(tmp_path, [(0, 60, 240)], [(0, "harm")], track="HARM1")
    assert _tokens(vocalchart.parse_mid(p)) == [(0.0, 0.25, "harm")]


def test_no_vocals_track(tmp_path):
    p = write_mid(tmp_path / "notes.mid", [(0, 96, 100, 10)])
    with pytest.raises(ValueError, match="no PART VOCALS"):
        vocalchart.parse_mid(p)
    assert vocalchart.load_song_folder(tmp_path) is None


def test_load_song_folder_reads_ini_delay(tmp_path):
    _mid(tmp_path, [(0, 60, 240)], [(0, "a")])
    write_ini(tmp_path / "song.ini", name="S", delay=250)
    c = vocalchart.load_song_folder(tmp_path)
    assert _tokens(c) == [(0.25, 0.25, "a")]


# ── ch_to_sloppak ───────────────────────────────────────────────────────────

def _band_folder(tmp_path, *, vocals=True):
    folder = tmp_path / "Band - Song"
    folder.mkdir()
    drums = [(i * TPQ, 96 if i % 2 == 0 else 97, 100, 10) for i in range(16)]
    extra = []
    if vocals:
        vox = [(0, 60, 100, 240), (480, 64, 100, 240), (960, 50, 100, 240), (0, PHRASE, 100, 1440)]
        extra = [("PART VOCALS", vox, [(0, "Hel-"), (480, "lo"), (960, "hey#")], "lyrics")]
        (folder / "vocals.ogg").write_bytes(FAKE_OGG)
    write_mid(folder / "notes.mid", drums, extra_tracks=extra)
    write_ini(folder / "song.ini", name="Song", artist="Band", delay=0)
    (folder / "song.ogg").write_bytes(FAKE_OGG)
    return folder


def test_convert_writes_authored_lyrics_and_pitch(tmp_path):
    from scripts import ch_to_sloppak
    from sloppak import load_song
    out = tmp_path / "dlc" / "Song.sloppak"
    out.parent.mkdir()
    s = ch_to_sloppak.convert(_band_folder(tmp_path), out)
    assert s["lyrics"] == 3 and s["vocal_pitch"] == 2
    assert s["stems"] == ["vocals", "other"]
    with zipfile.ZipFile(out) as z:
        man = yaml.safe_load(z.read("manifest.yaml"))
        lyrics = json.loads(z.read("lyrics.json"))
        pitch = json.loads(z.read("vocal_pitch.json"))
    assert man["lyrics"] == "lyrics.json" and man["vocal_pitch"] == "vocal_pitch.json"
    assert man["lyrics_source"] == "authored"
    assert [x["w"] for x in lyrics] == ["Hel-", "lo", "hey+"]
    assert pitch == {"version": 1, "notes": [{"t": 0.0, "d": 0.25, "midi": 60},
                                             {"t": 0.5, "d": 0.25, "midi": 64}]}
    loaded = load_song(out.name, out.parent, tmp_path / "cache")
    assert [x["w"] for x in loaded.song.lyrics] == ["Hel-", "lo", "hey+"]
    assert loaded.song.lyrics_source == "authored"


def test_convert_without_vocals_writes_no_lyrics(tmp_path):
    from scripts import ch_to_sloppak
    out = tmp_path / "p.sloppak"
    s = ch_to_sloppak.convert(_band_folder(tmp_path, vocals=False), out, as_dir=True)
    assert s["lyrics"] == 0 and s["vocal_pitch"] == 0
    man = yaml.safe_load((out / "manifest.yaml").read_text())
    assert "lyrics" not in man and "vocal_pitch" not in man
    assert not (out / "lyrics.json").exists()
