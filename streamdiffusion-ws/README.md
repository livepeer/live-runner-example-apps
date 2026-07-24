# StreamDiffusion (WebSocket, reverse-proxied)

Realtime prompt-driven img2img on the Livepeer network, using **daydream's [StreamDiffusion](https://github.com/daydreamlive/StreamDiffusion) `realtime-img2img` server run unmodified**. The prompt **auto-cycles** through a curated, SFW art-style bank every minute — point a copyright-free feed at it and get a hands-free, ever-shifting AI restyle you can stream out.

This example also exists to contrast the two ways to put an app on the live runner:

- [`../streamdiffusion`](../streamdiffusion) — **SDK-embedded, trickle.** You write a `runner.py` that reads/writes trickle channels and an `sd.py` that calls the model. You own the media loop.
- **this one** — **reverse-proxied, WebSocket.** The app is a third-party container that speaks its own HTTP/WS protocol and knows nothing about Livepeer. The orchestrator reverse-proxies its endpoints; you write only a client.

|              |                                             |
| ------------ | ------------------------------------------- |
| App id       | `livepeer-sample/streamdiffusion-ws`        |
| Runner mode  | persistent (held-open session)              |
| Registration | static (`runners.json`)                     |
| Transport    | WebSocket + MJPEG (the app's own protocol)  |
| Port         | 7860 (the StreamDiffusion server)           |

**Requires an NVIDIA GPU.** Prerequisites (Docker, `uv`, the not-yet-released SDK) are in the [repo README](../README.md).

> [!IMPORTANT]
> This example drives a **metered session** (`start_payments` / `payment_interval` / `aclose`), which lives on the SDK's `rs/live-runner-session-payments` branch — `pyproject.toml` pins it there until it merges into `ja/live-runner`.

## How it's wired

The container runs daydream's FastAPI server (`demo/realtime-img2img/main.py`) as-is; `runners.json` registers it as a **static** runner (`runner_url: http://app:7860`, `health_url: /api/queue`). The orchestrator health-polls `/api/queue` and reverse-proxies every other path to the container — so a container with **zero Livepeer code** works behind the proxy. `reserve_session` creates the session entirely at the orchestrator (it never calls the app).

All the Livepeer integration is therefore in [client.py](client.py) — grep `# Livepeer:`: `reserve_session` (1) reserves the paid session and returns the proxied `app_url`; the client then drives the app's native protocol over that url (2, `ws_connect`); `session.aclose` (3) ends it. The app exposes:

- `GET /api/queue` — liveness (the runner `health_url`).
- `WS /api/ws/{uuid}` — input: control messages + JPEG frames.
- `GET /api/stream/{uuid}` — output: an MJPEG (`multipart/x-mixed-replace`) stream; opening it lazily builds the pipeline and drives the per-frame pump.
- `POST /api/blending` — set the prompt (`{"prompt_list": [[prompt, weight]]}`).

## Prompts (auto-cycling, SFW)

By default the client rotates the prompt every `--prompt-interval` seconds (60 by default) from [prompts.py](prompts.py) — a hand-vetted **style × modifier** combinator (watercolor, ukiyo-e, cyberpunk neon, claymation, …). It's deliberately **not** an LLM/prompt-model: for a public stream you want deterministic, safe output, so every token is curated. Edit `prompts.py` to taste. Pin a single prompt instead with `--prompt "..."`.

## Run offchain (free)

Start the stack and confirm the runner registered:

```sh
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-sample/streamdiffusion-ws
```

The **first** stream open compiles TensorRT engines for your GPU (slow, minutes; cached under `./models` after). Pre-bake by opening the stream once, or just accept a slow first frame.

**From a webcam** — ffmpeg emits MJPEG on stdout, the client streams it through the app (auto-cycling prompts), and the diffused MJPEG is played by ffplay:

```sh
ffmpeg -f v4l2 -input_format mjpeg -framerate 30 -video_size 640x480 -i /dev/video0 \
  -vf scale=512:512 -f image2pipe -c:v mjpeg -q:v 5 - \
  | uv run client.py - --output - \
  | ffplay -f mjpeg -fflags nobuffer -flags low_delay -i -
```

Device numbers vary; confirm your camera node first (`v4l2-ctl --list-devices`, `ffplay -f v4l2 -i /dev/videoN`). macOS: `-f avfoundation -i 0`; Windows: `-f dshow -i video="<name>"`.

Add `--prompt "van Gogh oil painting, vivid colors"` to pin one style, or `--prompt-interval 30` to change every 30s.

Stop the stack with `docker compose down`.
