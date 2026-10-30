"""Clone Hero / YARG songs folder as part of the library.

The user points the app at their Clone Hero songs folder (setting ``clonehero_dir``;
when unset, the first existing default below). The library scan lists its song
folders and ``.sng`` packages and indexes each one from ``song.ini`` alone (the
``diff_*`` keys say which parts it has), so a folder of thousands of songs costs
one small file read per song. Nothing is converted until a song is played: then
lib/ch_convert.py writes a directory-form feedpak into ``<DLC>/clonehero-cache/``
and the song plays as any other pack.

A Clone Hero song's library filename is ``clonehero-cache/<path under the songs
folder>.feedpak`` — the DLC-relative path of its converted pack. Every route that
takes a filename (highway, stems, art, edit) therefore resolves the converted pack
like any other song; the cache folder is kept out of the scan the same way
``tutorials-builtin/`` is. A pack is rebuilt when its source is newer, and packs
whose source is gone are pruned by the scan.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath

import drum_rating
import drumchart
import fivefretchart
import sngfile
import vocalchart

log = logging.getLogger("feedBack.clonehero")

CACHE_DIR = "clonehero-cache"
PACK_SUFFIX = ".feedpak"
CONFIG_KEY = "clonehero_dir"
DEFAULT_DIRS = ("~/Documents/Clone Hero/Songs", "~/Clone Hero/Songs", "~/.clonehero/Songs")

CHART_NAMES = ("notes.mid", "notes.midi", "notes.chart")

# song.ini difficulty keys per part; any value >= 0 means the chart has the part.
DIFF_KEYS = {
    "drums": ("diff_drums", "diff_drums_real", "diff_drums_real_ps"),
    "guitar": ("diff_guitar",),
    "rhythm": ("diff_rhythm", "diff_guitar_coop"),
    "bass": ("diff_bass",),
    "vocals": ("diff_vocals", "diff_vocals_harm"),
}
MID_TRACKS = {
    "drums": drumchart.DRUM_TRACK_NAMES,
    **fivefretchart.MID_TRACKS,
    "vocals": vocalchart.VOCAL_TRACK_NAMES,
}
CHART_SECTIONS = {"drums": "ExpertDrums",
                  **{p: "Expert" + s for p, s in fivefretchart.CHART_SECTIONS.items()}}

# Same grouping as ch_convert.build_stems: what the converted pack's stems will be.
_ROLE_TO_STEM = {"guitar": "guitar", "rhythm": "guitar", "bass": "bass", "drums": "drums",
                 "vocals": "vocals", "keys": "piano", "song": "other"}
_STEM_ORDER = ["guitar", "bass", "drums", "vocals", "piano", "other"]
_AUDIO_RE = re.compile(r"^(song|guitar|rhythm|bass|drums|vocals|keys)(?:_\d+)?$")


# ── where the songs are ──────────────────────────────────────────────────────

def songs_dir(cfg: dict | None) -> Path | None:
    """The configured Clone Hero songs folder, else the first default that exists."""
    raw = str((cfg or {}).get(CONFIG_KEY) or "").strip()
    cands = [raw] if raw else list(DEFAULT_DIRS)
    for c in cands:
        p = Path(c).expanduser()
        if p.is_dir():
            return p
    return None


def _is_song_dir(names: set[str]) -> bool:
    low = {n.lower() for n in names}
    return "song.ini" in low and (any(c in low for c in CHART_NAMES)
                                  or any(n.endswith((".mid", ".chart")) for n in low))


def list_songs(root: Path) -> list[Path]:
    """Song folders (song.ini + a chart) and .sng packages under ``root``. A song
    folder is not descended into; hidden folders are skipped."""
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        if here != root and _is_song_dir(set(filenames)):
            out.append(here)
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        out += [here / f for f in sorted(filenames)
                if f.lower().endswith(".sng") and not f.startswith(".")]
    return sorted(out)


def library_key(src: Path, root: Path) -> str:
    return f"{CACHE_DIR}/{src.relative_to(root).as_posix()}{PACK_SUFFIX}"


def is_key(filename: str) -> bool:
    return isinstance(filename, str) and filename.replace("\\", "/").startswith(CACHE_DIR + "/")


def source_for_key(filename: str, root: Path | None) -> Path | None:
    """The song folder / .sng a library filename stands for (None if it escapes the
    songs folder or is gone)."""
    if root is None or not is_key(filename):
        return None
    rel = filename.replace("\\", "/")[len(CACHE_DIR) + 1:]
    if not rel.endswith(PACK_SUFFIX):
        return None
    rel = rel[:-len(PACK_SUFFIX)]
    pp = PurePosixPath(rel)
    if not rel or pp.is_absolute() or ".." in pp.parts or "\x00" in rel:
        return None
    src = root / rel
    return src if src.exists() else None


def _chart_file(folder: Path) -> Path | None:
    return drumchart.find_chart_file(folder)


def source_stat(src: Path) -> tuple[float, int]:
    """(mtime, size) the library cache keys on: the .sng file, or a folder's
    song.ini + chart."""
    if src.is_file():
        st = src.stat()
        return st.st_mtime, st.st_size
    files = [src / "song.ini", _chart_file(src)]
    sts = [f.stat() for f in files if f is not None and f.exists()]
    return max(s.st_mtime for s in sts), sum(s.st_size for s in sts)


# ── indexing from song.ini ───────────────────────────────────────────────────

def _sng_member(path: Path, files: dict, mask: bytes, name: str) -> bytes | None:
    entry = next((v for k, v in files.items() if k.lower() == name), None)
    if entry is None:
        return None
    off, size = entry
    with open(path, "rb") as f:
        f.seek(off)
        return sngfile.unmask(f.read(size), mask)


def _read_source(src: Path) -> tuple[dict, list[str], callable]:
    """(song.ini dict, file names, chart-bytes loader) for a folder or .sng."""
    if src.is_file():
        meta, files = sngfile.read_sng(src)
        mask = meta.pop("mask")
        ini = {str(k).strip().lower(): str(v).strip() for k, v in meta.items()}

        def chart():
            for n in CHART_NAMES:
                data = _sng_member(src, files, mask, n)
                if data is not None:
                    return n, data
            return None
        return ini, list(files), chart
    ini = drumchart.load_song_ini(src / "song.ini")

    def chart():
        p = _chart_file(src)
        return (p.name.lower(), p.read_bytes()) if p else None
    return ini, [p.name for p in src.iterdir()], chart


def _varlen(buf: bytes, i: int) -> tuple[int, int]:
    v = 0
    while i < len(buf):
        b = buf[i]
        i += 1
        v = (v << 7) | (b & 0x7F)
        if not b & 0x80:
            break
    return v, i


def mid_track_names(data: bytes) -> set[str]:
    """Track names of a MIDI file from each track's leading name event — no event
    parsing beyond that."""
    names, i = set(), 0
    while i + 8 <= len(data):
        tag, ln = data[i:i + 4], int.from_bytes(data[i + 4:i + 8], "big")
        body = data[i + 8:i + 8 + ln]
        i += 8 + ln
        if tag != b"MTrk":
            continue
        j = 0
        while j < len(body):
            _, j = _varlen(body, j)                     # delta
            if j + 1 >= len(body) or body[j] != 0xFF:
                break
            kind = body[j + 1]
            n, j = _varlen(body, j + 2)
            if kind == 0x03:
                names.add(body[j:j + n].decode("latin-1").strip())
                break
            j += n
    return names


def _sniff_parts(name: str, data: bytes) -> set[str]:
    """Parts a chart file declares, from MIDI track names or .chart section headers."""
    if name.endswith(".chart"):
        secs = set(re.findall(r"^\s*\[(\w+)\]", data.decode("utf-8-sig", "replace"), re.M))
        return {p for p, s in CHART_SECTIONS.items() if s in secs}
    tracks = mid_track_names(data)
    return {p for p, names in MID_TRACKS.items() if tracks & set(names)}


def _ini_parts(ini: dict) -> tuple[set[str], set[str]]:
    """(parts song.ini says are there, parts it says nothing about)."""
    have, unknown = set(), set()
    for part, keys in DIFF_KEYS.items():
        vals = []
        for k in keys:
            try:
                vals.append(int(float(ini[k])))
            except (KeyError, ValueError):
                pass
        if not vals:
            unknown.add(part)
        elif max(vals) >= 0:
            have.add(part)
    return have, unknown


def _stem_ids(files: list[str]) -> list[str]:
    roles = set()
    for f in files:
        p = PurePosixPath(f.replace("\\", "/"))
        if p.suffix.lower() in drumchart.AUDIO_EXTS and not p.name.lower().startswith("preview"):
            m = _AUDIO_RE.match(p.stem.lower())
            if m:
                roles.add(m.group(1))
    if roles <= {"song"}:
        return ["full"] if roles else []            # a lone song.ogg is the `full` mix
    return sorted({_ROLE_TO_STEM[r] for r in roles}, key=_STEM_ORDER.index)


def index_meta(src: Path) -> dict:
    """Library metadata for a CH song from song.ini (the chart is only sniffed for
    the parts song.ini does not mention). Same shape as sloppak.extract_meta gives
    for the converted pack; ``playable`` is False when it has neither drums nor a
    5-fret part (the converter would refuse it)."""
    ini, files, chart = _read_source(src)
    parts, unknown = _ini_parts(ini)
    chart_name = None
    if unknown:
        got = chart()
        if got:
            chart_name = got[0]
            parts |= _sniff_parts(*got) & unknown
    if chart_name is None:
        chart_name = next((n.lower() for n in files if n.lower() in CHART_NAMES), "")
    if chart_name.endswith(".chart"):
        parts.discard("vocals")                     # the converter reads vocals from notes.mid only
    from ch_convert import five_fret_name, parse_year
    five = [p for p in fivefretchart.PARTS if p in parts]
    drums = "drums" in parts
    arrangements = [{"index": 0, "name": "Drums", "notes": 0}] if drums else []
    if not arrangements and five:
        arrangements = [{"index": 0, "name": five_fret_name(five[0]), "notes": 0}]
    try:
        duration = max(0.0, float(ini.get("song_length") or 0) / 1000.0)
    except ValueError:
        duration = 0.0
    year = parse_year(ini.get("year"))
    stem_ids = _stem_ids(files)
    return {
        "title": ini.get("name") or (src.stem if src.is_file() else src.name),
        "artist": ini.get("artist") or "Unknown",
        "album": ini.get("album", ""),
        "year": str(year) if year else "",
        "genre": ini.get("genre", ""),
        "track_number": None,
        "disc": None,
        "duration": duration,
        "arrangements": arrangements,
        "has_lyrics": "vocals" in parts,
        "has_vocal_pitch": "vocals" in parts,
        "has_drum_tab": drums,
        # song.ini's authored tier; the chart's levels are only known once converted.
        "drum_tier": drum_rating.ini_tier(ini) if drums else None,
        "drum_tier_estimated": False,
        "drum_levels_generated": False,
        "five_fret_parts": [f"{p}:chart" for p in five],
        "five_fret_only": bool(five) and not drums,
        "stem_count": len(stem_ids),
        "stem_ids": stem_ids,
        "playable": drums or bool(five),
    }


# ── cover art before conversion ──────────────────────────────────────────────

_COVER_NAMES = ("album.jpg", "album.png", "album.jpeg", "cover.jpg", "cover.png")


def source_cover(src: Path) -> tuple[bytes, str] | None:
    """(bytes, media type) of the song's album art, read from the folder / .sng."""
    sng = sngfile.read_sng(src) if src.is_file() else None
    for name in _COVER_NAMES:
        if sng:
            data = _sng_member(src, sng[1], sng[0]["mask"], name)
        else:
            p = src / name
            data = p.read_bytes() if p.is_file() else None
        if data:
            return data, "image/png" if name.endswith(".png") else "image/jpeg"
    return None


# ── conversion on first play ─────────────────────────────────────────────────

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def pack_is_fresh(pack: Path, src: Path) -> bool:
    mf = pack / "manifest.yaml"
    try:
        return mf.stat().st_mtime >= source_stat(src)[0]
    except OSError:
        return False


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_converted(filename: str, src: Path, dlc: Path) -> tuple[Path, bool]:
    """(the converted pack for ``filename``, whether it was (re)built now). Builds
    into a temp folder beside the cache and swaps it in, so a failed conversion
    never leaves a half-written pack behind."""
    from ch_convert import convert
    pack = dlc / filename
    with _lock_for(filename):
        if pack_is_fresh(pack, src):
            return pack, False
        pack.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".convert-", dir=dlc / CACHE_DIR))
        try:
            out = tmp / "pack"
            md5 = _md5(src) if src.is_file() else None
            summary = convert(src, out, as_dir=True, md5=md5)
            if pack.exists():
                shutil.rmtree(pack) if pack.is_dir() else pack.unlink()
            out.rename(pack)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        log.info("clonehero: converted %s (drums %d notes, 5-fret %s, %d syllables)", src.name,
                 summary["notes"], summary["five_fret"], summary["lyrics"])
        return pack, True


def prune_cache(dlc: Path, keep: set[str]) -> int:
    """Remove converted packs (and stale temp folders) whose song is no longer in
    the Clone Hero folder. Returns how many were removed."""
    base = dlc / CACHE_DIR
    if not base.is_dir():
        return 0
    removed = 0
    for dirpath, dirnames, _files in os.walk(base):
        here = Path(dirpath)
        for d in list(dirnames):
            p = here / d
            rel = p.relative_to(dlc).as_posix()
            # a temp folder is an interrupted conversion once it is an hour old
            stale_tmp = d.startswith(".convert-") and time.time() - p.stat().st_mtime > 3600
            if stale_tmp or (d.endswith(PACK_SUFFIX) and rel not in keep):
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
                dirnames.remove(d)
            elif d.endswith(PACK_SUFFIX):
                dirnames.remove(d)
    for dirpath, dirnames, filenames in os.walk(base, topdown=False):   # folders left empty
        here = Path(dirpath)
        inside = here.relative_to(base).parts
        if (inside and not any(x.endswith(PACK_SUFFIX) or x.startswith(".convert-") for x in inside)
                and not any(here.iterdir())):
            here.rmdir()
    return removed


def source_for_filename(filename: str) -> Path | None:
    """source_for_key against the configured songs folder (None for any other song)."""
    if not is_key(filename):
        return None
    import appstate
    from appconfig import _load_config
    return source_for_key(filename, songs_dir(_load_config(appstate.config_dir / "config.json")))


def prepare_for_play(filename: str, dlc: Path) -> Path:
    """Convert (if needed) the Clone Hero song behind library ``filename`` and
    return its pack. A fresh conversion also refreshes the song's library row from
    the pack itself, so parts song.ini got wrong are corrected after first play."""
    import appstate
    src = source_for_filename(filename)
    if src is None:
        raise FileNotFoundError("This Clone Hero song is no longer in the Clone Hero songs folder")
    pack, built = ensure_converted(filename, src, dlc)
    if built:
        from scan_worker import _extract_meta_sloppak
        try:
            meta = _extract_meta_sloppak(pack)
            ini = _read_source(src)[0]
            if not meta.get("genre"):
                meta["genre"] = ini.get("genre", "")
            # song.ini's authored tier beats the estimate from the converted chart.
            tier = drum_rating.ini_tier(ini) if meta.get("has_drum_tab") else None
            if tier is not None:
                meta["drum_tier"], meta["drum_tier_estimated"] = tier, False
            appstate.meta_db.put(filename, *source_stat(src), meta)
        except Exception as e:
            log.warning("clonehero: could not refresh the library row for %s: %s", filename, e)
    return pack
