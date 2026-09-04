#!/usr/bin/env bash
# Nightly SQLite backup via the online-safe .backup command; keeps 14 days.
# Verifies before trusting: a truncated file (256KB, zero tables) passes
# PRAGMA integrity_check with flying colours, so the check is integrity AND
# table-count parity with the live db. A bad backup is deleted and the script
# fails — a failed timer is visible, a corrupt "backup" is not.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/backups
# Write .part first, verify, then move into place: a killed or truncated run
# must never leave a half-written mission-<date>.db looking like tonight's
# backup (same class as the zero-table file the verify below catches).
PART="data/backups/mission-$(date +%F).db.part"
NEW="data/backups/mission-$(date +%F).db"
# .timeout: don't fail if the dispatcher holds a write lock at 03:30
sqlite3 -cmd '.timeout 5000' data/mission.db ".backup '$PART'"
LIVE_TABLES=$(sqlite3 -cmd '.timeout 5000' data/mission.db \
  "SELECT count(*) FROM sqlite_master WHERE type='table';")
BACKUP_TABLES=$(sqlite3 -cmd '.timeout 5000' "$PART" \
  "SELECT count(*) FROM sqlite_master WHERE type='table';")
INTEGRITY=$(sqlite3 -cmd '.timeout 5000' "$PART" "PRAGMA integrity_check;")
if [ "$INTEGRITY" != "ok" ] || [ "$BACKUP_TABLES" != "$LIVE_TABLES" ]; then
  echo "backup FAILED verification (integrity=$INTEGRITY tables=$BACKUP_TABLES/$LIVE_TABLES); removing $PART" >&2
  rm -f "$PART"
  exit 1
fi
mv -f "$PART" "$NEW"
# Retention matches dated backups only. Anything else in this dir — notably
# the pre-purge recovery point (mission-pre-purge-*.db), our only rollback
# before a knowledge-graph purge — is never auto-deleted.
find data/backups -name 'mission-[0-9]*.db' -mtime +14 -delete
# The "nightly backup" used to cover SQLite only: data/spotify.json (the only
# unrecoverable local secret — credentials, no Premium needed to replace, but
# no way to re-derive) and config.yaml (every grant, budget and timer the box
# runs on) died with the disk. Tiny files, same verified dir, same retention
# story by date suffix; mode 600 on the secret. ~/.claude-per (login +
# sessions) and data/models stay out deliberately — machine-scale, and the
# login re-derives via device flow.
DAY=$(date +%F)
[ -f data/spotify.json ] && install -m 600 data/spotify.json "data/backups/spotify-$DAY.json"
[ -f config.yaml ] && cp config.yaml "data/backups/config-$DAY.yaml"
find data/backups -name 'spotify-[0-9]*.json' -mtime +14 -delete
find data/backups -name 'config-[0-9]*.yaml' -mtime +14 -delete
echo "backup written: data/backups/mission-$(date +%F).db"
