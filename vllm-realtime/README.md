# vllm-realtime app

Realtime speech transcription on the Livepeer network. The client streams audio
in over **Trickle** and receives the live transcript back over an
orchestrator-proxied **WebSocket**, plus derived metrics (word count + a simple
sentiment label) computed on the running text. Settings (e.g. language) can be
adjusted live over the same WebSocket without restarting the stream.

|              |                                                        |
| ------------ | ------------------------------------------------------ |
| App id       | `livepeer-sample/vllm-realtime`                        |
| Transport    | Trickle in (PCM audio), WebSocket out (JSON events)    |
| Registration | dynamic (self-registers via the SDK)                   |
| Ports        | 5000 (app), 8000 (vLLM)                                |

Prerequisites (Docker, `uv`, and the not-yet-released `livepeer-gateway` SDK —
pinned in `pyproject.toml`) and the shared on-chain/payment setup live in the
[repo README](../README.md).

## How it works

```
client                      orchestrator             app (runner.py)           vLLM | mock
publish PCM ─> in channel ─── trickle ───> subscribe (internal_url)
                                            feed audio ─ local ws ─> /v1/realtime   (GPU)
                                            transcript + metrics          or mock   (no GPU)
   <── JSON events ─────────── websocket ── /ws  (bidirectional)
   ──> {"type":"session.update", ...} ────> live settings, forwarded to vLLM
```

`POST /transcribe` mints a Trickle `in` channel. The client publishes PCM16/16 kHz
audio to it; the app subscribes (via the channel's runner-reachable
`internal_url`), bridges to a **local** vLLM `/v1/realtime` WebSocket
(co-located in the same compose, never proxied), and streams transcript deltas +
metrics to the client over `GET /ws` — a raw WebSocket the orchestrator proxies
in both directions. The client can send `{"type": "session.update", "session":
{...}}` on that socket at any time to adjust settings mid-stream. See
[runner.py](runner.py) and [transcriber.py](transcriber.py).

### Backends

- **`mock`** (default) — no GPU, no vLLM. Fabricates plausible, time-paced
  transcription events so the whole Trickle pipeline runs on a laptop.
- **`vllm`** — opens the real vLLM realtime WebSocket. Needs an NVIDIA GPU
  (≥16 GB) and the `vllm` compose profile.

Switch with the `TRANSCRIBER` env var.

## Run offchain (free)

GPU-free, using the mock backend — only the orchestrator and app start:

```sh
docker compose up -d --build
uv run client.py --discovery https://localhost:8935/discovery
# [delta] +'hello'  words=1 sentiment=neu
# ...
# [done] 'hello and welcome to the livepeer realtime transcription demo ...'  words=... sentiment=pos
docker compose down
```

The client synthesizes a few seconds of audio by default; pass a 16 kHz mono
16-bit WAV with `--input path.wav` to stream a real file. Pass `--language en`
to demonstrate a live `session.update` over the WebSocket after it connects.

### Real transcription (GPU box)

```sh
TRANSCRIBER=vllm docker compose --profile vllm up -d --build
docker compose logs -f vllm           # wait for /health to pass (model download)
uv run client.py --input speech-16k.wav --discovery https://localhost:8935/discovery
```

Validated on an RTX 4090 (24 GB): the compose file caps `--max-model-len` at
16384 so the KV cache fits next to the weights on 24 GB cards (the model's
native 131k context does not). The Voxtral model is public on HuggingFace — no
token needed.

## Run on-chain (paid)

Layer `docker-compose.onchain.yml` to add a remote signer and run the
orchestrator on-chain, so the app advertises a price and the SDK pays per
session. Needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and
an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup).

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build
# confirm the price is advertised (price_per_unit != 0):
curl -sk https://localhost:8935/discovery | jq
uv run client.py --discovery https://localhost:8935/discovery --signer http://localhost:7936
docker compose -f docker-compose.yml -f docker-compose.onchain.yml down
```

Add `--profile vllm` to the `up` command on a GPU box for real transcription.

> [!NOTE]
> Payment is settled once at session reservation via the SDK's 402 challenge.
> For very long streams the orchestrator may expect ongoing per-segment payment;
> see [FEEDBACK.md](FEEDBACK.md) (R4) for the open question and the fallback.
