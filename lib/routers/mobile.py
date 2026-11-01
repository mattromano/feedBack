"""Mobile offline player routes: the /mobile shell (index, service worker,
manifest) and the /api/mobile/* library + pack download API.

The interface is pinned in docs/mobile-contract.md. Every route is GET/HEAD;
the mobile gate (lib/mobile_gate.py) is what restricts the standalone mobile
server to this surface. Pack building lives in lib/mobile_pack.py.

Wiring (server.py, next to the other include_router calls)::

    from routers import mobile as mobile_router
    app.include_router(mobile_router.router)

No init call is needed: everything is read through the ``appstate`` seam at
request time.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response

import appstate
import loosefolder as loosefolder_mod
import mobile_pack
import sloppak as sloppak_mod
from dlc_paths import _get_dlc_dir
from mobile_gate import contained_song_root

import logging
log = logging.getLogger("feedBack.mobile")

router = APIRouter()

_GET = ["GET", "HEAD"]
_SONGS_MAX = 20000  # same ceiling as /api/library/table (TABLE_MAX_ROWS)

_BASE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'{ws}; "
    "media-src 'self' blob:; img-src 'self' blob: data:; font-src 'self'; "
    "worker-src 'self'; manifest-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)


# The service worker's CSP governs the worker itself: it only fetches and caches
# same-origin files. The manifest is data and needs nothing.
_SW_CSP = ("default-src 'self'; script-src 'self'; connect-src 'self'; "
           "object-src 'none'; base-uri 'none'")
_MANIFEST_CSP = "default-src 'none'"


def _not_found() -> JSONResponse:
    return JSONResponse({"error": "not found"}, status_code=404)


def _mobile_origin() -> str:
    """FEEDBACK_MOBILE_ORIGIN, normalized to scheme://host[:port], or ""."""
    raw = os.environ.get("FEEDBACK_MOBILE_ORIGIN", "").strip()
    if not raw:
        return ""
    try:
        u = urlparse(raw)
    except ValueError:
        return ""
    if u.scheme not in ("http", "https") or not u.netloc:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", u.netloc):
        return ""
    return f"{u.scheme}://{u.netloc}"


def _csp() -> str:
    # Safari has historically not matched ws:/wss: against 'self', so name the
    # WebSocket form of the mobile origin explicitly.
    origin = _mobile_origin()
    ws = ""
    if origin:
        scheme, rest = origin.split("://", 1)
        ws = f" {'wss' if scheme == 'https' else 'ws'}://{rest}"
    return _BASE_CSP.format(ws=ws)


def _mobile_static() -> Path:
    return Path(appstate.static_dir) / "mobile"


# ── shell ─────────────────────────────────────────────────────────────────────

@router.api_route("/mobile", methods=_GET)
@router.api_route("/mobile/", methods=_GET)
def mobile_index():
    p = _mobile_static() / "index.html"
    if not p.is_file():
        return _not_found()
    return FileResponse(str(p), media_type="text/html; charset=utf-8", headers={
        "Content-Security-Policy": _csp(),
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    })


# The 3D highway renderer the mobile page mounts: vendored three.js (relative to
# static/) and the highway_3d plugin's entry script and stylesheet (relative to
# the plugin dir). Keep in step with the allowlist in lib/mobile_gate.py.
_THREE_STATIC_FILES = (
    "vendor/three/three.module.min.js",
    *(f"vendor/three/addons/postprocessing/{n}.js" for n in (
        "EffectComposer", "RenderPass", "UnrealBloomPass", "OutputPass", "Pass",
        "ShaderPass", "MaskPass")),
    *(f"vendor/three/addons/shaders/{n}.js" for n in (
        "CopyShader", "LuminosityHighPassShader", "OutputShader")),
)
_HIGHWAY_3D_FILES = ("screen.js", "assets/plugin.css")


def _highway_3d_dir() -> Path:
    """The directory /api/plugins/highway_3d/* is served from: the loaded
    plugin's own dir (a user plugins dir can shadow the bundled one), else the
    bundled plugins/highway_3d."""
    import plugins as plugins_mod
    with plugins_mod.PLUGINS_LOCK:
        loaded = list(plugins_mod.LOADED_PLUGINS)
    for p in loaded:
        if p.get("id") == "highway_3d" and p.get("_dir"):
            return Path(p["_dir"])
    return plugins_mod.PLUGINS_DIR / "highway_3d"


def _precache_hash() -> str:
    """Hash over every file the service worker precaches: the mobile app's own
    files, the shared highway renderer and the 3D highway renderer."""
    static = Path(appstate.static_dir)
    named: list[tuple[str, Path]] = []
    mob = _mobile_static()
    if mob.is_dir():
        named += [(p.relative_to(static).as_posix(), p) for p in mob.rglob("*") if p.is_file()]
    hw = static / "highway.js"
    if hw.is_file():
        named.append(("highway.js", hw))
    js = static / "js"
    if js.is_dir():
        named += [(p.relative_to(static).as_posix(), p) for p in js.glob("highway-*.js") if p.is_file()]
    named += [(rel, static / rel) for rel in _THREE_STATIC_FILES]
    try:
        h3d = _highway_3d_dir()
        named += [(f"plugins/highway_3d/{rel}", h3d / rel) for rel in _HIGHWAY_3D_FILES]
    except Exception:
        log.debug("highway_3d dir lookup failed", exc_info=True)
    h = hashlib.sha1()
    for name, p in sorted(set(named)):
        try:
            data = p.read_bytes()
        except OSError:
            continue
        h.update(name.encode("utf-8") + b"\0")
        h.update(data)
        h.update(b"\0")
    return h.hexdigest()[:16]


def _app_version() -> str:
    fn = appstate.running_version
    try:
        return str(fn()) if callable(fn) else "unknown"
    except Exception:
        return "unknown"


@router.api_route("/mobile/sw.js", methods=_GET)
def mobile_sw():
    p = _mobile_static() / "sw.js"
    if not p.is_file():
        return _not_found()
    version = f"{_app_version()}-{_precache_hash()}"
    body = p.read_text(encoding="utf-8").replace("__FBM_CACHE_VERSION__", version)
    return Response(body, media_type="application/javascript", headers={
        "Cache-Control": "no-cache",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": _SW_CSP,
    })


@router.api_route("/mobile/manifest.webmanifest", methods=_GET)
def mobile_manifest():
    p = _mobile_static() / "manifest.webmanifest"
    if not p.is_file():
        return _not_found()
    return FileResponse(str(p), media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache",
                                 "Content-Security-Policy": _MANIFEST_CSP})


# ── API ───────────────────────────────────────────────────────────────────────

@router.api_route("/api/mobile/info", methods=_GET)
def mobile_info(request: Request):
    return {
        "version": _app_version(),
        "origin": _mobile_origin(),
        "user": request.headers.get("tailscale-user-login") or None,
    }


def _canonical_song_path(dlc: Path, song_id: str) -> Path | None:
    """The link-resolved path for `song_id`, or None unless it lies inside the
    resolved library AND `song_id` is already its canonical spelling (the path
    relative to the resolved library). One song, one id: aliases such as
    ``a/../x``, ``x/`` or an in-library symlink would otherwise each build and
    cache their own copy of the same pack, and a symlink that leaves the
    library would let a song read files outside it."""
    real = contained_song_root(dlc, song_id)
    if real is None:
        return None
    try:
        canonical = real.relative_to(dlc.resolve()).as_posix()
    except ValueError:
        return None
    return real if canonical == song_id else None


def _resolve_song(song_id: str) -> Path | None:
    """Validated on-disk path for a playable song id, or None."""
    dlc = _get_dlc_dir()
    if not dlc:
        return None
    p = _canonical_song_path(dlc, song_id)
    if p is None or not p.exists():
        return None
    if sloppak_mod.is_sloppak(p):
        return p
    if p.is_dir() and loosefolder_mod.is_loose_song(p):
        return p
    return None


def _list_songs() -> list[dict]:
    """The library rows (same query_page call /api/library/table makes),
    filtered to playable songs and shaped per the contract."""
    rows, _total = appstate.meta_db.query_page(
        page=0, size=_SONGS_MAX, sort="artist", direction="asc")
    dlc = _get_dlc_dir()
    if not dlc:
        return []
    out = []
    for r in rows:
        sid = r.get("filename") or ""
        if r.get("format") not in ("sloppak", "loose"):
            continue
        p = _canonical_song_path(dlc, sid)
        if p is None or not p.exists():
            continue
        if not (sloppak_mod.is_sloppak(p) or p.is_dir()):
            continue
        try:
            version, src_size = mobile_pack.version_for_cached(sid, p)
        except OSError:
            continue
        cached = mobile_pack.cached_pack(sid, version)
        if cached is not None:
            size, estimate = cached.stat().st_size, False
        else:
            size, estimate = src_size, True
        arrs = [a for a in (r.get("arrangements") or []) if isinstance(a, dict)]
        out.append({
            "id": sid,
            "title": r.get("title") or "",
            "artist": r.get("artist") or "",
            "album": r.get("album") or "",
            "duration": float(r.get("duration") or 0),
            "size_bytes": size,
            "size_is_estimate": estimate,
            "version": version,
            "has_stems": bool(r.get("stem_count") or r.get("stem_ids")),
            "arrangements": [a.get("name") or "" for a in arrs
                             if not mobile_pack._is_drums(a.get("name") or "")],
        })
    return out


@router.api_route("/api/mobile/songs", methods=_GET)
async def mobile_songs():
    songs = await run_in_threadpool(_list_songs)
    return {"songs": songs, "total": len(songs)}


@router.api_route("/api/mobile/songs/{song_id:path}/status", methods=_GET)
async def mobile_song_status(song_id: str):
    p = _resolve_song(song_id)
    if p is None:
        return _not_found()
    try:
        version, _ = await run_in_threadpool(mobile_pack.version_for, song_id, p)
    except OSError:
        return _not_found()
    return mobile_pack.get_status(song_id, version)


def _safe_download_name(song_id: str) -> str:
    stem = Path(song_id.replace("\\", "/").rstrip("/")).name
    for suffix in (".feedpak", ".sloppak"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:80]
    return safe or "song"


@router.api_route("/api/mobile/songs/{song_id:path}/download", methods=_GET)
async def mobile_song_download(song_id: str):
    p = _resolve_song(song_id)
    if p is None:
        return _not_found()
    try:
        version, _ = await run_in_threadpool(mobile_pack.version_for, song_id, p)
    except OSError:
        return _not_found()
    try:
        pack = await mobile_pack.ensure_pack(song_id, version)
    except mobile_pack.BuildBusy:
        return JSONResponse({"error": "busy"}, status_code=503,
                            headers={"Retry-After": "30"})
    except mobile_pack.PackError as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return FileResponse(str(pack), media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{_safe_download_name(song_id)}.fbm"',
        "ETag": f'"{version}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    })
