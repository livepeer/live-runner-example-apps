# Realtime transcode app (trickle)

Realtime video transcoding on the Livepeer network over **trickle** — the segmented media transport built for live. A continuous stream is decoded once from a trickle input channel and re-encoded into a **rendition ladder** — one output channel per profile. This follows the SDK's `echo` example (the live pattern), with a **resize** instead of a blur. PyAV does the decode/encode (it's ffmpeg under the hood). The app **self-registers** (dynamic) and handles **multiple concurrent streams** (configurable `capacity`).

|              |                                          |
| ------------ | ---------------------------------------- |
| App id       | `transcode/live-h264`                    |
| Transport    | trickle (segmented `video/mp2t`)         |
| Registration | dynamic (self-registers via the SDK)     |
| Port         | 8990                                     |
| Renditions   | a profile ladder (one output per height) |
| Concurrency  | multi-session, `capacity` (default 4)    |

Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared on-chain/payment setup are in the [repo README](../README.md).

> **Scope & when to use:** this is the realtime/live core — file in → trickle → transcode → file out, real-time paced (exactly like the SDK `echo` example). For *vanilla* transcoding, Livepeer's native transcoding network is the right tool; the live runner is worth it for **custom or AI-augmented** transcode (upscaling, watermarking, transcode + inference). It's also **video-only** (audio passthrough is an extension, as in `echo`).

## How it's wired

```
                                          ┌─▶ trickle "720p" ──▶ client out-720p.ts
client: file frames ─▶ trickle "in" ─▶ worker (decode once,
                                          └─▶ trickle "360p" ──▶ client out-360p.ts
                                              resize per profile, re-encode)
```

- **Worker** (`runner.py`): on `POST /transcode {"profiles": [...]}`, calls `create_trickle_channels` for `in` + one channel per rendition, decodes the `in` channel **once** with `MediaOutput(on_frame=...)`, and fans out — rescales each frame per profile (`av reformat`) and re-encodes to that profile's channel via `MediaPublish`. It keeps a **per-session pipeline** and registers with `capacity` > 1, so several streams run at once.
- **Client** (`client.py`): `reserve_session` → POST a profile ladder (`--heights 720,360`) to get the channel URLs → publish the source file's frames (real-time paced) to `in` → write each output channel to `<prefix>-<name>.ts`.
- **The client sends the profile ladder** in the request body. Profiles are an *app-level* convention, not a runner concept.

### Profiles

Each profile is a JSON object; the worker honors **`height`** (resize) and **`fps`** (frame-dropped to the target — the encoder fps alone is only a hint). `codec` is plumbed through but see the limitation below:

```json
{"name": "720p", "height": 720, "fps": 30, "codec": "libx264"}
```

Send a ladder two ways from the client:
- `--heights 720,360` — shorthand for height-only renditions.
- `--profiles '[{"name":"720p","height":720,"fps":30},{"name":"360p","height":360,"fps":15}]'` — full control.

> **Effectively H.264 only — AV1/HEVC/VP9 don't work yet (SDK limitation).** The encoder exists in PyAV (`libsvtav1`, `libx265`, `libvpx-vp9`, `av1_nvenc` are all present), but `MediaPublish` passes **libx264-specific encoder options** (`preset=superfast`, `tune=zerolatency`, `forced-idr`, `bf=0`) to every codec, so non-x264 encoders fail at `avcodec_open2` with `Invalid argument`. Codec-aware encoder options are an SDK change.
>
> **`bitrate` is not supported either.** The SDK's `VideoOutputConfig` has no `bit_rate` field, so per-rendition bitrate can't be set — the encoder uses its default.
>
> Both are worthwhile SDK additions; until then this app is H.264, default-bitrate.

Trickle rides a **reserved session**, so on-chain `reserve_session` pays at reserve and the session is metered while the stream runs (continuous stream = continuous billing — the right model for live).

## Run offchain (free)

```sh
docker compose up -d --build
# make a test clip if you don't have one:
ffmpeg -f lavfi -i testsrc=duration=5:size=1280x720:rate=30 -pix_fmt yuv420p clip.mp4
uv run client.py clip.mp4 --discovery https://localhost:8935/discovery --heights 720,360 --output-prefix out
docker compose down
```

This writes one file per rendition — `out-720p.ts`, `out-360p.ts` — each from a single decode. Check them with `ffprobe out-360p.ts`. (Pass `--heights 360` for a single rendition.) The runner advertises `capacity 4`, so multiple clients can transcode concurrently, each on its own session.

## Run on-chain (paid)

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build
uv run client.py clip.mp4 --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936 --heights 720,360 --output-prefix out
docker compose -f docker-compose.yml -f docker-compose.onchain.yml down
```

See [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README for the wallet/RPC requirements.

## Note: containerized worker (vs the SDK `echo` example)

The SDK `echo` example runs the worker on the **host**, so `127.0.0.1:8935` reaches the orchestrator. This worker runs in a **container**, which needs two adjustments (both in `runner.py`):

- **Create channels via the known orchestrator URL**, not the request. `create_trickle_channels(request, ...)` uses the `Livepeer-Session-Control` header, which advertises the orchestrator's `serviceAddr` (`127.0.0.1:8935` → points at the container itself). Pass the **session id as a string** plus explicit `orchestrator_url` / `runner_id` / `session_token` so the network-resolvable `orchestrator:8935` is used.
- **Use `internal_url` for the worker's own pub/sub.** Each channel has a public `url` (serviceAddr, for the host client) and an `internal_url` (the `-liveRunnerAddr`, resolvable in-container). The worker reads/writes `internal_url`; the public `url` is handed back to the client.

## Going live: RTMP in, HLS out

This example streams a file over the realtime path. A real live service adds an **operator-side media gateway** in front that bridges edge protocols to this trickle core:

```
OBS ──RTMP──▶ [media gateway: ffmpeg ingest] ──frames──▶ (this transcoder over trickle) ──▶ [gateway: HLS mux] ──HLS──▶ viewers
```

The gateway runs ffmpeg for RTMP ingest + HLS packaging and uses the same `MediaPublish`/`MediaOutput` trickle bridge as `client.py` — broadcasters push RTMP with a stream key, viewers watch an HLS URL, and neither touches the SDK. That media plane (not the transcode step) is the heavy part; the classic RTMP→transcode→HLS path needs **no WebRTC** (WebRTC is only for sub-second/browser ingest). It's a separate, larger example.
