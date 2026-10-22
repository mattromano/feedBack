"""scripts/drums_library.py --parts ... --from-drums: the only candidate is the chart each song's
drums were joined from, found in the local cache (mattromano custom build). In-process (inline
executor), caches in tmp_path, no network at all: chorus.search / chorus.fetch raise. Most tests
fake join_parts; the end-to-end one runs the real join (needs librosa, scipy, soundfile, ffmpeg)."""
import sys
from pathlib import Path

import pytest

import chorus
import drumjoin
import fivefret
import fivefretjoin
from tests.drum_fixtures import write_ini, write_mid
from tests.test_drums_library_five_fret import (FakeJoin, _doc, _members, _pack, _rows, _run,  # noqa: F401
                                                 env, needs_audio)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import drums_library  # noqa: E402

MD5_A = "a" * 32
MD5_B = "b" * 32
WARP = [[0.0, 0.7], [50.0, 50.8], [100.0, 100.9]]
TRACKS = {"guitar": "PART GUITAR", "rhythm": "PART RHYTHM", "bass": "PART BASS"}


@pytest.fixture
def offline(monkeypatch):
    """Any Chorus call fails the test."""
    def boom(*a, **k):
        raise AssertionError("--from-drums must not touch Chorus")
    monkeypatch.setattr(chorus, "search", boom)
    monkeypatch.setattr(chorus, "fetch", boom)


def _chart(folder, parts=("guitar", "bass"), notes=None):
    """A cached chart folder (drum-join style: notes.mid + song.ini, drums audio only)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    notes = notes or ([(480 * i, 96 + i % 5, 100, 10) for i in range(16)]
                      + [(480 * i, 60 + i % 5, 100, 10) for i in range(16)])
    first, *rest = parts
    write_mid(folder / "notes.mid", notes, track_name=TRACKS[first],
              extra_tracks=[(TRACKS[p], notes, []) for p in rest])
    write_ini(folder / "song.ini", name="Song", artist="Band", charter="Drum Charter")
    (folder / "drums_1.opus").write_bytes(b"")
    return folder


def _args(*extra):
    return ("--parts", "guitar,bass", "--from-drums", *extra)


# ── arguments ───────────────────────────────────────────────────────────────

def test_from_drums_needs_five_fret_parts(tmp_path):
    base = [str(tmp_path), "--backup-dir", str(tmp_path / "bak")]
    for bad in (["--from-drums"], ["--parts", "drums", "--from-drums"],
                ["--parts", "guitar", "--from-drums", "--upgrade"],
                ["--parts", "guitar", "--from-drums", "--auto"],
                ["--parts", "guitar", "--from-drums", "--local", str(tmp_path)]):
        with pytest.raises(SystemExit):
            drums_library.parse_args(base + bad)
    assert drums_library.parse_args(base + ["--parts", "guitar,bass", "--from-drums"]).from_drums is True
    assert drums_library.parse_args(base + ["--parts", "guitar"]).from_drums is False


# ── resolving the drum chart ────────────────────────────────────────────────

@pytest.mark.parametrize("style", ["posix", "windows"])
def test_candidate_is_the_cached_drum_chart_only(env, tmp_path, monkeypatch, offline, style):
    lib, bd = env
    cached = _chart(tmp_path / "cache" / "chorus" / MD5_A)
    _chart(tmp_path / "cache" / "chorus" / MD5_B)            # another cached chart: never used
    folder = (f"/Users/someone/.cache/slopsmith-drums/chorus/{MD5_A}" if style == "posix"
              else f"C:\\Users\\someone\\.cache\\slopsmith-drums\\chorus\\{MD5_A}")
    p = _pack(lib / "song.sloppak", drum_folder=folder, warp=WARP)
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    st = tmp_path / "st.json"
    s = _run(monkeypatch, lib, bd, st, *_args("--write"))
    assert len(fake.calls) == 1
    c = fake.calls[0]
    assert c["folder"] == cached and c["parts"] == ["guitar", "bass"]
    assert c["reuse"] == {"folder": folder, "warp": WARP}, "the drum join's warp is handed over"
    assert c["source"] == {"kind": "chart", "origin": "chorus", "md5": MD5_A}
    r = s["song.sloppak"]
    assert r["status"] == "joined", r
    assert r["parts"]["guitar"]["status"] == "joined" and r["parts"]["bass"]["status"] == "joined"
    assert r["chart"].startswith(f"chorus:{MD5_A}")
    assert {"five_fret_guitar.json", "five_fret_bass.json"} <= _members(p)
    assert _doc(p, "five_fret_guitar.json")["source"]["md5"] == MD5_A
    assert sorted(x.name for x in bd.iterdir()) == ["placeholder", "song.sloppak"]
    rows = _rows(st)
    assert [(x["part"], x["status"]) for x in rows] == [("guitar", "joined"), ("bass", "joined")]


def test_sng_cache_folder_remapped(env, tmp_path, monkeypatch, offline):
    lib, bd = env
    cached = _chart(drumjoin.SNG_CACHE / "0123456789abcdef")
    folder = "C:\\Users\\someone\\.cache\\slopsmith-drums\\sng\\0123456789abcdef"
    _pack(lib / "song.sloppak", drum_folder=folder, warp=WARP)
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", *_args())
    assert [c["folder"] for c in fake.calls] == [cached]
    assert fake.calls[0]["source"] == {"kind": "chart", "origin": "local"}
    assert fake.calls[0]["reuse"]["warp"] == WARP
    assert s["song.sloppak"]["status"] == "would-join"


def test_existing_local_folder_used_as_is(env, tmp_path, monkeypatch, offline):
    lib, bd = env
    here = _chart(tmp_path / "elsewhere" / "Band - Song")
    _pack(lib / "song.sloppak", drum_folder=str(here), warp=WARP)
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", *_args())
    assert [c["folder"] for c in fake.calls] == [here]
    assert s["song.sloppak"]["status"] == "would-join"


def test_no_drum_chart(env, tmp_path, monkeypatch, offline):
    lib, bd = env
    _pack(lib / "nodrums.sloppak")
    _pack(lib / "notcached.sloppak", drum_folder=f"C:\\x\\chorus\\{MD5_B}", warp=WARP)
    (tmp_path / "cache" / "chorus" / MD5_A).mkdir(parents=True)
    (tmp_path / "cache" / "chorus" / MD5_A / "song.ini").write_text("[song]\n")
    _pack(lib / "nonotes.sloppak", drum_folder=f"/old/chorus/{MD5_A}", warp=WARP)
    _pack(lib / "elsewhere.sloppak", drum_folder="/nowhere/Band - Song", warp=WARP)
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    st = tmp_path / "st.json"
    s = _run(monkeypatch, lib, bd, st, *_args("--write"))
    assert fake.calls == []
    for f in ("nodrums", "notcached", "nonotes", "elsewhere"):
        r = s[f"{f}.sloppak"]
        assert r["status"] == "no-drum-chart", (f, r)
        assert {k: v["status"] for k, v in r["parts"].items()} == {"guitar": "no-drum-chart",
                                                                   "bass": "no-drum-chart"}
    assert sorted(x.name for x in bd.iterdir()) == ["placeholder"]
    assert {x["status"] for x in _rows(st)} == {"no-drum-chart"}


def test_drum_chart_without_the_part(env, tmp_path, monkeypatch, offline):
    lib, bd = env
    _chart(tmp_path / "cache" / "chorus" / MD5_A, parts=("guitar",))
    _pack(lib / "song.sloppak", drum_folder=f"/old/chorus/{MD5_A}", warp=WARP)
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", *_args())
    assert [c["parts"] for c in fake.calls] == [["guitar"]]
    r = s["song.sloppak"]
    assert r["status"] == "would-join"
    assert r["parts"]["guitar"]["status"] == "would-join" and r["parts"]["bass"]["status"] == "no-chart"


def test_flagged_when_the_join_fails(env, tmp_path, monkeypatch, offline):
    lib, bd = env
    _chart(tmp_path / "cache" / "chorus" / MD5_A)
    p = _pack(lib / "song.sloppak", drum_folder=f"/old/chorus/{MD5_A}", warp=WARP)
    before = p.read_bytes()
    monkeypatch.setattr(fivefretjoin, "join_parts", FakeJoin(lambda f, part, n: False))
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", *_args("--write"))
    assert s["song.sloppak"]["status"] == "flagged"
    assert p.read_bytes() == before and sorted(x.name for x in bd.iterdir()) == ["placeholder"]


def test_finished_songs_in_state_skipped(env, tmp_path, monkeypatch, offline):
    lib, bd = env
    _chart(tmp_path / "cache" / "chorus" / MD5_A)
    for n in ("a", "b", "c"):
        _pack(lib / f"{n}.sloppak", drum_folder=f"/old/chorus/{MD5_A}", warp=WARP)
    st = tmp_path / "st.json"
    st.write_text('{"a.sloppak": {"file": "a.sloppak", "status": "joined"},'
                  ' "b.sloppak": {"file": "b.sloppak", "status": "no-chart"}}')
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    s = _run(monkeypatch, lib, bd, st, *_args())
    assert [c["sloppak"].name for c in fake.calls] == ["c.sloppak"]
    assert s["a.sloppak"]["status"] == "joined" and s["b.sloppak"]["status"] == "no-chart"
    _run(monkeypatch, lib, bd, st, *_args("--retry", "no-chart"))
    assert [c["sloppak"].name for c in fake.calls[1:]] == ["b.sloppak"]


# ── end to end (real join_parts, reused warp) ───────────────────────────────

@needs_audio
def test_from_drums_end_to_end(env, tmp_path, monkeypatch, offline):
    import fivefretchart
    from tests.drumjoin_fixtures import five_fret_notes, guitar_audio, make_sloppak
    lib, bd = env
    n_measures, dur = 30, 63
    cached = tmp_path / "cache" / "chorus" / MD5_A
    cached.mkdir(parents=True)
    write_mid(cached / "notes.mid", five_fret_notes(n_measures, seed=1), track_name="PART GUITAR",
              extra_tracks=[("PART BASS", five_fret_notes(n_measures, seed=2), [])])
    write_ini(cached / "song.ini", name="Song", artist="Band", charter="Tester")
    charts, _ = fivefretchart.load_song_folder(cached)
    shift = {p: [(n.time + 1.5, n.frets) for n in charts[p].notes] for p in ("guitar", "bass")}
    stems = {"guitar": guitar_audio(shift["guitar"], dur + 4, seed=5, noise=0.005),
             "bass": guitar_audio(shift["bass"], dur + 4, seed=6, noise=0.005)}
    drums = {"name": "Drums", "notes": [], "drums": {"source": {
        "source": "chart", "folder": f"C:\\Users\\x\\.cache\\slopsmith-drums\\chorus\\{MD5_A}",
        "warp": {"offset": 1.5}}}}
    song = make_sloppak(lib / "song.sloppak", stems, title="Song", artist="Band", extra_arrangements=[
        ({"id": "drums", "name": "Drums", "file": "arrangements/drums.json"}, drums)])
    st = tmp_path / "st.json"
    s = _run(monkeypatch, lib, bd, st, *_args("--write"))
    r = s["song.sloppak"]
    assert r["status"] == "joined", r
    for p in ("guitar", "bass"):
        assert r["parts"][p]["status"] == "joined"
        assert r["parts"][p]["reused_drums"] is True
        doc = _doc(song, f"five_fret_{p}.json")
        ok, why = fivefret.validate_five_fret(doc)
        assert ok, why
        assert doc["source"] == {"kind": "chart", "origin": "chorus", "md5": MD5_A, "charter": "Tester"}
    assert (bd / "song.sloppak").exists()


# ── state and fallthrough ───────────────────────────────────────────────────

def test_from_drums_default_state_is_its_own(tmp_path):
    base = [str(tmp_path), "--backup-dir", str(tmp_path / "bak"), "--parts", "guitar,bass"]
    lib_dir = Path.home() / "drums-work" / "library"
    a = drums_library.parse_args(base + ["--from-drums"])
    assert Path(a.state) == lib_dir / "five_fret_from_drums_state.json"
    assert a.online is False
    assert Path(drums_library.parse_args(base).state) == lib_dir / "five_fret_state.json"
    assert drums_library.parse_args(base + ["--from-drums", "--state", "x.json"]).state == "x.json"


def test_end_of_run_says_how_an_online_run_retries(env, tmp_path, monkeypatch, offline, capsys):
    lib, bd = env
    _pack(lib / "song.sloppak")
    monkeypatch.setattr(fivefretjoin, "join_parts", FakeJoin())
    _run(monkeypatch, lib, bd, tmp_path / "st.json", *_args())
    assert "--retry no-drum-chart,no-chart,flagged" in capsys.readouterr().out


def test_existing_folder_without_notes_falls_through_to_cache(env, tmp_path, monkeypatch, offline):
    lib, bd = env
    stale = tmp_path / "old" / "chorus" / MD5_A
    stale.mkdir(parents=True)
    (stale / "song.ini").write_text("[song]\n")
    cached = _chart(tmp_path / "cache" / "chorus" / MD5_A)
    _pack(lib / "song.sloppak", drum_folder=str(stale), warp=WARP)
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", *_args())
    assert [c["folder"] for c in fake.calls] == [cached]
    assert s["song.sloppak"]["status"] == "would-join"
