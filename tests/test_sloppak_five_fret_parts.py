"""Loader coverage for five-fret parts (feedpak `type: five_fret` entries).

A five-fret part rides the manifest as an arrangement entry with a
`five_fret` side-file pointer. The loader never turns it into a fretted
Arrangement and resolves it into `LoadedSloppak.five_fret_parts` in
manifest order, with the drum-parts permissive posture: a bad part
disables that part only, never the load.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import yaml

import sloppak as sloppak_mod


def _chart(name: str = "Bass", instrument: str = "bass", kind: str = "chart") -> dict:
    return {
        "version": 1,
        "name": name,
        "instrument": instrument,
        "source": {"kind": kind},
        "notes": [],
    }


def _write_pak(root: Path, manifest: dict, files: dict[str, dict | str]) -> Path:
    pak = root / f"{root.name}.sloppak"
    pak.mkdir(parents=True)
    (pak / "arrangements").mkdir()
    base = {
        "title": "Test", "artist": "Tester", "album": "", "year": 2026,
        "duration": 10.0,
        "stems": [{"id": "full", "file": "stems/full.ogg", "default": True}],
    }
    base.update(manifest)
    (pak / "manifest.yaml").write_text(yaml.safe_dump(base, sort_keys=False))
    for rel, payload in files.items():
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (pak / rel).write_text(text)
    return pak


def _lead_files() -> dict:
    return {"arrangements/lead.json": {
        "name": "Lead", "tuning": [0] * 6, "capo": 0, "notes": [], "chords": [],
        "anchors": [], "handshapes": [], "templates": [], "beats": [], "sections": [],
    }}


LEAD = {"id": "lead", "name": "Lead", "file": "arrangements/lead.json"}


def _ff_entry(id_="bass5", instrument="bass", path="five_fret_bass.json", **extra) -> dict:
    return {"id": id_, "name": "Bass 5", "type": "five_fret",
            "instrument": instrument, "five_fret": path, **extra}


def _load(pak: Path, tmp_path: Path):
    cache = tmp_path / "cache"
    cache.mkdir()
    return sloppak_mod.load_song(pak.name, pak.parent, cache)


def test_five_fret_entry_loaded(tmp_path):
    pak = _write_pak(tmp_path / "a", {"arrangements": [LEAD, _ff_entry(pairs_with="lead")]},
                     {**_lead_files(), "five_fret_bass.json": _chart()})
    loaded = _load(pak, tmp_path)
    assert len(loaded.five_fret_parts) == 1
    part = loaded.five_fret_parts[0]
    assert part["id"] == "bass5"
    assert part["instrument"] == "bass"
    assert part["source_kind"] == "chart"
    assert part["pairs_with"] == "lead"
    assert part["five_fret"]["name"] == "Bass"
    assert part["name"] == "Bass 5"


def test_name_falls_back_to_file_then_id(tmp_path):
    e1 = {**_ff_entry("a", path="a.json")}
    e1.pop("name")
    e2 = {**_ff_entry("b", path="b.json")}
    e2.pop("name")
    nameless = _chart()
    nameless.pop("name")
    pak = _write_pak(tmp_path / "n", {"arrangements": [LEAD, e1, e2]},
                     {**_lead_files(), "a.json": _chart("From File"), "b.json": nameless})
    parts = _load(pak, tmp_path).five_fret_parts
    assert [p["name"] for p in parts] == ["From File", "b"]
    assert parts[0]["pairs_with"] is None


def test_five_fret_never_fretted(tmp_path):
    entry = _ff_entry(file="arrangements/lead.json")
    pak = _write_pak(tmp_path / "b", {"arrangements": [LEAD, entry]},
                     {**_lead_files(), "five_fret_bass.json": _chart()})
    loaded = _load(pak, tmp_path)
    assert [a.name for a in loaded.song.arrangements] == ["Lead"]
    assert len(loaded.five_fret_parts) == 1


def test_only_five_fret_pack_loads(tmp_path):
    pak = _write_pak(tmp_path / "c", {"arrangements": [_ff_entry()]},
                     {"five_fret_bass.json": _chart()})
    loaded = _load(pak, tmp_path)
    assert len(loaded.five_fret_parts) == 1
    # Like a drum-only pack, a five-fret-only pack gets one note-less
    # placeholder arrangement so the highway WS proceeds past its
    # empty-arrangements guard; the part itself never becomes an Arrangement.
    assert [(a.name, len(a.notes)) for a in loaded.song.arrangements] == [("Bass 5", 0)]


def test_no_parts_is_none(tmp_path):
    pak = _write_pak(tmp_path / "d", {"arrangements": [LEAD]}, _lead_files())
    assert _load(pak, tmp_path).five_fret_parts is None


def test_invalid_file_disables_part_only(tmp_path, caplog):
    pak = _write_pak(
        tmp_path / "e",
        {"arrangements": [LEAD, _ff_entry("bad", path="bad.json"),
                          _ff_entry("good", path="good.json")]},
        {**_lead_files(), "bad.json": "{not json", "good.json": _chart()},
    )
    with caplog.at_level(logging.WARNING):
        loaded = _load(pak, tmp_path)
    assert [p["id"] for p in loaded.five_fret_parts] == ["good"]
    assert any("bad.json" in r.getMessage() for r in caplog.records)


def test_failed_validation_disables_part(tmp_path, caplog):
    bad = _chart()
    bad["version"] = 99
    pak = _write_pak(tmp_path / "v", {"arrangements": [LEAD, _ff_entry()]},
                     {**_lead_files(), "five_fret_bass.json": bad})
    with caplog.at_level(logging.WARNING):
        loaded = _load(pak, tmp_path)
    assert loaded.five_fret_parts is None
    assert any("failed validation" in r.getMessage() for r in caplog.records)


def test_traversal_rejected(tmp_path, caplog):
    (tmp_path / "x.json").write_text(json.dumps(_chart()))
    pak = _write_pak(tmp_path / "f", {"arrangements": [LEAD, _ff_entry(path="../x.json")]},
                     _lead_files())
    with caplog.at_level(logging.WARNING):
        loaded = _load(pak, tmp_path)
    assert loaded.five_fret_parts is None
    assert any("escapes" in r.getMessage() for r in caplog.records)


def test_bad_instrument_dropped_with_warning(tmp_path, caplog):
    pak = _write_pak(tmp_path / "i", {"arrangements": [LEAD, _ff_entry(instrument="keys")]},
                     {**_lead_files(), "five_fret_bass.json": _chart()})
    with caplog.at_level(logging.WARNING):
        loaded = _load(pak, tmp_path)
    assert loaded.five_fret_parts is None
    assert any("instrument" in r.getMessage() for r in caplog.records)


def test_drum_parts_unchanged(tmp_path):
    tab = {"version": 1, "name": "Kit", "kit": [{"id": "kick", "name": "Kick"}],
           "hits": [{"t": 1.0, "p": "kick", "v": 100}]}
    drum_entry = {"id": "drums", "name": "Drums", "type": "drums", "drum_tab": "drum_tab.json"}
    pak = _write_pak(
        tmp_path / "g",
        {"drum_tab": "drum_tab.json", "arrangements": [LEAD, drum_entry, _ff_entry()]},
        {**_lead_files(), "drum_tab.json": tab, "five_fret_bass.json": _chart()},
    )
    loaded = _load(pak, tmp_path)
    assert [p["id"] for p in loaded.drum_parts] == ["drums"]
    assert len(loaded.five_fret_parts) == 1
    assert [a.name for a in loaded.song.arrangements] == ["Lead"]
