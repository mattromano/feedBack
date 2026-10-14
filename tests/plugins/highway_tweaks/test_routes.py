"""highway_tweaks routes: the diagnostics log lives in the config dir."""

import importlib.util
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROUTES = Path(__file__).resolve().parents[3] / "plugins" / "highway_tweaks" / "routes.py"


def _load():
    spec = importlib.util.spec_from_file_location("highway_tweaks_routes_under_test", ROUTES)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def setup(tmp_path):
    mod = _load()
    app = FastAPI()
    mod.setup(app, {"config_dir": str(tmp_path)})
    return mod, TestClient(app), tmp_path


def test_post_appends_to_config_dir_log(setup):
    mod, client, cfg = setup
    assert client.post("/api/plugins/highway_tweaks/log", json={"kind": "jank", "stats": {"fps": 50}}).json() == {"ok": True}
    assert client.post("/api/plugins/highway_tweaks/log", json={"kind": "judgments", "events": []}).json() == {"ok": True}
    path = cfg / "highway_tweaks" / "jank_log.jsonl"
    assert path.exists()
    rows = client.get("/api/plugins/highway_tweaks/log?n=10").json()["entries"]
    assert [r["kind"] for r in rows] == ["jank", "judgments"]
    assert all("server_ts" in r for r in rows)
    only = client.get("/api/plugins/highway_tweaks/log?kind=judgments").json()["entries"]
    assert [r["kind"] for r in only] == ["judgments"]


def test_rejects_non_object_bodies(setup):
    _, client, cfg = setup
    assert client.post("/api/plugins/highway_tweaks/log", json=[1, 2]).json() == {"ok": False}
    assert client.post("/api/plugins/highway_tweaks/log", content=b"not json",
                       headers={"Content-Type": "application/json"}).json() == {"ok": False}
    assert not (cfg / "highway_tweaks" / "jank_log.jsonl").exists()


def test_rolls_over_at_max_bytes(setup, monkeypatch):
    mod, client, cfg = setup
    monkeypatch.setattr(mod, "MAX_BYTES", 10)
    client.post("/api/plugins/highway_tweaks/log", json={"kind": "a", "pad": "x" * 50})
    client.post("/api/plugins/highway_tweaks/log", json={"kind": "b"})
    assert (cfg / "highway_tweaks" / "jank_log.old.jsonl").exists()
    assert [r["kind"] for r in client.get("/api/plugins/highway_tweaks/log").json()["entries"]] == ["b"]
