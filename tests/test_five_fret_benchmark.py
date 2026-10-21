"""scripts/five_fret_benchmark.py: pairing, warp, read-only staging, reports, reducers.
Packs are zips built in tmp_path; real charts are Clone Hero folders from drum_fixtures."""

from __future__ import annotations

import csv
import json
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

import fivefret_bench as fb
from drumchart import TempoMap
from fivefretchart import FiveFretChart, FretNote, five_fret_json
from tests.drum_fixtures import TPQ, write_ini, write_mid

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import five_fret_benchmark as ffb  # noqa: E402

LANES = [0, 1, 2, 3, 4, 3, 2, 1] * 5          # 40 notes, one a beat
STEP = 0.5                                    # seconds per note (120 bpm, one per beat)


def _chart_folder(path: Path, levels=False) -> Path:
    """A Clone Hero folder whose PART GUITAR Expert walks LANES, one note a beat from 0 s;
    with `levels`, hand-charted Hard / Medium / Easy too."""
    path.mkdir(parents=True)
    notes = [(i * TPQ, 96 + lane, 100, 10) for i, lane in enumerate(LANES)]
    if levels:
        notes += [(i * TPQ, 84 + lane, 100, 10) for i, lane in enumerate(LANES) if i % 2 == 0]
        notes += [(i * TPQ, 72 + min(lane, 3), 100, 10) for i, lane in enumerate(LANES) if i % 2 == 0]
        notes += [(i * TPQ, 60 + min(lane, 2), 100, 10) for i, lane in enumerate(LANES) if i % 4 == 0]
    write_mid(path / "notes.mid", notes, track_name="PART GUITAR")
    write_ini(path / "song.ini", name="Song", artist="Band")
    return path


def _wire(name, t0=1.0, lanes=LANES, step=STEP, string=1):
    return {"name": name, "tuning": [0] * 6, "capo": 0,
            "notes": [{"t": round(t0 + i * step, 3), "s": string, "f": 2 * lane}
                      for i, lane in enumerate(lanes)],
            "chords": [], "anchors": [], "handshapes": [], "templates": [],
            "beats": [{"time": round(i * 0.5, 3), "measure": i // 4 if i % 4 == 0 else -1}
                      for i in range(60)]}


def _pack(lib: Path, name: str, arrs: dict, drum_source=None, extra=None) -> Path:
    """Zip pack: `arrs` id -> wire arrangement; `drum_source` -> the 0.2.9 tools'
    arrangements/drums.json `drums.source`; `extra` (manifest entry, rel, payload)."""
    lib.mkdir(parents=True, exist_ok=True)
    entries = [{"id": aid, "name": w["name"], "file": f"arrangements/{aid}.json",
                "tuning": [0] * 6, "capo": 0} for aid, w in arrs.items()]
    files = {f"arrangements/{aid}.json": w for aid, w in arrs.items()}
    if drum_source is not None:
        entries.append({"id": "drums", "name": "Drums", "file": "arrangements/drums.json"})
        files["arrangements/drums.json"] = {"name": "Drums", "notes": [], "chords": [],
                                            "drums": {"version": 1, "source": drum_source}}
    for entry, rel, payload in extra or ():
        entries.append(entry)
        files[rel] = payload
    manifest = {"title": "Song", "artist": "Band", "duration": 30.0,
                "stems": [{"id": "full", "file": "stems/full.ogg"}], "arrangements": entries}
    path = lib / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.yaml", yaml.safe_dump(manifest, sort_keys=False))
        for rel, payload in files.items():
            z.writestr(rel, json.dumps(payload))
        z.writestr("stems/full.ogg", b"OggS\x00fake-audio")
    return path


def _run(lib, out, *extra):
    return ffb.main([str(lib), "--out", str(out), *extra])


def _report(out):
    return json.loads((out / "report.json").read_text())


def _rows(out):
    return _report(out)["rows"]


def _snapshot(*roots):
    return sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                  for root in roots for p in root.rglob("*"))


# ── tests ───────────────────────────────────────────────────────────────────

def test_pairs_via_drum_source_and_warp(tmp_path):
    folder = _chart_folder(tmp_path / "ch" / "song")
    lib = tmp_path / "lib"
    _pack(lib, "A.sloppak", {"lead": _wire("Lead")},
          {"source": "chart", "folder": str(folder), "warp": {"offset": 1.0}})
    staged = ffb.stage_pack(lib / "A.sloppak", tmp_path / "stage")
    real = ffb.real_parts(staged)
    assert real["guitar"]["expert"][0].time == pytest.approx(1.0)       # 0 s + 1 s offset
    rows = ffb.bench_pack(staged, "A.sloppak")
    assert len(rows) == 1
    r = rows[0]
    assert r["part"] == "guitar" and r["arrangement"] == "Lead" and r["source"] == "drums"
    assert r["paired"] and r["onset_f1"] == pytest.approx(1.0)
    assert r["levels"]["expert"]["onset_f1"] == pytest.approx(1.0)
    assert r["levels"]["expert"]["matched"] == len(LANES)
    assert r["split"] == fb.split("A.sloppak")
    assert set(r["baselines"]) == {"stay", "absolute"}
    assert r["real_excluded"] == ["hard", "medium", "easy"]          # chart has Expert only
    assert not any(r["levels"][lv]["has_real"] for lv in ("hard", "medium", "easy"))


def test_pack_own_chart_part_preferred(tmp_path):
    folder = _chart_folder(tmp_path / "ch" / "song")
    notes = [FretNote(time=1.0 + i * STEP, frets=1 << lane) for i, lane in enumerate(LANES)]
    lower = [FretNote(time=n.time, frets=n.frets) for n in notes[::2]]
    chart = FiveFretChart(part="guitar", notes=notes, tempo=TempoMap(resolution=480),
                          levels={"hard": lower, "easy": lower}, levels_generated=["easy"])
    ff = five_fret_json(chart, {"kind": "chart"})
    lib = tmp_path / "lib"
    # The drum source's warp is way off (unpaired if it were used); the pack's own part wins.
    _pack(lib, "B.sloppak", {"lead": _wire("Lead")},
          {"source": "chart", "folder": str(folder), "warp": {"offset": 7.0}},
          extra=[({"id": "ffg", "name": "Guitar", "type": "five_fret", "instrument": "guitar",
                   "five_fret": "five_fret_guitar.json"}, "five_fret_guitar.json", ff)])
    staged = ffb.stage_pack(lib / "B.sloppak", tmp_path / "stage")
    assert (staged / "five_fret_guitar.json").is_file()
    r = ffb.bench_pack(staged, "B.sloppak")[0]
    assert r["source"] == "pack" and r["paired"] and r["onset_f1"] == pytest.approx(1.0)
    # the generated Easy is excluded, the hand-charted Hard is scored, Medium is missing
    assert r["real_levels"] == ["expert", "hard"]
    assert r["real_excluded"] == ["medium", "easy"]
    assert r["levels"]["hard"]["has_real"] and not r["levels"]["easy"]["has_real"]


def test_best_onset_arrangement_chosen(tmp_path):
    folder = _chart_folder(tmp_path / "ch" / "song")
    lib = tmp_path / "lib"
    _pack(lib, "C.sloppak", {"lead": _wire("Lead", t0=1.25), "rhythm": _wire("Rhythm"),
                             "bass": _wire("Bass", string=0)},
          {"source": "chart", "folder": str(folder), "warp": {"offset": 1.0}})
    r = ffb.bench_pack(ffb.stage_pack(lib / "C.sloppak", tmp_path / "stage"), "C.sloppak")[0]
    assert r["arrangement"] == "Rhythm" and r["paired"]
    assert set(r["candidates"]) == {"Lead", "Rhythm"}                   # bass never compared
    assert r["candidates"]["Lead"] < 0.5


def test_low_onset_unpaired(tmp_path):
    folder = _chart_folder(tmp_path / "ch" / "song")
    lib = tmp_path / "lib"
    _pack(lib, "D.sloppak", {"lead": _wire("Lead", t0=1.25)},
          {"source": "chart", "folder": str(folder), "warp": {"offset": 1.0}})
    out = tmp_path / "out"
    assert _run(lib, out) == 0
    rows = _rows(out)
    assert len(rows) == 1 and not rows[0]["paired"] and rows[0]["onset_f1"] < fb.MIN_ONSET_F1
    counts = _report(out)["counts"]
    assert counts["paired"] == 0 and counts["unpaired"] == 1
    assert _report(out)["verdict"]["n"] == 0


def test_warp_pairs_interpolate_and_extrapolate():
    w = ffb.warp_fn([[0.0, 10.0], [10.0, 30.0], [20.0, 40.0]])
    assert w(5.0) == pytest.approx(20.0)            # inside: interpolated
    assert w(15.0) == pytest.approx(35.0)
    assert w(-1.0) == pytest.approx(8.0)            # before: first segment's slope (2)
    assert w(22.0) == pytest.approx(42.0)           # after: last segment's slope (1)
    assert ffb.warp_fn({"offset": 1.5})(2.0) == pytest.approx(3.5)
    assert ffb.warp_fn([[1.0, 4.0]])(3.0) == pytest.approx(6.0)


def test_skip_reasons_counted(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    folder = _chart_folder(cache / "chorus" / "abc123")
    monkeypatch.setattr(ffb, "CACHE", cache)
    lib = tmp_path / "lib"
    _pack(lib, "nodrums.sloppak", {"lead": _wire("Lead")})
    _pack(lib, "gp.sloppak", {"lead": _wire("Lead")}, {"source": "gp", "gp": "C:\\x.gp5"})
    _pack(lib, "nullwarp.sloppak", {"lead": _wire("Lead")},
          {"source": "chart", "folder": str(folder), "warp": None})
    _pack(lib, "gone.sloppak", {"lead": _wire("Lead")},
          {"source": "chart", "folder": "C:\\Users\\x\\.cache\\slopsmith-drums\\chorus\\nope",
           "warp": {"offset": 1.0}})
    _pack(lib, "win.sloppak", {"lead": _wire("Lead")},
          {"source": "chart", "folder": "C:\\Users\\x\\.cache\\slopsmith-drums\\chorus\\abc123",
           "warp": {"offset": 1.0}})
    out = tmp_path / "out"
    assert _run(lib, out) == 0
    counts = _report(out)["counts"]
    assert counts["skipped"] == {"gp_no_folder": 1, "no_drum_source": 1, "null_warp": 1,
                                 "windows_path_unmapped": 1}
    assert counts["staged"] == 1 and counts["paired"] == 1
    assert not (out / "stage" / "nodrums.sloppak").exists()            # skipped before staging


def test_refuses_out_inside_library(tmp_path, capsys):
    lib = tmp_path / "lib"
    _pack(lib, "A.sloppak", {"lead": _wire("Lead")})
    before = _snapshot(lib)
    assert _run(lib, lib / "out") == 2
    assert _run(lib, tmp_path / "out", "--stage", str(lib / "A.sloppak" / "..")) == 2
    assert "refusing" in capsys.readouterr().err
    assert _snapshot(lib) == before
    assert not (tmp_path / "out").exists()


def test_library_untouched(tmp_path):
    ch = tmp_path / "ch"
    folder = _chart_folder(ch / "song", levels=True)
    lib = tmp_path / "lib"
    _pack(lib, "A.sloppak", {"lead": _wire("Lead"), "rhythm": _wire("Rhythm", t0=1.25)},
          {"source": "chart", "folder": str(folder), "warp": [[0.0, 1.0], [10.0, 11.0]]})
    _pack(lib, "B.sloppak", {"lead": _wire("Lead")})
    before = _snapshot(lib, ch)
    assert _run(lib, tmp_path / "out", "--reducers") == 0
    assert _snapshot(lib, ch) == before
    stage = tmp_path / "out" / "stage"
    assert sorted(p.name for p in stage.iterdir()) == ["A.sloppak"]   # only the SVG pick kept
    staged = ffb.stage_pack(lib / "A.sloppak", tmp_path / "stage2")
    assert sorted(p.relative_to(staged).as_posix() for p in staged.rglob("*") if p.is_file()) == [
        "arrangements/drums.json", "arrangements/lead.json", "arrangements/rhythm.json",
        "manifest.yaml"]                                                # no stems


def test_report_files_written(tmp_path):
    folder = _chart_folder(tmp_path / "ch" / "song", levels=True)
    lib = tmp_path / "lib"
    _pack(lib, "A.sloppak", {"lead": _wire("Lead")},
          {"source": "chart", "folder": str(folder), "warp": {"offset": 1.0}})
    out = tmp_path / "out"
    assert _run(lib, out) == 0
    rep = _report(out)
    assert rep["generator_version"] == ffb.ac.GENERATOR_VERSION
    assert {"verdict", "verdict_tune", "counts", "rows"} <= set(rep)
    assert rep["verdict"]["pass"] is False                         # one song-part < 20
    with (out / "songs.csv").open() as fh:
        csv_rows = list(csv.DictReader(fh))
    assert len(csv_rows) == 1 and csv_rows[0]["arrangement"] == "Lead"
    md = (out / "report.md").read_text()
    for heading in ("Verdict (holdout)", "Pass line", "Per-level medians", "Baselines",
                    "Worst 10", "Best 10"):
        assert heading in md
    svg = out / "svg" / "A__guitar.svg"
    assert svg.is_file() and svg.read_text().startswith("<svg")
    assert "real expert" in svg.read_text() and "auto easy" in svg.read_text()


def test_reducers_mode(tmp_path):
    folder = _chart_folder(tmp_path / "ch" / "song", levels=True)
    got = ffb.bench_reducers(folder)
    assert set(got) == {"guitar"}
    g = got["guitar"]
    assert g["real_levels"] == ["hard", "medium", "easy"]
    for red in ("simple", "onyx"):
        assert all(g[red][lv]["has_real"] for lv in ("hard", "medium", "easy"))
        assert g[red]["medium"]["fret_limit_ok"] and g[red]["easy"]["fret_limit_ok"]

    lib = tmp_path / "lib"
    _pack(lib, "A.sloppak", {"lead": _wire("Lead")},
          {"source": "chart", "folder": str(folder), "warp": {"offset": 1.0}})
    out = tmp_path / "out"
    assert _run(lib, out, "--reducers") == 0
    data = json.loads((out / "reducers.json").read_text())
    assert len(data["rows"]) == 1 and data["rows"][0]["split"] == fb.split("A.sloppak")
    dec = data["decision"]
    assert dec["decision"] in ("simple", "onyx")
    assert set(dec["levels"]) == {"hard", "medium", "easy"}
    for lv in dec["levels"].values():
        assert set(lv["medians"]) == {"simple", "onyx"}
        assert set(lv["medians"]["onyx"]) >= {"direction", "lane_closeness", "chord_size",
                                             "abs_log_density"}
    assert "Decision" in (out / "reducers.md").read_text()


def test_reducer_decision_rule():
    def lv(direction, lane, chord, dens, ok=True):
        return {"has_real": True, "direction": direction, "lane_closeness": lane,
                "chord_size": chord, "density_ratio": dens, "fret_limit_ok": ok}

    def row(onyx, simple, split="holdout"):
        return {"split": split, "onyx": {k: onyx for k in ("hard", "medium", "easy")},
                "simple": {k: simple for k in ("hard", "medium", "easy")}}

    good, base = lv(0.8, 0.5, 0.9, 1.0), lv(0.7, 0.6, 0.8, 1.5)
    assert ffb.reducer_decision([row(good, base)])["decision"] == "onyx"
    # beats on only 2 of 4 medians -> simple
    two = lv(0.8, 0.5, 0.7, 2.0)
    assert ffb.reducer_decision([row(two, base)])["decision"] == "simple"
    # a fret-limit violation anywhere (even a tune row) -> simple
    rows = [row(good, base), row(lv(0.8, 0.5, 0.9, 1.0, ok=False), base, split="tune")]
    assert ffb.reducer_decision(rows)["decision"] == "simple"
