"""lib/chorus.py per-part search, difficulty counts and role-aware incremental stem fetch."""
import json

import pytest

import chorus
from tests.drum_fixtures import write_sng


def _res(md5, instruments=None, **kw):
    return {"md5": md5, "notesData": {"instruments": instruments} if instruments is not None else None, **kw}


def test_search_filters_by_part(monkeypatch):
    data = {"data": [_res("a", ["guitar"]), _res("b", ["drums", "bass"]), _res("c", diff_bass=2),
                     _res("d", ["guitarcoop"])]}
    monkeypatch.setattr(chorus, "_req", lambda *a, **k: json.dumps(data).encode())
    assert [r["md5"] for r in chorus.search("x", parts=("bass",))] == ["b", "c"]
    assert [r["md5"] for r in chorus.search("x", parts=("rhythm",))] == ["d"]
    assert [r["md5"] for r in chorus.search("x")] == ["b"]


def test_part_levels():
    nc = [{"instrument": "guitar", "difficulty": "easy", "count": 10},
          {"instrument": "guitar", "difficulty": "medium", "count": 0},
          {"instrument": "guitar", "difficulty": "hard", "count": 5},
          {"instrument": "bass", "difficulty": "expert", "count": 9}]
    r = {"notesData": {"noteCounts": nc}}
    assert chorus.part_levels(r, "guitar") == 2
    assert chorus.part_levels(r, "bass") == 0


def test_stem_roles():
    assert chorus.stem_roles("guitar", False) == ("guitar",)
    assert chorus.stem_roles("rhythm", True) == ("rhythm", "guitar")
    assert chorus.stem_roles("bass", True) == ("bass",)
    assert chorus.stem_roles("bass", False) == ("bass", "rhythm")
    assert chorus.stem_roles("drums", False) == ("drums",)
    assert chorus.STEM_ROLES["bass"] == ("bass", "rhythm")


@pytest.fixture
def pkg(tmp_path, monkeypatch):
    monkeypatch.setattr(chorus, "THROTTLE", tmp_path / "throttle")

    def make(files):
        blob = write_sng(tmp_path / "p.sng", files, {"name": "Song", "artist": "Band"}).read_bytes()
        calls = []

        def fake_range(md5, a, b):
            calls.append((a, b))
            return blob[a:b + 1]
        monkeypatch.setattr(chorus, "_range", fake_range)
        return calls
    return make


def _names(d):
    return sorted(x.name for x in d.iterdir() if x.name != ".complete")


FULL = {"notes.mid": b"MThd", "drums_1.ogg": b"d1", "guitar.ogg": b"g", "song.ogg": b"s",
        "preview.ogg": b"p", "crowd.ogg": b"c"}


def test_fetch_guitar_stem(tmp_path, pkg):
    pkg(FULL)
    out = chorus.fetch("m", tmp_path / "o", roles=("guitar",))
    assert _names(out) == ["guitar.ogg", "notes.mid", "song.ini"]


def test_fetch_adds_missing_role_to_cached_drums_folder(tmp_path, pkg):
    calls = pkg(FULL)
    out = chorus.fetch("m", tmp_path / "o")
    assert _names(out) == ["drums_1.ogg", "notes.mid", "song.ini"]
    before = (out / "drums_1.ogg").stat().st_mtime_ns
    chorus.fetch("m", tmp_path / "o", roles=("guitar",))
    assert _names(out) == ["drums_1.ogg", "guitar.ogg", "notes.mid", "song.ini"]
    assert (out / "drums_1.ogg").stat().st_mtime_ns == before
    done = json.loads((out / ".complete").read_text())
    assert sorted(done["files"]) == ["drums_1.ogg", "guitar.ogg", "notes.mid"]
    assert "song.ogg" in done["package_files"]
    n = len(calls)
    chorus.fetch("m", tmp_path / "o", roles=("guitar",))
    assert len(calls) == n


def test_fetch_role_absent_takes_all_audio(tmp_path, pkg):
    pkg(FULL)
    out = chorus.fetch("m", tmp_path / "o", roles=("bass",))
    assert _names(out) == ["drums_1.ogg", "guitar.ogg", "notes.mid", "song.ini", "song.ogg"]


def test_fetch_bass_rhythm_roles_takes_only_rhythm(tmp_path, pkg):
    pkg({"notes.mid": b"MThd", "rhythm.ogg": b"r", "guitar.ogg": b"g", "song.ogg": b"s"})
    out = chorus.fetch("m", tmp_path / "o", roles=("bass", "rhythm"))
    assert _names(out) == ["notes.mid", "rhythm.ogg", "song.ini"]


def test_fetch_no_roles_chart_only(tmp_path, pkg):
    pkg(FULL)
    out = chorus.fetch("m", tmp_path / "o", roles=())
    assert _names(out) == ["notes.mid", "song.ini"]


def test_old_complete_without_package_files(tmp_path, pkg):
    calls = pkg(FULL)
    out = tmp_path / "o"
    out.mkdir()
    (out / "notes.mid").write_bytes(b"MThd")
    (out / "drums_1.ogg").write_bytes(b"d1")
    (out / "song.ini").write_text("[song]\n")
    (out / ".complete").write_text(json.dumps({"md5": "m", "files": ["notes.mid", "drums_1.ogg"]}))
    chorus.fetch("m", out, roles=("guitar",))
    assert calls[0] == (0, 65535) and len(calls) == 2          # one header read, then guitar.ogg
    assert (out / "guitar.ogg").read_bytes() == b"g"
    assert "package_files" in json.loads((out / ".complete").read_text())
