"""Chorus Encore (enchor.us) client: find Clone Hero charts with an Expert drums / guitar / rhythm / bass part and
download just the pieces of the .sng a part join (drums, guitar, rhythm, bass) needs (HTTP range requests).

Free public service run by the Clone Hero community; be polite: one request at a time
per process, a short pause between searches, retries with backoff.

mattromano custom build (ported from the Slopsmith 0.2.9 tools); used by
scripts/drums_library.py. Downloads land in the same cache as the old tools
(~/.cache/slopsmith-drums), so charts fetched there are reused.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import sngfile

API = "https://api.enchor.us/search"
FILES = "https://files.enchor.us/{md5}.sng"
UA = "feedBack-drums/1.0 (+https://github.com/mattromano/feedBack)"
THROTTLE = Path.home() / ".cache" / "slopsmith-drums" / "chorus.throttle"


def _lock(f, on: bool):
    if sys.platform == "win32":                 # no fcntl on Windows; lock byte 0 with msvcrt
        import msvcrt
        f.seek(0)
        if not on:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            return
        # LK_LOCK gives up after ~10 s (EDEADLK) while other workers queue: keep trying.
        while True:
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                time.sleep(0.2)
    else:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX if on else fcntl.LOCK_UN)


def _throttle(min_gap: float):
    """Space requests out across every process on this machine (file lock + timestamp)."""
    THROTTLE.parent.mkdir(parents=True, exist_ok=True)
    with open(THROTTLE, "a+") as f:
        _lock(f, True)
        f.seek(0)
        try:
            last = float(f.read().strip() or 0)
        except ValueError:
            last = 0.0
        wait = last + min_gap - time.time()
        if wait > 0:
            time.sleep(wait)
        f.seek(0)
        f.truncate()
        f.write(str(time.time()))
        f.flush()
        _lock(f, False)


def _req(url, data=None, headers=None, timeout=30, tries=6, min_gap=1.0):
    h = {"User-Agent": UA, **(headers or {})}
    err = None
    for k in range(tries):
        _throttle(min_gap)
        try:
            r = urllib.request.Request(url, data=data, headers=h, method="POST" if data else "GET")
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                raise
            err = e
            if e.code == 429:
                ra = e.headers.get("Retry-After")
                time.sleep(float(ra) if ra and ra.replace(".", "").isdigit() else 10 * (k + 1))
                continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            err = e
        time.sleep(2 ** k)
    raise err


PART_INSTRUMENTS = {"drums": ("drums",), "guitar": ("guitar",), "rhythm": ("rhythm", "guitarcoop"),
                    "bass": ("bass",)}
STEM_ROLES = {"drums": ("drums",), "guitar": ("guitar",), "rhythm": ("rhythm", "guitar"),
              "bass": ("bass", "rhythm")}
LOWER = ("easy", "medium", "hard")


def stem_roles(part: str, chart_has_rhythm: bool) -> tuple:
    """Stem names to fetch for ``part``, in preference order.  Rock Band bass audio is often
    rhythm.*, so bass also takes rhythm when the chart has no rhythm part of its own."""
    if part == "bass" and chart_has_rhythm:
        return ("bass",)
    return STEM_ROLES[part]


def has_part(r: dict, part: str) -> bool:
    """A Chorus search result with a ``part`` chart (notesData instruments or diff_<part>)."""
    if any(i in ((r.get("notesData") or {}).get("instruments") or []) for i in PART_INSTRUMENTS[part]):
        return True
    d = r.get(f"diff_{part}")
    return d is not None and d >= 0


def part_levels(r: dict, part: str) -> int:
    """Hand-charted lower levels (Easy/Medium/Hard with notes) of ``part`` in a search result."""
    counts = (r.get("notesData") or {}).get("noteCounts") or []
    ids = PART_INSTRUMENTS[part]
    have = {c.get("difficulty") for c in counts if c.get("instrument") in ids and (c.get("count") or 0) > 0}
    return sum(lv in have for lv in LOWER)


def search(query: str, per_page: int = 25, parts: tuple = ("drums",)) -> list[dict]:
    """Charts matching ``query`` that contain any of ``parts``.  (The API's own instrument /
    difficulty filters drop obvious matches, so results are filtered here instead.)"""
    body = json.dumps({"search": query, "page": 1, "per_page": per_page, "instrument": None,
                       "difficulty": None, "drumType": None, "source": "website"}).encode()
    out = json.loads(_req(API, body, {"Content-Type": "application/json"}, min_gap=1.2))
    return [r for r in out.get("data", []) if any(has_part(r, p) for p in parts)]


def _range(md5, a, b):
    return _req(FILES.format(md5=md5), headers={"Range": f"bytes={a}-{b}"}, timeout=60, min_gap=0.25)


AUDIO = (".opus", ".ogg", ".mp3", ".wav", ".flac")


def _wanted(package_files, roles: tuple) -> list[str]:
    """Chart, plus the audio files matching any of ``roles`` (else all non-preview, non-crowd audio)."""
    names = [n for n in package_files if n.lower() in ("notes.mid", "notes.chart")]
    if not roles:
        return names
    audio = [n for n in package_files if Path(n).suffix.lower() in AUDIO and not n.lower().startswith("preview")
             and not n.lower().startswith("crowd")]
    stems = [n for n in audio if any(n.lower().startswith(r) for r in roles)]
    return names + (stems or audio)


def fetch(md5: str, out_dir: Path, roles: tuple = ("drums",)) -> Path:
    """Song folder with song.ini, the chart and the audio a join needs: the stems matching any of
    ``roles`` if the package has them, else every audio file (the full mix); no roles = chart only.
    Cached in out_dir; a later call with other roles adds just the missing files."""
    out = Path(out_dir)
    done = out / ".complete"
    state = {}
    if done.exists():
        try:
            state = json.loads(done.read_text())
        except ValueError:
            state = {}
    have = list(state.get("files") or [])
    pkg = state.get("package_files")
    if state and pkg is not None and not [n for n in _wanted(pkg, roles) if n not in have]:
        return out
    buf = _range(md5, 0, 65535)
    while True:
        try:
            meta, files, _ = sngfile.parse_header(buf)
            break
        except sngfile.NeedMore as e:
            buf = _range(md5, 0, e.need - 1)
    mask = meta.pop("mask")
    out.mkdir(parents=True, exist_ok=True)
    for n in _wanted(list(files), roles):
        if n in have:
            continue
        off, size = files[n]
        safe = Path(n.replace("\\", "/")).name          # no directories from the package
        if not safe or safe in (".", ".."):
            continue
        data = _range(md5, off, off + size - 1) if size else b""
        (out / safe).write_bytes(sngfile.unmask(data, mask))
        have.append(n)
    if not state:
        ini = "".join(f"{k} = {v}\n" for k, v in meta.items() if "\n" not in k + v)
        (out / "song.ini").write_text("[song]\n" + ini, encoding="utf-8")
    done.write_text(json.dumps({"md5": md5, "files": have, "package_files": list(files)}))
    return out
