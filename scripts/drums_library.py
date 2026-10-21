"""Find Clone Hero / YARG drum charts for a whole sloppak library and join them.

mattromano custom build (ported from the Slopsmith 0.2.9 tools). Needs librosa + scipy
(``pip install librosa``); the app itself doesn't ship them. Joins are written as feedpak
drum tabs (lib/drumjoin.py). Songs that already have drums, in either the feedpak shape or
the 0.2.9 tools' guitar-style "Drums" arrangement, count as having drums.

  python scripts/drums_library.py LIBRARY_DIR --backup-dir DIR [--local CH_DIR ...] [--no-online]
         [--write] [--workers 6] [--limit N] [--only REGEX] [--redo] [--state STATE.json]

For every *.sloppak in LIBRARY_DIR (no Drums arrangement yet, unless --redo):
  1. candidate charts: fuzzy artist/title matches in the --local folders (song folders and
     .sng packages), then Chorus Encore (enchor.us) search results with an Expert drums
     part, ranked by name match and how close the chart's length is to ours;
  2. each candidate in turn is aligned to the song's audio and validated
     (scripts/drums_join.py / lib/drumjoin.py); the first one that passes is written;
  3. nothing is written without --write, and never before the song has an identical copy
     in --backup-dir (created on the spot with an APFS clone / plain copy if missing).
Progress is resumable: STATE.json records every finished song; re-running skips them.

--upgrade: instead, songs that already have Drums but fewer than three hand-charted lower levels
(Easy/Medium/Hard reduced by software, or none at all). A chart replaces theirs only when it has
more hand-charted levels than the current one AND passes the same validation; the song is backed
up to --backup-dir first (a dated copy when the pre-drums original is already there).
A CSV report (STATE.csv) lists source, chart, offset and validation numbers per song.

--parts guitar,rhythm,bass (any of them; default: drums, which is everything above): the same run
for real Clone Hero / Rock Band 5-fret charts instead, written as feedpak five_fret parts
(lib/fivefretjoin.py). A part is wanted when the song has none for that instrument or only an
auto-generated one (--upgrade: a real chart with < 3 hand-charted lower levels, replaced only by
one with more). Candidates are the --local charts (top 2 by name), then Chorus Encore results with
any wanted part; each one is aligned and validated per part (the drum join's warp is reused when
the song's drums came from the same chart, and only the chart is downloaded then), the passing
parts are written and the rest tried on the next candidate. The state defaults to
five_fret_state.json, and the CSV has one row per song and part. drums can't be mixed with the
five-fret parts in one run.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "lib"), str(ROOT / "scripts")]

CACHE = Path.home() / ".cache" / "slopsmith-drums"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ── backups ─────────────────────────────────────────────────────────────────

def ensure_backup(src: Path, backup_dir: Path) -> Path:
    """An untouched copy of src in backup_dir (same size), made now if missing."""
    dst = backup_dir / src.name
    if dst.exists() and (src.is_dir() or dst.stat().st_size == src.stat().st_size):
        return dst
    if dst.exists():          # backup differs from the current file: keep it, add a dated one
        dst = backup_dir / f"{src.stem}.{time.strftime('%Y%m%d-%H%M%S')}{src.suffix}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    cloned = sys.platform == "darwin" and not subprocess.run(
        ["cp", "-cRp", str(src), str(dst)], capture_output=True).returncode      # APFS clone
    if not cloned:
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
    return dst


# ── candidates ──────────────────────────────────────────────────────────────

def manifest_of(p: Path) -> dict:
    import zipfile
    import yaml
    if p.is_dir():
        return yaml.safe_load((p / "manifest.yaml").read_text(encoding="utf-8"))
    with zipfile.ZipFile(p) as z:
        return yaml.safe_load(z.read("manifest.yaml"))


def clean_title(t: str) -> str:
    """Drop chart-variant tags CDLC titles carry: (Songsterr), (Remastered), (Bass), [DD], v2..."""
    tag = r"(?:songsterr|remaster\w*|version|v\d[\w.]*|bass|lead|rhythm|guitar|dd|drop ?[a-g]|e ?standard)"
    t = t or ""
    while True:
        n = re.sub(r"\s*[\(\[][^)\]]*\b" + tag + r"\b[^)\]]*[\)\]]\s*$", "", t, flags=re.I).strip()
        if n == t:
            return n
        t = n


LOWER = ("easy", "medium", "hard")


def chart_levels(r: dict) -> int:
    """Hand-charted lower drum levels (Easy/Medium/Hard with notes) in a Chorus search result."""
    import chorus
    return chorus.part_levels(r, "drums")


def has_drums(man: dict) -> bool:
    """A drum part: a feedpak drum tab, or a 0.2.9-tools "Drums" arrangement."""
    import drumjoin
    return bool(man.get("drum_tab")) or any(drumjoin.is_drums_arrangement(x) for x in man.get("arrangements", []))


def _read_member(p: Path, rel: str) -> bytes:
    import zipfile
    if p.is_dir():
        return (p / rel).read_bytes()
    with zipfile.ZipFile(p) as z:
        return z.read(rel)


def current_levels(p: Path) -> int:
    """Hand-charted lower levels in the song's drum part (-1: no drums)."""
    import drumjoin
    man = manifest_of(p)
    e = next((x for x in man.get("arrangements", []) if drumjoin.is_drums_arrangement(x)), None)
    rel = (e or {}).get("drum_tab") or man.get("drum_tab")
    if rel:                                  # feedpak drum tab: levels.<lv>.generated
        lv = json.loads(_read_member(p, rel)).get("levels") or {}
        return sum(1 for k in LOWER if (lv.get(k) or {}).get("hits") and not (lv.get(k) or {}).get("generated"))
    if not e or not e.get("file"):
        return -1
    d = json.loads(_read_member(p, e["file"]))   # 0.2.9 tools: drums block in the arrangement
    blk = d.get("drums") or {}
    lv, gen = blk.get("levels") or {}, set(blk.get("levels_generated") or [])
    return sum(1 for k in LOWER if lv.get(k) and k not in gen)


def folder_levels(folder: Path) -> int:
    """Hand-charted lower levels in a downloaded / local chart folder."""
    import drumchart
    try:
        chart, _ = drumchart.load_song_folder(Path(folder), levels=True)
    except Exception:
        return 0
    gen = set(chart.levels_generated or [])
    return sum(1 for k in LOWER if chart.levels.get(k) and k not in gen)


def online_candidates(artist, title, duration, min_match, n=3) -> list[dict]:
    import chorus
    import drumjoin
    seen, out = set(), []
    for q in (f"{artist} {title}", title):
        try:
            res = chorus.search(q)
        except Exception as e:
            log(f"   chorus search failed for {q!r}: {e}")
            continue
        for r in res:
            if r.get("md5") in seen:
                continue
            s = drumjoin.name_score(artist, title, r.get("artist", ""), r.get("name", ""))
            if s < min_match:
                continue
            seen.add(r["md5"])
            ln = (r.get("song_length") or 0) / 1000.0
            dlen = abs(ln - duration) / duration if duration and ln else 0.5
            out.append({"kind": "online", "md5": r["md5"], "artist": r.get("artist"), "name": r.get("name"),
                        "charter": r.get("charter"), "score": round(s, 3), "len_diff": round(dlen, 3),
                        "pro": bool(r.get("pro_drums")), "levels": chart_levels(r)})
        if out:
            break
    # name match first, then length (live/edit versions), then charts with all four difficulties
    # (hand-charted Easy/Medium/Hard beat software-reduced ones), then pro drums
    out.sort(key=lambda c: (-round(c["score"], 1), c["len_diff"] > 0.08, -c["levels"], c["len_diff"], not c["pro"]))
    return out[:n]


# ── five-fret parts (--parts guitar,rhythm,bass) ────────────────────────────

FIVE_FRET_PARTS = ("guitar", "rhythm", "bass")


class PackView:
    """Manifest + JSON members of a pack, read without unpacking it (what
    fivefretjoin.existing_parts / reusable_warp need)."""

    def __init__(self, p: Path):
        self.path = Path(p)
        self.manifest = manifest_of(self.path)

    def read_json(self, rel: str):
        try:
            return json.loads(_read_member(self.path, rel))
        except KeyError as e:          # not in the zip
            raise FileNotFoundError(rel) from e


def wanted_parts(view, parts, upgrade: bool) -> tuple[list[str], dict[str, int] | None]:
    """The ``parts`` to look for in this pack, plus ``min_levels`` for --upgrade: normally the parts
    the pack has none of, or only an auto-generated one; with ``upgrade``, its real chart parts
    with fewer than three hand-charted lower levels (each must be beaten)."""
    import fivefretjoin
    ex = fivefretjoin.existing_parts(view)
    if upgrade:
        w = [p for p in parts if p in ex and ex[p]["kind"] == "chart" and ex[p]["levels"] < 3]
        return w, {p: ex[p]["levels"] for p in w}
    return [p for p in parts if p not in ex or ex[p]["kind"] == "auto"], None


def five_fret_candidates(artist, title, duration, parts, min_match, n=3) -> list[dict]:
    """Chorus results with any of ``parts``: ``parts_have`` (the wanted parts each has), ``levels``
    (hand-charted lower levels summed over those) and ``part_levels`` per part; ranked by name,
    length within 8 %, how many wanted parts, levels, then length difference."""
    import chorus
    import drumjoin
    seen, out = set(), []
    for q in (f"{artist} {title}", title):
        try:
            res = chorus.search(q, parts=tuple(parts))
        except Exception as e:
            log(f"   chorus search failed for {q!r}: {e}")
            continue
        for r in res:
            if r.get("md5") in seen:
                continue
            s = drumjoin.name_score(artist, title, r.get("artist", ""), r.get("name", ""))
            if s < min_match:
                continue
            have = [p for p in parts if chorus.has_part(r, p)]
            if not have:
                continue
            seen.add(r["md5"])
            ln = (r.get("song_length") or 0) / 1000.0
            dlen = abs(ln - duration) / duration if duration and ln else 0.5
            plv = {p: chorus.part_levels(r, p) for p in have}
            out.append({"kind": "online", "md5": r["md5"], "artist": r.get("artist"), "name": r.get("name"),
                        "charter": r.get("charter"), "score": round(s, 3), "len_diff": round(dlen, 3),
                        "parts_have": have, "levels": sum(plv.values()), "part_levels": plv,
                        "has_rhythm": chorus.has_part(r, "rhythm")})
        if out:
            break
    out.sort(key=lambda c: (-round(c["score"], 1), c["len_diff"] > 0.08, -len(c["parts_have"]), -c["levels"],
                            c["len_diff"]))
    return out[:n]


def _stem_roles(parts, chart_has_rhythm: bool) -> tuple:
    """Stems a chart download needs for ``parts`` (each part's roles, in order, no repeats)."""
    import chorus
    return tuple(dict.fromkeys(r for p in parts for r in chorus.stem_roles(p, chart_has_rhythm)))


# ── per-song worker ─────────────────────────────────────────────────────────

_LOCAL = []
_ARGS = None


def _init(local_index, args):
    global _LOCAL, _ARGS
    _LOCAL, _ARGS = local_index, args
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    import warnings
    warnings.filterwarnings("ignore")


def process(path_str: str) -> dict:
    if _ARGS.parts == ["drums"]:
        return process_drums(path_str)
    return process_five_fret(path_str)


def process_drums(path_str: str) -> dict:
    import drumjoin
    a = _ARGS
    p = Path(path_str)
    res = {"file": p.name, "status": "error", "t0": time.time()}
    try:
        man = manifest_of(p)
        artist, title = man.get("artist", ""), clean_title(man.get("title", ""))
        res.update(artist=artist, title=title)
        dur = float(man.get("duration") or 0)
        cands = []
        for e in _LOCAL:
            s = drumjoin.name_score(artist, title, e[1], e[2])
            if s >= a.min_match:
                cands.append({"kind": "local", "path": e[0], "artist": e[1], "name": e[2], "score": round(s, 3)})
        cands.sort(key=lambda c: -c["score"])
        cands = cands[:2]
        if a.online and artist and title:
            # upgrade: look further down the list, only charts with more levels count
            cands += online_candidates(artist, title, dur, a.min_match,
                                       a.max_candidates * (3 if a.upgrade else 1))
        cur = None
        if a.upgrade:
            cur = current_levels(p)
            res["levels_before"] = cur
            cands = [c for c in cands if c["kind"] == "local" or c.get("levels", 0) > cur][:a.max_candidates + 2]
        if not cands:
            res["status"] = "no-better" if a.upgrade else "no-chart"
            return res
        tried = []
        for c in cands[:a.max_candidates + 2]:
            if c["kind"] == "online":
                import chorus
                folder = chorus.fetch(c["md5"], CACHE / "chorus" / c["md5"])
                label = f"chorus:{c['md5']} {c['artist']} - {c['name']} ({c.get('charter')})"
            else:
                folder = Path(c["path"])
                label = f"local:{folder}"
            if cur is not None:
                lv = folder_levels(folder)
                if lv <= cur:
                    continue
                c["levels"] = lv
            if a.write:
                ensure_backup(p, Path(a.backup_dir))
            lines = []
            rep = drumjoin.join(p, chart_folder=folder, sources=("chart",), dry_run=not a.write,
                                backup_dir=None, log=lambda *x: lines.append(" ".join(map(str, x))))
            c0 = next((x for x in rep["candidates"] if "validation" in x), None)
            v = (c0 or {}).get("validation", {})
            al = (c0 or {}).get("alignment", {})
            tried.append({"chart": label, "ok": v.get("ok"), "within_30ms": v.get("within_30ms"),
                          "kick": (v.get("pads_within_30ms") or {}).get("kick"),
                          "median_ms": v.get("median_offset_ms"), "method": al.get("method"),
                          "offset": al.get("offset"), "reasons": v.get("reasons"),
                          "error": next((x.get("error") for x in rep["candidates"] if x.get("error")), None)})
            if v.get("ok"):
                done = ("upgraded" if a.write else "would-upgrade") if cur is not None else \
                    ("joined" if a.write else "would-join")
                if cur is not None:
                    res["levels_after"] = c.get("levels")
                res.update(status=done, chart=label, **{
                    k: tried[-1][k] for k in ("within_30ms", "kick", "median_ms", "method", "offset")})
                break
        else:
            res["status"] = "flagged" if tried else ("no-better" if a.upgrade else "no-chart")
        res["tried"] = tried
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        res["trace"] = traceback.format_exc()[-1500:]
    finally:
        res["secs"] = round(time.time() - res.pop("t0"), 1)
    return res


_PART_FIELDS = ("chart", "reused_drums", "method", "offset", "median_ms", "within_30ms", "beat_margin")


def process_five_fret(path_str: str) -> dict:
    """One song, --parts guitar/rhythm/bass: try the candidates in turn; each one's passing parts
    are written (with --write) and the rest go on to the next candidate."""
    import chorus
    import drumjoin
    import fivefretchart
    import fivefretjoin
    a = _ARGS
    p = Path(path_str)
    res = {"file": p.name, "status": "error", "t0": time.time()}
    try:
        view = PackView(p)
        man = view.manifest
        artist, title = man.get("artist", ""), clean_title(man.get("title", ""))
        res.update(artist=artist, title=title)
        dur = float(man.get("duration") or 0)
        wanted, min_levels = wanted_parts(view, a.parts, a.upgrade)
        if not wanted:
            res["status"] = "no-better" if a.upgrade else "has-parts"
            return res
        cands = []
        for e in _LOCAL:
            s = drumjoin.name_score(artist, title, e[1], e[2])
            if s >= a.min_match:
                cands.append({"kind": "local", "path": e[0], "artist": e[1], "name": e[2], "score": round(s, 3)})
        cands.sort(key=lambda c: -c["score"])
        cands = cands[:2]
        if a.online and artist and title:
            cands += five_fret_candidates(artist, title, dur, wanted, a.min_match,
                                          a.max_candidates * (3 if a.upgrade else 1))
        if a.upgrade:      # online: only charts with more hand-charted levels for some part count
            cands = [c for c in cands if c["kind"] == "local" or
                     any(c["part_levels"].get(x, 0) > min_levels[x] for x in wanted)][:a.max_candidates + 2]
        remaining, tried, passed = list(wanted), [], {}
        backed_up = False
        for c in cands[:a.max_candidates + 2]:
            if not remaining:
                break
            if c["kind"] == "online":
                have = [x for x in remaining if x in c["parts_have"]]
                cache = CACHE / "chorus" / c["md5"]
                label = f"chorus:{c['md5']} {c['artist']} - {c['name']} ({c.get('charter')})"
                ids = fivefretjoin.chart_ids(label=label, folder=cache)
                source = {"kind": "chart", "origin": "chorus", "md5": c["md5"], "charter": c.get("charter")}
            else:
                path = Path(c["path"])
                try:
                    charts, _ = fivefretchart.load_song_folder(drumjoin.chart_folder(path))
                except Exception as e:
                    log(f"   {path}: unreadable chart: {e}")
                    continue
                have = [x for x in remaining if x in charts]
                label = f"local:{path}"
                ids = fivefretjoin.chart_ids(label=label, folder=path)
                source = {"kind": "chart", "origin": "local"}
                if path.suffix.lower() == ".sng":
                    source["md5"] = hashlib.md5(path.read_bytes()).hexdigest()
            if not have:
                continue
            reuse = fivefretjoin.reusable_warp(view, ids)
            if c["kind"] == "online":       # a reused warp needs no audio: chart + song.ini only
                folder = chorus.fetch(c["md5"], cache, roles=() if reuse else _stem_roles(have, c["has_rhythm"]))
            else:
                folder = path

            def join(parts):
                nonlocal backed_up
                if a.write and not backed_up:      # once per song, before its first write
                    ensure_backup(p, Path(a.backup_dir))
                    backed_up = True
                return fivefretjoin.join_parts(
                    p, folder, parts, source=source, reuse=reuse,
                    min_levels={x: min_levels[x] for x in parts} if min_levels else None,
                    dry_run=not a.write, backup_dir=None, log=lambda *x: None)
            rep = join(have)
            got = dict(rep["parts"])
            failed = [x for x, r in got.items() if not r.get("ok")]
            if c["kind"] == "online" and reuse and failed:
                # the reused warp didn't do for these: a fresh alignment needs the chart's stems
                chorus.fetch(c["md5"], cache, roles=_stem_roles(failed, c["has_rhythm"]))
                got.update(join(failed)["parts"])
            for part, r in got.items():
                t = {"part": part, "chart": label, **{k: r.get(k) for k in _PART_FIELDS if k != "chart"},
                     "ok": bool(r.get("ok")), "levels": r.get("levels"),
                     "error": "; ".join(r.get("reasons") or []) or r.get("error")}
                tried.append(t)
                if t["ok"] and part in remaining:
                    remaining.remove(part)
                    passed[part] = t
        word = ("upgraded" if a.write else "would-upgrade") if a.upgrade else ("joined" if a.write else "would-join")
        none = "no-better" if a.upgrade else "no-chart"
        res["parts"] = {}
        for part in wanted:
            pr = {"levels_before": min_levels[part]} if a.upgrade else {}
            mine = [t for t in tried if t["part"] == part]
            if part in passed:
                pr.update(status=word, **{k: passed[part][k] for k in _PART_FIELDS})
                if a.upgrade:
                    pr["levels_after"] = passed[part]["levels"]
            elif mine:
                best = max(mine, key=lambda t: t.get("within_30ms") or 0)
                pr.update(status="flagged", error=best["error"], **{k: best[k] for k in _PART_FIELDS})
            else:
                pr["status"] = none
            res["parts"][part] = pr
        res["status"] = word if passed else ("flagged" if tried else none)
        if passed:
            res["chart"] = next(iter(passed.values()))["chart"]
        res["tried"] = tried
    except Exception as e:
        res["error"] = f"{type(e).__name__}: {e}"
        res["trace"] = traceback.format_exc()[-1500:]
    finally:
        res["secs"] = round(time.time() - res.pop("t0"), 1)
    return res


# ── driver ──────────────────────────────────────────────────────────────────

def build_local_index(dirs) -> list[tuple]:
    import drumjoin
    out = []
    for d in dirs:
        for e in drumjoin.index_chart_dir(Path(d)):
            out.append((str(e.folder), e.artist, e.title))
    # the same chart often sits in several folders (Downloads + Desktop): keep one per name
    seen, uniq = set(), []
    for e in out:
        k = (e[1].lower(), e[2].lower())
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return uniq


FIVE_FRET_COLS = ["file", "artist", "title", "part", "status", "chart", "reused_drums", "method", "offset",
                  "median_ms", "within_30ms", "beat_margin", "levels_before", "levels_after", "secs", "error"]


def write_five_fret_report(state: dict, csv_path: Path):
    """One row per song and part (songs without per-part results, e.g. has-parts: one row)."""
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIVE_FRET_COLS, extrasaction="ignore")
        w.writeheader()
        for r in sorted(state.values(), key=lambda r: (r.get("status", ""), r.get("file", ""))):
            song = {k: r.get(k) for k in ("file", "artist", "title", "secs")}
            parts = r.get("parts") or {}
            if not parts:
                w.writerow({**song, "status": r.get("status"), "error": r.get("error")})
            for part, pr in parts.items():
                w.writerow({**song, **pr, "part": part})


def write_report(state: dict, csv_path: Path, five_fret: bool = False):
    if five_fret:
        return write_five_fret_report(state, csv_path)
    cols = ["file", "artist", "title", "status", "chart", "method", "offset", "median_ms", "within_30ms", "kick",
            "levels_before", "levels_after", "secs", "error"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in sorted(state.values(), key=lambda r: (r.get("status", ""), r.get("file", ""))):
            row = dict(r)
            if r.get("status") == "flagged" and r.get("tried"):
                best = max(r["tried"], key=lambda t: t.get("within_30ms") or 0)
                row.update(chart=best["chart"], method=best.get("method"), offset=best.get("offset"),
                           median_ms=best.get("median_ms"), within_30ms=best.get("within_30ms"),
                           kick=best.get("kick"), error="; ".join(best.get("reasons") or []))
            w.writerow(row)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("library")
    ap.add_argument("--backup-dir", required=True, help="untouched copies go here before any write")
    ap.add_argument("--local", action="append", default=[], help="Clone Hero/YARG song folder(s) to search")
    ap.add_argument("--no-online", dest="online", action="store_false", help="don't search Chorus Encore")
    ap.add_argument("--write", action="store_true", help="write joins that pass validation (default: dry run)")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--only", help="regex on file name / artist / title")
    ap.add_argument("--redo", action="store_true", help="also songs that already have Drums / are in the state")
    ap.add_argument("--retry", default="error", help="comma list of state statuses to run again "
                    "(e.g. no-chart,flagged,error); default: error")
    ap.add_argument("--state", help="default: ~/drums-work/library/drums_state.json "
                    "(five_fret_state.json for five-fret --parts)")
    ap.add_argument("--parts", default="drums", help="drums (default) or a comma list of guitar, rhythm, bass")
    ap.add_argument("--min-match", type=float, default=0.86)
    ap.add_argument("--max-candidates", type=int, default=3)
    ap.add_argument("--upgrade", action="store_true",
                    help="songs WITH Drums (five-fret: a real chart part) but < 3 hand-charted lower levels: "
                         "replace only with a chart that has more hand-charted levels and validates")
    a = ap.parse_args(argv)
    parts = list(dict.fromkeys(x.strip().lower() for x in a.parts.split(",") if x.strip()))
    bad = [x for x in parts if x not in ("drums",) + FIVE_FRET_PARTS]
    if not parts or bad:
        ap.error(f"--parts: unknown part(s) {', '.join(bad) or '(none)'}; use drums or guitar, rhythm, bass")
    if "drums" in parts and len(parts) > 1:
        ap.error("--parts: drums can't be mixed with guitar / rhythm / bass; run them separately")
    a.parts = parts
    if a.state is None:
        a.state = str(Path.home() / "drums-work" / "library" /
                      ("drums_state.json" if parts == ["drums"] else "five_fret_state.json"))
    return a


def main(argv=None):
    a = parse_args(argv)
    ff = a.parts != ["drums"]

    lib = Path(a.library).expanduser()
    if a.write:
        bd = Path(a.backup_dir).expanduser()
        if not bd.is_dir() or not any(bd.iterdir()):
            sys.exit(f"--write needs an existing, populated backup dir; {bd} is empty or missing")
        if bd.resolve() == lib.resolve() or lib.resolve() in bd.resolve().parents:
            sys.exit("the backup dir must be outside the library (feedBack would list the copies)")
    state_path = Path(a.state).expanduser()
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state = json.loads(state_path.read_text()) if state_path.exists() else {}

    songs = sorted(lib.glob("*.sloppak"))
    todo = []
    for p in songs:
        prev = state.get(p.name)
        retry = {x.strip() for x in a.retry.split(",") if x.strip()}
        if a.upgrade and ff:
            if prev and prev.get("status") not in retry and not a.redo:
                continue
            if a.only and not re.search(a.only, p.name, re.I):
                continue
            try:
                if wanted_parts(PackView(p), a.parts, True)[0]:
                    todo.append(p)
            except Exception:
                pass
            continue
        if a.upgrade:
            if prev and prev.get("status") not in retry and not a.redo:
                continue
            if a.only and not re.search(a.only, p.name, re.I):
                continue
            try:
                lv = current_levels(p)
            except Exception:
                continue
            if 0 <= lv < 3:
                todo.append(p)
            continue
        if prev and not a.redo and prev.get("status") not in retry:
            if not (prev.get("status") == "would-join" and a.write):
                continue
        if a.only and not re.search(a.only, p.name, re.I):
            try:
                m = manifest_of(p)
                if not re.search(a.only, f"{m.get('artist', '')} {m.get('title', '')}", re.I):
                    continue
            except Exception:
                continue
        if ff and not a.redo and not (prev and prev.get("status") in retry):
            try:
                if not wanted_parts(PackView(p), a.parts, False)[0]:
                    state[p.name] = {"file": p.name, "status": "has-parts"}
                    continue
            except Exception:
                pass
        elif not a.redo and not (prev and prev.get("status") in retry):
            try:
                if has_drums(manifest_of(p)):
                    state[p.name] = {"file": p.name, "status": "has-drums"}
                    continue
            except Exception:
                pass
        todo.append(p)
    if a.limit:
        todo = todo[:a.limit]
    local = build_local_index([Path(d).expanduser() for d in a.local])
    log(f"{len(songs)} sloppaks, {len(todo)} to process, {len(local)} local charts, online={a.online}, "
        f"write={a.write}, workers={a.workers}")

    counts: dict[str, int] = {}
    csv_path = state_path.with_suffix(".csv")
    done = 0
    with ProcessPoolExecutor(a.workers, initializer=_init, initargs=(local, a)) as ex:
        futs = {ex.submit(process, str(p)): p for p in todo}
        for f in as_completed(futs):
            r = f.result()
            state[r["file"]] = r
            done += 1
            counts[r["status"]] = counts.get(r["status"], 0) + 1
            extra = ""
            if ff:
                extra = ", ".join(f"{k} {v.get('status')}" for k, v in (r.get("parts") or {}).items())
                if r.get("error"):
                    extra = r["error"][:120]
            elif r["status"] in ("joined", "would-join", "upgraded", "would-upgrade"):
                extra = f"{r['chart'][:60]}  {r.get('within_30ms')} within30, kick {r.get('kick')}, {r.get('method')}"
                if "levels_before" in r:
                    extra += f", levels {r['levels_before']}->{r.get('levels_after')}"
            elif r["status"] == "flagged":
                extra = "; ".join(f"{t.get('within_30ms')}" for t in r.get("tried", []))
            elif r.get("error"):
                extra = r["error"][:120]
            log(f"[{done}/{len(todo)}] {r['status']:<10} {r['file'][:45]:<46} {extra}")
            if done % 5 == 0 or done == len(todo):
                state_path.write_text(json.dumps(state, indent=0, default=str))
                write_report(state, csv_path, ff)
    state_path.write_text(json.dumps(state, indent=0, default=str))
    write_report(state, csv_path, ff)
    log("done: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f"; report {csv_path}")


if __name__ == "__main__":
    main()
