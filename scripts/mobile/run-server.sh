#!/usr/bin/env bash
# Start the gated feedBack mobile server on 127.0.0.1:8765.
#
# Reads ~/.config/feedback-mobile/env (written by setup.sh). The file is parsed as
# KEY=VALUE lines, never sourced, and must be a regular file (not a symlink) owned by
# you and not writable by group or others. Started by the app.feedback.mobile LaunchAgent; safe to run by hand.
set -euo pipefail

ENV_FILE="${FEEDBACK_MOBILE_ENV:-$HOME/.config/feedback-mobile/env}"
REQUIRED=(FEEDBACK_ROOT FEEDBACK_PYTHON DLC_DIR CONFIG_DIR FEEDBACK_MOBILE_ORIGIN FEEDBACK_MOBILE_ALLOWED_USERS)
OPTIONAL=(FEEDBACK_MOBILE_HOSTS)

die() { printf 'run-server: %s\n' "$*" >&2; exit 1; }

# A symlink is refused outright: the owner/mode checks below would otherwise judge
# the link (or, with -L, a target someone else may control) rather than the file read.
[[ -L "$ENV_FILE" ]] && die "$ENV_FILE is a symlink (replace it with a regular file)"
[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE (run scripts/mobile/setup.sh)"
# -L is a no-op after the symlink check; it keeps the stat on the file even if the
# check above is ever edited away.
owner="$(stat -L -f '%u' "$ENV_FILE")"
mode="$(stat -L -f '%Lp' "$ENV_FILE")"
[[ "$owner" == "$(id -u)" ]] || die "$ENV_FILE is not owned by you"
(( (8#$mode & 8#022) == 0 )) || die "$ENV_FILE is group/world-writable (chmod 600 it)"

is_known() {
  local k="$1" x
  for x in "${REQUIRED[@]}" "${OPTIONAL[@]}"; do [[ "$x" == "$k" ]] && return 0; done
  return 1
}

while IFS= read -r line || [[ -n "$line" ]]; do
  line="${line%$'\r'}"
  [[ -z "${line//[[:space:]]/}" || "$line" =~ ^[[:space:]]*# ]] && continue
  [[ "$line" =~ ^([A-Z_][A-Z0-9_]*)=(.*)$ ]] || die "bad line in $ENV_FILE: $line"
  key="${BASH_REMATCH[1]}"
  val="${BASH_REMATCH[2]}"
  if [[ "$val" =~ ^\"(.*)\"$ ]]; then val="${BASH_REMATCH[1]}"; fi
  is_known "$key" || die "unknown key in $ENV_FILE: $key"
  export "$key=$val"
done < "$ENV_FILE"

for k in "${REQUIRED[@]}"; do
  [[ -n "${!k:-}" ]] || die "$k is not set in $ENV_FILE"
done

export HOST=127.0.0.1 PORT=8765 FEEDBACK_MOBILE_MODE=1

[[ -f "$FEEDBACK_ROOT/main.py" ]] || die "FEEDBACK_ROOT has no main.py: $FEEDBACK_ROOT"
[[ -x "$FEEDBACK_PYTHON" ]] || die "FEEDBACK_PYTHON is not executable: $FEEDBACK_PYTHON"
[[ -d "$DLC_DIR" ]] || die "DLC_DIR is not a directory: $DLC_DIR"
mkdir -p "$CONFIG_DIR"
# Same import path the Docker image and the desktop app use.
export PYTHONPATH="$FEEDBACK_ROOT/lib:$FEEDBACK_ROOT"

BUNDLED_BIN=/Applications/feedback.app/Contents/Resources/bin
if [[ -d "$BUNDLED_BIN" ]]; then export PATH="$BUNDLED_BIN:$PATH"; fi

cd "$FEEDBACK_ROOT"
if command -v caffeinate >/dev/null 2>&1; then
  exec caffeinate -s "$FEEDBACK_PYTHON" main.py
fi
exec "$FEEDBACK_PYTHON" main.py
