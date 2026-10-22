"""scripts/drums_library.py --parts guitar,rhythm,bass: the five-fret library run (mattromano
custom build). Everything runs in-process (an inline executor stands in for the process pool)
with the caches pointed at tmp_path and no network: ``chorus.search`` / ``chorus.fetch`` and,
where audio isn't the point, ``fivefretjoin.join_parts`` are fakes. The end-to-end run uses the
real join_parts and needs librosa, scipy, soundfile and ffmpeg (skipped without)."""
import concurrent.futures
import csv
import hashlib
import json
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

import chorus
import drumjoin
import fivefret
import fivefretjoin
from tests.drum_fixtures import write_ini, write_mid, write_sng

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import drums_library  # noqa: E402

MD5_A = "a" * 32
MD5_B = "b" * 32
LEVELS_1 = {"easy": {"notes": [{"t": 1.0, "f": 1}]},
            "medium": {"notes": [{"t": 1.0, "f": 1}], "generated": True},
            "hard": {"notes": [{"t": 1.0, "f": 1}], "generated": True}}
LEVELS_3 = {lv: {"notes": [{"t": 1.0, "f": 1}]} for lv in ("easy", "medium", "hard")}


def _have_audio_stack():
    try:
        import librosa  # noqa: F401
        import scipy  # noqa: F401
        import soundfile  # noqa: F401
    except ImportError:
        return False
    from tests.drumjoin_fixtures import ffmpeg
    return ffmpeg() is not None


needs_audio = pytest.mark.skipif(not _have_audio_stack(), reason="needs librosa, scipy, soundfile and ffmpeg")


class _Inline:
    """ProcessPoolExecutor stand-in: runs the worker here, so monkeypatches apply."""

    def __init__(self, workers, initializer=None, initargs=()):
        if initializer:
            initializer(*initargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args):
        f = concurrent.futures.Future()
        f.set_result(fn(*args))
        return f


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(drums_library, "ProcessPoolExecutor", _Inline)
    monkeypatch.setattr(drums_library, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(drumjoin, "SNG_CACHE", tmp_path / "sngcache")
    monkeypatch.setattr(chorus, "THROTTLE", tmp_path / "throttle")
    monkeypatch.setenv("OMP_NUM_THREADS", "1")

    def no_net(*a, **k):
        raise AssertionError("no network in tests")
    monkeypatch.setattr(chorus, "_req", no_net)
    monkeypatch.setattr(chorus, "_range", no_net)
    lib = tmp_path / "lib"
    lib.mkdir()
    bd = tmp_path / "bak"
    bd.mkdir()
    (bd / "placeholder").write_text("x")
    return lib, bd


# ── fixtures ────────────────────────────────────────────────────────────────

def _ff(part, kind="chart", levels=None, md5=None):
    d = {"version": 1, "name": part, "instrument": part, "source": {"kind": kind},
         "notes": [{"t": 1.0, "f": 1}, {"t": 2.0, "f": 2}]}
    if md5:
        d["source"]["md5"] = md5
    if levels:
        d["levels"] = levels
    return d


def _pack(path, *, existing=None, drum_folder=None, warp=None, title="Song", artist="Band"):
    """A zip pack (no audio): a Lead arrangement, ``existing`` five-fret parts
    ({part: payload}), optionally a legacy 0.2.9-tools drums arrangement whose source is
    ``drum_folder`` with ``warp``."""
    arrs = [{"id": "lead", "name": "Lead", "file": "arrangements/lead.json"}]
    files = {"arrangements/lead.json": {"name": "Lead", "notes": [], "chords": []}}
    for part, payload in (existing or {}).items():
        arrs.append({"id": f"five_fret_{part}", "name": part, "type": "five_fret", "instrument": part,
                     "five_fret": f"five_fret_{part}.json"})
        files[f"five_fret_{part}.json"] = payload
    if drum_folder is not None:
        arrs.append({"id": "drums", "name": "Drums", "file": "arrangements/drums.json"})
        files["arrangements/drums.json"] = {"name": "Drums", "notes": [], "drums": {
            "source": {"source": "chart", "folder": drum_folder, "warp": warp or [[0.0, 0.5], [100.0, 100.5]]}}}
    man = {"title": title, "artist": artist, "duration": 200.0, "arrangements": arrs}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.yaml", yaml.safe_dump(man, sort_keys=False))
        for rel, data in files.items():
            z.writestr(rel, json.dumps(data))
    return path


def _local_chart(root, parts=("guitar", "bass"), name="Song", artist="Band"):
    """A CH song folder with Expert notes for ``parts`` (Easy too, so it has one hand level)."""
    folder = root / f"{artist} - {name}"
    folder.mkdir(parents=True)
    tracks = {"guitar": "PART GUITAR", "rhythm": "PART RHYTHM", "bass": "PART BASS"}
    notes = [(480 * i, 96 + i % 5, 100, 10) for i in range(16)] + [(480 * i, 60 + i % 5, 100, 10) for i in range(16)]
    first, *rest = parts
    write_mid(folder / "notes.mid", notes, track_name=tracks[first],
              extra_tracks=[(tracks[p], notes, []) for p in rest])
    write_ini(folder / "song.ini", name=name, artist=artist, charter="Local Charter")
    return folder


def _members(pack):
    with zipfile.ZipFile(pack) as z:
        return set(z.namelist())


def _doc(pack, rel):
    with zipfile.ZipFile(pack) as z:
        return json.loads(z.read(rel))


class FakeJoin:
    """join_parts stand-in: ``decide(folder, part, call_no)`` says which parts pass; passing parts
    are written (unless dry_run) through the real write_five_fret, which calls ``before_write``
    (drums_library's backup) just before the pack changes."""

    def __init__(self, decide=None):
        self.calls = []
        self.decide = decide or (lambda folder, part, n: True)

    def __call__(self, sloppak, chart_folder, parts, *, source, reuse=None, min_levels=None, thresholds=None,
                 dry_run=True, before_write=None, log=print):
        n = len(self.calls)
        self.calls.append({"sloppak": Path(sloppak), "folder": Path(chart_folder), "parts": list(parts),
                           "source": dict(source), "reuse": reuse, "min_levels": min_levels,
                           "dry_run": dry_run, "before_write": before_write})
        rep = {"parts": {}, "written": []}
        passed = {}
        for p in parts:
            ok = bool(self.decide(Path(chart_folder), p, n))
            rep["parts"][p] = {"ok": ok, "reused_drums": bool(reuse) and ok, "method": "offset", "offset": 1.5,
                               "median_ms": 2.0, "within_30ms": 0.9 if ok else 0.2, "beat_margin": 0.3,
                               "levels": 3, "reasons": [] if ok else ["too few notes within 30 ms"],
                               "chart_audio": "guitar"}
            if ok:
                passed[p] = {**_ff(p, levels=LEVELS_3), "source": {"kind": "chart", **{
                    k: v for k, v in source.items() if k != "kind" and v is not None}}}
        if passed and not dry_run:
            sp = drumjoin.SloppakFiles(Path(sloppak))
            try:
                fivefretjoin.write_five_fret(sp, passed, {p: {"tool": "fake"} for p in passed},
                                             before_write=before_write)
            finally:
                sp.close()
            rep["written"] = list(passed)
        return rep


def _run(monkeypatch, lib, bd, state, *extra):
    argv = ["drums_library.py", str(lib), "--backup-dir", str(bd), "--workers", "1", "--state", str(state),
            *extra]
    monkeypatch.setattr(sys, "argv", argv)
    drums_library.main()
    return json.loads(Path(state).read_text())


def _rows(state):
    with open(Path(state).with_suffix(".csv"), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _result(md5, name="Song", parts=("guitar", "bass"), levels=3, length=200000, artist="Band", charter="CH"):
    lv = ("easy", "medium", "hard")[:levels]
    counts = [{"instrument": p, "difficulty": d, "count": 50} for p in parts for d in (*lv, "expert")]
    return {"md5": md5, "artist": artist, "name": name, "charter": charter, "song_length": length,
            "notesData": {"instruments": list(parts), "noteCounts": counts}}


class FakeChorus:
    def __init__(self, tmp_path, results):
        self.results = results
        self.fetches = []
        self.queries = []
        self.tmp = tmp_path

    def search(self, q, per_page=25, parts=("drums",)):
        self.queries.append((q, tuple(parts)))
        return list(self.results)

    def fetch(self, md5, out_dir, roles=("drums",)):
        self.fetches.append((md5, tuple(roles)))
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "song.ini").write_text("[song]\nname = Song\nartist = Band\n", encoding="utf-8")
        return out


@pytest.fixture
def online(tmp_path, monkeypatch):
    def make(results):
        fc = FakeChorus(tmp_path, results)
        monkeypatch.setattr(chorus, "search", fc.search)
        monkeypatch.setattr(chorus, "fetch", fc.fetch)
        return fc
    return make


# ── arguments ───────────────────────────────────────────────────────────────

def test_parts_arg_rejects_mixing(tmp_path):
    base = [str(tmp_path), "--backup-dir", str(tmp_path / "bak")]
    with pytest.raises(SystemExit):
        drums_library.parse_args(base + ["--parts", "drums,bass"])
    with pytest.raises(SystemExit):
        drums_library.parse_args(base + ["--parts", "guitar,keys"])
    assert drums_library.parse_args(base + ["--parts", "guitar, bass,guitar"]).parts == ["guitar", "bass"]
    assert drums_library.parse_args(base).parts == ["drums"]


def test_default_state_path_per_parts(tmp_path):
    base = [str(tmp_path), "--backup-dir", str(tmp_path / "bak")]
    lib_dir = Path.home() / "drums-work" / "library"
    assert Path(drums_library.parse_args(base + ["--parts", "bass"]).state) == lib_dir / "five_fret_state.json"
    assert Path(drums_library.parse_args(base + ["--parts", "drums"]).state) == lib_dir / "drums_state.json"
    assert Path(drums_library.parse_args(base).state) == lib_dir / "drums_state.json"
    assert drums_library.parse_args(base + ["--parts", "bass", "--state", "x.json"]).state == "x.json"


# ── candidates ──────────────────────────────────────────────────────────────

def test_five_fret_candidates_rank(env, online):
    fc = online([_result("other", name="Other Song", levels=3),
                 _result("live", levels=3, length=260000),
                 _result("guitaronly", parts=("guitar",), levels=3),
                 _result("studio", levels=3, length=201000),
                 _result("studio", levels=3, length=201000)])
    c = drums_library.five_fret_candidates("Band", "Song", 200.0, ["guitar", "bass"], 0.86)
    assert [x["md5"] for x in c] == ["studio", "guitaronly", "live"]
    assert c[0]["parts_have"] == ["guitar", "bass"] and c[0]["levels"] == 6
    assert c[1]["parts_have"] == ["guitar"] and c[1]["levels"] == 3
    assert fc.queries[0] == ("Band Song", ("guitar", "bass"))


# ── song selection and wanted parts ─────────────────────────────────────────

def test_has_parts_skipped_and_auto_replaced(env, tmp_path, monkeypatch):
    lib, bd = env
    _local_chart(tmp_path / "charts")
    a = _pack(lib / "a.sloppak", existing={"bass": _ff("bass"), "guitar": _ff("guitar", kind="auto")})
    b = _pack(lib / "b.sloppak", existing={"bass": _ff("bass"), "guitar": _ff("guitar")})
    b_bytes = b.read_bytes()
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass", "--local",
             str(tmp_path / "charts"), "--no-online", "--write")
    assert [c["parts"] for c in fake.calls] == [["guitar"]]
    assert fake.calls[0]["sloppak"] == a
    assert s["a.sloppak"]["status"] == "joined"
    assert s["b.sloppak"]["status"] == "has-parts"
    assert b.read_bytes() == b_bytes
    assert sorted(x.name for x in bd.iterdir()) == ["a.sloppak", "placeholder"], "no backup for b"
    assert _doc(a, "five_fret_guitar.json")["source"]["kind"] == "chart"
    # --redo: b is looked at again, but its chart parts stay untouched
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass", "--local",
             str(tmp_path / "charts"), "--no-online", "--write", "--redo")
    assert s["a.sloppak"]["status"] == "has-parts" and s["b.sloppak"]["status"] == "has-parts"
    assert len(fake.calls) == 1 and b.read_bytes() == b_bytes
    assert sorted(x.name for x in bd.iterdir()) == ["a.sloppak", "placeholder"], "no backup on --redo either"


def test_dry_run_writes_nothing_then_write_backs_up(env, tmp_path, monkeypatch):
    lib, bd = env
    _local_chart(tmp_path / "charts")
    p = _pack(lib / "song.sloppak")
    before = p.read_bytes()
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    args = ("--parts", "guitar,bass", "--local", str(tmp_path / "charts"), "--no-online")
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", *args)
    assert s["song.sloppak"]["status"] == "would-join"
    assert fake.calls[-1]["dry_run"] is True and fake.calls[-1]["before_write"] is None
    assert p.read_bytes() == before
    assert sorted(x.name for x in bd.iterdir()) == ["placeholder"]
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", *args, "--write")
    assert s["song.sloppak"]["status"] == "joined"
    assert len(fake.calls) == 2 and fake.calls[-1]["dry_run"] is False
    assert callable(fake.calls[-1]["before_write"]), "drums_library backs up through join_parts' hook"
    assert (bd / "song.sloppak").read_bytes() == before
    assert {"five_fret_guitar.json", "five_fret_bass.json"} <= _members(p)
    src = _doc(p, "five_fret_guitar.json")["source"]
    assert src == {"kind": "chart", "origin": "local"}, "a plain local folder has no md5"


def test_write_requires_populated_backup_dir_outside_library(env, tmp_path, monkeypatch):
    lib, _ = env
    _pack(lib / "song.sloppak")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit, match="empty or missing"):
        _run(monkeypatch, lib, empty, tmp_path / "st.json", "--parts", "guitar", "--no-online", "--write")
    inside = lib / "bak"
    inside.mkdir()
    (inside / "x").write_text("x")
    with pytest.raises(SystemExit, match="outside the library"):
        _run(monkeypatch, lib, inside, tmp_path / "st.json", "--parts", "guitar", "--no-online", "--write")


# ── per-part results ────────────────────────────────────────────────────────

def test_partial_song_rows(env, tmp_path, monkeypatch):
    lib, bd = env
    _local_chart(tmp_path / "charts")
    p = _pack(lib / "song.sloppak")
    monkeypatch.setattr(fivefretjoin, "join_parts", FakeJoin(lambda folder, part, n: part == "guitar"))
    st = tmp_path / "st.json"
    s = _run(monkeypatch, lib, bd, st, "--parts", "guitar,bass", "--local", str(tmp_path / "charts"),
             "--no-online", "--write")
    r = s["song.sloppak"]
    assert r["status"] == "joined"
    assert r["parts"]["guitar"]["status"] == "joined" and r["parts"]["bass"]["status"] == "flagged"
    rows = [x for x in _rows(st) if x["file"] == "song.sloppak"]
    assert [(x["part"], x["status"]) for x in rows] == [("guitar", "joined"), ("bass", "flagged")]
    assert rows[1]["error"] == "too few notes within 30 ms" and rows[0]["chart"].startswith("local:")
    assert list(_rows(st)[0]) == ["file", "artist", "title", "part", "status", "chart", "reused_drums", "method",
                                  "offset", "median_ms", "within_30ms", "beat_margin", "levels_before",
                                  "levels_after", "secs", "error", "real_status", "arrangement", "auto"]
    m = _members(p)
    assert "five_fret_guitar.json" in m and "five_fret_bass.json" not in m


def test_upgrade_rule(env, tmp_path, monkeypatch, online):
    lib, bd = env
    low = _pack(lib / "low.sloppak", existing={"guitar": _ff("guitar", levels=LEVELS_1)})
    full = _pack(lib / "full.sloppak", existing={"guitar": _ff("guitar", levels=LEVELS_3)}, title="Full")
    full_bytes = full.read_bytes()
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    online([_result(MD5_A, parts=("guitar",), levels=1)])
    s = _run(monkeypatch, lib, bd, tmp_path / "u1.json", "--parts", "guitar", "--upgrade", "--write")
    assert s["low.sloppak"]["status"] == "no-better"
    assert "full.sloppak" not in s, "a chart part with 3 hand levels isn't selected"
    assert fake.calls == [] and sorted(x.name for x in bd.iterdir()) == ["placeholder"]
    online([_result(MD5_B, parts=("guitar",), levels=3)])
    s = _run(monkeypatch, lib, bd, tmp_path / "u2.json", "--parts", "guitar", "--upgrade", "--write")
    r = s["low.sloppak"]
    assert r["status"] == "upgraded", r
    assert fake.calls[0]["min_levels"] == {"guitar": 1}
    assert r["parts"]["guitar"]["levels_before"] == 1 and r["parts"]["guitar"]["levels_after"] == 3
    assert _doc(low, "five_fret_guitar.json")["source"]["md5"] == MD5_B
    assert full.read_bytes() == full_bytes and "full.sloppak" not in s


# ── drum-warp reuse ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("style", ["posix", "windows"])
def test_reuse_fetches_chart_only(env, tmp_path, monkeypatch, online, style):
    lib, bd = env
    folder = (f"/Users/x/.cache/slopsmith-drums/chorus/{MD5_A}" if style == "posix"
              else f"C:\\Users\\x\\.cache\\slopsmith-drums\\chorus\\{MD5_A}")
    warp = [[0.0, 0.7], [50.0, 50.8], [100.0, 100.9]]
    _pack(lib / "song.sloppak", drum_folder=folder, warp=warp)
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    fc = online([_result(MD5_A)])
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass")
    assert fc.fetches == [(MD5_A, ())]
    assert fake.calls[0]["reuse"] == {"folder": folder, "warp": warp}
    assert fake.calls[0]["source"] == {"kind": "chart", "origin": "chorus", "md5": MD5_A, "charter": "CH"}
    assert s["song.sloppak"]["status"] == "would-join"


def test_other_chart_fetches_stems_no_reuse(env, tmp_path, monkeypatch, online):
    lib, bd = env
    _pack(lib / "song.sloppak", drum_folder=f"/c/chorus/{MD5_A}")
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    fc = online([_result(MD5_B)])
    _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass")
    assert fc.fetches == [(MD5_B, ("guitar", "bass", "rhythm"))], "bass also takes rhythm.* (no rhythm part)"
    assert fake.calls[0]["reuse"] is None
    # a chart with a rhythm part of its own: bass is bass.* only
    fc = online([_result(MD5_B, parts=("guitar", "rhythm", "bass"))])
    _run(monkeypatch, lib, bd, tmp_path / "st2.json", "--parts", "guitar,bass")
    assert fc.fetches == [(MD5_B, ("guitar", "bass"))]


def test_reuse_fallback_fetches_audio(env, tmp_path, monkeypatch, online):
    lib, bd = env
    folder = f"/c/chorus/{MD5_A}"
    p = _pack(lib / "song.sloppak", drum_folder=folder)
    # first call (chart only): guitar passes on the reused warp, bass doesn't; with audio it does
    fake = FakeJoin(lambda f, part, n: part == "guitar" or n == 1)
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    fc = online([_result(MD5_A)])
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass", "--write")
    assert fc.fetches == [(MD5_A, ()), (MD5_A, ("bass", "rhythm"))]
    assert [c["parts"] for c in fake.calls] == [["guitar", "bass"], ["bass"]]
    assert all(c["reuse"] is not None for c in fake.calls)
    r = s["song.sloppak"]
    assert r["status"] == "joined"
    assert r["parts"]["guitar"]["status"] == "joined" and r["parts"]["bass"]["status"] == "joined"
    assert {"five_fret_guitar.json", "five_fret_bass.json"} <= _members(p)
    assert sorted(x.name for x in bd.iterdir()) == ["placeholder", "song.sloppak"]


def test_backup_once_per_song(env, tmp_path, monkeypatch, online):
    lib, bd = env
    p = _pack(lib / "song.sloppak")
    before = p.read_bytes()
    # guitar from the first chart, bass from the second
    fake = FakeJoin(lambda f, part, n: (f.name == MD5_A) == (part == "guitar"))
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    online([_result(MD5_A), _result(MD5_B, length=202000)])
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass", "--write")
    assert [(c["folder"].name, c["parts"]) for c in fake.calls] == [(MD5_A, ["guitar", "bass"]), (MD5_B, ["bass"])]
    assert s["song.sloppak"]["status"] == "joined"
    backups = sorted(x.name for x in bd.iterdir() if x.name != "placeholder")
    assert backups == ["song.sloppak"], "one backup per song per run"
    assert (bd / "song.sloppak").read_bytes() == before
    assert _doc(p, "five_fret_guitar.json")["source"]["md5"] == MD5_A
    assert _doc(p, "five_fret_bass.json")["source"]["md5"] == MD5_B
    rows = _rows(tmp_path / "st.json")
    assert [(x["part"], x["status"], x["chart"].split()[0]) for x in rows] == [
        ("guitar", "joined", f"chorus:{MD5_A}"), ("bass", "joined", f"chorus:{MD5_B}")]


def test_no_backup_when_every_part_flags(env, tmp_path, monkeypatch):
    lib, bd = env
    _local_chart(tmp_path / "charts")
    p = _pack(lib / "song.sloppak")
    before = p.read_bytes()
    monkeypatch.setattr(fivefretjoin, "join_parts", FakeJoin(lambda folder, part, n: False))
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass", "--local",
             str(tmp_path / "charts"), "--no-online", "--write")
    assert s["song.sloppak"]["status"] == "flagged"
    assert sorted(x.name for x in bd.iterdir()) == ["placeholder"], "nothing written, nothing backed up"
    assert p.read_bytes() == before


def test_redo_with_remaining_part_flagging_makes_no_dated_copy(env, tmp_path, monkeypatch):
    lib, bd = env
    _local_chart(tmp_path / "charts")
    _pack(bd / "song.sloppak")                      # the pre-join original, already backed up
    p = _pack(lib / "song.sloppak", existing={"guitar": _ff("guitar")})
    assert p.read_bytes() != (bd / "song.sloppak").read_bytes()
    before = p.read_bytes()
    fake = FakeJoin(lambda folder, part, n: False)
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass", "--local",
             str(tmp_path / "charts"), "--no-online", "--write", "--redo")
    assert [c["parts"] for c in fake.calls] == [["bass"]]
    assert s["song.sloppak"]["parts"]["bass"]["status"] == "flagged"
    assert sorted(x.name for x in bd.iterdir()) == ["placeholder", "song.sloppak"], "no dated copy"
    assert p.read_bytes() == before


def test_failing_candidate_doesnt_stop_the_song(env, tmp_path, monkeypatch, online):
    lib, bd = env
    p = _pack(lib / "song.sloppak")
    md5_c = "c" * 32
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    fc = online([_result(MD5_A, parts=("guitar",)), _result(MD5_B, parts=("bass",)),
                 _result(md5_c, parts=("rhythm",))])
    real_fetch = fc.fetch

    def fetch(md5, out_dir, roles=("drums",)):
        if md5 == MD5_B:
            raise ConnectionError("download failed")
        return real_fetch(md5, out_dir, roles)
    monkeypatch.setattr(chorus, "fetch", fetch)
    st = tmp_path / "st.json"
    s = _run(monkeypatch, lib, bd, st, "--parts", "guitar,rhythm,bass", "--write")
    r = s["song.sloppak"]
    assert r["status"] == "joined", r
    assert [(c["folder"].name, c["parts"]) for c in fake.calls] == [(MD5_A, ["guitar"]), (md5_c, ["rhythm"])], \
        "the candidate after the failing one is still tried"
    assert r["parts"]["guitar"]["status"] == "joined" and r["parts"]["rhythm"]["status"] == "joined"
    assert r["parts"]["bass"]["status"] == "error"
    assert "download failed" in r["parts"]["bass"]["error"]
    assert any(t["part"] == "bass" and "download failed" in (t["error"] or "") for t in r["tried"])
    assert {"five_fret_guitar.json", "five_fret_rhythm.json"} <= _members(p)
    rows = {x["part"]: x for x in _rows(st)}
    assert rows["bass"]["status"] == "error" and "download failed" in rows["bass"]["error"]
    assert sorted(x.name for x in bd.iterdir()) == ["placeholder", "song.sloppak"]


# ── resume ──────────────────────────────────────────────────────────────────

def test_resume_skips_finished(env, tmp_path, monkeypatch):
    lib, bd = env
    _local_chart(tmp_path / "charts")
    _pack(lib / "good.sloppak")
    _pack(lib / "bad.sloppak")
    calls = []

    def decide(folder, part, n):
        return calls[-1] == "good.sloppak"
    fake = FakeJoin(decide)
    real_call = fake.__call__

    def tracking(sloppak, *a, **k):
        calls.append(Path(sloppak).name)
        return real_call(sloppak, *a, **k)
    monkeypatch.setattr(fivefretjoin, "join_parts", tracking)
    args = ("--parts", "guitar", "--local", str(tmp_path / "charts"), "--no-online", "--write")
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", *args)
    assert s["good.sloppak"]["status"] == "joined" and s["bad.sloppak"]["status"] == "flagged"
    n = len(calls)
    _run(monkeypatch, lib, bd, tmp_path / "st.json", *args)
    assert len(calls) == n, "finished songs are skipped"
    _run(monkeypatch, lib, bd, tmp_path / "st.json", *args, "--retry", "flagged")
    assert calls[n:] == ["bad.sloppak"]


# ── end to end (real join_parts) ────────────────────────────────────────────

@needs_audio
def test_library_run_end_to_end(env, tmp_path, monkeypatch):
    import fivefretchart
    import sloppak
    from tests.drumjoin_fixtures import five_fret_notes, guitar_audio, make_sloppak, write_audio
    lib, bd = env
    n_measures, dur = 30, 63
    src = tmp_path / "src"
    src.mkdir()
    write_mid(src / "notes.mid", five_fret_notes(n_measures, seed=1), track_name="PART GUITAR",
              extra_tracks=[("PART BASS", five_fret_notes(n_measures, seed=2), [])])
    write_ini(src / "song.ini", name="Song", artist="Band", charter="Tester")
    charts, _ = fivefretchart.load_song_folder(src)
    tf = {p: [(n.time, n.frets) for n in charts[p].notes] for p in ("guitar", "bass")}
    files = {"notes.mid": (src / "notes.mid").read_bytes()}
    for i, p in enumerate(("guitar", "bass")):
        write_audio(src / f"{p}.ogg", guitar_audio(tf[p], dur, seed=11 + i))
        files[f"{p}.ogg"] = (src / f"{p}.ogg").read_bytes()
    (tmp_path / "charts").mkdir()
    sng = write_sng(tmp_path / "charts" / "Band - Song.sng", files, {"name": "Song", "artist": "Band",
                                                                     "charter": "Tester", "delay": "0"})
    shift = {p: [(t + 1.5, f) for t, f in tf[p]] for p in tf}
    stems = {"guitar": guitar_audio(shift["guitar"], dur + 4, seed=5, noise=0.005),
             "bass": guitar_audio(shift["bass"], dur + 4, seed=6, noise=0.005)}
    song = make_sloppak(lib / "song.sloppak", stems, title="Song", artist="Band")
    make_sloppak(lib / "nomatch.sloppak", {"guitar": stems["guitar"]}, title="Unknown", artist="Nobody")
    st = tmp_path / "st.json"
    args = ("--parts", "guitar,bass", "--local", str(tmp_path / "charts"), "--no-online")
    s = _run(monkeypatch, lib, bd, st, *args)
    assert s["song.sloppak"]["status"] == "would-join", s["song.sloppak"]
    assert s["nomatch.sloppak"]["status"] == "no-chart"
    assert not any(n.startswith("five_fret_") for n in _members(song))
    s = _run(monkeypatch, lib, bd, st, *args, "--write")
    r = s["song.sloppak"]
    assert r["status"] == "joined", r
    for p in ("guitar", "bass"):
        assert r["parts"][p]["status"] == "joined"
        assert r["parts"][p]["offset"] == pytest.approx(1.5, abs=0.02)
    assert (bd / "song.sloppak").exists() and not any(n.startswith("five_fret_") for n in _members(bd / "song.sloppak"))
    for p in ("guitar", "bass"):
        doc = _doc(song, f"five_fret_{p}.json")
        ok, why = fivefret.validate_five_fret(doc)
        assert ok, why
        assert doc["source"] == {"kind": "chart", "origin": "local",
                                 "md5": hashlib.md5(sng.read_bytes()).hexdigest(), "charter": "Tester"}
    assert sloppak.extract_meta(song)["five_fret_parts"] == ["guitar:chart", "bass:chart"]
    rows = _rows(st)
    assert sorted((x["file"], x["part"], x["status"]) for x in rows) == [
        ("nomatch.sloppak", "bass", "no-chart"), ("nomatch.sloppak", "guitar", "no-chart"),
        ("song.sloppak", "bass", "joined"), ("song.sloppak", "guitar", "joined")]


def test_retry_reaches_partly_joined_songs(env, tmp_path, monkeypatch):
    lib, bd = env
    _local_chart(tmp_path / "charts")
    p = _pack(lib / "song.sloppak")
    fake = FakeJoin(lambda folder, part, n: part == "guitar" or n >= 1)
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    st = tmp_path / "st.json"
    args = ("--parts", "guitar,bass", "--local", str(tmp_path / "charts"), "--no-online", "--write")
    s = _run(monkeypatch, lib, bd, st, *args)
    assert s["song.sloppak"]["status"] == "joined"
    assert s["song.sloppak"]["parts"]["bass"]["status"] == "flagged"
    _run(monkeypatch, lib, bd, st, *args)
    assert len(fake.calls) == 1, "the default retry (error) leaves it alone"
    s = _run(monkeypatch, lib, bd, st, *args, "--retry", "flagged")
    assert [c["parts"] for c in fake.calls[1:]] == [["bass"]], "a flagged part brings the song back"
    r = s["song.sloppak"]
    assert r["status"] == "joined"
    assert r["parts"]["guitar"]["status"] == "joined", "the earlier part's result is kept"
    assert r["parts"]["bass"]["status"] == "joined"
    assert {"five_fret_guitar.json", "five_fret_bass.json"} <= _members(p)
    _run(monkeypatch, lib, bd, st, *args, "--retry", "flagged")
    assert len(fake.calls) == 2, "nothing flagged any more"
