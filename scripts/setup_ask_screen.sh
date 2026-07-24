#!/usr/bin/env bash
# Idempotent setup for ask-about-my-screen: installs jeepney into the venv and
# registers the GNOME custom shortcut that runs `python -m jarvis.ask_screen`.
# Usage: scripts/setup_ask_screen.sh [binding]   (default from config.yaml)
set -euo pipefail
cd "$(dirname "$0")/.."

.venv/bin/pip install --quiet jeepney
mkdir -p data/screenshots

BINDING="${1:-$(.venv/bin/python -c \
  "from jarvis.config import JarvisConfig; print(JarvisConfig.load().ask_screen['shortcut'])")}"

KEYPATH="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/mission-ask-screen/"
LIST_SCHEMA="org.gnome.settings-daemon.plugins.media-keys"
BINDING_SCHEMA="org.gnome.settings-daemon.plugins.media-keys.custom-keybinding"

current="$(gsettings get "$LIST_SCHEMA" custom-keybindings)"
if [[ "$current" != *"$KEYPATH"* ]]; then
  if [[ "$current" == "@as []" ]]; then
    gsettings set "$LIST_SCHEMA" custom-keybindings "['$KEYPATH']"
  else
    gsettings set "$LIST_SCHEMA" custom-keybindings "${current%]}, '$KEYPATH']"
  fi
fi

# GNOME spawns shortcut commands with no cwd in the repo — self-contained cmd.
# Shell-quote the repo path so a directory with spaces/specials still cd's
# correctly. NOTE: the path is baked in here — re-run this script after moving
# the repo, or the shortcut will cd into the old location.
REPO_Q=$(printf '%q' "$PWD")
gsettings set "$BINDING_SCHEMA:$KEYPATH" name "Mission ask screen"
gsettings set "$BINDING_SCHEMA:$KEYPATH" binding "$BINDING"
gsettings set "$BINDING_SCHEMA:$KEYPATH" command \
  "sh -c 'cd $REPO_Q && exec .venv/bin/python -m jarvis.ask_screen'"

echo "ask-screen ready: press $BINDING (dispatcher must be running)"
