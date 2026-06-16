#!/usr/bin/env bash
# Build StreamDiffusion TensorRT engines for THIS machine's GPU, into ./models.
#
# Self-contained: builds our own app image and runs sd.py --build inside it,
# which constructs StreamDiffusionWrapper with build_engines_if_missing=True so
# the upstream streamdiffusion library compiles the engines. No ai-runner image.
# Engines are GPU-arch specific, so this must run on the box that serves the app.
#
# Usage:
#   ./build_engines.sh [MODEL] [WIDTHxHEIGHT] [MODELS_DIR]
#     MODEL       HF model id (default: stabilityai/sd-turbo)
#     WIDTHxHEIGHT resolution to compile for (default: 512x512)
#     MODELS_DIR  where engines + HF cache land (default: ./models)
#
# Env: HF_TOKEN (if a gated model needs it), GPUS (docker --gpus, default all),
#      IMAGE (image tag to build/use, default example-apps-streamdiffusion).
set -euo pipefail

MODEL="${1:-stabilityai/sd-turbo}"
SIZE="${2:-512x512}"
MODELS_DIR="${3:-$(pwd)/models}"
GPUS="${GPUS:-all}"
IMAGE="${IMAGE:-example-apps-streamdiffusion}"
WIDTH="${SIZE%x*}"; HEIGHT="${SIZE#*x}"

command -v docker >/dev/null || { echo "ERROR: docker not found" >&2; exit 1; }
mkdir -p "$MODELS_DIR"

echo ">> building app image: $IMAGE"
docker build -t "$IMAGE" .

echo ">> compiling engines  model=$MODEL  ${WIDTH}x${HEIGHT}  -> $MODELS_DIR/engines"
echo ">> NOTE: first build downloads weights and compiles TensorRT engines (long, GPU-bound)."
docker run --rm --name sd-engines-prepare --gpus "$GPUS" \
  -v "$MODELS_DIR:/models" \
  -e HF_TOKEN="${HF_TOKEN:-}" \
  "$IMAGE" \
  python sd.py --build --model "$MODEL" --width "$WIDTH" --height "$HEIGHT"

# Files written as root inside the container; hand them back.
docker run --rm -v "$MODELS_DIR:/models" "$IMAGE" chown -R "$(id -u):$(id -g)" /models

echo ">> Done. Engines under: $MODELS_DIR/engines"
echo ">> 'docker compose up -d' now starts the orchestrator + this app with these engines mounted."
