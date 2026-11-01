# feedBack mobile offline player: setup guide

Play your feedBack library on your iPhone: browse and stream it at home, and download songs to the phone to play with no connection at all. You host everything yourself, on your own Mac and your own Tailscale account.

Developer notes and the full design are in `docs/mobile-offline-plan.md` and `docs/mobile-contract.md`.

## 1. What it is, what you need, and the privacy model

**What you get:** a web app (a home-screen "app" on your iPhone) with three tabs.

- **Library:** your whole library from the Mac, with search and sort by artist or title. Each song has **Stream** (play live from the Mac) and **Download** (copy to the phone). Songs you already have show a "Downloaded" or "Update available" badge.
- **Downloaded:** songs stored on the phone, each with **Play**, **Delete** and an **Update** button if the Mac copy changed. It also has a storage meter and **Delete all downloads**.
- **Settings:** the server address, the Tailscale login you are signed in as, the server version, offline-cache status, the 3D/2D highway choice and **Check for app update**.

Scoring, microphone input, MIDI/drums and multiplayer are not part of the mobile player.

**What you need**

- A Mac with the feedBack desktop app and your song library on it.
- An iPhone on iOS 17 or later.
- A free Tailscale account (the same account on both devices).
- The feedBack repo checked out on the Mac, with a Python environment that has feedBack's requirements installed (`fastapi`, `uvicorn`, and the rest of `requirements.txt`).
- `ffmpeg` is used to build download packs. The setup uses the copy bundled with the desktop app (`/Applications/feedback.app/Contents/Resources/bin`) if it exists, otherwise whatever is on your `PATH`.

**Privacy model**

- The server is reachable **only inside your tailnet**. It listens on `127.0.0.1:8765` and Tailscale Serve publishes it on your tailnet. **Tailscale Funnel (the public internet) is never used**, and setup refuses to continue if Funnel is on.
- It is not the full feedBack app. A separate server runs behind a strict gate that only allows a small, read-only set of requests: the mobile page, the song list, song downloads, playback streams, audio, cover art and the highway renderer files. Everything else (the desktop UI, settings, uploads, plugins, deletes) returns 404 or 405.
- Every request must carry your Tailscale identity, and only the logins on an allowlist are accepted. Setup puts your own login on it.
- Your library never leaves your devices. The only copies are songs you choose to download to your own phone.

## 2. Install Tailscale on the Mac and the iPhone

1. **Mac:** download **Tailscale Standalone** from https://tailscale.com/download/mac. Do not use the Mac App Store build, and do not install both. Open it, sign in, and enable **Launch at login**.
2. **Mac:** in the Tailscale app open **Settings -> CLI integration** and click **Install**. It asks for your admin password. Check it worked:

   ```bash
   tailscale version
   ```

   You need version **1.104.4 or later** (older versions have a Tailscale Serve security bug). Setup refuses to run on older clients.
3. **iPhone:** install **Tailscale** from the App Store, sign in with **the same account**, and turn the VPN on. Turn on App Store auto-updates for it.
   - Optional: in the Tailscale app, open your profile and enable **VPN On Demand**, so the VPN connects by itself when you open the web app.

## 3. Harden your tailnet (admin console)

Do this once, at https://login.tailscale.com/admin. `setup.sh` prints a short version of this checklist at the end; here it is in full.

1. **Turn on MFA** with the identity provider you sign in to Tailscale with (Google, Microsoft, Apple, GitHub, and so on). This is done in that provider's account settings, not in Tailscale.
2. **Replace the default allow-all access policy.** Go to **Access controls** and edit the policy. The sample below lets only your user reach your Mac, only on HTTPS (port 443). Nothing may reach the phone. Replace the placeholders:
   - `you@example.com` with the login you use for Tailscale.

   ```json
   {
     "tagOwners": {
       "tag:feedback-host": ["you@example.com"]
     },
     "grants": [
       {
         "src": ["you@example.com"],
         "dst": ["tag:feedback-host"],
         "ip":  ["tcp:443"]
       }
     ],
     "tests": [
       {
         "src": "you@example.com",
         "accept": ["tag:feedback-host:443"],
         "deny": [
           "tag:feedback-host:22",
           "tag:feedback-host:8765",
           "tag:feedback-host:18000"
         ]
       }
     ]
   }
   ```

   Then tag the Mac: **Machines -> your Mac -> ... menu -> Edit ACL tags -> `tag:feedback-host`**.

   Notes:
   - Keep the **phone user-owned, not tagged**. Tailscale Serve only sends the identity header (which the gate requires) for user-owned devices.
   - Do not add a rule that grants access *to* the phone.
   - Add a test that denies your user reaching the phone itself on port 443 if your policy editor lets you name the phone in a test.
   - If you use Tailscale's visual policy editor instead of JSON, make the same rules: one grant, source your user, destination the tag, port 443 TCP only.
3. **Funnel stays off.** Do not add a `funnel` node attribute to the policy. After setup, this must print nothing about Funnel being on:

   ```bash
   tailscale funnel status
   ```
4. **Switch off what you don't use:**
   - Tailscale SSH, subnet routes and exit-node advertising on the Mac (setup offers to do this for you with `tailscale set`).
   - **App connectors** (admin console -> **Apps**, or in the policy).
   - **Send Files (Taildrop)**: in the Tailscale app, **Settings -> General**.
5. **Do not turn on shields-up** on the Mac. It blocks incoming connections, including Serve.
6. **Pick one of these two:**
   - **Device approval** (simpler): admin console -> **Settings -> Device management -> Device approval**. New devices must be approved by you before they join.
   - **Tailnet Lock** (stronger): stops even a compromised Tailscale coordination server from adding devices. **Warning:** enabling it prints *disablement secrets*. Store them in a password manager. If you lose them and the lock-signing devices, you can be locked out of your own tailnet.
7. **Auto-update** both Tailscale clients (Mac: Tailscale **Settings -> General -> Automatically install updates**, if offered by your build; iPhone: App Store auto-updates). Subscribe to Tailscale's security bulletins: https://tailscale.com/security-bulletins/index.xml (add it to any RSS reader).
8. **Keep key expiry on** (the default; **Machines -> your Mac -> ... -> Disable key expiry** stays *unchecked*). Remove old or unknown machines from the **Machines** page. After setup, look at **Logs -> Configuration audit logs** to confirm only changes you made.
9. **Names:** see section 4. This has to happen before you enable HTTPS.

## 4. Choose a neutral machine name, then enable MagicDNS and HTTPS

Tailscale HTTPS certificates are issued by a public certificate authority, and every certificate is recorded in public **Certificate Transparency (CT) logs**. Anyone can search those logs. That means **your machine name and your tailnet name become public** once you enable HTTPS. Your IP addresses and your traffic stay private, but pick names you don't mind being public.

Do these in order:

1. **Rename the Mac** to something neutral that does not identify you (for example `home-mac-7`, not `janes-macbook`): admin console -> **Machines** -> your Mac -> **...** -> **Edit machine name**. Turn off **Auto-generate from OS hostname**, otherwise the name can change back.
2. **Optional but recommended: randomize the tailnet name.** Admin console -> **DNS** -> **Rename tailnet**. Choose a randomized name (Tailscale offers one). Do this **before** enabling HTTPS.
3. Admin console -> **DNS**:
   - Turn on **MagicDNS**.
   - Under **HTTPS Certificates**, click **Enable HTTPS** and acknowledge the warning.

Your address is now:

```
https://<your-mac>.<your-tailnet>.ts.net/mobile/
```

**Never rename the Mac or the tailnet afterwards.** Offline downloads on the phone are stored per web address. If the address changes, the phone app starts out empty and you must download everything again. Old CT log entries also stay public.

## 5. Run the setup script

From the feedBack repo on the Mac:

```bash
bash scripts/mobile/setup.sh --dry-run
```

This prints what would happen and changes nothing. When it looks right, run it for real:

```bash
bash scripts/mobile/setup.sh
```

It is safe to re-run at any time (for example after moving your library).

**Desktop, Documents and Downloads:** macOS stops background services from reading these folders. If your feedBack checkout is in one, `setup.sh` copies the server code to `~/Library/Application Support/feedback-mobile/app` and runs it from there, so re-run `setup.sh` after pulling changes. It uses the desktop app's bundled Python rather than a venv inside those folders. Your song library must live outside them; if it is inside one, setup stops and asks you to move it.

**What it checks first**

- The Tailscale CLI exists (otherwise it prints the install link and stops).
- Tailscale is running and signed in, the Mac has a MagicDNS name, and your login can be read. It uses that login as the first allowed user.
- The client is 1.104.4 or later.
- Funnel is off. If it is on, setup stops (run `tailscale funnel reset` and try again).

**What it asks you**

| Prompt | Notes |
|---|---|
| Song library folder (`DLC_DIR`) | Detected from the desktop app's config or the default library folders. Press Enter to accept, or type the folder. |
| Python with feedBack's requirements (`FEEDBACK_PYTHON`) | Detected from `.venv` in the repo. It must be able to import `fastapi` and `uvicorn`. |
| Install and (re)start the LaunchAgent? | Default is no. Answer `y` to keep the server running. |
| Run `tailscale serve --bg 8765`? | Default is no. Answer `y` to publish it on your tailnet (never Funnel). If Serve already has other mappings, it shows them and warns that adding ours replaces what is served at the root. |
| Turn off Tailscale SSH, exit-node advertising and advertised routes? | Runs `tailscale set --ssh=false --advertise-exit-node=false --advertise-routes=`. |

**What it installs and where**

| Item | Location |
|---|---|
| Settings file (owner-only, mode 600) | `~/.config/feedback-mobile/env` |
| LaunchAgent `app.feedback.mobile` | `~/Library/LaunchAgents/app.feedback.mobile.plist` |
| Server log | `~/Library/Logs/feedback-mobile.log` |
| Mobile server data (library index, download packs) | `~/Library/Application Support/feedback-mobile` |
| Tailscale Serve mapping | `https://<your-mac>.<your-tailnet>.ts.net/` to `http://127.0.0.1:8765` |

The mobile server uses its **own** data folder and only reads your library. It does not share favorites or settings with the desktop app. The port (8765) and bind address (loopback only) are fixed.

At the end it prints the hardening checklist, the phone URL (plus a QR code if `qrencode` is installed, `brew install qrencode`), and runs the security probe.

**Settings file**

`~/.config/feedback-mobile/env` holds simple `KEY=VALUE` lines. The ones you might edit:

- `DLC_DIR`: your library folder.
- `FEEDBACK_MOBILE_ALLOWED_USERS`: comma-separated Tailscale logins allowed to use the server.
- `FEEDBACK_MOBILE_HOSTS`: optional extra allowed `Host` values.

The file must be a regular file (not a symlink), owned by you, and not writable by anyone else, or the server refuses to start. Re-running `setup.sh` keeps extra logins you added by hand, as long as your own login is still on the list. After editing the file, restart the server:

```bash
launchctl kickstart -k gui/$(id -u)/app.feedback.mobile
```

**Removing everything**

```bash
bash scripts/mobile/setup.sh --uninstall
```

It asks before each step: stop and remove the LaunchAgent, remove the Serve mapping (only if it is the one setup created; otherwise it tells you the command to remove it by hand), and delete the settings file. It leaves the server data folder and log file for you to delete if you want. It does not undo `tailscale set` or any admin console change.

## 6. Verify

On the Mac, run the probe. It is read-only and prints PASS or FAIL per check; it exits non-zero if anything fails:

```bash
bash scripts/mobile/probe.sh
```

By default it reads your address from the settings file. You can also pass one:

```bash
bash scripts/mobile/probe.sh https://<your-mac>.<your-tailnet>.ts.net
```

It checks that:

- the Tailscale client is 1.104.4 or later;
- Serve has exactly one mapping, to `127.0.0.1:8765`;
- Funnel is off and Tailscale SSH is off;
- the feedBack ports (8000, 8765, 8766, 18000) listen on loopback only;
- a direct request to the server without a Tailscale identity is refused;
- `/`, `/v3`, `/api/settings`, `/api/plugins` and a `POST` to `/api/songs/upload` are refused;
- `/mobile` returns 200 with a Content-Security-Policy header;
- a WebSocket opened with a foreign `Origin` is refused.

Fix any FAIL before continuing.

**Then check from the phone** (Tailscale VPN on):

1. Open `https://<your-mac>.<your-tailnet>.ts.net/mobile/` in Safari. The feedBack page loads with a valid lock icon and your songs appear in the Library tab.
2. Open `https://<your-mac>.<your-tailnet>.ts.net/` (the root). It must show a **404**. That proves the gate is on.
3. Turn the iPhone's Tailscale VPN **off** and reload. It must fail to load. That proves the server is private.

## 7. Using the app on the iPhone

1. **Add to Home Screen first.** In Safari: **Share -> Add to Home Screen**. Do this *before* downloading anything: a home-screen app has its **own storage, separate from Safari**. Songs downloaded in a Safari tab are not visible to the home-screen app. Always launch from the home-screen icon afterwards.
2. **Download songs (at home, VPN on).** In the **Library** tab, tap **Download**. The Mac may take a while to build the download the first time (a "Preparing..." state, up to 5 minutes), then a progress bar shows the transfer. Tap **Cancel** to stop. Downloads include all arrangements and stems; drums arrangements are skipped. The first download asks the browser to make your storage persistent.
   - If a song changes on the Mac, it shows **Update available**; tap **Update**. **Re-download** is shown for songs that are current.
3. **Play offline.** The top bar shows **Online** or **Offline**. Offline, the Library tab says so and the **Downloaded** tab still works with no network, even after you force-quit the app (the app itself is cached by a service worker). Tap **Play** on a song. You can do this on a plane, with the VPN off and Mac asleep.
4. **Player controls.** The player is landscape only: in portrait it shows "Rotate your phone to landscape". Tap **Tap to start** (iOS needs a tap to start audio). Then:
   - **Play/Pause**, **-5s** and **+5s**, and the time display.
   - The **arrangement** picker (Lead, Rhythm, Bass, ...).
   - **Speed**: tap the speed button for presets (50%, 60%, 70%, 80%, 90%, 100%) and a slider from 40% to 100%. Pitch is preserved.
   - **Close** returns to the lists.
   - Audio plays even with the iPhone silent switch on.
5. **3D or 2D highway.** **Settings -> Highway** chooses **3D (default)** or **2D**. 3D uses the same 3D highway as the desktop app. The 3D bloom glow is switched off by default on the phone because it is the heaviest effect on an iPhone GPU; the 2D highway is the lighter option on older phones. If 3D cannot run (for example no WebGL2), the app falls back to 2D and says so in Settings.
6. **Keep the screen on.** The app asks iOS to keep the screen awake while playing. If your iOS version doesn't honor that for home-screen apps (some versions before iOS 18.4), the player shows a tip: set **Auto-Lock to Never** in iOS **Settings -> Display & Brightness -> Auto-Lock** while you play.
7. **Storage.** The **Downloaded** tab shows how much your downloads use, the free space left, a meter, and a **Persistent / Not persistent** label. A download refuses to start if the song won't fit. Expect roughly 15-40 MB per song with stems (50 songs can reach 1-2 GB). **Delete** removes one song; **Delete all downloads** removes everything.
8. **App updates.** When a new version of the app is available, a banner offers **Reload**. You can also tap **Settings -> Check for app update**.

## 8. Keeping it running

- **Sleep:** the server runs under `caffeinate -s`, so the Mac stays awake while the server is running **and the Mac is on AC power**. It changes no system setting, and removing the LaunchAgent undoes it. On battery the Mac sleeps normally.
- **Lid closed:** a closed lid still sleeps the Mac unless an external display is connected.
- **After a reboot:** the Mac must be **logged in** for the app to come back. Tailscale's app, and the server's LaunchAgent, only start at login. Make sure **Launch at login** is on in Tailscale. The Serve mapping persists on its own.
- **Offline vs. online:** the Mac needs to be awake and online for browsing, Stream and Download. Playing already-downloaded songs needs nothing.
- **Logs:**

  ```bash
  tail -f ~/Library/Logs/feedback-mobile.log
  ```

- **Is it running?**

  ```bash
  launchctl print gui/$(id -u)/app.feedback.mobile
  ```

- **Restart:**

  ```bash
  launchctl kickstart -k gui/$(id -u)/app.feedback.mobile
  ```

## 9. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Browser shows a certificate error or the address doesn't load | The HTTPS certificate is not ready yet, or HTTPS isn't enabled. Check **Enable HTTPS** in admin console -> **DNS**, wait a minute and retry. Make sure the iPhone's VPN is on. |
| `403 Forbidden` on `/mobile` | Your Tailscale login isn't on the allowlist. Open **Settings** in the app to see the login it sees, or run `tailscale status --json` on the Mac. Add the login to `FEEDBACK_MOBILE_ALLOWED_USERS` in `~/.config/feedback-mobile/env` (comma-separated, for example `you@example.com,partner@example.com`), then restart the server. Another cause: the request didn't come through Tailscale Serve (for example a tagged phone, which sends no user identity). Keep the phone user-owned. |
| `404` on `/` | Expected. Use `/mobile/`. |
| Library says "Could not load the library", or the badge says Offline while on home Wi-Fi | The Mac is asleep, off, on battery, or logged out; the server isn't running; or the iPhone's Tailscale VPN is off. Check section 8 and `tail ~/Library/Logs/feedback-mobile.log`. |
| Songs load but won't play or the highway stays blank while streaming | WebSocket problem. Run the probe, check Serve is still `https://... -> 127.0.0.1:8765`, and that the server was restarted after changing `FEEDBACK_MOBILE_ORIGIN`/host settings. The origin must exactly match the address in Safari. |
| The page opens in Safari but the home-screen app is empty | Expected: separate storage. Download again from the home-screen app (section 7). |
| App is empty after it worked before | The machine name or tailnet name changed, so the web address changed. Re-add to Home Screen from the new address and re-download. |
| "Not enough storage..." when downloading | Free up space on the phone or tap **Delete** on downloaded songs. |
| Download stays on "Preparing..." and fails | The first build of a large song can take a while (limit 5 minutes). Retry; check the log for ffmpeg errors. |
| Song says "Could not load song" / "Audio unavailable" | The song file is damaged, or its audio files can't be read. Check it plays in the desktop app. |
| "3D is unavailable (...); the 2D highway is used instead" | The phone or browser can't run the 3D renderer (no WebGL2, or the renderer failed to load or crashed). 2D is used automatically. Close and reopen the app to retry 3D, or pick 2D in Settings. |
| 3D is choppy | Choose 2D in **Settings -> Highway**. |
| Songs from a symlinked folder don't show or won't play | Songs reached through a symlink that points outside the library are refused on the mobile server. Point `DLC_DIR` at the real folder and re-run `setup.sh`. |
| Log shows "Operation not permitted" (exit 126) | Code, Python or library is under Desktop/Documents/Downloads. Re-run `setup.sh`; move the library if it asks. |
| `setup.sh` says Tailscale "is older than 1.104.4" | Update Tailscale, then re-run. |
| `setup.sh` says Funnel is on | Run `tailscale funnel reset`, then re-run. |
| Server won't start; log says the env file is a symlink or writable | Make `~/.config/feedback-mobile/env` a regular file you own: `chmod 600 ~/.config/feedback-mobile/env`. |

## 10. Security notes and limits

- **What the gate allows:** only `GET`/`HEAD` requests to `/mobile`, `/api/mobile/*`, a fixed list of static files for the page and highway renderers, the song playback WebSocket, audio and cover art for songs inside your library, and the 3D highway plugin's two files. Everything else is refused. Requests from other websites (wrong `Origin`, cross-site) and DNS-rebinding attempts (wrong `Host`) are refused. Only 2 playback sockets per user and 4 in total are allowed.
- **Three layers:** the Tailscale access policy (who can reach the Mac), the identity allowlist (which logins the server accepts), and the route gate (what they can do).
- **Lost or stolen phone:** it can reach your Mac until you remove it. Go to admin console -> **Machines** -> the phone -> **...** -> **Delete** (or **Disable key**). Songs already downloaded to it stay on that phone; it is protected by your iPhone passcode.
- **Other people on your tailnet:** nobody else can use the server unless their login is on the allowlist and the access policy lets them reach the Mac. Add logins only on purpose.
- **Keep Tailscale updated** on the Mac and the iPhone, and follow the security bulletins feed: https://tailscale.com/security-bulletins/index.xml
- **Never:** run `tailscale funnel`, forward ports on your router, bind any feedBack server to `0.0.0.0`, or point Serve at the desktop app's own port or at any server without the gate.
- **Limits:** the Mac must be awake, on power and logged in for browsing, streaming and downloading. The mobile server doesn't share favorites or settings with the desktop app. Songs that are `.psarc` files don't play; only `.feedpak`, `.sloppak` and folder songs are listed. Changing the machine name or tailnet name wipes offline access on the phone.
