"""Tests for lib/mobile_gate.py — the mobile-mode allowlist gate (plan section 9.2/9.3)."""

import asyncio
import importlib
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.routing import Mount
from starlette.websockets import WebSocketDisconnect

import mobile_gate
from mobile_gate import HIGHWAY_STATIC_FILES, MobileGate, install_mobile_gate, is_allowed_path

ROOT = Path(__file__).resolve().parent.parent
ORIGIN = "https://mbp-home.tail1234.ts.net"
USER = "Owner@Example.com"
GOOD = {"Tailscale-User-Login": "owner@example.com", "Origin": ORIGIN}
BASE_URL = "http://127.0.0.1:8765"
WS_URL = "ws://127.0.0.1:8765"


def _mobile_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("FEEDBACK_SYNC_STARTUP", "1")
    monkeypatch.setenv("FEEDBACK_MOBILE_MODE", "1")
    monkeypatch.setenv("FEEDBACK_MOBILE_ORIGIN", ORIGIN)
    monkeypatch.setenv("FEEDBACK_MOBILE_ALLOWED_USERS", f" {USER} ,other@example.com")
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "8765")
    monkeypatch.delenv("FEEDBACK_MOBILE_HOSTS", raising=False)


def _import_server(monkeypatch):
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    monkeypatch.setattr(server, "load_plugins", lambda *a, **kw: None)
    monkeypatch.setattr(server, "startup_scan", lambda: None)
    return server


def _close(server):
    meta_db = getattr(server, "meta_db", None)
    conn = getattr(meta_db, "conn", None)
    if conn is not None:
        getattr(server, "_join_background_db_threads", lambda: None)()
        conn.close()
    # Never leave a gated server behind for other test modules.
    sys.modules.pop("server", None)


@pytest.fixture()
def gated(tmp_path, monkeypatch):
    _mobile_env(monkeypatch, tmp_path)
    server = _import_server(monkeypatch)
    gate_mw = server.app.user_middleware[0]
    assert gate_mw.cls is MobileGate
    gate_mw.kwargs["burst"] = 1e9   # the route sweep sends thousands of requests
    tc = TestClient(server.app, base_url=BASE_URL, client=("127.0.0.1", 50000))
    try:
        yield tc, server
    finally:
        tc.close()
        _close(server)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _flat_routes(routes):
    for route in routes:
        # FastAPI >= 0.15x keeps include_router() children behind an _IncludedRouter;
        # its effective contexts carry the prefixed path (on starlette_route for
        # plain Route / WebSocket routes).
        if hasattr(route, "effective_route_contexts"):
            ctxs = list(route.effective_route_contexts())
            ctxs += list(getattr(route, "effective_low_priority_routes", list)())
            for ctx in ctxs:
                path = getattr(ctx, "path", "") or getattr(ctx.starlette_route, "path", "")
                yield ctx.original_route, path
        else:
            yield route, getattr(route, "path", None)


def _route_paths(app):
    for route, path in _flat_routes(app.routes):
        if not path:
            continue
        if isinstance(route, Mount):
            path = path.rstrip("/") + "/x"
        yield route, _concrete(path)


# ── (a) generated from app.routes ────────────────────────────────────────────

def test_every_non_allowlisted_route_is_404(gated):
    tc, server = gated
    checked = 0
    for _route, path in _route_paths(server.app):
        if is_allowed_path(path, "http"):
            continue
        r = tc.get(path, headers=GOOD)
        assert r.status_code == 404, (path, r.status_code)
        assert r.text == "Not Found"
        checked += 1
    assert checked > 100
    for path in ("/", "/v3", "/api/settings", "/api/songs/upload", "/pane", "/ws/highway/x",
                 "/static/app.js", "/static/v3/index.html", "/api/song/x/art/candidates",
                 "/api/plugins", "/docs", "/openapi.json"):
        assert tc.get(path, headers=GOOD).status_code == 404, path


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_every_route_rejects_non_get_methods(gated, method):
    tc, server = gated
    paths = {p for _r, p in _route_paths(server.app)} | {"/mobile", "/api/mobile/songs"}
    for path in sorted(paths):
        r = tc.request(method, path, headers=GOOD)
        assert r.status_code == 405, (method, path, r.status_code)
        assert r.headers["allow"] == "GET, HEAD"


# Concrete values that satisfy the tightened allowlist for routes whose params it
# constrains (member extension, cached-audio name, the one allowed plugin).
_ALLOWED_PARAMS = {"{rel_path:path}": "x.ogg", "{plugin_id}": "highway_3d",
                   "{asset_path:path}": "plugin.css"}


def _concrete_allowed(path: str) -> str:
    if path == "/audio/{filename:path}":
        return "/audio/audio_x.ogg"
    for k, v in _ALLOWED_PARAMS.items():
        path = path.replace(k, v)
    return _concrete(path)


def test_allowlisted_routes_are_reviewed(gated):
    """Every real route the allowlist lets through is one the contract names."""
    _tc, server = gated
    templates = {p for _r, p in _flat_routes(server.app.routes) if p}
    candidates = {_concrete(p) for p in templates} | {_concrete_allowed(p) for p in templates}
    allowed = sorted(p for p in candidates if is_allowed_path(p, "http"))
    core = {"/api/sloppak/x/file/x.ogg", "/api/song/x/art", "/audio/audio_x.ogg",
            "/api/plugins/highway_3d/screen.js", "/api/plugins/highway_3d/assets/plugin.css"}
    # lib/routers/mobile.py's routes (docs/mobile-contract.md), once it is included.
    mobile = {"/mobile", "/mobile/", "/mobile/sw.js", "/mobile/manifest.webmanifest",
              "/api/mobile/info", "/api/mobile/songs", "/api/mobile/songs/x/status",
              "/api/mobile/songs/x/download"}
    assert core <= set(allowed)
    assert set(allowed) <= core | mobile, sorted(set(allowed) - core - mobile)
    ws = sorted({p for _r, p in _route_paths(server.app) if is_allowed_path(p, "websocket")})
    assert ws == ["/ws/highway/x"]


# ── (b) the allowlisted static files load ────────────────────────────────────

def test_highway_static_is_served_with_headers(gated):
    tc, _ = gated
    for path in HIGHWAY_STATIC_FILES:
        r = tc.get(path, headers=GOOD)
        assert r.status_code == 200, path
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"
        assert r.headers["cache-control"] == "no-cache"   # app's value is kept
    r = tc.head("/static/highway.js", headers=GOOD)
    assert r.status_code == 200


def test_cache_control_private_added_when_absent(gated):
    tc, server = gated
    (server.AUDIO_CACHE_DIR).mkdir(parents=True, exist_ok=True)
    (server.AUDIO_CACHE_DIR / "audio_gate_test.ogg").write_bytes(b"OggS")
    r = tc.get("/audio/audio_gate_test.ogg", headers=GOOD)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "private"


# ── (c) identity ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("headers", [
    {"Origin": ORIGIN},
    {"Origin": ORIGIN, "Tailscale-User-Login": "intruder@example.com"},
    {"Origin": ORIGIN, "Tailscale-User-Login": ""},
])
def test_identity_required(gated, headers):
    tc, _ = gated
    r = tc.get("/static/highway.js", headers=headers)
    assert r.status_code == 403
    assert r.text == "Forbidden"


def test_identity_case_insensitive_and_no_origin_ok(gated):
    tc, _ = gated
    r = tc.get("/static/highway.js", headers={"Tailscale-User-Login": "OWNER@example.COM"})
    assert r.status_code == 200


def test_duplicate_identity_header_rejected(gated):
    tc, _ = gated
    r = tc.get("/static/highway.js", headers=[
        ("Tailscale-User-Login", "intruder@example.com"),
        ("Tailscale-User-Login", "owner@example.com"),
    ])
    assert r.status_code == 403


# ── (d) Origin, Sec-Fetch-Site, Host ─────────────────────────────────────────

@pytest.mark.parametrize("extra", [
    {"Origin": "https://evil.example"},
    {"Origin": "null"},
    {"Origin": ORIGIN + ":444"},
    {"Origin": "http://mbp-home.tail1234.ts.net"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "same-site"},
])
def test_origin_and_fetch_site(gated, extra):
    tc, _ = gated
    r = tc.get("/static/highway.js", headers={**GOOD, **extra})
    assert r.status_code == 403


def test_fetch_site_same_origin_ok(gated):
    tc, _ = gated
    assert tc.get("/static/highway.js", headers={**GOOD, "Sec-Fetch-Site": "same-origin"}).status_code == 200
    # "none" is only for launching the app entry (see test_fetch_site_none_only_for_app_entry).
    assert tc.get("/static/highway.js", headers={**GOOD, "Sec-Fetch-Site": "none"}).status_code == 403


@pytest.mark.parametrize("host", ["evil.example", "127.0.0.1:9999", "127.0.0.1", "mbp-home.tail1234.ts.net.evil"])
def test_bad_host(gated, host):
    tc, _ = gated
    r = tc.get("/static/highway.js", headers={**GOOD, "Host": host})
    assert r.status_code == 400


@pytest.mark.parametrize("host", ["mbp-home.tail1234.ts.net", "mbp-home.tail1234.ts.net:443", "localhost:8765"])
def test_good_hosts(gated, host):
    tc, _ = gated
    assert tc.get("/static/highway.js", headers={**GOOD, "Host": host}).status_code == 200


# ── (e) WebSocket ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("headers", [
    {**GOOD, "Origin": "https://evil.example"},
    {"Origin": ORIGIN},
    {**GOOD, "Tailscale-User-Login": "intruder@example.com"},
])
def test_ws_highway_rejected_without_good_headers(gated, headers):
    tc, _ = gated
    with pytest.raises(WebSocketDisconnect) as exc:
        with tc.websocket_connect(WS_URL + "/ws/highway/x", headers=dict(headers)):
            pass
    assert exc.value.code == 1008


def test_ws_highway_bad_host_rejected(gated):
    tc, _ = gated
    with pytest.raises(WebSocketDisconnect) as exc:
        with tc.websocket_connect("ws://evil.example/ws/highway/x", headers=dict(GOOD)):
            pass
    assert exc.value.code == 1008


def test_ws_sync_not_allowlisted(gated):
    tc, _ = gated
    with pytest.raises(WebSocketDisconnect) as exc:
        with tc.websocket_connect(WS_URL + "/ws/sync", headers=dict(GOOD)):
            pass
    assert exc.value.code == 1008


def test_ws_highway_allowed_with_good_headers(gated):
    tc, _ = gated
    with tc.websocket_connect(WS_URL + "/ws/highway/does-not-exist.sloppak", headers=dict(GOOD)) as ws:
        msg = ws.receive_json()
    assert isinstance(msg, dict)


# ── (f) traversal ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "/static/../server.py",
    "/static/%2e%2e/server.py",
    "/static/..%2fserver.py",
    "/static/mobile/..%2F..%2Fserver.py",
    "/audio/%252e%252e/etc/passwd",
    "/api/sloppak/..%2F..%2Fetc/file/passwd",
    "/api/sloppak/x/file/a%5c..%5cb",
    "/audio/a%00b",
    "/static/mobile/./x.js",
])
def test_traversal_rejected(gated, raw):
    _tc, server = gated
    # Drive the ASGI app directly: httpx would normalize the dot segments away.
    from urllib.parse import unquote
    scope = {
        "type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": unquote(raw), "raw_path": raw.encode("ascii"), "query_string": b"",
        "root_path": "", "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", 8765),
        "headers": [(b"host", b"127.0.0.1:8765")]
        + [(k.lower().encode(), v.encode()) for k, v in GOOD.items()],
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(m):
        sent.append(m)

    asyncio.run(server.app(scope, receive, send))
    assert sent[0]["status"] == 400, (raw, sent[0]["status"])


# ── (g) fail-closed startup ──────────────────────────────────────────────────

class _FakeApp:
    def __init__(self):
        self.added = []

    def add_middleware(self, cls, **kw):
        self.added.append((cls, kw))


@pytest.mark.parametrize("env", [
    {"HOST": "0.0.0.0"},
    {"HOST": "192.168.1.5"},
    {"HOST": None},
    {"FEEDBACK_MOBILE_ALLOWED_USERS": " , "},
    {"FEEDBACK_MOBILE_ALLOWED_USERS": None},
    {"FEEDBACK_MOBILE_ORIGIN": ""},
    {"FEEDBACK_MOBILE_ORIGIN": "https://mbp-home.tail1234.ts.net/mobile"},
    {"FEEDBACK_MOBILE_ORIGIN": "ftp://mbp-home"},
    {"FEEDBACK_MOBILE_ORIGIN": "https://user@host"},
    {"PORT": "abc"},
])
def test_install_fails_closed(monkeypatch, tmp_path, env):
    _mobile_env(monkeypatch, tmp_path)
    for k, v in env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    app = _FakeApp()
    with pytest.raises(RuntimeError):
        install_mobile_gate(app)
    assert app.added == []


def test_install_valid_config(monkeypatch, tmp_path):
    _mobile_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FEEDBACK_MOBILE_HOSTS", "mac.local:8765, ")
    app = _FakeApp()
    install_mobile_gate(app)
    (cls, kw), = app.added
    assert cls is MobileGate
    assert kw["origin"] == ORIGIN and kw["port"] == 8765
    assert kw["extra_hosts"] == ["mac.local:8765"]


def test_gate_is_outermost_user_middleware(gated):
    _tc, server = gated
    classes = [m.cls for m in server.app.user_middleware]
    assert classes[0] is MobileGate
    assert classes.count(MobileGate) == 1


# ── (h) mode off: no gate ────────────────────────────────────────────────────

def test_mode_off_gate_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("FEEDBACK_SYNC_STARTUP", "1")
    monkeypatch.delenv("FEEDBACK_MOBILE_MODE", raising=False)
    server = _import_server(monkeypatch)
    try:
        assert all(m.cls is not MobileGate for m in server.app.user_middleware)
        with TestClient(server.app) as tc:
            r = tc.get("/")
            assert r.status_code == 200
            assert "referrer-policy" not in r.headers
    finally:
        _close(server)


# ── (i) highway.js import set drift ──────────────────────────────────────────

_IMPORT_RES = (
    re.compile(r"^\s*(?:import|export)\b[^;]*?\bfrom\s*['\"]([^'\"]+)['\"]", re.M | re.S),
    re.compile(r"^\s*import\s*['\"]([^'\"]+)['\"]", re.M),
    re.compile(r"\bimport\(\s*['\"]([^'\"]+)['\"]\s*\)"),
)


def _transitive_imports(entry: Path, static_root: Path) -> set[str]:
    seen: set[Path] = set()
    todo = [entry.resolve()]
    while todo:
        f = todo.pop()
        if f in seen:
            continue
        seen.add(f)
        src = f.read_text(encoding="utf-8")
        for rx in _IMPORT_RES:
            for spec in rx.findall(src):
                if spec.startswith("/static/"):
                    target = static_root / spec[len("/static/"):]
                elif spec.startswith("."):
                    target = f.parent / spec
                else:
                    raise AssertionError(f"unexpected bare import {spec!r} in {f}")
                todo.append(target.resolve())
    return {"/static/" + p.relative_to(static_root.resolve()).as_posix() for p in seen}


def test_highway_import_set_matches_allowlist():
    static = ROOT / "static"
    assert _transitive_imports(static / "highway.js", static) == set(HIGHWAY_STATIC_FILES)


# ── (j) middleware unit tests: rate limit, WS concurrency, lifespan ─────────

def _scope(kind="http", path="/static/highway.js", method="GET", headers=None):
    hdrs = {"host": "127.0.0.1:8765", "tailscale-user-login": "owner@example.com"}
    hdrs.update(headers or {})
    scope = {
        "type": kind, "path": path, "raw_path": path.encode(),
        "headers": [(k.encode(), v.encode()) for k, v in hdrs.items()],
    }
    if kind == "http":
        scope["method"] = method
    return scope


class _Clock:
    t = 1000.0

    def __call__(self):
        return self.t


def _gate(inner, **kw):
    return MobileGate(inner, origin=ORIGIN, allowed_users=[USER], port=8765, **kw)


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def _run_http(gate, scope):
    sent = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(m):
        sent.append(m)

    asyncio.run(gate(scope, receive, send))
    return sent[0]["status"], dict(sent[0]["headers"])


def test_rate_limit_429_and_refill():
    clock = _Clock()
    gate = _gate(_ok_app, rate=20, burst=60, clock=clock)
    statuses = [_run_http(gate, _scope())[0] for _ in range(61)]
    assert statuses[:60] == [200] * 60
    assert statuses[60] == 429
    status, headers = _run_http(gate, _scope())
    assert status == 429 and headers[b"retry-after"] == b"1"
    clock.t += 0.5   # 10 tokens back
    assert [_run_http(gate, _scope())[0] for _ in range(11)] == [200] * 10 + [429]


def test_rate_limit_is_per_user():
    clock = _Clock()
    gate = MobileGate(_ok_app, origin=ORIGIN, allowed_users=["a@x", "b@x"], port=8765,
                      rate=1, burst=2, clock=clock)
    a = _scope(headers={"tailscale-user-login": "a@x"})
    b = _scope(headers={"tailscale-user-login": "b@x"})
    assert [_run_http(gate, a)[0] for _ in range(3)] == [200, 200, 429]
    assert _run_http(gate, b)[0] == 200


def test_security_headers_override_and_keep_cache_control():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": [
            (b"x-content-type-options", b"bogus"), (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": b""})

    status, headers = _run_http(_gate(app), _scope())
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"referrer-policy"] == b"no-referrer"


def test_ws_concurrency_limit():
    async def main():
        release = asyncio.Event()
        entered = []

        async def ws_app(scope, receive, send):
            await receive()
            await send({"type": "websocket.accept"})
            entered.append(scope["path"])
            await release.wait()

        gate = _gate(ws_app)

        def conn(path):
            sent = []
            first = [True]

            async def receive():
                if first[0]:
                    first[0] = False
                    return {"type": "websocket.connect"}
                await asyncio.sleep(3600)

            async def send(m):
                sent.append(m)

            return gate(_scope("websocket", path), receive, send), sent

        c1, s1 = conn("/ws/highway/a")
        c2, s2 = conn("/ws/highway/b")
        t1, t2 = asyncio.create_task(c1), asyncio.create_task(c2)
        await asyncio.sleep(0.01)
        assert len(entered) == 2
        c3, s3 = conn("/ws/highway/c")
        await c3
        assert s3 == [{"type": "websocket.close", "code": 1013}]
        release.set()
        await asyncio.gather(t1, t2)
        # Slots are freed once the sockets end.
        release.clear()
        c4, s4 = conn("/ws/highway/d")
        t4 = asyncio.create_task(c4)
        await asyncio.sleep(0.01)
        assert s4 == [{"type": "websocket.accept"}]
        release.set()
        await t4

    asyncio.run(main())


def test_lifespan_passes_through_and_unknown_scope_dropped():
    calls = []

    async def app(scope, receive, send):
        calls.append(scope["type"])

    gate = _gate(app)
    asyncio.run(gate({"type": "lifespan"}, None, None))
    asyncio.run(gate({"type": "weird"}, None, None))
    assert calls == ["lifespan"]


def test_gate_rejects_head_without_body():
    sent = []

    async def receive():
        return {"type": "http.request"}

    async def send(m):
        sent.append(m)

    asyncio.run(_gate(_ok_app)(_scope(method="HEAD", path="/"), receive, send))
    assert sent[0]["status"] == 404 and sent[1]["body"] == b""


def test_constructor_rejects_empty_users():
    with pytest.raises(ValueError):
        MobileGate(_ok_app, origin=ORIGIN, allowed_users=[" "])


def test_module_constants_sane():
    assert mobile_gate.MAX_HIGHWAY_SOCKETS == 2
    assert mobile_gate.MAX_HIGHWAY_SOCKETS_TOTAL == 4
    assert (mobile_gate.RATE_PER_SEC, mobile_gate.RATE_BURST) == (20.0, 60.0)


# ── (k) adversarial-review regressions ───────────────────────────────────────

import json
import os

SANDBOX_CSP = "default-src 'none'; sandbox"


@pytest.fixture()
def gated_dlc(tmp_path, monkeypatch):
    """A gated server whose DLC dir sits inside a fake home with a secret next to it."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("-----BEGIN PRIVATE KEY----- SECRET")
    (home / "secret.ogg").write_bytes(b"OggS-SECRET")
    dlc = home / "Music" / "lib"
    dlc.mkdir(parents=True)
    good = dlc / "A.feedpak"
    good.mkdir()
    (good / "manifest.yaml").write_text("title: a\n")
    (good / "s.ogg").write_bytes(b"OggS-good")
    (good / "UP.OGG").write_bytes(b"OggS-upper")
    (good / "cover.webp").write_bytes(b"RIFF....WEBP")
    (good / "x.html").write_text("<script>alert(1)</script>")
    (good / "x.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>')
    os.symlink(str(home / ".ssh" / "id_rsa"), good / "leak.ogg")   # member symlink out
    os.symlink("../..", dlc / "B.feedpak")                          # package root -> ~
    _mobile_env(monkeypatch, tmp_path / "cfg")
    monkeypatch.setenv("DLC_DIR", str(dlc))
    server = _import_server(monkeypatch)
    server.app.user_middleware[0].kwargs["burst"] = 1e9
    tc = TestClient(server.app, base_url=BASE_URL, client=("127.0.0.1", 50000))
    try:
        yield tc, server, dlc, home
    finally:
        tc.close()
        _close(server)


# Finding 1: a symlinked package root turned /api/sloppak into read-anything-in-$HOME.
@pytest.mark.parametrize("path", [
    "/api/sloppak/B.feedpak/file/secret.ogg",
    "/api/sloppak/B.feedpak/file/Music/lib/A.feedpak/s.ogg",
    "/api/sloppak/A.feedpak/file/leak.ogg",
])
def test_symlinked_package_root_exfiltration_blocked(gated_dlc, path):
    tc, *_ = gated_dlc
    r = tc.get(path, headers=GOOD)
    assert r.status_code in (403, 404), (path, r.status_code)
    assert b"SECRET" not in r.content


def test_symlinked_song_root_blocked_for_ws_and_art(gated_dlc):
    tc, *_ = gated_dlc
    assert tc.get("/api/song/B.feedpak/art", headers=GOOD).status_code == 404
    with pytest.raises(WebSocketDisconnect) as exc:
        with tc.websocket_connect(WS_URL + "/ws/highway/B.feedpak", headers=dict(GOOD)):
            pass
    assert exc.value.code == 1008


def test_legit_package_member_still_served(gated_dlc):
    tc, *_ = gated_dlc
    r = tc.get("/api/sloppak/A.feedpak/file/s.ogg", headers=GOOD)
    assert r.status_code == 200 and r.content == b"OggS-good"
    assert tc.get("/api/sloppak/A.feedpak/file/UP.OGG", headers=GOOD).status_code == 200
    assert tc.get("/api/sloppak/A.feedpak/file/cover.webp", headers=GOOD).status_code == 200


# Finding 2a: HTML/SVG/YAML members were served same-origin.
@pytest.mark.parametrize("member", ["x.html", "x.svg", "manifest.yaml", "s.ogg.html", "s"])
def test_sloppak_member_extension_allowlist(gated_dlc, member):
    tc, *_ = gated_dlc
    r = tc.get(f"/api/sloppak/A.feedpak/file/{member}", headers=GOOD)
    assert r.status_code == 404 and r.text == "Not Found"


# Finding 2b: every gated response gets a sandboxing CSP unless it set its own.
def test_sandbox_csp_on_every_response(gated_dlc):
    tc, *_ = gated_dlc
    for path in ("/static/highway.js", "/api/sloppak/A.feedpak/file/s.ogg", "/nope"):
        r = tc.get(path, headers=GOOD)
        assert r.headers["content-security-policy"] == SANDBOX_CSP, path
    r = tc.get("/mobile/", headers=GOOD)
    assert r.status_code == 200
    assert "sandbox" not in r.headers["content-security-policy"]
    sw = tc.get("/mobile/sw.js", headers=GOOD)
    assert sw.status_code == 200
    csp = sw.headers["content-security-policy"]
    assert "sandbox" not in csp and "connect-src 'self'" in csp and "script-src 'self'" in csp
    m = tc.get("/mobile/manifest.webmanifest", headers=GOOD)
    assert m.status_code == 200 and m.headers["content-security-policy"] == "default-src 'none'"


# Finding 2c: Sec-Fetch-Site: none is only for launching the app itself.
@pytest.mark.parametrize("path,dest,status", [
    ("/mobile/", "document", 200),
    ("/mobile", "document", 200),
    ("/mobile/", None, 403),
    ("/mobile/", "script", 403),
    ("/mobile/sw.js", "document", 403),
    ("/static/highway.js", "document", 403),
    ("/api/sloppak/A.feedpak/file/s.ogg", "document", 403),
    ("/api/mobile/songs", "empty", 403),
])
def test_fetch_site_none_only_for_app_entry(gated_dlc, path, dest, status):
    tc, *_ = gated_dlc
    h = {**GOOD, "Sec-Fetch-Site": "none"}
    if dest:
        h["Sec-Fetch-Dest"] = dest
    assert tc.get(path, headers=h).status_code == status


# Finding 3: /audio/ served all of static/.
@pytest.mark.parametrize("path", ["/audio/app.js", "/audio/style.css", "/audio/mobile/index.html",
                                  "/audio/audio_x.js", "/audio/audio_x.ogg.html", "/audio/sub/audio_x.ogg"])
def test_audio_route_only_serves_cached_audio(gated, path):
    tc, _ = gated
    r = tc.get(path, headers=GOOD)
    assert r.status_code == 404 and r.text == "Not Found", path


# Finding 5: ws_highway error frames can carry absolute paths.
def test_ws_error_frames_scrubbed_integration(gated_dlc):
    tc, _server, dlc, _home = gated_dlc
    bad = dlc / "bad.feedpak"
    bad.mkdir()
    (bad / "manifest.yaml").write_text("title: [unclosed\n")
    with tc.websocket_connect(WS_URL + "/ws/highway/bad.feedpak", headers=dict(GOOD)) as ws:
        for _ in range(10):
            m = ws.receive_json()
            if "error" in m:
                break
    assert m == {"error": "Could not load song"}


def _ws_run(gate, path, frames):
    """Run one websocket through `gate`; the inner app sends `frames` (dicts or str)."""
    async def ws_app(scope, receive, send):
        await receive()
        await send({"type": "websocket.accept"})
        for f in frames:
            await send({"type": "websocket.send", "text": f if isinstance(f, str) else json.dumps(f)})
        await send({"type": "websocket.close", "code": 1000})

    sent = []
    first = [True]

    async def receive():
        if first[0]:
            first[0] = False
            return {"type": "websocket.connect"}
        await asyncio.sleep(3600)

    async def send(m):
        sent.append(m)

    asyncio.run(_gate(ws_app)(_scope("websocket", path), receive, send) if gate is None
                else gate(ws_app)(_scope("websocket", path), receive, send))
    return [json.loads(m["text"]) for m in sent if m["type"] == "websocket.send"]


def test_ws_error_frames_scrubbed_unit():
    big = {"type": "notes", "data": list(range(50))}
    out = _ws_run(None, "/ws/highway/x.feedpak", [
        {"type": "loading", "stage": "x"},
        {"error": "[Errno 2] No such file: '/Users/me/Music/x.feedpak/arr.json'"},
        {"type": "song_info", "title": "t", "audio_error": "Audio conversion failed: /Users/me/a.wem"},
        {"type": "song_info", "title": "t", "audio_error": None},
        big,
    ])
    assert out[0] == {"type": "loading", "stage": "x"}
    assert out[1] == {"error": "Could not load song"}
    assert out[2]["audio_error"] == "Audio unavailable" and out[2]["title"] == "t"
    assert out[3]["audio_error"] is None
    assert out[4] == big


# Finding 9: one user could hold every highway slot.
def test_ws_slots_per_user_and_global():
    async def main():
        release = asyncio.Event()
        entered = []

        async def ws_app(scope, receive, send):
            await receive()
            await send({"type": "websocket.accept"})
            entered.append(scope["path"])
            await release.wait()

        gate = MobileGate(ws_app, origin=ORIGIN, allowed_users=["a@x", "b@x", "c@x"], port=8765)

        def conn(user, path):
            sent = []
            first = [True]

            async def receive():
                if first[0]:
                    first[0] = False
                    return {"type": "websocket.connect"}
                await asyncio.sleep(3600)

            async def send(m):
                sent.append(m)

            sc = _scope("websocket", path, headers={"tailscale-user-login": user})
            return gate(sc, receive, send), sent

        tasks = []
        for user, n in (("a@x", 2), ("b@x", 2)):
            for i in range(n):
                c, _s = conn(user, f"/ws/highway/{user}{i}")
                tasks.append(asyncio.create_task(c))
        await asyncio.sleep(0.01)
        assert len(entered) == 4
        c, s = conn("a@x", "/ws/highway/a-extra")
        await c
        assert s == [{"type": "websocket.close", "code": 1013}]   # per-user cap
        c, s = conn("c@x", "/ws/highway/c0")
        await c
        assert s == [{"type": "websocket.close", "code": 1013}]   # global cap
        release.set()
        await asyncio.gather(*tasks)
        release.clear()
        c, s = conn("c@x", "/ws/highway/c1")
        t = asyncio.create_task(c)
        await asyncio.sleep(0.01)
        assert s == [{"type": "websocket.accept"}]
        release.set()
        await t

    asyncio.run(main())


# 3D highway renderer: exactly these plugin/vendor files are reachable.
THREE_D_OK = [
    "/static/vendor/three/three.module.min.js",
    "/static/vendor/three/addons/postprocessing/EffectComposer.js",
    "/static/vendor/three/addons/shaders/CopyShader.js",
    "/api/plugins/highway_3d/screen.js",
    "/api/plugins/highway_3d/assets/plugin.css",
]


@pytest.fixture()
def gated_3d(gated, monkeypatch):
    tc, server = gated
    import plugins as plugins_mod
    d = ROOT / "plugins" / "highway_3d"
    manifest = json.loads((d / "plugin.json").read_text())
    monkeypatch.setattr(plugins_mod, "LOADED_PLUGINS", [
        {"id": "highway_3d", "_dir": d, "_manifest": manifest, "status": "ready"},
        {"id": "drums", "_dir": d, "_manifest": manifest, "status": "ready"},
    ])
    return tc, server


def test_highway_3d_files_allowed(gated_3d):
    tc, _ = gated_3d
    for path in THREE_D_OK:
        r = tc.get(path, headers=GOOD)
        assert r.status_code == 200, (path, r.status_code)
        assert r.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("path", [
    "/api/plugins", "/api/plugins/highway_3d/files/x", "/api/plugins/highway_3d/settings",
    "/api/plugins/drums/screen.js", "/api/plugins/highway_3d/screen.html",
    "/api/plugins/highway_3d/settings.html", "/api/plugins/highway_3d/assets/viz-worklet.js",
    "/api/plugins/highway_3d/g/1/screen.js", "/api/plugins/highway_3d/src/main.js",
    "/static/vendor/three/LICENSE", "/static/vendor/three/addons/x.css",
    "/static/vendor/three/three.module.js",
])
def test_other_plugin_paths_stay_404(gated_3d, path):
    tc, _ = gated_3d
    r = tc.get(path, headers=GOOD)
    assert r.status_code == 404 and r.text == "Not Found", path


@pytest.mark.parametrize("path,mode,dest", [
    ("/api/plugins/highway_3d/screen.js", "no-cors", "script"),      # classic <script>
    ("/api/plugins/highway_3d/assets/plugin.css", "no-cors", "style"),  # <link rel=stylesheet>
    ("/static/vendor/three/three.module.min.js", "cors", "script"),   # module import
    ("/static/vendor/three/addons/postprocessing/RenderPass.js", "cors", "script"),
    ("/static/vendor/three/addons/shaders/CopyShader.js", "cors", "script"),
])
def test_highway_3d_fetch_metadata_same_origin(gated_3d, path, mode, dest):
    tc, _ = gated_3d
    h = {**GOOD, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": mode, "Sec-Fetch-Dest": dest}
    if mode == "no-cors":
        h.pop("Origin")   # browsers send no Origin on a no-cors GET
    r = tc.get(path, headers=h)
    assert r.status_code == 200, (path, r.status_code)
    # Cross-site variants of the same request stay refused.
    assert tc.get(path, headers={**h, "Sec-Fetch-Site": "cross-site"}).status_code == 403


# Finding 6: run-server.sh judged a symlinked env file by the link, not the target.
@pytest.mark.skipif(sys.platform != "darwin", reason="run-server.sh uses BSD stat")
def test_run_server_rejects_symlinked_env(tmp_path):
    import subprocess
    target = tmp_path / "target.env"
    target.write_text("FEEDBACK_ROOT=/nonexistent\n")
    target.chmod(0o666)                    # world-writable: must never be trusted
    link = tmp_path / "env"
    link.symlink_to(target)
    r = subprocess.run(["bash", str(ROOT / "scripts/mobile/run-server.sh")],
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
                            "FEEDBACK_MOBILE_ENV": str(link)},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode != 0
    assert "symlink" in r.stderr
