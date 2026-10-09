#!/bin/bash
set -e

# Render supplies PORT; the upstream server reads MFP_PORT.
export MFP_PORT="${MFP_PORT:-${PORT:-8000}}"
export MFP_HOST="${MFP_HOST:-0.0.0.0}"
export MFP_TRANSPORT="${MFP_TRANSPORT:-streamable-http}"

# Virtual display for Camoufox login challenges.
rm -f /tmp/.X99-lock
Xvfb :99 -screen 0 1280x800x24 -ac &
XVFB_PID=$!
openbox >/tmp/openbox.log 2>&1 &

cleanup() {
  kill "$XVFB_PID" 2>/dev/null || true
}
trap cleanup SIGTERM SIGINT

exec python -m mfp_mcp.server
