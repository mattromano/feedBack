"""Five-fret parts on the highway WebSocket.

`song_info.five_fret_parts` always lists the pack's parts; a part streams only
when asked for with `?five_fret_part=<id>` (opt-in, unlike drums): a
`five_fret` header frame, `five_fret_notes` chunks (<=500), then per-level
`five_fret_level_notes` chunks.
"""

from __future__ import annotations

import importlib
import json
import sys

import pytest
import yaml
from fastapi.testclient import TestClient



DRUM_TAB = {
    "version": 1,
    "name": "Drums",
    "kit": [{"id": "kick", "name": "Kick"}, {"id": "snare", "name": "Snare"}],
    "hits": [{"t": 0.5, "p": "kick"}, {"t": 0.75, "p": "snare"}],
    "levels": {"easy": {"hits": [{"t": 0.5, "p": "kick"}], "generated": True}},
}


def _write_drum_pack(dlc_root, tab, name):
    pak = dlc_root / f"{name}.sloppak"
    pak.mkdir()
    (pak / "drum_tab.json").write_text(json.dumps(tab))
    manifest = {
        "title": "Drum WS Test", "artist": "Tester", "album": "", "year": 2026,
        "duration": 60.0, "stems": [], "drum_tab": "drum_tab.json",
        "arrangements": [{"id": "drums", "name": "Drums", "type": "drums", "drum_tab": "drum_tab.json"}],
    }
    (pak / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return pak


def _chart(n_notes=3, levels=None, **extra):
    data = {
        "version": 1,
        "name": "Lead 5",
        "instrument": "guitar",
        "source": {"kind": "chart"},
        "notes": [{"t": round(0.5 + i * 0.01, 3), "f": 1 << (i % 5)} for i in range(n_notes)],
    }
    if levels:
        data["levels"] = levels
    data.update(extra)
    return data


def _write_pack(dlc_root, charts, name="fftest", with_lead=True):
    """charts: {part_id: chart dict}. Lead arrangement is optional."""
    pak = dlc_root / f"{name}.sloppak"
    pak.mkdir()
    arrs = []
    if with_lead:
        (pak / "arrangements").mkdir()
        (pak / "arrangements" / "lead.json").write_text(json.dumps({
            "name": "Lead", "tuning": [0] * 6, "capo": 0, "notes": [], "chords": [],
            "anchors": [], "handshapes": [], "templates": [], "beats": [], "sections": [],
        }))
        arrs.append({"id": "lead", "name": "Lead", "file": "arrangements/lead.json"})
    for pid, chart in charts.items():
        rel = f"five_fret_{pid}.json"
        (pak / rel).write_text(json.dumps(chart))
        arrs.append({"id": pid, "name": chart.get("name", pid), "type": "five_fret",
                     "instrument": chart.get("instrument", "guitar"), "five_fret": rel})
    manifest = {"title": "FF WS Test", "artist": "Tester", "album": "", "year": 2026,
                "duration": 60.0, "arrangements": arrs, "stems": []}
    (pak / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return pak


@pytest.fixture()
def make_client(tmp_path, monkeypatch):
    def _make():
        monkeypatch.setenv("CONFIG_DIR", str(tmp_path / "config"))
        monkeypatch.setenv("DLC_DIR", str(tmp_path / "dlc"))
        monkeypatch.setenv("FEEDBACK_SYNC_STARTUP", "1")
        sys.modules.pop("server", None)
        server = importlib.import_module("server")
        monkeypatch.setattr(server, "load_plugins", lambda *a, **kw: None)
        monkeypatch.setattr(server, "startup_scan", lambda: None)
        monkeypatch.setattr(server, "SLOPPAK_CACHE_DIR", tmp_path / "cache")
        import appstate as _appstate
        monkeypatch.setattr(_appstate, "sloppak_cache_dir", tmp_path / "cache")
        return server

    (tmp_path / "dlc").mkdir()
    yield _make
    server = sys.modules.get("server")
    conn = getattr(getattr(server, "meta_db", None), "conn", None)
    if conn is not None:
        getattr(server, "_join_background_db_threads", lambda: None)()
        conn.close()


def _frames(client, path, limit=400):
    out = []
    with client.websocket_connect(path) as ws:
        for _ in range(limit):
            msg = ws.receive_json()
            if msg.get("error"):
                raise AssertionError(f"WS error frame: {msg}")
            if msg.get("type") == "loading":
                continue
            out.append(msg)
            if msg.get("type") == "ready":
                break
    assert out and out[-1]["type"] == "ready", [f.get("type") for f in out]
    return out


def _by_type(frames, t):
    return [f for f in frames if f["type"] == t]


def test_song_info_lists_parts(make_client):
    server = make_client()
    _write_pack(server._get_dlc_dir(), {"lead5": _chart()})
    with TestClient(server.app) as client:
        frames = _frames(client, "/ws/highway/fftest.sloppak?arrangement=0")
    info = _by_type(frames, "song_info")[0]
    assert info["five_fret_parts"] == [
        {"id": "lead5", "name": "Lead 5", "instrument": "guitar", "source_kind": "chart"}
    ]
    # listing alone never streams a part
    assert not [f for f in frames if f["type"].startswith("five_fret")]


def test_song_info_empty_list_without_parts(make_client):
    server = make_client()
    _write_drum_pack(server._get_dlc_dir(), DRUM_TAB, name="nodr")
    with TestClient(server.app) as client:
        frames = _frames(client, "/ws/highway/nodr.sloppak?arrangement=0")
    assert _by_type(frames, "song_info")[0]["five_fret_parts"] == []


def test_stream_selected_part_chunks(make_client):
    server = make_client()
    chart = _chart(1201, star_power=[{"start_time": 1.0, "end_time": 2.0}],
                   solos=[{"start_time": 3.0, "end_time": 4.0}])
    _write_pack(server._get_dlc_dir(), {"lead5": chart})
    with TestClient(server.app) as client:
        frames = _frames(client, "/ws/highway/fftest.sloppak?arrangement=0&five_fret_part=lead5")
    types = [f["type"] for f in frames]
    head = _by_type(frames, "five_fret")
    assert len(head) == 1
    head = head[0]
    assert head["part_id"] == "lead5"
    assert head["name"] == "Lead 5"
    assert head["instrument"] == "guitar"
    assert head["source_kind"] == "chart"
    assert head["total"] == 1201
    assert head["star_power"] == [{"start_time": 1.0, "end_time": 2.0}]
    assert head["solos"] == [{"start_time": 3.0, "end_time": 4.0}]
    assert "levels" not in head
    chunks = _by_type(frames, "five_fret_notes")
    assert [len(c["notes"]) for c in chunks] == [500, 500, 201]
    assert types.index("five_fret") < types.index("five_fret_notes")
    assert sum(len(c["notes"]) for c in chunks) == head["total"]
    assert chunks[0]["notes"][0] == {"t": 0.5, "f": 1}


def test_levels_stream(make_client):
    server = make_client()
    levels = {
        "easy": {"notes": [{"t": 0.5, "f": 1}], "generated": True},
        "hard": {"notes": [{"t": 0.5, "f": 1}, {"t": 0.75, "f": 2}]},
    }
    _write_pack(server._get_dlc_dir(), {"lead5": _chart(levels=levels)})
    with TestClient(server.app) as client:
        frames = _frames(client, "/ws/highway/fftest.sloppak?arrangement=0&five_fret_part=lead5")
    types = [f["type"] for f in frames]
    head = _by_type(frames, "five_fret")[0]
    assert head["levels"] == {
        "easy": {"total": 1, "generated": True},
        "hard": {"total": 2, "generated": False},
    }
    lv = _by_type(frames, "five_fret_level_notes")
    by_level: dict[str, list] = {}
    for f in lv:
        by_level.setdefault(f["level"], []).extend(f["notes"])
    assert by_level["easy"] == [{"t": 0.5, "f": 1}]
    assert by_level["hard"] == [{"t": 0.5, "f": 1}, {"t": 0.75, "f": 2}]
    # level notes follow the top-level notes
    assert max(i for i, t in enumerate(types) if t == "five_fret_notes") < min(
        i for i, t in enumerate(types) if t == "five_fret_level_notes")


def test_unknown_part_sends_nothing(make_client):
    server = make_client()
    _write_pack(server._get_dlc_dir(), {"lead5": _chart()})
    with TestClient(server.app) as client:
        frames = _frames(client, "/ws/highway/fftest.sloppak?arrangement=0&five_fret_part=nope")
    assert not [f for f in frames if f["type"].startswith("five_fret")]


def test_only_five_fret_pack_streams(make_client):
    """A pack whose only arrangements are five_fret entries has no fretted
    arrangements; the WS must still send song_info and stream the part."""
    server = make_client()
    _write_pack(server._get_dlc_dir(), {"lead5": _chart()}, name="ffonly", with_lead=False)
    with TestClient(server.app) as client:
        frames = _frames(client, "/ws/highway/ffonly.sloppak?five_fret_part=lead5")
    info = _by_type(frames, "song_info")[0]
    assert [p["id"] for p in info["five_fret_parts"]] == ["lead5"]
    assert _by_type(frames, "five_fret")[0]["total"] == 3
    assert sum(len(c["notes"]) for c in _by_type(frames, "five_fret_notes")) == 3


def test_drum_stream_unchanged(make_client):
    server = make_client()
    _write_drum_pack(server._get_dlc_dir(), DRUM_TAB, name="drumsame")
    with TestClient(server.app) as client:
        frames = _frames(client, "/ws/highway/drumsame.sloppak?arrangement=0")
    types = {f["type"] for f in frames}
    assert {"drum_tab", "drum_hits", "drum_level_hits"} <= types
    assert not [t for t in types if t.startswith("five_fret")]
    assert _by_type(frames, "drum_tab")[0]["total"] == 2
