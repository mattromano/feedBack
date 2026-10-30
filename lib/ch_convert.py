#!/usr/bin/env python3
"""Clone Hero / YARG / Rock Band song folder (or .sng) -> a drums / 5-fret feedpak.

Shared by scripts/ch_to_sloppak.py (command line) and lib/clonehero.py (the library's
Clone Hero songs folder, converted on first play).

SONG_FOLDER holds ``notes.mid`` (PART DRUMS and/or PART GUITAR / RHYTHM / BASS) or
``notes.chart`` ([ExpertDrums], [ExpertSingle] / [ExpertDoubleRhythm] / [ExpertDoubleBass]),
``song.ini`` and the audio (``song.ogg`` and/or ``guitar``/``rhythm``/``bass``/
``drums[_1-4]``/``vocals``/``keys`` stems). An ``.sng`` package is unpacked first.

The pack it writes (feedpak 1.21.0); it needs drums or at least one 5-fret part:

- ``drum_tab.json`` — when the chart has drums: the Expert drums part as 4-lane pro piece hits (2x kicks
  flagged ``x2``), plus the chart's star power, fills and solos, and its
  Easy/Medium/Hard parts as ``levels`` (any missing level is reduced from Expert
  and marked ``generated``). A chart with star power but no fills gets YARG's
  generated activation fills. Spec §7.5 "Drum gameplay metadata".
- ``five_fret_<part>.json`` — each guitar / rhythm / bass part with Expert notes: fret
  masks with HOPOs and taps resolved, sustains, star power and solos, and the chart's
  Easy/Medium/Hard (missing ones reduced from Expert and marked ``generated``). Spec §7.10;
  parsing rules: lib/fivefretchart.py.
- ``song_timeline.json`` — tempo map, time signatures, beats and sections.
- ``lyrics.json`` + ``vocal_pitch.json`` — when ``notes.mid`` has a ``PART VOCALS`` part: the
  chart's syllables, line breaks and sung pitches (spec §7.1 / §7.2, ``lyrics_source: authored``;
  rules in lib/vocalchart.py), so the pack is karaoke-ready with the chart's ``vocals`` stem.
- ``manifest.yaml`` — a ``type: drums`` arrangement pointing at the tab, with the
  song-level ``drum_tab`` key as its primary-part alias (spec §7.5 "Multiple drum
  parts"), then one ``type: five_fret`` arrangement per 5-fret part (``five_fret_guitar``,
  ``five_fret_rhythm``, ``five_fret_bass``). The pack has no pitched arrangement.

song.ini ``delay`` is applied, so chart times line up with the audio as YARG plays
it. Audio: a lone ``song.ogg`` becomes the ``full`` stem; separate CH stems become
pack stems (guitar+rhythm -> guitar, keys -> piano, song -> other, drums_1..4 mixed
into drums; crowd is dropped). A single ``.ogg`` per stem is copied as-is; anything
else is mixed / transcoded with ffmpeg. Parsing rules: lib/drumchart.py.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import yaml

import audio
import drumchart
import drumchart_gen
import fivefret
import fivefretchart
import sngfile
import vocalchart

log = logging.getLogger("feedBack.ch_convert")

# The format version this converter writes: five-fret parts (1.21.0, fork only).
FEEDPAK_VERSION = "1.21.0"

STEM_ORDER = ["full", "guitar", "bass", "drums", "vocals", "piano", "other"]
ROLE_TO_STEM = {"guitar": "guitar", "rhythm": "guitar", "bass": "bass", "drums": "drums",
                "vocals": "vocals", "keys": "piano", "song": "other"}


def _ffmpeg() -> str:
    ff = audio._ffmpeg_cmd()
    if not ff:
        raise RuntimeError("ffmpeg is required to mix or transcode these stems")
    return ff


def audio_seconds(path: Path) -> float:
    """Duration via ffprobe next to ffmpeg; 0.0 when unavailable."""
    probe = audio._bundled_or_path("ffprobe")
    if not probe:
        return 0.0
    try:
        r = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                            str(path)], capture_output=True, text=True, timeout=60)
        return float(r.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0.0


def encode_stem(inputs: list[Path], out: Path) -> None:
    """One or more audio files -> one OGG (summed, not normalised, like the CH mixer)."""
    if len(inputs) == 1 and inputs[0].suffix.lower() == ".ogg":
        shutil.copyfile(inputs[0], out)
        return
    cmd = [_ffmpeg(), "-v", "error", "-y"]
    for p in inputs:
        cmd += ["-i", str(p)]
    if len(inputs) > 1:
        cmd += ["-filter_complex", f"amix=inputs={len(inputs)}:duration=longest:normalize=0"]
    cmd += ["-vn", "-c:a", "libvorbis", "-q:a", "6", str(out)]
    subprocess.run(cmd, check=True)


def build_stems(folder: Path, stems_dir: Path) -> list[dict]:
    files = drumchart.song_audio_files(folder)
    files.pop("crowd", None)
    if not files:
        raise ValueError(f"{folder.name}: no audio (song.ogg / stems) found")
    stems_dir.mkdir(parents=True, exist_ok=True)
    if set(files) == {"song"}:
        encode_stem(files["song"], stems_dir / "full.ogg")
        return [{"id": "full", "file": "stems/full.ogg", "default": True}]
    groups: dict[str, list[Path]] = {}
    for role, paths in files.items():
        groups.setdefault(ROLE_TO_STEM[role], []).extend(paths)
    out = []
    for sid in sorted(groups, key=STEM_ORDER.index):
        encode_stem(groups[sid], stems_dir / f"{sid}.ogg")
        out.append({"id": sid, "file": f"stems/{sid}.ogg", "default": True})
    return out


def parse_year(v) -> int:
    m = re.search(r"\d{4}", str(v or ""))
    return int(m.group(0)) if m else 0


def find_cover(folder: Path) -> Path | None:
    for name in ("album.jpg", "album.png", "album.jpeg", "cover.jpg", "cover.png"):
        p = folder / name
        if p.exists():
            return p
    return None


def zip_dir(work: Path, out: Path) -> None:
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as z:
        for f in sorted(work.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(work).as_posix())
    if out.exists():
        out.unlink() if out.is_file() else shutil.rmtree(out)
    tmp.rename(out)


def prepare_drums(folder: Path) -> tuple[drumchart.DrumChart | None, dict]:
    """Parse the folder's drums (None when the chart has no Expert drum notes) and fill in
    what YARG/feedBack generate for charts without it: activation fills (when there is
    star power) and lower levels."""
    chart, ini = drumchart.load_song_folder(folder, missing_ok=True)
    if not chart.hits:
        return None, ini
    if chart.star_power and not chart.fills:
        beats = [(b["time"], b["measure"]) for b in chart.beat_list()]
        chart.fills = drumchart_gen.auto_fills([h.time for h in chart.hits], beats, chart.star_power,
                                               chart.solos, [t for t, _ in chart.sections])
    drumchart_gen.ensure_levels(chart)
    return chart, ini


def prepare_five_fret(folder: Path) -> dict[str, fivefretchart.FiveFretChart]:
    """The folder's guitar / rhythm / bass parts with Expert notes, lower levels filled in."""
    parts, _ = fivefretchart.load_song_folder(folder)
    for chart in parts.values():
        fivefretchart.ensure_levels(chart)
    return parts


def five_fret_name(part: str) -> str:
    return f"{fivefretchart.PART_NAMES[part]} (5-fret)"


def convert(src: Path, out: Path, title=None, artist=None, album=None, year=None, as_dir=False,
            md5: str | None = None) -> dict:
    """Write a drums / 5-fret pack for a CH/YARG song folder or .sng. ``md5`` (the .sng
    file's, from the caller) goes into the 5-fret parts' ``source``. Returns a summary dict."""
    src = Path(src)
    unpacked = None
    work = Path(tempfile.mkdtemp(prefix="ch2pak_"))
    try:
        if src.is_file() and src.suffix.lower() == ".sng":
            unpacked = Path(tempfile.mkdtemp(prefix="sng_"))
            folder = sngfile.extract_sng(src, unpacked)
        else:
            folder = src
        chart, ini = prepare_drums(folder)
        parts = prepare_five_fret(folder)
        if chart is None and not parts:
            raise ValueError(f"{folder.name}: no drums or 5-fret guitar / rhythm / bass part with notes")
        # the timeline comes from the drums parse (tempo map + sections), hits or not
        timeline = chart or drumchart.load_song_folder(folder, levels=False, missing_ok=True)[0]
        stems = build_stems(folder, work / "stems")
        last = [chart.hits[-1].time] if chart else []
        last += [n.time + n.length for c in parts.values() for n in c.notes]
        dur = max([audio_seconds(work / s["file"]) for s in stems] + [max(last) + 1.0])
        if chart:
            (work / "drum_tab.json").write_text(json.dumps(drumchart.drum_tab(chart), separators=(",", ":")),
                                                encoding="utf-8")
        source = {"kind": "chart", "origin": "local"}
        if md5 and src.is_file() and src.suffix.lower() == ".sng":
            source["md5"] = md5
        for part, ff in parts.items():
            name = f"five_fret_{part}.json"
            data = fivefretchart.five_fret_json(ff, source, five_fret_name(part))
            ok, why = fivefret.validate_five_fret(data)
            if not ok:
                raise ValueError(f"{folder.name}: {name} failed validation: {why}")
            (work / name).write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        end_tick = max((n.tick for c in parts.values() for n in c.notes), default=0)
        (work / "song_timeline.json").write_text(json.dumps(drumchart.song_timeline(timeline, end_tick),
                                                            separators=(",", ":")), encoding="utf-8")
        vocals = vocalchart.load_song_folder(folder)
        pitch = vocalchart.vocal_pitch_json(vocals) if vocals else {"notes": []}
        if vocals:
            (work / "lyrics.json").write_text(json.dumps(vocalchart.lyrics_json(vocals), ensure_ascii=False,
                                                         separators=(",", ":")), encoding="utf-8")
            (work / "vocal_pitch.json").write_text(json.dumps(pitch, separators=(",", ":")), encoding="utf-8")
        manifest = {
            "feedpak_version": FEEDPAK_VERSION,
            "title": title or ini.get("name") or folder.name,
            "artist": artist or ini.get("artist") or "Unknown",
            "album": album if album is not None else ini.get("album", ""),
            "year": year if year is not None else parse_year(ini.get("year")),
            "duration": round(dur, 3),
        }
        cover = find_cover(folder)
        if cover:
            name = "cover" + cover.suffix.lower().replace(".jpeg", ".jpg")
            shutil.copyfile(cover, work / name)
            manifest["cover"] = name
        manifest["stems"] = stems
        arrangements = []
        if chart:
            arrangements.append({"id": "drums", "name": "Drums", "type": "drums", "drum_tab": "drum_tab.json"})
        arrangements += [{"id": f"five_fret_{p}", "name": five_fret_name(p), "type": "five_fret",
                          "instrument": p, "five_fret": f"five_fret_{p}.json"} for p in parts]
        manifest["arrangements"] = arrangements
        if chart:
            manifest["drum_tab"] = "drum_tab.json"
        manifest["song_timeline"] = "song_timeline.json"
        if vocals:
            manifest["lyrics"] = "lyrics.json"
            manifest["lyrics_source"] = "authored"
            if pitch["notes"]:
                manifest["vocal_pitch"] = "vocal_pitch.json"
        (work / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
                                            encoding="utf-8")
        out = Path(out)
        if as_dir:
            if out.exists():
                shutil.rmtree(out) if out.is_dir() else out.unlink()
            shutil.copytree(work, out)
        else:
            zip_dir(work, out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        if unpacked:
            shutil.rmtree(unpacked, ignore_errors=True)
    return {"out": str(out), "notes": len(chart.hits) if chart else 0, "format": timeline.source_format,
            "star_power": len(chart.star_power) if chart else 0, "fills": len(chart.fills) if chart else 0,
            "solos": len(chart.solos) if chart else 0, "sections": len(timeline.sections),
            "delay": timeline.offset, "five_lane": chart.five_lane if chart else False,
            "levels_generated": sorted(chart.levels_generated) if chart else [],
            "five_fret": {p: len(c.notes) for p, c in parts.items()},
            "stems": [s["id"] for s in stems], "duration": manifest["duration"],
            "lyrics": len(vocals.notes) if vocals else 0, "vocal_pitch": len(pitch["notes"]),
            "vocal_phrases": len(vocals.phrases) if vocals else 0}
