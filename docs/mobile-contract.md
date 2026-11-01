# Mobile player — interface contract

These are the interfaces shared by the gate, the backend and the frontend. The plan is in `docs/mobile-offline-plan.md`. Change this file only by agreement.

## Ports and env
- The mobile server binds to `127.0.0.1:8765` and runs `python main.py` with `HOST=127.0.0.1 PORT=8765`.
- Env vars it reads:

| Var | Meaning |
|---|---|
| `FEEDBACK_MOBILE_MODE` | `1` turns on the gate |
| `FEEDBACK_MOBILE_ORIGIN` | e.g. `https://mbp-home.tail1234.ts.net`; in dev, `http://127.0.0.1:8766` |
| `FEEDBACK_MOBILE_ALLOWED_USERS` | comma-separated Tailscale logins |
| `FEEDBACK_MOBILE_HOSTS` | optional extra allowed `Host` values, comma-separated |
| `FEEDBACK_MOBILE_CACHE_MAX_BYTES` | optional cap on the total size of `mobile_cache/*.fbm` (default 20 GB) |
| `CONFIG_DIR` | config directory |
| `DLC_DIR` | library directory |

- The dev proxy (`scripts/mobile/dev-proxy.py`) listens on `127.0.0.1:8766`. It forwards HTTP and WebSocket traffic to 8765 and injects `Tailscale-User-Login: <login>`, the same way `tailscale serve` does. It strips any copy of that header the client sends.

## Routes (all GET or HEAD; nothing else gets through the gate)

| Path | Owner | Notes |
|---|---|---|
| `/mobile`, `/mobile/` | `lib/routers/mobile.py` | serves `static/mobile/index.html` with the CSP header |
| `/mobile/sw.js` | `mobile.py` | serves `static/mobile/sw.js`, replacing the literal `__FBM_CACHE_VERSION__` with `<VERSION>-<hash of precached files>` (the mobile app, the 2D highway files and the 3D renderer files below); `Content-Type: application/javascript`, `Cache-Control: no-cache`, its own worker CSP (`default-src 'self'; script-src 'self'; connect-src 'self'; ...`) |
| `/mobile/manifest.webmanifest` | `mobile.py` | serves `static/mobile/manifest.webmanifest`; `Content-Type: application/manifest+json`, `Content-Security-Policy: default-src 'none'` |
| `/static/mobile/*` | existing `/static` mount | the frontend's own assets |
| `/static/highway.js`, `/static/js/highway-*.js`, plus any other files highway.js imports transitively | existing mount | the gate allowlists the exact transitive import set |
| `/static/vendor/three/three.module.min.js`, `/static/vendor/three/addons/<[A-Za-z0-9_/-]+>.js` | existing mount | 3D highway renderer (module imports) |
| `/api/plugins/highway_3d/screen.js`, `/api/plugins/highway_3d/assets/plugin.css` | core plugin routes | 3D highway renderer entry script and stylesheet. No other `/api/plugins/*` path (including the `/g/<n>/` generation prefix, `src/`, other assets, other plugins) passes the gate |
| `WS /ws/highway/{id}` | existing | allowed after the Origin, identity and Host checks, and only if `{id}` (symlinks followed) is inside the library. At most 2 sockets per user and 4 in total. The gate replaces any `{"error": ...}` frame's value with `"Could not load song"` and a non-empty `song_info.audio_error` with `"Audio unavailable"` |
| `/api/sloppak/{id}/file/{rel}` | existing | only when `{rel}` ends in `.ogg .opus .oga .mp3 .m4a .aac .flac .wav .jpg .jpeg .png .webp` (any case), `{id}` (symlinks followed) is inside the library, and for a directory-form package `{rel}` (symlinks followed) is inside the package |
| `/audio/audio_<name>.{mp3,ogg,wav}` | existing | single segment only: the names `ws_highway` generates for cached audio. Nothing else under `/audio/` |
| `/api/song/{id}/art` | existing | only if `{id}` (symlinks followed) is inside the library |

### Gate rules that apply to every route
- **CSP:** any response without its own `Content-Security-Policy` (including the gate's own 4xx responses) gets `default-src 'none'; sandbox`. `/mobile`, `/mobile/sw.js` and the manifest set their own.
- **Fetch metadata:** `Sec-Fetch-Site` must be `same-origin`, or `none` only for a top-level navigation (`Sec-Fetch-Dest: document`) to `/mobile` or `/mobile/` (typed URL, home-screen launch). A missing header is allowed (older Safari, curl); the other checks and the CSP sandbox still apply.
- **Songs reached through symlinks:** a song id whose link-resolved path leaves the (link-resolved) library is refused on every mobile route. A library whose songs sit behind an in-library symlink or junction pointing elsewhere works on the desktop app but not on the mobile surface; point `DLC_DIR` at the real location instead.
| `GET /api/mobile/info` | `mobile.py` | `{"version": str, "origin": str, "user": str or null}`; `user` is the request's `Tailscale-User-Login` |
| `GET /api/mobile/songs` | `mobile.py` | see below |
| `GET /api/mobile/songs/{id:path}/status` | `mobile.py` | `{"state": "missing" or "building" or "ready" or "error", "version": str, "size_bytes": int or null, "error": str or null}` |
| `GET /api/mobile/songs/{id:path}/download` | `mobile.py` | builds the pack if needed (blocking; max 1 build at a time, a 300 s timeout, then 503), then a `FileResponse` of the `.fbm` file |

`/download` response headers:
- `Content-Type: application/octet-stream`
- `Content-Length`
- `Content-Disposition: attachment; filename="<safe>.fbm"`
- `ETag: "<version>"`
- `Cache-Control: private, no-store`

**Song id:** the path relative to `DLC_DIR`, which matches the core `filename`. In URLs the client encodes it as `encodeURIComponent(id)`, and the server accepts it through `{id:path}`. `/status` and `/download` accept only the canonical spelling: the link-resolved path relative to the link-resolved `DLC_DIR`. Aliases (`a/../x`, a trailing `/`, an in-library symlink to another song) get 404, and so does any id whose resolved path leaves the library.

### `GET /api/mobile/songs` response
```json
{"songs": [{"id": "Artist/song.feedpak", "title": "", "artist": "", "album": "",
            "duration": 215.3, "size_bytes": 12345678, "size_is_estimate": true,
            "version": "<40-hex sha1>", "has_stems": true,
            "arrangements": ["Lead", "Rhythm", "Bass"]}],
 "total": 1}
```
- Only songs that can be played: `.feedpak` and `.sloppak` files, plus loose folders.
- `version` is `sha1(f"{id}|{mtime}|{size}|{MOBILE_PACK_FORMAT}")`. `MOBILE_PACK_FORMAT` starts at `1`.
- The list caches versions: a single file by its own `(mtime, size)`, so the value is always current; a directory pack by the directory's own mtime for up to 60 s, so an in-place edit of a file inside a directory pack can take up to 60 s to show in the list. `/status` and `/download` always compute the version fresh.

## `.fbm` file format (version 1)
```
bytes 0..3   ASCII "FBMP"
bytes 4..7   uint32 big-endian N = header length in bytes
bytes 8..8+N UTF-8 JSON header
then         blob section; offsets in the header are relative to the start of this section
```
Header:
```json
{"format": 1, "id": "...", "version": "...", "title": "", "artist": "", "album": "",
 "duration": 0, "built_at": "ISO-8601",
 "arrangements": [{"index": 0, "name": "Lead", "messages": [ {...}, ... ]}],
 "files": [{"key": "full", "mime": "audio/mp4", "offset": 0, "length": 123}]}
```
- **`messages`** are exactly the JSON objects that `/ws/highway/{id}?arrangement=<index>` sends, in the same order, with `type: "loading"` messages dropped. They end with `{"type": "ready"}`.
- **Arrangements:** one entry per arrangement index, skipping drums arrangements.
- **File keys:**
  - `full` is the full mix.
  - `stem:<stem id>` is one per stem.
  - `audio` is the single track when there is no full mix or stems, e.g. a loose folder.
  - `cover` is the cover image, if present.
- **URL rewriting:** every audio URL inside `song_info` (`audio_url`, `full_mix_url`, `stems[].url`) is replaced with `fbm:<key>`, e.g. `"audio_url": "fbm:stem:guitar"`.
- **Audio** is transcoded to AAC in an MP4 container (`audio/mp4`): 160 kbps for `full` and `audio`, 128 kbps for stems. MP3 and M4A sources are copied as they are, with mime `audio/mpeg` or `audio/mp4`.
- **Builder limits:** 2 GB total output, 500 MB per file, and 600 s total build time.
- **Builder inputs:** audio comes only from audio members of the song's own package (same containment as the gate; for a zip the file must be in the unpack cache) or from `/audio/audio_*.{mp3,ogg,wav}`. Sources must have extension `.ogg .opus .oga .flac .wav .mp3 .m4a .aac`; ffmpeg runs with `-protocol_whitelist file -format_whitelist ogg,flac,wav,mp3,aac,mov` and `-fs` one byte over the per-file cap.
- **Scrubbing:** a non-empty `song_info.audio_error` is replaced with `"Audio unavailable"` before it is written into a pack.
- **Cache:** after each build, other versions of the same song are deleted unless touched in the last 120 s (a download may be starting); they are pruned on a later request. Packs are touched whenever they are handed out, and the whole cache is held under `FEEDBACK_MOBILE_CACHE_MAX_BYTES` by evicting the least recently touched packs, never one touched in the last 120 s.
- **Changes agreed during implementation:**
  - A single-mix pack carries its mixdown as stem id `full`, so its key is `stem:full` and `full_mix_url` is null. Players should use `full_mix_url`, falling back to `audio_url`.
  - `original_audio_url` is rewritten to `fbm:` too.
  - Cache file name: `mobile_cache/<sha1(id)>-<version>.fbm`.
  - A failed build returns HTTP 500 with a generic message.
  - All mobile routes accept HEAD.

## Frontend storage (IndexedDB database `feedback-mobile`, version 1)
- `songs` store, keyed by `id`: `{id, version, title, artist, album, duration, size_bytes, downloaded_at, arrangements: [...header.arrangements], files: [{key, mime, length}]}`
- `blobs` store, keyed by `` `${id}|${key}` ``: `{mime, data: ArrayBuffer}`. Not a `Blob`, because WebKit rejects Blobs in IndexedDB in private browsing and automation. `getBlob()` rebuilds the Blob.
