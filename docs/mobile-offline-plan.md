# Mobile offline tab player — plan

## Context
You want to use your iPhone as a tab player for your feedBack library. At home, it browses the full library on the MacBook Pro over Tailscale. Anywhere else, it plays songs you've downloaded to the phone, fully offline. Scoring, mic input, MIDI/drums and multiplayer are out of scope.

**Target:** core repo `~/Desktop/repos/feedback-upstream/feedBack`, branch `origin/custom` (3ddfcf4). The desktop fork's `custom` branch (release v0.4.10) bundles this. Work happens on a new branch, `feature/mobile-offline`, cut from `origin/custom` in its own worktree (`../feedBack-mobile`). That leaves your current checkout (`feature/build-a-band-stage`) untouched.

**Deliverable on approval:** the full plan below, written to `docs/mobile-offline-plan.md` on that branch. Build phases start one at a time, each after you sign off on the one before.

**Your decisions so far:**
- A standalone backend, not the desktop app's embedded server.
- Downloads include everything, stems too.
- Portrait plain-tab view deferred.
- The tailnet holds only your devices.
- The setup must be **easy for other feedBack users to repeat on their own Mac and tailnet, with their own devices**. Each person self-hosts their own library. Nobody joins anyone else's tailnet, and libraries are never shared. See section 8.
- **Security comes first.** The mobile server runs behind a strict allowlist gate, and every phase must pass a security gate before you sign off on it (section 9). The feature stays opt-in and out of tagged releases until phase 3's gate passes.

## Phase 1 findings (summary)
- **Backend:** FastAPI/uvicorn (`server.py`, `lib/routers/*`).
  - `main.py` reads `HOST`/`PORT`, which default to `0.0.0.0:8000`.
  - The desktop app spawns uvicorn on `127.0.0.1:18000`. That port can change, and the server only runs while the app is open (`feedBack-desktop/src/main/python.ts:34,680`).
- **Library:** a background scan of `DLC_DIR` feeds a SQLite index at `$CONFIG_DIR/web_library.db` (`lib/metadata_db.py`).
  - A song's id is its path relative to `DLC_DIR`.
  - `GET /api/library` and `GET /api/library/table` already exist.
- **Song packages:** `.feedpak`/`.sloppak` files are a zip or a folder. Inside are `manifest.yaml`, `arrangements/*.json` and `stems/*.ogg|mp3`. `lib/sloppak.py` parses them. `.psarc` files don't play.
- **Play path:**
  - `WS /ws/highway/{id}?arrangement=N` (`lib/routers/ws_highway.py:206`) streams JSON messages: `song_info` (with `audio_url`, `stems[]` and `full_mix_url`), then beats/sections/anchors/chord_templates/lyrics, notes and chords in chunks of 500, and finally `ready`.
  - `highway_ws()` only calls `websocket.accept()` and `websocket.send_json()`.
  - Audio comes from `/api/sloppak/{id}/file/{rel}` as a plain `FileResponse`.
  - There's no whole-song download endpoint.
- **Renderer:** `static/highway.js` exports `createHighway()` and draws in Canvas2D.
  - It runs standalone through `init(canvas, container)`, `connect(wsUrl, {secondary:true, onSongInfo})` and `setTime(t)`.
  - It needs an `<audio id="audio">` element on the page.
  - Its only data path is `new WebSocket(...)`.
- **Works behind a proxy, but isn't safe to expose as-is:** relative fetches, `wss:` derived from `location`, and no hardcoded hosts or ports mean the app runs behind `tailscale serve` unchanged. But it has no auth, no Origin or CSRF checks, and more than 40 routes that change state or run code (uploads, deletes, `git pull`, `rmtree`, settings import). Proxying the full app would expose all of them, so the mobile server is gated (section 9).
- **Gaps:**
  - No auth.
  - `/api/audio-local-path` (`lib/routers/media.py:87`) trusts loopback, and every request through serve arrives as 127.0.0.1.
  - No service worker or IndexedDB.
  - Wake Lock already exists in `js/session.js:297`.
  - Speed is `playbackRate` with `preservesPitch` (`app.js:700`).
- **ffmpeg:** the app bundles `ffmpeg` (`/Applications/feedback.app/Contents/Resources/bin`), and Homebrew's is also installed.
- **Tailscale:** not installed on this Mac.
  - `tailscale serve --bg` persists across reboots, but only once the Tailscale app starts at login.
  - HTTPS certificates publish machine names to public Certificate Transparency logs.
  - Funnel is off by default.
  - The docs don't cover WebSocket support or large-download limits, so acceptance tests check both.

---

## 1. Tailscale setup runbook
**You do these steps:**
1. Download **Tailscale Standalone** for macOS from tailscale.com/download/mac. Don't use the App Store build, and never install both. Open it and sign in. Enable "Launch at login".
2. Tailscale app → Settings → **CLI integration** → Install, which needs your admin password. Afterwards `/usr/local/bin/tailscale` exists.
3. Admin console → **Machines** → the Mac:
   - Rename it to something neutral, e.g. `mbp-home`, because the name becomes public in Certificate Transparency logs.
   - Turn off "Auto-generate from OS hostname".
   - Keep key expiry **on** (the 180-day default). Re-authenticating doesn't change the hostname, and keys that never expire are a standing risk.
4. Admin console → **DNS**:
   - **Before** enabling HTTPS, optionally switch the tailnet name to a randomized one (Rename tailnet). It becomes public along with the certificate, and changing it later breaks the phone's offline data.
   - Enable **MagicDNS**.
   - Under HTTPS Certificates, choose **Enable HTTPS** and acknowledge the warning.
   - After this, never rename the tailnet.
5. iPhone: install Tailscale from the App Store, sign in to the same account, and turn the VPN on. Optionally enable VPN On Demand: profile → VPN On Demand. Turn on App Store auto-updates.
6. **Harden the tailnet** (full checklist in section 9.1): MFA on your sign-in provider, the restrictive access policy, Send Files off, auto-updates on (client 1.104.4 or later), and device approval or Tailnet Lock.

**I can do these steps (with your OK):**
7. Start the gated mobile backend on `127.0.0.1:8765` (see section 2), a port of its own so serve can never point at an ungated server.
8. `tailscale set --ssh=false --advertise-exit-node=false --advertise-routes=`, then `tailscale serve --bg 8765`.
9. Verify:
   - `tailscale serve status`: it should show `https://mbp-home.<tailnet>.ts.net → http://127.0.0.1:8765`, and nothing else.
   - `tailscale funnel status`: it should report no Funnel.
   - `lsof -iTCP -sTCP:LISTEN -n -P | grep -E '8765|18000|8000'`: every listener is bound to `127.0.0.1`.
   - The security probe script (section 9.4) passes.

**You verify from the phone:**
10. In Safari, open `https://mbp-home.<tailnet>.ts.net/mobile`. A connection-test page loads with a valid lock icon and streams one song's notes over the WebSocket.
11. Open `https://mbp-home.<tailnet>.ts.net/` (the full desktop UI). It returns 404, which proves the gate is on.
12. Turn the iPhone's Tailscale VPN off and reload. It should fail. That shows the server is private.

**Never:** `tailscale funnel`, port-forwarding, binding any feedBack server to `0.0.0.0`, or pointing serve at the desktop app's port or at any server without the gate.

## 2. Keeping it running
- **Separate config:** the standalone backend gets its own `CONFIG_DIR` (`~/Library/Application Support/feedback-mobile`) and points at the same `DLC_DIR` as the desktop app.
  - This gives it its own SQLite index and caches, so two processes never write to one database.
  - It reads the library and never writes to it.
  - It doesn't share favorites or settings with the desktop app, and the phone doesn't need them.
  - Step one of build phase 1 is reading the desktop app's `DLC_DIR` from its config (`python.ts`).
- **New `scripts/mobile/run-server.sh`:**
  - Sets `HOST=127.0.0.1`, `PORT=8765`, `FEEDBACK_MOBILE_MODE=1`, `FEEDBACK_MOBILE_ORIGIN`, `FEEDBACK_MOBILE_ALLOWED_USERS`, `CONFIG_DIR` and `DLC_DIR`.
  - Refuses to start unless `HOST` is loopback and mobile mode is on.
  - Puts the bundled ffmpeg on `PATH`.
  - Runs `caffeinate -s python main.py`.
  - During development it runs from the worktree's `.venv`. Once mobile ships in a release, switch it to the app's bundled Python and code (`/Applications/feedback.app/Contents/Resources/{python,slopsmith}`), so it updates with each app release.
- **New `scripts/mobile/app.feedback.mobile.plist` (template, filled in by `setup.sh`):** a LaunchAgent with `RunAtLoad` and `KeepAlive`, logging to `~/Library/Logs/feedback-mobile.log`.
  - I install it into `~/Library/LaunchAgents` and load it with `launchctl bootstrap gui/$UID …`, only with your OK.
- **Serve:** `tailscale serve --bg` already persists, so it needs no LaunchAgent.
- **Sleep — recommendation: `caffeinate -s` around the server.**
  - It keeps the Mac awake only while the server runs **and** the Mac is on AC power.
  - It changes no system setting, and stopping the agent undoes it.
  - Caveats:
    - A closed lid still sleeps unless an external display is connected.
    - On battery the Mac sleeps normally.
    - After a reboot, nothing is reachable until you log in, because Tailscale's GUI only runs after login.
  - The alternative is System Settings → Battery → Options → "Prevent automatic sleeping on power adapter when display is off". It is global and always on, so I don't recommend it.

## 3. Backend changes (new `lib/routers/mobile.py`, plus a one-line `include_router` in `server.py`)
- **`GET /api/mobile/songs`:**
  - Returns `[{id, title, artist, album, duration, size_bytes, version, has_stems, arrangements}]`, read from `metadata_db`. It reuses the `/api/library/table` query.
  - `size_bytes` is the built pack's size when it's cached, or an estimate from the source size when it isn't.
  - `version` is `sha1(id | mtime | size | MOBILE_PACK_FORMAT)`. That's cheap and changes whenever the song file or pack format changes.
- **`GET /api/mobile/songs/{id:path}/download`:** returns one `.fbm` file per song.
  - It builds the pack on first request and caches it at `$CONFIG_DIR/mobile_cache/<version>.fbm`.
  - It returns a `FileResponse` with these headers:
    - `Content-Type: application/octet-stream`
    - `Content-Length`, so the phone can show progress
    - `Content-Disposition`
    - `ETag: version`
    - `Cache-Control: private, no-store`
  - Paths go through `lib/dlc_paths._resolve_dlc_path` to block traversal.
- **`GET /api/mobile/songs/{id:path}/status`:** reports the build state, because transcoding stems takes several seconds the first time.
- **Building the pack:**
  - For each arrangement index (skipping drums), call the existing `highway_ws()` with a recorder object that implements `accept()` and `send_json()`, and collect the messages. This reuses the exact data the renderer already consumes, with **no edits to `ws_highway.py`**.
  - Drop the `loading` messages.
  - Rewrite `audio_url`, `full_mix_url` and `stems[].url` to pack keys: `fbm:full` and `fbm:stem:<id>`.
  - Transcode the full mix and each stem to AAC `.m4a` with ffmpeg: 160k for the full mix, 128k for stems. Reuse the ffmpeg lookup in `lib/audio.py`. Copy mp3/m4a as-is.
  - Add the cover image (`sloppak.read_cover_bytes`).
- **`.fbm` format** (our own, so it needs no unzip dependency):
  - The file starts with `FBMP` and a `uint32` header length.
  - Then comes a header JSON: `{format, id, version, title, artist, duration, arrangements:[{index, name, messages}], files:[{key, mime, offset, length}]}`.
  - Then the raw file blobs, back to back.
- **Mobile shell routes:** `/mobile`, `/mobile/sw.js` and `/mobile/manifest.webmanifest`.
  - The service worker must be served from `/mobile/` so its scope covers the app.
  - `sw.js` has the app `VERSION` plus a hash of the files it precaches substituted in, so a new deploy busts its cache.
- **Security:** the mobile-mode gate (section 9.2) does the heavy lifting. Defense in depth: `/api/audio-local-path` also treats any request carrying `Tailscale-*` or `X-Forwarded-For` headers as remote (two-line edit in `media.py`).

## 4. Phone app design (new `static/mobile/`: `index.html`, `mobile.js`, `mobile.css`, `fbm.js`, `store.js`, `player.js`, icons)
- **A lightweight shell that skips the full v3 UI and plugin loader.** It imports `/static/highway.js` and its `js/highway-*.js` helpers, nothing else. It never loads `app.js`, which calls dozens of other endpoints; it stubs `window.feedBack` instead.
- **No HTML injection:** all song metadata goes into the page with `textContent`, never `innerHTML`. The page ships with a strict CSP (`default-src 'self'; connect-src 'self'; media-src 'self' blob:; img-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'`).
- **Tabs:**
  - **Library** (online): search, sort by artist or title, a **Stream** button that plays live from the server, and a **Download** button.
  - **Downloaded** (offline): each song's size, **Play** and **Delete**, plus a storage meter from `navigator.storage.estimate()`.
- **Download:**
  - `fetch` plus a `ReadableStream` reader drives a progress bar from `Content-Length`.
  - It parses the `.fbm` file and stores it in IndexedDB:
    - `songs` holds the header and messages, keyed by id.
    - `blobs` holds each audio or cover `Blob`, keyed by `id|key`.
  - It's a single write transaction, so a half-finished download is never visible.
  - On first download it calls `navigator.storage.persist()`.
- **Player** (`player.js`):
  - A canvas, a hidden `<audio id="audio">` and `createHighway()`. It calls `h.init(canvas, container)` and `h.connect(url, {secondary:true, onSongInfo})`.
  - **Offline:** a `FakeWebSocket` class assigned to `window.WebSocket` only while `connect()` runs. It replays the stored messages through `onmessage`, with a `.url` and `close()`, and rewrites `fbm:` keys to `URL.createObjectURL(blob)`. Core stays unchanged.
  - **Online stream:** the real `wss://…/ws/highway/{id}`.
  - **Clock:** `requestAnimationFrame` drives `h.setTime(audio.currentTime)`.
  - Arrangement picker, play/pause, and a ±5s scrub.
- **Audio on iOS:**
  - A full-screen **"Tap to start"** button calls `audio.play()` inside the gesture.
  - Set `navigator.audioSession.type = 'playback'` where supported, so the silent switch doesn't mute playback.
  - Speed: 50–100% presets plus a slider, using `playbackRate` with `preservesPitch` and `webkitPreservesPitch`. Tested on the device.
- **Stems:**
  - The full mix is the default track, played from one `<audio>` element. That keeps speed control and sync rock-solid.
  - A stem mixer (per-stem mute and volume) is a **phase 4 spike**:
    - (a) One `<audio>` per stem, each routed through `MediaElementSource` → gain, with drift correction against the lead stem every 250 ms.
    - (b) If drift or pitch preservation fails on iOS, fall back to full mix only. Stems are still downloaded, so a later fix needs no re-download.
- **Screen:**
  - Wake Lock while playing. Reuse the pattern from `js/session.js`, and re-acquire it on `visibilitychange`.
  - Landscape highway. In portrait, show a "rotate your phone" overlay, since iOS web apps can't lock orientation.
  - Cap the canvas at DPR 2 for performance.
- **Service worker** (`/mobile/sw.js`):
  - Precaches the shell, `highway.js` and all of its imports, CSS and fonts, under a versioned cache name.
  - Serves cache-first for the shell and network-only for `/api` and `/ws`.
  - Song data lives in IndexedDB, not the Cache API.
- **Manifest:** `start_url: /mobile/`, `display: standalone`, landscape hint, and icons. Add the app to the home screen **before** downloading anything, because home-screen apps on iOS have their own storage, separate from Safari.

## 5. Parsing — recommendation: pre-convert on the server
Don't port sloppak parsing to JavaScript. The server already turns sloppaks into renderer messages, and `highway_ws()` can be reused as-is through the recorder. That gives one source of truth, no duplicated parser that drifts from core, and no client-side zip or YAML dependencies. The phone stores and replays exactly what the renderer already understands. The cost is that downloads need the Mac reachable, which is acceptable because downloading needs the network anyway.

## 6. Build phases and acceptance tests (run on the phone)
**Phase 1 — Gated HTTPS serve:** runbook steps 1–12, tailnet hardening, the mobile-mode gate, the standalone server, the LaunchAgent and the `audio-local-path` guard.
- ✅ `/mobile` loads with a valid certificate, and its connection test streams a song over the WebSocket.
- ✅ `/`, `/api/settings` and `/api/songs/upload` return 404 through serve.
- ✅ The section 9.4 probe script passes.
- ✅ It fails with the iPhone VPN off.
- ✅ `tailscale funnel status` shows nothing.
- ✅ After a Mac reboot and login, the site comes back without manual steps.

**Phase 2 — Song list and Library tab:** `/api/mobile/songs`, the `/mobile` shell and Stream playback.
- ✅ `/mobile` lists the whole library, and search works.
- ✅ Tapping Stream plays a song in landscape with the speed slider at 70%, pitch preserved.
- ✅ Add to Home Screen opens full-screen.

**Phase 3 — Download, IndexedDB and offline playback:** the `.fbm` builder, the download endpoint, the IndexedDB store, `FakeWebSocket` and the service worker.
- ✅ Download a stems song and a non-stems song; the progress bar moves.
- ✅ Turn on airplane mode, force-quit the app and reopen it from the home screen. It loads, and the Downloaded tab plays both songs with every arrangement switchable.
- ✅ Delete a song and the storage meter drops.

**Phase 4 — Polish:**
- An offline indicator.
- An "update available" badge when a song's `version` changes.
- Re-download and "delete all".
- A storage meter with a persisted/not-persisted indicator.
- Service worker update prompts.
- The stem mixer spike.
- ✅ Edit a song on the Mac, and the phone shows its update badge.
- ✅ Deploy a new build, and the app offers to reload.
- ✅ Stem mute works in sync (or the fallback is documented).

**Every phase ends with the security gate in section 9.3.** You sign off only after it passes.

**Phase 5 (deferred):** a portrait plain-tab view. It needs alphaTab, which is a new dependency, or a custom renderer. Ask before starting.

**Tests:**
- pytest for `mobile.py`: song list shape, pack build and its header round-trip, a stable `version`, path traversal rejected, and the `audio-local-path` header guard.
- A JS unit test (`npm run test:js`) for the `fbm.js` parser and `FakeWebSocket` replay.
- Existing suites stay green.

## 7. Risks and open questions
- **iOS storage:**
  - Home-screen web apps aren't subject to Safari's 7-day eviction of storage.
  - The quota is a large share of free disk.
  - `persist()` should be granted.
  - The phone shows free space, and a download refuses to start if it won't fit.
- **Size:** stems make packs larger, roughly 15–40 MB per song with AAC at 128k. 50 songs could reach about 1–2 GB. The storage meter and per-song sizes make that visible.
- **Unverified Tailscale behavior:** whether serve passes WebSockets and handles 40 MB+ downloads isn't documented. Phase 1 and phase 3 tests cover both. If downloads stall, fall back to Range-resumable chunked fetches.
- **Renderer performance:** Canvas2D at DPR 3 on a phone. Cap DPR at 2 and check for smooth playback on long songs. The 3D highway plugin is out of scope.
- **iOS Wake Lock:** home-screen apps had a Wake Lock bug before iOS 18.4. Test on your iOS version, and if it's broken, show "set Auto-Lock to Never while playing".
- **Pitch preservation on iOS:** behavior with `playbackRate` below 0.5 isn't known yet. Test the presets.
- **Hostname or tailnet name changes:** offline data is tied to the origin. A new name means an empty app and re-downloading everything. Mitigations: pin the machine name, disable key expiry, and never rename the tailnet. It's documented in the runbook.
- **Reachability:** the Mac must be awake, on AC power and logged in for Library, Stream and Download. Offline play needs none of that.
- **No app-level auth in core:** covered for the mobile server by three layers: the Tailscale access policy, the identity allowlist and the route gate (section 9). A stolen, unlocked phone can still reach the server until you remove it from the Machines page.
- **Upstream merges:** all new files plus three small edits (`server.py` include, `media.py` guard, `.gitignore`), so conflicts with upstream stay minimal.

## 8. Making it repeatable for other users
- **Nothing personal in the repo:**
  - The LaunchAgent label is generic (`app.feedback.mobile`), not `com.mattromano.*`.
  - No hostnames or paths are hardcoded.
  - The scripts read everything from an env file at `~/.config/feedback-mobile/env`, which holds `DLC_DIR`, `PORT` and `CONFIG_DIR`.
- **Two guides:**
  - `docs/mobile-offline-plan.md` is the dev plan.
  - `docs/mobile-setup.md` is a short user guide anyone can follow, using placeholders like `<your-mac>.<your-tailnet>.ts.net` and the same runbook steps.
- **`scripts/mobile/setup.sh`:** one interactive, idempotent command that is safe to re-run.
  1. Checks for the Tailscale CLI. If it's missing, it prints the install link and stops.
  2. Checks whether this Mac is logged in to Tailscale, using `tailscale status`.
  3. Detects `DLC_DIR` from the desktop app config, or asks for it.
  4. Writes the env file.
  5. Installs and loads the LaunchAgent, after asking first.
  6. Runs `tailscale serve --bg $PORT`.
  7. Checks that Funnel is off and refuses to continue if it's on.
  8. Prints the phone URL and a QR code in the terminal: the Mac's `qrencode` if present, otherwise just the URL.
  - `setup.sh --uninstall` reverses all of it (agent, serve config, env file).
- **Device auth stays with Tailscale:** each person signs in to Tailscale on their own Mac and phone with their own account, and those devices are the only ones that can reach that Mac.
- **Identity allowlist, on by default:** `setup.sh` reads the signed-in login from `tailscale status --json` and writes it to `FEEDBACK_MOBILE_ALLOWED_USERS`. The gate rejects any request whose `Tailscale-User-Login` isn't on that list, and rejects requests with no identity header at all. Users with other people on their tailnet add logins on purpose. The header can't be forged, because the backend binds to loopback and serve strips any client-supplied copy.
- `setup.sh` also prints the hardening checklist (section 9.1) and runs the probe script (section 9.4) at the end.
- **Settings page (Phase 4):** shows the mobile URL and serve status read-only, so users can find the link without the terminal. It's a small panel inside `/mobile`, not an edit to an existing plugin.

## 9. Security design and gates
A review of the design (Oct 31, 2026) covered Tailscale hardening and every backend route. Key facts behind this section:
- Core has 40+ routes that write or run code. Many of them can be triggered cross-site (CSRF) with no preflight: song upload, `git stash`/`git pull` on plugins, folder `rmtree`/move/rename, rescans, and every GET that has side effects.
- Neither WebSocket checks the Origin, so any website open on a tailnet device can read `/ws/highway` and trigger ffmpeg or unpack work.
- Stored XSS sinks exist in the full UI (`static/app.js:654`, `static/js/section-practice.js:908`).
- `main.py` binds `0.0.0.0` by default.
- `/art/url` has an SSRF guard that misses 100.64.0.0/10, which is the tailnet's range (`lib/routers/art.py:406`).
- Tailscale bulletin TS-2026-016 (Oct 26, 2026) covered a serve bug fixed in client 1.104.4.

Conclusion: never expose the full app. Expose only a tiny read-only surface, behind layered checks.

### 9.1 Tailnet hardening checklist (you, in the admin console; `setup.sh` prints it)
1. **MFA** on the identity provider you sign in to Tailscale with.
2. **Access policy:** replace the default allow-all policy. Tag the Mac `tag:feedback-host`, owned by you. Grant only your user → `tag:feedback-host` on `tcp:443`. Nothing grants access *to* the phone. Include `tests` that accept `mac:443` and deny `mac:22`, `mac:8765`, `mac:18000` and phone:443. Keep the phone user-owned (not tagged) so serve sends its identity header.
3. **Funnel:** no `funnel` nodeAttr in the policy; `tailscale funnel reset`; `tailscale funnel status` empty.
4. **Off:** Tailscale SSH (`tailscale set --ssh=false`), subnet routes, exit nodes, app connectors, and Send Files (Taildrop) under Settings → General.
5. **Don't** turn on shields-up on the Mac (it blocks serve).
6. **Device approval** (simpler) **or Tailnet Lock** (stronger: stops a compromised coordination server from adding nodes, but you must store the disablement secrets in a password manager, or risk being locked out). You choose one.
7. **Auto-update** on both clients, at version 1.104.4 or later. Subscribe to https://tailscale.com/security-bulletins/index.xml.
8. **Key expiry** stays on. Remove old or unknown machines. Review the configuration audit log (Admin → Logs) after setup.
9. Neutral machine name, and a randomized tailnet name before enabling HTTPS (Certificate Transparency makes both public; IPs and traffic stay private).

### 9.2 Mobile-mode gate (`lib/mobile_gate.py`, active only when `FEEDBACK_MOBILE_MODE=1`)
- **Implementation:** a pure ASGI middleware, registered outermost, that handles both the `http` and `websocket` scopes. `@app.middleware("http")` never sees WebSocket upgrades, so it can't be used.
- **Methods:** only `GET`/`HEAD`. Everything else gets 405.
- **Path allowlist** (anchored regexes; everything else gets 404, including `/`, `/v3`, `/api/settings*` and all plugin routes):
  - `/mobile`, `/mobile/*`
  - `/api/mobile/*`, which is read-only
  - the exact `/static/*` files the page needs, as an explicit list rather than a prefix
  - `WS /ws/highway/*`
  - `/api/sloppak/{fn}/file/*`
  - `/audio/*`
  - `/api/song/{fn}/art`
- **Identity:** require `Tailscale-User-Login` ∈ `FEEDBACK_MOBILE_ALLOWED_USERS`. A missing header is rejected, which also blocks any local process that skips serve.
- **Origin:** when `Origin` is present (always on a WebSocket upgrade from a browser), it must equal `FEEDBACK_MOBILE_ORIGIN` (`https://<mac>.<tailnet>.ts.net`). Reject `Sec-Fetch-Site: cross-site`. Together these block CSRF, cross-site WebSocket hijacking and hotlinking from other websites.
- **Host:** must equal the serve hostname or `127.0.0.1:8765`. This blocks DNS rebinding.
- **Limits:**
  - at most 2 concurrent `/ws/highway` connections and 1 pack build at a time, with per-user rate limits
  - pack builds capped on total uncompressed size and per-file size (zip-bomb guard)
  - download builds time out
- **Headers:** responses carry `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer` and `Cache-Control: private`. `/mobile` adds the CSP from section 4.
- **Startup checks:** the gate refuses to start the app if `HOST` isn't loopback or the allowlist or origin is empty. A misconfigured setup fails closed.
- **No core route changes.** The gate wraps the existing app, so the desktop app's behavior is untouched.

### 9.3 Security gate at the end of every phase (before you sign off)
1. **Automated tests** (`tests/test_mobile_gate.py`):
   - Every route in the app's route table that isn't allowlisted returns 404/405 under mobile mode. The test is generated from `app.routes`, so new core routes are covered automatically.
   - A WebSocket upgrade with a foreign Origin is rejected.
   - A missing or foreign identity header is rejected.
   - The traversal ids `../`, `%2e%2e/` and absolute paths are rejected.
   - The pack size caps hold.
2. **`/security-review`** on the phase's diff, plus an independent adversarial review by a subagent that gets only the diff and threat model and is asked to break it. Findings are fixed or written down as accepted risks with your OK.
3. **Probe script** (section 9.4) passes against the live serve URL.
4. **Phone checks** from that phase's acceptance list.

### 9.4 Probe script (`scripts/mobile/probe.sh`, read-only, run from the Mac)
- **Tailscale state:** `tailscale serve status --json` shows exactly one mapping, to `127.0.0.1:8765`. `tailscale funnel status` is empty. `tailscale get ssh` is false. The client version is 1.104.4 or later.
- **Listeners:** `lsof` shows no feedBack listener outside loopback.
- **Gate checks over the serve URL:**
  - `/`, `/api/settings` and `POST /api/songs/upload` are refused.
  - `/mobile` returns 200 with the CSP header.
  - A WebSocket opened with `Origin: https://evil.example` is refused.
- Prints PASS/FAIL per check, and exits non-zero on any failure.

### 9.5 Release policy
- The feature is opt-in: nothing runs or is exposed unless a user runs `setup.sh`.
- It isn't part of a tagged desktop release until phase 3's security gate passes.

## 10. Pre-existing core issues found by the review (outside this feature)
These affect the desktop app and Docker today, whether or not mobile ships. The gate keeps them out of the mobile server, but they deserve their own fix PR:
- **CSRF-able writes:**
  - `POST /api/plugins/{id}/update` (`plugins/__init__.py:2255`) runs `git stash` and `git pull` with no body.
  - The folder_library create/rename/delete/move routes use raw `request.json()` (`plugins/folder_library/routes.py:303`), so a `text/plain` body works.
  - `POST /api/songs/upload` takes a multipart form.
- **No Origin check on `/ws/highway` and `/ws/sync`, and no `TrustedHostMiddleware`.** This is the DNS-rebinding risk on 127.0.0.1.
- **XSS:** unescaped title in the Retune modal (`static/app.js:654`), and `esc()` used inside attributes (`section-practice.js:908`, `library.js:1036`).
- **SSRF:** `/art/url` doesn't block 100.64.0.0/10 (`art.py:406`; use `not ip.is_global`).
- **Defaults and dependencies:**
  - `main.py` defaults to `HOST=0.0.0.0`.
  - The FastAPI version isn't pinned (`fastapi>=0.110.0`), so `strict_content_type` protection depends on which version gets installed.
  - The sloppak zip unpack has no size cap.

## Files
- **New:** `lib/routers/mobile.py`, `lib/mobile_gate.py`, `lib/mobile_pack.py`, `scripts/mobile/probe.sh`, `tests/test_mobile_gate.py`, `static/mobile/*`, `scripts/mobile/run-server.sh`, `scripts/mobile/app.feedback.mobile.plist` (template), `scripts/mobile/setup.sh`, `tests/test_mobile.py`, `tests/js/fbm.test.js`, `docs/mobile-offline-plan.md`, `docs/mobile-setup.md`.
- **Edited:** `server.py` (one `include_router`, plus registering the gate when `FEEDBACK_MOBILE_MODE=1`), `lib/routers/media.py` (guard), `requirements.txt` (FastAPI lower bound raised to a version with `strict_content_type`).
- **Reused:**
  - `ws_highway.highway_ws`
  - `metadata_db` library query (from `library.py` `/api/library/table`)
  - `dlc_paths._resolve_dlc_path`
  - `sloppak.read_cover_bytes`
  - the ffmpeg helper in `lib/audio.py`
  - `createHighway`
  - the Wake Lock pattern in `js/session.js`
- **No new dependencies.**

