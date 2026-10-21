"""scripts/sloppak_to_ch.py, incl. a round trip CH folder -> pack -> CH folder."""

from __future__ import annotations

import json
import zipfile

import mido
import pytest
import yaml

import drumchart
import sngfile
from scripts import ch_to_sloppak, sloppak_to_ch
from tests.drum_fixtures import FAKE_OGG, TPQ, _mask_bytes, drum_pattern, write_ini, write_mid, write_sng

KICK, RED, YEL, BLU, ORG = 96, 97, 98, 99, 100


def _key(h):
    return (round(h.time, 2), h.pad, h.cymbal, h.kick2x, h.dyn)


def _source_folder(tmp_path):
    folder = tmp_path / "src"
    folder.mkdir()
    notes = drum_pattern(12)
    notes += [(TPQ * 3, 95, 100, 10),                                   # 2x kick
              (TPQ * 5, BLU, 100, 10), (TPQ * 5, 111, 100, 10),          # blue tom
              (TPQ * 6, ORG, 100, 10), (TPQ * 6, 112, 100, 10),          # green tom
              (TPQ * 7, RED, 127, 10), (TPQ * 9, RED, 1, 10),            # accent, ghost
              (TPQ * 8, 116, 100, TPQ * 4), (TPQ * 20, 120, 100, TPQ * 4),
              (TPQ * 28, 103, 100, TPQ * 8)]                             # solo
    write_mid(folder / "notes.mid", notes, tempos=[(0, 120.0), (TPQ * 16, 150.0), (TPQ * 32, 96.0)],
              sigs=[(0, 4, 4), (TPQ * 24, 3, 4), (TPQ * 30, 4, 4)],
              texts=[(0, "[ENABLE_CHART_DYNAMICS]")], sections=[(0, "Intro"), (TPQ * 16, "Verse")])
    write_ini(folder / "song.ini", name="Round Trip", artist="Band", year=1999, delay=40)
    (folder / "drums.ogg").write_bytes(FAKE_OGG)
    (folder / "song.ogg").write_bytes(FAKE_OGG + b"song")
    return folder


def test_round_trip_ch_pack_ch(tmp_path):
    folder = _source_folder(tmp_path)
    orig, _ = drumchart.load_song_folder(folder)
    pack = tmp_path / "rt.sloppak"
    ch_to_sloppak.convert(folder, pack)
    out = tmp_path / "out"
    s = sloppak_to_ch.export(pack, out)
    assert s["parts"]["PART DRUMS"] == len(orig.hits)
    assert s["stems"] == ["drums", "song"]
    assert (out / "drums.ogg").read_bytes() == FAKE_OGG and (out / "song.ogg").read_bytes() == FAKE_OGG + b"song"
    back, ini = drumchart.load_song_folder(out)
    assert ini["name"] == "Round Trip" and ini["pro_drums"] == "True"
    assert ini["delay"] == "40"       # first beat 40 ms in -> exported as delay, not a pickup beat
    assert sorted(map(_key, back.hits)) == sorted(map(_key, orig.hits))
    assert sorted(h.time for h in back.hits) == pytest.approx(sorted(h.time for h in orig.hits), abs=0.002)
    assert back.dynamics
    # the source has no lower levels, so ch_to_sloppak generated them; they export as
    # real Easy/Medium/Hard parts and come back identical
    tab = json.loads(zipfile.ZipFile(pack).read("drum_tab.json"))
    assert {lv: v.get("generated") for lv, v in tab["levels"].items()} == {
        "easy": True, "medium": True, "hard": True}
    for lv in drumchart.LOWER_LEVELS:
        want = sorted(map(_key, drumchart.tab_to_hits(tab["levels"][lv]["hits"])))
        assert sorted(map(_key, back.levels[lv])) == want, lv
    assert back.star_power == pytest.approx(orig.star_power, abs=0.002)
    assert back.fills == pytest.approx(orig.fills, abs=0.002)
    assert back.solos == pytest.approx(orig.solos, abs=0.002)
    assert [n for _, n in back.sections] == ["Intro", "Verse"]
    assert [t for t, _ in back.sections] == pytest.approx([t for t, _ in orig.sections], abs=0.002)
    # tempo map survives well enough that beats line up
    b0 = [b["time"] for b in orig.beat_list()]
    b1 = [b["time"] for b in back.beat_list()]
    n = min(len(b0), len(b1))
    assert b1[:n] == pytest.approx(b0[:n], abs=0.002)
    # and a second conversion gives the same drum tab
    pack2 = tmp_path / "rt2.sloppak"
    ch_to_sloppak.convert(out, pack2)
    tab2 = json.loads(zipfile.ZipFile(pack2).read("drum_tab.json"))
    assert [(h["p"], h.get("a"), h.get("g"), h.get("x2")) for h in tab["hits"]] == \
        [(h["p"], h.get("a"), h.get("g"), h.get("x2")) for h in tab2["hits"]]


def _ons(track):
    tick, out = 0, []
    for m in track:
        tick += m.time
        if m.type == "note_on" and m.velocity > 0:
            out.append((tick, m.note, m.velocity))
    return out


def _drum_pack(tmp_path, tabs: dict, *, beats, primary="drums"):
    """Directory-form pack with one or more drum parts, a song_timeline and a full stem."""
    root = tmp_path / "d.sloppak"
    (root / "stems").mkdir(parents=True)
    (root / "stems" / "full.ogg").write_bytes(FAKE_OGG)
    arrs = []
    for pid, tab in tabs.items():
        (root / f"drum_tab_{pid}.json").write_text(json.dumps(tab))
        arrs.append({"id": pid, "name": pid.title(), "type": "drums", "drum_tab": f"drum_tab_{pid}.json"})
    (root / "song_timeline.json").write_text(json.dumps({"version": 1, "beats": beats, "sections": []}))
    man = {"feedpak_version": "1.20.0", "title": "Parts", "artist": "Band", "duration": 12.0,
           "stems": [{"id": "full", "file": "stems/full.ogg", "default": True}], "arrangements": arrs,
           "drum_tab": f"drum_tab_{primary}.json", "song_timeline": "song_timeline.json"}
    (root / "manifest.yaml").write_text(yaml.safe_dump(man, sort_keys=False))
    return root


def test_export_picks_part_maps_pieces_and_handles_pickup(tmp_path):
    beats = [{"time": round(0.3 + 0.5 * i, 3), "measure": i // 4 + 1 if i % 4 == 0 else -1} for i in range(24)]
    main = {"version": 1, "hits": [{"t": 0.3 + 0.5 * i, "p": "kick"} for i in range(8)]}
    alt = {"version": 1, "hits": [{"t": 0.3, "p": "ride_bell"}, {"t": 0.8, "p": "tom_low", "a": True},
                                  {"t": 1.3, "p": "cowbell"}, {"t": 1.8, "p": "hh_open", "g": True}],
           "star_power": [{"start_time": 0.3, "end_time": 1.3}]}
    pack = _drum_pack(tmp_path, {"drums": main, "live": alt}, beats=beats)
    s = sloppak_to_ch.export(pack, tmp_path / "a")
    assert s["parts"]["PART DRUMS"] == 8 and s["stems"] == ["song"]     # lone full mix -> song.ogg
    back, _ = drumchart.load_song_folder(tmp_path / "a")
    assert len(back.hits) == 8 and all(h.pad == "kick" for h in back.hits)
    assert back.hits[0].time == pytest.approx(0.3, abs=0.002)
    # pickup: first beat at 0.3 s -> 1 pickup beat, beat 1 lands on tick 480
    clock = sloppak_to_ch.BeatClock(beats)
    assert clock.pickup == 1 and clock.tick(0.3) == TPQ and clock.tick(0.0) == 0 and clock.tick(0.8) == 2 * TPQ

    s = sloppak_to_ch.export(pack, tmp_path / "b", part="live")
    assert s["parts"]["PART DRUMS"] == 3                                 # unknown piece dropped
    mid = mido.MidiFile(str(tmp_path / "b" / "notes.mid"))
    ons = _ons(next(t for t in mid.tracks if t.name == "PART DRUMS"))
    notes = {n for _, n, _ in ons}
    assert {99, 100, 98, 112, 116} <= notes                              # blue cym, green tom + marker, SP
    assert (TPQ * 2, 100, 127) in ons                                    # accented floor tom
    back, _ = drumchart.load_song_folder(tmp_path / "b")
    assert [(h.pad, h.cymbal, h.dyn) for h in back.hits] == [
        ("blue", True, None), ("green", False, "accent"), ("yellow", True, "ghost")]
    assert back.star_power == pytest.approx([(0.3, 1.3)], abs=0.002)

    with pytest.raises(ValueError, match="no drum part"):
        sloppak_to_ch.export(pack, tmp_path / "c", part="nope")


def test_export_cli_without_drums(tmp_path, caplog):
    root = tmp_path / "g.sloppak"
    (root / "arrangements").mkdir(parents=True)
    (root / "arrangements" / "lead.json").write_text(json.dumps({"notes": [{"t": 1.0, "s": 0, "f": 3}]}))
    (root / "manifest.yaml").write_text(yaml.safe_dump(
        {"title": "G", "artist": "B", "duration": 2.0, "stems": [],
         "arrangements": [{"id": "lead", "name": "Lead", "file": "arrangements/lead.json"}]}))
    assert sloppak_to_ch.main([str(root), str(tmp_path / "o")]) == 1
    assert "no drum_tab" in caplog.text


# ── .sng ────────────────────────────────────────────────────────────────────

def test_sng_unmask_matches_reference_and_extract(tmp_path):
    mask = bytes(range(40, 56))
    data = bytes((i * 37) % 251 for i in range(1000))
    assert sngfile.unmask(_mask_bytes(data, mask), mask) == data
    assert sngfile.unmask(b"", mask) == b""
    sng = write_sng(tmp_path / "s.sng", {"notes.chart": b"[Song]", "../evil.ogg": b"x", "video.mp4": b"v"},
                    {"name": "N", "artist": "A"}, mask=mask)
    meta, files = sngfile.read_sng(sng)
    assert meta["name"] == "N" and set(files) == {"notes.chart", "../evil.ogg", "video.mp4"}
    out = sngfile.extract_sng(sng, tmp_path / "x")
    assert sorted(p.name for p in out.iterdir()) == ["evil.ogg", "notes.chart", "song.ini"]
    assert (out / "notes.chart").read_bytes() == b"[Song]"
    assert drumchart.load_song_ini(out / "song.ini") == {"name": "N", "artist": "A"}


def test_sng_rejects_garbage(tmp_path):
    p = tmp_path / "bad.sng"
    p.write_bytes(b"NOTSNG" + bytes(40))
    with pytest.raises(sngfile.SngError):
        sngfile.read_sng(p)
    p.write_bytes(b"SNGPKG" + (1).to_bytes(4, "little") + bytes(10))
    with pytest.raises(sngfile.SngError, match="truncated"):
        sngfile.read_sng(p)


# ── five-fret parts ─────────────────────────────────────────────────────────

def test_export_five_fret_only_pack(tmp_path):
    """A pack with a five_fret part and no drums exports PART BASS (no PART DRUMS), with a
    forced strum / HOPO / tap per note and the authored level only."""
    import fivefretchart
    root = tmp_path / "b.sloppak"
    (root / "stems").mkdir(parents=True)
    (root / "stems" / "full.ogg").write_bytes(FAKE_OGG)
    beats = [{"time": 0.5 * i, "measure": i // 4 + 1 if i % 4 == 0 else -1} for i in range(16)]
    notes = [{"t": 0.5, "f": 1}, {"t": 0.625, "f": 2, "h": True}, {"t": 1.0, "f": 6, "l": 0.5},
             {"t": 2.0, "f": 0}, {"t": 2.5, "f": 4, "tap": True}]
    ff = {"version": 1, "name": "Bass (5-fret)", "instrument": "bass", "source": {"kind": "chart"},
          "notes": notes, "star_power": [{"start_time": 0.5, "end_time": 1.5}],
          "levels": {"hard": {"notes": [{"t": 0.5, "f": 1}, {"t": 0.625, "f": 2, "tap": True}]},
                     "easy": {"notes": [{"t": 0.5, "f": 1}], "generated": True}}}
    (root / "five_fret_bass.json").write_text(json.dumps(ff))
    (root / "song_timeline.json").write_text(json.dumps({"version": 1, "beats": beats, "sections": []}))
    man = {"feedpak_version": "1.21.0", "title": "B", "artist": "Band", "duration": 8.0,
           "stems": [{"id": "full", "file": "stems/full.ogg", "default": True}],
           "arrangements": [{"id": "five_fret_bass", "name": "Bass (5-fret)", "type": "five_fret",
                             "instrument": "bass", "five_fret": "five_fret_bass.json"}],
           "song_timeline": "song_timeline.json"}
    (root / "manifest.yaml").write_text(yaml.safe_dump(man, sort_keys=False))
    s = sloppak_to_ch.export(root, tmp_path / "o")
    assert s["parts"] == {"PART BASS": 5}
    mid = mido.MidiFile(str(tmp_path / "o" / "notes.mid"))
    assert "PART DRUMS" not in [t.name for t in mid.tracks]
    ons = _ons(next(t for t in mid.tracks if t.name == "PART BASS"))
    assert {(TPQ, 96), (TPQ, 102), (TPQ * 5 // 4, 97), (TPQ * 5 // 4, 101), (TPQ * 4, 95),
            (TPQ * 4, 102), (TPQ * 5, 98), (TPQ * 5, 104), (TPQ, 116), (TPQ, 84), (TPQ, 90)} <= \
        {(t, n) for t, n, _ in ons}
    assert not {n for _, n, _ in ons} & {60, 66}                          # generated Easy not written
    back, _ = fivefretchart.load_song_folder(tmp_path / "o")
    assert [fivefretchart.note_json(n) for n in back["bass"].notes] == notes
    assert set(back["bass"].levels) == {"hard"}
    # a Hard-only tap goes out as a Hard Phase Shift tap phrase, not 104 (which would tap Expert too)
    assert [fivefretchart.note_json(n) for n in back["bass"].levels["hard"]] == ff["levels"]["hard"]["notes"]
