"""Real album track order from MusicBrainz, for albums whose charts carry no
track numbers (most CDLC / converted charts don't, so the album page falls
back to title order).

One release search + one release lookup per album, ever: results (including
"not found") are cached in ``<config_dir>/album_tracklists.json``. Uses the
enrichment module's MusicBrainz client, so it shares its 1 req/s throttle,
User-Agent and offline kill-switch (FEEDBACK_ENRICH_OFFLINE).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

import appstate
from mb_match import denoise, similarity

log = logging.getLogger("feedBack.album_tracklist")

_CACHE_FILE = "album_tracklists.json"
_NEGATIVE_TTL_S = 7 * 24 * 3600   # retry a "not found" album after a week
_TITLE_MIN = 0.75                  # release-title similarity to accept a hit
_TRACK_MIN = 0.8                   # chart title ↔ track title match
_lock = threading.Lock()


def _cache_path() -> Path:
    return Path(appstate.config_dir) / _CACHE_FILE


def _load_cache() -> dict:
    try:
        return json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        log.warning("album tracklist cache write failed: %s", e)


def _key(artist: str, album: str) -> str:
    return denoise(artist, strip_leading_the=True) + "␟" + denoise(album)


def _lucene(s: str) -> str:
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _pick_release(releases: list[dict], album: str) -> dict | None:
    """Best release for the album name: close title, official, studio album,
    then the original (earliest) release, then the plainest edition (fewest
    tracks) — so deluxe / reissue bonus tracks don't show as "missing"."""
    cands = []
    for r in releases or []:
        if not isinstance(r, dict) or not r.get("id"):
            continue
        sim = similarity(r.get("title", ""), album)
        if sim < _TITLE_MIN:
            continue
        rg = r.get("release-group") or {}
        official = str(r.get("status", "")).lower() == "official"
        album_type = str(rg.get("primary-type", "")).lower() == "album"
        secondary = bool(rg.get("secondary-types"))
        tracks = int(r.get("track-count") or 0) or 9999
        has_disambig = bool(str(r.get("disambiguation") or "").strip())
        cands.append(((-sim, not official, not album_type, secondary,
                       str(r.get("date") or "9999"), has_disambig, tracks), r))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    return cands[0][1]


def fetch_tracklist(artist: str, album: str) -> dict:
    """{"release_id", "release_title", "tracks": [{disc, position, title}]},
    or {"tracks": []} when MusicBrainz has no match. Cached; network errors
    (offline, rate-limited) return {"tracks": [], "error": ...} uncached."""
    key = _key(artist, album)
    with _lock:
        hit = _load_cache().get(key)
    if hit and (hit.get("tracks") or time.time() - hit.get("at", 0) < _NEGATIVE_TTL_S):
        return hit

    from enrichment import EnrichTransportError, _mb_http_get as _raw_get

    def _mb_http_get(path, params):
        # MusicBrainz answers a burst with 503; one polite retry covers the
        # common case (another lookup just ran) without hammering it.
        try:
            return _raw_get(path, params)
        except EnrichTransportError as e:
            if "503" not in str(e):
                raise
            time.sleep(2.0)
            return _raw_get(path, params)

    try:
        body = _mb_http_get("release", {
            "query": f"release:{_lucene(album)} AND artist:{_lucene(artist)}",
            "limit": 25,
        }) or {}
        rel = _pick_release(body.get("releases") or [], album)
        result: dict = {"tracks": [], "at": time.time()}
        if rel:
            full = _mb_http_get(f"release/{rel['id']}", {"inc": "recordings"}) or {}
            tracks = []
            for m in full.get("media") or []:
                disc = int(m.get("position") or 1)
                for t in m.get("tracks") or []:
                    title = (t.get("title") or (t.get("recording") or {}).get("title") or "").strip()
                    if title:
                        tracks.append({"disc": disc, "position": int(t.get("position") or 0), "title": title})
            result.update({"release_id": rel["id"], "release_title": full.get("title") or rel.get("title", ""),
                           "tracks": tracks})
    except EnrichTransportError as e:
        return {"tracks": [], "error": str(e)}

    with _lock:
        cache = _load_cache()
        cache[key] = result
        _save_cache(cache)
    return result


def order_songs(artist: str, album: str, songs: list[dict]) -> dict:
    """Match the album's charts to the real track list.

    ``songs`` = [{filename, title}]. Returns {"order": [{filename, disc,
    position}] sorted by disc/position (charts with no matching track are left
    out — the caller keeps them after the matched ones), "missing": [{title,
    disc, position}] tracks with no chart on this album, "release_title",
    "source"}."""
    tl = fetch_tracklist(artist, album)
    tracks = tl.get("tracks") or []
    out = {"order": [], "missing": [], "release_title": tl.get("release_title", ""),
           "source": "musicbrainz" if tracks else "", "error": tl.get("error", "")}
    if not tracks:
        return out
    used: set[int] = set()
    matched = []
    for s in songs or []:
        fn, title = str(s.get("filename") or ""), str(s.get("title") or "")
        if not fn or not title:
            continue
        best, best_i = 0.0, -1
        for i, t in enumerate(tracks):
            if i in used:
                continue
            sim = similarity(title, t["title"])
            if sim > best:
                best, best_i = sim, i
        if best_i >= 0 and best >= _TRACK_MIN:
            used.add(best_i)
            t = tracks[best_i]
            matched.append({"filename": fn, "disc": t["disc"], "position": t["position"]})
    matched.sort(key=lambda m: (m["disc"], m["position"]))
    out["order"] = matched
    out["missing"] = [{"title": t["title"], "disc": t["disc"], "position": t["position"]}
                      for i, t in enumerate(tracks) if i not in used]
    return out
