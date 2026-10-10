#!/bin/bash
set -e

# Render supplies PORT; the upstream server reads MFP_PORT.
export MFP_PORT="${MFP_PORT:-${PORT:-8000}}"
export MFP_HOST="${MFP_HOST:-0.0.0.0}"
export MFP_TRANSPORT="${MFP_TRANSPORT:-streamable-http}"

# Keep temp files off Render's small /tmp volume (2GB cap killed the
# instance when the browser wrote temp/cache files there).
export TMPDIR="/home/mcp/tmp"
mkdir -p "$TMPDIR"

# No crash dumps eating disk.
export MOZ_CRASHREPORTER_DISABLE=1
export MOZ_CRASHREPORTER_NO_REPORT=1

XVFB_PID=""
cleanup() {
  if [ -n "$XVFB_PID" ]; then
    kill "$XVFB_PID" 2>/dev/null || true
  fi
}
trap cleanup SIGTERM SIGINT

# Virtual display only for headed/manual-login mode (MFP_HEADED=1).
# Headless runs skip Xvfb/openbox entirely: less RAM, less disk.
if [ "${MFP_HEADED:-0}" = "1" ]; then
  rm -f /tmp/.X99-lock
  Xvfb :99 -screen 0 1280x800x24 -ac &
  XVFB_PID=$!
  openbox >/home/mcp/tmp/openbox.log 2>&1 &
  export DISPLAY=:99
else
  unset DISPLAY
fi

exec python -m mfp_mcp.server
