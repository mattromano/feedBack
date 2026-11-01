#!/usr/bin/env bash
# One-time, re-runnable setup for the feedBack mobile player (docs/mobile-offline-plan.md
# sections 8 and 9.1).
#
#   setup.sh              interactive setup (asks before every system change)
#   setup.sh --dry-run    show what would be done; change nothing
#   setup.sh --uninstall  remove the LaunchAgent, the serve mapping and the env file
#   setup.sh --yes        accept every default and answer yes to every question
#
# Never runs `tailscale funnel` (except `funnel status`), never binds anything to a
# non-loopback address.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_DIR="$HOME/.config/feedback-mobile"
ENV_FILE="$ENV_DIR/env"
LABEL="app.feedback.mobile"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_FILE="$HOME/Library/Logs/feedback-mobile.log"
MOBILE_CONFIG_DIR="$HOME/Library/Application Support/feedback-mobile"
# Where the server's code is copied when the repo sits in a folder macOS keeps
# background jobs out of (see in_protected below).
APP_DIR="$MOBILE_CONFIG_DIR/app"
BUNDLED_PY=/Applications/feedback.app/Contents/Resources/python/runtime/bin/python3
PORT=8765
MIN_TS_VERSION="1.104.4"

DRY_RUN=0
UNINSTALL=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -y|--yes) ASSUME_YES=1 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) printf 'setup: unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

say() { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() { printf 'setup: %s\n' "$*" >&2; exit 1; }

# in_protected PATH: true if PATH (symlinks followed) is under Desktop, Documents or
# Downloads. macOS privacy protection (TCC) blocks LaunchAgents from reading those, so
# the server fails with "Operation not permitted" if its code, Python or library is there.
in_protected() {
  # Both the path as written (a venv's python links out, but its packages stay put)
  # and the link-resolved path.
  local p d
  for p in "$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "$1")" \
           "$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$1")"; do
    for d in Desktop Documents Downloads; do
      case "$p/" in "$HOME/$d/"*) return 0 ;; esac
    done
  done
  return 1
}

# run CMD...: execute, or just print it under --dry-run.
run() {
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    [dry-run] %s\n' "$*"
  else
    "$@"
  fi
}

# ask "question": yes/no from the terminal, default no. Always "no" under --dry-run.
ask() {
  local reply=""
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    [dry-run] would ask: %s\n' "$1"
    return 1
  fi
  if [[ $ASSUME_YES -eq 1 ]]; then
    printf '%s [y/N] y (--yes)\n' "$1"
    return 0
  fi
  [[ -r /dev/tty ]] || return 1
  printf '%s [y/N] ' "$1" > /dev/tty
  IFS= read -r reply < /dev/tty || return 1
  [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]
}

# prompt "question" "default": free-text answer from the terminal.
prompt() {
  local reply=""
  if [[ $DRY_RUN -eq 1 || $ASSUME_YES -eq 1 || ! -r /dev/tty ]]; then
    printf '%s' "$2"
    return
  fi
  printf '%s [%s]: ' "$1" "$2" > /dev/tty
  IFS= read -r reply < /dev/tty || true
  printf '%s' "${reply:-$2}"
}

find_tailscale() {
  if command -v tailscale >/dev/null 2>&1; then
    command -v tailscale
  elif [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
    echo /Applications/Tailscale.app/Contents/MacOS/Tailscale
  fi
}

agent_loaded() { launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1; }

# serve_state: prints "none", "ours" (exactly one mapping to 127.0.0.1:8765), "funnel"
# or "other".
serve_state() {
  local json
  json="$("$TS" serve status --json 2>/dev/null || true)"
  printf '%s' "$json" | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
try:
    cfg = json.loads(raw) if raw else {}
except ValueError:
    print("other"); sys.exit()
if any((cfg.get("AllowFunnel") or {}).values()):
    print("funnel"); sys.exit()
# Anything we do not recognise (Tailscale Services, foreground sessions, new
# config keys) counts as foreign config, so we never remove it.
if set(cfg) - {"TCP", "Web", "AllowFunnel"} and any(
        cfg[k] for k in set(cfg) - {"TCP", "Web", "AllowFunnel"}):
    print("other"); sys.exit()
handlers = [(m, h) for web in (cfg.get("Web") or {}).values()
            for m, h in (web.get("Handlers") or {}).items()]
tcp_fwd = [t for t in (cfg.get("TCP") or {}).values() if t.get("TCPForward")]
if not handlers and not tcp_fwd:
    print("none")
elif (len(handlers) == 1 and not tcp_fwd and handlers[0][0] == "/"
      and (handlers[0][1].get("Proxy") or "").rstrip("/") in
          ("http://127.0.0.1:8765", "http://localhost:8765", "127.0.0.1:8765")):
    print("ours")
else:
    print("other")
'
}

# ── Uninstall ───────────────────────────────────────────────────────────────
if [[ $UNINSTALL -eq 1 ]]; then
  TS="$(find_tailscale)"
  say "Uninstalling the feedBack mobile server"
  if agent_loaded || [[ -f "$PLIST_DST" ]]; then
    if ask "Stop and remove the LaunchAgent ($PLIST_DST)?"; then
      if agent_loaded; then run launchctl bootout "gui/$(id -u)/$LABEL" || true; fi
      run rm -f "$PLIST_DST"
    fi
  else
    note "LaunchAgent not installed"
  fi
  if [[ -n "$TS" ]]; then
    state="$(serve_state)"
    case "$state" in
      ours)
        if ask "Remove the tailscale serve mapping to 127.0.0.1:$PORT?"; then
          run "$TS" serve --https=443 off
        fi ;;
      none) note "no tailscale serve config" ;;
      *)
        note "tailscale serve has other mappings; not touching them."
        note "Remove ours by hand: $TS serve --https=443 off" ;;
    esac
  fi
  if [[ -f "$ENV_FILE" ]] && ask "Delete $ENV_FILE?"; then
    run rm -f "$ENV_FILE"
  fi
  if [[ -d "$APP_DIR" ]] && ask "Delete the server code copy in $APP_DIR?"; then
    run rm -rf "$APP_DIR"
  fi
  note "Left in place: $MOBILE_CONFIG_DIR (index and pack cache) and $LOG_FILE."
  note "Delete them by hand if you no longer want them."
  exit 0
fi

# ── 1. Tailscale CLI, login and version ─────────────────────────────────────
say "Checking Tailscale"
TS="$(find_tailscale)"
if [[ -z "$TS" ]]; then
  note "The Tailscale CLI was not found."
  note "Install Tailscale Standalone from https://tailscale.com/download/mac, sign in,"
  note "then Settings -> CLI integration -> Install. Re-run this script afterwards."
  exit 1
fi
note "CLI: $TS"

status_json="$("$TS" status --json 2>/dev/null || true)"
read_status() {
  printf '%s' "$status_json" | python3 -c '
import json, sys
try:
    st = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
self_ = st.get("Self") or {}
user = (st.get("User") or {}).get(str(self_.get("UserID")), {})
print(st.get("BackendState", ""))
print((self_.get("DNSName") or "").rstrip("."))
# A tagged Mac (access policy step) has no owning user; report that instead.
print("" if self_.get("Tags") else (user.get("LoginName") or ""))
print("tagged" if self_.get("Tags") else "user")
'
}
status_lines="$(read_status)" || die "could not read 'tailscale status --json'"
backend_state="$(printf '%s\n' "$status_lines" | sed -n 1p)"
dns_name="$(printf '%s\n' "$status_lines" | sed -n 2p)"
login="$(printf '%s\n' "$status_lines" | sed -n 3p)"
node_kind="$(printf '%s\n' "$status_lines" | sed -n 4p)"
if [[ "$node_kind" == "tagged" && -z "$login" ]]; then
  # Tagged devices have no owner login. Reuse the allowlist from a previous
  # run, otherwise ask (it must be the login your iPhone is signed in with).
  if [[ -f "$ENV_FILE" ]]; then
    login="$(sed -n 's/^FEEDBACK_MOBILE_ALLOWED_USERS=//p' "$ENV_FILE" | tail -n 1 | cut -d, -f1 | tr -d ' ')"
  fi
  if [[ -z "$login" ]]; then
    login="$(prompt "This Mac is tagged. Tailscale login your iPhone uses (e.g. you@github)" "")"
  fi
  [[ "$login" =~ ^[^[:space:],]+$ ]] || die "invalid login: $login"
fi
[[ "$backend_state" == "Running" ]] || die "Tailscale is not running/logged in (state: ${backend_state:-unknown}). Open the Tailscale app and sign in."
[[ -n "$dns_name" ]] || die "this Mac has no MagicDNS name. Enable MagicDNS and HTTPS in the admin console."
[[ -n "$login" ]] || die "could not determine your Tailscale login"
note "Mac: $dns_name"
note "Login: $login"

ts_version="$("$TS" version 2>/dev/null | head -n 1 || true)"
if ! python3 - "$ts_version" "$MIN_TS_VERSION" <<'PY'
import sys
def key(v):
    return [int(p) if p.isdigit() else 0 for p in v.strip().split("-")[0].split(".")]
sys.exit(0 if sys.argv[1] and key(sys.argv[1]) >= key(sys.argv[2]) else 1)
PY
then
  die "Tailscale ${ts_version:-unknown} is older than $MIN_TS_VERSION (security bulletin TS-2026-016). Update it first."
fi
note "Version: $ts_version"

# ── 2. Funnel must be off ───────────────────────────────────────────────────
state="$(serve_state)"
funnel_out="$("$TS" funnel status 2>&1 || true)"
if [[ "$state" == "funnel" ]] || printf '%s' "$funnel_out" | grep -qi 'funnel on'; then
  die "Tailscale Funnel is on. Turn it off (tailscale funnel reset) and re-run. This setup never uses Funnel."
fi

# ── 3. Library (DLC_DIR) ────────────────────────────────────────────────────
# The desktop app (feedBack-desktop src/main/python.ts getConfigDir/getDLCDir) keeps its
# config in ~/.local/share/slopsmith if that exists, else
# ~/Library/Application Support/feedback-desktop/slopsmith-config, and reads "dlc_dir"
# from config.json there; with no setting it falls back to these default folders.
say "Finding your song library"
detected=""
existing_dlc=""
if [[ -f "$ENV_FILE" ]]; then
  existing_dlc="$(sed -n 's/^DLC_DIR=//p' "$ENV_FILE" | tail -n 1)"
fi
for cfg in "$HOME/.local/share/slopsmith/config.json" \
           "$HOME/Library/Application Support/feedback-desktop/slopsmith-config/config.json"; do
  [[ -f "$cfg" ]] || continue
  d="$(python3 -c '
import json, os, sys
try:
    v = json.load(open(sys.argv[1])).get("dlc_dir") or ""
except Exception:
    v = ""
print(v if v and os.path.isdir(v) else "")
' "$cfg")"
  if [[ -n "$d" ]]; then detected="$d"; note "from $cfg"; break; fi
done
if [[ -z "$detected" ]]; then
  for d in "$HOME/.local/share/feedback/library" "$HOME/.local/share/slopsmith/library" "$HOME/Music/Slopsmith"; do
    if [[ -d "$d" ]]; then detected="$d"; note "default folder"; break; fi
  done
fi
# A saved value in a protected folder (e.g. before the library moved) loses to a fresh one.
if [[ -n "$existing_dlc" ]] && in_protected "$existing_dlc" && [[ -n "$detected" ]]; then existing_dlc=""; fi
dlc_dir="$(prompt "Song library folder (DLC_DIR)" "${existing_dlc:-$detected}")"
[[ -n "$dlc_dir" && -d "$dlc_dir" ]] || die "library folder not found: ${dlc_dir:-<empty>}"
# Store the real location: a shortcut left on the Desktop is itself unreadable to the agent.
dlc_dir="$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$dlc_dir")"
[[ "$dlc_dir" != *$'\n'* ]] || die "library path contains a newline"
in_protected "$dlc_dir" && die "the library is inside Desktop, Documents or Downloads, which macOS blocks the background server from reading. Move it (e.g. to ~/rocksmith), update the desktop app's library folder, and re-run."
note "DLC_DIR: $dlc_dir"

# ── 4. Python ───────────────────────────────────────────────────────────────
say "Finding Python"
default_py=""
existing_py=""
if [[ -f "$ENV_FILE" ]]; then
  existing_py="$(sed -n 's/^FEEDBACK_PYTHON=//p' "$ENV_FILE" | tail -n 1)"
fi
# A venv inside Desktop/Documents/Downloads can't be read by the LaunchAgent, so those
# are skipped; the desktop app's bundled Python is the usual fallback.
for p in "$existing_py" "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/../feedBack/.venv/bin/python" "$BUNDLED_PY"; do
  if [[ -n "$p" && -x "$p" ]] && ! in_protected "$p"; then default_py="$p"; break; fi
done
py="$(prompt "Python with feedBack's requirements installed (FEEDBACK_PYTHON)" "$default_py")"
[[ -n "$py" && -x "$py" ]] || die "python not found: ${py:-<empty>}"
if [[ $DRY_RUN -eq 0 ]]; then
  "$py" -c 'import fastapi, uvicorn' 2>/dev/null || die "$py cannot import fastapi/uvicorn (pip install -r requirements.txt)"
fi
in_protected "$py" && die "$py is inside Desktop, Documents or Downloads; the background server can't run it. Use $BUNDLED_PY or a venv elsewhere."
note "FEEDBACK_PYTHON: $py"

# ── 4b. Server code ─────────────────────────────────────────────────────────
# A repo in a protected folder is copied to $APP_DIR and run from there. Re-run setup
# after pulling changes to refresh the copy.
APP_ROOT="$REPO_ROOT"
if in_protected "$REPO_ROOT"; then
  say "Copying the server code out of a protected folder"
  note "$REPO_ROOT -> $APP_DIR"
  if [[ $DRY_RUN -eq 0 ]]; then
    mkdir -p "$APP_DIR"
    rsync -a --delete --exclude .git --exclude node_modules --exclude tests --exclude specs \
      --exclude .venv --exclude __pycache__ --exclude .pytest_cache --exclude '*.pyc' \
      "$REPO_ROOT/" "$APP_DIR/"
    chmod -R go-w "$APP_DIR"
  else
    note "[dry-run] would rsync the repo into $APP_DIR"
  fi
  APP_ROOT="$APP_DIR"
fi

# ── 5. Env file ─────────────────────────────────────────────────────────────
origin="https://$dns_name"
users="$login"
hosts=""
if [[ -f "$ENV_FILE" ]]; then
  existing_users="$(sed -n 's/^FEEDBACK_MOBILE_ALLOWED_USERS=//p' "$ENV_FILE" | tail -n 1)"
  # Keep logins you added by hand, as long as your own is still on the list.
  if printf '%s' ",$existing_users," | tr '[:upper:]' '[:lower:]' | tr -d ' ' \
      | grep -qF ",$(printf '%s' "$login" | tr '[:upper:]' '[:lower:]'),"; then
    users="$existing_users"
  fi
  hosts="$(sed -n 's/^FEEDBACK_MOBILE_HOSTS=//p' "$ENV_FILE" | tail -n 1)"
fi
say "Writing $ENV_FILE"
new_env="$(cat <<EOF
# feedBack mobile server settings (written by scripts/mobile/setup.sh).
# KEY=VALUE lines only; run-server.sh parses this file, it is never sourced.
FEEDBACK_ROOT=$APP_ROOT
FEEDBACK_PYTHON=$py
DLC_DIR=$dlc_dir
CONFIG_DIR=$MOBILE_CONFIG_DIR
FEEDBACK_MOBILE_ORIGIN=$origin
FEEDBACK_MOBILE_ALLOWED_USERS=$users
EOF
)"
if [[ -n "$hosts" ]]; then new_env="$new_env"$'\n'"FEEDBACK_MOBILE_HOSTS=$hosts"; fi
if [[ -f "$ENV_FILE" ]] && [[ "$(cat "$ENV_FILE")" == "$new_env" ]]; then
  note "unchanged"
elif [[ $DRY_RUN -eq 1 ]]; then
  printf '%s\n' "$new_env" | sed 's/^/    [dry-run] | /'
else
  ( umask 077; mkdir -p "$ENV_DIR"; printf '%s\n' "$new_env" > "$ENV_FILE.tmp" )
  chmod 700 "$ENV_DIR"
  chmod 600 "$ENV_FILE.tmp"
  mv "$ENV_FILE.tmp" "$ENV_FILE"
  note "written (mode 600)"
fi
note "allowed users: $users"

# ── 6. LaunchAgent ──────────────────────────────────────────────────────────
say "LaunchAgent ($LABEL)"
if ask "Install and (re)start the LaunchAgent that keeps the server running on 127.0.0.1:$PORT?"; then
  mkdir -p "$(dirname "$PLIST_DST")" "$(dirname "$LOG_FILE")"
  python3 - "$SCRIPT_DIR/app.feedback.mobile.plist" "$PLIST_DST.tmp" "$APP_ROOT/scripts/mobile/run-server.sh" "$LOG_FILE" <<'PY'
import sys
from xml.sax.saxutils import escape
src, dst, run_server, log = sys.argv[1:]
text = open(src, encoding="utf-8").read()
text = text.replace("__RUN_SERVER__", escape(run_server)).replace("__LOG__", escape(log))
open(dst, "w", encoding="utf-8").write(text)
PY
  plutil -lint "$PLIST_DST.tmp" >/dev/null || die "generated plist is invalid"
  mv "$PLIST_DST.tmp" "$PLIST_DST"
  if agent_loaded; then launchctl bootout "gui/$(id -u)/$LABEL" || true; sleep 1; fi
  launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
  note "loaded; log: $LOG_FILE"
  printf '    waiting for the server'
  up=0
  for _ in $(seq 1 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 2 "http://127.0.0.1:$PORT/mobile" 2>/dev/null || true)"
    if [[ -n "$code" && "$code" != "000" ]]; then up=1; break; fi
    printf '.'
    sleep 1
  done
  echo
  [[ $up -eq 1 ]] || note "server did not answer within 30 s; check $LOG_FILE"
else
  note "skipped"
fi

# ── 7. tailscale serve ──────────────────────────────────────────────────────
say "tailscale serve"
state="$(serve_state)"
case "$state" in
  ours) note "already serving https://$dns_name -> 127.0.0.1:$PORT" ;;
  funnel) die "Funnel is on; refusing to continue" ;;
  *)
    if [[ "$state" == "other" ]]; then
      note "tailscale serve already has other mappings:"
      "$TS" serve status 2>/dev/null | sed 's/^/      /' || true
      note "Adding ours replaces whatever is served at https://$dns_name/ ."
    fi
    if ask "Run 'tailscale serve --bg $PORT' (tailnet only, never Funnel)?"; then
      run "$TS" serve --bg "$PORT"
    else
      note "skipped"
    fi ;;
esac

if ask "Turn off Tailscale SSH, exit-node advertising and advertised routes on this Mac (tailscale set)?"; then
  run "$TS" set --ssh=false --advertise-exit-node=false --advertise-routes=
fi

# ── 8. Hardening checklist and phone URL ────────────────────────────────────
cat <<'EOF'

==> Tailnet hardening checklist (admin console; see docs/mobile-offline-plan.md 9.1)
  1. MFA on the identity provider you sign in to Tailscale with.
  2. Access policy: replace allow-all. Tag this Mac tag:feedback-host (owned by you) and
     grant only your user -> tag:feedback-host on tcp:443. Nothing grants access to the
     phone. Add policy tests: accept mac:443; deny mac:22, mac:8765, mac:18000, phone:443.
     Keep the phone user-owned (not tagged) so serve sends its identity header.
  3. Funnel: no funnel nodeAttr in the policy; `tailscale funnel status` stays empty.
  4. Off: Tailscale SSH, subnet routes, exit nodes, app connectors, Send Files (Taildrop).
  5. Don't enable shields-up on this Mac (it blocks serve).
  6. Device approval, or Tailnet Lock (store its disablement secrets safely).
  7. Auto-update on both clients (1.104.4 or later); subscribe to
     https://tailscale.com/security-bulletins/index.xml
  8. Key expiry stays on. Remove old/unknown machines. Review Admin -> Logs after setup.
  9. Neutral machine name; randomize the tailnet name before enabling HTTPS. Never rename
     either afterwards (the phone's offline data is tied to the URL).
EOF

url="$origin/mobile/"
echo
say "Open this on your phone (Tailscale VPN on), then Share -> Add to Home Screen:"
note "$url"
if command -v qrencode >/dev/null 2>&1; then
  qrencode -t ANSIUTF8 "$url"
fi

# ── 9. Probe ────────────────────────────────────────────────────────────────
echo
say "Running the security probe"
if [[ $DRY_RUN -eq 1 ]]; then
  note "[dry-run] would run: $SCRIPT_DIR/probe.sh $origin"
else
  bash "$SCRIPT_DIR/probe.sh" "$origin" || note "probe reported failures (see above)"
fi
