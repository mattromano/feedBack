"""Pack side of the five-fret join (mattromano custom build; offline tool).

The five-fret counterpart of ``drumjoin.py``: finds a song's real Clone Hero / Rock Band
guitar, rhythm and bass charts and writes them into a sloppak as feedpak ``five_fret`` parts
(``type: five_fret`` arrangement entries pointing at ``five_fret_<part>.json``; how each part was
made goes to a ``five_fret_join.json`` side file). This module holds the helpers that work on the
pack: which parts exist already, which arrangement a part pairs with, the drum join's chart->audio
warp (reused when the same chart is used), and the writer.
"""
from __future__ import annotations

import json
import re
from pathlib import Path, PureWindowsPath
from typing import Callable

import drumalign
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
