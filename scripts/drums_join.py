"""Attach a Drums part (feedpak drum_tab) to one existing sloppak.

  python scripts/drums_join.py SONG.sloppak [--chart FOLDER_OR_SNG | --chart-dir DIR] [--gp TAB.gp5]
         [--source chart,gp] [--method auto|offset|warp] [--force] [--dry-run] [--report OUT.json]
         [--backup-dir DIR] [--min-within-30ms 0.5] [--max-median-ms 25] [--max-drift-ms 40]

mattromano custom build (ported from the Slopsmith 0.2.9 tools). Needs librosa + scipy
(``pip install librosa``); the app itself doesn't ship them.

Sources in priority order: a YARG/Clone Hero chart folder (--chart, or the best fuzzy
artist/title match under --chart-dir), then the GP tab's drum track (--gp, default: the
tab recorded in the sloppak's x_build) placed with the stored sync map.  Every candidate
is checked against onsets in stems/drums.ogg; one that fails is reported, not written,
unless --force.  The old file is backed up to --backup-dir (default ~/drums-work/backup)
first.  See lib/drumjoin.py.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "lib"), str(ROOT / "scripts")]

import drumjoin  # noqa: E402


def add_common_args(ap):
    ap.add_argument("--source", default="chart,gp", help="comma list, priority order (chart, gp)")
    ap.add_argument("--method", default="auto", choices=["auto", "offset", "warp"],
                    help="chart alignment: auto picks a beat-level warp when correlation is weak or drifts")
    ap.add_argument("--force", action="store_true", help="write the best candidate even if validation fails")
    ap.add_argument("--dry-run", action="store_true", help="align and validate, don't write")
    ap.add_argument("--transcriber", help="name of a registered transcription hook (source c; none ship yet)")
    ap.add_argument("--min-within-30ms", type=float, default=drumjoin.drumalign.THRESHOLDS["min_within_30ms"])
    ap.add_argument("--max-median-ms", type=float, default=drumjoin.drumalign.THRESHOLDS["max_abs_median_ms"])
    ap.add_argument("--max-drift-ms", type=float, default=drumjoin.drumalign.THRESHOLDS["max_drift_span_ms"])
    ap.add_argument("--min-pad-within-30ms", type=float, default=drumjoin.drumalign.THRESHOLDS["min_pad_within_30ms"],
                    help="kick/cymbal notes vs kick-/cymbal-band onsets (catches whole-beat offsets)")
    ap.add_argument("--min-match", type=float, default=0.82, help="fuzzy artist/title match threshold (0-1)")


def thresholds(a):
    return {"min_within_30ms": a.min_within_30ms, "max_abs_median_ms": a.max_median_ms,
            "max_drift_span_ms": a.max_drift_ms, "min_pad_within_30ms": a.min_pad_within_30ms}


def find_chart(index, artist, title, min_match):
    entry, score = drumjoin.best_match(index, artist, title, min_match)
    if entry:
        print(f"  matched chart {entry.artist} - {entry.title} ({score:.2f}): {entry.folder}", flush=True)
        return entry.folder
    print(f"  no chart matches {artist} - {title} (best score {score:.2f})", flush=True)
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sloppak")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--chart", help="YARG/Clone Hero song folder or .sng package")
    g.add_argument("--chart-dir", help="folder of song folders; picks the best artist/title match")
    ap.add_argument("--gp", help="Guitar Pro tab (default: x_build.gp in the sloppak)")
    ap.add_argument("--report", help="write the join report JSON here")
    ap.add_argument("--backup-dir", default=str(Path.home() / "drums-work" / "backup"),
                    help="a dated copy of the sloppak goes here before it is rewritten")
    add_common_args(ap)
    a = ap.parse_args()
    sp = Path(a.sloppak)
    chart = Path(a.chart) if a.chart else None
    if a.chart_dir:
        m = drumjoin.SloppakFiles(sp)
        artist, title = m.manifest.get("artist", ""), m.manifest.get("title", "")
        m.close()
        chart = find_chart(drumjoin.index_chart_dir(Path(a.chart_dir)), artist, title, a.min_match)
    print(f"{sp.name}", flush=True)
    rep = drumjoin.join(sp, chart_folder=chart, gp_path=Path(a.gp) if a.gp else None,
                        sources=[s.strip() for s in a.source.split(",") if s.strip()],
                        transcriber=a.transcriber, force=a.force, dry_run=a.dry_run,
                        force_method=None if a.method == "auto" else a.method, thresholds=thresholds(a),
                        backup_dir=Path(a.backup_dir).expanduser(), report_path=a.report,
                        log=lambda *x: print(*x, flush=True))
    sys.exit(0 if rep["written"] or a.dry_run else 2)


if __name__ == "__main__":
    main()
