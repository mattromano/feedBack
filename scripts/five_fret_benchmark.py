"""Benchmark the five-fret auto-charter against real (hand-charted) 5-fret charts.

  python scripts/five_fret_benchmark.py LIBRARY --out DIR [--stage DIR] [--only REGEX]
         [--limit N] [--split tune|holdout|all] [--reducers] [--svg N]

For every *.sloppak in LIBRARY that has a real 5-fret chart for a part, the auto-charter's
chart of the best-matching Rocksmith arrangement is scored against it (lib/fivefret_bench.py).
The real chart comes from, in order:
  1. the pack's own `type: five_fret` parts with `source.kind: "chart"` (already aligned);
  2. the pack's drum part `source.folder` (a cached Clone Hero folder; a Windows path is
     remapped by its last folder name under ~/.cache/slopsmith-drums/{chorus,sng}/), its
     guitar / rhythm / bass parts mapped through the drum part's `source.warp`
     (chart time -> song time). A drum part from Guitar Pro (no folder) or with a null
     warp is skipped; each skip reason is counted in the report.
A real guitar / rhythm chart is compared with every non-bass fretted arrangement, a bass
chart with every bass arrangement; the arrangement with the best Expert onset F1 is kept,
and a song-part below 0.50 is `unpaired` (left out of the verdict).

Read-only on the library: each pack's manifest.yaml, arrangement / five-fret / drum /
song-timeline JSON members are copied into a scratch stage (never the stems), and only the
staged copy is loaded (sloppak.load_song on a dir-form pack, so manifest overrides and
beats apply). Each staged copy is removed once scored; only the SVG songs' stay. The script refuses (exit 2) when --out or --stage lies inside LIBRARY.
Exit code is 0 otherwise; the verdict is in the report.

Outputs in --out: report.json, songs.csv, report.md, svg/<pack>__<part>.svg (lane strips,
real above auto, first 30 s of Expert and Easy) and, with --reducers, reducers.json /
reducers.md (Plan 1's "simple" reducer vs Onyx's on real charts' own Expert).
"""
from __future__ import annotations

import argparse
import bisect
import copy
import csv
import functools
import json
import logging
import math
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "lib") not in sys.path:
    sys.path.insert(0, str(ROOT / "lib"))

import fivefret  # noqa: E402
import fivefret_autochart as ac  # noqa: E402
import fivefret_bench as fb  # noqa: E402
import fivefretchart  # noqa: E402
import sloppak  # noqa: E402
from drumjoin import is_drums_arrangement  # noqa: E402
from fivefretchart import FretNote  # noqa: E402
from song import arrangement_is_bass  # noqa: E402

CACHE = Path.home() / ".cache" / "slopsmith-drums"
DLC_ROOT = Path.home() / "Desktop" / "rocksmith" / "dlc"
CH_SONGS = Path.home() / "Desktop" / "clone_hero_songs"
STAGE_MARKER = ".five_fret_benchmark_stage"     # a staged dir this script made (safe to replace)
NOT_FRETTED = ("drums", "drum", "piano", "keys")
LOWER = ("hard", "medium", "easy")
SVG_SECONDS = 30.0
LANE_COLORS = ("#2ecc40", "#ff4136", "#ffdc00", "#0074d9", "#ff851b")
OPEN_COLOR = "#b10dc9"


class SkipPack(Exception):
    """A pack with no usable real chart; `reason` is counted in the report."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ── warp ────────────────────────────────────────────────────────────────────

def warp_fn(warp):
    """Chart time -> song time. `{"offset": x}` gives t + x; `[[chart_t, song_t], …]` is
    piecewise linear, extrapolated past either end with the edge segment's slope."""
    if isinstance(warp, dict):
        off = float(warp.get("offset", 0.0))
        return lambda t: t + off
    pairs = sorted((float(a), float(b)) for a, b in warp)
    if not pairs:
        raise ValueError("empty warp")
    xs = [a for a, _ in pairs]
    ys = [b for _, b in pairs]
    if len(xs) == 1:
        return lambda t: ys[0] + (t - xs[0])

    def seg(i: int, t: float) -> float:
        dx = xs[i + 1] - xs[i]
        r = (ys[i + 1] - ys[i]) / dx if dx else 1.0
        return ys[i] + (t - xs[i]) * r

    def f(t: float) -> float:
        i = bisect.bisect_right(xs, t) - 1
        return seg(min(max(i, 0), len(xs) - 2), t)
    return f


def _warp_notes(notes, w) -> list[FretNote]:
    out = []
    for n in notes:
        t = w(n.time)
        end = w(n.time + n.length) if n.length > 0 else t
        out.append(FretNote(time=t, frets=n.frets, length=max(0.0, end - t),
                            hopo=n.hopo, tap=n.tap, tick=n.tick))
    out.sort(key=lambda n: n.time)
    return out


# ── pairing source (works on a library zip and on a staged dir alike) ─────────

def _read_json(pack: Path, rel) -> object | None:
    if not isinstance(rel, str) or not rel.strip():
        return None
    raw = sloppak.read_member_bytes(pack, rel)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError):
        return None


def _manifest(pack: Path) -> dict:
    try:
        return sloppak.load_manifest(pack)
    except (OSError, ValueError, zipfile.BadZipFile) as e:
        raise SkipPack("bad_pack") from e
    except Exception as e:  # yaml errors
        raise SkipPack("bad_pack") from e


def _entries(manifest: dict) -> list[dict]:
    return [e for e in manifest.get("arrangements") or [] if isinstance(e, dict)]


def _pack_chart_parts(pack: Path, manifest: dict) -> list[tuple[str, dict]]:
    """(instrument, five_fret payload) for the pack's own `source.kind: "chart"` parts."""
    out = []
    for e in _entries(manifest):
        if str(e.get("type") or "").strip().lower() != "five_fret":
            continue
        if e.get("instrument") not in fivefret.PARTS:
            continue
        data = _read_json(pack, e.get("five_fret"))
        if data is None or not fivefret.validate_five_fret(data)[0]:
            continue
        if data["source"].get("kind") == "chart":
            out.append((e["instrument"], data))
    return out


def _drum_source(pack: Path, manifest: dict) -> dict | None:
    """`source` of the pack's drum part JSON (the 0.2.9 tools' arrangements/drums.json
    `drums.source`, or a feedpak drum tab's `source`), or None."""
    for e in _entries(manifest):
        if not is_drums_arrangement(e):
            continue
        for rel in (e.get("file"), e.get("drum_tab")):
            data = _read_json(pack, rel)
            if not isinstance(data, dict):
                continue
            drums = data.get("drums") if isinstance(data.get("drums"), dict) else data
            src = drums.get("source")
            if isinstance(src, dict):
                return src
    return None


def _chart_folder(folder: str) -> Path:
    """The cached chart folder, remapping a Windows path by its last folder name."""
    if re.match(r"^[A-Za-z]:[\\/]", folder) or "\\" in folder:
        name = PureWindowsPath(folder).name
        for sub in ("chorus", "sng"):
            cand = CACHE / sub / name
            if name and cand.is_dir():
                return cand
        raise SkipPack("windows_path_unmapped")
    p = Path(folder)
    if not p.is_dir():
        raise SkipPack("folder_missing")
    return p


def pairing_source(pack: Path) -> dict:
    """Where the pack's real chart comes from: {"kind": "pack", "parts": [...]} or
    {"kind": "drums", "folder": Path, "warp": ...}. Raises SkipPack with the reason."""
    manifest = _manifest(pack)
    parts = _pack_chart_parts(pack, manifest)
    if parts:
        return {"kind": "pack", "parts": parts}
    src = _drum_source(pack, manifest)
    if src is None:
        raise SkipPack("no_drum_source")
    folder = src.get("folder")
    if not isinstance(folder, str) or not folder:
        raise SkipPack("gp_no_folder" if src.get("source") == "gp" or src.get("gp") else "no_folder")
    warp = src.get("warp")
    if warp is None:
        raise SkipPack("null_warp")
    if not isinstance(warp, (dict, list)) or (isinstance(warp, list) and not warp):
        raise SkipPack("bad_warp")
    return {"kind": "drums", "folder": _chart_folder(folder), "warp": warp}


@functools.lru_cache(maxsize=4)
def _load_folder(folder: str):
    """fivefretchart.load_song_folder, cached so --reducers reuses the pack's parse.
    Callers must not mutate the charts (bench_reducers works on deep copies)."""
    return fivefretchart.load_song_folder(Path(folder))[0]


def _wire_notes(notes) -> list[FretNote]:
    out = [FretNote(time=float(n["t"]), frets=int(n["f"]), length=float(n.get("l", 0.0)),
                    hopo=bool(n.get("h")), tap=bool(n.get("tap"))) for n in notes or []]
    out.sort(key=lambda n: n.time)
    return out


def _real_parts_info(staged: Path) -> tuple[dict, dict]:
    """(part -> level -> notes with only hand-charted levels, info). info has `source`,
    `folder` (drum source only) and `excluded` (part -> lower levels left out because they
    are missing or generated)."""
    src = pairing_source(staged)
    parts: dict[str, dict[str, list[FretNote]]] = {}
    excluded: dict[str, list[str]] = {}
    if src["kind"] == "pack":
        for inst, data in src["parts"]:
            if inst in parts:
                continue
            lv = {"expert": _wire_notes(data["notes"])}
            levels = data.get("levels") or {}
            for name in LOWER:
                entry = levels.get(name)
                if entry and entry.get("notes") and not entry.get("generated"):
                    lv[name] = _wire_notes(entry["notes"])
            parts[inst] = lv
            excluded[inst] = [n for n in LOWER if n not in lv]
        return parts, {"source": "pack", "excluded": excluded}
    try:
        charts = _load_folder(str(src["folder"]))
    except Exception as e:
        raise SkipPack("chart_load_error") from e
    if not charts:
        raise SkipPack("no_five_fret_parts")
    w = warp_fn(src["warp"])
    for part, chart in charts.items():
        lv = {"expert": _warp_notes(chart.notes, w)}
        for name in LOWER:
            if chart.levels.get(name) and name not in chart.levels_generated:
                lv[name] = _warp_notes(chart.levels[name], w)
        parts[part] = lv
        excluded[part] = [n for n in LOWER if n not in lv]
    return parts, {"source": "drums", "folder": str(src["folder"]), "excluded": excluded}


def real_parts(staged: Path) -> dict[str, dict[str, list[FretNote]]]:
    """part -> level -> notes (song time) of the pack's real chart; hand-charted levels only."""
    return _real_parts_info(staged)[0]


# ── staging ─────────────────────────────────────────────────────────────────

def _member_names(src: Path) -> list[str]:
    if src.is_dir():
        return [p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file()]
    with zipfile.ZipFile(str(src), "r") as zf:
        return [i.filename for i in zf.infolist() if not i.is_dir()]


def stage_pack(src: Path, stage: Path) -> Path:
    """Copy the JSON a benchmark needs (manifest, arrangements, five-fret parts, the drum
    part's JSON, song timeline) into a dir-form pack under `stage`. Stems never."""
    manifest = _manifest(src)
    wanted = set()
    for e in _entries(manifest):
        for key in ("file", "five_fret", "drum_tab"):
            if isinstance(e.get(key), str):
                wanted.add(sloppak._zip_member_key(e[key]))
    for key in ("song_timeline", "drum_tab"):
        if isinstance(manifest.get(key), str):
            wanted.add(sloppak._zip_member_key(manifest[key]))
    dest = stage / src.name
    if dest.exists():
        if not (dest / STAGE_MARKER).is_file():
            raise SkipPack("stage_dir_not_ours")       # never delete what we didn't make
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    (dest / STAGE_MARKER).write_text("staged by scripts/five_fret_benchmark.py\n", encoding="utf-8")
    for name in _member_names(src):
        key = sloppak._zip_member_key(name)
        if key is None:
            continue
        base = key.rsplit("/", 1)[-1]
        take = key in ("manifest.yaml", "manifest.yml") or (
            key.endswith(".json") and (key in wanted or key.startswith("arrangements/")
                                       or base.startswith("five_fret")
                                       or key == "song_timeline.json"))
        if not take:
            continue
        data = sloppak.read_member_bytes(src, key)
        if data is None:
            continue
        out = dest / key
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(data)
    return dest


def _load_staged(staged: Path):
    staged = staged.resolve()
    sloppak._source_cache.pop(staged.name, None)        # dir-form: no unpack, no stale cache
    loaded = sloppak.load_song(staged.name, staged.parent, staged.parent)
    sloppak._source_cache.pop(staged.name, None)
    return loaded


# ── scoring ─────────────────────────────────────────────────────────────────

def _is_fretted(arr) -> bool:
    """Not drums / piano / keys (by type), nor a drums arrangement by name."""
    return (arr.type or "").strip().lower() not in NOT_FRETTED \
        and not is_drums_arrangement(arr.name or "")


def _baseline_absolute(arr, auto_notes) -> list[FretNote]:
    """Lane = quintile of the event's mean pitch over the whole song (open stays open)."""
    events = ac.arrangement_events(arr)
    means = sorted(sum(e.pitches) / len(e.pitches) for e in events if e.pitches)
    out = []
    for e, n in zip(events, auto_notes):
        if e.pitches and means:
            q = min(4, 5 * bisect.bisect_left(means, sum(e.pitches) / len(e.pitches)) // len(means))
            frets = 1 << q
        else:
            frets = 0
        out.append(FretNote(time=n.time, frets=frets, length=n.length))
    return out


def _baseline(real, notes) -> dict:
    pairs = fb.match_notes(real, notes)
    return {"direction": fb.direction_agreement(pairs), "lane_closeness": fb.lane_closeness(pairs)}


def _bench(staged: Path, name: str, keep_notes: bool = False) -> tuple[list[dict], dict]:
    """Rows for every real part of a staged pack, plus info (source, parts with no
    candidate arrangement)."""
    parts, info = _real_parts_info(staged)
    loaded = _load_staged(staged)
    grid = ac.BeatGrid.from_beats(loaded.song.beats)
    arrs = [a for a in loaded.song.arrangements if _is_fretted(a) and (a.notes or a.chords)]
    autos: dict[int, object] = {}

    def auto_of(i):
        if i not in autos:
            autos[i] = ac.autochart(arrs[i], grid, "guitar")
        return autos[i]

    rows, no_candidate = [], []
    sp = fb.split(name)
    for part, real in parts.items():
        want_bass = part == "bass"
        cands = [i for i, a in enumerate(arrs) if arrangement_is_bass(a) == want_bass]
        if not cands or not real["expert"]:
            no_candidate.append(part)
            continue
        f1s = {i: fb.onset_f1(real["expert"], auto_of(i).notes) for i in cands}
        best = max(cands, key=lambda i: (f1s[i], -i))
        chart = copy.deepcopy(auto_of(best))
        chart.part = part
        ac.auto_levels(chart, grid)
        auto = {"expert": chart.notes, **{lv: chart.levels.get(lv) or [] for lv in LOWER}}
        stay = [FretNote(time=n.time, frets=1, length=n.length) for n in chart.notes]
        row = {
            "pack": name, "part": part, "arrangement": arrs[best].name, "split": sp,
            "paired": f1s[best] >= fb.MIN_ONSET_F1, "onset_f1": f1s[best],
            "candidates": {arrs[i].name: round(f1s[i], 4) for i in cands},
            "source": info["source"], "real_levels": ["expert"] + [lv for lv in LOWER if lv in real],
            "real_excluded": info["excluded"].get(part, []),
            "n_real": len(real["expert"]), "n_auto": len(chart.notes),
            "levels": fb.score_levels(real, auto),
            "baselines": {"stay": _baseline(real["expert"], stay),
                          "absolute": _baseline(real["expert"],
                                                _baseline_absolute(arrs[best], chart.notes))},
        }
        if keep_notes:
            row["_notes"] = {"real": real, "auto": auto}
        rows.append(row)
    info = dict(info, no_candidate=no_candidate)
    return rows, info


def bench_pack(staged: Path, name: str) -> list[dict]:
    """`verdict` rows (plus arrangement, onset F1 and the stay / absolute baselines) for
    every real part of a staged pack."""
    return _bench(staged, name)[0]


# ── reducers ────────────────────────────────────────────────────────────────

def bench_reducers(folder: Path) -> dict:
    """For every part of a Clone Hero folder with at least one hand-charted lower level:
    `score_levels` of Plan 1's "simple" reducer and Onyx's, each run on the chart's own
    Expert, against the chart's hand-charted levels (chart time; no warp needed)."""
    charts = _load_folder(str(folder))
    out = {}
    for part, chart in charts.items():
        real = {"expert": sorted(chart.notes, key=lambda n: n.time)}
        for lv in LOWER:
            if chart.levels.get(lv) and lv not in chart.levels_generated:
                real[lv] = sorted(chart.levels[lv], key=lambda n: n.time)
        if len(real) == 1:
            continue
        entry = {"real_levels": [lv for lv in LOWER if lv in real]}
        for reducer in fivefretchart.REDUCERS:
            c = copy.deepcopy(chart)
            c.levels, c.levels_generated = {}, []
            fivefretchart.ensure_levels(c, reducer=reducer)
            gen = {"expert": c.notes, **{lv: c.levels.get(lv) or [] for lv in LOWER}}
            entry[reducer] = fb.score_levels(real, gen)
        out[part] = entry
    return out


REDUCER_METRICS = (("direction", True), ("lane_closeness", False), ("chord_size", True),
                   ("abs_log_density", False))


def _abs_log_density(entry):
    d = entry.get("density_ratio")
    return None if d is None else abs(math.log(max(d, 1e-6)))


def _metric(entry, key):
    return _abs_log_density(entry) if key == "abs_log_density" else entry.get(key)


_med = fb._med


def reducer_decision(rows: list[dict]) -> dict:
    """Holdout medians per level for both reducers; "onyx" wins only if, at every lower
    level, it beats "simple" on at least 3 of the 4 medians and breaks no fret limit
    (violations counted over every row)."""
    hold = [r for r in rows if r["split"] == "holdout"]
    levels, all_win = {}, True
    for lv in LOWER:
        meds = {}
        for red in fivefretchart.REDUCERS:
            have = [r[red][lv] for r in hold if r[red][lv]["has_real"]]
            meds[red] = {k: _med(_metric(e, k) for e in have) for k, _ in REDUCER_METRICS}
            meds[red]["rows"] = len(have)
        better = []
        for k, higher in REDUCER_METRICS:
            o, s = meds["onyx"][k], meds["simple"][k]
            if o is not None and s is not None and (o > s if higher else o < s):
                better.append(k)
        viol = {red: sum(not r[red][lv]["fret_limit_ok"] for r in rows)
                for red in fivefretchart.REDUCERS}
        wins = len(better) >= 3 and viol["onyx"] == 0
        all_win &= wins
        levels[lv] = {"medians": meds, "onyx_better": better, "violations": viol,
                      "onyx_wins": wins}
    return {"decision": "onyx" if all_win else "simple", "n": len(rows), "holdout": len(hold),
            "levels": levels}


# ── outputs ─────────────────────────────────────────────────────────────────

def _f(v, nd=3):
    if v is None:
        return "–"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _exp(r, key):
    return r["levels"]["expert"].get(key)


CSV_FIELDS = ("pack", "part", "arrangement", "split", "paired", "source", "onset_f1",
              "n_real", "n_auto", "expert_matched", "expert_direction", "expert_lane_closeness",
              "expert_chord_size", "expert_hopo", "hard_density", "medium_density",
              "easy_density", "medium_fret_ok", "easy_fret_ok", "stay_direction",
              "stay_lane_closeness", "absolute_direction", "absolute_lane_closeness",
              "real_excluded")


def _csv_row(r) -> dict:
    lv = r["levels"]
    return {
        "pack": r["pack"], "part": r["part"], "arrangement": r["arrangement"],
        "split": r["split"], "paired": r["paired"], "source": r["source"],
        "onset_f1": round(r["onset_f1"], 4), "n_real": r["n_real"], "n_auto": r["n_auto"],
        "expert_matched": _exp(r, "matched"), "expert_direction": _exp(r, "direction"),
        "expert_lane_closeness": _exp(r, "lane_closeness"),
        "expert_chord_size": _exp(r, "chord_size"), "expert_hopo": _exp(r, "hopo"),
        "hard_density": lv["hard"]["density_ratio"], "medium_density": lv["medium"]["density_ratio"],
        "easy_density": lv["easy"]["density_ratio"],
        "medium_fret_ok": lv["medium"]["fret_limit_ok"], "easy_fret_ok": lv["easy"]["fret_limit_ok"],
        "stay_direction": r["baselines"]["stay"]["direction"],
        "stay_lane_closeness": r["baselines"]["stay"]["lane_closeness"],
        "absolute_direction": r["baselines"]["absolute"]["direction"],
        "absolute_lane_closeness": r["baselines"]["absolute"]["lane_closeness"],
        "real_excluded": " ".join(r["real_excluded"]),
    }


def _ranked(rows):
    """Paired rows with an Expert direction score, worst first."""
    return sorted((r for r in rows if r["paired"] and _exp(r, "direction") is not None),
                  key=lambda r: (_exp(r, "direction"), r["pack"], r["part"]))


def _level_medians(rows) -> dict:
    out = {}
    for lv in fb.LEVELS:
        have = [r["levels"][lv] for r in rows if r["levels"][lv]["has_real"]]
        out[lv] = {"rows": len(have), **{k: _med(e[k] for e in have) for k in
                   ("onset_f1", "direction", "lane_closeness", "chord_size", "hopo",
                    "density_ratio")}}
    return out


def _baseline_medians(rows) -> dict:
    exp = [r for r in rows if (_exp(r, "matched") or 0) >= fb.MIN_MATCHED]
    out = {"auto": {k: _med(_exp(r, k) for r in exp) for k in ("direction", "lane_closeness")}}
    for b in ("stay", "absolute"):
        out[b] = {k: _med(r["baselines"][b][k] for r in exp) for k in ("direction", "lane_closeness")}
    out["rows"] = len(exp)
    return out


def write_report(out: Path, rows: list[dict], counts: dict, verdicts: dict) -> dict:
    hold = [r for r in rows if r["paired"] and r["split"] == "holdout"]
    report = {"generator_version": ac.GENERATOR_VERSION, "generator": ac.GENERATOR,
              "counts": counts, "verdict": verdicts["holdout"], "verdict_tune": verdicts["tune"],
              "level_medians_holdout": _level_medians(hold),
              "baselines_holdout": _baseline_medians(hold), "rows": rows}
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    with (out / "songs.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(_csv_row(r))

    v = verdicts["holdout"]
    md = [f"# Five-fret auto-chart benchmark (generator v{ac.GENERATOR_VERSION})", "",
          f"**Verdict (holdout): {'PASS' if v['pass'] else 'FAIL'}** on {v['n']} paired "
          "holdout song-parts.", "",
          "## Counts", "", "| | n |", "|---|---|"]
    md += [f"| {k} | {val} |" for k, val in counts.items() if not isinstance(val, dict)]
    for k, val in counts.items():
        if isinstance(val, dict):
            md += [f"| {k}: {kk} | {vv} |" for kk, vv in sorted(val.items())]
    md += ["", "## Pass line (holdout)", "", "| check | level | value | line | ok | note |",
           "|---|---|---|---|---|---|"]
    for c in v["checks"]:
        line = c["line"]
        line = f"{line[0]}–{line[1]}" if isinstance(line, tuple) else line
        note = ", ".join(f"{k} {c[k]}" for k in ("rows", "excluded") if k in c)
        md.append(f"| {c['name']} | {c['level'] or ''} | {_f(c['value'])} | {line} | "
                  f"{'yes' if c['ok'] else 'NO'} | {note} |")
    md += ["", "Fret-limit checks count every row (any split, paired or not).", "",
           "## Per-level medians (paired holdout)", "",
           "| level | rows | onset F1 | direction | lane closeness | chord size | HOPO | density |",
           "|---|---|---|---|---|---|---|---|"]
    for lv, m in report["level_medians_holdout"].items():
        md.append(f"| {lv} | {m['rows']} | {_f(m['onset_f1'])} | {_f(m['direction'])} | "
                  f"{_f(m['lane_closeness'])} | {_f(m['chord_size'])} | {_f(m['hopo'])} | "
                  f"{_f(m['density_ratio'])} |")
    b = report["baselines_holdout"]
    md += ["", f"## Baselines (Expert, paired holdout with ≥ {fb.MIN_MATCHED} matched; "
           f"{b['rows']} rows; context only)", "",
           "| chart | direction | lane closeness |", "|---|---|---|"]
    for k in ("auto", "stay", "absolute"):
        md.append(f"| {k} | {_f(b[k]['direction'])} | {_f(b[k]['lane_closeness'])} |")
    ranked = _ranked(rows)
    for title, sel in (("Worst 10 (paired, by Expert direction)", ranked[:10]),
                       ("Best 10 (paired, by Expert direction)", ranked[::-1][:10])):
        md += ["", f"## {title}", "",
               "| pack | part | arrangement | split | onset F1 | direction | lane | chord |",
               "|---|---|---|---|---|---|---|---|"]
        md += [f"| {r['pack']} | {r['part']} | {r['arrangement']} | {r['split']} | "
               f"{_f(r['onset_f1'])} | {_f(_exp(r, 'direction'))} | "
               f"{_f(_exp(r, 'lane_closeness'))} | {_f(_exp(r, 'chord_size'))} |" for r in sel]
    (out / "report.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return report


def _strip(notes, y, t0, label, px_per_s, lane_h) -> list[str]:
    out = [f'<text x="4" y="{y + lane_h * 3}" font-size="11" fill="#ddd">{label}</text>']
    x0 = 80
    for i in range(5):
        out.append(f'<line x1="{x0}" x2="{x0 + SVG_SECONDS * px_per_s:.0f}" '
                   f'y1="{y + i * lane_h + lane_h / 2}" y2="{y + i * lane_h + lane_h / 2}" '
                   'stroke="#333" stroke-width="1"/>')
    for n in notes:
        if not t0 <= n.time < t0 + SVG_SECONDS:
            continue
        x = x0 + (n.time - t0) * px_per_s
        if n.length > 0:
            w = min(n.length, t0 + SVG_SECONDS - n.time) * px_per_s
            for i in range(5):
                if n.frets >> i & 1:
                    out.append(f'<rect x="{x:.1f}" y="{y + i * lane_h + lane_h / 2 - 1:.1f}" '
                               f'width="{w:.1f}" height="2" fill="{LANE_COLORS[i]}" opacity="0.6"/>')
        if not n.frets:
            out.append(f'<rect x="{x - 1:.1f}" y="{y}" width="2" height="{lane_h * 5}" '
                       f'fill="{OPEN_COLOR}"/>')
            continue
        for i in range(5):
            if n.frets >> i & 1:
                stroke = ' stroke="#fff" stroke-width="1"' if n.hopo or n.tap else ""
                out.append(f'<rect x="{x - 3:.1f}" y="{y + i * lane_h + 1}" width="6" '
                           f'height="{lane_h - 2}" rx="2" fill="{LANE_COLORS[i]}"{stroke}/>')
    return out


def write_svg(path: Path, row: dict) -> None:
    real, auto = row["_notes"]["real"], row["_notes"]["auto"]
    starts = [n[0].time for n in (real["expert"], auto["expert"]) if n]
    t0 = max(0.0, min(starts)) if starts else 0.0
    px, lane_h, gap = 40, 8, 14
    body, y = [], 22
    for lv in ("expert", "easy"):
        for side, notes in (("real", real.get(lv)), ("auto", auto.get(lv))):
            label = f"{side} {lv}" if notes is not None else f"{side} {lv} (none)"
            body += _strip(notes or [], y, t0, label, px, lane_h)
            y += lane_h * 5 + gap
        y += gap
    w = 80 + int(SVG_SECONDS * px) + 10
    title = (f"{row['pack']} — {row['part']} vs {row['arrangement']} "
             f"(direction {_f(_exp(row, 'direction'))}, from {t0:.1f} s)")
    title = title.replace("&", "&amp;").replace("<", "&lt;")
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{y}" '
           f'viewBox="0 0 {w} {y}">', f'<rect width="{w}" height="{y}" fill="#111"/>',
           f'<text x="4" y="14" font-size="12" fill="#fff">{title}</text>', *body, "</svg>"]
    path.write_text("\n".join(svg) + "\n", encoding="utf-8")


def pick_svg_rows(rows: list[dict], n: int) -> list[dict]:
    """Two worst, two median, two best paired rows by Expert direction (for n = 6)."""
    ranked = _ranked(rows)
    if n <= 0 or not ranked:
        return []
    per = max(1, n // 3)
    mid_n = n - 2 * per
    m0 = max(0, len(ranked) // 2 - mid_n // 2)
    picks, seen = [], set()
    for r in ranked[-per:][::-1] + ranked[m0:m0 + mid_n] + ranked[:per]:
        key = (r["pack"], r["part"])
        if key not in seen:
            seen.add(key)
            picks.append(r)
    return picks[:n]


def write_reducers(out: Path, rows: list[dict]) -> dict:
    dec = reducer_decision(rows)
    data = {"decision": dec, "rows": rows}
    (out / "reducers.json").write_text(json.dumps(data, indent=1, default=str), encoding="utf-8")
    md = ["# Reducer benchmark: real Expert -> lower levels, vs the real hand-charted levels", "",
          f"**Decision: `{dec['decision']}`** ({dec['n']} chart parts, {dec['holdout']} holdout). "
          '"onyx" wins only if, at every level, it beats "simple" on at least 3 of the 4 '
          "holdout medians and has 0 fret-limit violations.", "",
          "| level | reducer | rows | direction ↑ | lane closeness ↓ | chord size ↑ | "
          "abs log density ↓ | fret-limit violations |", "|---|---|---|---|---|---|---|---|"]
    for lv, d in dec["levels"].items():
        for red in fivefretchart.REDUCERS:
            m = d["medians"][red]
            md.append(f"| {lv} | {red} | {m['rows']} | {_f(m['direction'])} | "
                      f"{_f(m['lane_closeness'])} | {_f(m['chord_size'])} | "
                      f"{_f(m['abs_log_density'])} | {d['violations'][red]} |")
        md.append(f"| {lv} | onyx better on | | {', '.join(d['onyx_better']) or 'none'} | | | | "
                  f"onyx wins: {'yes' if d['onyx_wins'] else 'no'} |")
    (out / "reducers.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return dec


# ── main ────────────────────────────────────────────────────────────────────

def _inside(p: Path, root: Path) -> bool:
    p, root = p.resolve(), root.resolve()
    return p == root or root in p.parents


def _unsafe_target(p: Path, library: Path) -> Path | None:
    """The read-only root `p` overlaps (inside it, or an ancestor of it), or None.
    Read-only: LIBRARY, the whole Rocksmith dlc root, and the Clone Hero caches."""
    for root in (library, DLC_ROOT, CACHE, CH_SONGS):
        if _inside(p, root) or _inside(root, p):
            return root
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("library", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--stage", type=Path)
    ap.add_argument("--only")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--split", choices=("tune", "holdout", "all"), default="all")
    ap.add_argument("--reducers", action="store_true")
    ap.add_argument("--svg", type=int, default=6)
    a = ap.parse_args(argv)
    stage = a.stage or a.out / "stage"
    for label, p in (("--out", a.out), ("--stage", stage)):
        root = _unsafe_target(p, a.library)
        if root is not None:
            print(f"refusing: {label} {p} overlaps the read-only {root} (inside it or "
                  "containing it)", file=sys.stderr)
            return 2
    logging.getLogger("sloppak").setLevel(logging.ERROR)
    logging.getLogger("fivefretchart").setLevel(logging.ERROR)

    packs = sorted(p for p in a.library.iterdir() if p.name.endswith(".sloppak"))
    if a.only:
        rx = re.compile(a.only, re.I)
        packs = [p for p in packs if rx.search(p.name)]
    if a.split != "all":
        packs = [p for p in packs if fb.split(p.name) == a.split]
    if a.limit is not None:
        packs = packs[:a.limit]
    a.out.mkdir(parents=True, exist_ok=True)
    stage.mkdir(parents=True, exist_ok=True)

    rows, red_rows, pack_of = [], [], {}
    skips: dict[str, int] = {}
    errors: list[str] = []
    counts = {"packs": len(packs), "staged": 0, "song_parts": 0, "paired": 0, "unpaired": 0,
              "no_candidate_arrangement": 0}
    t_start = time.time()
    for k, pack in enumerate(packs, 1):
        if k % 50 == 0:
            print(f"[{k}/{len(packs)}] staged {counts['staged']} paired {counts['paired']} "
                  f"({time.time() - t_start:.0f} s)", file=sys.stderr, flush=True)
        staged = None
        try:
            src = pairing_source(pack)              # cheap, read-only: skip early
            staged = stage_pack(pack, stage)
            counts["staged"] += 1
            pack_rows, info = _bench(staged, pack.name)
        except SkipPack as e:
            skips[e.reason] = skips.get(e.reason, 0) + 1
            continue
        except Exception as e:  # one bad pack never stops the run
            skips["error"] = skips.get("error", 0) + 1
            errors.append(f"{pack.name}: {type(e).__name__}: {e}")
            continue
        finally:
            # Arrangement JSON is MBs a pack: drop each staged copy once scored (the SVG
            # picks are re-staged at the end), so a full run never holds the whole library.
            if staged is not None:
                shutil.rmtree(staged, ignore_errors=True)
        pack_of[pack.name] = pack
        counts["no_candidate_arrangement"] += len(info["no_candidate"])
        rows += pack_rows
        if a.reducers and src["kind"] == "drums" and any(r["paired"] for r in pack_rows):
            try:
                red = bench_reducers(src["folder"])
            except Exception as e:
                errors.append(f"{pack.name} (reducers): {type(e).__name__}: {e}")
                red = {}
            for r in pack_rows:
                if r["paired"] and r["part"] in red:
                    red_rows.append({"pack": pack.name, "part": r["part"],
                                     "split": fb.split(pack.name), **red[r["part"]]})
    counts["song_parts"] = len(rows)
    counts["paired"] = sum(r["paired"] for r in rows)
    counts["unpaired"] = counts["song_parts"] - counts["paired"]
    counts["seconds"] = round(time.time() - t_start, 1)
    counts["skipped"] = dict(sorted(skips.items()))
    tune_rows = [dict(r, split="holdout") for r in rows if r["split"] == "tune"]
    verdicts = {"holdout": fb.verdict(rows), "tune": fb.verdict(tune_rows)}
    report = write_report(a.out, rows, counts, verdicts)

    svg_dir = a.out / "svg"
    for r in pick_svg_rows(rows, a.svg):
        try:
            staged = stage_pack(pack_of[r["pack"]], stage)      # kept: the SVG's source
            again = _bench(staged, r["pack"], keep_notes=True)[0]
            full = next(x for x in again if x["part"] == r["part"])
            svg_dir.mkdir(exist_ok=True)
            write_svg(svg_dir / f"{Path(r['pack']).stem}__{r['part']}.svg", full)
        except Exception as e:
            errors.append(f"{r['pack']} (svg): {type(e).__name__}: {e}")
    if errors:
        (a.out / "errors.txt").write_text("\n".join(errors) + "\n", encoding="utf-8")
    if a.reducers:
        dec = write_reducers(a.out, red_rows)
        print(f"reducers: {dec['decision']} ({dec['n']} chart parts)", file=sys.stderr)
    v = report["verdict"]
    print(f"{counts['packs']} packs, {counts['staged']} staged, {counts['paired']} paired, "
          f"{counts['unpaired']} unpaired; skipped {counts['skipped']}; "
          f"verdict {'PASS' if v['pass'] else 'FAIL'} on {v['n']} holdout song-parts; "
          f"{counts['seconds']} s -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
