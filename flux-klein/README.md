# FLUX Klein app (realtime trickle video-to-video)

A realtime **FLUX.2-klein-4B** image-to-image app on the Livepeer network. It receives a live video stream over **trickle**, transforms every frame against a text prompt using a **Krea-style feedback loop**, and streams the result back. Prompt, noise seed and camera anchoring are all adjustable live over the control path.

Like [`vllm`](../vllm), it's attached as a **static runner** — the best fit for a fixed, long-running GPU deployment: the orchestrator is configured with the app's URL in `runners.json` and health-polls `/health`, so there's no SDK registrar or heartbeat in the app.

|              |                                             |
| ------------ | ------------------------------------------- |
| App id       | `livepeer-example/flux-klein`               |
| Runner mode  | persistent (held-open session)              |
| Registration | static (orchestrator config + health poll)  |
| Transport    | trickle (realtime video in/out + `/update`) |
| Port         | 8720                                        |

**Requires an NVIDIA GPU** with ~13 GB VRAM. Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared on-chain/payment setup are in the [repo README](../README.md).

> [!NOTE]
> This example pins the SDK's `rs/live-runner-session-payments` branch rather than the `ja/live-runner` the other examples use, because a long-lived streaming session needs `session.start_payments()`. See the note in `pyproject.toml`.

| File | Role |
| ---- | ---- |
| `flux_klein.py` | FLUX Klein driver (load / process / update prompt, seed, blend) + the feedback loop |
| `runner.py` | the app: warm-up load, `/health` `/status` `/stats`, trickle `in`/`out`, latest-frame inference worker |
| `client.py` | publishes a file or webcam (Linux/macOS), reads the output, updates prompt/seed/blend live |
| `view.sh` | turnkey showcase: webcam → FLUX Klein → an mpv/ffplay window |
| `runners.json` | static config the orchestrator loads (`-liveRunnerConfig`) |
| `FEEDBACK.md` | developer-experience notes collected while building this example |

## How it's wired

**Static registration.** The orchestrator reads `runners.json` via `-liveRunnerConfig`, learns the app at `http://app:8720`, and health-polls `/health` — no registrar, no heartbeat, no SDK registration in the app. The SDK is used only for trickle plumbing once a session starts.

**Self-contained inference.** `flux_klein.py` wraps `diffusers` (`Flux2KleinPipeline`) directly. It does **not** run [Daydream Scope](https://github.com/daydreamlive/scope) or any plugin runtime. The reference implementation, [`hthillman/scope-flux-klein`](https://github.com/hthillman/scope-flux-klein), is a *Scope plugin*; this example borrows its recipe — the Krea-style feedback loop and the truncated-schedule `refine_frame` — and reimplements it standalone.

**Warm-up gates routing.** On boot `runner.py` loads the FLUX pipeline, downloading ~15 GB of weights on first run into the mounted `./models`. Until it's ready `/health` returns 503, so the orchestrator routes nothing; once ready it returns 200 and sessions flow. A failed load exits non-zero. Watch `curl localhost:8720/status` → `{state: building|ready|error}`.

**Per frame — latest-frame worker.** On `/stream` the app opens trickle `in`/`out` channels with `create_trickle_channels`. Decoded frames are *not* queued: the frame callback stashes only the **newest** frame, and a worker processes whatever is newest whenever the GPU is free. Frames arriving while Klein is busy are skipped — essential for a model slower than the input frame rate, otherwise the backlog and end-to-end latency grow without bound. `/update` changes prompt/seed/blend mid-stream. The client drives it with `reserve_session` → `MediaPublish`/`MediaOutput`, routed by **app id**, the same flow as [`echo`](../echo).

**Session stats.** `GET /stats` reports live throughput while a session runs: trickle input (`video_frames_decoded`, `input_fps`, decoder queue and subscriber health), output (per-track `frames_in`, `encode_fps`, publisher throughput) and inference (`frames_processed`, `avg/last_inference_s`, `inference_fps`, `frames_skipped`), plus the active `seed`/`input_blend`.

## The feedback loop

FLUX Klein is a 4-step model — full inference on every frame would be far too slow for video. The [Krea](https://www.krea.ai/)-style feedback loop avoids that:

- **First frame** → full inference (`image_to_image`), cached as the previous output.
- **Every frame after** → `refine_frame`: the incoming frame is blended with the previous output (so the model refines something that already carries the prompt, not the raw camera feed), then **partially denoised**, running only `feedback_strength × steps` steps. `0.5` runs ~2 of 4 steps.

Because `Flux2KleinPipeline` has no `strength` parameter (it conditions on the image via joint attention), `refine_frame` hand-rolls a truncated flow-matching schedule against diffusers internals. Prompt embeddings, VAE batch-norm stats and latent position IDs are cached across frames to keep per-frame cost down.

### Tuning: shimmer vs. drift

Two loop parameters shape the look, both adjustable live:

- **`seed`** — `-1` (default) injects fresh random noise every frame: organic but shimmery. A **fixed seed** reuses the same noise each frame: much steadier, but the constant bias compounds, so the image drifts harder toward the prompt and seed-specific artifacts can "burn in".
- **`input_blend`** — the camera's weight (0..1, default 0.5) in the prev-output/camera blend. Higher anchors the stream to reality and washes burned-in artifacts out; lower lets the prompt take over.

A good starting combination is **`--seed 50 --input-blend 0.85`** (steady *and* anchored). Raise `input_blend` if fixed-seed artifacts appear; lower it if the prompt is too weak. Seeds are not interchangeable — each one settles into its own attractor, so if a fixed seed keeps drawing the same stray shapes, try another value before reaching for the blend.

## Run offchain (free)

```sh
docker compose up -d --build                                         # first boot downloads ~15 GB of weights
curl -s localhost:8720/status                                        # building -> ready
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-example/flux-klein

uv sync
./view.sh "a cyberpunk portrait, neon lighting"                      # webcam -> FLUX Klein -> live window

docker compose down
```

`view.sh` is just `client.py` piped to a player (prefers `mpv`, falls back to `ffplay`), auto-detecting macOS (AVFoundation) vs Linux (v4l2). Point it at a remote orchestrator with `DISCOVERY=https://host:8935/discovery ./view.sh "..."`.

Full control via the client:

```sh
uv run client.py --webcam-macos 0 --fps 10 \
    --prompt "a watercolor painting" \
    --seed 50 --input-blend 0.85 \
    --reprompt "10=Change to a 90-year-old man" \
    --reprompt "20=seed:7" \
    --reprompt "30=blend:0.6" \
    --output - | mpv --profile=low-latency --no-cache -
```

- **Webcam by platform:** Linux uses `--webcam /dev/videoN` (v4l2); macOS uses `--webcam-macos INDEX` (AVFoundation — list cameras with `ffmpeg -f avfoundation -list_devices true -i ""`, and grant your terminal camera access under System Settings → Privacy & Security → Camera, or capture times out). On macOS capture always runs at `--capture-fps` (default 30, since AVFoundation rejects unsupported rates) and `--fps` is honoured by client-side decimation.
- **Live updates:** `--reprompt "SECONDS=..."` (repeatable) fires `/update` mid-stream — plain text changes the prompt, `seed:N` the noise seed (`seed:-1` = random), `blend:X` the camera weight. Seed/blend changes never touch the prompt.
- **Publish less, not slower:** `--fps 10` is plenty; Klein only processes ~19 fps anyway, and less input means less decode work everywhere.
- **File instead of webcam:** `uv run client.py ~/samples/clip.mp4 --output out.ts`.
- **No window, and the runner logs `Trickle ... channel does not exist`?** The player never opened, so it never drained the pipe; the client's stdout then blocks and the output channel times out. That's a dead viewer, not a pipeline bug. `ffplay` (SDL/X11) can fail under Wayland — `mpv` is more reliable there. If in doubt, record then play: `uv run client.py --webcam-macos 0 --max-frames 300 --output live.ts`.
- **`mpegts: Packet corrupt` warnings** appear at every trickle segment joint (independent TS chunks concatenated). Cosmetic; silence with `--msg-level=ffmpeg/demuxer=error`.

## Run on-chain (paid)

Layer `docker-compose.onchain.yml` to add the shared remote signer and re-point the orchestrator on-chain. The price is already in `runners.json`, so unlike the dynamic examples there's no `--price` on the app. This needs an Ethereum RPC, a funded signer wallet and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env       # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build
uv run client.py --webcam /dev/video0 \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936 --output out.ts
docker compose -f docker-compose.yml -f docker-compose.onchain.yml down
```

Keep `PIXELS_PER_UNIT` small — if it's too large the per-unit price floors to 0 wei and calls are effectively free despite being on-chain.

## Parameters

| Runner env | Default | Meaning |
| ---------- | ------- | ------- |
| `FLUX_MODEL` | `black-forest-labs/FLUX.2-klein-4B` | HF model id |
| `FLUX_WIDTH` / `FLUX_HEIGHT` | `384` × `384` | inference resolution (snapped to /16) |
| `FLUX_STEPS` | `4` | full-inference steps; refine runs a fraction |
| `FLUX_FEEDBACK` | `0.5` | fraction of steps per refine frame; `int(FLUX_STEPS × FLUX_FEEDBACK)` must be ≥ 2, or the refine starts at ~zero noise and stops transforming |
| `FLUX_GUIDANCE` | `1.0` | prompt adherence (Klein's recommended value) |
| `FLUX_MAX_TEXT_TOKENS` | `128` | prompt token budget. The DiT attends over text+image tokens jointly, so padding to the pipeline default of 512 makes it carry ~512 dead tokens next to only 576 image tokens. Raise it only if long prompts get truncated — the runner logs the embed shape on every prompt change |
| `FLUX_SEED` | `-1` | noise seed; `-1` random per frame, fixed = steady |
| `FLUX_INPUT_BLEND` | `0.5` | camera weight in the feedback blend |
| `FLUX_CPU_OFFLOAD` | off | set `1` for < 16 GB VRAM (slower per frame) |
| `FLUX_COMPILE` | off (unset) | `torch.compile` mode for the transformer + VAE, passed through verbatim: `default` (fast compile), `max-autotune` (longest warm-up, fastest kernels), `reduce-overhead` (CUDA graphs). Adds minutes to warm-up |
| `FLUX_BATCH` | `1` | frames refined per GPU pass; `2` trades latency and feedback fidelity for throughput (see below) |

Client flags: `--prompt`, `--seed`, `--input-blend` set the session at `/stream` (omit to keep the runner defaults); `--reprompt` changes any of them live. Plus `--fps` (publish rate), `--capture-fps` (macOS capture), `--video-size`, `--max-frames`, `--output`, `--discovery`, `--signer`.

### Throughput

Measured on an NVIDIA RTX 6000 Pro at 384×384, 4 steps, feedback 0.5:

| Config | fps |
| ------ | --- |
| Defaults | **~19 fps** |
| `FLUX_BATCH=2 FLUX_COMPILE=max-autotune` | **~22.7 fps** (best measured) |

### `FLUX_BATCH` — frame batching (opt-in)

At 384×384 with 2 refine steps the GPU is not saturated at batch 1. `FLUX_BATCH=N` makes the runner collect the newest N frames and refine them **in one pass** from the same previous output. Because the pass is bandwidth-bound rather than compute-bound at these shapes, `N=2` costs only ~1.25–1.35× the wall time of `N=1` for 2× the frames.

Two costs come with it:

- **Latency.** You can't refine a frame that hasn't arrived yet, so the batch adds the collection wait — about one frame interval (~34 ms at a 29 fps input) per additional frame. At `N=2` that's one extra frame of delay.
- **Stride-N feedback.** Every frame in a batch descends from the *same* parent output, so the feedback loop advances once per batch instead of once per frame. At `N=2` this is subtle (a faint pairing in the temporal texture); at `N=4` and above it is clearly visible as stepping.

`N=2` is the only value worth using. `FLUX_BATCH=1` (the default) is byte-identical to the non-batched path. Batching is orthogonal to `FLUX_COMPILE`, but a batched run compiles **two** shapes (N, plus 1 for a partial batch at a stream tail); warm-up compiles both before `/health` reports ready, so neither stalls a live session. `/stats` reports the configured `batch` plus `inference.last_batch_size`, which drops below `FLUX_BATCH` when the input can't fill the buffer.

## Notes

- **One GPU, one session.** The app holds a single active session (`capacity: 1` in `runners.json`). The pipeline loads at boot, so the first session starts immediately once `/status` is `ready`.
- **Fixed model/resolution per container.** Model, resolution, steps and feedback are load-time env vars; prompt, seed and blend are per-session and live-updatable.
- **Framerate expectations.** FLUX Klein is not a TensorRT-class realtime model — expect ~19 fps at the defaults, not a locked 30. The latest-frame worker plus segment skipping keeps latency bounded at roughly one inference interval. Lower resolution trades quality for fps; check `GET /stats` for live numbers.
- **diffusers version.** `Flux2KleinPipeline` and the flux2 internals that `refine_frame` reaches into ship from **diffusers git, not a stable release** — the Dockerfile installs diffusers from GitHub. If those private APIs change, `refine_frame` is where it would break; the full-inference paths don't touch internals.
