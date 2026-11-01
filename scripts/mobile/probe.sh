#!/usr/bin/env bash
# Read-only security probe for the feedBack mobile server (plan section 9.4).
#
#   probe.sh                 probe the serve URL from ~/.config/feedback-mobile/env
#   probe.sh https://host    probe that URL
#   probe.sh --local         probe the dev proxy on http://127.0.0.1:8766; skip Tailscale checks
#
# Prints PASS/FAIL per check and exits non-zero if any check fails. Changes nothing.
set -euo pipefail

ENV_FILE="${FEEDBACK_MOBILE_ENV:-$HOME/.config/feedback-mobile/env}"
MIN_TS_VERSION="1.104.4"
LOCAL=0
BASE=""

for arg in "$@"; do
  case "$arg" in
    --local) LOCAL=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    http://*|https://*) BASE="${arg%/}" ;;
    *) printf 'probe: unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

if [[ $LOCAL -eq 1 ]]; then
  BASE="http://127.0.0.1:8766"
elif [[ -z "$BASE" ]]; then
  if [[ -f "$ENV_FILE" ]]; then
    BASE="$(sed -n 's/^FEEDBACK_MOBILE_ORIGIN="\{0,1\}\([^"]*\)"\{0,1\}$/\1/p' "$ENV_FILE" | tail -n 1)"
  fi
  [[ -n "$BASE" ]] || { echo "probe: no URL given and none in $ENV_FILE" >&2; exit 2; }
  BASE="${BASE%/}"
fi

FAILS=0
pass() { printf 'PASS  %s\n' "$*"; }
fail() { printf 'FAIL  %s\n' "$*"; FAILS=$((FAILS + 1)); }
warn() { printf 'WARN  %s\n' "$*"; }

find_tailscale() {
  if command -v tailscale >/dev/null 2>&1; then
    command -v tailscale
  elif [[ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]]; then
    echo /Applications/Tailscale.app/Contents/MacOS/Tailscale
  fi
}

# curl helper: prints only the HTTP status code ("000" when the request failed).
status_of() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$@" 2>/dev/null)" || true
  printf '%s' "${code:-000}"
}

refused() { [[ "$1" =~ ^(400|403|404|405)$ ]]; }

# ── Tailscale state ─────────────────────────────────────────────────────────
if [[ $LOCAL -eq 0 ]]; then
  TS="$(find_tailscale)"
  if [[ -z "$TS" ]]; then
    fail "tailscale CLI not found"
  else
    ver="$("$TS" version 2>/dev/null | head -n 1 || true)"
    if python3 - "$ver" "$MIN_TS_VERSION" <<'PY'
import sys
def key(v):
    out = []
    for p in v.strip().split("-")[0].split("."):
        out.append(int(p) if p.isdigit() else 0)
    return out
sys.exit(0 if sys.argv[1] and key(sys.argv[1]) >= key(sys.argv[2]) else 1)
PY
    then pass "tailscale client $ver >= $MIN_TS_VERSION"
    else fail "tailscale client '${ver:-unknown}' is older than $MIN_TS_VERSION"
    fi

    serve_json="$("$TS" serve status --json 2>/dev/null || true)"
    serve_verdict="$(printf '%s' "$serve_json" | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
try:
    cfg = json.loads(raw) if raw else {}
except ValueError:
    print("unparseable serve status"); sys.exit()
handlers = []
for hp, web in (cfg.get("Web") or {}).items():
    for mount, h in (web.get("Handlers") or {}).items():
        handlers.append((hp, mount, h))
tcp_fwd = [p for p, t in (cfg.get("TCP") or {}).items() if t.get("TCPForward") or t.get("TerminateTLS")]
funnel = [k for k, v in (cfg.get("AllowFunnel") or {}).items() if v]
if funnel:
    print("funnel enabled for " + ", ".join(funnel)); sys.exit()
if tcp_fwd:
    print("raw TCP forwarding on " + ", ".join(tcp_fwd)); sys.exit()
if len(handlers) != 1:
    print("expected exactly 1 serve mapping, found %d" % len(handlers)); sys.exit()
hp, mount, h = handlers[0]
proxy = (h.get("Proxy") or "").rstrip("/")
if mount != "/" or proxy not in ("http://127.0.0.1:8765", "http://localhost:8765", "127.0.0.1:8765"):
    print("unexpected mapping %s%s -> %s" % (hp, mount, proxy or h)); sys.exit()
print("ok")
')"
    if [[ "$serve_verdict" == "ok" ]]; then
      pass "tailscale serve: exactly one mapping, / -> 127.0.0.1:8765"
    else
      fail "tailscale serve: $serve_verdict"
    fi

    funnel_out="$("$TS" funnel status 2>&1 || true)"
    if printf '%s' "$funnel_out" | grep -qi 'funnel on'; then
      fail "tailscale funnel status reports Funnel on"
    else
      pass "tailscale funnel status: no Funnel"
    fi

    ssh_state="$("$TS" debug prefs 2>/dev/null | python3 -c '
import json, sys
try:
    print("on" if json.load(sys.stdin).get("RunSSH") else "off")
except Exception:
    print("unknown")
' || true)"
    case "$ssh_state" in
      off) pass "Tailscale SSH is off" ;;
      on) fail "Tailscale SSH is on (tailscale set --ssh=false)" ;;
      *) warn "could not read Tailscale SSH state" ;;
    esac
  fi
fi

# ── Listeners ───────────────────────────────────────────────────────────────
if command -v lsof >/dev/null 2>&1; then
  exposed="$(lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR > 1 { print $9 }' \
    | grep -E ':(8000|8765|8766|18000)$' \
    | grep -vE '^(127\.0\.0\.1|\[::1\]|localhost):' || true)"
  if [[ -z "$exposed" ]]; then
    pass "feedBack ports (8000, 8765, 8766, 18000) listen on loopback only"
  else
    fail "non-loopback listener(s): $(printf '%s' "$exposed" | tr '\n' ' ')"
  fi
else
  warn "lsof not found; listener check skipped"
fi

# ── Gate, direct on loopback (no identity header) ───────────────────────────
code="$(status_of "http://127.0.0.1:8765/mobile")"
if [[ "$code" == "000" ]]; then
  fail "mobile server not reachable on 127.0.0.1:8765"
elif [[ "$code" == "403" ]]; then
  pass "direct request without identity is refused (403)"
else
  fail "direct request without identity returned $code (expected 403)"
fi

# ── Gate, through $BASE ─────────────────────────────────────────────────────
echo "probing $BASE"
for path in / /v3 /api/settings /api/plugins; do
  code="$(status_of "$BASE$path")"
  if refused "$code"; then pass "GET $path refused ($code)"; else fail "GET $path returned $code"; fi
done

code="$(status_of -X POST -H 'Content-Type: application/json' --data '{}' "$BASE/api/songs/upload")"
if refused "$code"; then pass "POST /api/songs/upload refused ($code)"; else fail "POST /api/songs/upload returned $code"; fi

hdrs="$(curl -s -D - -o /dev/null --max-time 10 "$BASE/mobile" 2>/dev/null || true)"
code="$(printf '%s' "$hdrs" | awk 'toupper($1) ~ /^HTTP\// { c = $2 } END { print c }')"
if [[ "$code" == "200" ]] && printf '%s' "$hdrs" | grep -qi '^content-security-policy:'; then
  pass "GET /mobile is 200 with a CSP header"
else
  fail "GET /mobile: status ${code:-none}, CSP header $(printf '%s' "$hdrs" | grep -qi '^content-security-policy:' && echo present || echo missing)"
fi
if printf '%s' "$hdrs" | grep -qi '^x-content-type-options: *nosniff'; then
  pass "gate adds X-Content-Type-Options: nosniff"
else
  fail "X-Content-Type-Options: nosniff missing on /mobile"
fi

code="$(status_of "$BASE/static/highway.js")"
if [[ "$code" == "200" ]]; then pass "GET /static/highway.js is 200"; else fail "GET /static/highway.js returned $code"; fi

ws_code="$(curl -s -o /dev/null -w '%{http_code}' --http1.1 --max-time 5 \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: ZmVlZGJhY2stcHJvYmUhIQ==' -H 'Origin: https://evil.example' \
  "$BASE/ws/highway/probe.sloppak" 2>/dev/null || true)"
ws_code="${ws_code:-000}"
if [[ "$ws_code" == "101" ]]; then
  fail "WebSocket with Origin https://evil.example was accepted (101)"
elif [[ "$ws_code" == "000" ]]; then
  fail "WebSocket probe got no response"
else
  pass "WebSocket with Origin https://evil.example refused ($ws_code)"
fi

echo
if [[ $FAILS -eq 0 ]]; then
  echo "probe: all checks passed"
else
  echo "probe: $FAILS check(s) failed"
  exit 1
fi
