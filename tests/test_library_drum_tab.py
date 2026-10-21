"""The library knows which songs carry a drum chart.

Mirrors the runtime `has_drum_tab` (ws_highway.py): a song-level `drum_tab`,
or an arrangement entry typed drums that carries a `drum_tab` pointer. A
drums-typed entry without a pointer is dropped by load_song, so it is not a
drum chart here either.
"""

import json
import sqlite3
from pathlib import Path

import yaml

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


def _with_drums_entry(entry: dict) -> dict:
    return {"arrangements": [
        {"id": "lead", "name": "Lead", "file": "arrangements/lead.json"}, entry]}


def test_extract_meta_reports_song_level_drum_tab(tmp_path):
    pak = _write_pak(tmp_path, {"drum_tab": "drum_tab.json"})
    assert sloppak_mod.extract_meta(pak)["has_drum_tab"] is True


def test_extract_meta_reports_drums_arrangement(tmp_path):
    pak = _write_pak(tmp_path, _with_drums_entry(
        {"id": "drums", "name": "Drums", "type": "drums", "drum_tab": "drums.json"}))
    assert sloppak_mod.extract_meta(pak)["has_drum_tab"] is True


def test_extract_meta_drums_entry_without_pointer_is_not_a_drum_chart(tmp_path):
    pak = _write_pak(tmp_path, _with_drums_entry(
        {"id": "drums", "name": "Drums", "type": "drums"}))
    assert sloppak_mod.extract_meta(pak)["has_drum_tab"] is False


def test_extract_meta_without_drums(tmp_path):
    pak = _write_pak(tmp_path, {})
    assert sloppak_mod.extract_meta(pak)["has_drum_tab"] is False


def test_put_get_round_trips_drum_tab(tmp_path):
    db = MetadataDB(tmp_path)
    db.put("d.sloppak", 1.0, 1, {"title": "D", "has_drum_tab": True})
    db.put("n.sloppak", 1.0, 1, {"title": "N"})
    assert db.get("d.sloppak", 1.0, 1)["has_drum_tab"] is True
    # put() never writes NULL: a freshly-scanned row without a drum chart is 0,
    # not "never extracted", so the scanner doesn't re-queue it forever.
    assert db.get("n.sloppak", 1.0, 1)["has_drum_tab"] is False


def test_rows_predating_the_column_are_marked_for_reextraction(tmp_path):
    conn = sqlite3.connect(tmp_path / "web_library.db")
    conn.execute("CREATE TABLE songs (filename TEXT PRIMARY KEY, mtime REAL, size INTEGER, "
                 "title TEXT, artist TEXT, album TEXT, year TEXT, duration REAL, tuning TEXT, "
                 "arrangements TEXT, has_lyrics INTEGER DEFAULT 0, format TEXT DEFAULT 'archive')")
    conn.execute("INSERT INTO songs VALUES ('old.sloppak', 1.0, 1, 'Old', 'A', '', '', 1.0, '', '[]', 1, 'sloppak')")
    conn.execute("INSERT INTO songs VALUES ('old.archive', 1.0, 1, 'Old', 'A', '', '', 1.0, '', '[]', 1, 'archive')")
    conn.commit()
    conn.close()

    db = MetadataDB(tmp_path)
    rows = dict(db.conn.execute("SELECT filename, has_drum_tab FROM songs").fetchall())
    assert "has_drum_tab" in MetadataDB._EXTRACTION_MARKER_COLS
    assert rows["old.archive"] == 0
    # The sloppak row stays NULL until the scanner re-extracts it.
    assert db.get("old.sloppak", 1.0, 1)["has_drum_tab"] is None
