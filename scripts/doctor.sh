#!/usr/bin/env bash
# First-look health check for the mission-control install. Read-only,
# purely diagnostic — checks the things the "Known quirks" section of
# CLAUDE.md otherwise requires a human to remember.
set -uo pipefail
cd "$(dirname "$0")/.."
VENV=.venv/bin/python
MODELS=data/models
PORT="${MC_PORT:-8765}"

R=$'\e[0m'; GRN=$'\e[32m'; YEL=$'\e[33m'; RED=$'\e[31m'; D=$'\e[2m'
ok()   { printf '%b✓%b %s\n' "$GRN" "$R" "$1"; }
warn() { printf '%b!%b %s\n' "$YEL" "$R" "$1"; }
bad()  { printf '%b✗%b %s\n' "$RED" "$R" "$1"; }
hdr()  { printf '\n%b%s%b\n' "$D" "$1" "$R"; }

hdr "─── venv / config ───"
if [ -x "$VENV" ]; then ok "venv present: $VENV"
else bad "venv missing — expected .venv/bin/python"
fi
if [ -f config.yaml ]; then
  if $VENV -c 'import yaml,sys; yaml.safe_load(open("config.yaml"))' 2>/dev/null; then
    ok "config.yaml parses"
  else
    bad "config.yaml present but fails to parse"
  fi
else
  bad "config.yaml missing"
fi

hdr "─── uinput / ydotool (text injection) ───"
if [ -e /dev/uinput ]; then
  perms=$(stat -c '%a %G' /dev/uinput 2>/dev/null || echo "?")
  if [ -w /dev/uinput ]; then
    ok "/dev/uinput writable ($perms)"
  else
    warn "/dev/uinput not writable ($perms) — chmod g+rw /dev/uinput && modprobe uinput"
  fi
else
  bad "/dev/uinput does not exist — modprobe uinput"
fi
if systemctl --user is-active --quiet ydotool 2>/dev/null || systemctl --user is-active --quiet ydotoold 2>/dev/null; then
  ok "ydotoold running (user service)"
elif pgrep -x ydotoold >/dev/null 2>&1; then
  ok "ydotoold running (process)"
else
  warn "ydotoold not running — dictation text injection will fail"
fi

hdr "─── input group (evdev hotkey capture) ───"
if id -nG "$USER" 2>/dev/null | grep -qw input; then
  ok "$USER is in the 'input' group"
else
  bad "$USER not in 'input' group — sudo usermod -aG input \$USER, then re-login"
fi

hdr "─── voice models (data/models/) ───"
if [ -f "$MODELS/silero_vad.onnx" ]; then ok "silero_vad.onnx present"
else bad "silero_vad.onnx missing — run scripts/setup_voice.sh"
fi
if ls "$MODELS"/piper/*.onnx >/dev/null 2>&1; then
  ok "piper voice present ($(ls "$MODELS"/piper/*.onnx | head -1))"
else
  bad "no piper voice found under $MODELS/piper/ — run scripts/setup_voice.sh"
fi
if $VENV -c '
from pathlib import Path
from openwakeword import get_pretrained_model_paths
names = [Path(p).stem for p in get_pretrained_model_paths()]
assert any(n.startswith("hey_jarvis") for n in names)
' >/dev/null 2>&1; then
  ok "openwakeword hey_jarvis model present"
else
  warn "openwakeword hey_jarvis model not verified (openwakeword import failed or model missing)"
fi

hdr "─── dispatcher ───"
if curl -sf --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  ok "dispatcher reachable on :$PORT"
else
  warn "dispatcher not reachable on :$PORT — is mission-dispatcher.service / python -m dispatcher.main running?"
fi
if systemctl --user is-active --quiet mission-dispatcher 2>/dev/null; then
  ok "mission-dispatcher.service active"
else
  warn "mission-dispatcher.service not active (may be run manually instead)"
fi

hdr "─── media (spotify) ───"
if [ -f data/spotify.json ]; then
  ok "spotify credentials present (data/spotify.json)"
else
  warn "no data/spotify.json — 'play <song>' can't search; run scripts/setup_spotify.sh"
fi
if busctl --user list 2>/dev/null | grep -q org.mpris.MediaPlayer2.spotify; then
  ok "spotify client running (MPRIS reachable)"
else
  warn "spotify client not running (it's launched on demand by a play command)"
fi

hdr "─── desktop control (computer-use T1) ───"
# every verb shells out to one of these; all ship with the base system, so a
# missing one means something was removed rather than never installed
missing=""
for b in wpctl loginctl gtk-launch xdg-open wl-copy wl-paste gdbus; do
  command -v "$b" >/dev/null 2>&1 || missing="$missing $b"
done
if [ -z "$missing" ]; then
  ok "all desktop-control binaries present"
else
  warn "missing desktop binaries:$missing (those verbs will refuse politely)"
fi

hdr "─── quick-path backend ───"
# The CLI path needs no key. An API key that is present but unfunded is the
# trap that cost session 21 an evening: `auto` routes to it, every question
# 400s, and the CLI would ALSO break because the CLI prefers the key over the
# claude.ai login (the dispatcher strips it from subprocesses to prevent that).
backend=$(sed -n 's/^ *quick_backend: *\([a-z_]*\).*/\1/p' config.yaml | head -1)
[ -n "$backend" ] && ok "quick_backend: $backend" || warn "quick_backend not set in config.yaml"
if [ -f .env ] && grep -q '^[[:space:]]*ANTHROPIC_API_KEY=' .env 2>/dev/null; then
  ok "ANTHROPIC_API_KEY present in .env"
  if git check-ignore -q .env 2>/dev/null; then
    ok ".env is gitignored"
  else
    bad ".env is NOT gitignored — it holds a secret"
  fi
  if [ "$backend" = "claude_cli" ]; then
    warn "key present but backend pinned to claude_cli (set quick_backend: auto once the account has API credit)"
  fi
else
  ok "no API key — running on the subscription CLI path (that is fine)"
fi

hdr "─── phone access (tailscale) ───"
if command -v tailscale >/dev/null 2>&1; then
  if tailscale status >/dev/null 2>&1; then
    ok "tailscale up"
    if systemctl --user is-active --quiet mission-tailscale-serve 2>/dev/null; then
      ok "mission-tailscale-serve active"
    else
      warn "mission-tailscale-serve not active (dashboard not reachable from the phone)"
    fi
    if grep -q 'public_hosts' config.yaml && ! grep -q 'public_hosts: *\[\] *$' config.yaml; then
      ok "security.public_hosts set (the origin guard trusts your tailnet name)"
    else
      warn "security.public_hosts empty — phone POSTs would be rejected by the origin guard"
    fi
  else
    warn "tailscale installed but not up (run scripts/setup_tailscale.sh)"
  fi
else
  ok "tailscale not installed (phone access optional)"
fi

hdr "─── systemd timers ───"
# derive the list from the shipped unit files so a new timer can't be silently
# missed by a stale hardcoded list
shopt -s nullglob
timers=(systemd/mission-*.timer)
shopt -u nullglob
if [ ${#timers[@]} -eq 0 ]; then
  warn "no systemd/mission-*.timer files found"
fi
for f in "${timers[@]}"; do
  t=$(basename "$f" .timer)
  if systemctl --user is-enabled --quiet "$t.timer" 2>/dev/null; then
    ok "$t.timer enabled"
  else
    warn "$t.timer not enabled"
  fi
done

echo
echo "Done. ✓ = fine, ! = check when convenient, ✗ = likely broken."
