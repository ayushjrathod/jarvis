#!/usr/bin/env bash
# Spotify media control setup: store client-credentials for catalog search.
#
# Playback itself needs nothing from Spotify's servers — it happens in your own
# desktop client over MPRIS/D-Bus. The Web API is used ONLY to turn "bohemian
# rhapsody" into a spotify:track: URI, under the client-credentials flow:
# app-only auth, no user login, no redirect URI, no Premium requirement.
#
# Idempotent: re-run any time to replace the stored credentials.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CREDS="$ROOT/data/spotify.json"

cat <<'EOF'
Spotify media control setup
===========================

One-time app registration (free, no Premium needed):

  1. Open https://developer.spotify.com/dashboard and log in.
  2. "Create app". Name/description: anything ("mission-control").
  3. Redirect URI: http://127.0.0.1:8765/ (unused by us, but the form
     requires one).
  4. Check "Web API", save.
  5. Open the app -> Settings -> copy the Client ID, then "View client secret".

EOF

read -rp "Client ID: " CLIENT_ID
read -rsp "Client secret (hidden): " CLIENT_SECRET
echo

if [[ -z "$CLIENT_ID" || -z "$CLIENT_SECRET" ]]; then
  echo "error: both values are required" >&2
  exit 1
fi

mkdir -p "$(dirname "$CREDS")"
umask 077
# Both values go in on STDIN, never in argv (fixed 2026-08-10). `python3 -c …
# "$CLIENT_SECRET"` published the secret in /proc/<pid>/cmdline, which is world
# readable, for the lifetime of that call — undoing the `read -rs` and the
# `umask 077` immediately above it. `printf` is a shell builtin, so the pipe
# never forks a process carrying the values either.
# Written to a temp file and moved into place so a failed encode can't leave
# truncated (or empty) credentials behind on a re-run.
TMP="$CREDS.tmp.$$"
trap 'rm -f "$TMP"' EXIT
printf '%s\n%s\n' "$CLIENT_ID" "$CLIENT_SECRET" | python3 -c '
import json, sys
client_id = sys.stdin.readline().rstrip("\n")
client_secret = sys.stdin.readline().rstrip("\n")
print(json.dumps({"client_id": client_id, "client_secret": client_secret}, indent=2))
' > "$TMP"
chmod 600 "$TMP"
mv -f "$TMP" "$CREDS"
echo "wrote $CREDS (mode 600, gitignored)"

echo
echo "verifying: requesting a token and searching the catalog..."
"$ROOT/.venv/bin/python" - <<'PY'
import sys
sys.path.insert(0, ".")
from dispatcher.config import Config
from dispatcher import spotify

cfg = Config.load()
try:
    hit = spotify.search(cfg, "bohemian rhapsody", "track")
except spotify.MediaError as e:
    print(f"  FAIL: {e}")
    sys.exit(1)
if not hit:
    print("  FAIL: token worked but the search returned nothing")
    sys.exit(1)
print(f"  ok: search -> {hit['name']} by {hit['artist']}")

print("  spotify client running (MPRIS reachable): "
      f"{'yes' if spotify.is_running() else 'no — open Spotify, or it will be launched on demand'}")
PY

cat <<EOF

Done. Restart the dispatcher to pick up the credentials:
  systemctl --user restart mission-dispatcher

Then try:
  curl -sN -X POST 127.0.0.1:8765/media -H 'content-type: application/json' \\
       -d '{"command":"play bohemian rhapsody"}'
  ...or just say: "hey jarvis, play bohemian rhapsody"
EOF
