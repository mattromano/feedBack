"""scripts/ch_to_sloppak.py + scripts/sloppak_to_ch.py with 5-fret guitar / rhythm / bass parts."""

from __future__ import annotations

import json
import zipfile

import mido
import pytest
import yaml

import fivefret
import fivefretchart
from scripts import ch_to_sloppak, sloppak_to_ch
from sloppak import load_song
from tests.drum_fixtures import FAKE_OGG, TPQ, write_chart, write_ini, write_mid, write_sng

G, R, Y, B, O = 96, 97, 98, 99, 100            # Expert frets
HG, HR, HY, HB = 84, 85, 86, 87                # Hard frets
FORCE_HOPO, FORCE_STRUM, TAP, SOLO, SP = 101, 102, 104, 103, 116
OPEN = 95                                      # Expert open, with [ENHANCED_OPENS]


def _guitar():
    """Expert guitar exercising every flag, plus an authored Hard part: (tick, note, vel, len)."""
    n = [(0, G, 10), (120, R, 10), (240, Y, 10),                    # strum, natural HOPO x2
         (480, G, 10), (480, Y, 10), (600, B, 10),                  # chord, HOPO outside the chord
         (720, B, 10), (720, FORCE_HOPO, 1),                        # same fret, forced HOPO
         (960, R, 10), (960, B, 10), (960, FORCE_HOPO, 10),         # forced-HOPO chord
         (1080, Y, 10), (1080, FORCE_STRUM, 1),                     # would be natural, forced strum
         (1440, O, 960),                                            # sustain
         (2400, G, 10), (2400, TAP, 10),                            # tap
         (2880, OPEN, 10), (3000, R, 10),                           # open, then natural HOPO
         (3840, G, 10), (3960, R, 600), (4320, R, 10), (4560, Y, 10), (4560, O, 10)]
    n += [(480, SP, 960), (2400, SOLO, 1440)]
    n += [(0, HG, 10), (480, HY, 10), (1440, HB, 960), (3840, HR, 10)]
    return [(t, note, 100, ln) for t, note, ln in n]


def _bass(start=0, count=16):
    return [(start + i * TPQ, G + i % 5, 100, 10) for i in range(count)]


def _folder(tmp_path, *, drums=False, guitar=True, bass=True, name="src"):
    folder = tmp_path / name
    folder.mkdir()
    extra = []
    if guitar:
        extra.append(("PART GUITAR", _guitar(), [(0, "[ENHANCED_OPENS]")]))
    if bass:
        extra.append(("PART BASS", _bass(), []))
    drum_notes = [(i * TPQ, 96 if i % 2 == 0 else 97, 100, 10) for i in range(4)] if drums else []
    write_mid(folder / "notes.mid", drum_notes, track_name="PART DRUMS" if drums else None,
              tempos=[(0, 120.0), (3840, 150.0)], sigs=[(0, 4, 4)],
              sections=[(0, "Intro"), (3840, "Verse")], extra_tracks=extra)
    write_ini(folder / "song.ini", name="Five", artist="Band", year=2001, delay=40)
    (folder / "song.ogg").write_bytes(FAKE_OGG)
    return folder


def _pack_json(pack, name):
    with zipfile.ZipFile(pack) as z:
        return json.loads(z.read(name)) if name.endswith(".json") else yaml.safe_load(z.read(name))


# ── ch_to_sloppak ───────────────────────────────────────────────────────────

def test_guitar_and_bass_no_drums_converts(tmp_path):
    pack = tmp_path / "gb.sloppak"
    s = ch_to_sloppak.convert(_folder(tmp_path), pack)
    assert s["notes"] == 0 and s["five_fret"] == {"guitar": 16, "bass": 16}
    man = _pack_json(pack, "manifest.yaml")
    assert "drum_tab" not in man
    assert [a["type"] for a in man["arrangements"]] == ["five_fret", "five_fret"]
    gtr = _pack_json(pack, "five_fret_guitar.json")
    assert fivefret.validate_five_fret(gtr) == (True, "")
    assert gtr["levels"]["hard"].get("generated") is None                   # authored
    assert gtr["levels"]["easy"]["generated"] and gtr["levels"]["medium"]["generated"]
    tl = _pack_json(pack, "song_timeline.json")
    last = max(n["t"] for n in gtr["notes"])
    assert tl["beats"][-1]["time"] >= last                                  # beats reach the last note
    assert [x["name"] for x in tl["sections"]] == ["Intro", "Verse"]
    assert man["duration"] >= last + 1.0
    loaded = load_song(pack.name, pack.parent, tmp_path / "cache")
    assert [p["id"] for p in loaded.five_fret_parts] == ["five_fret_guitar", "five_fret_bass"]
    assert loaded.drum_tab is None and loaded.song.beats and loaded.feedpak_version == "1.21.0"


def test_drums_plus_bass_converts_both(tmp_path):
    pack = tmp_path / "db.sloppak"
    s = ch_to_sloppak.convert(_folder(tmp_path, drums=True, guitar=False), pack)
    assert s["notes"] == 4 and s["five_fret"] == {"bass": 16}
    man = _pack_json(pack, "manifest.yaml")
    assert [a["id"] for a in man["arrangements"]] == ["drums", "five_fret_bass"]
    assert man["drum_tab"] == "drum_tab.json"
    # duration and beats follow the bass, which runs past the 4 drum hits
    last = _pack_json(pack, "five_fret_bass.json")["notes"][-1]["t"]
    assert last > 4.0 and man["duration"] >= last + 1.0
    assert _pack_json(pack, "song_timeline.json")["beats"][-1]["time"] >= last
    loaded = load_song(pack.name, pack.parent, tmp_path / "cache")
    assert len(loaded.drum_tab["hits"]) == 4
    assert [(p["id"], p["instrument"]) for p in loaded.five_fret_parts] == [("five_fret_bass", "bass")]


def test_empty_drum_track_is_skipped(tmp_path):
    folder = tmp_path / "e"
    folder.mkdir()
    write_mid(folder / "notes.mid", [], extra_tracks=[("PART BASS", _bass(), [])])   # PART DRUMS, no notes
    write_ini(folder / "song.ini", name="E", artist="B")
    (folder / "song.ogg").write_bytes(FAKE_OGG)
    pack = tmp_path / "e.sloppak"
    ch_to_sloppak.convert(folder, pack)
    assert [a["id"] for a in _pack_json(pack, "manifest.yaml")["arrangements"]] == ["five_fret_bass"]


def test_neither_raises(tmp_path):
    folder = tmp_path / "n"
    folder.mkdir()
    write_mid(folder / "notes.mid", [], track_name=None,
              extra_tracks=[("PART VOCALS", [(0, 60, 100, 240)], [(0, "la")], "lyrics")])
    write_ini(folder / "song.ini", name="N", artist="B")
    (folder / "song.ogg").write_bytes(FAKE_OGG)
    with pytest.raises(ValueError, match="no drums or 5-fret"):
        ch_to_sloppak.convert(folder, tmp_path / "n.sloppak")
    assert not (tmp_path / "n.sloppak").exists()


def test_negative_delay_converts_valid(tmp_path):
    folder = _folder(tmp_path, guitar=False)
    write_ini(folder / "song.ini", name="Five", artist="Band", delay=-1250)     # first 3 bass notes < 0
    pack = tmp_path / "neg.sloppak"
    s = ch_to_sloppak.convert(folder, pack)
    assert s["five_fret"] == {"bass": 13}
    bass = _pack_json(pack, "five_fret_bass.json")
    assert fivefret.validate_five_fret(bass) == (True, "")
    assert bass["notes"][0]["t"] == pytest.approx(0.25)


def test_invalid_five_fret_payload_raises(tmp_path, monkeypatch):
    real = fivefretchart.five_fret_json

    def broken(*a, **kw):
        data = real(*a, **kw)
        data["notes"][0]["t"] = -1.0
        return data
    monkeypatch.setattr(fivefretchart, "five_fret_json", broken)
    out = tmp_path / "bad.sloppak"
    with pytest.raises(ValueError, match=r"five_fret_bass\.json.*t must be a number >= 0"):
        ch_to_sloppak.convert(_folder(tmp_path, guitar=False), out)
    assert not out.exists()


def test_manifest_entries_and_version(tmp_path):
    pack = tmp_path / "m.sloppak"
    ch_to_sloppak.convert(_folder(tmp_path), pack)
    man = _pack_json(pack, "manifest.yaml")
    assert man["feedpak_version"] == "1.21.0"
    assert man["arrangements"] == [
        {"id": "five_fret_guitar", "name": "Guitar (5-fret)", "type": "five_fret", "instrument": "guitar",
         "five_fret": "five_fret_guitar.json"},
        {"id": "five_fret_bass", "name": "Bass (5-fret)", "type": "five_fret", "instrument": "bass",
         "five_fret": "five_fret_bass.json"}]
    for part, name in (("guitar", "Guitar (5-fret)"), ("bass", "Bass (5-fret)")):
        data = _pack_json(pack, f"five_fret_{part}.json")
        assert data["name"] == name and data["instrument"] == part
        assert data["source"] == {"kind": "chart", "origin": "local"}


def test_sng_md5_goes_into_source(tmp_path):
    folder = _folder(tmp_path, guitar=False)
    files = {n: (folder / n).read_bytes() for n in ("notes.mid", "song.ogg")}
    sng = write_sng(tmp_path / "s.sng", files, {"name": "Packed", "artist": "Band"})
    pack = tmp_path / "s.sloppak"
    ch_to_sloppak.convert(sng, pack, md5="0123456789abcdef0123456789abcdef")
    assert _pack_json(pack, "five_fret_bass.json")["source"] == {
        "kind": "chart", "origin": "local", "md5": "0123456789abcdef0123456789abcdef"}
    # a plain folder is not identified by the caller's md5
    ch_to_sloppak.convert(folder, tmp_path / "f.sloppak", md5="ffff")
    assert "md5" not in _pack_json(tmp_path / "f.sloppak", "five_fret_bass.json")["source"]


# ── round trip through sloppak_to_ch ────────────────────────────────────────

def _expert(folder):
    parts, _ = fivefretchart.load_song_folder(folder)
    return {p: [fivefretchart.note_json(n) for n in c.notes] for p, c in parts.items()}, parts


def test_round_trip_expert_exact(tmp_path):
    src = _folder(tmp_path)
    want, orig = _expert(src)
    assert {n.get("h") for n in want["guitar"]} == {True, None}             # the fixture has HOPOs,
    assert any(n.get("tap") for n in want["guitar"]) and any(n["f"] == 0 for n in want["guitar"])
    pack = tmp_path / "rt.sloppak"
    ch_to_sloppak.convert(src, pack)
    out = tmp_path / "out"
    s = sloppak_to_ch.export(pack, out)
    assert s["parts"] == {"PART GUITAR": 16, "PART BASS": 16}
    got, back = _expert(out)
    assert got == want
    assert back["guitar"].star_power == pytest.approx(orig["guitar"].star_power, abs=0.002)
    assert back["guitar"].solos == pytest.approx(orig["guitar"].solos, abs=0.002)
    # the authored Hard part comes back; generated Easy/Medium are not written
    assert set(back["guitar"].levels) == {"hard"}
    assert [fivefretchart.note_json(n) for n in back["guitar"].levels["hard"]] == \
        [fivefretchart.note_json(n) for n in orig["guitar"].levels["hard"]]
    assert back["bass"].levels == {}
    ini = (out / "song.ini").read_text()
    assert "pro_drums" not in ini
    mid = mido.MidiFile(str(out / "notes.mid"))
    assert "PART DRUMS" not in [t.name for t in mid.tracks]


def test_round_trip_from_chart(tmp_path):
    folder = tmp_path / "c"
    folder.mkdir()
    lines = [(0, "N", 0, 0), (48, "N", 1, 24),                       # short sustain (no .chart cutoff)
             (96, "N", 2, 0), (96, "N", 5, 0),                       # natural HOPO flipped to strum
             (192, "N", 0, 0), (192, "N", 2, 0), (288, "N", 3, 0),
             (384, "N", 7, 0), (480, "N", 4, 96), (480, "N", 6, 0),   # open; tapped sustain
             (576, "S", 2, 192), (768, "N", 1, 0), (768, "E", "solo"), (960, "E", "soloend"),
             (1152, "N", 0, 0), (1152, "N", 7, 0)]                   # open + fret keeps the fret
    write_chart(folder / "notes.chart", lines, tempos=[(0, 120.0), (768, 100.0)], section="ExpertSingle")
    write_ini(folder / "song.ini", name="C", artist="B")
    (folder / "song.ogg").write_bytes(FAKE_OGG)
    want, orig = _expert(folder)
    assert any(n.get("l", 1) < 0.2 for n in want["guitar"])
    pack = tmp_path / "c.sloppak"
    ch_to_sloppak.convert(folder, pack)
    sloppak_to_ch.export(pack, tmp_path / "o")
    got, back = _expert(tmp_path / "o")
    assert got == want
    assert back["guitar"].solos == pytest.approx(orig["guitar"].solos, abs=0.002)


def test_export_writes_opens_and_markers(tmp_path):
    pack = tmp_path / "x.sloppak"
    ch_to_sloppak.convert(_folder(tmp_path, bass=False), pack)
    sloppak_to_ch.export(pack, tmp_path / "x")
    mid = mido.MidiFile(str(tmp_path / "x" / "notes.mid"))
    tr = next(t for t in mid.tracks if t.name == "PART GUITAR")
    texts = [m.text for m in tr if m.type == "text"]
    notes = {m.note for m in tr if m.type == "note_on" and m.velocity}
    assert "[ENHANCED_OPENS]" in texts
    assert {OPEN, FORCE_HOPO, FORCE_STRUM, TAP, SP, SOLO} <= notes
    assert {HG + 5, HG + 6} & notes                                     # Hard gets its own markers
