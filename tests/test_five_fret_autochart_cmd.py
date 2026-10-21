"""scripts/five_fret_autochart.py: arrangement picking, actions, dry run, --out only, safety.
Packs are zips built in tmp_path (the library's form); nothing outside tmp_path is touched."""

from __future__ import annotations

import csv
import json
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest
import yaml

import fivefret
import fivefret_autochart as ac
from drumchart import TempoMap
from fivefretchart import FiveFretChart, FretNote, five_fret_json

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import five_fret_autochart as ffa  # noqa: E402
import five_fret_benchmark as ffb  # noqa: E402

PITCH_FRETS = [0, 2, 4, 5, 7, 5, 4, 2] * 4        # 32 single notes, one a beat


def _wire(name, beats=True, t0=1.0, string=1):
    return {"name": name, "tuning": [0] * 6, "capo": 0,
            "notes": [{"t": round(t0 + i * 0.5, 3), "s": string, "f": f}
                      for i, f in enumerate(PITCH_FRETS)],
            "chords": [], "anchors": [], "handshapes": [], "templates": [],
            "beats": [{"time": round(i * 0.5, 3), "measure": i // 4 if i % 4 == 0 else -1}
                      for i in range(60)] if beats else []}


def _five_fret(part, kind):
    chart = FiveFretChart(part=part, notes=[FretNote(time=1.0 + i, frets=1) for i in range(4)],
                          tempo=TempoMap(resolution=480))
    src = {"kind": "chart", "origin": "local"} if kind == "chart" else \
        {"kind": "auto", "generator": ac.GENERATOR, "generator_version": "0",
         "from_arrangement": "lead"}
    return five_fret_json(chart, src, f"{part} ({kind})")


def _pack(lib: Path, name: str, arrs: dict, five_fret=()) -> Path:
    """Zip pack: `arrs` id -> (wire, extra entry keys); `five_fret` (part, kind) parts."""
    lib.mkdir(parents=True, exist_ok=True)
    entries, files = [], {}
    for aid, (w, extra) in arrs.items():
        entries.append({"id": aid, "name": w["name"], "file": f"arrangements/{aid}.json",
                        "tuning": [0] * 6, "capo": 0, **extra})
        files[f"arrangements/{aid}.json"] = w
    for part, kind in five_fret:
        entries.append({"id": f"five_fret_{part}", "name": f"{part} ({kind})",
                        "type": "five_fret", "instrument": part,
                        "five_fret": f"five_fret_{part}.json"})
        files[f"five_fret_{part}.json"] = _five_fret(part, kind)
    manifest = {"title": "Song", "artist": "Band", "duration": 30.0, "feedpak_version": "1.21.0",
                "stems": [{"id": "full", "file": "stems/full.ogg"}], "arrangements": entries}
    path = lib / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.yaml", yaml.safe_dump(manifest, sort_keys=False))
        for rel, payload in files.items():
            z.writestr(rel, json.dumps(payload))
        z.writestr("stems/full.ogg", b"OggS\x00fake-audio")
    return path


def _band(lib, name="A.sloppak", five_fret=(), beats=True):
    return _pack(lib, name, {"lead": (_wire("Lead", beats), {}),
                             "rhythm": (_wire("Rhythm", beats, t0=1.25), {}),
                             "bass": (_wire("Bass", beats, string=0), {"type": "bass"})},
                 five_fret)


def _snapshot(*roots):
    """Every path under `roots` with mtime, size and bytes (the temp staging dir excluded)."""
    return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size, p.read_bytes() if p.is_file()
                   else b"") for root in roots for p in root.rglob("*") if "systmp" not in p.parts)


def _actions(pack, tmp_path, parts=("guitar", "rhythm", "bass")):
    staged = ffb.stage_pack(pack, tmp_path / "stage")
    return {r["part"]: r for r in ffa.auto_parts(staged, parts)}


@pytest.fixture
def scratch_tmp(tmp_path, monkeypatch):
    """Route the command's temp staging into tmp_path so tests can see it is removed."""
    d = tmp_path / "systmp"
    d.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(d))
    return d


# ── tests ───────────────────────────────────────────────────────────────────

def test_dry_run_writes_nothing(tmp_path, scratch_tmp, capsys):
    lib = tmp_path / "lib"
    pack = _band(lib)
    _band(lib, "B.sloppak")
    before = _snapshot(tmp_path)
    assert ffa.main([str(pack)]) == 0
    out = capsys.readouterr().out
    assert "dry run" in out
    for part, arr in (("guitar", "lead"), ("rhythm", "rhythm"), ("bass", "bass")):
        assert any(part in line and "write" in line and arr in line for line in out.splitlines())
    assert _snapshot(tmp_path) == before
    assert list(scratch_tmp.iterdir()) == []                       # staged copy deleted

    # --library + --only + --report: only the CSV is written.
    report = tmp_path / "reports" / "r.csv"
    assert ffa.main(["--library", str(lib), "--only", "^B", "--parts", "bass",
                     "--report", str(report)]) == 0
    rows = list(csv.DictReader(report.open()))
    assert [(r["pack"], r["part"], r["action"], r["arrangement"]) for r in rows] == [
        ("B.sloppak", "bass", "write", "bass")]
    assert int(rows[0]["expert"]) == len(PITCH_FRETS)
    after = [s for s in _snapshot(tmp_path) if "reports" not in s[0]]
    assert after == before
    assert list(scratch_tmp.iterdir()) == []


def test_out_dir_only(tmp_path, scratch_tmp):
    lib = tmp_path / "lib"
    pack = _band(lib)
    before = _snapshot(lib)
    out = tmp_path / "out"
    assert ffa.main([str(pack), "--out", str(out)]) == 0
    assert _snapshot(lib) == before                                # pack bytes / mtime unchanged
    assert list(scratch_tmp.iterdir()) == []
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "A/five_fret_bass.json", "A/five_fret_guitar.json", "A/five_fret_rhythm.json",
        "A/manifest_entries.json", "summary.json"]

    for part, arr in (("guitar", "lead"), ("rhythm", "rhythm"), ("bass", "bass")):
        data = json.loads((out / "A" / f"five_fret_{part}.json").read_text())
        assert fivefret.validate_five_fret(data) == (True, "")
        assert data["source"] == {"kind": "auto", "generator": ac.GENERATOR,
                                  "generator_version": ac.GENERATOR_VERSION,
                                  "from_arrangement": arr}
        assert data["name"] == f"{part.capitalize()} (auto)" and data["instrument"] == part
        assert data["notes"]
        assert set(data["levels"]) == {"hard", "medium", "easy"}
        assert all(lv["generated"] is True for lv in data["levels"].values())

    entries = json.loads((out / "A" / "manifest_entries.json").read_text())
    assert entries == [
        {"id": f"five_fret_{p}", "name": f"{p.capitalize()} (auto)", "type": "five_fret",
         "instrument": p, "five_fret": f"five_fret_{p}.json", "pairs_with": arr}
        for p, arr in (("guitar", "lead"), ("rhythm", "rhythm"), ("bass", "bass"))]
    summary = json.loads((out / "summary.json").read_text())
    assert summary["generator_version"] == ac.GENERATOR_VERSION
    assert [(r["part"], r["action"]) for r in summary["packs"][0]["parts"]] == [
        ("guitar", "write"), ("rhythm", "write"), ("bass", "write")]

    # Loader shape: the files and entries, added to a copy of the pack, load as auto parts.
    copy = tmp_path / "merged" / "A.sloppak"
    with zipfile.ZipFile(pack) as z:
        z.extractall(copy)
    manifest = yaml.safe_load((copy / "manifest.yaml").read_text())
    manifest["arrangements"] += entries
    (copy / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    for e in entries:
        (copy / e["five_fret"]).write_bytes((out / "A" / e["five_fret"]).read_bytes())
    loaded = ffb._load_staged(copy)
    got = {p["instrument"]: (p["source_kind"], p["pairs_with"], p["name"])
           for p in loaded.five_fret_parts}
    assert got == {"guitar": ("auto", "lead", "Guitar (auto)"),
                   "rhythm": ("auto", "rhythm", "Rhythm (auto)"),
                   "bass": ("auto", "bass", "Bass (auto)")}


def test_chart_part_never_replaced(tmp_path, scratch_tmp):
    lib = tmp_path / "lib"
    pack = _pack(lib, "A.sloppak", {"lead": (_wire("Lead"), {})},
                 five_fret=[("guitar", "chart")])
    acts = _actions(pack, tmp_path)
    assert {p: r["action"] for p, r in acts.items()} == {
        "guitar": "skip-chart", "rhythm": "skip-none", "bass": "skip-none"}
    out = tmp_path / "out"
    assert ffa.main([str(pack), "--out", str(out)]) == 0
    assert not (out / "A").exists()                               # nothing to write
    summary = json.loads((out / "summary.json").read_text())
    assert summary["packs"][0]["parts"][0]["action"] == "skip-chart"


def test_auto_part_replaced(tmp_path, scratch_tmp):
    lib = tmp_path / "lib"
    pack = _pack(lib, "A.sloppak", {"lead": (_wire("Lead"), {})},
                 five_fret=[("guitar", "auto")])
    acts = _actions(pack, tmp_path, ("guitar",))
    assert acts["guitar"]["action"] == "write"
    assert acts["guitar"]["data"]["source"]["generator_version"] == ac.GENERATOR_VERSION
    out = tmp_path / "out"
    assert ffa.main([str(pack), "--out", str(out), "--parts", "guitar"]) == 0
    data = json.loads((out / "A" / "five_fret_guitar.json").read_text())
    assert len(data["notes"]) == len(PITCH_FRETS)               # the new chart, not the old 4


def test_refuses_out_inside_library(tmp_path, scratch_tmp, monkeypatch, capsys):
    lib = tmp_path / "dlc" / "lib"
    pack = _band(lib)
    ro = tmp_path / "ro"
    ro.mkdir()
    before = _snapshot(tmp_path)
    assert ffa.main(["--library", str(lib), "--out", str(lib / "out")]) == 2      # inside
    assert ffa.main(["--library", str(lib), "--out", str(lib)]) == 2              # equal
    assert ffa.main(["--library", str(lib), "--out", str(tmp_path / "dlc")]) == 2  # ancestor
    assert ffa.main([str(pack), "--out", str(lib / "x")]) == 2                    # pack's folder
    assert ffa.main([str(pack), "--report", str(lib / "r.csv")]) == 2
    for attr in ("DLC_ROOT", "CACHE", "CH_SONGS"):
        monkeypatch.setattr(ffb, attr, ro)
        assert ffa.main([str(pack), "--out", str(ro / "out")]) == 2
        assert ffa.main([str(pack), "--report", str(tmp_path / "ro" / "r.csv")]) == 2
        monkeypatch.undo()
        monkeypatch.setattr(tempfile, "tempdir", str(scratch_tmp))
    assert "refusing" in capsys.readouterr().err
    assert _snapshot(tmp_path) == before
    assert list(ro.iterdir()) == [] and list(scratch_tmp.iterdir()) == []


def test_pick_arrangements_combo_and_bass():
    combo = {"arrangements": [
        {"id": "d", "name": "Drums", "type": "drums", "file": "a/d.json"},
        {"id": "k", "name": "Combo Keys", "type": "keys", "file": "a/k.json"},
        {"id": "a0", "name": "Combo", "file": "a/0.json"},
        {"id": "a1", "name": "Combo 2", "file": "a/1.json"},
        {"id": "a2", "name": "Low End", "type": "bass", "file": "a/2.json"},
        {"id": "f", "name": "Lead (5-fret)", "type": "five_fret", "instrument": "guitar",
         "five_fret": "five_fret_guitar.json"}]}
    assert ffa.pick_arrangements(combo) == {"guitar": "a0", "rhythm": "a1", "bass": "a2"}
    assert not any(paired for _, paired in ffa.pick_with_pairing(combo).values())
    by_name_bass = {"arrangements": [{"id": "x", "name": "Alt Bass", "file": "a/x.json"}]}
    assert ffa.pick_arrangements(by_name_bass) == {"bass": "x"}

    # Plan 4's rule first: id lead / rhythm / bass, then a name starting with that word.
    plan4 = {"arrangements": [
        {"id": "a0", "name": "Lead", "file": "a/0.json"},
        {"id": "lead", "name": "Combo", "file": "a/l.json"},
        {"id": "r", "name": "RHYTHM guitar", "file": "a/r.json"},
        {"id": "b", "name": "Bass 2", "type": "bass", "file": "a/b.json"},
        {"id": "f", "name": "Rhythm (auto)", "type": "five_fret", "instrument": "rhythm",
         "five_fret": "five_fret_rhythm.json"}]}
    assert ffa.pick_with_pairing(plan4) == {"guitar": ("lead", True), "rhythm": ("r", True),
                                            "bass": ("b", True)}
    assert ffa.pick_arrangements({"arrangements": []}) == {}


def test_pack_without_beats_still_charts(tmp_path, scratch_tmp):
    lib = tmp_path / "lib"
    pack = _pack(lib, "A.sloppak", {"lead": (_wire("Lead", beats=False), {})})
    acts = _actions(pack, tmp_path, ("guitar",))
    r = acts["guitar"]
    assert r["action"] == "write" and r["notes"]["expert"] == len(PITCH_FRETS)
    assert fivefret.validate_five_fret(r["data"]) == (True, "")
    assert set(r["data"]["levels"]) == {"hard", "medium", "easy"}


def test_write_error_counts_for_that_pack_and_summary_is_written(tmp_path, scratch_tmp, monkeypatch):
    lib = tmp_path / "lib"
    _band(lib, "A.sloppak")
    _band(lib, "B.sloppak")
    real = ffa.write_pack

    def flaky(out, pack, rows):
        if pack.name == "A.sloppak":
            raise OSError("disk full")
        return real(out, pack, rows)

    monkeypatch.setattr(ffa, "write_pack", flaky)
    out = tmp_path / "out"
    assert ffa.main(["--library", str(lib), "--out", str(out)]) == 1   # errors -> exit 1
    summary = json.loads((out / "summary.json").read_text())
    assert summary["errors"] == 1
    by_pack = {p["pack"]: p for p in summary["packs"]}
    assert by_pack["A.sloppak"]["error"].startswith("OSError")
    assert by_pack["A.sloppak"]["parts"] == []
    assert by_pack["B.sloppak"]["parts"] and (out / "B" / "five_fret_guitar.json").exists()
