#!/usr/bin/env bash
# Nightly SQLite backup via the online-safe .backup command; keeps 14 days.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/backups
sqlite3 data/mission.db ".backup 'data/backups/mission-$(date +%F).db'"
find data/backups -name 'mission-*.db' -mtime +14 -delete
echo "backup written: data/backups/mission-$(date +%F).db"
