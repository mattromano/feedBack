"""Offline song packs (``.fbm``) for the mobile player.

A pack is one file per song holding everything the phone needs to play it
with no server: the exact renderer messages ``/ws/highway/{id}`` streams for
every (non-drum) arrangement, plus the audio and cover bytes. The wire format
is pinned in ``docs/mobile-contract.md``::

    "FBMP" | uint32 BE header length N | N bytes UTF-8 JSON header | blobs

Header ``files[].offset`` is relative to the start of the blob section.

The messages are produced by calling the real ``highway_ws()`` handler with a
recorder standing in for the WebSocket, so the pack carries exactly what the
renderer already consumes (one source of truth, ``ws_highway.py`` unchanged).
Audio URLs inside ``song_info`` are resolved back to local files through the
same containment-checked helpers the HTTP routes use (``routers/media.py``),
never over HTTP, then rewritten to ``fbm:<key>``.

Cache layout: ``$CONFIG_DIR/mobile_cache/<sha1(id)>-<version>.fbm``. The id
hash prefix makes pruning a glob: once a new version is written, every other
``<sha1(id)>-*.fbm`` is deleted unless it was touched in the last
PRUNE_GRACE_S (a download of it may still be starting). Packs are touched
whenever they are handed out, and the whole cache is capped at
``FEEDBACK_MOBILE_CACHE_MAX_BYTES`` (default 20 GB) by evicting the least
recently touched packs after each build. Work files live in a per-build
directory under ``mobile_cache/tmp/`` that is always removed, and the final
file is published with ``os.replace`` so a reader never sees a partial pack.

Hardening (adversarial review): audio is only taken from audio members of
the song's own package (resolved, symlinks followed, inside the library or
the unpack cache) or from ``/audio/audio_*.{mp3,ogg,wav}``; only known audio
extensions reach ffmpeg, which runs with ``-protocol_whitelist file``, a
demuxer ``-format_whitelist`` and an output ``-fs`` cap; free-text error
fields in ``song_info`` are replaced with generic text before baking.

This module does no IO at import time (Principle V); paths are read from the
``appstate`` seam at call time.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote

from fastapi import WebSocketDisconnect
from fastapi.responses import FileResponse

import appstate
from audio import _ffmpeg_cmd

log = logging.getLogger("feedBack.mobile")

MOBILE_PACK_FORMAT = 1
COVER_MIMES = frozenset({"image/jpeg", "image/png", "image/webp", "image/gif", "image/avif"})
MAGIC = b"FBMP"

# Builder limits (contract). Module attributes so tests can shrink them.
MAX_TOTAL_BYTES = 2 * 1024 ** 3
MAX_FILE_BYTES = 500 * 1024 ** 2
BUILD_TIMEOUT_S = 600.0
LOCK_WAIT_S = 300.0

DEFAULT_CACHE_MAX_BYTES = 20 * 1024 ** 3
PRUNE_GRACE_S = 120.0
DIR_VERSION_TTL_S = 60.0

_FULL_BITRATE = "160k"
_STEM_BITRATE = "128k"
_COPY_MIME = {".mp3": "audio/mpeg", ".m4a": "audio/mp4"}
# Source extensions the builder accepts, and the ffmpeg demuxers they need
# (names from `ffmpeg -formats`; "mov" matches the "mov,mp4,m4a,3gp,3g2,mj2"
# demuxer, ogg covers .opus/.oga). Playlist/script demuxers (hls, concat, ...)
# stay off even if a file is misnamed.
_SOURCE_EXTS = frozenset({".ogg", ".opus", ".oga", ".flac", ".wav", ".mp3", ".m4a", ".aac"})
_FFMPEG_DEMUXERS = "ogg,flac,wav,mp3,aac,mov"
_AUDIO_ERROR_TEXT = "Audio unavailable"


class PackError(Exception):
    """A build failure whose message is safe to show the client."""


class BuildBusy(Exception):
    """Another build held the global lock past LOCK_WAIT_S."""


# ── versioning / cache paths ──────────────────────────────────────────────────

def song_version(song_id: str, mtime, size) -> str:
    return hashlib.sha1(
        f"{song_id}|{mtime}|{size}|{MOBILE_PACK_FORMAT}".encode("utf-8")
    ).hexdigest()


def source_stat(path: Path) -> tuple[float, int]:
    """(mtime, size) for a song source. A file uses its own stat. A directory
    (directory-form sloppak, loose folder) uses the newest member mtime and the
    summed member size, since a directory's own stat does not change when a
    file inside it is edited in place."""
    st = path.stat()
    if not path.is_dir():
        return st.st_mtime, st.st_size
    mtime, size = st.st_mtime, 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                fst = os.stat(os.path.join(root, name))
            except OSError:
                continue
            size += fst.st_size
            mtime = max(mtime, fst.st_mtime)
    return mtime, size


def version_for(song_id: str, path: Path) -> tuple[str, int]:
    """(version, source size) for a resolved song path."""
    mtime, size = source_stat(path)
    return song_version(song_id, mtime, size), size


# id -> (key, expires_at, (version, size)). A single file is keyed by its own
# (mtime, size), so the cached value is exactly what version_for() returns. A
# directory is keyed by the directory's own mtime and also expires after
# DIR_VERSION_TTL_S, so an in-place edit of a member (which does not change the
# directory mtime) shows up within the TTL without walking every pack per call.
_version_cache: dict[str, tuple[tuple, float, tuple[str, int]]] = {}


def _now() -> float:
    return time.monotonic()


def version_for_cached(song_id: str, path: Path) -> tuple[str, int]:
    """version_for() with the cache above; used by the songs list."""
    st = path.stat()
    is_dir = path.is_dir()
    key = (str(path), is_dir, st.st_mtime, None if is_dir else st.st_size)
    now = _now()
    hit = _version_cache.get(song_id)
    if hit is not None and hit[0] == key and (not is_dir or now < hit[1]):
        return hit[2]
    result = version_for(song_id, path)
    _version_cache[song_id] = (key, now + DIR_VERSION_TTL_S, result)
    return result


def cache_dir() -> Path:
    return Path(appstate.config_dir) / "mobile_cache"


def _id_hash(song_id: str) -> str:
    return hashlib.sha1(song_id.encode("utf-8")).hexdigest()


def cache_path(song_id: str, version: str) -> Path:
    return cache_dir() / f"{_id_hash(song_id)}-{version}.fbm"


def cached_pack(song_id: str, version: str) -> Path | None:
    p = cache_path(song_id, version)
    return p if p.is_file() else None


def _touch(p: Path) -> None:
    try:
        os.utime(p)
    except OSError:
        pass


def _recently_touched(p: Path, now: float) -> bool:
    try:
        st = p.stat()
    except OSError:
        return True   # vanished or unreadable: leave it alone
    return now - max(st.st_mtime, st.st_atime) < PRUNE_GRACE_S


def _prune_old_versions(song_id: str, keep: Path) -> None:
    """Delete this song's other versions, except ones touched in the last
    PRUNE_GRACE_S: a download of an old version may have just stat'ed it (for
    Content-Length) and not opened it yet. They are pruned on a later call."""
    now = time.time()
    for old in cache_dir().glob(f"{_id_hash(song_id)}-*.fbm"):
        if old == keep or _recently_touched(old, now):
            continue
        try:
            old.unlink()
        except OSError:
            log.debug("mobile pack prune failed for %s", old, exc_info=True)


def cache_max_bytes() -> int:
    raw = os.environ.get("FEEDBACK_MOBILE_CACHE_MAX_BYTES", "").strip()
    try:
        n = int(raw)
    except ValueError:
        return DEFAULT_CACHE_MAX_BYTES
    return n if n > 0 else DEFAULT_CACHE_MAX_BYTES


def enforce_cache_cap(keep: Path | None = None) -> None:
    """Evict least-recently-touched packs until the cache fits the byte cap.
    Never evicts `keep` (the pack just built) or a pack touched in the last
    PRUNE_GRACE_S, so the cap can be exceeded briefly rather than delete a
    file mid-download."""
    cap = cache_max_bytes()
    entries = []
    total = 0
    for p in cache_dir().glob("*.fbm"):
        try:
            st = p.stat()
        except OSError:
            continue
        total += st.st_size
        entries.append((max(st.st_mtime, st.st_atime), st.st_size, p))
    if total <= cap:
        return
    now = time.time()
    for touched, size, p in sorted(entries, key=lambda e: e[0]):
        if total <= cap:
            break
        if p == keep or now - touched < PRUNE_GRACE_S:
            continue
        try:
            p.unlink()
            total -= size
        except OSError:
            log.debug("mobile pack eviction failed for %s", p, exc_info=True)


# ── status tracking + global build lock ───────────────────────────────────────

# id -> {"state": "building"|"error", "version": str, "error": str|None}.
# "ready"/"missing" are derived from the cache on read, not stored.
_status: dict[str, dict] = {}

# One build at a time. asyncio.Lock binds to the loop it first waits on, and a
# TestClient (or a uvicorn reload) can run successive requests on different
# loops, so keep one lock per running loop.
_lock_state: tuple[object, asyncio.Lock] | None = None


def _build_lock() -> asyncio.Lock:
    global _lock_state
    loop = asyncio.get_running_loop()
    if _lock_state is None or _lock_state[0] is not loop:
        _lock_state = (loop, asyncio.Lock())
    return _lock_state[1]


def get_status(song_id: str, version: str) -> dict:
    p = cached_pack(song_id, version)
    if p is not None:
        return {"state": "ready", "version": version,
                "size_bytes": p.stat().st_size, "error": None}
    st = _status.get(song_id)
    if st and st.get("version") == version:
        return {"state": st["state"], "version": version, "size_bytes": None,
                "error": st.get("error")}
    return {"state": "missing", "version": version, "size_bytes": None, "error": None}


async def ensure_pack(song_id: str, version: str) -> Path:
    """Return the cached pack for (id, version), building it if needed.

    Waits up to LOCK_WAIT_S for the global build lock (BuildBusy after that);
    raises PackError when the build fails."""
    p = cached_pack(song_id, version)
    if p is not None:
        _touch(p)
        _prune_old_versions(song_id, p)
        return p
    lock = _build_lock()
    try:
        await asyncio.wait_for(lock.acquire(), LOCK_WAIT_S)
    except asyncio.TimeoutError:
        raise BuildBusy() from None
    try:
        p = cached_pack(song_id, version)   # built while we waited
        if p is not None:
            _touch(p)
            return p
        _status[song_id] = {"state": "building", "version": version, "error": None}
        try:
            out = await asyncio.wait_for(build_pack(song_id, version), BUILD_TIMEOUT_S)
        except asyncio.TimeoutError:
            _status[song_id] = {"state": "error", "version": version,
                                "error": "build timed out"}
            raise PackError("build timed out") from None
        except PackError as e:
            _status[song_id] = {"state": "error", "version": version, "error": str(e)}
            raise
        except Exception:
            log.exception("mobile pack build failed for %s", song_id)
            _status[song_id] = {"state": "error", "version": version,
                                "error": "build failed"}
            raise PackError("build failed") from None
        _status.pop(song_id, None)
        return out
    finally:
        lock.release()


# ── recording the highway stream ──────────────────────────────────────────────

class _Recorder:
    """The subset of ``WebSocket`` that ``highway_ws`` touches: accept,
    send_json, close, receive_text (its post-``ready`` keep-alive loop, which
    we end with a disconnect). Messages are JSON round-tripped so the copy is
    exactly what a real client would parse."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.closed = False

    async def accept(self, *a, **kw) -> None:
        return None

    async def send_json(self, data, mode: str = "text") -> None:
        if self.closed:
            raise WebSocketDisconnect(1000)
        self.messages.append(json.loads(json.dumps(data)))

    async def receive_text(self) -> str:
        raise WebSocketDisconnect(1000)

    async def close(self, code: int = 1000, reason: str | None = None) -> None:
        self.closed = True


async def record_highway_messages(filename: str, arrangement_index: int) -> list[dict]:
    """Every message ``/ws/highway/{filename}?arrangement=<i>`` sends, in
    order, minus ``loading`` frames. Raises PackError on an ``error`` frame or
    when the stream does not end with ``ready``."""
    from routers.ws_highway import highway_ws  # late: keeps import graph light

    rec = _Recorder()
    await highway_ws(rec, filename, arrangement=arrangement_index)
    out = []
    for m in rec.messages:
        if not isinstance(m, dict):
            continue
        if "error" in m:
            log.warning("mobile pack: highway error for %s[%s]: %s",
                        filename, arrangement_index, m.get("error"))
            raise PackError("song could not be loaded")
        if m.get("type") == "loading":
            continue
        out.append(m)
    if not out or out[-1].get("type") != "ready":
        raise PackError("song could not be loaded")
    return out


def _song_info(messages: list[dict]) -> dict | None:
    return next((m for m in messages if m.get("type") == "song_info"), None)


def _is_drums(name: str) -> bool:
    from routers.ws_highway import _is_drums_arrangement
    return _is_drums_arrangement(SimpleNamespace(name=name or ""))


# ── URL -> local file ─────────────────────────────────────────────────────────

_SLOPPAK_URL = re.compile(r"/api/sloppak/([^/?#]+)/file/([^?#]+)")
_AUDIO_URL = re.compile(r"/audio/([^?#]+)")


_AUDIO_NAME = re.compile(r"audio_[^/]+\.(?:mp3|ogg|wav)")


def _member_ext_ok(rel: str) -> bool:
    from mobile_gate import SLOPPAK_MEMBER_EXTS
    return Path(rel).suffix.lower().lstrip(".") in SLOPPAK_MEMBER_EXTS


def resolve_audio_url(song_id: str, url: str) -> Path:
    """Map a song_info audio URL to the file the HTTP route would serve,
    reusing the routes' own containment checks, plus the mobile gate's
    (symlinks followed: the package must be inside the library, and the file
    inside the package or, for a zip, inside the unpack cache)."""
    from mobile_gate import _dlc_dir, contained_member, contained_song_root
    from routers import media

    m = _SLOPPAK_URL.fullmatch(url)
    if m:
        fn, rel = unquote(m.group(1)), unquote(m.group(2))
        if fn != song_id:
            raise PackError("audio reference outside the song")
        if not _member_ext_ok(rel):
            raise PackError("unsupported audio reference")
        dlc = _dlc_dir()
        pkg = contained_song_root(dlc, fn) if dlc else None
        if pkg is None:
            raise PackError("audio reference outside the library")
        res = media._resolve_sloppak_local_file(fn, rel)
        if isinstance(res, tuple):
            raise PackError("audio file missing")
        real = Path(res).resolve()
        if pkg.is_dir():
            ok = contained_member(pkg, rel) == real
        else:
            ok = real.is_relative_to(Path(appstate.sloppak_cache_dir).resolve())
        if not ok:
            raise PackError("audio reference outside the library")
        return real
    m = _AUDIO_URL.fullmatch(url)
    if m:
        name = unquote(m.group(1))
        if not _AUDIO_NAME.fullmatch(name):
            raise PackError("unsupported audio reference")
        # serve_audio carries the traversal + relative_to containment check
        # over the audio cache and static dirs; reuse it rather than copy it.
        resp = media.serve_audio(name)
        if isinstance(resp, FileResponse):
            return Path(resp.path)
        raise PackError("audio file missing")
    raise PackError("unsupported audio reference")


def _collect_audio(song_infos: list[dict]) -> dict[str, tuple[str, str]]:
    """url -> (key, bitrate) across every arrangement's song_info."""
    url_keys: dict[str, tuple[str, str]] = {}
    for si in song_infos:
        for s in si.get("stems") or []:
            if isinstance(s, dict) and s.get("url") and s.get("id") is not None:
                url_keys.setdefault(s["url"], (f"stem:{s['id']}", _STEM_BITRATE))
        if si.get("full_mix_url"):
            url_keys.setdefault(si["full_mix_url"], ("full", _FULL_BITRATE))
        if si.get("audio_url"):
            url_keys.setdefault(si["audio_url"], ("audio", _FULL_BITRATE))
    return url_keys


def _scrub_song_info(si: dict) -> None:
    """Core fills audio_error with str(exception), which can hold absolute
    paths; a pack is a file that leaves the machine, so keep only the fact."""
    if si.get("audio_error"):
        si["audio_error"] = _AUDIO_ERROR_TEXT


def _rewrite_song_info(si: dict, url_keys: dict[str, tuple[str, str]]) -> None:
    def fbm(u):
        return f"fbm:{url_keys[u][0]}" if u in url_keys else u
    for field in ("audio_url", "full_mix_url", "original_audio_url"):
        if si.get(field):
            si[field] = fbm(si[field])
    for s in si.get("stems") or []:
        if isinstance(s, dict) and s.get("url"):
            s["url"] = fbm(s["url"])


# ── transcoding ───────────────────────────────────────────────────────────────

async def _transcode(src: Path, dest: Path, bitrate: str, deadline: float) -> None:
    ffmpeg = _ffmpeg_cmd()
    if not ffmpeg:
        raise PackError("ffmpeg not available")
    loop = asyncio.get_running_loop()
    remaining = deadline - loop.time()
    if remaining <= 0:
        raise PackError("build timed out")
    if src.suffix.lower() not in _SOURCE_EXTS:
        raise PackError("unsupported audio format")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-protocol_whitelist", "file", "-format_whitelist", _FFMPEG_DEMUXERS,
        "-i", str(src), "-vn", "-c:a", "aac", "-b:a", bitrate,
        # One byte over the per-file cap: a truncated output is always larger
        # than MAX_FILE_BYTES, so the size check after this rejects it.
        "-fs", str(MAX_FILE_BYTES + 1),
        "-movflags", "+faststart", "-f", "mp4", str(dest),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _out, err = await asyncio.wait_for(proc.communicate(), remaining)
    except asyncio.TimeoutError:
        raise PackError("build timed out") from None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        log.warning("mobile pack: ffmpeg failed (%s): %s", proc.returncode,
                    (err or b"")[-500:].decode("utf-8", "replace"))
        raise PackError("audio conversion failed")


async def _prepare_audio(src: Path, workdir: Path, bitrate: str,
                         deadline: float) -> tuple[Path, str]:
    ext = src.suffix.lower()
    if ext not in _SOURCE_EXTS:
        raise PackError("unsupported audio format")
    if ext in _COPY_MIME:
        return src, _COPY_MIME[ext]
    dest = workdir / f"{uuid.uuid4().hex}.m4a"
    await _transcode(src, dest, bitrate, deadline)
    return dest, "audio/mp4"


def _check_file_size(n: int) -> None:
    if n > MAX_FILE_BYTES:
        raise PackError("song too large")


# ── cover ─────────────────────────────────────────────────────────────────────

async def _cover_bytes(song_id: str) -> tuple[bytes, str] | None:
    """The art the app itself shows (override > pack art > CAA cache), via
    the art route's own resolution chain."""
    from routers import art as art_router
    try:
        resp = await art_router.get_song_art(song_id, request=None)
    except Exception:
        log.debug("mobile pack: cover lookup failed for %s", song_id, exc_info=True)
        return None
    if getattr(resp, "status_code", 0) != 200:
        return None
    mt = (resp.media_type or "").split(";")[0].strip()
    # Raster formats only, matching the phone's parser (static/mobile/fbm.js);
    # an SVG cover could carry script and would make the phone reject the pack.
    if mt.lower() not in COVER_MIMES:
        return None
    if isinstance(resp, FileResponse):
        p = Path(resp.path)
        _check_file_size(p.stat().st_size)
        return await asyncio.to_thread(p.read_bytes), mt
    return bytes(resp.body), mt


# ── building ──────────────────────────────────────────────────────────────────

def _album_for(song_id: str) -> str:
    """song_info carries no album; take the library's stored value."""
    try:
        return str(appstate.meta_db.pack_fields(song_id).get("album") or "")
    except Exception:
        return ""


def _write_fbm(dest: Path, header: dict, blobs: list[tuple[Path | bytes, int]]) -> None:
    raw = json.dumps(header, ensure_ascii=False, allow_nan=False,
                     separators=(",", ":")).encode("utf-8")
    total = 8 + len(raw) + sum(n for _, n in blobs)
    if total > MAX_TOTAL_BYTES:
        raise PackError("song too large")
    with open(dest, "wb") as f:
        f.write(MAGIC)
        f.write(struct.pack(">I", len(raw)))
        f.write(raw)
        for src, n in blobs:
            if isinstance(src, (bytes, bytearray)):
                f.write(src)
                continue
            with open(src, "rb") as s:
                copied = 0
                while True:
                    chunk = s.read(1024 * 1024)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > n:
                        raise PackError("audio file changed during build")
                    f.write(chunk)
                if copied != n:
                    raise PackError("audio file changed during build")
        f.flush()
        os.fsync(f.fileno())


async def build_pack(song_id: str, version: str) -> Path:
    """Build and publish the pack for (id, version). Callers normally go
    through ensure_pack(), which holds the global lock and tracks status."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + BUILD_TIMEOUT_S

    first = await record_highway_messages(song_id, 0)
    si0 = _song_info(first)
    if si0 is None:
        raise PackError("song could not be loaded")
    arr_entries = sorted(
        (a for a in si0.get("arrangements") or [] if isinstance(a, dict)),
        key=lambda a: int(a.get("index", 0)),
    )
    arrangements = []
    for a in arr_entries:
        idx = int(a.get("index", 0))
        name = str(a.get("name") or "")
        if _is_drums(name):
            continue
        msgs = first if idx == 0 else await record_highway_messages(song_id, idx)
        arrangements.append({"index": idx, "name": name, "messages": msgs})
        if loop.time() > deadline:
            raise PackError("build timed out")
    if not arrangements:
        raise PackError("no playable arrangements")

    song_infos = [si for si in (_song_info(a["messages"]) for a in arrangements) if si]
    url_keys = _collect_audio(song_infos)

    root = cache_dir()
    tmp_root = root / "tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    workdir = tmp_root / uuid.uuid4().hex
    workdir.mkdir()
    try:
        files: list[dict] = []
        blobs: list[tuple[Path | bytes, int]] = []
        offset = 0
        total = 0
        done_keys: set[str] = set()
        for url, (key, bitrate) in url_keys.items():
            if key in done_keys:
                continue
            src = resolve_audio_url(song_id, url)
            path, mime = await _prepare_audio(src, workdir, bitrate, deadline)
            n = path.stat().st_size
            _check_file_size(n)
            total += n
            if total > MAX_TOTAL_BYTES:
                raise PackError("song too large")
            files.append({"key": key, "mime": mime, "offset": offset, "length": n})
            blobs.append((path, n))
            offset += n
            done_keys.add(key)

        cover = await _cover_bytes(song_id)
        if cover is not None:
            data, mt = cover
            _check_file_size(len(data))
            files.append({"key": "cover", "mime": mt, "offset": offset, "length": len(data)})
            blobs.append((data, len(data)))
            offset += len(data)

        for si in song_infos:
            _rewrite_song_info(si, url_keys)
            _scrub_song_info(si)

        header = {
            "format": MOBILE_PACK_FORMAT,
            "id": song_id,
            "version": version,
            "title": str(si0.get("title") or ""),
            "artist": str(si0.get("artist") or ""),
            "album": _album_for(song_id),
            "duration": si0.get("duration") or 0,
            "built_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
            "arrangements": arrangements,
            "files": files,
        }
        if loop.time() > deadline:
            raise PackError("build timed out")
        tmp_out = workdir / "pack.fbm"
        try:
            await asyncio.to_thread(_write_fbm, tmp_out, header, blobs)
        except ValueError:
            # allow_nan=False: a NaN would make the header unparseable JSON.
            raise PackError("song data could not be encoded") from None
        final = cache_path(song_id, version)
        os.replace(tmp_out, final)
        _prune_old_versions(song_id, final)
        enforce_cache_cap(final)
        return final
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# ── reading (tests / tooling) ─────────────────────────────────────────────────

def parse_fbm(path) -> tuple[dict, int]:
    """Return (header, blob_offset) where blob_offset is the absolute file
    offset of the blob section."""
    with open(path, "rb") as f:
        if f.read(4) != MAGIC:
            raise ValueError("not an fbm file")
        (n,) = struct.unpack(">I", f.read(4))
        header = json.loads(f.read(n).decode("utf-8"))
    return header, 8 + n
