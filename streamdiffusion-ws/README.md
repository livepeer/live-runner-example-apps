# StreamDiffusion (WebSocket, reverse-proxied)

Realtime prompt-driven img2img on the Livepeer network, using **daydream's [StreamDiffusion](https://github.com/daydreamlive/StreamDiffusion) `realtime-img2img` server run unmodified**. This example exists to contrast the two ways to put an app on the live runner:

- [`../streamdiffusion`](../streamdiffusion) — **SDK-embedded, trickle.** You write a `runner.py` that reads/writes trickle channels and an `sd.py` that calls the model. You own the media loop.
- **this one** — **reverse-proxied, WebSocket.** The app is a third-party container that speaks its own HTTP/WS protocol and knows nothing about Livepeer. The orchestrator reverse-proxies its endpoints; you write only a client.

|              |                                             |
| ------------ | ------------------------------------------- |
| App id       | `livepeer-sample/streamdiffusion-ws`        |
| Runner mode  | persistent (held-open session)              |
| Registration | static (`runners.json`)                     |
| Transport    | WebSocket + MJPEG (the app's own protocol)  |
| Port         | 7860 (the StreamDiffusion server)           |

Prerequisites (Docker, `uv`, the not-yet-released SDK, an NVIDIA GPU) are in the [repo README](../README.md).

## How it's wired

The container runs daydream's FastAPI server (`demo/realtime-img2img/main.py`) as-is. It exposes:

- `GET /api/queue` — liveness (used as the runner `health_url`).
- `WS /api/ws/{uuid}` — input channel: control messages + JPEG frames.
- `GET /api/stream/{uuid}` — output channel: an MJPEG (`multipart/x-mixed-replace`) stream. Opening it lazily builds the pipeline and drives the per-frame pump.
- `POST /api/blending`, `/api/params` — set prompt / seed / steps (not per-frame fields).

`runners.json` registers the container as a **static** runner (`runner_url: http://app:7860`, `health_url: /api/queue`). Nothing else is needed on the app side: the orchestrator health-polls `/api/queue` and reverse-proxies every other path to the container. `reserve_session` creates the session entirely at the orchestrator (it never calls the app), so a container with zero Livepeer code works behind the proxy.

The client (`client.py`) then:

1. `reserve_session` → gets the proxied `app_url`.
2. generates a `uuid`, `POST {app_url}/api/blending` to set the prompt.
3. opens `WS {app_url}/api/ws/{uuid}` and `GET {app_url}/api/stream/{uuid}` with the same uuid.
4. on each `send_frame` the server sends, replies `{"status":"next_frame"}` → params → the latest input JPEG; decodes output JPEGs off the MJPEG stream.

Input arrives as an MJPEG stream on stdin (pipe ffmpeg); the diffused MJPEG goes to stdout (pipe to a player). The client keeps only the latest input frame, so a slow diffuser never builds an input backlog.

## Run offchain (free)

Start the stack and confirm the runner registered:

```sh
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-sample/streamdiffusion-ws
```

The **first** stream open compiles TensorRT engines for your GPU (slow, minutes; cached under `./models` after). Pre-bake by opening the stream once, or just accept a slow first frame.

**From a webcam** — ffmpeg emits MJPEG on stdout, the client streams it through the app, and the diffused MJPEG is played by ffplay:

```sh
ffmpeg -f v4l2 -input_format mjpeg -framerate 30 -video_size 640x480 -i /dev/video0 \
  -vf scale=512:512 -f image2pipe -c:v mjpeg -q:v 5 - \
  | uv run client.py - --prompt "a psychedelic landscape, vivid colors" --output - \
  | ffplay -f mjpeg -fflags nobuffer -flags low_delay -i -
```

Device numbers vary; confirm your camera node first (`v4l2-ctl --list-devices`, `ffplay -f v4l2 -i /dev/videoN`). macOS: `-f avfoundation -i 0`; Windows: `-f dshow -i video="<name>"`.

Stop the stack with `docker compose down`.

## Trade-off vs the trickle example

- **Less code we own**: no `runner.py`, no `sd.py`, no `build_engines.sh` — the container is the app. We maintain a client + `runners.json` + a packaging `Dockerfile`.
- **We inherit their protocol and container**: the client must speak their two-channel WS+MJPEG dance, and their image drives the model config and engine build.
- **Warm-up is coarser**: the trickle example gates `/health` 503 until engines build; here `/api/queue` is always 200, so the first session just blocks on the lazy engine build instead of being held off.
- **Abrupt disconnects can wedge the server**: the demo runs a single global pipeline and doesn't always clean up when a stream is killed mid-flight. The client quits cleanly on input EOF (closing the WS), which is fine; if you `kill -9` a run and the runner drops out of discovery, `docker compose restart app`.

Engines persist under `./models` (the container's relative `engines/` dir is symlinked onto the mounted volume), so only the first run compiles them.

This is the general pattern for "bring your own inference container": if it serves HTTP/WS, register it static and let the orchestrator proxy it.
