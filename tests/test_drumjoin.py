"""Drum-chart joiner (lib/drumalign.py, lib/drumjoin.py) on synthetic charts + audio with known
offsets and tempo warps. mattromano custom build, ported from the Slopsmith 0.2.9 tools; the join
now writes a feedpak drum tab instead of a guitar-style Drums arrangement.

Needs the offline-tool dependencies (librosa, scipy, soundfile) and ffmpeg; skipped without them.
"""
import json
import zipfile

import pytest

pytest.importorskip("librosa")
pytest.importorskip("scipy")
pytest.importorskip("soundfile")

import numpy as np  # noqa: E402
import yaml  # noqa: E402

import drumalign  # noqa: E402
import drumchart  # noqa: E402
import drumchart_gen  # noqa: E402
import drumjoin  # noqa: E402
import drums as drums_mod  # noqa: E402
from tests.drum_fixtures import write_ini, write_mid  # noqa: E402
from tests.drumjoin_fixtures import (click_track, drum_audio, drum_pattern, ffmpeg, make_gp5,  # noqa: E402
                                     make_sloppak, write_audio)

pytestmark = pytest.mark.skipif(ffmpeg() is None, reason="ffmpeg not available")

N_MEASURES = 30          # 60 s at 120 BPM


def _chart_folder(tmp_path, *, delay=0, audio="drums", seed=1, name="Song", artist="Band"):
    folder = tmp_path / f"{artist} - {name}"
    folder.mkdir()
    notes = drum_pattern(N_MEASURES, seed=seed) + [(4 * 1920, 116, 100, 1920), (12 * 1920, 116, 100, 1920)]
    write_mid(folder / "notes.mid", notes, sections=[(0, "Intro"), (8 * 1920, "Verse")])
    write_ini(folder / "song.ini", name=name, artist=artist, delay=delay)
    chart, _ = drumchart.load_song_folder(folder)
    hits = [(h.time, h.pad, h.cymbal) for h in chart.hits]   # chart-audio time (delay applied)
    if audio == "drums":
        write_audio(folder / "drums.ogg", drum_audio(hits, N_MEASURES * 2 + 3, seed=11))
    elif audio == "mix":
        y = drum_audio(hits, N_MEASURES * 2 + 3, seed=11) + click_track(
            np.arange(0.3, N_MEASURES * 2, 0.77), N_MEASURES * 2 + 3, freq=[220, 330, 440], seed=3)
        write_audio(folder / "song.ogg", y)
    return folder, chart


def _our_sloppak(tmp_path, hits, *, dur=None, name="ours.sloppak", other=None, **kw):
    """hits: DrumHits / (time, pad, cymbal) tuples / bare times, already on our timeline."""
    hits = [(h.time, h.pad, h.cymbal) if hasattr(h, "pad") else (h if isinstance(h, tuple) else (h, "red", False))
            for h in hits]
    dur = dur or (max(h[0] for h in hits) + 4)
    stems = {"drums": drum_audio(hits, dur, seed=99, noise=0.005)}
    if other is not None:
        stems["other"] = other
    beats = [{"time": round(0.5 * i, 3), "measure": i // 4 + 1 if i % 4 == 0 else -1} for i in range(int(dur * 2))]
    return make_sloppak(tmp_path / name, stems, beats=beats, **kw)


def _moved(h, t):
    return (float(t), h.pad, h.cymbal)


def _read(sp):
    """(manifest, drum_tab or None, drum_join.json or None) of a zip or directory sloppak."""
    if sp.is_dir():
        names = {p.relative_to(sp).as_posix() for p in sp.rglob("*") if p.is_file()}
        read = lambda rel: (sp / rel).read_bytes()  # noqa: E731
    else:
        z = zipfile.ZipFile(sp)
        names = set(z.namelist())
        read = z.read
    man = yaml.safe_load(read("manifest.yaml"))
    tab = json.loads(read("drum_tab.json")) if "drum_tab.json" in names else None
    info = json.loads(read("drum_join.json")) if "drum_join.json" in names else None
    return man, tab, info


def _hit_times(tab):
    return sorted(h["t"] for h in tab["hits"])


# ── xcorr / validation primitives ───────────────────────────────────────────

def test_xcorr_recovers_lag_subframe():
    t = np.sort(np.random.default_rng(0).uniform(1, 40, 120))
    a = drumalign.onset_env(click_track(t, 50))
    b = drumalign.onset_env(click_track(t + 2.345, 50, seed=5))
    lag, conf, ratio = drumalign.xcorr(b, a, 10)
    assert lag == pytest.approx(2.345, abs=0.006)
    assert conf > 10 and ratio > 1.05


def test_validate_reports_offset_and_drift():
    nt = np.arange(0, 60, 0.25)
    good = drumalign.validate(nt, nt + 0.008)
    assert good.ok and good.within_30ms == 1.0 and good.median_offset == pytest.approx(0.008, abs=1e-6)
    late = drumalign.validate(nt, nt + 0.06)
    assert not late.ok and late.within_30ms == 0.0 and late.within_30ms_centered == 1.0
    drift = drumalign.validate(nt, nt + nt * 0.0012)            # 72 ms over a minute
    assert not drift.ok and any("drifts" in r for r in drift.reasons)
    assert drift.drift_ms_per_min == pytest.approx(72, abs=2)


# ── source a: charts ────────────────────────────────────────────────────────

def test_join_chart_with_known_offset(tmp_path):
    folder, chart = _chart_folder(tmp_path)
    true_off = 1.237
    sp = _our_sloppak(tmp_path, [_moved(h, h.time + true_off) for h in chart.hits])
    rep = drumjoin.join(sp, chart_folder=folder, sources=("chart",), backup_dir=tmp_path / "bak")
    cand = rep["candidates"][0]
    assert rep["written"] == "chart"
    assert cand["alignment"]["method"] == "offset"
    assert cand["alignment"]["offset"] == pytest.approx(true_off, abs=0.006)
    assert cand["validation"]["within_30ms"] > 0.9
    man, tab, info = _read(sp)
    # feedpak 1.20.0 shape: a type: drums entry + the song-level drum_tab alias, no wire notes
    assert [a["name"] for a in man["arrangements"]] == ["Lead", "Drums"]
    assert man["arrangements"][1] == {"id": "drums", "name": "Drums", "type": "drums", "drum_tab": "drum_tab.json"}
    assert man["drum_tab"] == "drum_tab.json" and man["feedpak_version"] == "1.20.0"
    assert not any(k.startswith("x_drums") for k in man), "provenance lives in drum_join.json"
    ok, why = drums_mod.validate_drum_tab(tab)
    assert ok, why
    assert tab["star_power"][0]["start_time"] == pytest.approx(4 * 2 + true_off, abs=0.01)
    assert tab["fills"], "YARG-style activation fills generated from star power"
    assert set(tab["levels"]) == {"easy", "medium", "hard"}
    got = _hit_times(tab)
    want = sorted(round(h.time + true_off, 3) for h in chart.hits)
    assert len(got) == len(want)
    assert np.max(np.abs(np.array(got) - np.array(want))) < 0.01
    assert info["source"] == "chart" and not info["forced"] and info["validation"]["ok"]
    assert list((tmp_path / "bak").glob("ours.*.sloppak")), "original backed up"


def test_joined_pack_loads_as_a_drum_part(tmp_path):
    import sloppak
    folder, chart = _chart_folder(tmp_path)
    lib = tmp_path / "lib"
    lib.mkdir()
    sp = _our_sloppak(lib, [_moved(h, h.time + 0.4) for h in chart.hits], zip_form=False)
    assert drumjoin.join(sp, chart_folder=folder, sources=("chart",))["written"] == "chart"
    loaded = sloppak.load_song(sp.name, lib, tmp_path / "cache")
    assert loaded.drum_tab is not None and len(loaded.drum_tab["hits"]) == len(chart.hits)
    assert [p["id"] for p in loaded.drum_parts] == ["drums"]
    assert [a.name for a in loaded.song.arrangements] == ["Lead"]   # drums never become a fretted part


def test_join_honours_song_ini_delay(tmp_path):
    folder, chart = _chart_folder(tmp_path, delay=300)
    assert chart.offset == pytest.approx(0.3)
    sp = _our_sloppak(tmp_path, [_moved(h, h.time + 0.5) for h in chart.hits])
    rep = drumjoin.join(sp, chart_folder=folder, sources=("chart",))
    assert rep["written"] == "chart"
    assert rep["candidates"][0]["alignment"]["offset"] == pytest.approx(0.5, abs=0.006)
    _, tab, _ = _read(sp)
    assert _hit_times(tab)[0] == pytest.approx(chart.hits[0].time + 0.5, abs=0.01)  # chart 0 + delay + offset


def test_join_full_mix_when_chart_has_no_drum_stem(tmp_path):
    folder, chart = _chart_folder(tmp_path, audio="mix")
    other = click_track(np.arange(0.3, N_MEASURES * 2, 0.77) + 0.8, N_MEASURES * 2 + 4, freq=[220, 330, 440],
                        seed=3)
    sp = _our_sloppak(tmp_path, [_moved(h, h.time + 0.8) for h in chart.hits], other=other)
    rep = drumjoin.join(sp, chart_folder=folder, sources=("chart",))
    c = rep["candidates"][0]
    assert c["chart_audio"] == "mix"
    assert c["alignment"]["offset"] == pytest.approx(0.8, abs=0.01)
    assert rep["written"] == "chart"


def test_join_synthetic_envelope_when_chart_has_no_audio(tmp_path):
    folder, chart = _chart_folder(tmp_path, audio=None)
    sp = _our_sloppak(tmp_path, [_moved(h, h.time + 3.1) for h in chart.hits])
    rep = drumjoin.join(sp, chart_folder=folder, sources=("chart",))
    c = rep["candidates"][0]
    assert c["chart_audio"] == "synthetic"
    assert c["alignment"]["offset"] == pytest.approx(3.1, abs=0.01)
    assert rep["written"] == "chart"


def test_join_tempo_warp_falls_back_to_beat_warp(tmp_path):
    folder, chart = _chart_folder(tmp_path)

    def warp(t):          # a different master: 1.5% slower plus a wobble
        return 0.7 + 1.015 * t + 0.12 * np.sin(2 * np.pi * t / 37)
    sp = _our_sloppak(tmp_path, [_moved(h, warp(h.time)) for h in chart.hits])
    rep = drumjoin.join(sp, chart_folder=folder, sources=("chart",))
    c = rep["candidates"][0]
    assert c["alignment"]["method"] == "warp"
    assert c["validation"]["within_30ms"] > 0.85, c["validation"]
    assert rep["written"] == "chart"
    _, tab, _ = _read(sp)
    got = np.array(_hit_times(tab))
    want = np.array(sorted(warp(h.time) for h in chart.hits))
    assert np.median(np.abs(got - want)) < 0.02


def test_offset_only_on_warped_audio_is_flagged(tmp_path):
    folder, chart = _chart_folder(tmp_path)
    sp = _our_sloppak(tmp_path, [_moved(h, 0.7 + 1.015 * h.time) for h in chart.hits])
    rep = drumjoin.join(sp, chart_folder=folder, sources=("chart",), force_method="offset")
    v = rep["candidates"][0]["validation"]
    assert not v["ok"] and rep["written"] is None
    man, tab, _ = _read(sp)
    assert tab is None and [a["name"] for a in man["arrangements"]] == ["Lead"]


def test_wrong_song_is_flagged_not_written_unless_forced(tmp_path):
    folder, chart = _chart_folder(tmp_path)
    other_song = drumchart.parse_mid(write_mid(tmp_path / "x.mid", drum_pattern(N_MEASURES, seed=42)))
    sp = _our_sloppak(tmp_path, [_moved(h, h.time * 0.93 + 0.4) for h in other_song.hits])
    rep = drumjoin.join(sp, chart_folder=folder, sources=("chart",))
    assert rep["written"] is None
    assert not rep["candidates"][0]["validation"]["ok"]
    assert _read(sp)[1] is None
    rep = drumjoin.join(sp, chart_folder=folder, sources=("chart",), force=True)
    assert rep["written"] == "chart"
    _, tab, info = _read(sp)
    assert info["forced"] and tab is not None


def test_rejoin_replaces_existing_drums(tmp_path):
    folder, chart = _chart_folder(tmp_path)
    sp = _our_sloppak(tmp_path, [_moved(h, h.time + 1.0) for h in chart.hits], zip_form=False)   # directory form too
    for _ in range(2):
        assert drumjoin.join(sp, chart_folder=folder, sources=("chart",))["written"] == "chart"
    man = yaml.safe_load((sp / "manifest.yaml").read_text())
    assert [a["name"] for a in man["arrangements"]] == ["Lead", "Drums"]
    assert sorted(p.name for p in (sp / "arrangements").iterdir()) == ["lead.json"]


def test_join_replaces_a_legacy_drums_arrangement(tmp_path):
    """Packs joined by the 0.2.9 tools carry a guitar-style Drums arrangement: it goes."""
    folder, chart = _chart_folder(tmp_path)
    legacy = ({"id": "drums", "name": "Drums", "file": "arrangements/drums.json", "tuning": [0] * 6, "capo": 0},
              {"name": "Drums", "notes": [{"t": 1.0, "s": 1, "f": 12}], "drums": {"levels": {}}})
    sp = _our_sloppak(tmp_path, [_moved(h, h.time + 0.6) for h in chart.hits], extra_arrangements=[legacy])
    assert drumjoin.join(sp, chart_folder=folder, sources=("chart",))["written"] == "chart"
    man, tab, _ = _read(sp)
    assert [a.get("type") for a in man["arrangements"]] == [None, "drums"]
    assert "arrangements/drums.json" not in zipfile.ZipFile(sp).namelist()
    assert tab and len(tab["hits"]) == len(chart.hits)


# ── fuzzy matching ──────────────────────────────────────────────────────────

def test_fuzzy_match_against_chart_dir(tmp_path):
    root = tmp_path / "charts"
    for artist, name in [("The Dirty Nil", "Doom Boy"), ("Prince Daddy & The Hyena", "Lauren (Track 2)"),
                         ("Dirty Nil", "Possession"), ("Other Band", "Doom Bay")]:
        d = root / f"{artist} - {name}"
        d.mkdir(parents=True)
        write_ini(d / "song.ini", name=name, artist=artist)
    (root / "Folder Only - Song Title").mkdir()
    (root / "Folder Only - Song Title" / "notes.chart").write_text("")
    idx = drumjoin.index_chart_dir(root)
    assert len(idx) == 5
    e, s = drumjoin.best_match(idx, "Dirty Nil", "Doom Boy (Songsterr)")
    assert e.title == "Doom Boy" and e.artist == "The Dirty Nil" and s > 0.9
    e, _ = drumjoin.best_match(idx, "Prince Daddy and the Hyena", "Lauren (Track 2)")
    assert e.title == "Lauren (Track 2)"
    e, _ = drumjoin.best_match(idx, "folder only", "song title")
    assert e.title == "Song Title"
    e, s = drumjoin.best_match(idx, "Nobody", "Completely Different")
    assert e is None and s < 0.82


# ── source b: GP drum track ─────────────────────────────────────────────────

def _gp_slots(m, slot):
    out = [36] if slot % 4 == 0 else []
    if slot in (2, 6):
        out.append(38)
    if (m * 8 + slot) % 3 != 1:
        out.append(42)
    if slot == 0 and m % 4 == 0:
        out.append(49)
    return out


def test_gp_drum_track_placed_with_stored_sync_map(tmp_path):
    n = 24
    gp = make_gp5(tmp_path / "tab.gp5", n, _gp_slots)
    tab_beats = [0.5 * i for i in range(n * 4 + 1)]
    audio_beats = [1.3 + 0.52 * i for i in range(n * 4 + 1)]       # band ~4% slower, 1.3 s intro
    expect = []
    for m in range(n):
        for slot in range(8):
            if _gp_slots(m, slot):
                expect.append(1.3 + 0.52 * (m * 4 + slot / 2))
    sp = _our_sloppak(tmp_path, expect, x_build={"tool": "gp_to_sloppak", "gp": str(gp)},
                      x_sync={"tab_beats": tab_beats, "audio_beats": audio_beats, "report": {"method": "beat-dtw"}})
    rep = drumjoin.join(sp, sources=("chart", "gp"))
    assert rep["written"] == "gp"
    c = rep["candidates"][0]
    assert c["track_name"] == "Drums" and c["sync_method"] == "beat-dtw"
    _, tab, info = _read(sp)
    assert {h["p"] for h in tab["hits"]} == {drums_mod.midi_to_piece(g) for g in (36, 38, 42, 49)}
    assert _hit_times(tab)[0] == pytest.approx(1.3, abs=0.005)
    assert tab["star_power"], "auto star power for sources without it"
    assert info["source"] == "gp" and info["detail"]["track_name"] == "Drums"


def test_no_gp_tab_is_skipped_cleanly(tmp_path):
    sp = _our_sloppak(tmp_path, list(np.arange(1, 20, 0.5)))
    lines = []
    rep = drumjoin.join(sp, sources=("gp",), log=lines.append)
    assert rep["written"] is None and rep["candidates"] == []
    assert any("no GP tab" in str(x) for x in lines)


def test_chart_preferred_over_gp_and_gp_used_when_chart_fails(tmp_path):
    folder, chart = _chart_folder(tmp_path)
    gp = make_gp5(tmp_path / "tab.gp5", 4, _gp_slots)
    sp = _our_sloppak(tmp_path, [_moved(h, h.time + 0.25) for h in chart.hits], x_build={"gp": str(gp)},
                      x_sync={"tab_beats": [0, 1], "audio_beats": [0, 1]})
    rep = drumjoin.join(sp, chart_folder=folder)
    assert rep["written"] == "chart" and len(rep["candidates"]) == 1


# ── transcription hook ──────────────────────────────────────────────────────

def test_transcriber_hook(tmp_path):
    times = list(np.arange(1, 50, 0.37))
    sp = _our_sloppak(tmp_path, times)
    drumjoin.register_transcriber("fake", lambda path, sr: [drumchart.DrumHit(t, "red") for t in times])
    rep = drumjoin.join(sp, sources=(), transcriber="fake")
    assert rep["written"] == "transcribe:fake"
    rep = drumjoin.join(sp, sources=(), transcriber="missing")
    assert rep["written"] is None


# ── generators ──────────────────────────────────────────────────────────────

def test_auto_star_power_and_fills():
    beats = [(0.5 * i, i // 4 + 1 if i % 4 == 0 else -1) for i in range(400)]
    notes = list(np.arange(0, 200, 0.25))
    sp = drumjoin.auto_star_power(notes, beats)
    assert sp[0] == (8.0, 9.999) and sp[1][0] == pytest.approx(24.0)
    fills = drumchart_gen.auto_fills(notes, beats, sp, sections=[0.0])
    assert fills and all(b - a == pytest.approx(2.0) for a, b in fills)
    assert fills[0][1] - sp[0][1] >= 2.0
    assert not any(a < e and b > s for a, b in fills for s, e in sp)   # never inside a SP phrase
    assert drumchart_gen.auto_fills(notes, beats, []) == []


def test_pad_check_catches_a_chart_one_eighth_off(tmp_path):
    # hats on every 8th, kick on 1 and 3: shifting the whole chart by an 8th still puts
    # every note on *some* onset, but the kicks land on hat-only slots
    hits = []
    for i in range(240):
        t = 1.0 + 0.25 * i
        hits.append((t, "yellow", True))
        if i % 4 == 0:
            hits.append((t, "kick", False))
        if i % 4 == 2:
            hits.append((t, "red", False))
    y = drum_audio(hits, 64, seed=3, noise=0.003)
    on, bands = drumalign.detect_onsets(y), drumalign.band_onsets(y)
    good = drumalign.validate([h[0] for h in hits], on, pad_hits=hits, bands=bands)
    assert good.ok and good.pads["kick"] > 0.9 and good.pads["cymbal"] > 0.9
    off = [(t + 0.25, p, c) for t, p, c in hits]
    bad = drumalign.validate([h[0] for h in off], on, pad_hits=off, bands=bands)
    assert bad.within_30ms > 0.9            # the plain onset check is fooled...
    assert not bad.ok and bad.pads["kick"] < 0.5 and any("kick" in r for r in bad.reasons)   # ...this isn't
