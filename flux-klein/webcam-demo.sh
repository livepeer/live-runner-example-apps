#!/usr/bin/env bash
# Live webcam -> flux-klein -> your screen, against an orchestrator.
#
#   ./webcam-demo.sh                       # live mpv window, default prompt
#   ./webcam-demo.sh "an oil painting"     # live window, custom prompt
#   ./webcam-demo.sh "a neon city" save    # save a ~10s clip and open it
#
# Env overrides:
#   DISCOVERY   orchestrator discovery url (default: pon's orch, below)
#   WEBCAM      capture device            (default: /dev/video0)
#   FPS         publish rate              (default: 8)
set -euo pipefail
cd "$(dirname "$0")"

DISCOVERY="${DISCOVERY:-http://154.61.61.108:8787/discovery}"
PROMPT="${1:-a psychedelic landscape, vivid colors, intricate details}"
MODE="${2:-live}"
DEVICE="${WEBCAM:-/dev/video0}"
FPS="${FPS:-8}"
PY=.venv/bin/python

[[ -x "$PY" ]] || { echo "no venv here — run 'uv sync' in $(pwd) first" >&2; exit 1; }

if [[ "$MODE" == "save" ]]; then
  OUT="flux-klein-out.ts"
  "$PY" client.py --webcam "$DEVICE" --discovery "$DISCOVERY" \
    --prompt "$PROMPT" --fps "$FPS" --max-frames 300 --output "$OUT"
  echo "saved $OUT" >&2
  command -v mpv    >/dev/null && exec mpv "$OUT"
  command -v ffplay >/dev/null && exec ffplay -autoexit "$OUT"
  echo "install mpv or ffplay to view $OUT" >&2
else
  if   command -v mpv    >/dev/null; then PLAYER=(mpv --no-cache --untimed --profile=low-latency -)
  elif command -v ffplay >/dev/null; then PLAYER=(ffplay -fflags nobuffer -flags low_delay -framedrop -)
  else echo "need mpv or ffplay for live view (or use: $0 \"$PROMPT\" save)" >&2; exit 1
  fi
  echo "live: $DEVICE -> flux-klein -> ${PLAYER[0]} (ctrl-c to stop)" >&2
  "$PY" client.py --webcam "$DEVICE" --discovery "$DISCOVERY" \
    --prompt "$PROMPT" --fps "$FPS" --output - | "${PLAYER[@]}"
fi
