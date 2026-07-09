# Transcode (batch + live, one container)

One `livepeer-samples/transcode` container that replaces both the `transcode-vod`
and `transcode-live` examples: a single image, one ffmpeg engine, **two surfaces**,
driven by go-livepeer's **native profile contract** so it's a drop-in for the
native transcoding pipeline.

|              |                                                        |
| ------------ | ------------------------------------------------------ |
| App id       | `transcode/ffmpeg`                                      |
| Surfaces     | `POST /transcode` (batch/VOD) · `POST /transcode/live` (trickle) |
| Config       | `{"profiles": [<JsonProfile>, …]}` (go-livepeer native) |
| Registration | dynamic (default) or static (`runners.json`)           |
| Port         | 8990                                                   |

> [!NOTE]
> Registration defaults to **dynamic** (self-register + heartbeat) — the right fit for an elastic transcode fleet with self-reported `capacity`. It also supports **static**: run with `--no-register` and point the orchestrator at the included `runners.json` (it health-polls `GET /healthz`). Static suits a fixed, centrally-managed set of nodes; see [Registration](../README.md#registration) in the repo README.

## The contract: native profiles

Both surfaces take the same ladder — go-livepeer's `ffmpeg.JsonProfile`:

```json
{ "name": "720p", "width": 0, "height": 720, "bitrate": 0, "fps": 0, "fpsDen": 1,
  "profile": "H264High", "gop": "", "encoder": "H264", "quality": 0,
  "colorDepth": 8, "chromaFormat": 0 }
```

- `encoder`: `H264` (default) · `H265`/`HEVC` · `VP8` · `VP9` · `AV1`.
- `width`/`height`: 0 on an axis keeps aspect.
- rate control: `bitrate` if set, else `quality` (CRF), else a sane per-codec default.
- `gop`: `"intra"` (all-intra) or seconds between keyframes; `profile`, `colorDepth`, `chromaFormat` map to the ffmpeg equivalents.

`profiles.py` is the single mapping to ffmpeg, shared by both surfaces — same config in, same encode out.

## Run

```sh
docker compose up -d --build
uv sync
```

### Batch (VOD) — `POST /transcode`

File in, one rendition per profile out. Base64 for small test clips; **URL / object
storage at scale** (bytes never touch the control plane).

```sh
# base64 (small clips): two renditions, written to out-<name>.<ext>
uv run client.py clip.mp4 --heights 720,360
uv run client.py clip.mp4 --profiles '[{"name":"720p","height":720,"bitrate":3000000,"profile":"H264High"}]'

# object storage (scale): runner fetches input_url, PUTs each rendition
uv run client.py --input-url https://.../src.mp4 \
  --profiles '[{"name":"720p","height":720}]' \
  --output-urls '{"720p":"https://.../720p.mp4?sig=..."}'
```

### Live (trickle) — `POST /transcode/live`

Real-time rendition ladder over trickle (same as the old `transcode-live`):

```sh
uv run client.py clip.mp4 --live --heights 720,360   # writes out-<name>.ts
```

## Notes

- **AV1** is just `encoder: "AV1"` → `libsvtav1` (CPU) or `av1_nvenc` (GPU via `TRANSCODE_AV1_ENCODER` + a CUDA base). H.264/H.265 have the same override (`TRANSCODE_H264_ENCODER`/`TRANSCODE_H265_ENCODER` → `h264_nvenc`/`hevc_nvenc`); H.264 CPU (`libx264`) remains the default and needs no GPU. VP8/VP9 stay CPU-only — ffmpeg has no GPU encoder for either.
- **Multi-GPU**: batch jobs round-robin across every GPU the container can see (`nvidia-smi -L` at startup, `-gpu <index>` passed to any `*_nvenc` encoder) — `count: all` in the GPU reservation puts the whole box to work instead of pinning every job to device 0.
- **Batch honors the full profile** (bitrate, profile, gop, pix fmt). **Live honors `height`/`fps`/`encoder`/`bitrate`/`profile`** via the SDK's per-track encoder-options support ([livepeer-python-gateway#35](https://github.com/livepeer/livepeer-python-gateway/pull/35), pinned in `pyproject.toml`/`Dockerfile`); only `gop` is still segment-driven on live.
- The 54 TB AV1 archival job is the **batch** surface with `input_url`/`output_urls` + a fan-out driver over the clip list.

## Capacity (concurrent sessions)

`--capacity` / `TRANSCODE_CAPACITY` (default 4) is the max concurrent jobs the runner advertises; the orchestrator won't schedule beyond it — the live-runner equivalent of the old pipeline's `-maxSessions`. It's a **static** number you set: there is **no auto-detection**. The right value depends on your **hardware** and the **profile ladder** (resolution, codec, number of renditions, CPU vs GPU), so a single number can't be universal — benchmark for your workload and set it.

### How to pick the number

The limiting signal differs by surface:

- **Batch (VOD)** is throughput-bound: run one representative job, then run `N` in parallel and find the highest `N` where per-job time stays acceptable and CPU/GPU/VRAM aren't saturated.
- **Live (trickle)** must sustain real time: capacity can't exceed the encode's real-time factor.

Measure the real-time factor with ffmpeg on your ladder — the `speed=Nx` it prints is the realtime multiple:

```sh
# one rendition; repeat / chain filters for your full ladder (one decode -> N encodes = one session)
ffmpeg -benchmark -i sample.mp4 -vf scale=-2:720 -c:v libx264 -preset superfast -f null - 2>&1 | grep speed=
# speed=8.2x  ->  ~8 concurrent realtime 720p streams on this box (per encoder), before headroom
```

Then validate by actually running `N` concurrent sessions of your real workload and watching:

- **CPU** with `htop` — target ~70–80%, leave headroom.
- **GPU** with `nvidia-smi dmon -s u` — SM %, **encoder %** (`enc`), and VRAM. Also note your GPU's **NVENC session cap** (consumer cards historically ~3–8; L4/L40/datacenter much higher) — GPU capacity can be limited by that, not just compute.
- **Live keeping up:** the SDK's per-track publish stats expose `time_debt_s` and `frames_dropped_debt` — if debt grows or debt-drops appear, that session isn't sustaining real time, i.e. you're over capacity.

Rule of thumb: set `capacity` to the largest `N` that holds with ~20–30% headroom, and **re-benchmark whenever the hardware or the profile ladder changes**. (Automating this — a startup benchmark + NVENC-limit clamp behind `--auto-capacity` — is a natural production follow-up; the reference example keeps it manual.)

_On-chain: layer `docker-compose.onchain.yml` and pass `--signer http://localhost:7936` (batch)._
