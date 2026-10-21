"""The library indexes a pack's five-fret parts and filters on them.

`five_fret_parts` is a JSON list of "<instrument>:<kind>" (kind = the part
file's source.kind, chart|auto). The `five_fret` filter grammar: `any`,
`guitar|rhythm|bass` (real or auto), `<instrument>:chart|auto` (that source only).
"""

import importlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import sloppak as sloppak_mod
from library_registry import _parse_five_fret
from metadata_db import MetadataDB


def _write_pak(root: Path, parts: list[tuple[str, str]], write_files: bool = True) -> Path:
    """parts: (instrument, source kind) per five_fret arrangement entry."""
    pak = root / f"{root.name}.sloppak"
    (pak / "arrangements").mkdir(parents=True)
    (pak / "arrangements" / "lead.json").write_text(json.dumps({
        "name": "Lead", "tuning": [0] * 6, "capo": 0, "notes": [], "chords": [],
        "anchors": [], "handshapes": [], "templates": [], "beats": [], "sections": [],
    }))
    arrs = [{"id": "lead", "name": "Lead", "file": "arrangements/lead.json"}]
    for inst, kind in parts:
        rel = f"five_fret_{inst}.json"
        arrs.append({"id": f"ff_{inst}", "name": inst.title(), "type": "five_fret",
                     "instrument": inst, "five_fret": rel})
        if write_files:
            (pak / rel).write_text(json.dumps({"source": {"kind": kind}, "notes": []}))
    (pak / "manifest.yaml").write_text(yaml.safe_dump(
        {"title": "T", "artist": "A", "album": "", "duration": 1.0, "arrangements": arrs}))
    return pak


def test_meta_lists_parts(tmp_path):
    pak = _write_pak(tmp_path, [("guitar", "chart"), ("bass", "auto")])
    assert sloppak_mod.extract_meta(pak)["five_fret_parts"] == ["guitar:chart", "bass:auto"]


def test_meta_defaults_to_chart_when_file_unreadable(tmp_path):
    pak = _write_pak(tmp_path, [("rhythm", "auto")], write_files=False)
    assert sloppak_mod.extract_meta(pak)["five_fret_parts"] == ["rhythm:chart"]


def test_meta_without_parts(tmp_path):
    assert sloppak_mod.extract_meta(_write_pak(tmp_path, []))["five_fret_parts"] == []


def test_meta_reads_zipped_pack(tmp_path):
    import zipfile
    src = _write_pak(tmp_path / "src", [("bass", "auto")])
    z = tmp_path / "z.sloppak"
    with zipfile.ZipFile(z, "w") as zf:
        for f in src.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(src).as_posix())
    assert sloppak_mod.extract_meta(z)["five_fret_parts"] == ["bass:auto"]


def test_meta_five_fret_only(tmp_path):
    # the helper's "Lead" fretted arrangement makes it a real-guitar pack
    assert sloppak_mod.extract_meta(_write_pak(tmp_path / "a", [("bass", "chart")]))["five_fret_only"] is False
    assert sloppak_mod.extract_meta(_write_pak(tmp_path / "b", []))["five_fret_only"] is False
    only = _pack(tmp_path / "c", [_ff("guitar", "Plastic Guitar"), _ff("bass", "Plastic Bass")])
    assert sloppak_mod.extract_meta(only)["five_fret_only"] is True
    # a drum tab means it's not 5-fret-only
    with_drums = _pack(tmp_path / "d", [
        {"id": "d", "name": "Drums", "type": "drums", "drum_tab": "d.json"}, _ff("guitar", "G")])
    assert sloppak_mod.extract_meta(with_drums)["five_fret_only"] is False


def test_put_get_round_trips_five_fret_only(tmp_path):
    db = MetadataDB(tmp_path)
    db.put("o.sloppak", 1.0, 1, {"title": "O", "five_fret_parts": ["guitar:chart"], "five_fret_only": True})
    db.put("n.sloppak", 1.0, 1, {"title": "N"})
    assert db.get("o.sloppak", 1.0, 1)["five_fret_only"] is True
    # put() never writes NULL: "not only" is False, not "never extracted".
    assert db.get("n.sloppak", 1.0, 1)["five_fret_only"] is False


def test_put_get_round_trips_parts(tmp_path):
    db = MetadataDB(tmp_path)
    db.put("p.sloppak", 1.0, 1, {"title": "P", "five_fret_parts": ["guitar:chart"]})
    db.put("n.sloppak", 1.0, 1, {"title": "N"})
    assert db.get("p.sloppak", 1.0, 1)["five_fret_parts"] == ["guitar:chart"]
    # put() never writes NULL: "no parts" is [], not "never extracted".
    assert db.get("n.sloppak", 1.0, 1)["five_fret_parts"] == []


def test_migration_adds_column(tmp_path):
    conn = sqlite3.connect(tmp_path / "web_library.db")
    conn.execute("CREATE TABLE songs (filename TEXT PRIMARY KEY, mtime REAL, size INTEGER, "
                 "title TEXT, artist TEXT, album TEXT, year TEXT, duration REAL, tuning TEXT, "
                 "arrangements TEXT, has_lyrics INTEGER DEFAULT 0, format TEXT DEFAULT 'archive')")
    conn.execute("INSERT INTO songs VALUES ('old.sloppak', 1.0, 1, 'Old', 'A', '', '', 1.0, '', '[]', 1, 'sloppak')")
    conn.execute("INSERT INTO songs VALUES ('old.archive', 1.0, 1, 'Old', 'A', '', '', 1.0, '', '[]', 1, 'archive')")
    conn.commit()
    conn.close()

    db = MetadataDB(tmp_path)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(songs)")}
    assert "five_fret_parts" in cols
    assert "five_fret_parts" in MetadataDB._EXTRACTION_MARKER_COLS
    assert "five_fret_only" in cols
    assert "five_fret_only" in MetadataDB._EXTRACTION_MARKER_COLS
    rows = dict(db.conn.execute("SELECT filename, five_fret_parts FROM songs").fetchall())
    assert rows["old.archive"] == "[]"
    # The sloppak row stays NULL until the scanner re-extracts it.
    assert rows["old.sloppak"] is None
    assert db.get("old.sloppak", 1.0, 1)["five_fret_parts"] is None
    assert db.get("old.sloppak", 1.0, 1)["five_fret_only"] is None
    assert db.conn.execute("SELECT five_fret_only FROM songs WHERE filename='old.archive'").fetchone()[0] == 0


def test_parse_five_fret():
    for ok in ("any", "guitar", "rhythm", "bass", "bass:chart", "guitar:auto", " BASS:Chart "):
        assert _parse_five_fret(ok) == (ok.strip().lower(),)
    for bad in ("", "drums", "bass:real", ":chart", "x", "any:real"):
        assert _parse_five_fret(bad) is None


def test_parse_five_fret_list():
    assert _parse_five_fret("guitar,bass") == ("guitar", "bass")
    assert _parse_five_fret("any:chart") == ("any:chart",)
    assert _parse_five_fret("any:auto") == ("any:auto",)
    assert _parse_five_fret("bass,nope") == ("bass",)
    assert _parse_five_fret("nope") is None
    assert _parse_five_fret(" Guitar , BASS:chart ,") == ("guitar", "bass:chart")
    assert _parse_five_fret("bass,bass, bass") == ("bass",)
    assert _parse_five_fret(",,") is None


# ── API filter ──────────────────────────────────────────────────────────────

_SONGS = (
    ("both.sloppak", ["guitar:chart", "bass:auto"]),
    ("realbass.sloppak", ["bass:chart"]),
    ("rhythm.sloppak", ["rhythm:auto"]),
    ("guitaronly.sloppak", ["guitar:chart"]),
    ("none.sloppak", []),
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    sys.modules.pop("server", None)
    mod = importlib.import_module("server")
    for fn, parts in _SONGS:
        mod.meta_db.put(fn, 1.0, 1, {"title": fn, "artist": fn, "album": f"Album {fn}",
                                     "format": "sloppak", "five_fret_parts": parts,
                                     "five_fret_only": fn == "guitaronly.sloppak"})
    c = TestClient(mod.app)
    try:
        yield c
    finally:
        c.close()
        getattr(mod, "_join_background_db_threads", lambda: None)()
        mod.meta_db.conn.close()


def _files(client, **kw):
    return {s["filename"] for s in client.get("/api/library", params=kw).json()["songs"]}


def test_items_carry_parts(client):
    items = {s["filename"]: s for s in client.get("/api/library").json()["songs"]}
    assert items["both.sloppak"]["five_fret_parts"] == ["guitar:chart", "bass:auto"]
    assert items["none.sloppak"]["five_fret_parts"] == []
    assert items["both.sloppak"]["five_fret_only"] is False
    assert items["guitaronly.sloppak"]["five_fret_only"] is True


def test_filter_any(client):
    assert _files(client, five_fret="any") == {"both.sloppak", "realbass.sloppak", "rhythm.sloppak", "guitaronly.sloppak"}


def test_filter_part(client):
    assert _files(client, five_fret="bass") == {"both.sloppak", "realbass.sloppak"}
    assert _files(client, five_fret="guitar") == {"both.sloppak", "guitaronly.sloppak"}
    assert _files(client, five_fret="rhythm") == {"rhythm.sloppak"}


def test_filter_part_real_only(client):
    # an auto bass doesn't match bass:chart
    assert _files(client, five_fret="bass:chart") == {"realbass.sloppak"}
    assert _files(client, five_fret="bass:auto") == {"both.sloppak"}
    assert _files(client, five_fret="guitar:chart") == {"both.sloppak", "guitaronly.sloppak"}


def test_filter_and_list(client):
    # every entry must match: a guitar-only song fails "guitar,bass"
    assert _files(client, five_fret="guitar,bass") == {"both.sloppak"}
    assert "guitaronly.sloppak" not in _files(client, five_fret="guitar,bass")
    assert "guitaronly.sloppak" in _files(client, five_fret="guitar")
    assert _files(client, five_fret="bass,rhythm") == set()
    # invalid entries are dropped, not ANDed
    assert _files(client, five_fret="bass,nope") == _files(client, five_fret="bass")


def test_filter_any_chart(client):
    # an auto-only song fails any:chart; a real-chart part anywhere passes
    assert _files(client, five_fret="any:chart") == {"both.sloppak", "realbass.sloppak", "guitaronly.sloppak"}
    assert _files(client, five_fret="any:auto") == {"both.sloppak", "rhythm.sloppak"}
    assert _files(client, five_fret="any:chart,bass") == {"both.sloppak", "realbass.sloppak"}


def test_invalid_filter_is_ignored(client):
    everything = {fn for fn, _ in _SONGS}
    assert _files(client, five_fret="") == everything
    assert _files(client, five_fret="drums") == everything


def test_filter_reaches_every_endpoint(client):
    q = {"five_fret": "bass:chart"}
    assert client.get("/api/library/table", params=q).json()["total"] == 1
    assert client.get("/api/library/stats", params=q).json()["total_songs"] == 1
    assert client.get("/api/library/artists", params=q).json()["total_artists"] == 1
    assert client.get("/api/library/albums", params=q).json()["total"] == 1


def test_filter_in_grouped_mode(client):
    rows = client.get("/api/library", params={"five_fret": "any", "group": 1}).json()["songs"]
    assert {s.get("title") for s in rows} == {
        "both.sloppak", "realbass.sloppak", "rhythm.sloppak", "guitaronly.sloppak"}


@pytest.fixture()
def scan_env(tmp_path, monkeypatch, reset_scan_state):
    """Server + the real scanner (thread pool instead of spawn) over a dlc dir."""
    import concurrent.futures
    import scan as scan_mod

    dlc = tmp_path / "dlc"
    dlc.mkdir()
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("DLC_DIR", raising=False)
    (tmp_path / "config.json").write_text(json.dumps({"dlc_dir": str(dlc)}))
    sys.modules.pop("server", None)
    mod = importlib.import_module("server")
    monkeypatch.setattr(scan_mod, "_make_scan_executor",
                        lambda: concurrent.futures.ThreadPoolExecutor(max_workers=2))
    c = TestClient(mod.app)
    try:
        yield dlc, scan_mod, c
    finally:
        c.close()
        getattr(mod, "_join_background_db_threads", lambda: None)()
        mod.meta_db.conn.close()


def test_rescan_updates(scan_env):
    """Add a part to the SAME pack (new mtime) and rescan -> the song matches."""
    import os

    dlc, scan_mod, client = scan_env
    pak = _write_pak(dlc / "song", [])
    scan_mod.background_scan(force=True)
    assert _files(client, five_fret="bass:chart") == set()
    assert _files(client)  # the pack itself is indexed

    # Edit the pack in place: add a bass part, bump mtime.
    (pak / "five_fret_bass.json").write_text(json.dumps({"source": {"kind": "chart"}, "notes": []}))
    man = yaml.safe_load((pak / "manifest.yaml").read_text())
    man["arrangements"].append({"id": "ff_bass", "name": "Bass", "type": "five_fret",
                                "instrument": "bass", "five_fret": "five_fret_bass.json"})
    (pak / "manifest.yaml").write_text(yaml.safe_dump(man))
    future = pak.stat().st_mtime + 100
    for f in (pak, pak / "manifest.yaml"):
        os.utime(f, (future, future))
    scan_mod.background_scan(force=True)
    assert len(_files(client, five_fret="bass:chart")) == 1


# ── arrangement indexes stay aligned with load_song ─────────────────────────

def _pack(root: Path, entries: list[dict]) -> Path:
    """entries: manifest arrangement dicts; fretted ones get a file written."""
    pak = root / f"{root.name}.sloppak"
    (pak / "arrangements").mkdir(parents=True)
    for e in entries:
        t = e.get("type")
        if t == "five_fret":
            (pak / e["five_fret"]).write_text(json.dumps({
                "version": 1, "source": {"kind": "chart"}, "notes": [], "name": e.get("name", "")}))
        elif t in ("drums", "drum"):
            (pak / e["drum_tab"]).write_text(json.dumps({
                "version": 1, "name": "Drums", "kit": [{"id": "kick", "name": "Kick"}],
                "hits": [{"t": 1.0, "p": "kick", "v": 100}]}))
        else:
            (pak / e["file"]).write_text(json.dumps({
                "name": e["name"], "tuning": [0] * 6, "capo": 0, "notes": [], "chords": [],
                "anchors": [], "handshapes": [], "templates": [], "beats": [], "sections": []}))
    (pak / "manifest.yaml").write_text(yaml.safe_dump(
        {"title": "T", "artist": "A", "album": "", "duration": 1.0, "arrangements": entries}))
    return pak


def _fretted(name):
    return {"id": name.lower(), "name": name, "file": f"arrangements/{name.lower()}.json"}


def _ff(inst, name):
    return {"id": f"ff_{inst}", "name": name, "type": "five_fret",
            "instrument": inst, "five_fret": f"ff_{inst}.json"}


@pytest.mark.parametrize("entries", [
    pytest.param([_fretted("Lead"), _ff("bass", "Bass"), _fretted("Solo")], id="five_fret_before_fretted"),
    pytest.param([_ff("guitar", "Plastic Guitar"), _ff("bass", "Plastic Bass")], id="five_fret_only"),
    pytest.param([{"id": "d", "name": "Drums", "type": "drums", "drum_tab": "d.json"},
                  _ff("rhythm", "Rhythm"), _fretted("Lead"), _fretted("Bass")], id="drums_five_fret_fretted"),
])
def test_meta_arrangements_align_with_load_song(tmp_path, entries):
    pak = _pack(tmp_path / "p", entries)
    meta_names = [a["name"] for a in sloppak_mod.extract_meta(pak)["arrangements"]]
    loaded = sloppak_mod.load_song(pak.name, pak.parent, tmp_path / "cache")
    loaded_names = [a.name for a in loaded.song.arrangements]
    # Drums entries are listed LAST by design (pre-existing contract), so every
    # loadable arrangement keeps the index the highway uses; nothing else extra.
    assert meta_names[:len(loaded_names)] == loaded_names
    assert all(n == "Drums" for n in meta_names[len(loaded_names):])
    assert [a["index"] for a in sloppak_mod.extract_meta(pak)["arrangements"]] == list(range(len(meta_names)))
