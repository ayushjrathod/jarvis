#!/usr/bin/env bash
# Nightly SQLite backup via the online-safe .backup command; keeps 14 days.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/backups
# .timeout: don't fail if the dispatcher holds a write lock at 03:30
sqlite3 -cmd '.timeout 5000' data/mission.db ".backup 'data/backups/mission-$(date +%F).db'"
# Retention matches dated backups only. Anything else in this dir — notably
# the pre-purge recovery point (mission-pre-purge-*.db), our only rollback
# before a knowledge-graph purge — is never auto-deleted.
find data/backups -name 'mission-[0-9]*.db' -mtime +14 -delete
echo "backup written: data/backups/mission-$(date +%F).db"
