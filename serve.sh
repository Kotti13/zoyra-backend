#!/usr/bin/env bash
# Run the API on this Mac and publish it with Tailscale Funnel (stable https URL).
# Usage: ./serve.sh        (Ctrl+C stops both)
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8010}"
TS_SOCK="$HOME/.tailscale-user/tailscaled.sock"
TS="$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)"
# Userspace (no-sudo) daemon: start it if it is not running.
if [ ! -S "$TS_SOCK" ] && command -v tailscaled >/dev/null; then
  mkdir -p "$HOME/.tailscale-user"
  nohup tailscaled --tun=userspace-networking --statedir="$HOME/.tailscale-user" \
    --socket="$TS_SOCK" >"$HOME/.tailscale-user/tailscaled.log" 2>&1 &
  sleep 4
fi
[ -S "$TS_SOCK" ] && TS="$TS --socket=$TS_SOCK"

if [ ! -f .env ] || ! grep -q '^API_KEY=.\+' .env; then
  echo "warning: API_KEY is empty in .env. The public URL will be open to anyone." >&2
fi

cleanup() { $TS funnel --https=443 off >/dev/null 2>&1 || true; kill "$API_PID" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

uv run uvicorn app.main:app --host 127.0.0.1 --port "$PORT" &
API_PID=$!

$TS funnel --bg "$PORT"
echo; $TS funnel status
echo "API running. Ctrl+C to stop."

# Keep the Mac awake while serving.
caffeinate -i -w "$API_PID" &
wait "$API_PID"
