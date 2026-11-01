"""Mobile offline player: /mobile shell routes, /api/mobile/* and the .fbm
pack builder (lib/mobile_pack.py). Contract: docs/mobile-contract.md."""

from __future__ import annotations

import importlib
import os
import shutil
import struct
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from urllib.parse import quote

REPO = Path(__file__).resolve().parent.parent
STARTER = REPO / "content" / "starter"
STAR = "star_spangled_banner.feedpak"        # lead + full.ogg + cover.webp
ELISE = "beethoven-fur_elise.feedpak"        # keys + full.ogg + an mp3 stem

HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    dlc = tmp_path / "dlc"
    cfg.mkdir()
    dlc.mkdir()
    for name in (STAR, ELISE):
        shutil.copy2(STARTER / name, dlc / name)
    monkeypatch.setenv("CONFIG_DIR", str(cfg))
    monkeypatch.setenv("DLC_DIR", str(dlc))
    monkeypatch.setenv("FEEDBACK_SYNC_STARTUP", "1")
    monkeypatch.delenv("FEEDBACK_MOBILE_ORIGIN", raising=False)
    sys.modules.pop("server", None)
    server = importlib.import_module("server")
    monkeypatch.setattr(server, "load_plugins", lambda *a, **kw: None)
    monkeypatch.setattr(server, "startup_scan", lambda: None)

    import mobile_pack
    from routers import mobile
    from scan_worker import _scan_one
    mobile_pack._status.clear()
    mobile_pack._version_cache.clear()
    # sloppak's unpack-dir cache is process-global and keyed by filename; a
    # previous test's (still existing) tmp unpack dir would be handed back.
    import sloppak as sloppak_mod
    with sloppak_mod._source_lock:
        sloppak_mod._source_cache.clear()
    if not any(getattr(r, "path", "") == "/api/mobile/songs" for r in server.app.routes):
        server.app.include_router(mobile.router)

    for f in sorted(dlc.iterdir()):
        st = f.stat()
        rel, mtime, size, meta = _scan_one((f, st.st_mtime, st.st_size, dlc))
        server.meta_db.put(rel, mtime, size, meta)

    client = TestClient(server.app)
    try:
        yield client, server, dlc, cfg, mobile_pack, mobile
    finally:
        client.close()
        conn = getattr(getattr(server, "meta_db", None), "conn", None)
        if conn is not None:
            getattr(server, "_join_background_db_threads", lambda: None)()
            conn.close()


def _url(song_id: str, action: str) -> str:
    return f"/api/mobile/songs/{quote(song_id, safe='')}/{action}"


# ── songs list ────────────────────────────────────────────────────────────────

def test_songs_list_shape_and_stable_version(env):
    client, *_ = env
    r = client.get("/api/mobile/songs")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == len(body["songs"]) == 2
    by_id = {s["id"]: s for s in body["songs"]}
    assert set(by_id) == {STAR, ELISE}
    s = by_id[STAR]
    assert set(s) == {"id", "title", "artist", "album", "duration", "size_bytes",
                      "size_is_estimate", "version", "has_stems", "arrangements"}
    assert len(s["version"]) == 40 and int(s["version"], 16) >= 0
    assert s["size_is_estimate"] is True
    assert s["size_bytes"] == (env[2] / STAR).stat().st_size
    assert s["arrangements"] and isinstance(s["arrangements"][0], str)
    again = {x["id"]: x["version"] for x in client.get("/api/mobile/songs").json()["songs"]}
    assert again[STAR] == s["version"]


def test_version_changes_with_mtime(env):
    client, _server, dlc, *_ = env
    v1 = {x["id"]: x["version"] for x in client.get("/api/mobile/songs").json()["songs"]}
    p = dlc / STAR
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 100))
    v2 = {x["id"]: x["version"] for x in client.get("/api/mobile/songs").json()["songs"]}
    assert v1[STAR] != v2[STAR]
    assert v1[ELISE] == v2[ELISE]


def test_song_version_formula(env):
    *_, mobile_pack, _m = env
    import hashlib
    assert mobile_pack.song_version("a/b.feedpak", 1.5, 10) == hashlib.sha1(
        b"a/b.feedpak|1.5|10|1").hexdigest()


# ── download + pack format ────────────────────────────────────────────────────

def _check_pack(data: bytes, mobile_pack, tmp_path, song_id: str) -> dict:
    assert data[:4] == b"FBMP"
    (n,) = struct.unpack(">I", data[4:8])
    p = tmp_path / "check.fbm"
    p.write_bytes(data)
    header, blob_off = mobile_pack.parse_fbm(p)
    assert blob_off == 8 + n
    assert header["format"] == 1 and header["id"] == song_id
    keys = {f["key"]: f for f in header["files"]}
    for f in header["files"]:
        assert f["offset"] >= 0 and f["length"] > 0
        assert blob_off + f["offset"] + f["length"] <= len(data)
    # Blobs are back to back and fill the file exactly.
    end = max(f["offset"] + f["length"] for f in header["files"])
    assert blob_off + end == len(data)
    assert header["arrangements"]
    for arr in header["arrangements"]:
        msgs = arr["messages"]
        assert msgs[-1] == {"type": "ready"}
        types = [m.get("type") for m in msgs]
        assert "loading" not in types
        assert "song_info" in types and "notes" in types
        si = next(m for m in msgs if m.get("type") == "song_info")
        assert si["arrangement_index"] == arr["index"]
        urls = [si.get("audio_url"), si.get("full_mix_url")] + [
            s["url"] for s in si.get("stems") or []]
        for u in urls:
            if u is None:
                continue
            assert u.startswith("fbm:"), u
            assert u[4:] in keys, u
    for f in header["files"]:
        blob = data[blob_off + f["offset"]: blob_off + f["offset"] + f["length"]]
        if f["mime"] == "audio/mp4":
            assert blob[4:8] == b"ftyp"
        elif f["mime"] == "audio/mpeg":
            assert blob[:3] == b"ID3" or (blob[0] == 0xFF and blob[1] & 0xE0 == 0xE0)
        elif f["key"] == "cover":
            assert f["mime"].startswith("image/")
    return header


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_download_builds_valid_pack_and_status(env, tmp_path):
    client, _server, _dlc, cfg, mobile_pack, _m = env
    st = client.get(_url(STAR, "status")).json()
    assert st["state"] == "missing" and st["size_bytes"] is None
    r = client.get(_url(STAR, "download"))
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["etag"] == f'"{st["version"]}"'
    assert r.headers["content-disposition"] == 'attachment; filename="star_spangled_banner.fbm"'
    assert int(r.headers["content-length"]) == len(r.content)
    header = _check_pack(r.content, mobile_pack, tmp_path, STAR)
    keys = {f["key"] for f in header["files"]}
    assert "stem:full" in keys and "cover" in keys
    assert header["version"] == st["version"]
    assert header["album"] == "The Anacreontic Song" and header["title"]
    st2 = client.get(_url(STAR, "status")).json()
    assert st2 == {"state": "ready", "version": st["version"],
                   "size_bytes": len(r.content), "error": None}
    # songs list now reports the real size
    s = {x["id"]: x for x in client.get("/api/mobile/songs").json()["songs"]}[STAR]
    assert s["size_bytes"] == len(r.content) and s["size_is_estimate"] is False
    # no work files left behind
    tmp = cfg / "mobile_cache" / "tmp"
    assert not tmp.exists() or not any(tmp.iterdir())


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_download_with_stems_and_mp3_copy(env, tmp_path):
    client, *_rest = env
    mobile_pack = env[4]
    r = client.get(_url(ELISE, "download"))
    assert r.status_code == 200
    header = _check_pack(r.content, mobile_pack, tmp_path, ELISE)
    assert header["files"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_second_download_uses_cache_and_prunes_old(env, monkeypatch):
    client, _server, dlc, cfg, mobile_pack, _m = env
    assert client.get(_url(STAR, "download")).status_code == 200
    calls = []
    real = mobile_pack.build_pack

    async def spy(*a, **kw):
        calls.append(a)
        return await real(*a, **kw)
    monkeypatch.setattr(mobile_pack, "build_pack", spy)
    assert client.get(_url(STAR, "download")).status_code == 200
    assert calls == []
    # A new source version rebuilds and prunes the old pack (once it is no longer
    # recently touched: a pack served in the last PRUNE_GRACE_S is kept).
    for old in (cfg / "mobile_cache").glob("*.fbm"):
        t = time.time() - mobile_pack.PRUNE_GRACE_S - 10
        os.utime(old, (t, t))
    p = dlc / STAR
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 50))
    assert client.get(_url(STAR, "download")).status_code == 200
    assert len(calls) == 1
    packs = list((cfg / "mobile_cache").glob("*.fbm"))
    assert len(packs) == 1


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_size_cap_sets_error_and_leaves_no_file(env, monkeypatch):
    client, _server, _dlc, cfg, mobile_pack, _m = env
    monkeypatch.setattr(mobile_pack, "MAX_FILE_BYTES", 1000)
    r = client.get(_url(STAR, "download"))
    assert r.status_code == 500
    assert r.json() == {"error": "song too large"}
    st = client.get(_url(STAR, "status")).json()
    assert st["state"] == "error" and st["error"] == "song too large"
    cache = cfg / "mobile_cache"
    assert not list(cache.glob("*.fbm"))
    assert not (cache / "tmp").exists() or not any((cache / "tmp").iterdir())


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_total_cap(env, monkeypatch):
    client, _server, _dlc, cfg, mobile_pack, _m = env
    monkeypatch.setattr(mobile_pack, "MAX_TOTAL_BYTES", 5000)
    r = client.get(_url(STAR, "download"))
    assert r.status_code == 500
    assert not list((cfg / "mobile_cache").glob("*.fbm"))


def test_busy_returns_503(env, monkeypatch):
    client, _server, _dlc, _cfg, mobile_pack, _m = env

    async def busy(*a, **kw):
        raise mobile_pack.BuildBusy()
    monkeypatch.setattr(mobile_pack, "ensure_pack", busy)
    r = client.get(_url(STAR, "download"))
    assert r.status_code == 503 and "retry-after" in r.headers


@pytest.mark.parametrize("bad", [
    "../../etc/passwd",
    "/etc/passwd",
    "..%2F..%2Fetc%2Fpasswd",
    "%2e%2e/%2e%2e/etc/passwd",
    "nope.feedpak",
])
def test_traversal_and_unknown_ids(env, bad):
    client, *_ = env
    for action in ("status", "download"):
        r = client.get(f"/api/mobile/songs/{quote(bad, safe='%')}/{action}")
        assert r.status_code in (400, 404), (bad, action, r.status_code)
        assert "passwd" not in r.text and "root:" not in r.text


def test_resolve_audio_url_rejects_foreign_song(env):
    *_, mobile_pack, _m = env
    with pytest.raises(mobile_pack.PackError):
        mobile_pack.resolve_audio_url(STAR, f"/api/sloppak/{quote(ELISE, safe='')}/file/x.ogg")
    with pytest.raises(mobile_pack.PackError):
        mobile_pack.resolve_audio_url(STAR, "/audio/../../etc/passwd")
    with pytest.raises(mobile_pack.PackError):
        mobile_pack.resolve_audio_url(STAR, "https://evil.example/x.mp3")


# ── shell routes ──────────────────────────────────────────────────────────────

def test_mobile_index_csp(env, tmp_path, monkeypatch):
    client, server, *_ = env
    static = tmp_path / "static"
    (static / "mobile").mkdir(parents=True)
    monkeypatch.setattr(server.appstate, "static_dir", static)
    assert client.get("/mobile").status_code == 404
    (static / "mobile" / "index.html").write_text("<!doctype html><title>m</title>")
    for path in ("/mobile", "/mobile/"):
        r = client.get(path)
        assert r.status_code == 200
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "connect-src 'self';" in csp
    assert client.head("/mobile").status_code == 200
    monkeypatch.setenv("FEEDBACK_MOBILE_ORIGIN", "https://mbp.tail1.ts.net")
    csp = client.get("/mobile").headers["content-security-policy"]
    assert "connect-src 'self' wss://mbp.tail1.ts.net;" in csp
    monkeypatch.setenv("FEEDBACK_MOBILE_ORIGIN", "https://x.net; script-src *")
    csp = client.get("/mobile").headers["content-security-policy"]
    assert "script-src *" not in csp


def test_sw_and_manifest(env, tmp_path, monkeypatch):
    client, server, *_ = env
    static = tmp_path / "static"
    (static / "mobile").mkdir(parents=True)
    (static / "js").mkdir()
    monkeypatch.setattr(server.appstate, "static_dir", static)
    (static / "mobile" / "sw.js").write_text("const V='__FBM_CACHE_VERSION__';")
    (static / "mobile" / "manifest.webmanifest").write_text('{"name":"m"}')
    (static / "highway.js").write_text("// hw")
    r = client.get("/mobile/sw.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    assert r.headers["cache-control"] == "no-cache"
    assert "__FBM_CACHE_VERSION__" not in r.text
    v1 = r.text
    (static / "js" / "highway-x.js").write_text("// changed")
    v2 = client.get("/mobile/sw.js").text
    assert v1 != v2
    m = client.get("/mobile/manifest.webmanifest")
    assert m.status_code == 200
    assert m.headers["content-type"].startswith("application/manifest+json")


def test_info(env, monkeypatch):
    client, *_ = env
    monkeypatch.setenv("FEEDBACK_MOBILE_ORIGIN", "http://127.0.0.1:8766")
    r = client.get("/api/mobile/info", headers={"Tailscale-User-Login": "me@example.com"})
    assert r.status_code == 200
    body = r.json()
    assert body["origin"] == "http://127.0.0.1:8766"
    assert body["user"] == "me@example.com"
    assert isinstance(body["version"], str)
    assert client.get("/api/mobile/info").json()["user"] is None


def test_build_timeout_sets_error_and_leaves_no_file(env, monkeypatch):
    client, _server, _dlc, cfg, mobile_pack, _m = env
    import asyncio

    async def slow(*a, **kw):
        await asyncio.sleep(5)
    monkeypatch.setattr(mobile_pack, "build_pack", slow)
    monkeypatch.setattr(mobile_pack, "BUILD_TIMEOUT_S", 0.05)
    r = client.get(_url(STAR, "download"))
    assert r.status_code == 500 and r.json() == {"error": "build timed out"}
    assert client.get(_url(STAR, "status")).json()["state"] == "error"
    assert not list((cfg / "mobile_cache").glob("*.fbm")) if (cfg / "mobile_cache").exists() else True


def test_head_on_status(env):
    client, *_ = env
    assert client.head(_url(STAR, "status")).status_code == 200


# ── adversarial-review regressions ────────────────────────────────────────────

import asyncio
import json
import subprocess


def test_resolve_audio_url_rejects_symlinked_package_root(env, tmp_path):
    """Finding 1 (builder side): a package that is a symlink out of the library."""
    _c, _s, dlc, _cfg, mobile_pack, _m = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.ogg").write_bytes(b"OggS-SECRET")
    os.symlink(str(outside), dlc / "B.feedpak")
    with pytest.raises(mobile_pack.PackError):
        mobile_pack.resolve_audio_url("B.feedpak", "/api/sloppak/B.feedpak/file/x.ogg")


def test_resolve_audio_url_member_and_audio_name_allowlists(env):
    """Findings 2/3 (builder side): only audio members and cached-audio names."""
    *_, mobile_pack, _m = env
    # manifest.yaml exists inside the starter pack, but is not audio.
    with pytest.raises(mobile_pack.PackError):
        mobile_pack.resolve_audio_url(STAR, f"/api/sloppak/{quote(STAR, safe='')}/file/manifest.yaml")
    # /audio/ used to resolve any file under static/.
    with pytest.raises(mobile_pack.PackError):
        mobile_pack.resolve_audio_url(STAR, "/audio/app.js")
    with pytest.raises(mobile_pack.PackError):
        mobile_pack.resolve_audio_url(STAR, "/audio/mobile/index.html")


@pytest.mark.parametrize("name", ["s.m3u8", "s.ffconcat", "s.webm", "s.html", "s"])
def test_prepare_audio_rejects_non_audio_sources(env, tmp_path, monkeypatch, name):
    """Finding 4: only known audio extensions ever reach ffmpeg."""
    *_, mobile_pack, _m = env
    src = tmp_path / name
    src.write_bytes(b"#EXTM3U\n")
    calls = []

    async def fake_exec(*a, **kw):
        calls.append(a)
        raise AssertionError("ffmpeg must not run")
    monkeypatch.setattr(mobile_pack.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(mobile_pack.PackError):
        asyncio.run(mobile_pack._prepare_audio(src, tmp_path, "128k", 1e12))
    assert calls == []


def test_transcode_passes_whitelists_and_size_cap(env, tmp_path, monkeypatch):
    *_, mobile_pack, _m = env
    seen = []

    class _P:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def fake_exec(*a, **kw):
        seen.append(list(a))
        return _P()
    monkeypatch.setattr(mobile_pack.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(mobile_pack, "_ffmpeg_cmd", lambda: "ffmpeg")
    src = tmp_path / "a.flac"
    src.write_bytes(b"fLaC")
    asyncio.run(mobile_pack._transcode(src, tmp_path / "o.m4a", "128k", 1e12))
    args = seen[0]
    i = args.index("-i")
    pre = args[:i]
    assert pre[pre.index("-protocol_whitelist") + 1] == "file"
    assert set(pre[pre.index("-format_whitelist") + 1].split(",")) == {
        "ogg", "flac", "wav", "mp3", "aac", "mov"}
    assert int(args[args.index("-fs") + 1]) == mobile_pack.MAX_FILE_BYTES + 1


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_transcode_refuses_concat_script_disguised_as_flac(env, tmp_path):
    """Finding 4: a demuxer script named like audio must not be followed."""
    *_, mobile_pack, _m = env
    d = tmp_path / "pk"
    d.mkdir()
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i",
                    "sine=d=1", str(d / "other.wav")], check=True)
    (d / "s.flac").write_text("ffconcat version 1.0\nfile other.wav\n")
    with pytest.raises(mobile_pack.PackError):
        asyncio.run(mobile_pack._transcode(d / "s.flac", tmp_path / "o.m4a", "128k",
                                           asyncio.new_event_loop().time() + 1e9))


def test_audio_error_scrubbed_from_pack(env, monkeypatch, tmp_path):
    """Finding 5: song_info free text (str(exception), absolute paths) never ships."""
    *_, mobile_pack, _m = env

    async def fake_record(fn, idx):
        return [{"type": "song_info", "title": "t", "artist": "a", "arrangement_index": 0,
                 "arrangements": [{"index": 0, "name": "Lead"}], "audio_url": None,
                 "audio_error": "Audio conversion failed: /Users/secret/Music/x/audio.wem"},
                {"type": "ready"}]

    async def no_cover(_id):
        return None
    monkeypatch.setattr(mobile_pack, "record_highway_messages", fake_record)
    monkeypatch.setattr(mobile_pack, "_cover_bytes", no_cover)
    out = asyncio.run(mobile_pack.build_pack(STAR, "v1"))
    header, _ = mobile_pack.parse_fbm(out)
    raw = out.read_bytes()
    assert b"/Users/secret" not in raw
    si = header["arrangements"][0]["messages"][0]
    assert si["audio_error"] == "Audio unavailable"


@pytest.mark.parametrize("alias", ["sub/../" + STAR, STAR + "/", "./" + STAR, "link.feedpak"])
def test_noncanonical_ids_rejected(env, alias):
    """Finding 7: one song, one id — aliases would each get their own cached pack."""
    client, _s, dlc, *_ = env
    os.symlink(STAR, dlc / "link.feedpak")
    for action in ("status", "download"):
        r = client.get(_url(alias, action))
        assert r.status_code == 404, (alias, action, r.status_code)
    assert client.get(_url(STAR, "status")).status_code == 200


def _fake_pack(cache: Path, name: str, size: int, age_s: float) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    p = cache / name
    p.write_bytes(b"x" * size)
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def test_cache_cap_evicts_least_recently_used(env, monkeypatch):
    """Finding 7: total cache bytes are capped, oldest first."""
    _c, _s, _d, cfg, mobile_pack, _m = env
    cache = cfg / "mobile_cache"
    oldest = _fake_pack(cache, "a" * 40 + "-1.fbm", 100, 3000)
    old = _fake_pack(cache, "b" * 40 + "-1.fbm", 100, 2000)
    newer = _fake_pack(cache, "c" * 40 + "-1.fbm", 100, 1000)
    recent = _fake_pack(cache, "d" * 40 + "-1.fbm", 100, 10)     # may be mid-download
    keep = _fake_pack(cache, "e" * 40 + "-1.fbm", 100, 5000)     # the pack just built
    monkeypatch.setenv("FEEDBACK_MOBILE_CACHE_MAX_BYTES", "200")
    mobile_pack.enforce_cache_cap(keep)
    assert not oldest.exists() and not old.exists() and not newer.exists()
    assert recent.exists() and keep.exists()


def test_cache_cap_default(env, monkeypatch):
    *_, mobile_pack, _m = env
    monkeypatch.delenv("FEEDBACK_MOBILE_CACHE_MAX_BYTES", raising=False)
    assert mobile_pack.cache_max_bytes() == 20 * 1024 ** 3
    monkeypatch.setenv("FEEDBACK_MOBILE_CACHE_MAX_BYTES", "junk")
    assert mobile_pack.cache_max_bytes() == 20 * 1024 ** 3


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_build_enforces_cache_cap(env, monkeypatch):
    client, _s, _d, cfg, mobile_pack, _m = env
    stale = _fake_pack(cfg / "mobile_cache", "f" * 40 + "-1.fbm", 10, 5000)
    monkeypatch.setenv("FEEDBACK_MOBILE_CACHE_MAX_BYTES", "1")
    assert client.get(_url(STAR, "download")).status_code == 200
    assert not stale.exists()
    assert len(list((cfg / "mobile_cache").glob("*.fbm"))) == 1


def test_prune_keeps_recently_touched_versions(env):
    """Finding 10: an old version still being downloaded is not deleted under it."""
    _c, _s, _d, cfg, mobile_pack, _m = env
    cache = cfg / "mobile_cache"
    h = mobile_pack._id_hash(STAR)
    fresh_old = _fake_pack(cache, f"{h}-old1.fbm", 10, 30)
    stale_old = _fake_pack(cache, f"{h}-old2.fbm", 10, 500)
    keep = _fake_pack(cache, f"{h}-new.fbm", 10, 0)
    mobile_pack._prune_old_versions(STAR, keep)
    assert fresh_old.exists() and keep.exists() and not stale_old.exists()


def test_version_cache_for_directory_packs(env, tmp_path, monkeypatch):
    """Finding 8: /api/mobile/songs does not re-walk every directory pack each call."""
    *_, mobile_pack, _m = env
    mobile_pack._version_cache.clear()
    d = tmp_path / "dirpack.feedpak"
    d.mkdir()
    (d / "a.ogg").write_bytes(b"1")
    walks = []
    real_walk = os.walk

    def counting_walk(p, *a, **kw):
        walks.append(p)
        return real_walk(p, *a, **kw)
    monkeypatch.setattr(mobile_pack.os, "walk", counting_walk)
    clock = [1000.0]
    monkeypatch.setattr(mobile_pack, "_now", lambda: clock[0])
    v1 = mobile_pack.version_for_cached("dirpack.feedpak", d)
    v2 = mobile_pack.version_for_cached("dirpack.feedpak", d)
    assert v1 == v2 == mobile_pack.version_for("dirpack.feedpak", d)
    assert len(walks) == 2   # one cached call + the uncached version_for above
    clock[0] += mobile_pack.DIR_VERSION_TTL_S + 1
    mobile_pack.version_for_cached("dirpack.feedpak", d)
    assert len(walks) == 3
    (d / "b.ogg").write_bytes(b"22")              # changes the dir's own mtime
    st = d.stat()
    os.utime(d, (st.st_atime, st.st_mtime + 5))
    v3 = mobile_pack.version_for_cached("dirpack.feedpak", d)
    assert len(walks) == 4 and v3 != v1
    # Single files: keyed by (mtime, size), never walked.
    f = tmp_path / "f.feedpak"
    f.write_bytes(b"zip")
    assert mobile_pack.version_for_cached("f.feedpak", f) == mobile_pack.version_for("f.feedpak", f)
    assert len(walks) == 4


@pytest.mark.parametrize("which", [
    "static:vendor/three/three.module.min.js",
    "static:vendor/three/addons/postprocessing/EffectComposer.js",
    "static:vendor/three/addons/shaders/OutputShader.js",
    "plugin:screen.js",
    "plugin:assets/plugin.css",
])
def test_sw_version_tracks_3d_renderer_files(env, tmp_path, monkeypatch, which):
    client, server, *_ = env
    _mobile = env[5]
    static = tmp_path / "static"
    (static / "mobile").mkdir(parents=True)
    (static / "mobile" / "sw.js").write_text("const V='__FBM_CACHE_VERSION__';")
    h3d = tmp_path / "h3d"
    monkeypatch.setattr(server.appstate, "static_dir", static)
    monkeypatch.setattr(_mobile, "_highway_3d_dir", lambda: h3d)
    files = [static / r for r in _mobile._THREE_STATIC_FILES] + \
            [h3d / r for r in _mobile._HIGHWAY_3D_FILES]
    for f in files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("// v1")
    v1 = client.get("/mobile/sw.js").text
    assert client.get("/mobile/sw.js").text == v1
    kind, rel = which.split(":", 1)
    target = (static if kind == "static" else h3d) / rel
    target.write_text("// v2")
    assert client.get("/mobile/sw.js").text != v1


def test_highway_3d_dir_prefers_loaded_plugin(env, monkeypatch, tmp_path):
    *_, mobile = env
    import plugins as plugins_mod
    monkeypatch.setattr(plugins_mod, "LOADED_PLUGINS", [])
    assert mobile._highway_3d_dir() == plugins_mod.PLUGINS_DIR / "highway_3d"
    monkeypatch.setattr(plugins_mod, "LOADED_PLUGINS", [{"id": "highway_3d", "_dir": tmp_path}])
    assert mobile._highway_3d_dir() == tmp_path


@pytest.mark.parametrize("media_type,kept", [
    ("image/svg+xml", False),
    ("text/html", False),
    ("image/png", True),
    ("image/webp; charset=binary", True),
])
def test_cover_only_raster_images(env, monkeypatch, media_type, kept):
    """An SVG/HTML cover must not be packed: it can carry script, and the
    phone's parser rejects any pack holding a non-raster image."""
    import asyncio
    from fastapi.responses import Response
    import mobile_pack
    from routers import art as art_router

    async def fake_art(song_id, request=None):
        return Response(content=b"<svg onload=alert(1)>", media_type=media_type)

    monkeypatch.setattr(art_router, "get_song_art", fake_art)
    got = asyncio.run(mobile_pack._cover_bytes("x.feedpak"))
    assert (got is not None) == kept
