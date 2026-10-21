"""Audio side of lib/fivefretjoin.py: placing real 5-fret charts on a song's stems, validating
them and joining them into a pack. mattromano custom build.

Synthetic charts + plucked audio with known offsets and tempo warps. Needs the offline-tool
dependencies (librosa, scipy, soundfile) and ffmpeg; skipped without them.
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
import drumjoin  # noqa: E402
import fivefret  # noqa: E402
import fivefretchart  # noqa: E402
import fivefretjoin  # noqa: E402
from tests.drum_fixtures import write_ini, write_mid  # noqa: E402
from tests.drumjoin_fixtures import (drum_pattern, ffmpeg, five_fret_notes, guitar_audio,  # noqa: E402
                                     make_sloppak, write_audio)

pytestmark = pytest.mark.skipif(ffmpeg() is None, reason="ffmpeg not available")

N_MEASURES = 30          # 60 s at 120 BPM
DUR = N_MEASURES * 2 + 3
TH = {"min_within_30ms": 0.8, "max_abs_median_ms": 25.0, "max_drift_span_ms": 40.0, "min_beat_margin": 0.1}
SOURCE = {"kind": "chart", "origin": "local"}


def _hard(notes):
    return [(t, n - 12, v, ln) for t, n, v, ln in notes]


def _chart(tmp_path, *, audio=("guitar", "bass"), seed=1, bass_seed=2, bass_hard=False, drums_from=None,
           rhythm=False, charter="Tester"):
    """Chart folder with PART GUITAR + PART BASS (+ PART RHYTHM / PART DRUMS); audio roles are
    written from the parsed notes in chart time ("song" = guitar + bass mixed)."""
    folder = tmp_path / "Band - Song"
    folder.mkdir()
    g = five_fret_notes(N_MEASURES, seed=seed)
    b = five_fret_notes(N_MEASURES, seed=bass_seed)
    extra = [("PART BASS", b + (_hard(b) if bass_hard else []), [])]
    if rhythm:
        extra.append(("PART RHYTHM", five_fret_notes(N_MEASURES, seed=seed + 50), []))
    if drums_from is not None:
        extra.append(("PART DRUMS", [n for n in drum_pattern(N_MEASURES, seed=3) if n[0] >= drums_from], []))
    write_mid(folder / "notes.mid", g, track_name="PART GUITAR", extra_tracks=extra)
    write_ini(folder / "song.ini", name="Song", artist="Band", charter=charter)
    charts, _ = fivefretchart.load_song_folder(folder)
    ys = {p: guitar_audio(_tf(charts[p].notes), DUR, seed=11 + i) for i, p in enumerate(("guitar", "bass"))}
    for role in audio:
        if role == "song":
            write_audio(folder / "song.ogg", ys["guitar"] + ys["bass"])
        elif role == "drums":
            write_audio(folder / "drums.ogg", ys["guitar"])     # any audio: a drums-only folder
        else:
            write_audio(folder / f"{role}.ogg", ys[role])
    return folder, charts


def _tf(notes, warp=lambda t: t):
    return [(warp(n.time), n.frets) for n in notes]


def _ours(tmp_path, guitar_notes, bass_notes=None, *, full_only=False, name="ours.sloppak"):
    """Our pack: guitar / bass stems from (time, frets) on our timeline (other seeds + noise)."""
    g = guitar_audio(guitar_notes, DUR + 4, seed=5, noise=0.005)
    b = guitar_audio(bass_notes, DUR + 4, seed=6, noise=0.005) if bass_notes is not None else None
    if full_only:
        stems = {"full": g + (b if b is not None else 0)}
    else:
        stems = {"guitar": g, **({"bass": b} if b is not None else {})}
    return make_sloppak(tmp_path / name, stems)


def _read(pack):
    z = zipfile.ZipFile(pack)
    names = set(z.namelist())
    man = yaml.safe_load(z.read("manifest.yaml"))
    docs = {n: json.loads(z.read(n)) for n in names if n.startswith("five_fret_") and n != fivefretjoin.JOIN_INFO}
    return man, docs


def _join(pack, folder, parts=("guitar",), **kw):
    kw.setdefault("thresholds", TH)
    kw.setdefault("source", SOURCE)
    kw.setdefault("log", lambda *a: None)
    return fivefretjoin.join_parts(pack, folder, list(parts), **kw)


# ── placement ───────────────────────────────────────────────────────────────

def _mini_chart():
    tempo = drumchart.TempoMap(480)
    notes = [fivefretchart.FretNote(0.5, 1, tick=480), fivefretchart.FretNote(2.0, 2, length=1.0, tick=1920)]
    return fivefretchart.FiveFretChart("guitar", notes, tempo, star_power=[(0.2, 0.8), (1.8, 3.2)],
                                       levels={"hard": [fivefretchart.FretNote(0.5, 1), fivefretchart.FretNote(2.0, 2)]})


def test_place_retimes_sustains_and_drops_negative():
    c = _mini_chart()
    placed = fivefretjoin.place(c, lambda t: t - 1.0)
    assert [(n.time, n.length) for n in placed.notes] == [(1.0, 1.0)]
    assert [n.time for n in placed.levels["hard"]] == [1.0]
    assert placed.star_power == [(0.8, 2.2)]
    assert [n.time for n in c.notes] == [0.5, 2.0], "the source chart is untouched"
    assert all(type(n.time) is float and type(n.length) is float for n in placed.notes)
    data = fivefretchart.five_fret_json(placed, SOURCE)
    ok, why = fivefret.validate_five_fret(data)
    assert ok, why
    json.dumps(data)
    rate = fivefretjoin.place(c, fivefretjoin.warp_fn([[0.0, 0.3], [10.0, 11.3]]))     # rate 1.1
    assert rate.notes[1].time == pytest.approx(0.3 + 2.0 * 1.1, abs=1e-4)
    assert rate.notes[1].length == pytest.approx(1.1, abs=1e-4)


def test_chart_beats_in_seconds_with_offset():
    c = _mini_chart()
    c.offset = 0.25
    beats = fivefretjoin.chart_beats(c)
    assert beats[:3] == pytest.approx([0.25, 0.75, 1.25])
    assert len(beats) == 8            # through the measure holding the last note (tick 1920)


def test_chart_part_audio_roles(tmp_path):
    import soundfile as sf

    def folder(name, files):
        d = tmp_path / name
        d.mkdir()
        for f in files:
            sf.write(str(d / f), np.zeros(2205, np.float32), 22050)
        return d
    a = folder("a", ["rhythm.wav", "drums.wav", "song.wav"])
    assert fivefretjoin.chart_part_audio(a, "bass", chart_parts={"guitar", "bass"})[1] == "rhythm"
    assert fivefretjoin.chart_part_audio(a, "bass", chart_parts={"guitar", "bass", "rhythm"})[1] == "mix"
    assert fivefretjoin.chart_part_audio(a, "guitar", chart_parts={"guitar"})[1] == "mix"
    assert fivefretjoin.chart_part_audio(a, "rhythm", chart_parts={"rhythm"})[1] == "rhythm"
    b = folder("b", ["drums_1.wav", "drums_2.wav", "crowd.wav"])     # BCR4: drums-only -> no audio
    assert fivefretjoin.chart_part_audio(b, "guitar", chart_parts={"guitar"}) == (None, "none")
    c = folder("c", ["guitar.wav", "song.wav"])
    y, kind = fivefretjoin.chart_part_audio(c, "guitar", chart_parts={"guitar"})
    assert kind == "guitar" and len(y) == 2205


# ── fresh alignment ─────────────────────────────────────────────────────────

def test_fresh_offset_recovered(tmp_path):
    folder, charts = _chart(tmp_path)
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + 1.5))
    r = _join(pack, folder)["parts"]["guitar"]
    assert r["ok"], r
    assert r["method"] == "offset" and not r["reused_drums"]
    assert r["offset"] == pytest.approx(1.5, abs=0.01)
    assert r["within_30ms"] >= 0.9
    assert r["chart_audio"] == "guitar"


def test_fresh_mix_when_no_part_stem(tmp_path):
    folder, charts = _chart(tmp_path, audio=("song",))
    sh = lambda t: t + 0.8  # noqa: E731
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, sh), _tf(charts["bass"].notes, sh), full_only=True)
    r = _join(pack, folder)["parts"]["guitar"]
    assert r["chart_audio"] == "mix"
    assert r["offset"] == pytest.approx(0.8, abs=0.01)
    assert r["ok"], r


@pytest.mark.parametrize("audio", [(), ("drums",)])
def test_fresh_synthetic_when_chart_has_no_audio(tmp_path, audio):
    folder, charts = _chart(tmp_path, audio=audio)
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + 3.1))
    r = _join(pack, folder)["parts"]["guitar"]
    assert r["chart_audio"] == "synthetic"
    assert r["offset"] == pytest.approx(3.1, abs=0.01)
    assert r["ok"], r


def test_tempo_drift_uses_warp(tmp_path):
    folder, charts = _chart(tmp_path)
    warp = lambda t: 0.7 + 1.02 * t  # noqa: E731
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, warp))
    r = _join(pack, folder, dry_run=False)["parts"]["guitar"]
    assert r["method"] == "warp"
    assert r["ok"], r
    _, docs = _read(pack)
    got = np.array([n["t"] for n in docs["five_fret_guitar.json"]["notes"]])
    want = np.array(sorted(round(warp(n.time), 3) for n in charts["guitar"].notes))
    assert len(got) == len(want) and np.median(np.abs(got - want)) < 0.02


# ── reuse of the drum join's warp ───────────────────────────────────────────

def test_reused_warp_used_and_recorded(tmp_path, monkeypatch):
    folder, charts = _chart(tmp_path)
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + 1.5))

    def no_env(*a, **k):
        raise AssertionError("the reuse path must not correlate audio")
    monkeypatch.setattr(drumalign, "onset_env", no_env)
    r = _join(pack, folder, reuse={"folder": str(folder), "warp": {"offset": 1.5}}, dry_run=False)
    g = r["parts"]["guitar"]
    assert g["ok"] and g["reused_drums"], g
    assert g["method"] == "offset" and g["offset"] == pytest.approx(1.5)
    info = json.loads(zipfile.ZipFile(pack).read(fivefretjoin.JOIN_INFO))
    assert info["guitar"]["reused_drums"] is True
    assert info["guitar"]["detail"]["warp"] == {"offset": 1.5}
    al = info["guitar"]["detail"]["alignment"]
    assert set(al) == set(drumalign.Alignment("offset", 0.0, 0.0, 0.0).summary()), "one shape with fresh joins"
    assert al["method"] == "offset" and al["offset"] == pytest.approx(1.5)


def test_onsets_detected_once_per_stem_role(tmp_path, monkeypatch):
    folder, charts = _chart(tmp_path, rhythm=True)
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + 1.5))
    calls = []
    real = drumalign.detect_onsets
    monkeypatch.setattr(drumalign, "detect_onsets", lambda y, *a, **k: calls.append(1) or real(y, *a, **k))
    r = _join(pack, folder, parts=("guitar", "rhythm"), reuse={"folder": str(folder), "warp": {"offset": 1.5}})
    assert set(r["parts"]) == {"guitar", "rhythm"}
    assert len(calls) == 1, "guitar and rhythm share our guitar stem"


def test_bad_reuse_falls_back_to_fresh(tmp_path):
    folder, charts = _chart(tmp_path)
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + 1.5))
    r = _join(pack, folder, reuse={"folder": str(folder), "warp": {"offset": 3.0}})["parts"]["guitar"]
    assert r["ok"] and not r["reused_drums"], r
    assert r["offset"] == pytest.approx(1.5, abs=0.01)


def test_reused_warp_clamped_before_first_drum_hit(tmp_path):
    first = 4 * 1920                     # drums start at measure 5 (8 s); guitar from 0 s
    folder, charts = _chart(tmp_path, drums_from=first)
    hits = drumchart.load_song_folder(folder, levels=False)[0].hits
    h0, h1 = hits[0].time, hits[-1].time
    assert h0 == pytest.approx(8.0)
    intro = [n for n in charts["guitar"].notes if n.time < h0]
    assert len(intro) >= 8
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + 1.5))
    # the drum warp's lag climbs from 0.3 s to the true 1.5 s before the first drum hit
    pairs = [[0.0, 0.3], [h0 / 2, h0 / 2 + 0.9], [h0, h0 + 1.5], [h1, h1 + 1.5]]
    r = _join(pack, folder, reuse={"folder": str(folder), "warp": pairs}, dry_run=False)["parts"]["guitar"]
    assert r["ok"] and r["reused_drums"] and r["clamped"], r
    _, docs = _read(pack)
    got = [n["t"] for n in docs["five_fret_guitar.json"]["notes"]]
    want = sorted({round(n.time + 1.5, 3) for n in charts["guitar"].notes})
    assert got[:len(intro)] == pytest.approx(want[:len(intro)], abs=0.002)
    info = json.loads(zipfile.ZipFile(pack).read(fivefretjoin.JOIN_INFO))
    assert info["guitar"]["detail"]["clamped"] is True


def test_reused_warp_intro_failing_forces_fresh(tmp_path):
    """BCR1 'either fails': the whole part passes on the reused warp but the notes before the first
    drum hit don't (our intro is synced differently) -> fresh alignment."""
    first = 4 * 1920
    folder, charts = _chart(tmp_path, drums_from=first)
    hits = drumchart.load_song_folder(folder, levels=False)[0].hits
    h0, h1 = hits[0].time, hits[-1].time
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + (1.62 if t < h0 else 1.5)))
    pairs = [[0.0, 1.5], [h0, h0 + 1.5], [h1, h1 + 1.5]]
    logs = []
    r = _join(pack, folder, reuse={"folder": str(folder), "warp": pairs}, log=logs.append)["parts"]["guitar"]
    assert any("before the first drum hit fail" in m for m in logs), logs
    assert not r["reused_drums"] and r["ok"], r
    assert r["offset"] == pytest.approx(1.5, abs=0.02)


# ── fresh alignment: the tempo-ratio scan ───────────────────────────────────

def test_rate_scan_only_for_a_real_tempo_ratio(tmp_path):
    folder, charts = _chart(tmp_path, audio=())
    notes = charts["guitar"].notes
    src = drumalign.onset_env(guitar_audio(_tf(notes), DUR, seed=11))

    def ref(w):
        return drumalign.onset_env(guitar_audio(_tf(notes, w), DUR + 6, seed=5, noise=0.005))
    assert fivefretjoin._rate_scan_lag(ref(lambda t: t + 1.5), src) is None
    cut = lambda t: t + 1.5 if t < 20 else t + 4.0  # noqa: E731   an edit at ratio 1.0
    assert fivefretjoin._rate_scan_lag(ref(cut), src) is None
    mid = src.shape[-1] / drumalign.FR / 2
    lag = fivefretjoin._rate_scan_lag(ref(lambda t: 0.7 + 1.02 * t), src)
    assert lag == pytest.approx(0.7 + 0.02 * mid, abs=0.05)


def test_vote_override_keeps_its_lag(tmp_path, monkeypatch):
    """global_offset overruling the whole-song peak with its window votes (confidence 0, ratio 1)
    is an edit, not a tempo ratio: no rate scan, the warp tracks around the voted lag."""
    folder, charts = _chart(tmp_path)
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + 1.5))
    monkeypatch.setattr(drumalign, "global_offset",
                        lambda ref, src, *a, **k: drumalign.Alignment("offset", 1.5, 0.0, 1.0, [], 0.0))

    def no_scan(*a, **k):
        raise AssertionError("rate scan after a vote override")
    monkeypatch.setattr(fivefretjoin, "_rate_scan_lag", no_scan)
    seen, real = [], drumalign.warp_pairs
    monkeypatch.setattr(drumalign, "warp_pairs", lambda r, s, b, h, base, **k: seen.append(base) or real(r, s, b, h, base, **k))
    r = _join(pack, folder)["parts"]["guitar"]
    assert seen == [1.5], r
    assert r["method"] == "warp" and r["ok"], r


def test_align_part_without_notes_is_a_clear_error(tmp_path):
    folder, charts = _chart(tmp_path, audio=())
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + 1.5))
    sp = drumjoin.SloppakFiles(pack)
    try:
        y, kind = fivefretjoin.our_part_audio(sp, "guitar")
        empty = fivefretchart.FiveFretChart("guitar", [], charts["guitar"].tempo)
        with pytest.raises(ValueError, match="no notes"):
            fivefretjoin.align_part(folder, sp, empty, "guitar", ref_y=y, ref_kind=kind, onsets=None,
                                    chart_parts={"guitar", "bass"}, log=lambda *a: None)
    finally:
        sp.close()


# ── validation ──────────────────────────────────────────────────────────────

def test_one_beat_off_flagged(tmp_path):
    folder, charts = _chart(tmp_path, audio=())
    times = np.array([n.time + 1.5 for n in charts["guitar"].notes])
    y = guitar_audio([(t, 1) for t in times], DUR + 4, seed=5, noise=0.005)
    onsets = drumalign.detect_onsets(y)
    good = fivefretjoin.validate_part(times, onsets, 0.5, TH)
    assert good.ok, good.reasons
    assert good.pads["beat_margin"] > 0.3
    off = fivefretjoin.validate_part(times + 0.5, onsets, 0.5, TH)
    assert not off.ok
    assert any(r.startswith("one beat off? margin") for r in off.reasons), off.reasons
    assert off.pads["beat_margin"] < 0.1


def test_wrong_song_flagged_not_written(tmp_path):
    folder, _ = _chart(tmp_path)
    other = fivefretchart.parse_mid(write_mid(tmp_path / "x.mid", five_fret_notes(N_MEASURES, seed=42),
                                              track_name="PART GUITAR"))
    other_b = fivefretchart.parse_mid(write_mid(tmp_path / "y.mid", five_fret_notes(N_MEASURES, seed=43),
                                                track_name="PART BASS"), part="bass")
    pack = _ours(tmp_path, _tf(other.notes, lambda t: t + 0.4), _tf(other_b.notes, lambda t: t + 0.4))
    before = pack.read_bytes()
    r = _join(pack, folder, parts=("guitar", "bass"), dry_run=False)
    assert set(r["parts"]) == {"guitar", "bass"}
    assert not any(p["ok"] for p in r["parts"].values()), r
    assert r["written"] == []
    assert pack.read_bytes() == before


def test_min_levels_filters_parts(tmp_path):
    folder, charts = _chart(tmp_path, bass_hard=True)
    assert list(charts["bass"].levels) == ["hard"]
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + 1.5))
    r = _join(pack, folder, parts=("guitar", "bass"), min_levels={"bass": 1},
              reuse={"folder": str(folder), "warp": {"offset": 1.5}})
    assert set(r["parts"]) == {"guitar"}
    r = _join(pack, folder, parts=("guitar", "bass"), min_levels={"bass": 0},
              reuse={"folder": str(folder), "warp": {"offset": 1.5}})
    assert set(r["parts"]) == {"guitar", "bass"} and r["parts"]["bass"]["levels"] == 1


def test_join_writes_passing_part_only(tmp_path):
    folder, charts = _chart(tmp_path)
    other_b = fivefretchart.parse_mid(write_mid(tmp_path / "y.mid", five_fret_notes(N_MEASURES, seed=77),
                                                track_name="PART BASS"), part="bass")
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + 1.5), _tf(other_b.notes, lambda t: t + 1.5))
    r = _join(pack, folder, parts=("guitar", "bass", "rhythm"), dry_run=False)
    assert set(r["parts"]) == {"guitar", "bass"}, "rhythm: the chart has none"
    assert r["parts"]["guitar"]["ok"] and not r["parts"]["bass"]["ok"], r
    assert r["written"] == ["guitar"]
    man, docs = _read(pack)
    assert set(docs) == {"five_fret_guitar.json"}
    doc = docs["five_fret_guitar.json"]
    assert doc["source"] == {"kind": "chart", "origin": "local", "charter": "Tester"}
    assert doc["name"] == "Guitar (5-fret)"
    assert set(doc["levels"]) == {"easy", "medium", "hard"}
    ok, why = fivefret.validate_five_fret(doc)
    assert ok, why
    assert [e["id"] for e in man["arrangements"] if e.get("type") == "five_fret"] == ["five_fret_guitar"]
    assert man["feedpak_version"] == fivefretjoin.FEEDPAK_VERSION
    assert not any(tmp_path.glob("*.sloppak.*")), "no backup when backup_dir is None"


def test_source_charter_falls_back_to_frets(tmp_path):
    folder, charts = _chart(tmp_path, charter="")
    with open(folder / "song.ini", "a", encoding="utf-8") as f:
        f.write("frets = Fretter\n")
    pack = _ours(tmp_path, _tf(charts["guitar"].notes, lambda t: t + 1.5))
    _join(pack, folder, source={"kind": "chart", "origin": "chorus", "md5": "c" * 32, "charter": None},
          reuse={"folder": str(folder), "warp": {"offset": 1.5}}, dry_run=False)
    _, docs = _read(pack)
    assert docs["five_fret_guitar.json"]["source"] == {"kind": "chart", "origin": "chorus", "md5": "c" * 32,
                                                       "charter": "Fretter"}
