# ffmpeg tool app (tool → agent capability)

Expose a **tool** as a Livepeer capability an **agent** can call. `ffmpeg` is the
worked example: one app, `livepeer/ffmpeg`, with several **operations**
(`transcode`, `clip`, `thumbnail`). The client picks an op and sends a file; the
runner runs a vetted ffmpeg command and returns the result. Plain HTTP
request/response — no live media plane. The app **self-registers** (dynamic).

|              |                                      |
| ------------ | ------------------------------------ |
| App id       | `livepeer/ffmpeg`                    |
| Transport    | HTTP (JSON request/response)         |
| Registration | dynamic (self-registers via the SDK) |
| Mode         | persistent (single-shot by nature; payment pending [go-livepeer#3955](https://github.com/livepeer/go-livepeer/issues/3955)) |
| Port         | 5000                                 |

Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared
on-chain/payment setup are in the [repo README](../README.md).

## The pattern (why one app, many ops)

This generalizes the single-op [`vod-transcode`](../vod-transcode) example into the
reusable **tool-runner** shape — copy it for `imagemagick`, `yt-dlp`, `pandoc`, a
code sandbox, etc.:

- **One app, op-as-param.** Register *at the granularity clients pay at*. Models are
  shopped individually → one app per model (see `vllm`). Tool *operations* aren't —
  they share a binary, hardware, and pricing → **one app, op chosen per request**.
- **`GET /ops`** advertises the machine-readable op/param schema.
- **[`SKILL.md`](SKILL.md)** is the agent-facing contract — when and how to call each op.
- **Command templates are the security boundary.** Each op maps validated params to a
  fixed ffmpeg argv; the app never passes through arbitrary flags (and `encoder` is
  allowlisted). This is the real work — the live-runner proxying is free.

## How it's wired

The input file is base64'd into a JSON request, so it rides the **standard buffered
call path** (the same `call_runner` + payment flow as hello-world). The app runs
`ffmpeg` and returns the output bytes, base64'd, in the JSON response. Fine for short
clips; **production passes URLs + object storage** instead of inlining bytes.

Wire protocol on `POST /run`:
- request: `{"op": "clip", "input_b64": "...", "start": 3, "end": 8}`
- response: `{"output_b64": "...", "bytes": N, "media_type": "video/mp4"}`

Ops + params: see [`SKILL.md`](SKILL.md), or `GET /ops` for the JSON schema.

## Run offchain (free)

```sh
docker compose up -d --build
# make a tiny test clip if you don't have one:
ffmpeg -f lavfi -i testsrc=duration=3:size=640x480:rate=24 -pix_fmt yuv420p clip.mp4

uv run client.py --op transcode --height 480 --input clip.mp4 --output out.mp4
uv run client.py --op clip --start 1 --end 3   --input clip.mp4 --output cut.mp4
uv run client.py --op thumbnail --at 1.5        --input clip.mp4 --output thumb.jpg

docker compose down
```

`client.py` reserves a session, sends the file through the orchestrator to the app,
and writes the result. Inspect with `ffprobe out.mp4` / open `thumb.jpg`.

Confirm the runner registered and list its ops:

```sh
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # livepeer/ffmpeg
```

## Run on-chain (paid)

Layer `docker-compose.onchain.yml` to add a remote signer and run the orchestrator
on-chain. Needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an
orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup)
in the repo README. Copy `.env.example` to `.env` and fill it in, then:

```sh
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build
uv run client.py --op transcode --height 480 --input clip.mp4 --output out.mp4 \
  --discovery https://localhost:8935/discovery --signer http://localhost:7936
```

## GPU

CPU (`libx264`) by default so it runs anywhere. To run on an **NVIDIA GPU**, an
operator just layers the GPU overlay — no code or image change:

```sh
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

That gives the container GPU access and sets `FFMPEG_DEFAULT_ENCODER=h264_nvenc`, so
transcodes use the GPU by default (callers can still pass `encoder` explicitly).
Requires the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
on the host. The image's `ffmpeg` already includes `h264_nvenc`, so **no CUDA base
image is needed** for plain nvenc — only CUDA *filters* (e.g. `scale_npp`) would
require one. Combine with payments by adding `-f docker-compose.onchain.yml`.
