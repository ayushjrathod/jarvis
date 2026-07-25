#!/usr/bin/env bash
# Idempotent setup for phone access over Tailscale (future/phone-access-and-
# feature-gaps.md — Tailscale was the chosen option). Brings up tailscaled,
# makes this user a Tailscale operator so `tailscale serve` runs without sudo,
# publishes the loopback dispatcher on the tailnet as HTTPS via Tailscale Serve,
# and prints the exact config.yaml line to trust that hostname in the origin
# guard. Re-runnable — every step is a no-op if already done.
#
# The dispatcher stays bound to 127.0.0.1: Tailscale is the access control, so
# no public port is opened and no token middleware is needed. Only devices on
# your tailnet (this box + your phone) can reach it.
#
# Usage: scripts/setup_tailscale.sh
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v tailscale >/dev/null 2>&1; then
  echo "tailscale is not installed. On Arch:" >&2
  echo "  sudo pacman -S tailscale" >&2
  echo "then re-run this script." >&2
  exit 1
fi

# 1. Daemon up (system service; needs sudo). Safe to re-run.
if ! systemctl is-active --quiet tailscaled; then
  echo "==> enabling tailscaled (sudo)"
  sudo systemctl enable --now tailscaled
fi

# 2. Join the tailnet if not already logged in (opens a browser auth URL).
if ! tailscale status >/dev/null 2>&1; then
  echo "==> tailscale up — authenticate in the browser window that opens"
  sudo tailscale up
fi

# 3. Let THIS user run `serve` without sudo (operator), and enable HTTPS certs.
echo "==> setting $USER as Tailscale operator"
sudo tailscale set --operator="$USER"

# 4. Discover this machine's MagicDNS name for the origin-guard line + URL.
DNSNAME="$(tailscale status --json 2>/dev/null \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("Self",{}).get("DNSName","").rstrip("."))' \
  || true)"

# 5. Install + enable the Serve unit (publishes loopback:8765 as tailnet HTTPS).
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cp systemd/mission-tailscale-serve.service "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now mission-tailscale-serve.service || {
  echo "!! serve unit failed to start. Common cause: HTTPS certificates are not" >&2
  echo "   yet enabled for your tailnet. In the admin console enable HTTPS +" >&2
  echo "   MagicDNS (https://login.tailscale.com/admin/dns), then re-run this." >&2
}

echo
echo "----------------------------------------------------------------------"
if [[ -n "$DNSNAME" ]]; then
  echo "Tailnet URL for your phone:  https://$DNSNAME/"
  echo
  echo "Add this to config.yaml under dispatcher.security so the dashboard's"
  echo "own POSTs aren't rejected by the CSRF origin guard, then restart the"
  echo "dispatcher (systemctl --user restart mission-dispatcher):"
  echo
  echo "    public_hosts: [\"$DNSNAME\"]"
else
  echo "Could not read the tailnet DNS name (is MagicDNS enabled?). Once you"
  echo "know it (tailscale status), add it to config.yaml:"
  echo "    dispatcher.security.public_hosts: [\"<machine>.<tailnet>.ts.net\"]"
fi
echo "----------------------------------------------------------------------"
echo
echo "On the phone: install the Tailscale app, sign in to the same tailnet,"
echo "then open the URL above. Add it to the home screen to install the PWA."
