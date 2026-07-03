#!/usr/bin/env bash
# Start Cloudflare quick tunnels for the gateway (:8080), MCP (:9000), and website
# (:8088); capture the random URLs and write them as env to .tunnels.env.
#
#   ./tunnels.sh            # start tunnels, write .tunnels.env
#   source .tunnels.env     # GATEWAY_URL / MCP_URL / WEBSITE_URL in your shell
#   ./tunnels.sh stop       # kill the tunnels
#
# Then start the website with those URLs (see the echo at the end).
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
LOGDIR="${TMPDIR:-/tmp}/lp-tunnels"
HERE="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$LOGDIR"

if [ "${1:-}" = "stop" ]; then
  for p in "$LOGDIR"/*.pid; do [ -f "$p" ] && kill "$(cat "$p")" 2>/dev/null && rm -f "$p"; done
  echo "tunnels stopped"; exit 0
fi

start() { # name port
  cloudflared tunnel --url "http://localhost:$2" >"$LOGDIR/$1.log" 2>&1 &
  echo $! >"$LOGDIR/$1.pid"
}
url_of() { # name -> waits for the https url
  for _ in $(seq 1 25); do
    u=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOGDIR/$1.log" 2>/dev/null | head -1)
    [ -n "$u" ] && { echo "$u"; return; }
    sleep 1
  done
}

start gateway 8080
start mcp 9000
start website 8088

GW="$(url_of gateway)"; MCP="$(url_of mcp)"; WEB="$(url_of website)"
cat >"$HERE/.tunnels.env" <<EOF
export GATEWAY_URL=$GW/v1
export MCP_URL=$MCP/mcp
export WEBSITE_URL=$WEB
EOF
echo "wrote $HERE/.tunnels.env:"
cat "$HERE/.tunnels.env"
echo
echo "next:"
echo "  source .tunnels.env"
echo "  # add \$WEBSITE_URL to the Auth0 SPA app (callbacks + web_origins), then start the site:"
echo "  # (env for the website below)"
