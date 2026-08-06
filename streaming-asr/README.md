# Streaming ASR app (WebSocket speech-to-text)

Realtime speech-to-text on the Livepeer network over a **WebSocket** — the client streams audio _up_ and gets transcripts streamed _back_ on one socket. This is the example where WebSockets are genuinely required: HTTP can't stream audio upstream, and SSE is one-directional (server→client). The app is a small aiohttp server wrapping `faster-whisper` that **self-registers** (dynamic) — the dynamic, WebSocket counterpart to the static HTTP vLLM example.

|              |                                          |
| ------------ | ---------------------------------------- |
| App id       | `livepeer-example/streaming-asr`         |
| Runner mode  | persistent (held-open WebSocket session) |
| Registration | dynamic (self-registers via the SDK)     |
| Transport    | WebSocket (`/transcribe`)                |
| Port         | 5005                                     |

Runs on **CPU by default** so it works anywhere; set `WHISPER_DEVICE=cuda` with a CUDA-enabled image for lower latency. Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared on-chain/payment setup are in the [repo README](../README.md).

## How it's wired

The app is **dynamically registered**: it self-registers with the orchestrator via `register_runner` ([runner.py](runner.py)) and exposes a `GET /transcribe` WebSocket, whose upgrade the orchestrator proxies straight through — your app speaks standard WS, nothing Livepeer-specific in the socket. The client calls it with `reserve_session` → `ws_connect` → `stop_runner_session` ([client.py](client.py)) — reserve a session, open a `wss://` socket to the session URL, stream audio up / transcripts back, release. Grep `# Livepeer:` in either file to see the exact calls.

On-chain, the reserved session is the billing unit: `reserve_session` pays at reserve and the meter runs while the socket is open — continuous connection = continuous billing, the right model for live audio.

**Realtime design:** the receive loop only appends audio (never blocking on the model); a background worker transcribes the _current utterance_ (bounded to 15s) every ~0.5s, emits partials, and finalizes on trailing silence or max length — so cost stays bounded no matter how long the stream runs, instead of re-transcribing an ever-growing buffer. It uses the low-latency Whisper preset (`beam_size=1`, no cross-segment conditioning). For production-grade streaming you'd reach for a LocalAgreement approach (whisper_streaming / WhisperLive).

Wire protocol on `/transcribe`:

- client → server: binary frames of **16 kHz mono PCM (int16)**
- client → server: text `eos` to finish
- server → client: JSON `{"text": "...", "final": false|true}`

## Audio

Input must be **16 kHz mono WAV**. Convert any file you have, or record a few seconds of yourself talking:

```sh
ffmpeg -i input.mp3 -ar 16000 -ac 1 sample.wav                 # convert
ffmpeg -f alsa -i default -ar 16000 -ac 1 -t 20 sample.wav     # record (macOS: -f avfoundation -i :0)
```

Use a clip with a couple of sentences and a pause between them: the app finalizes on trailing silence, so that is what shows partials turning into finals more than once.

## Run offchain (free)

```sh
docker compose up -d --build      # first run downloads the whisper model
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-example/streaming-asr registered
uv run client.py --discovery https://localhost:8935/discovery --file sample.wav
docker compose down
```

The client reserves a session, opens a WebSocket through the orchestrator, streams the WAV in real-time-paced chunks, and prints partial transcripts as they arrive plus a final one per utterance.

## Run on-chain (paid)

Layer `compose.onchain.yml` to add a remote signer and run the orchestrator on-chain. Needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f compose.yml -f compose.onchain.yml up -d --build
uv run client.py --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936 --file sample.wav
docker compose -f compose.yml -f compose.onchain.yml down
```

`reserve_session` pays for the session through the remote signer; the WebSocket then streams over it. Because the session is metered, keep clips short for the demo — a long-lived socket keeps billing for its duration.

## Run without Docker

Start an orchestrator built from go-livepeer `v0.9.0` or newer (see [Build from source](https://docs.livepeer.org/v1/orchestrators/guides/install-go-livepeer#build-from-source)), then the app and client directly (the app needs `faster-whisper` installed):

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
uv run runner.py --orchestrator https://localhost:8935 --orchSecret abcdef
uv run client.py --file sample.wav
```
