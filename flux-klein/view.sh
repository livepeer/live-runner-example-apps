#!/usr/bin/env bash
# Turnkey live showcase: webcam -> FLUX Klein -> a low-latency player window.
# Under the hood it's just client.py piping MPEG-TS to a player (prefers mpv,
# falls back to ffplay). Auto-detects macOS (AVFoundation) vs Linux (v4l2).
#
#   ./view.sh "a cyberpunk portrait, neon lighting"
#   ./view.sh "an oil painting" 1                     # camera index (macOS) ...
#   ./view.sh "an oil painting" /dev/video2           # ... or device (Linux)
#   DISCOVERY=https://1.2.3.4:8935/discovery ./view.sh "..."   # remote orchestrator
set -euo pipefail

PROMPT="${1:-a psychedelic landscape, vivid colors, intricate details}"
DISCOVERY="${DISCOVERY:-https://localhost:8935/discovery}"

# macOS -> AVFoundation camera index (default 0); Linux -> v4l2 device.
if [[ "$(uname)" == "Darwin" ]]; then
    DEVICE="${2:-0}"
    WEBCAM=(--webcam-macos "$DEVICE")
else
    DEVICE="${2:-/dev/video0}"
    WEBCAM=(--webcam "$DEVICE")
fi

# Prefer uv if available, else the venv's python.
if command -v uv >/dev/null 2>&1; then
    RUN=(uv run client.py)
else
    RUN=(python client.py)
fi

if command -v mpv >/dev/null 2>&1; then
    PLAYER=(mpv --profile=low-latency --no-cache -)
elif command -v ffplay >/dev/null 2>&1; then
    PLAYER=(ffplay -fflags nobuffer -flags low_delay -)
else
    echo "need mpv or ffplay installed to view the stream" >&2
    exit 1
fi

"${RUN[@]}" "${WEBCAM[@]}" --prompt "$PROMPT" --discovery "$DISCOVERY" --output - | "${PLAYER[@]}"
