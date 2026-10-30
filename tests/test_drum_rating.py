"""Library drum badges: the 0-6 drum tier (lib/drum_rating.py, estimated from the
Expert chart or authored in song.ini) and the generated-levels flag, through pack
metadata, the library DB and the table API."""

import json
import sqlite3
from pathlib import Path

import yaml

import clonehero
import drum_rating
import sloppak as sloppak_mod
from metadata_db import MetadataDB


def _beat(n, step, pads):
    return [{"t": round(i * step, 3), "p": p} for i in range(n) for p in pads(i)]


SPARSE = _beat(200, 0.6, lambda i: ["kick"] if i % 2 == 0 else ["snare"])
BUSY = _beat(1200, 0.075, lambda i: [["hh_closed", "tom_hi", "snare", "tom_floor", "crash_l", "ride"][i % 6]]
             + (["kick"] if i % 2 == 0 else []))


def test_estimate_tier_spans_the_scale():
    assert drum_rating.estimate_tier(SPARSE) == 0
    assert drum_rating.estimate_tier(BUSY) == 6
    assert drum_rating.estimate_tier([]) is None
    assert drum_rating.estimate_tier([{"t": "x", "p": "kick"}, {"p": "snare"}]) is None


def test_estimate_tier_rises_with_density():
    tiers = [drum_rating.estimate_tier(_beat(int(120 / step), step,
                                             lambda i: ["hh_closed"] + (["kick"] if i % 2 == 0 else ["snare"])))
             for step in (0.6, 0.3, 0.15, 0.1)]
    assert tiers == sorted(tiers) and tiers[0] < tiers[-1]


def test_levels_generated():
    assert drum_rating.levels_generated({"levels": {"easy": {"hits": [], "generated": True}}}) is True
    assert drum_rating.levels_generated({"levels": {"easy": {"hits": []}, "hard": {"generated": False}}}) is False
    assert drum_rating.levels_generated({}) is False
    assert drum_rating.levels_generated({"levels": ["bad"]}) is False


def test_ini_tier():
    assert drum_rating.ini_tier({"diff_drums": "4"}) == 4
    assert drum_rating.ini_tier({"diff_drums": "-1", "diff_drums_real": "3"}) == 3
    assert drum_rating.ini_tier({"diff_drums": "9"}) == 6
    assert drum_rating.ini_tier({"diff_drums": "-1"}) is None
    assert drum_rating.ini_tier({}) is None


def _pak(root: Path, tab: dict | None, *, as_arrangement=False) -> Path:
    pak = root / "song.sloppak"
    pak.mkdir(parents=True)
    manifest = {"title": "T", "artist": "A", "duration": 90.0, "arrangements": []}
    if tab is not None:
        (pak / "drum_tab.json").write_text(json.dumps(tab))
        if as_arrangement:
            manifest["arrangements"] = [{"id": "drums", "name": "Drums", "type": "drums",
                                         "drum_tab": "drum_tab.json"}]
        else:
            manifest["drum_tab"] = "drum_tab.json"
    (pak / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    return pak


def test_extract_meta_rates_the_drum_tab(tmp_path):
    tab = {"version": 1, "hits": BUSY, "levels": {"easy": {"hits": BUSY[:10], "generated": True}}}
    m = sloppak_mod.extract_meta(_pak(tmp_path, tab))
    assert (m["drum_tier"], m["drum_tier_estimated"], m["drum_levels_generated"]) == (6, True, True)


def test_extract_meta_rates_a_drums_arrangement(tmp_path):
    m = sloppak_mod.extract_meta(_pak(tmp_path, {"version": 1, "hits": SPARSE}, as_arrangement=True))
    assert (m["drum_tier"], m["drum_levels_generated"]) == (0, False)


def test_extract_meta_without_drums_or_with_a_broken_tab(tmp_path):
    m = sloppak_mod.extract_meta(_pak(tmp_path / "none", None))
    assert (m["drum_tier"], m["drum_tier_estimated"], m["drum_levels_generated"]) == (None, False, False)
    pak = _pak(tmp_path / "bad", {"version": 1, "hits": SPARSE})
    (pak / "drum_tab.json").write_text("{nope")
    m = sloppak_mod.extract_meta(pak)
    assert m["has_drum_tab"] is True and m["drum_tier"] is None


def test_clonehero_index_uses_the_authored_tier(tmp_path):
    song = tmp_path / "Song"
    song.mkdir()
    (song / "song.ini").write_text("[song]\nname = S\nartist = A\ndiff_drums = 5\ndiff_guitar = -1\n")
    (song / "notes.mid").write_bytes(b"")
    m = clonehero.index_meta(song)
    assert (m["drum_tier"], m["drum_tier_estimated"], m["drum_levels_generated"]) == (5, False, False)


def test_db_round_trip_and_library_reads(tmp_path):
    db = MetadataDB(tmp_path / "meta.db")
    db.put("a.sloppak", 1, 1, {"title": "A", "format": "sloppak", "has_drum_tab": True,
                               "drum_tier": 4, "drum_tier_estimated": True, "drum_levels_generated": True})
    db.put("b.sloppak", 1, 1, {"title": "B", "format": "sloppak"})
    assert db.get("a.sloppak", 1, 1)["drum_tier"] == 4
    assert db.get("b.sloppak", 1, 1)["drum_tier"] == -1      # rated: nothing to rate
    songs, _ = db.query_page(page=0, size=10)
    by = {s["filename"]: s for s in songs}
    assert (by["a.sloppak"]["drum_tier"], by["a.sloppak"]["drum_tier_estimated"],
            by["a.sloppak"]["drum_levels_generated"]) == (4, True, True)
    assert (by["b.sloppak"]["drum_tier"], by["b.sloppak"]["drum_levels_generated"]) == (None, False)


def test_migration_marks_only_sloppak_drum_rows_for_rating(tmp_path):
    path = tmp_path / "meta.db"
    db = MetadataDB(path)
    for fn, fmt, drums in (("d.sloppak", "sloppak", 1), ("n.sloppak", "sloppak", 0), ("p.psarc", "psarc", 0)):
        db.put(fn, 1, 1, {"title": fn, "format": fmt, "has_drum_tab": bool(drums)})
    db.conn.execute("UPDATE songs SET drum_tier = NULL")     # rows from before the columns
    db.conn.commit()
    db.conn.close()
    db = MetadataDB(path)
    tiers = dict(db.conn.execute("SELECT filename, drum_tier FROM songs"))
    assert tiers == {"d.sloppak": None, "n.sloppak": -1, "p.psarc": -1}
    assert db.get("d.sloppak", 1, 1)["drum_tier"] is None   # the scanner re-extracts it
    assert "drum_tier" in MetadataDB._EXTRACTION_MARKER_COLS
