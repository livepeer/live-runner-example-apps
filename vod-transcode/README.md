# VOD transcoding app (ffmpeg over HTTP)

File/VOD transcoding on the Livepeer network by wrapping **ffmpeg**. The client POSTs a video, the app scales it to a target height with H.264 and returns the result. This is the simple, fully-feasible transcoding shape — plain HTTP request/response, no live media plane (no RTMP, WebRTC, HLS, or trickle). The app **self-registers** (dynamic), like hello-world.

|              |                                          |
| ------------ | ---------------------------------------- |
| App id       | `transcode/h264-720p`                    |
| Transport    | HTTP (JSON request/response)             |
| Registration | dynamic (self-registers via the SDK)     |
| Port         | 5000                                     |

Runs on **CPU (`libx264`) by default** so it works anywhere; a **GPU (`h264_nvenc`) is faster** (see below). Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared on-chain/payment setup are in the [repo README](../README.md).

> **Scope:** this is *VOD* (transcode a file). *Live* transcoding (RTMP in → HLS out) is a separate, much larger example — it needs a media gateway and trickle. And for *vanilla* transcoding, Livepeer's native transcoding network is the right tool; the live runner shines when you need **custom or AI-augmented** transcoding (exotic codecs, watermarking, upscaling, transcode + inference).

## How it's wired

The video is base64'd into a JSON request, so it rides the **standard buffered call path** (the same `call_runner` + payment flow as hello-world) — no streaming needed. The app runs `ffmpeg` and returns the transcoded bytes, base64'd, in the JSON response. That's fine for short clips; **production would pass URLs + object storage** instead of inlining bytes (base64 adds ~33% and buffers everything in memory).

Wire protocol on `POST /transcode`:
- request: `{"video_b64": "...", "height": 720}`
- response: `{"output_b64": "...", "bytes": N}`

## Run offchain (free)

```sh
docker compose up -d --build
# make a tiny test clip if you don't have one:
ffmpeg -f lavfi -i testsrc=duration=3:size=640x480:rate=24 -pix_fmt yuv420p clip.mp4
uv run client.py --discovery https://localhost:8935/discovery --input clip.mp4 --output out.mp4
docker compose down
```

`client.py` reserves a session, sends the clip through the orchestrator to the app, and writes the transcoded `out.mp4`. Inspect it with `ffprobe out.mp4`.

## Run on-chain (paid)

Layer `docker-compose.onchain.yml` to add a remote signer and run the orchestrator on-chain. Needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build
uv run client.py --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936 --input clip.mp4 --output out.mp4
docker compose -f docker-compose.yml -f docker-compose.onchain.yml down
```

The app advertises a price; the SDK pays per call through the remote signer. Transcoding is the one app where the per-pixel pricing model is the natural unit (it's video).

## GPU (faster)

`libx264` (CPU) works everywhere but is slower. For GPU NVENC:

1. Use a CUDA-enabled ffmpeg base image in the `Dockerfile` (e.g. an `nvidia/cuda` runtime image with an ffmpeg build that has `--enable-nvenc`, or `jrottenberg/ffmpeg` CUDA tags).
2. Set `TRANSCODE_ENCODER=h264_nvenc` (env or `.env`).
3. Uncomment the `deploy:` GPU reservation block in `docker-compose.yml`.

## Run without Docker

Start an orchestrator built from `ja/live-runner`, then the app and client directly (the app needs `ffmpeg` installed):

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
uv run runner.py --orchestrator http://localhost:8935 --orchSecret abcdef
uv run client.py --input clip.mp4 --output out.mp4
```
