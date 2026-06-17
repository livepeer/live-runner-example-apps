#!/usr/bin/env bash
# Live viewer: webcam -> StreamDiffusion -> a player window, low-latency.
#
# Streams your webcam through the running StreamDiffusion app and shows the
# diffused output live. Re-prompt by re-running with a new prompt. Needs a
# display and the stack already up (`docker compose up -d`).
#
# Prefers mpv (native Wayland, robust low-latency pipe playback) and falls back
# to ffplay. IMPORTANT: the player must keep draining the pipe -- if its window
# never opens, the client's stdout blocks, the output trickle channel times out,
# and the runner logs "Trickle ... channel does not exist". A dead viewer, not a
# pipeline bug. If you see that, your player isn't opening a window (see README).
#
# Usage:
#   ./view.sh                                   # default prompt + /dev/video0
#   ./view.sh "an impressionist oil painting"   # custom prompt
#   ./view.sh "neon cyberpunk portrait" /dev/video2
set -eu

PROMPT="${1:-a cyberpunk portrait, neon lighting, intricate detail}"
SOURCE="${2:-/dev/video0}"

# Source is a webcam device by default; pass a file path as arg 2 to stream a
# video file instead (handy to demo without a camera).
if [ -f "$SOURCE" ]; then
    SRC_ARGS=("$SOURCE")
else
    SRC_ARGS=(--webcam "$SOURCE")
fi

if command -v mpv >/dev/null; then
    # --force-window opens the window immediately (before the first frame) so the
    # pipe is drained from the start. Keep a small cache: mpv needs it to probe the
    # MPEG-TS and sync to a keyframe -- fully disabling it (--no-cache /
    # --profile=low-latency) shows a black window because the decoder never locks
    # on. nobuffer demux keeps latency low while still letting it decode.
    # --cache=yes is required: mpv needs it to probe a piped MPEG-TS (defaults
    # fail with "Failed to recognize file format"). --cache-pause=no is the key
    # for a live diffused stream: the runner emits slightly below 30fps, so a
    # realtime player would perpetually pause-to-buffer and show a black window;
    # this keeps it presenting frames as they arrive. --no-correct-pts +
    # untimed-ish keeps latency low without stalling on PTS gaps.
    PLAYER=(mpv --force-window=immediate
            --cache=yes --cache-secs=0.3 --cache-pause=no
            --demuxer-lavf-o=fflags=+nobuffer
            --title=StreamDiffusion -)
elif command -v ffplay >/dev/null; then
    PLAYER=(ffplay -hide_banner -loglevel info -probesize 32k -analyzeduration 0
            -fflags nobuffer -flags low_delay -framedrop -i -)
else
    echo "ERROR: need mpv or ffplay (install mpv or ffmpeg)" >&2
    exit 1
fi

# Capture below the diffusion throughput (~25fps) so the runner keeps up and no
# backlog forms -- the main cause of latency creeping up over time.
uv run client.py "${SRC_ARGS[@]}" --fps 20 --prompt "$PROMPT" --output - \
  | "${PLAYER[@]}"
