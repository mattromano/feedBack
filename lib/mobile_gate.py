"""Mobile-mode gate: the allowlist that stands between `tailscale serve` and the app.

Active only when FEEDBACK_MOBILE_MODE=1 (see docs/mobile-offline-plan.md section 9.2 and
docs/mobile-contract.md). It is a pure ASGI middleware, not ``@app.middleware("http")``,
because an http middleware never sees WebSocket upgrades.

Checks, in order, for every http and websocket scope:

1. ``Host`` is the serve hostname, ``127.0.0.1:<PORT>``/``localhost:<PORT>`` or an extra
   configured host (blocks DNS rebinding)                                      -> 400
2. the raw and decoded path hold no ``..``/``.`` segment, NUL or backslash     -> 400
3. exactly one ``Tailscale-User-Login`` header, on the allowlist                -> 403
4. ``Origin``, when present, equals the configured origin exactly, and
   ``Sec-Fetch-Site``, when present, is ``same-origin``, or ``none`` only for a
   top-level ``document`` navigation to the app entry ``/mobile[/]``          -> 403
5. per-user token-bucket rate limit                                             -> 429
6. method is GET or HEAD                                                        -> 405
7. path matches the anchored allowlist                                          -> 404
8. song-path containment (filesystem, at request time) for the routes that take
   a song id: the id, with every symlink followed, must stay inside the
   resolved DLC dir; for ``/api/sloppak/{id}/file/{rel}`` on a directory-form
   package the member must also stay inside the resolved package            -> 404
9. at most 2 concurrent ``/ws/highway`` sockets per user, 4 in total            -> close 1013

A rejected WebSocket is closed before ``accept`` (uvicorn answers the handshake with 403).
Every response gets ``X-Content-Type-Options: nosniff``, ``Referrer-Policy: no-referrer``,
``Cache-Control: private`` unless the app set one, and
``Content-Security-Policy: default-src 'none'; sandbox`` unless the app set one (the
/mobile shell routes set their own). On ``/ws/highway``, outgoing ``{"error": ...}``
frames and ``song_info.audio_error`` are replaced with generic text, because core
puts ``str(exception)`` (which can hold absolute paths) in them.

Why containment (8) lives here: core's ``_resolve_dlc_path`` is deliberately lexical
(in-library junctions are a supported desktop setup), so a song package that is
itself a symlink (``B.feedpak -> ../..``) passes core's checks and turns
``/api/sloppak/B.feedpak/file/<anything>`` into a read of the link target. The mobile
surface does not support songs reached through a symlink that leaves the library;
desktop behaviour is unchanged. For a zip-form package core serves from its unpack
cache (zip-slip guarded, symlink entries extracted as plain files), so the gate only
needs to prove the zip itself is inside the library.
"""
from __future__ import annotations

import ipaddress
import os
import re
import json
import time
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit

# The exact transitive import set of static/highway.js (static `import ... from`,
# `export ... from` and dynamic `import('...')`), as URL paths. Hardcoded on purpose: the
# gate allows these files and nothing else under /static outside /static/mobile/.
# tests/test_mobile_gate.py recomputes the set from disk and fails if it drifts.
HIGHWAY_STATIC_FILES: tuple[str, ...] = (
    "/static/highway.js",
    "/static/js/highway-av-offset.js",
    "/static/js/highway-constants.js",
    "/static/js/highway-draw.js",
    "/static/js/highway-geometry.js",
    "/static/js/highway-state-primitives.js",
)

# Song-package members the player streams: audio and cover images. Anything else
# (HTML, SVG, JSON, YAML, ...) stays unreachable, so a hostile package cannot get a
# document rendered same-origin.
SLOPPAK_MEMBER_EXTS: tuple[str, ...] = (
    "ogg", "opus", "oga", "mp3", "m4a", "aac", "flac", "wav",
    "jpg", "jpeg", "png", "webp",
)
_MEMBER_EXT_RE = "|".join(SLOPPAK_MEMBER_EXTS)

_SEG = r"[^/]+"
# Anchored allowlists, matched against the decoded scope["path"].
HTTP_ALLOW: tuple[re.Pattern[str], ...] = (
    re.compile(r"^/mobile/?$"),
    re.compile(r"^/mobile/.+$"),
    re.compile(r"^/api/mobile/.+$"),
    re.compile(r"^/static/mobile/.+$"),
    *(re.compile("^" + re.escape(p) + "$") for p in HIGHWAY_STATIC_FILES),
    # 3D highway renderer (plugins/highway_3d/screen.js mounted on the mobile page):
    # the vendored three.js build, its postprocessing/shader addons, the plugin's
    # entry script and stylesheet. Nothing else under /api/plugins.
    re.compile(r"^/static/vendor/three/three\.module\.min\.js$"),
    re.compile(r"^/static/vendor/three/addons/[A-Za-z0-9_/-]+\.js$"),
    re.compile(r"^/api/plugins/highway_3d/screen\.js$"),
    re.compile(r"^/api/plugins/highway_3d/assets/plugin\.css$"),
    re.compile(rf"^/api/sloppak/.+/file/.+\.(?i:{_MEMBER_EXT_RE})$"),
    # ws_highway names cached audio /audio/audio_{audio_id}{ext} with ext from
    # convert_wem (.mp3/.ogg/.wav). audio_id is a filename stem (any character but
    # '/'), so match one segment with that exact prefix and suffix.
    re.compile(r"^/audio/audio_[^/]+\.(?:mp3|ogg|wav)$"),
    re.compile(rf"^/api/song/{_SEG}(?:/{_SEG})*/art$"),
)
WS_ALLOW: tuple[re.Pattern[str], ...] = (re.compile(r"^/ws/highway/.+$"),)
WS_LIMITED = re.compile(r"^/ws/highway/")

# The same splits Starlette makes for these routes ({x:path} compiles to a greedy
# ".*"), so the gate checks exactly the id and member the handler will see.
_SLOPPAK_ROUTE = re.compile(r"^/api/sloppak/(?P<fn>.*)/file/(?P<rel>.*)$")
_ART_ROUTE = re.compile(r"^/api/song/(?P<fn>.*)/art$")
_WS_HIGHWAY_ROUTE = re.compile(r"^/ws/highway/(?P<fn>.*)$")

MAX_HIGHWAY_SOCKETS = 2          # per user
MAX_HIGHWAY_SOCKETS_TOTAL = 4    # across all users
RATE_PER_SEC = 20.0
RATE_BURST = 60.0

_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)
SANDBOX_CSP = b"default-src 'none'; sandbox"
_APP_ENTRY = re.compile(r"^/mobile/?$")
_WS_ERROR_TEXT = "Could not load song"
_WS_AUDIO_ERROR_TEXT = "Audio unavailable"
_ORIGIN_RE = re.compile(r"^(https?)://([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::(\d{1,5}))?$")


def is_allowed_path(path: str, scope_type: str = "http") -> bool:
    patterns = WS_ALLOW if scope_type == "websocket" else HTTP_ALLOW
    return any(p.match(path) for p in patterns)


# ── song-path containment (shared with mobile_pack / routers.mobile) ─────────

def _dlc_dir() -> Path | None:
    """The library dir, through the same helper core routes use, or None."""
    try:
        from dlc_paths import _get_dlc_dir
        return _get_dlc_dir()
    except Exception:
        # Unconfigured app state: core's own _get_dlc_dir call fails the same way,
        # so the route cannot serve library files either.
        return None


def contained_song_root(dlc: Path, song_id: str) -> Path | None:
    """The fully link-resolved path of ``song_id`` if it stays strictly inside the
    resolved DLC dir after following every symlink, else None."""
    from dlc_paths import _resolve_dlc_path
    cand = _resolve_dlc_path(dlc, song_id)
    if cand is None:
        return None
    try:
        root = dlc.resolve()
        real = cand.resolve()
    except (OSError, RuntimeError):
        return None
    if real == root or not real.is_relative_to(root):
        return None
    return real


def contained_member(pkg_real: Path, rel: str) -> Path | None:
    """For a directory-form package (already link-resolved), the resolved member
    path if it stays inside the package, else None."""
    try:
        target = (pkg_real / rel).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return target if target.is_relative_to(pkg_real) else None


def _song_paths_ok(path: str, kind: str) -> bool:
    """Containment for the allowlisted routes that take a song id (see module doc)."""
    if kind == "websocket":
        m = _WS_HIGHWAY_ROUTE.match(path)
        fn, rel = (m.group("fn"), None) if m else (None, None)
    else:
        m = _SLOPPAK_ROUTE.match(path)
        if m:
            fn, rel = m.group("fn"), m.group("rel")
        else:
            m = _ART_ROUTE.match(path)
            fn, rel = (m.group("fn"), None) if m else (None, None)
    if fn is None:
        return True   # not a song-id route
    dlc = _dlc_dir()
    if dlc is None:
        return True   # route answers "not configured" itself; nothing to read
    real = contained_song_root(dlc, fn)
    if real is None:
        return False
    if rel is not None and real.is_dir() and contained_member(real, rel) is None:
        return False
    return True


def _scrub_ws_text(text: str) -> str:
    """Replace free-text error fields core fills with str(exception)."""
    # Cheap substring pre-check: note frames are large and never carry these keys.
    if '"error"' not in text and '"audio_error"' not in text:
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, dict):
        return text
    changed = False
    if "error" in data:
        data["error"] = _WS_ERROR_TEXT
        changed = True
    if data.get("type") == "song_info" and data.get("audio_error"):
        data["audio_error"] = _WS_AUDIO_ERROR_TEXT
        changed = True
    return json.dumps(data) if changed else text


def _bad_path(*candidates: str) -> bool:
    """True if any candidate, decoded up to 3 times, has a dot segment, NUL or backslash."""
    for value in candidates:
        for _ in range(4):
            if "\x00" in value or "\\" in value:
                return True
            if any(seg in ("..", ".") for seg in value.split("/")):
                return True
            decoded = unquote(value)
            if decoded == value:
                break
            value = decoded
    return False


def normalize_origin(origin: str) -> str:
    """Return ``scheme://host[:port]`` lowercased with default ports dropped, or raise."""
    m = _ORIGIN_RE.match((origin or "").strip().lower())
    if not m:
        raise ValueError("origin must be http(s)://host[:port] with no path")
    scheme, host, port = m.groups()
    if port is not None:
        if not 0 < int(port) < 65536:
            raise ValueError("origin port out of range")
        if (scheme, port) in (("https", "443"), ("http", "80")):
            port = None
    return f"{scheme}://{host}" + (f":{port}" if port else "")


class MobileGate:
    def __init__(
        self,
        app,
        *,
        origin: str,
        allowed_users: Iterable[str],
        extra_hosts: Iterable[str] = (),
        port: int | None = None,
        rate: float = RATE_PER_SEC,
        burst: float = RATE_BURST,
        max_highway_sockets: int = MAX_HIGHWAY_SOCKETS,
        max_highway_sockets_total: int = MAX_HIGHWAY_SOCKETS_TOTAL,
        song_paths_ok: Callable[[str, str], bool] = _song_paths_ok,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.origin = normalize_origin(origin)
        self.allowed_users = {u.strip().casefold() for u in allowed_users if u.strip()}
        if not self.allowed_users:
            raise ValueError("allowed_users is empty")
        parts = urlsplit(self.origin)
        hosts = {parts.netloc}
        if parts.port is None:
            hosts.add(f"{parts.hostname}:{443 if parts.scheme == 'https' else 80}")
        if port is not None:
            hosts.update({f"127.0.0.1:{port}", f"localhost:{port}"})
        hosts.update(h.strip().lower() for h in extra_hosts if h.strip())
        self.allowed_hosts = frozenset(hosts)
        self.rate = float(rate)
        self.burst = float(burst)
        self.max_highway_sockets = max_highway_sockets
        self.max_highway_sockets_total = max_highway_sockets_total
        self._song_paths_ok = song_paths_ok
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._highway_sockets: dict[str, int] = {}

    # ── helpers ──────────────────────────────────────────────────────────────

    def _take_token(self, user: str) -> bool:
        now = self._clock()
        tokens, last = self._buckets.get(user, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1.0:
            self._buckets[user] = (tokens, now)
            return False
        self._buckets[user] = (tokens - 1.0, now)
        return True

    def _check(self, scope) -> tuple[tuple[int, str] | None, str]:
        """Return ((status, text) or None if the request may proceed, user)."""
        headers: dict[str, list[str]] = {}
        for k, v in scope.get("headers") or ():
            headers.setdefault(k.decode("latin-1").lower(), []).append(v.decode("latin-1"))

        def one(name: str) -> str | None:
            vals = headers.get(name)
            if vals is None:
                return None
            return vals[0] if len(vals) == 1 else "\x00"   # duplicates never match

        host = one("host")
        if host is None or host.lower() not in self.allowed_hosts:
            return (400, "Bad Request"), ""

        path = scope.get("path") or ""
        raw = scope.get("raw_path") or b""
        if _bad_path(path, raw.decode("latin-1")):
            return (400, "Bad Request"), ""

        user = one("tailscale-user-login")
        if user is None or user.strip().casefold() not in self.allowed_users:
            return (403, "Forbidden"), ""
        user = user.strip().casefold()

        origin = one("origin")
        if "origin" in headers and origin != self.origin:
            return (403, "Forbidden"), user
        if "sec-fetch-site" in headers:
            site = (one("sec-fetch-site") or "").lower()
            if site == "none":
                # User-initiated (typed URL, bookmark, home-screen launch): only the
                # app entry as a top-level document. Everything else the app loads is
                # same-origin; a "none" subresource or a typed deep link is refused.
                dest = (one("sec-fetch-dest") or "").lower()
                if not (scope["type"] == "http" and dest == "document" and _APP_ENTRY.match(path)):
                    return (403, "Forbidden"), user
            elif site != "same-origin":
                return (403, "Forbidden"), user
        # An absent Sec-Fetch-Site (old Safari, curl) is allowed: the identity, Origin
        # and Host checks still hold, and responses are CSP-sandboxed.

        if not self._take_token(user):
            return (429, "Too Many Requests"), user

        if scope["type"] == "http" and scope.get("method") not in ("GET", "HEAD"):
            return (405, "Method Not Allowed"), user

        if not is_allowed_path(path, scope["type"]):
            return (404, "Not Found"), user
        if not self._song_paths_ok(path, scope["type"]):
            return (404, "Not Found"), user
        return None, user

    @staticmethod
    async def _reject_http(scope, send, status: int, text: str) -> None:
        body = text.encode()
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store"),
            (b"content-security-policy", SANDBOX_CSP),
            *_SECURITY_HEADERS,
        ]
        if status == 405:
            headers.append((b"allow", b"GET, HEAD"))
        if status == 429:
            headers.append((b"retry-after", b"1"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({
            "type": "http.response.body",
            "body": b"" if scope.get("method") == "HEAD" else body,
        })

    @staticmethod
    async def _reject_ws(receive, send, code: int) -> None:
        # Consume websocket.connect, then close before accept: the server answers 403.
        msg = await receive()
        if msg.get("type") == "websocket.connect":
            await send({"type": "websocket.close", "code": code})

    # ── ASGI entry ───────────────────────────────────────────────────────────

    async def __call__(self, scope, receive, send) -> None:
        kind = scope["type"]
        if kind == "lifespan":
            await self.app(scope, receive, send)
            return
        if kind not in ("http", "websocket"):
            return   # unknown scope type: fail closed

        rejection, user = self._check(scope)
        if kind == "http":
            if rejection:
                await self._reject_http(scope, send, *rejection)
                return

            async def send_with_headers(message):
                if message["type"] == "http.response.start":
                    hdrs = [
                        (k, v) for k, v in message.get("headers") or ()
                        if k.lower() not in (b"x-content-type-options", b"referrer-policy")
                    ]
                    hdrs.extend(_SECURITY_HEADERS)
                    if not any(k.lower() == b"cache-control" for k, _ in hdrs):
                        hdrs.append((b"cache-control", b"private"))
                    if not any(k.lower() == b"content-security-policy" for k, _ in hdrs):
                        hdrs.append((b"content-security-policy", SANDBOX_CSP))
                    message = {**message, "headers": hdrs}
                await send(message)

            await self.app(scope, receive, send_with_headers)
            return

        # websocket
        if rejection:
            await self._reject_ws(receive, send, 1008 if rejection[0] != 429 else 1013)
            return
        limited = bool(WS_LIMITED.match(scope.get("path") or ""))
        if not limited:
            await self.app(scope, receive, send)
            return
        mine = self._highway_sockets.get(user, 0)
        if (mine >= self.max_highway_sockets
                or sum(self._highway_sockets.values()) >= self.max_highway_sockets_total):
            await self._reject_ws(receive, send, 1013)
            return
        self._highway_sockets[user] = mine + 1

        async def send_scrubbed(message):
            if message.get("type") == "websocket.send" and isinstance(message.get("text"), str):
                text = _scrub_ws_text(message["text"])
                if text is not message["text"]:
                    message = {**message, "text": text}
            await send(message)

        try:
            await self.app(scope, receive, send_scrubbed)
        finally:
            left = self._highway_sockets.get(user, 1) - 1
            if left > 0:
                self._highway_sockets[user] = left
            else:
                self._highway_sockets.pop(user, None)


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def install_mobile_gate(app) -> None:
    """Wrap `app` in MobileGate when FEEDBACK_MOBILE_MODE=1; otherwise do nothing.

    Fails closed: a non-loopback HOST, a malformed or empty origin, or an empty user
    allowlist raises RuntimeError so the server never starts half-gated. Must be called
    after every other add_middleware call — Starlette makes the last-added middleware the
    outermost user middleware, so the gate wraps CorrelationIdMiddleware, the demo guard,
    the plugin prefix rewriter and every route.
    """
    if os.environ.get("FEEDBACK_MOBILE_MODE", "").strip() != "1":
        return
    host = os.environ.get("HOST", "0.0.0.0").strip()
    if not _is_loopback_host(host):
        raise RuntimeError("FEEDBACK_MOBILE_MODE=1 requires HOST to be a loopback address")
    try:
        port = int(os.environ.get("PORT", "8000"))
    except ValueError as exc:
        raise RuntimeError("FEEDBACK_MOBILE_MODE=1: PORT is not a number") from exc
    try:
        origin = normalize_origin(os.environ.get("FEEDBACK_MOBILE_ORIGIN", ""))
    except ValueError as exc:
        raise RuntimeError(f"FEEDBACK_MOBILE_MODE=1: FEEDBACK_MOBILE_ORIGIN invalid ({exc})") from exc
    users = [u.strip() for u in os.environ.get("FEEDBACK_MOBILE_ALLOWED_USERS", "").split(",") if u.strip()]
    if not users:
        raise RuntimeError("FEEDBACK_MOBILE_MODE=1 requires FEEDBACK_MOBILE_ALLOWED_USERS")
    extra = [h.strip() for h in os.environ.get("FEEDBACK_MOBILE_HOSTS", "").split(",") if h.strip()]
    app.add_middleware(
        MobileGate, origin=origin, allowed_users=users, extra_hosts=extra, port=port,
    )
