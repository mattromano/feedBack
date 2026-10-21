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
    rows = dict(db.conn.execute("SELECT filename, five_fret_parts FROM songs").fetchall())
    assert rows["old.archive"] == "[]"
    # The sloppak row stays NULL until the scanner re-extracts it.
    assert rows["old.sloppak"] is None
    assert db.get("old.sloppak", 1.0, 1)["five_fret_parts"] is None


def test_parse_five_fret():
    for ok in ("any", "guitar", "rhythm", "bass", "bass:chart", "guitar:auto", " BASS:Chart "):
        assert _parse_five_fret(ok) == ok.strip().lower()
    for bad in ("", "drums", "bass:real", "any:chart", ":chart", "x"):
        assert _parse_five_fret(bad) is None


# ── API filter ──────────────────────────────────────────────────────────────

_SONGS = (
    ("both.sloppak", ["guitar:chart", "bass:auto"]),
    ("realbass.sloppak", ["bass:chart"]),
    ("rhythm.sloppak", ["rhythm:auto"]),
    ("none.sloppak", []),
)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    sys.modules.pop("server", None)
    mod = importlib.import_module("server")
    for fn, parts in _SONGS:
        mod.meta_db.put(fn, 1.0, 1, {"title": fn, "artist": fn, "album": f"Album {fn}",
                                     "format": "sloppak", "five_fret_parts": parts})
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


def test_filter_any(client):
    assert _files(client, five_fret="any") == {"both.sloppak", "realbass.sloppak", "rhythm.sloppak"}


def test_filter_part(client):
    assert _files(client, five_fret="bass") == {"both.sloppak", "realbass.sloppak"}
    assert _files(client, five_fret="guitar") == {"both.sloppak"}
    assert _files(client, five_fret="rhythm") == {"rhythm.sloppak"}


def test_filter_part_real_only(client):
    # an auto bass doesn't match bass:chart
    assert _files(client, five_fret="bass:chart") == {"realbass.sloppak"}
    assert _files(client, five_fret="bass:auto") == {"both.sloppak"}
    assert _files(client, five_fret="guitar:chart") == {"both.sloppak"}


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
    assert {s.get("title") for s in rows} == {"both.sloppak", "realbass.sloppak", "rhythm.sloppak"}


def test_rescan_updates(tmp_path, client):
    """Index a pack with no parts, add one to the pack, rescan -> it matches."""
    import server
    from scan_worker import _extract_meta_sloppak

    pak = _write_pak(tmp_path / "pk", [])
    server.meta_db.put("pk.sloppak", 1.0, 1, _extract_meta_sloppak(pak))
    assert "pk.sloppak" not in _files(client, five_fret="bass")

    _write_pak(tmp_path / "pk2", [("bass", "chart")])
    rescanned = _extract_meta_sloppak(tmp_path / "pk2" / "pk2.sloppak")
    server.meta_db.put("pk.sloppak", 2.0, 1, rescanned)
    assert "pk.sloppak" in _files(client, five_fret="bass:chart")
