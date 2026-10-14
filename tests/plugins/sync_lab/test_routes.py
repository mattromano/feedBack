"""sync_lab: the calibration song is a pack core can load, and the routes work."""

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import sloppak

ROUTES = Path(__file__).resolve().parents[3] / "plugins" / "sync_lab" / "routes.py"


def _load():
    spec = importlib.util.spec_from_file_location("sync_lab_routes_under_test", ROUTES)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def test_song_loads_with_core_loader(mod, tmp_path):
    dlc = tmp_path / "dlc"
    (dlc / "sloppak").mkdir(parents=True)
    rel = mod.ensure_song(dlc)
    assert rel == f"sloppak/{mod.NAME}"
    loaded = sloppak.load_song(rel, dlc, tmp_path / "cache")
    song = loaded.song
    assert (song.title, song.artist) == (mod.TITLE, mod.ARTIST)
    assert [a.name for a in song.arrangements] == ["Lead", "Bass", "Drums"]
    times = mod.note_times()
    assert len(times) == 32 + 32 + 24
    lead = song.arrangements[0]
    assert {(n.string, n.fret) for n in lead.notes} == {(1, 0)}            # open A
    drums = song.arrangements[2]
    assert {n.string * 24 + n.fret for n in drums.notes} == {38}           # GM snare
    assert loaded.drum_tab is None                                          # no song-level drum tab (Auto viz)
    assert [s["id"] for s in loaded.stems] == ["full"]
    assert [s.name for s in song.sections] == ["count", "listen", "count", "watch", "count", "play"]


def test_plan_matches_the_song(mod):
    p = mod.plan()
    times = mod.note_times()
    for name, (a, b) in p["sections"].items():
        inside = [t for t in times if a - 1e-6 <= t < b]
        assert inside, name
    assert p["blank"][1] <= p["sections"]["watch"][0]
    assert p["arrangements"] == ["Lead", "Bass", "Drums"]


def test_click_track_is_valid_wav(mod):
    import io
    import wave
    with wave.open(io.BytesIO(mod.click_track())) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, mod.SR)
        assert abs(w.getnframes() / mod.SR - mod.DURATION) < 0.01


def test_old_versions_replaced_and_dlc_root_fallback(mod, tmp_path):
    dlc = tmp_path / "dlc"
    dlc.mkdir()
    (dlc / "_Sync_Calibration_v1.sloppak").write_bytes(b"old")
    assert mod.ensure_song(dlc) == mod.NAME
    assert not (dlc / "_Sync_Calibration_v1.sloppak").exists()
    assert mod.ensure_song(None) is None


def test_routes(mod, tmp_path):
    dlc = tmp_path / "dlc"
    (dlc / "sloppak").mkdir(parents=True)
    app = FastAPI()
    mod.setup(app, {"config_dir": str(tmp_path / "cfg"), "get_dlc_dir": lambda: dlc})
    c = TestClient(app)
    info = c.get("/api/plugins/sync_lab/info").json()
    assert info["ok"] and info["plan"]["file"] == f"sloppak/{mod.NAME}"
    assert c.post("/api/plugins/sync_lab/log", json={"kind": "calibration", "result": {"av": 0}}).json() == {"ok": True}
    assert c.post("/api/plugins/sync_lab/log", json=[1]).json() == {"ok": False}
    rows = c.get("/api/plugins/sync_lab/log").json()["entries"]
    assert [r["kind"] for r in rows] == ["calibration"]
    assert (tmp_path / "cfg" / "sync_lab" / "sync_log.jsonl").exists()
