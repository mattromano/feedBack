"""Legacy guitar-encoded drums arrangements (old 0.2.x drumjoin packs).

Those packs keep their drums as a fretted arrangement (GM number packed as
`s * 24 + f`) plus a `drums` block with star power, fills, solos and the
hand-charted lower levels. The loader turns that block into an in-memory
drum_tab, marked `derived`, so the drums plugin gets star power and levels
without the pack being rewritten:

- `drums.tab_from_legacy_arrangement` maps notes, flags, ranges and levels,
- `load_song` uses it only when the pack has no real drum tab, and keeps the
  arrangement list as it was,
- the highway WS streams it as a `drum_tab` frame with `derived`.
"""

from __future__ import annotations

import importlib
import json
import sys
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import drums as drums_mod
import sloppak as sloppak_mod


def _note(t, gm, **flags):
    s, f = divmod(gm, 24)
    return {"t": t, "s": s, "f": f, "sus": 0.0, "ac": False, "mt": False, **flags}


LEGACY_ARR = {
    "name": "Drums",
    "tuning": [0, 0, 0, 0, 0, 0],
    "capo": 0,
    "notes": [
        _note(1.0, 36),               # kick
        _note(1.0, 46),               # open hi-hat — must not merge into closed
        _note(1.5, 38, ac=True),      # accented snare
        _note(1.75, 38, mt=True),     # ghost snare
        _note(2.0, 35),               # double kick
        _note(2.0, 39),               # hand clap — the old joiner played it as snare
        _note(2.5, 81),               # unmapped GM note — dropped
        _note(1.0, 36),               # duplicate — dropped
    ],
    "chords": [{"t": 3.0, "notes": [_note(3.0, 49), _note(3.0, 36)]}],
    "anchors": [], "handshapes": [], "templates": [],
    "drums": {
        "version": 1,
        "pro": True,
        "kick2x": True,
        "star_power": [[1.0, 2.5], [8.0, 9.0]],
        "fills": [[5.0, 6.0]],
        "solos": [[10.0, 12.0]],
        "levels": {
            "easy": [[1.0, 36, 0]],
            "medium": [[1.0, 36, 0], [1.5, 38, 1], [1.75, 38, 2]],
            "hard": [[1.0, 36, 0], [2.0, 35, 0]],
        },
        "levels_generated": ["easy"],
        "source": {"kind": "chorus"},
    },
}

LEAD_ARR = {
    "name": "Lead", "tuning": [0, 0, 0, 0, 0, 0], "capo": 0,
    "notes": [], "chords": [], "anchors": [], "handshapes": [], "templates": [],
}


# ── The converter ─────────────────────────────────────────────────────────────

def test_hits_flags_and_dedupe():
    tab = drums_mod.tab_from_legacy_arrangement(LEGACY_ARR)
    assert tab["version"] == drums_mod.SCHEMA_VERSION
    assert tab["hits"] == [
        {"t": 1.0, "p": "hh_open"},
        {"t": 1.0, "p": "kick"},
        {"t": 1.5, "p": "snare", "a": True},
        {"t": 1.75, "p": "snare", "g": True},
        {"t": 2.0, "p": "kick", "x2": True},
        {"t": 2.0, "p": "snare"},
        {"t": 3.0, "p": "crash_l"},
        {"t": 3.0, "p": "kick"},
    ]
    assert {k["id"] for k in tab["kit"]} == {"hh_open", "kick", "snare", "crash_l"}


def test_ranges_and_levels():
    tab = drums_mod.tab_from_legacy_arrangement(LEGACY_ARR)
    assert drums_mod.ranges_to_wire(tab["star_power"]) == [
        {"start_time": 1.0, "end_time": 2.5}, {"start_time": 8.0, "end_time": 9.0},
    ]
    assert drums_mod.ranges_to_wire(tab["fills"]) == [{"start_time": 5.0, "end_time": 6.0}]
    assert drums_mod.ranges_to_wire(tab["solos"]) == [{"start_time": 10.0, "end_time": 12.0}]
    levels = drums_mod.levels_to_wire(tab["levels"])
    assert levels["easy"] == {"hits": [{"t": 1.0, "p": "kick"}], "generated": True}
    assert levels["medium"]["hits"] == [
        {"t": 1.0, "p": "kick"}, {"t": 1.5, "p": "snare", "a": True}, {"t": 1.75, "p": "snare", "g": True},
    ]
    assert levels["medium"]["generated"] is False
    # x2 never rides on a level hit
    assert levels["hard"]["hits"] == [{"t": 1.0, "p": "kick"}, {"t": 2.0, "p": "kick"}]


@pytest.mark.parametrize("data", [
    None,
    LEAD_ARR,
    {**LEGACY_ARR, "drums": {**LEGACY_ARR["drums"], "version": 2}},
    {**LEGACY_ARR, "notes": [_note(1.0, 81)], "chords": []},
])
def test_not_a_legacy_drums_arrangement(data):
    assert drums_mod.tab_from_legacy_arrangement(data) is None


# ── The loader ────────────────────────────────────────────────────────────────

def _manifest(extra=None):
    m = {
        "title": "Legacy", "artist": "Tester", "album": "", "year": 2026, "duration": 20.0,
        "arrangements": [
            {"id": "lead", "name": "Lead", "file": "arrangements/lead.json"},
            {"id": "drums", "name": "Drums", "file": "arrangements/drums.json"},
        ],
        "stems": [],
    }
    m.update(extra or {})
    return m


def _files(manifest, drum_tab=None):
    files = {
        "manifest.yaml": yaml.safe_dump(manifest, sort_keys=False),
        "arrangements/lead.json": json.dumps(LEAD_ARR),
        "arrangements/drums.json": json.dumps(LEGACY_ARR),
    }
    if drum_tab is not None:
        files["drum_tab.json"] = json.dumps(drum_tab)
    return files


def _write_dir(root: Path, name: str, files: dict) -> Path:
    pak = root / f"{name}.sloppak"
    for rel, text in files.items():
        (pak / rel).parent.mkdir(parents=True, exist_ok=True)
        (pak / rel).write_text(text)
    return pak


def _write_zip(root: Path, name: str, files: dict) -> Path:
    pak = root / f"{name}.sloppak"
    with zipfile.ZipFile(pak, "w") as z:
        for rel, text in files.items():
            z.writestr(rel, text)
    return pak


def _load(pak: Path, tmp_path: Path):
    cache = tmp_path / "cache"
    cache.mkdir(exist_ok=True)
    return sloppak_mod.load_song(pak.name, pak.parent, cache)


@pytest.mark.parametrize("writer", [_write_dir, _write_zip])
def test_load_song_derives_the_tab(tmp_path, writer):
    pak = writer(tmp_path, f"legacy-{writer.__name__}", _files(_manifest()))
    loaded = _load(pak, tmp_path)
    assert loaded.drum_tab_derived == drums_mod.LEGACY_DERIVED
    assert loaded.drum_tab["star_power"] == [{"start_time": 1.0, "end_time": 2.5},
                                             {"start_time": 8.0, "end_time": 9.0}]
    assert loaded.drum_parts is None
    # the arrangement list is untouched: Drums is still a plain arrangement
    assert sorted(a.name for a in loaded.song.arrangements) == ["Drums", "Lead"]


def test_a_real_drum_tab_wins(tmp_path):
    tab = {"version": 1, "name": "Drums", "hits": [{"t": 0.5, "p": "kick"}]}
    pak = _write_dir(tmp_path, "with-tab", _files(_manifest({"drum_tab": "drum_tab.json"}), tab))
    loaded = _load(pak, tmp_path)
    assert loaded.drum_tab_derived is None
    assert loaded.drum_tab["hits"] == tab["hits"]


def test_a_pack_without_legacy_drums_is_unchanged(tmp_path):
    files = _files(_manifest())
    files["arrangements/drums.json"] = json.dumps({**LEGACY_ARR, "drums": None})
    loaded = _load(_write_dir(tmp_path, "plain", files), tmp_path)
    assert loaded.drum_tab is None and loaded.drum_tab_derived is None


# ── The highway WS ────────────────────────────────────────────────────────────

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


def test_ws_streams_the_derived_tab(make_client):
    server = make_client()
    _write_zip(server._get_dlc_dir(), "legacyws", _files(_manifest()))
    with TestClient(server.app) as client:
        frames = _frames(client, "/ws/highway/legacyws.sloppak?arrangement=0")
    types = [f["type"] for f in frames]

    info = next(f for f in frames if f["type"] == "song_info")
    assert info["has_drum_tab"] is True
    assert "Drums" in [a["name"] for a in info["arrangements"]]

    tab = frames[types.index("drum_tab")]
    assert tab["derived"] == drums_mod.LEGACY_DERIVED
    assert tab["star_power"] == [{"start_time": 1.0, "end_time": 2.5}, {"start_time": 8.0, "end_time": 9.0}]
    assert tab["fills"] == [{"start_time": 5.0, "end_time": 6.0}]
    assert tab["solos"] == [{"start_time": 10.0, "end_time": 12.0}]
    assert tab["levels"] == {
        "easy": {"total": 1, "generated": True},
        "medium": {"total": 3, "generated": False},
        "hard": {"total": 2, "generated": False},
    }
    hits = [h for f in frames if f["type"] == "drum_hits" for h in f["data"]]
    assert len(hits) == 8
    assert {"t": 2.0, "p": "kick", "x2": True} in hits
    assert {f["level"] for f in frames if f["type"] == "drum_level_hits"} == {"easy", "medium", "hard"}
