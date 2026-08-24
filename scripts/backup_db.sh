#!/usr/bin/env bash
# Nightly SQLite backup via the online-safe .backup command; keeps 14 days.
# Verifies before trusting: a truncated file (256KB, zero tables) passes
# PRAGMA integrity_check with flying colours, so the check is integrity AND
# table-count parity with the live db. A bad backup is deleted and the script
# fails — a failed timer is visible, a corrupt "backup" is not.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/backups
# .timeout: don't fail if the dispatcher holds a write lock at 03:30
sqlite3 -cmd '.timeout 5000' data/mission.db ".backup 'data/backups/mission-$(date +%F).db'"
NEW="data/backups/mission-$(date +%F).db"
LIVE_TABLES=$(sqlite3 -cmd '.timeout 5000' data/mission.db \
  "SELECT count(*) FROM sqlite_master WHERE type='table';")
BACKUP_TABLES=$(sqlite3 -cmd '.timeout 5000' "$NEW" \
  "SELECT count(*) FROM sqlite_master WHERE type='table';")
INTEGRITY=$(sqlite3 -cmd '.timeout 5000' "$NEW" "PRAGMA integrity_check;")
if [ "$INTEGRITY" != "ok" ] || [ "$BACKUP_TABLES" != "$LIVE_TABLES" ]; then
  echo "backup FAILED verification (integrity=$INTEGRITY tables=$BACKUP_TABLES/$LIVE_TABLES); removing $NEW" >&2
  rm -f "$NEW"
  exit 1
fi
# Retention matches dated backups only. Anything else in this dir — notably
# the pre-purge recovery point (mission-pre-purge-*.db), our only rollback
# before a knowledge-graph purge — is never auto-deleted.
find data/backups -name 'mission-[0-9]*.db' -mtime +14 -delete
echo "backup written: data/backups/mission-$(date +%F).db"
