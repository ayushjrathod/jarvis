#!/usr/bin/env bash
# Install/refresh all Mission Control systemd user units.
# Enables: dispatcher (+ dashboard), daily-brief/weekly-review/backup timers.
# Jarvis voice + dictation are installed but NOT enabled — need mic/`input`
# group (and ydotool for dictation) first:
#   systemctl --user enable --now mission-jarvis mission-dictate
set -euo pipefail
cd "$(dirname "$0")"

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cp mission-*.service mission-*.timer "$UNIT_DIR/"
systemctl --user daemon-reload

systemctl --user enable --now mission-dispatcher.service
systemctl --user enable --now mission-daily-brief.timer
systemctl --user enable --now mission-weekly-review.timer
systemctl --user enable --now mission-backup.timer

echo
systemctl --user --no-pager status mission-dispatcher.service | head -5
echo
systemctl --user list-timers --no-pager | grep -E 'NEXT|mission'
echo
echo "Dashboard: http://127.0.0.1:8765/"
echo "Jarvis wake word (after input-group + audio setup): systemctl --user enable --now mission-jarvis"
echo "Dictation on F9 (after ydotool setup): systemctl --user enable --now mission-dictate"
echo "Survive logout/reboot without login: loginctl enable-linger $USER"
