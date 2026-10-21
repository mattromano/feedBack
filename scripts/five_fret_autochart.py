"""Make five-fret auto charts from a pack's Rocksmith guitar and bass arrangements.

  python scripts/five_fret_autochart.py PACK... | --library DIR [--only REGEX]
         [--parts guitar,rhythm,bass] [--out DIR] [--report CSV]

Without --out it is a dry run: a per-pack, per-part summary (action, source arrangement,
note counts per level) goes to stdout and nothing is written, except the --report CSV
when one is given. With --out it writes, per pack, OUT/<pack stem>/five_fret_<part>.json
(the feedpak part file: `source.kind: "auto"`, named "<Part> (auto)", Expert plus
generated Hard / Medium / Easy), OUT/<pack stem>/manifest_entries.json (the arrangement
entries a pack writer would add) and OUT/summary.json. Packs are never modified: Plan 4's
library tool adds the parts to packs.

Actions per part: "write"; "skip-chart" (the pack already has a real chart for that
instrument: real charts win); "skip-none" (no arrangement to chart). An existing auto
part is replaced ("write").

Read-only on packs: each pack's manifest.yaml and JSON members (never the stems) are
staged into a temp dir, loaded with sloppak.load_song there (so manifest overrides and
beats apply) and deleted afterwards. The command refuses (exit 2) when --out or --report
lies inside, equals or contains LIBRARY (or a PACK's folder), the Rocksmith dlc root or
the Clone Hero caches.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "lib", ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import fivefret  # noqa: E402
import fivefret_autochart as ac  # noqa: E402
import fivefretchart  # noqa: E402
from drumjoin import is_drums_arrangement  # noqa: E402
# Shared with the benchmark: zipfile staging of JSON members, staged loading, the
# read-only-root refusal rule and the fretted-arrangement test.
import five_fret_benchmark as ffb  # noqa: E402

PART_WORD = {"guitar": "lead", "rhythm": "rhythm", "bass": "bass"}    # Plan 4's pairing rule
NOT_CHARTABLE = ("drums", "drum", "five_fret", "keys", "piano")
LEVELS = ("expert",) + ffb.LOWER
CSV_FIELDS = ("pack", "part", "action", "arrangement", "arrangement_name", "pairs_with",
              *LEVELS, "reason")


def auto_name(part: str) -> str:
    return f"{fivefretchart.PART_NAMES[part]} (auto)"


# ── which arrangement each part comes from ─────────────────────────────────────

def _id(e: dict) -> str:
    return str(e.get("id") or "").strip()


def _name(e: dict) -> str:
    return str(e.get("name") or "").strip().lower()


def _candidates(manifest: dict) -> list[dict]:
    """Arrangement entries that can be charted: an id, not drums / five-fret / keys / piano."""
    return [e for e in ffb._entries(manifest)
            if _id(e) and str(e.get("type") or "").strip().lower() not in NOT_CHARTABLE
            and not is_drums_arrangement(e)]


def _is_bass(e: dict) -> bool:
    return str(e.get("type") or "").strip().lower() == "bass" or "bass" in _name(e)


def pick_with_pairing(manifest: dict) -> dict[str, tuple[str, bool]]:
    """part -> (arrangement id, paired). Plan 4's rule first (paired = True): guitar ->
    id "lead", rhythm -> "rhythm", bass -> "bass", matched on id, then on a name starting
    with that word (case-insensitive). Otherwise (paired = False): guitar <- the first
    "Combo", rhythm <- the next unused "Combo", bass <- type bass or "Bass" in the name."""
    cands = _candidates(manifest)
    out: dict[str, tuple[str, bool]] = {}
    used: set[str] = set()

    def take(pred) -> str | None:
        e = next((e for e in cands if _id(e) not in used and pred(e)), None)
        if e is None:
            return None
        used.add(_id(e))
        return _id(e)

    for part, word in PART_WORD.items():
        aid = take(lambda e: _id(e).lower() == word) or take(lambda e: _name(e).startswith(word))
        if aid:
            out[part] = (aid, True)
    for part in ("guitar", "rhythm"):
        if part not in out:
            aid = take(lambda e: "combo" in _name(e) and not _is_bass(e))
            if aid:
                out[part] = (aid, False)
    if "bass" not in out:
        aid = take(_is_bass)
        if aid:
            out["bass"] = (aid, False)
    return out


def pick_arrangements(manifest: dict) -> dict[str, str]:
    """part -> arrangement id (see pick_with_pairing)."""
    return {part: aid for part, (aid, _) in pick_with_pairing(manifest).items()}


# ── actions ───────────────────────────────────────────────────────────────────

def auto_parts(staged: Path, parts) -> list[dict]:
    """One action per part for a staged (dir-form) pack: {"part", "action", "file",
    "arrangement", "arrangement_name", "pairs_with", "notes" (level -> count), "reason"},
    plus "data" (the part file) and "entry" (its manifest entry) when the action is
    "write"."""
    manifest = ffb._manifest(staged)
    loaded = ffb._load_staged(staged)
    grid = ac.BeatGrid.from_beats(loaded.song.beats)
    picks = pick_with_pairing(manifest)
    arr_by_id = {aid: a for aid, a in zip(loaded.arrangement_ids, loaded.song.arrangements)
                 if aid}
    real = {p["instrument"] for p in loaded.five_fret_parts or [] if p["source_kind"] == "chart"}
    rows = []
    for part in parts:
        row = {"part": part, "action": "skip-none", "file": f"five_fret_{part}.json",
               "arrangement": None, "arrangement_name": None, "pairs_with": None,
               "notes": {}, "reason": ""}
        rows.append(row)
        if part in real:
            row.update(action="skip-chart", reason="pack has a real chart for this part")
            continue
        if part not in picks:
            row["reason"] = "no matching arrangement"
            continue
        aid, paired = picks[part]
        arr = arr_by_id.get(aid)
        row.update(arrangement=aid, arrangement_name=arr.name if arr else None)
        if arr is None or not ffb._is_fretted(arr) or not (arr.notes or arr.chords):
            row["reason"] = "arrangement missing or has no notes"
            continue
        chart = ac.autochart(arr, grid, part)
        ac.auto_levels(chart, grid)
        source = {"kind": "auto", "generator": ac.GENERATOR,
                  "generator_version": ac.GENERATOR_VERSION, "from_arrangement": aid}
        data = fivefretchart.five_fret_json(chart, source, auto_name(part))
        ok, why = fivefret.validate_five_fret(data)
        if not ok:
            raise ValueError(f"{part}: generated part fails validation: {why}")
        entry = {"id": f"five_fret_{part}", "name": auto_name(part), "type": "five_fret",
                 "instrument": part, "five_fret": row["file"]}
        if paired:
            entry["pairs_with"] = aid
        row.update(action="write", pairs_with=aid if paired else None, data=data, entry=entry,
                   notes={"expert": len(chart.notes),
                          **{lv: len(chart.levels.get(lv) or []) for lv in ffb.LOWER}})
    return rows


def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in ("data", "entry")}


def run_pack(pack: Path, parts, scratch: Path) -> list[dict]:
    """auto_parts on a staged copy of `pack` (manifest + JSON members), removed afterwards."""
    staged = None
    try:
        staged = ffb.stage_pack(pack, scratch)
        return auto_parts(staged, parts)
    finally:
        if staged is not None:
            shutil.rmtree(staged, ignore_errors=True)


def write_pack(out: Path, pack: Path, rows: list[dict]) -> None:
    writes = [r for r in rows if r["action"] == "write"]
    if not writes:
        return
    d = out / Path(pack.name).stem
    d.mkdir(parents=True, exist_ok=True)
    for r in writes:
        (d / r["file"]).write_text(json.dumps(r["data"], separators=(",", ":")), encoding="utf-8")
    (d / "manifest_entries.json").write_text(
        json.dumps([r["entry"] for r in writes], indent=2) + "\n", encoding="utf-8")


def _line(pack: str, r: dict) -> str:
    src = f"from {r['arrangement']}" + (f" ({r['arrangement_name']})" if r["arrangement_name"]
                                        else "") if r["arrangement"] else ""
    counts = " ".join(f"{lv} {n}" for lv, n in r["notes"].items())
    tail = counts or r["reason"]
    return f"{pack}  {r['part']:<6}  {r['action']:<10}  {src}  {tail}".rstrip()


# ── main ────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("packs", nargs="*", type=Path, metavar="PACK")
    ap.add_argument("--library", type=Path)
    ap.add_argument("--only")
    ap.add_argument("--parts", default="guitar,rhythm,bass")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--report", type=Path)
    a = ap.parse_args(argv)
    if bool(a.packs) == bool(a.library):
        ap.error("give PACK... or --library DIR (not both)")
    parts = [p.strip() for p in a.parts.split(",") if p.strip()]
    bad = [p for p in parts if p not in fivefret.PARTS]
    if bad or not parts:
        ap.error(f"--parts takes {', '.join(fivefret.PARTS)}")
    parts = list(dict.fromkeys(parts))

    roots = [a.library] if a.library else sorted({p.resolve().parent for p in a.packs})
    for label, p in (("--out", a.out), ("--report", a.report)):
        if p is None:
            continue
        for lib in roots:
            root = ffb._unsafe_target(p, lib)
            if root is not None:
                print(f"refusing: {label} {p} overlaps the read-only {root} (inside it or "
                      "containing it)", file=sys.stderr)
                return 2
    logging.getLogger("sloppak").setLevel(logging.ERROR)

    if a.library:
        packs = sorted(p for p in a.library.iterdir() if p.name.endswith(".sloppak"))
    else:
        packs = list(a.packs)
    if a.only:
        rx = re.compile(a.only, re.I)
        packs = [p for p in packs if rx.search(p.name)]

    if a.out is None:
        print("dry run: nothing written (use --out DIR to write the part files)")
    summary, errors = [], 0
    with tempfile.TemporaryDirectory(prefix="five_fret_autochart_") as tmp:
        for pack in packs:
            try:
                rows = run_pack(pack, parts, Path(tmp))
                if a.out is not None:
                    write_pack(a.out, pack, rows)
            except ffb.SkipPack as e:
                rows, errors = [], errors + 1
                summary.append({"pack": pack.name, "error": e.reason, "parts": []})
                print(f"{pack.name}  error  {e.reason}")
                continue
            except Exception as e:  # one bad pack never stops the run
                rows, errors = [], errors + 1
                summary.append({"pack": pack.name, "error": f"{type(e).__name__}: {e}",
                                "parts": []})
                print(f"{pack.name}  error  {type(e).__name__}: {e}")
                continue
            summary.append({"pack": pack.name, "parts": [_public(r) for r in rows]})
            for r in rows:
                print(_line(pack.name, r))

    counts: dict[str, int] = {}
    for s in summary:
        for r in s["parts"]:
            counts[r["action"]] = counts.get(r["action"], 0) + 1
    if a.out is not None:
        a.out.mkdir(parents=True, exist_ok=True)
        (a.out / "summary.json").write_text(json.dumps(
            {"generator": ac.GENERATOR, "generator_version": ac.GENERATOR_VERSION,
             "parts": parts, "counts": counts, "errors": errors, "packs": summary},
            indent=1) + "\n", encoding="utf-8")
    if a.report is not None:
        a.report.parent.mkdir(parents=True, exist_ok=True)
        with a.report.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            w.writeheader()
            for s in summary:
                if "error" in s:
                    w.writerow({"pack": s["pack"], "action": "error", "reason": s["error"]})
                for r in s["parts"]:
                    w.writerow({"pack": s["pack"], "part": r["part"], "action": r["action"],
                                "arrangement": r["arrangement"] or "",
                                "arrangement_name": r["arrangement_name"] or "",
                                "pairs_with": r["pairs_with"] or "", "reason": r["reason"],
                                **{lv: r["notes"].get(lv, "") for lv in LEVELS}})
    print(f"{len(packs)} packs; {', '.join(f'{k} {v}' for k, v in sorted(counts.items())) or 'no parts'}"
          f"; {errors} errors" + (f" -> {a.out}" if a.out is not None else " (dry run)"),
          file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
