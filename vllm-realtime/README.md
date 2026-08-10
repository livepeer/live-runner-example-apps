# vllm-realtime app

Realtime speech transcription on the Livepeer network. The client streams audio
in over **Trickle** and receives the live transcript back over an
orchestrator-proxied **WebSocket**, plus derived metrics (word count + a simple
sentiment label) computed on the running text. The same WebSocket carries live
`session.update` messages to the runner mid-stream — a settings-transport path
(whether the backend acts on a given setting is up to the vLLM build; see the
note under [Run offchain](#run-offchain-free)).

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
{...}}` on that socket at any time; the runner forwards it to vLLM mid-stream.
(Voxtral on vLLM 0.24 does not act on the `language` field — this exercises the
settings-transport path, not a language switch.) See
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
uv run client.py --discovery https://localhost:8935/discovery --insecure
# [delta] +'hello'  words=1 sentiment=neu
# ...
# [done] 'hello and welcome to the livepeer realtime transcription demo ...'  words=... sentiment=pos
docker compose down
```

The client synthesizes a few seconds of audio by default; pass a 16 kHz mono
16-bit WAV with `--input path.wav` to stream a real file. Pass `--language en`
to demonstrate the live `session.update` **settings-transport** path: the client
pushes it over the WebSocket after connecting and the runner forwards it to vLLM.
It does **not** change the transcript — Voxtral on vLLM 0.24 ignores the
`language` field, and the mock backend ignores all settings — so what this proves
is that live settings reach the backend mid-stream, not that the language
switched.

`--insecure` skips TLS verification on the transcript WebSocket, which the local
orchestrator serves with a self-signed cert. Verification is on by default; use
`--insecure` only for local development, never against a public deployment.

### Real transcription (GPU box)

```sh
TRANSCRIBER=vllm docker compose --profile vllm up -d --build
docker compose logs -f vllm           # wait for /health to pass (model download)
uv run client.py --input speech-16k.wav --discovery https://localhost:8935/discovery --insecure
```

Validated on an RTX 4090 (24 GB): the compose file caps `--max-model-len` at
16384 so the KV cache fits next to the weights on 24 GB cards (the model's
native 131k context does not). The Voxtral model is public on HuggingFace — no
token needed.

## Performance

The last event of every session is `{"type": "stats", ...}`, which the client
prints as a summary. It has three parts:

```
──── performance ────
  audio                6.56 s
  wall clock           7.405 s
  real-time factor     1.129x
  time to first word   2.392 s
  finalize tail        0.359 s
  words / deltas       15 / 28

  trickle publish (SDK)  segments=14/14 bytes=209920 (30 kB/s) posts_ok=14 failed=0 retries=0
  trickle ingest  (SDK)  segments=14 seq_gaps=0 retries=0 failures=0 stall=7036ms
  websocket out   (app)  events=43 deltas=28 bytes=4375 failures=0 cmds_in=1
```

The three transport lines follow the audio's real path: the client publishes, the
runner ingests, the runner streams results back.

- **trickle publish** and **trickle ingest** come free from the SDK
  (`TricklePublisher.get_stats()` and `TrickleSubscriber.get_stats()`). Reading
  them *together* is the useful part — `publish segments=14/14` against `ingest
  segments=14` proves nothing was dropped in between. That check matters because
  the transport will not tell you: dropped audio still reports `seq_gaps=0`.
- **websocket out** and every latency figure are metered by this app in
  [stats.py](stats.py). The SDK does not meter WebSockets, so an app that streams
  its results over one has to count them itself.

### Two modes, two different numbers

By default the client paces audio to real time, so wall clock can never drop
below the audio duration — the **real-time factor is pinned near 1.0 however fast
the backend is**. In that mode it answers *"did the pipeline keep up?"*, and the
latency signal is the **finalize tail** (last audio byte → final transcript),
which reproduces within ~10 ms across runs. Time to first word is *not* a stable
figure: it moves with whatever silence precedes speech in the clip.

Run with `--no-realtime` to remove the pacing floor and measure real throughput:

```
audio 6.56 s · wall clock 1.57 s · real-time factor 0.24x    → ~4x realtime
trickle publish  segments=14/14  bytes=209920 (1159 kB/s)    → 38x the paced rate,
trickle ingest   segments=14  seq_gaps=0                        still zero loss
```

Both are honest; they measure different things. Paced gives live latency, unpaced
gives throughput headroom.

`--no-realtime` is safe because the client applies **backpressure**: a Trickle
channel keeps no backlog, so a segment published before the runner reads the
previous one is destroyed, not queued. The client publishes one segment, waits
for the runner's `progress` event, then publishes the next — so "as fast as
possible" means "as fast as the runner consumes". Without that, an unpaced run
delivers 1 segment of 14 and reports confident nonsense, with no error raised
anywhere. See [FEEDBACK.md](FEEDBACK.md) #2 and #9.

Numbers above: RTX 4090, Voxtral-Mini-4B-Realtime.

## Run on-chain (paid)

Layer `compose.onchain.yml` to add a remote signer and run the orchestrator
on-chain, so the app advertises a price and the SDK pays per session. This
example uses **metered pricing** (`PRICE` is USD per hour, billed per second for
as long as the client holds the session — the natural fit for a stream with no
fixed length). Needs an Ethereum RPC, a funded signer wallet (deposit +
reserve), and an orchestrator wallet — see
[On-chain (paid) setup](../README.md#on-chain-paid-setup).

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f compose.yml -f compose.onchain.yml up -d --build
# confirm the price is advertised (price_per_unit != 0):
curl -sk https://localhost:8935/discovery | jq
uv run client.py --discovery https://localhost:8935/discovery --signer http://localhost:7936 --insecure
docker compose -f compose.yml -f compose.onchain.yml down
```

Add `--profile vllm` to the `up` command on a GPU box for real transcription.

> [!NOTE]
> A metered session pays **more than once**. The upfront payment that answers the
> 402 challenge only buys the signer's preroll; the orchestrator keeps debiting
> every few seconds and drops the session on the first debit it cannot cover. So
> `reserve_session` keeps the session funded in the background for as long as the
> client holds it, and releasing the session (the `stop_runner_session` call in
> the client's `finally`) stops that funding before the reservation is freed.
> This needs an orchestrator with the session-scoped payment URL added after
> `v0.9.0` ([go-livepeer #4008](https://github.com/livepeer/go-livepeer/pull/4008)),
> which is why the compose files pin a master build. To watch it, stream a clip of
> a few tens of seconds and follow `docker compose logs -f orchestrator` — you
> should see repeated payments, not one.
