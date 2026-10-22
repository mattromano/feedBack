"""scripts/drums_library.py --parts ... --auto: auto parts for songs with no real chart (mattromano
custom build). In-process (inline executor), caches in tmp_path, no network; real-chart joins
are FakeJoin (no audio), auto parts come from the real Plan 5 auto-charter."""
import json
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

import fivefret
import fivefret_autochart as ac
import fivefretjoin
from tests.test_drums_library_five_fret import (FakeJoin, _doc, _local_chart, _rows, _run,  # noqa: F401
                                                 env, online)
from tests.test_five_fret_autochart_cmd import _band

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import drums_library  # noqa: E402


def _manifest(pack):
    with zipfile.ZipFile(pack) as z:
        return yaml.safe_load(z.read("manifest.yaml"))


def _entry(pack, part):
    return next(e for e in _manifest(pack)["arrangements"]
                if e.get("type") == "five_fret" and e.get("instrument") == part)


def _set_auto_version(pack, part, version):
    """Rewrite the pack's auto part for ``part`` to claim ``version``."""
    with zipfile.ZipFile(pack) as z:
        members = {n: z.read(n) for n in z.namelist()}
    rel = f"five_fret_{part}.json"
    doc = json.loads(members[rel])
    doc["source"]["generator_version"] = version
    members[rel] = json.dumps(doc).encode()
    with zipfile.ZipFile(pack, "w", zipfile.ZIP_STORED) as z:
        for n, b in members.items():
            z.writestr(n, b)


def _bak(bd):
    return sorted(x.name for x in bd.iterdir())


# ── arguments ───────────────────────────────────────────────────────────────

def test_auto_needs_five_fret_parts(tmp_path):
    base = [str(tmp_path), "--backup-dir", str(tmp_path / "bak")]
    with pytest.raises(SystemExit):
        drums_library.parse_args(base + ["--auto"])
    with pytest.raises(SystemExit):
        drums_library.parse_args(base + ["--parts", "drums", "--auto"])
    with pytest.raises(SystemExit):
        drums_library.parse_args(base + ["--parts", "guitar", "--auto", "--upgrade"])
    assert drums_library.parse_args(base + ["--parts", "guitar,bass", "--auto"]).auto is True
    assert drums_library.parse_args(base + ["--parts", "guitar"]).auto is False


# ── writing ─────────────────────────────────────────────────────────────────

def test_auto_written_when_no_real_chart(env, tmp_path, monkeypatch):
    lib, bd = env
    p = _band(lib, "song.sloppak")
    other = set(zipfile.ZipFile(p).namelist())
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass", "--auto",
             "--no-online", "--write")
    r = s["song.sloppak"]
    assert r["status"] == "auto"
    assert {k: v["status"] for k, v in r["parts"].items()} == {"guitar": "auto", "bass": "auto"}
    g = _doc(p, "five_fret_guitar.json")
    assert g["source"] == {"kind": "auto", "generator": ac.GENERATOR,
                           "generator_version": ac.GENERATOR_VERSION, "from_arrangement": "lead"}
    assert g["name"] == "Guitar (auto)"
    assert fivefret.validate_five_fret(g)[0]
    assert _doc(p, "five_fret_bass.json")["source"]["from_arrangement"] == "bass"
    assert _entry(p, "guitar")["pairs_with"] == "lead" and _entry(p, "guitar")["name"] == "Guitar (auto)"
    assert _entry(p, "bass")["pairs_with"] == "bass"
    assert other <= set(zipfile.ZipFile(p).namelist())
    rows = {(x["file"], x["part"]): x for x in _rows(tmp_path / "st.json")}
    assert rows[("song.sloppak", "guitar")]["status"] == "auto"
    assert rows[("song.sloppak", "guitar")]["arrangement"] == "lead"
    assert rows[("song.sloppak", "bass")]["status"] == "auto"
    assert _bak(bd) == ["placeholder", "song.sloppak"]
    # resumable: the finished song is skipped next time
    before = p.read_bytes()
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass", "--auto",
             "--no-online", "--write")
    assert s["song.sloppak"]["status"] == "auto" and p.read_bytes() == before


def test_real_chart_never_replaced(env, tmp_path, monkeypatch):
    lib, bd = env
    _local_chart(tmp_path / "charts", parts=("guitar",))
    p = _band(lib, "song.sloppak", five_fret=[("bass", "chart")])
    real_bass = _doc(p, "five_fret_bass.json")
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,rhythm,bass", "--auto",
             "--local", str(tmp_path / "charts"), "--no-online", "--write")
    r = s["song.sloppak"]
    assert r["status"] == "joined"
    assert r["parts"]["guitar"]["status"] == "joined"      # the real chart found this run
    assert r["parts"]["rhythm"]["status"] == "auto"        # nothing real for rhythm
    assert "bass" not in r["parts"]
    assert _doc(p, "five_fret_guitar.json")["source"]["kind"] == "chart"
    assert _doc(p, "five_fret_rhythm.json")["source"]["kind"] == "auto"
    assert _doc(p, "five_fret_bass.json") == real_bass
    assert _bak(bd) == ["placeholder", "song.sloppak"], "one backup, before the first write"


def test_older_auto_replaced_equal_skipped(env, tmp_path, monkeypatch):
    lib, bd = env
    old = _band(lib, "old.sloppak", five_fret=[("guitar", "auto")])      # generator_version "0"
    same = _band(lib, "same.sloppak", five_fret=[("guitar", "auto")])
    _set_auto_version(same, "guitar", ac.GENERATOR_VERSION)
    same_bytes = same.read_bytes()
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar", "--auto",
             "--no-online", "--write")
    assert s["old.sloppak"]["parts"]["guitar"]["status"] == "auto"
    assert _doc(old, "five_fret_guitar.json")["source"]["generator_version"] == ac.GENERATOR_VERSION
    assert len([e for e in _manifest(old)["arrangements"] if e.get("type") == "five_fret"]) == 1
    assert s["same.sloppak"]["parts"]["guitar"]["status"] == "no-chart"
    assert s["same.sloppak"]["parts"]["guitar"]["auto"] == "current"
    assert same.read_bytes() == same_bytes
    assert _bak(bd) == ["old.sloppak", "placeholder"], "no backup when nothing is written"


def test_dry_run_writes_nothing(env, tmp_path, monkeypatch):
    lib, bd = env
    p = _band(lib, "song.sloppak")
    before = p.read_bytes()
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass", "--auto", "--no-online")
    assert s["song.sloppak"]["status"] == "would-auto"
    assert {v["status"] for v in s["song.sloppak"]["parts"].values()} == {"would-auto"}
    assert p.read_bytes() == before
    assert _bak(bd) == ["placeholder"]
    # a would-auto song runs again with --write
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass", "--auto",
             "--no-online", "--write")
    assert s["song.sloppak"]["status"] == "auto"
    assert _doc(p, "five_fret_guitar.json")["source"]["kind"] == "auto"


def test_backup_once_per_song(env, tmp_path, monkeypatch):
    lib, bd = env
    _local_chart(tmp_path / "charts", parts=("guitar",))
    p = _band(lib, "song.sloppak")
    original = p.read_bytes()
    fake = FakeJoin()
    monkeypatch.setattr(fivefretjoin, "join_parts", fake)
    calls = []
    real_backup = drums_library.ensure_backup
    monkeypatch.setattr(drums_library, "ensure_backup", lambda *a: calls.append(a) or real_backup(*a))
    _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar,bass", "--auto",
         "--local", str(tmp_path / "charts"), "--no-online", "--write")
    assert len(calls) == 1
    assert _bak(bd) == ["placeholder", "song.sloppak"]
    assert (bd / "song.sloppak").read_bytes() == original
    assert _doc(p, "five_fret_guitar.json")["source"]["kind"] == "chart"
    assert _doc(p, "five_fret_bass.json")["source"]["kind"] == "auto"


def test_no_arrangement_keeps_search_status(env, tmp_path, monkeypatch):
    lib, bd = env
    p = _band(lib, "song.sloppak")
    with zipfile.ZipFile(p) as z:
        members = {n: z.read(n) for n in z.namelist()}
    man = yaml.safe_load(members["manifest.yaml"])
    man["arrangements"] = [e for e in man["arrangements"] if e["id"] != "bass"]
    members["manifest.yaml"] = yaml.safe_dump(man).encode()
    with zipfile.ZipFile(p, "w") as z:
        for n, b in members.items():
            z.writestr(n, b)
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "bass", "--auto", "--no-online", "--write")
    pr = s["song.sloppak"]["parts"]["bass"]
    assert pr["status"] == "no-chart" and pr["auto"] == "skip-none"
    assert s["song.sloppak"]["status"] == "no-chart"
    assert _bak(bd) == ["placeholder"]


def test_auto_failure_keeps_search_results(env, tmp_path, monkeypatch):
    lib, bd = env
    p = _band(lib, "song.sloppak")
    before = p.read_bytes()
    import five_fret_autochart as ffa

    def boom(*a, **k):
        raise ValueError("bad arrangement")
    monkeypatch.setattr(ffa, "run_pack", boom)
    s = _run(monkeypatch, lib, bd, tmp_path / "st.json", "--parts", "guitar", "--auto", "--no-online", "--write")
    r = s["song.sloppak"]
    assert r["status"] == "no-chart"
    assert r["parts"]["guitar"]["status"] == "no-chart" and r["parts"]["guitar"]["auto"] == "error"
    assert "bad arrangement" in r["parts"]["guitar"]["error"]
    assert p.read_bytes() == before and _bak(bd) == ["placeholder"]
