"""Pack side of lib/fivefretjoin.py: existing five-fret parts, drum-warp reuse, writing.
mattromano custom build. No audio or librosa needed."""
import json
import zipfile
from pathlib import Path

import pytest
import yaml

import drumjoin
import fivefretjoin
import sloppak

MD5_A = "a" * 32
MD5_B = "b" * 32


def _note(t=1.0, f=1):
    return {"t": t, "f": f}


def _payload(part="bass", kind="chart", levels=None, md5=None, notes=None):
    d = {"version": 1, "name": part, "instrument": part, "source": {"kind": kind},
         "notes": notes if notes is not None else [_note()]}
    if md5:
        d["source"]["md5"] = md5
    if levels:
        d["levels"] = levels
    return d


def _entry(part, id_=None):
    return {"id": id_ or f"five_fret_{part}", "name": part, "type": "five_fret", "instrument": part,
            "five_fret": f"five_fret_{part}.json"}


def _pack(tmp_path, manifest, files, zip_form=True, name="song.sloppak"):
    manifest = dict(manifest)
    manifest.setdefault("title", "T")
    p = tmp_path / name
    members = {"manifest.yaml": yaml.safe_dump(manifest).encode(), **files}
    members = {k: (json.dumps(v).encode() if not isinstance(v, bytes) else v) for k, v in members.items()}
    if zip_form:
        with zipfile.ZipFile(p, "w", zipfile.ZIP_STORED) as z:
            for k, v in members.items():
                z.writestr(k, v)
    else:
        p.mkdir()
        for k, v in members.items():
            (p / k).parent.mkdir(parents=True, exist_ok=True)
            (p / k).write_bytes(v)
    return p


@pytest.fixture
def sp_factory(tmp_path):
    opened = []

    def make(manifest, files=None, **kw):
        sp = drumjoin.SloppakFiles(_pack(tmp_path, manifest, files or {}, **kw))
        opened.append(sp)
        return sp
    yield make
    for sp in opened:
        sp.close()


def test_existing_parts_kinds_and_levels(sp_factory):
    lv = {"easy": {"notes": [_note()]}, "medium": {"notes": [_note()], "generated": True},
          "hard": {"notes": [_note()], "generated": True}}
    sp = sp_factory(
        {"arrangements": [_entry("bass"), _entry("guitar"), _entry("rhythm")]},
        {"five_fret_bass.json": _payload("bass", "chart", lv, md5="m5"),
         "five_fret_guitar.json": _payload("guitar", "auto"),
         "five_fret_rhythm.json": b"{not json"})
    got = fivefretjoin.existing_parts(sp)
    assert got["bass"] == {"id": "five_fret_bass", "kind": "chart", "levels": 1, "md5": "m5"}
    assert got["guitar"]["kind"] == "auto" and got["guitar"]["md5"] is None
    assert got["rhythm"]["kind"] == "chart" and got["rhythm"]["levels"] == 3


def test_pairs_with():
    m = {"arrangements": [{"id": "lead", "name": "Lead"}, {"id": "rhythm2", "name": "Rhythm 2"},
                          {"id": "bass", "name": "Bass"}, {"id": "drums", "name": "Drums", "type": "drums"},
                          _entry("bass")]}
    assert fivefretjoin.pairs_with(m, "guitar") == "lead"
    assert fivefretjoin.pairs_with(m, "rhythm") == "rhythm2"
    assert fivefretjoin.pairs_with(m, "bass") == "bass"
    assert fivefretjoin.pairs_with({"arrangements": [{"id": "drums", "name": "Drums"}, _entry("bass")]},
                                   "bass") is None


def test_drum_source_legacy_and_new(sp_factory):
    pairs = [[0.0, 0.659], [0.306, 1.186]]
    legacy = sp_factory({"arrangements": [{"id": "drums", "name": "Drums", "file": "arrangements/drums.json"}]},
                        {"arrangements/drums.json": {"drums": {"source": {"source": "chart", "folder": "/f/x",
                                                                          "warp": pairs}}}})
    assert fivefretjoin.drum_source(legacy) == {"folder": "/f/x", "warp": pairs}
    new = sp_factory({"arrangements": []},
                     {"drum_join.json": {"detail": {"folder": "/f/y", "warp": {"offset": 1.5}}}},
                     name="n.sloppak")
    assert fivefretjoin.drum_source(new) == {"folder": "/f/y", "warp": {"offset": 1.5}}
    assert fivefretjoin.drum_source(sp_factory({"arrangements": []}, name="z.sloppak")) is None


def _src_pack(sp_factory, folder, warp=None):
    return sp_factory({"arrangements": []},
                      {"drum_join.json": {"detail": {"folder": str(folder), "warp": warp or {"offset": 0.5}}}})


def test_reuse_same_chart_only(sp_factory, tmp_path, monkeypatch):
    monkeypatch.setattr(drumjoin, "SNG_CACHE", tmp_path / "sngcache")
    chorus = tmp_path / "chorus"
    sp = _src_pack(sp_factory, chorus / MD5_A)
    assert fivefretjoin.reusable_warp(sp, fivefretjoin.chart_ids(folder=chorus / MD5_A)) == {
        "folder": str(chorus / MD5_A), "warp": {"offset": 0.5}}
    assert fivefretjoin.reusable_warp(sp, fivefretjoin.chart_ids(folder=chorus / MD5_B)) is None
    assert fivefretjoin.reusable_warp(sp, fivefretjoin.chart_ids(label=f"chorus:{MD5_A}")) is not None
    # an .sng chart: the pack's source folder is the sng cache dir
    sng = tmp_path / "Song.sng"
    sng.write_bytes(b"x")
    cache = drumjoin.sng_cache_dir(sng)
    sp2 = _src_pack(sp_factory, cache)
    ids = fivefretjoin.chart_ids(label=f"local:{sng}", folder=sng)
    assert f"local:{sng.resolve()}" in ids
    assert fivefretjoin.reusable_warp(sp2, ids) is not None
    assert not cache.exists()      # nothing extracted


def test_reuse_windows_source_folder(sp_factory):
    sp = _src_pack(sp_factory, rf"C:\Users\x\.cache\slopsmith-drums\chorus\{MD5_A}")
    assert fivefretjoin.chart_ids(folder=rf"C:\Users\x\chorus\{MD5_A}") >= {f"chorus:{MD5_A}"}
    assert fivefretjoin.reusable_warp(sp, {f"chorus:{MD5_A}"}) is not None
    assert fivefretjoin.reusable_warp(sp, {f"chorus:{MD5_B}"}) is None


def test_reuse_ignores_state_label(sp_factory, tmp_path):
    sp = _src_pack(sp_factory, tmp_path / "chorus" / MD5_A)
    # Heart_Shaped_Box shape: the drums-state label named another chart
    assert fivefretjoin.reusable_warp(sp, fivefretjoin.chart_ids(label=f"chorus:{MD5_B} X - Y (Z)")) is None


def test_sng_cache_dir_matches_chart_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(drumjoin, "SNG_CACHE", tmp_path / "cache")
    p = tmp_path / "a.sng"
    p.write_bytes(b"x")
    d = drumjoin.sng_cache_dir(p)
    assert not d.exists() and not (tmp_path / "cache").exists()
    (d / "song.ini").parent.mkdir(parents=True)
    (d / "song.ini").write_text("[song]")
    assert drumjoin.chart_folder(p) == d


def test_warp_fn():
    assert fivefretjoin.warp_fn([[0, 10], [1, 11.5]])(0.5) == pytest.approx(10.75)
    assert fivefretjoin.warp_fn({"offset": -2})(3) == pytest.approx(1)


def _bass_pack(sp_factory, tmp_path, version="1.20.0"):
    return sp_factory({"feedpak_version": version, "arrangements": [{"id": "bass", "name": "Bass"}]},
                      {"audio.bin": b"\x00\x01binary"})


def test_write_new_part_zip_keeps_other_members(sp_factory, tmp_path):
    sp = _bass_pack(sp_factory, tmp_path)
    path = sp.path
    fivefretjoin.write_five_fret(sp, {"bass": _payload("bass")}, {"bass": {"source": "chorus"}})
    with zipfile.ZipFile(path) as z:
        assert z.read("audio.bin") == b"\x00\x01binary"
        assert json.loads(z.read("five_fret_join.json")) == {"bass": {"source": "chorus"}}
        m = yaml.safe_load(z.read("manifest.yaml"))
    ent = [e for e in m["arrangements"] if e.get("type") == "five_fret"]
    assert ent[0]["pairs_with"] == "bass" and ent[0]["five_fret"] == "five_fret_bass.json"
    assert m["feedpak_version"] == "1.21.0"
    assert "bass:chart" in sloppak.extract_meta(path)["five_fret_parts"]


def test_write_keeps_newer_feedpak_version(sp_factory, tmp_path):
    sp = _bass_pack(sp_factory, tmp_path, "1.22.0")
    fivefretjoin.write_five_fret(sp, {"bass": _payload("bass")}, {})
    assert yaml.safe_load(zipfile.ZipFile(sp.path).read("manifest.yaml"))["feedpak_version"] == "1.22.0"


def test_write_replaces_same_instrument_only(sp_factory):
    g = _payload("guitar", "chart")
    sp = sp_factory({"arrangements": [_entry("bass"), _entry("guitar")]},
                    {"five_fret_bass.json": _payload("bass", "auto"), "five_fret_guitar.json": g})
    fivefretjoin.write_five_fret(sp, {"bass": _payload("bass", "chart", notes=[_note(2.0)])}, {})
    with zipfile.ZipFile(sp.path) as z:
        names = z.namelist()
        assert names.count("five_fret_bass.json") == 1
        assert json.loads(z.read("five_fret_bass.json"))["source"]["kind"] == "chart"
        assert json.loads(z.read("five_fret_guitar.json")) == g
        m = yaml.safe_load(z.read("manifest.yaml"))
    ff = [e for e in m["arrangements"] if e.get("type") == "five_fret"]
    assert sorted(e["instrument"] for e in ff) == ["bass", "guitar"]


def test_write_rejects_invalid_payload(sp_factory, tmp_path):
    sp = _bass_pack(sp_factory, tmp_path)
    before = sp.path.read_bytes()
    with pytest.raises(ValueError):
        fivefretjoin.write_five_fret(sp, {"bass": _payload("bass", notes=[{"t": -1, "f": 1}])}, {})
    assert sp.path.read_bytes() == before
