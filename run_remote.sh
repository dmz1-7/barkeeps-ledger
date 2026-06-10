#!/usr/bin/env bash
# Start Barkeep's Ledger + a public Cloudflare quick-tunnel, detached so they
# keep running after you close the terminal. Prints the public https URL.
#
# Usage:  ./run_remote.sh
# Notes:  keep this PC awake while you're away; the URL changes each run.
set -e
cd "$(dirname "$0")"

# Fetch cloudflared if it's not here yet.
if [ ! -x ./cloudflared ]; then
  curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 -o cloudflared
  chmod +x cloudflared
fi

# Start the app (detached) unless it's already responding.
if ! curl -fsS -o /dev/null http://localhost:8088/api/health 2>/dev/null; then
  setsid nohup .venv/bin/python app.py > /tmp/ledger.log 2>&1 &
  sleep 3
fi

# Start the tunnel (detached) and wait for the URL to appear.
: > /tmp/cf.log
setsid nohup ./cloudflared tunnel --no-autoupdate --url http://localhost:8088 > /tmp/cf.log 2>&1 &
for i in $(seq 1 20); do
  url=$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' /tmp/cf.log | head -1)
  [ -n "$url" ] && break
  sleep 1
done
echo "Barkeep's Ledger is live at:"
echo "  ${url:-<still starting — check /tmp/cf.log>}"
