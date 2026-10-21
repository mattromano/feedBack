"""The library knows which songs carry per-syllable vocal pitch (spec §7.2).

The Karaoke-ready filter used to mean "synced lyrics + a vocals stem", which
also listed songs whose pitch had never been extracted — the lyrics plugin's
singing mode stays off on those. `has_lyrics=pitch` narrows the lyrics facet
to songs whose manifest declares `vocal_pitch`, i.e. the songs singing mode
actually runs on.
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
from metadata_db import MetadataDB


def _write_pak(root: Path, extras: dict) -> Path:
    pak = root / f"{root.name}.sloppak"
    (pak / "arrangements").mkdir(parents=True)
    (pak / "arrangements" / "lead.json").write_text(json.dumps({
        "name": "Lead", "tuning": [0] * 6, "capo": 0, "notes": [], "chords": [],
        "anchors": [], "handshapes": [], "templates": [], "beats": [], "sections": [],
    }))
    manifest = {"title": "T", "artist": "A", "album": "", "duration": 1.0,
                "arrangements": [{"id": "lead", "name": "Lead", "file": "arrangements/lead.json"}],
                **extras}
    (pak / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    return pak


def test_extract_meta_reports_vocal_pitch(tmp_path):
    pak = _write_pak(tmp_path, {"lyrics": "lyrics.json", "vocal_pitch": "vocal_pitch.json"})
    assert sloppak_mod.extract_meta(pak)["has_vocal_pitch"] is True


def test_extract_meta_without_vocal_pitch(tmp_path):
    pak = _write_pak(tmp_path, {"lyrics": "lyrics.json"})
    assert sloppak_mod.extract_meta(pak)["has_vocal_pitch"] is False


def test_put_get_round_trips_vocal_pitch(tmp_path):
    db = MetadataDB(tmp_path)
    db.put("p.sloppak", 1.0, 1, {"title": "P", "has_lyrics": True, "has_vocal_pitch": True})
    db.put("n.sloppak", 1.0, 1, {"title": "N", "has_lyrics": True})
    assert db.get("p.sloppak", 1.0, 1)["has_vocal_pitch"] is True
    # put() never writes NULL: a freshly-scanned row without pitch is 0, not
    # "never extracted", so the scanner doesn't re-queue it forever.
    assert db.get("n.sloppak", 1.0, 1)["has_vocal_pitch"] is False


def test_rows_predating_the_column_are_marked_for_reextraction(tmp_path):
    """A library scanned before the column existed must be backfilled: sloppak
    rows come back NULL (re-extracted by the scanner), other formats can't carry
    vocal pitch and are settled at 0 without a rescan."""
    conn = sqlite3.connect(tmp_path / "web_library.db")
    conn.execute("CREATE TABLE songs (filename TEXT PRIMARY KEY, mtime REAL, size INTEGER, "
                 "title TEXT, artist TEXT, album TEXT, year TEXT, duration REAL, tuning TEXT, "
                 "arrangements TEXT, has_lyrics INTEGER DEFAULT 0, format TEXT DEFAULT 'archive')")
    conn.execute("INSERT INTO songs VALUES ('old.sloppak', 1.0, 1, 'Old', 'A', '', '', 1.0, '', '[]', 1, 'sloppak')")
    conn.execute("INSERT INTO songs VALUES ('old.archive', 1.0, 1, 'Old', 'A', '', '', 1.0, '', '[]', 1, 'archive')")
    conn.commit()
    conn.close()

    db = MetadataDB(tmp_path)
    rows = dict(db.conn.execute("SELECT filename, has_vocal_pitch FROM songs").fetchall())
    assert "has_vocal_pitch" in MetadataDB._EXTRACTION_MARKER_COLS
    assert rows["old.archive"] == 0
    # The sloppak row stays NULL until the scanner re-extracts it.
    assert db.get("old.sloppak", 1.0, 1)["has_vocal_pitch"] is None


# ── API: has_lyrics=pitch ───────────────────────────────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    sys.modules.pop("server", None)
    mod = importlib.import_module("server")
    for fn, lyrics, pitch in (("sing.sloppak", True, True),
                              ("words.sloppak", True, False),
                              ("inst.sloppak", False, False)):
        mod.meta_db.put(fn, 1.0, 1, {"title": fn, "artist": "A", "format": "sloppak",
                                     "has_lyrics": lyrics, "has_vocal_pitch": pitch,
                                     "stem_ids": ["vocals"], "stem_count": 1})
    c = TestClient(mod.app)
    try:
        yield c
    finally:
        c.close()
        getattr(mod, "_join_background_db_threads", lambda: None)()
        mod.meta_db.conn.close()


def _files(client, **kw):
    return {s["filename"] for s in client.get("/api/library", params=kw).json()["songs"]}


def test_has_lyrics_pitch_lists_only_singable_songs(client):
    assert _files(client, has_lyrics="pitch") == {"sing.sloppak"}
    assert _files(client, has_lyrics="pitch", stems_has="vocals") == {"sing.sloppak"}


def test_has_lyrics_require_still_ignores_pitch(client):
    assert _files(client, has_lyrics="1") == {"sing.sloppak", "words.sloppak"}
    assert _files(client, has_lyrics="0") == {"inst.sloppak"}
