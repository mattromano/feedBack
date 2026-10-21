"""Pack side of the five-fret join (mattromano custom build; offline tool).

The five-fret counterpart of ``drumjoin.py``: finds a song's real Clone Hero / Rock Band
guitar, rhythm and bass charts and writes them into a sloppak as feedpak ``five_fret`` parts
(``type: five_fret`` arrangement entries pointing at ``five_fret_<part>.json``; how each part was
made goes to a ``five_fret_join.json`` side file). This module holds the helpers that work on the
pack: which parts exist already, which arrangement a part pairs with, the drum join's chart->audio
warp (reused when the same chart is used), and the writer; and the audio side: placing a chart on
our audio (the reused drum warp, clamped to the drummed span, else a fresh alignment that mirrors
``drumjoin.align_chart`` like with like), validating it against our part stem's onsets (plus a
one-beat-shift margin), and ``join_parts``. The audio side needs librosa / scipy (offline tool).
"""
from __future__ import annotations

import copy
import json
import re
import time
from pathlib import Path, PureWindowsPath
from typing import Callable

import numpy as np

import chorus
import drumalign
import drumchart
import drumjoin
import fivefret
import fivefretchart

FEEDPAK_VERSION = "1.21.0"      # five_fret arrangement entries
JOIN_INFO = "five_fret_join.json"
PAIRS = {"guitar": "lead", "rhythm": "rhythm", "bass": "bass"}   # part -> arrangement it pairs with

_MD5 = re.compile(r"^[0-9a-f]{32}$", re.I)


def _is_part_entry(e) -> bool:
    return (isinstance(e, dict) and str(e.get("type") or "").strip().lower() == "five_fret"
            and e.get("instrument") in fivefret.PARTS)


def existing_parts(sp: drumjoin.SloppakFiles) -> dict[str, dict]:
    """``{part: {"id", "kind": "chart"|"auto", "levels", "md5"}}`` for the pack's five-fret parts.

    ``levels`` counts hand-charted lower levels. An unreadable file counts as a chart with 3 levels,
    so it is never replaced."""
    out: dict[str, dict] = {}
    for e in sp.manifest.get("arrangements") or []:
        if not _is_part_entry(e):
            continue
        info = {"id": str(e.get("id") or ""), "kind": "chart", "levels": 3, "md5": None}
        try:
            doc = sp.read_json(str(e["five_fret"]))
            src = doc["source"]
            kind = "auto" if src.get("kind") == "auto" else "chart"
            md5 = src.get("md5") if isinstance(src.get("md5"), str) else None
            levels = doc.get("levels") or {}
            n = sum(1 for lv in fivefret.LEVELS
                    if isinstance(levels.get(lv), dict) and levels[lv].get("notes")
                    and not levels[lv].get("generated"))
            info.update(kind=kind, levels=n, md5=md5)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        out[e["instrument"]] = info
    return out


def pairs_with(manifest: dict, part: str) -> str | None:
    """Id of the fretted arrangement this part is the plastic counterpart of (None when none)."""
    word = PAIRS[part]
    cands = [e for e in manifest.get("arrangements") or []
             if isinstance(e, dict) and e.get("id") and not _is_part_entry(e)
             and not drumjoin.is_drums_arrangement(e)]
    for e in cands:
        if str(e["id"]).lower() == word:
            return str(e["id"])
    for e in cands:
        if str(e["id"]).lower().startswith(word) or str(e.get("name") or "").strip().lower().startswith(word):
            return str(e["id"])
    return None


def drum_source(sp: drumjoin.SloppakFiles) -> dict | None:
    """``{"folder", "warp"}`` of the chart the pack's drums were joined from, or None."""
    def pick(src):
        if isinstance(src, dict) and isinstance(src.get("folder"), str) and src["folder"] \
                and isinstance(src.get("warp"), (list, dict)) and src["warp"]:
            return {"folder": src["folder"], "warp": src["warp"]}
        return None
    info = drumjoin.read_join_info(sp)
    if isinstance(info, dict):
        got = pick(info.get("detail"))
        if got:
            return got
    for e in sp.manifest.get("arrangements") or []:
        if not (isinstance(e, dict) and drumjoin.is_drums_arrangement(e)):
            continue
        rel = e.get("file")
        if not (isinstance(rel, str) and rel):
            continue
        try:
            doc = sp.read_json(rel)
        except (OSError, ValueError):
            continue
        drums = doc.get("drums") if isinstance(doc, dict) else None
        got = pick(drums.get("source")) if isinstance(drums, dict) else None
        if got:
            return got
    return None


def chart_ids(label: str | None = None, folder=None) -> set[str]:
    """Identities of a chart: ``chorus:<md5>``, ``local:<resolved path>``, ``folder:<resolved dir>``."""
    ids: set[str] = set()
    if label:
        label = label.strip()
        m = re.match(r"^chorus:([0-9a-f]{32})\b", label, re.I)
        if m:
            ids.add(f"chorus:{m.group(1).lower()}")
        elif label.startswith("local:"):
            ids.add(f"local:{Path(label[6:]).resolve()}")
    if folder is not None:
        s = str(folder)
        if "\\" in s or re.match(r"^[A-Za-z]:", s):
            pw = PureWindowsPath(s)      # can't resolve locally: md5 identity only
            if _MD5.match(pw.name) and pw.parent.name.lower() == "chorus":
                ids.add(f"chorus:{pw.name.lower()}")
            return ids
        p = Path(s)
        if _MD5.match(p.name) and p.parent.name.lower() == "chorus":
            ids.add(f"chorus:{p.name.lower()}")
        if p.suffix.lower() == ".sng":
            try:
                ids.add(f"folder:{drumjoin.sng_cache_dir(p).resolve()}")
            except OSError:
                pass
        else:
            ids.add(f"folder:{p.resolve()}")
    return ids


def reusable_warp(sp: drumjoin.SloppakFiles, cand_ids: set[str]) -> dict | None:
    """The pack's drum source (``{"folder", "warp"}``) when the candidate chart is that same chart.

    Decided by the pack's own drum source folder only, never by a drums-state label."""
    src = drum_source(sp)
    if src and cand_ids & chart_ids(folder=src["folder"]):
        return src
    return None


def warp_fn(spec) -> Callable[[float], float]:
    """Chart time -> our time from a drum-join warp (pairs list or ``{"offset": x}``)."""
    if isinstance(spec, dict):
        off = float(spec["offset"])
        return lambda t: t + off
    xs, ys = zip(*spec)
    return drumalign._pw_linear(xs, ys)


def write_five_fret(sp: drumjoin.SloppakFiles, parts: dict[str, dict], info: dict[str, dict],
                    backup_dir=None) -> None:
    """Write ``parts`` (``{part: five_fret payload}``) into the pack, replacing the same instrument's
    existing part (and its file) only, and merge ``info`` into ``five_fret_join.json``.

    Raises ValueError, writing nothing, when a payload fails ``validate_five_fret``."""
    for part, payload in parts.items():
        if part not in fivefret.PARTS:
            raise ValueError(f"unknown five-fret part {part!r}")
        ok, why = fivefret.validate_five_fret(payload)
        if not ok:
            raise ValueError(f"{part}: {why}")
    keep = []
    for e in sp.manifest.get("arrangements") or []:
        if _is_part_entry(e) and e["instrument"] in parts:
            rel = e.get("five_fret")
            if isinstance(rel, str) and rel and (sp.dir / rel).is_file():
                (sp.dir / rel).unlink()
            continue
        keep.append(e)
    for part, payload in parts.items():
        rel = f"five_fret_{part}.json"
        (sp.dir / rel).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
        entry = {"id": f"five_fret_{part}", "name": f"{fivefretchart.PART_NAMES[part]} (5-fret)",
                 "type": "five_fret", "instrument": part, "five_fret": rel}
        pw = pairs_with({"arrangements": keep}, part)
        if pw:
            entry["pairs_with"] = pw
        keep.append(entry)
    sp.manifest["arrangements"] = keep
    if info:
        try:
            merged = sp.read_json(JOIN_INFO)
            if not isinstance(merged, dict):
                merged = {}
        except (OSError, ValueError):
            merged = {}
        merged.update(info)
        (sp.dir / JOIN_INFO).write_text(json.dumps(merged, indent=1, default=float), encoding="utf-8")
    if drumjoin._version_tuple(sp.manifest.get("feedpak_version")) < drumjoin._version_tuple(FEEDPAK_VERSION):
        sp.manifest["feedpak_version"] = FEEDPAK_VERSION
    sp.save(backup_dir)


# ── audio side: placement, alignment, validation ────────────────────────────

# Per-part validation thresholds (rhythm uses guitar's). From a trial on 21 real songs (Plan 4 Task 4):
# the lowest cuts that pass no one-beat-shifted or wrong-chart placement; median / drift kept (they don't separate).
THRESHOLDS = {
    "guitar": {"min_within_30ms": 0.35, "max_abs_median_ms": 25.0, "max_drift_span_ms": 40.0, "min_beat_margin": 0.05},
    "bass": {"min_within_30ms": 0.45, "max_abs_median_ms": 25.0, "max_drift_span_ms": 40.0, "min_beat_margin": 0.05},
}
MIN_INTRO_NOTES = 8     # notes before the first drum hit that get validated on their own (reuse)


def part_thresholds(part: str, thresholds: dict | None = None) -> dict:
    """THRESHOLDS for ``part`` with overrides: per part (``{"guitar": {...}}``) or flat."""
    key = "bass" if part == "bass" else "guitar"
    th = dict(THRESHOLDS[key])
    if thresholds:
        if any(k in fivefret.PARTS for k in thresholds):
            th.update(thresholds.get(part) or (thresholds.get("guitar") if part == "rhythm" else None) or {})
        else:
            th.update(thresholds)
    return th


def chart_beats(chart: fivefretchart.FiveFretChart) -> list[float]:
    """Beat times (chart audio time, seconds) through the measure holding the last note."""
    end = max([n.tick for n in chart.notes] + [0])
    return [round(chart.tempo.tick_to_time(t) + chart.offset, 4) for t, _ in chart.tempo.beats(end)]


def _beat(chart) -> float:
    b = np.diff(chart_beats(chart))
    return float(np.median(b)) if len(b) else 0.5


def _place_notes(notes, warp):
    out = []
    for n in notes:
        t = float(warp(n.time))
        if t < 0:
            continue
        m = copy.copy(n)
        m.time = round(t, 4)
        if n.length:
            m.length = round(float(warp(n.time + n.length)) - t, 4)
        out.append(m)
    return out


def place(chart: fivefretchart.FiveFretChart, warp: Callable[[float], float]) -> fivefretchart.FiveFretChart:
    """Copy of ``chart`` on our audio: every time through ``warp`` (sustains re-timed as
    warp(t + l) - warp(t)), notes the warp moves before 0 s dropped (their level copies too),
    star power / solo spans warped and clipped at 0."""
    c = copy.deepcopy(chart)
    c.notes = _place_notes(chart.notes, warp)
    c.levels = {lv: _place_notes(ns, warp) for lv, ns in chart.levels.items()}

    def spans(rng):
        out = ((max(round(float(warp(a)), 4), 0.0), round(float(warp(b)), 4)) for a, b in rng)
        return sorted({(a, b) for a, b in out if b > 0})
    c.star_power = spans(chart.star_power)
    c.solos = spans(chart.solos)
    return c


def _our_role(part: str) -> str:
    return "bass" if part == "bass" else "guitar"


def our_part_audio(sp: drumjoin.SloppakFiles, part: str) -> tuple[np.ndarray | None, str]:
    """Our stem for ``part``: guitar / rhythm -> the guitar stem, bass -> the bass stem, else the mix."""
    role = _our_role(part)
    p = sp.stem(role)
    if p is not None:
        return drumalign.load_audio(p), role
    return sp.mix_audio(), "mix"


def _sum_audio(paths) -> np.ndarray:
    ys = [drumalign.load_audio(p) for p in paths]
    n = max(len(y) for y in ys)
    return sum(np.pad(y, (0, n - len(y))) for y in ys)


def chart_part_audio(folder, part: str, chart_parts=None) -> tuple[np.ndarray | None, str]:
    """The chart's own audio for ``part``: the first stem in ``chorus.stem_roles`` order, else the
    full mix when the folder has ``song.*`` (every non-crowd file summed), else ``(None, "none")``
    (drums-only folders included), for which the alignment synthesises a note envelope."""
    folder = Path(folder)
    if chart_parts is None:
        chart_parts = set(fivefretchart.load_song_folder(folder)[0])
    files = drumchart.song_audio_files(folder)
    files.pop("crowd", None)
    for role in chorus.stem_roles(part, "rhythm" in chart_parts):
        if files.get(role):
            return _sum_audio(files[role]), role
    if files.get("song"):
        return _sum_audio([p for ps in files.values() for p in ps]), "mix"
    return None, "none"


def align_part(folder, sp: drumjoin.SloppakFiles, chart: fivefretchart.FiveFretChart, part: str, *,
               ref_y, ref_kind: str, onsets, chart_parts=None, force_method: str | None = None,
               min_conf: float = 3.0, min_ratio: float = 1.08, max_drift: float = 0.035,
               log=print) -> tuple[fivefretchart.FiveFretChart, dict]:
    """Fresh alignment of one part, like with like (``drumjoin.align_chart``'s recipe): the
    chart's part stem against our part stem (``ref_y``), the chart's mix against our mix, or a
    note envelope (every note in the mid band, where pick attacks sit) against our part stem.
    Weak correlation or drift -> the beat-level warp; then a snap onto ``onsets``."""
    if ref_y is None:
        raise RuntimeError("sloppak has no audio")
    src_y, src_kind = chart_part_audio(folder, part, chart_parts)
    ours_y = ref_y
    if src_kind == "mix" and ref_kind != "mix":
        ours_y = sp.mix_audio()
    part_env = drumalign.onset_env(ref_y)
    ref_env = part_env if ours_y is ref_y else drumalign.onset_env(ours_y)
    note_times = np.array([n.time for n in chart.notes])
    if not len(note_times):
        raise ValueError(f"{part}: the chart part has no notes")
    if src_y is not None:
        src_env = drumalign.onset_env(src_y)
    else:
        src_kind = "synthetic"
        n = int((note_times.max() + 5) * drumalign.FR)
        src_env = drumalign.synth_env([(t, "red", False) for t in note_times], n)
    al = drumalign.global_offset(ref_env, src_env)
    log(f"  {part} xcorr ({src_kind} vs our {'mix' if ours_y is not ref_y else ref_kind}): offset "
        f"{al.offset:+.3f}s, confidence {al.confidence:.1f}, peak ratio {al.peak_ratio:.2f}, "
        f"drift {al.drift * 1000:.0f} ms over {len(al.local)} windows")
    weak = al.confidence < min_conf or al.peak_ratio < min_ratio
    drifting = al.drift > max_drift
    method = force_method or ("warp" if (weak or drifting) else "offset")
    # global_offset marks a vote override (an edit / cut section: the whole-song peak lost to the
    # 12 s window votes) with confidence 0 and peak ratio 1; keep that lag, don't rescan it away
    voted = al.confidence == 0.0 and al.peak_ratio == 1.0
    if method == "warp":
        lag = None if voted else _rate_scan_lag(ref_env, src_env)
        if lag is not None:
            log(f"  {part}: a tempo-ratio scan puts the song middle at lag {lag:+.3f}s")
            al.offset = lag
        pairs = drumalign.warp_pairs(ref_env, src_env, chart_beats(chart), note_times, al.offset,
                                     onset_ref_env=part_env)
        al = drumalign.Alignment("warp", al.offset, al.confidence, al.peak_ratio, al.local, al.drift, pairs,
                                 rate=drumalign.LAST_RATE.get("rate", 1.0))
        log(f"  {part}: beat-level warp over {len(pairs)} chart beats ({'weak correlation' if weak else 'drift'})")
    warp = al.warp()
    snap = drumalign.snap_correction([warp(t) for t in note_times], onsets) if onsets is not None else 0.0
    al.snap = snap
    placed = place(chart, lambda t: warp(t) + snap)
    det = {"chart_audio": src_kind, "alignment": al.summary(),
           "warp": ([[round(a, 3), round(b + snap, 3)] for a, b in al.warp_pairs]
                    if al.method == "warp" and al.warp_pairs else {"offset": round(al.offset + snap, 4)})}
    return placed, det


RATE_SCAN = np.round(np.arange(0.97, 1.0301, 0.0025), 4)


def _rate_scan_lag(ref_env, src_env, min_gain: float = 1.5):
    """Lag at the middle of the chart when the chart envelope re-timed by a constant tempo ratio
    correlates clearly better (``min_gain`` x the xcorr confidence at ratio 1.0) than as it is
    (else None). Guitar parts sit on a dense note grid:
    a 1-2 % tempo difference smears both the whole-song correlation and the 12 s window votes
    (0.24 s per window at 2 %, about an eighth note), so ``global_offset`` can land seconds off and
    the beat-level warp then tracks around the wrong lag. Re-timing the envelope first restores a
    sharp peak; the warp then refines the ratio itself."""
    conf_at_1 = drumalign.xcorr(ref_env, src_env, 60.0)[1]
    best = None
    for r in RATE_SCAN:
        if r == 1.0:
            continue
        lag, conf, _ = drumalign.xcorr(ref_env, drumalign.resample_env(src_env, r), 60.0)
        if best is None or conf > best[1]:
            best = (lag, conf, r)
    if best is None or best[1] < min_gain * max(conf_at_1, 1.0):
        return None
    lag, _, r = best
    mid = drumalign._bands(src_env).shape[-1] / drumalign.FR / 2
    return float(lag + (r - 1.0) * mid)


def validate_part(times, onsets, beat: float, thresholds: dict) -> drumalign.Validation:
    """``drumalign.validate`` (no pad check) plus the beat-shift margin: within_30ms of the notes
    minus the best within_30ms of the notes moved one beat either way (``pads["beat_margin"]``).
    A steady strum one beat off can pass the onset check; it can't beat its own shifted copy."""
    v = drumalign.validate(times, onsets, thresholds)
    nt = np.unique(np.round(np.asarray(times, float), 3))
    margin = 0.0
    if len(nt) and len(onsets):
        margin = drumalign._within(nt, onsets) - max(drumalign._within(nt + beat, onsets),
                                                     drumalign._within(nt - beat, onsets))
    v.pads["beat_margin"] = margin
    floor = thresholds.get("min_beat_margin")
    if floor is not None and margin < floor:
        v.reasons.append(f"one beat off? margin {margin:.2f} < {floor:.2f}")
        v.ok = False
    return v


def _hand_levels(chart) -> int:
    return sum(1 for lv in fivefret.LEVELS if chart.levels.get(lv) and lv not in chart.levels_generated)


def _clamp(warp, lo: float, hi: float):
    """Outside the drummed span [lo, hi] a warp keeps the lag at the nearest edge."""
    lag_lo, lag_hi = warp(lo) - lo, warp(hi) - hi
    return lambda t: t + lag_lo if t < lo else (t + lag_hi if t > hi else warp(t))


def _reuse_part(folder, chart, spec, onsets, beat, th, log):
    """Place ``chart`` with the drum join's warp -> (placed, detail, validation, ok)."""
    w, clamped, first = warp_fn(spec), False, None
    if isinstance(spec, list):
        try:
            hits = drumchart.load_song_folder(folder, levels=False)[0].hits
        except Exception:     # no usable drum part: the warp as it is
            hits = []
        if hits:
            first, last = min(h.time for h in hits), max(h.time for h in hits)
            w = _clamp(w, first, last)
            clamped = True
    placed = place(chart, w)
    v = validate_part([n.time for n in placed.notes], onsets, beat, th)
    ok = v.ok
    if ok and first is not None:
        intro = [float(w(n.time)) for n in chart.notes if n.time < first]
        intro = [t for t in intro if t >= 0]
        if len(intro) >= MIN_INTRO_NOTES:
            vi = validate_part(intro, onsets, beat, th)
            if not vi.ok:
                log(f"  reused warp: the {len(intro)} notes before the first drum hit fail: {'; '.join(vi.reasons)}")
                ok = False
    # the Alignment.summary() shape fresh alignments record: offset = median lag over the notes,
    # rate = the warp's overall slope; no correlation ran, so confidence / peak ratio are 0
    lags = [float(w(n.time)) - n.time for n in chart.notes]
    rate = 1.0
    if isinstance(spec, list) and len(spec) >= 2 and spec[-1][0] != spec[0][0]:
        rate = (spec[-1][1] - spec[0][1]) / (spec[-1][0] - spec[0][0])
    al = drumalign.Alignment("warp" if isinstance(spec, list) else "offset",
                             float(np.median(lags)) if lags else 0.0, 0.0, 0.0, rate=float(rate))
    det = {"chart_audio": None, "warp": spec, "clamped": clamped, "alignment": al.summary()}
    return placed, det, v, ok


def _source_with_charter(source: dict, ini: dict) -> dict:
    out = dict(source)
    out["charter"] = source.get("charter") or ini.get("charter") or ini.get("frets")
    return {k: v for k, v in out.items() if v not in (None, "")}


def join_parts(sloppak: Path, chart_folder: Path, parts: list[str], *, source: dict, reuse: dict | None = None,
               min_levels: dict[str, int] | None = None, thresholds: dict | None = None, dry_run: bool = True,
               backup_dir=None, log=print) -> dict:
    """Place, validate and (unless ``dry_run``) write the chart's ``parts`` into the pack.

    Per part: the reused drum warp (``reuse = {"folder", "warp"}``) first, then a fresh
    alignment; the first that validates is kept. Only parts the chart has are considered, and
    with ``min_levels`` only those with more hand-charted lower levels than ``min_levels[part]``."""
    folder = drumjoin.chart_folder(Path(chart_folder))
    charts, ini = fivefretchart.load_song_folder(folder)
    report: dict = {"parts": {}, "written": []}
    wanted = []
    for part in parts:
        if part not in charts:
            continue
        if min_levels and part in min_levels and _hand_levels(charts[part]) <= min_levels[part]:
            log(f"  {part}: {_hand_levels(charts[part])} hand-charted lower level(s), not more than "
                f"{min_levels[part]}: skipped")
            continue
        wanted.append(part)
    if not wanted:
        return report
    sp = drumjoin.SloppakFiles(sloppak)
    try:
        audio: dict[str, tuple] = {}      # our role -> (y, kind)
        onsets_by_kind: dict[str, np.ndarray] = {}
        passed: dict[str, tuple] = {}
        for part in wanted:
            chart = charts[part]
            res = {"ok": False, "reused_drums": False, "method": None, "offset": None, "median_ms": None,
                   "within_30ms": None, "beat_margin": None, "levels": _hand_levels(chart), "reasons": [],
                   "chart_audio": None}
            report["parts"][part] = res
            try:
                role = _our_role(part)
                if role not in audio:
                    audio[role] = our_part_audio(sp, part)
                ref_y, ref_kind = audio[role]
                if ref_y is None:
                    raise RuntimeError("sloppak has no audio")
                if ref_kind not in onsets_by_kind:
                    onsets_by_kind[ref_kind] = drumalign.detect_onsets(ref_y)
                onsets = onsets_by_kind[ref_kind]
                th = part_thresholds(part, thresholds)
                beat = _beat(chart)
                got = None
                if reuse and reuse.get("warp"):
                    placed, det, v, ok = _reuse_part(folder, chart, reuse["warp"], onsets, beat, th, log)
                    log(f"  {part}: reused drum warp -> {'OK' if ok else 'FLAGGED: ' + '; '.join(v.reasons)}")
                    if ok:
                        got = (placed, det, v, True)
                if got is None:
                    placed, det = align_part(folder, sp, chart, part, ref_y=ref_y, ref_kind=ref_kind, onsets=onsets,
                                             chart_parts=set(charts), log=log)
                    v = validate_part([n.time for n in placed.notes], onsets, beat, th)
                    got = (placed, det, v, False)
                placed, det, v, reused = got
                res.update(ok=v.ok, reused_drums=reused, method=det["alignment"]["method"],
                           offset=det["alignment"]["offset"], median_ms=round(v.median_offset * 1000, 1),
                           within_30ms=round(v.within_30ms, 3), beat_margin=round(v.pads["beat_margin"], 3),
                           reasons=list(v.reasons), chart_audio=det["chart_audio"])
                if reused:
                    res["clamped"] = det["clamped"]
                log(f"  {part}: {len(placed.notes)} notes, median offset {v.median_offset * 1000:+.0f} ms, "
                    f"{v.within_30ms:.0%} within 30 ms, beat margin {v.pads['beat_margin']:.2f} -> "
                    f"{'OK' if v.ok else 'FLAGGED: ' + '; '.join(v.reasons)}")
                if v.ok:
                    passed[part] = (placed, det, v, reused)
            except Exception as e:      # one bad part shouldn't stop the next
                log(f"  {part} failed: {e}")
                res["error"] = str(e)
        if passed and not dry_run:
            src = _source_with_charter(source, ini)
            payloads, info = {}, {}
            for part, (placed, det, v, reused) in passed.items():
                fivefretchart.ensure_levels(placed)
                payloads[part] = fivefretchart.five_fret_json(placed, src, f"{fivefretchart.PART_NAMES[part]} (5-fret)")
                info[part] = {"tool": "fivefretjoin", "folder": str(Path(folder).resolve()), "source": src,
                              "reused_drums": reused, "detail": det, "validation": v.summary(),
                              "joined": time.strftime("%Y-%m-%d %H:%M")}
            write_five_fret(sp, payloads, info, backup_dir)
            report["written"] = list(payloads)
            log(f"  wrote {', '.join(payloads)}")
    finally:
        sp.close()
    return report
