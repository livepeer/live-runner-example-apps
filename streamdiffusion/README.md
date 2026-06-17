# StreamDiffusion app (new runner, trickle realtime video)

A realtime **StreamDiffusion** app on the Livepeer network, hosted on the **new
runner** (Josh's general-runner stack). It's the [`echo`](../echo) app with the
per-frame transform swapped for StreamDiffusion img2img: it receives a live video
stream over trickle, diffuses each frame against a text prompt, and streams the
result back. Re-prompt live over the control path.

Like [`vllm`](../vllm), it's attached as a **static runner** — best for a fixed,
long-running GPU deployment: the orchestrator is configured with the app's URL in
`runners.json` and health-polls `/health`, so there's no SDK registrar or
heartbeat in the app.

|              |                                                  |
| ------------ | ------------------------------------------------ |
| App id       | `livepeer-sample/streamdiffusion`                |
| Transport    | trickle (realtime video in/out + `/update`)      |
| Registration | static (`runners.json` + `/health` poll, like vllm) |
| Port         | 8900                                             |
| Requires     | NVIDIA GPU (TensorRT engines built per-arch)     |

## Self-contained

This example owns its inference: `sd.py` wraps **only** the upstream
[`streamdiffusion`](https://github.com/daydreamlive/StreamDiffusion) library
(`StreamDiffusionWrapper`), which builds and loads its own TensorRT engines. It
does **not** depend on the (deprecated) `ai-runner` image or its internal
`pipeline`/`runner` modules. The serving layer is purely the new-runner SDK
(`livepeer_gateway`), exactly like echo.

| File | Role |
| ---- | ---- |
| `sd.py` | StreamDiffusion driver (load / process / update_prompt) + `--build` for engines |
| `runner.py` | the app: warm-up build, `/health` + `/status`, creates trickle `in`/`out`, runs `sd.process` per frame |
| `runners.json` | static config the orchestrator loads (`-liveRunnerConfig`) |
| `client.py` | publishes a file or webcam, reads the diffused output, re-prompts live |
| `view.sh` | live showcase: webcam → StreamDiffusion → an ffplay window (low-latency) |
| `Dockerfile` | CUDA 12.8 / torch 2.7.1 / streamdiffusion + tensorrt + the SDK |
| `build_engines.sh` | builds the image and compiles TensorRT engines into `./models` |
| `docker-compose.yml` | offchain orchestrator (`-liveRunnerConfig`) + this GPU app |

## How it's wired

**Static registration (like vllm).** The orchestrator reads `runners.json` via
`-liveRunnerConfig`, learns the app at `http://app:8900`, and health-polls
`/health`. The app needs no registrar or heartbeat.

**Warm-up gates routing.** On boot `runner.py` compiles/loads the TensorRT
engines (build-if-missing); until they're ready `/health` returns 503, so the
orchestrator routes nothing to it. When ready `/health` returns 200 and sessions
flow. If the build fails the container exits non-zero (health never passes).
Watch progress with `curl localhost:8900/status` → `{state: building|ready|error}`.

**Per frame.** On `/stream` the app creates trickle `in`/`out` channels; each
input frame is decoded (PyAV) to RGB, run through `sd.process` (StreamDiffusion
img2img, GPU), and published to `out`. `/update` calls `sd.update_prompt` to
change the look mid-stream. The client uses the echo flow (`reserve_session` +
`/stream`), routed by **app id**.

## Run (offchain / free, on your GPU)

TensorRT engines are GPU-arch specific. The app **builds them on first boot**
into the mounted `./models` (warm-up); later boots reuse the cache. You can
pre-bake them instead with `build_engines.sh` to avoid a slow first start.

```sh
# (optional) pre-bake engines for this GPU; otherwise the app builds on first boot.
./build_engines.sh                                  # sd-turbo @ 512x512 by default
#   ./build_engines.sh stabilityai/sdxl-turbo 512x512   # heavier SDXL variant

# Bring up the orchestrator + the StreamDiffusion app.
docker compose up -d --build
curl -s localhost:8900/status            # building -> ready (first boot compiles engines)
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # app once healthy

# Stream a webcam and watch the diffused output live (opens an ffplay window).
uv sync
./view.sh "a cyberpunk portrait, neon lighting"        # webcam -> SD -> live window

docker compose down
```

- `view.sh` is the turnkey live showcase. Under the hood it's just the client
  piping MPEG-TS to a low-latency player (prefers `mpv`, falls back to `ffplay`);
  run the client directly for more control:
  `uv run client.py --webcam /dev/video0 --prompt "..." --reprompt "8=an oil painting" --output - | mpv --profile=low-latency -`.
- **No window / runner logs `Trickle ... channel does not exist`?** The player
  didn't open a window, so it never drained the pipe; the client's stdout then
  blocks and the output channel times out. It's a dead viewer, not a pipeline
  bug. `ffplay` (SDL/X11) can fail to open under Wayland — `mpv` is more reliable
  there. Sanity-check your player can open a window at all:
  `mpv --force-window=immediate av://lavfi:testsrc` (or `ffplay -f lavfi -i testsrc`).
- **Guaranteed alternative — record then play.** Skips the live pipe entirely:
  `uv run client.py --webcam /dev/video0 --prompt "..." --max-frames 300 --output live.ts`
  then `mpv live.ts`.
- File instead of webcam: `uv run client.py ~/samples/clip.mp4 --output out.ts`.
- `--reprompt SECONDS=PROMPT` (repeatable) changes the prompt live via `/update`.
- `--video-size`, `--fps` tune webcam capture. The default `640x480` is a
  near-universal native YUYV size and the app resizes it to the engine size, so
  capture size is independent of the engines. A non-native size can make v4l2
  fall back to MJPG and fail to decode; pass a square native size (e.g.
  `--video-size 440x440`) to avoid the 4:3→1:1 aspect squish.

## Notes

- **One GPU, one session.** The app holds a single active session (like echo).
  Engines load at boot (warm-up), so the first session starts streaming
  immediately once `/status` is `ready`.
- **Fixed model per container.** `SD_MODEL` / `SD_WIDTH` / `SD_HEIGHT` (env) set
  what the container builds and serves; the client only changes the **prompt**.
  Changing model/resolution needs a rebuild of engines.
- **Fleet / strict mode.** Set `SD_REQUIRE_PREBUILT=1` to skip the boot build and
  require engines already present in `./models` (fail fast if missing) — for
  hosts using pre-baked or downloaded engine bundles.
- **Version sensitivity.** The streamdiffusion + TensorRT + torch stack is
  pinned in the Dockerfile; keep the versions aligned if you bump them.
- On-chain (paid) hosting adds a price (`runners.json` already carries
  `price_info`) + a signer, like the other examples; this README covers the free
  offchain path.
