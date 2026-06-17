# Streaming ASR app (WebSocket speech-to-text)

Live speech-to-text on the Livepeer network over a **WebSocket** — the client streams audio *up* and gets transcripts streamed *back* on one socket. This is the example where WebSockets are genuinely required: HTTP can't stream audio upstream, and SSE is one-directional (server→client only). The app is a small aiohttp server wrapping `faster-whisper` that **self-registers** (dynamic), so it embeds the SDK and is its own server — the dynamic counterpart to the static vLLM example.

|              |                                          |
| ------------ | ---------------------------------------- |
| App id       | `whisper/base.en`                        |
| Transport    | WebSocket (`/transcribe`)                |
| Registration | dynamic (self-registers via the SDK)     |
| Port         | 5005                                     |

Runs on **CPU by default** so it works anywhere; a **GPU is recommended for low latency** (see below). Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared on-chain/payment setup are in the [repo README](../README.md).

## How it's wired

The orchestrator proxies the WebSocket upgrade straight through to the app (it's a transparent reverse proxy — your app speaks standard WS, nothing Livepeer-specific). The socket rides a **reserved session**: the client does one SDK `reserve_session` call, then opens a normal `wss://` connection to the session URL. On-chain, that session is the billing unit — `reserve_session` pays at reserve and the meter runs while the socket is open (continuous connection = continuous billing, which is the right model for live audio).

Wire protocol on `/transcribe`:
- client → server: binary frames of **16 kHz mono PCM (int16)**
- client → server: text `eos` to finish
- server → client: JSON `{"text": "...", "final": false|true}`

## Audio

Input must be **16 kHz mono WAV**. Convert anything with ffmpeg:

```sh
ffmpeg -i input.mp3 -ar 16000 -ac 1 sample.wav
```

## Run offchain (free)

```sh
docker compose up -d --build      # first run downloads the whisper model
uv run client.py --discovery https://localhost:8935/discovery --file sample.wav
docker compose down
```

The client reserves a session, opens a WebSocket through the orchestrator, streams the WAV in real-time-paced chunks, and prints partial transcripts as they arrive plus a final one at the end.

## Run on-chain (paid)

Layer `docker-compose.onchain.yml` to add a remote signer and run the orchestrator on-chain. Needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build
uv run client.py --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936 --file sample.wav
docker compose -f docker-compose.yml -f docker-compose.onchain.yml down
```

`reserve_session` pays for the session through the remote signer; the WebSocket then streams over it. Because the session is metered, keep clips short for the demo — a long-lived socket keeps billing for its duration.

## GPU (low latency)

CPU is fine for the demo but adds latency. For GPU:

1. Add the NVIDIA CUDA libraries to the image (faster-whisper needs cuBLAS + cuDNN). In the `Dockerfile`, extend the pip install:
   ```
   nvidia-cublas-cu12 nvidia-cudnn-cu12
   ```
   and set `ENV LD_LIBRARY_PATH=/usr/local/lib/python3.12/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.12/site-packages/nvidia/cudnn/lib`.
2. Set `WHISPER_DEVICE=cuda` and `WHISPER_COMPUTE=float16` (env or `.env`).
3. Uncomment the `deploy:` GPU reservation block in `docker-compose.yml`.

## Run without Docker

Start an orchestrator built from `ja/live-runner`, then the app and client directly (the app needs `faster-whisper` installed):

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
uv run runner.py --orchestrator http://localhost:8935 --orchSecret abcdef
uv run client.py --file sample.wav
```
