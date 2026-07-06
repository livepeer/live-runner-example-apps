# Audio diarized transcription (speaker separation + transcription)

Speech-to-text with speaker labels, from [moatus/audio-diarized-transcription-runner](https://github.com/moatus/audio-diarized-transcription-runner). Two capabilities that work **together** in one response: NVIDIA NeMo **transcription** (ASR) plus **speaker diarization** (who spoke when). Diarization is additive on an OpenAI-compatible route — turn it on and every segment and word gains a `speaker`.

|              |                                                       |
| ------------ | ----------------------------------------------------- |
| App id       | `moatus/audio-diarized-transcription`                 |
| Capability   | `openai:audio-transcriptions`                         |
| Registration | static (orchestrator config + health poll)            |
| Transport    | HTTP multipart (bounded) · WebSocket (true streaming) |
| Port         | 8080                                                  |

**Requires an NVIDIA GPU.** The first request downloads NeMo weights into a Docker volume, so it takes ~1 minute; later requests are fast. Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared setup are in the [repo README](../README.md).

## How it's wired

This runner is a passive OpenAI-compatible service with no embedded SDK, so it attaches to a go-livepeer orchestrator via **static** registration — the orchestrator reads `runners.json` (`-liveRunnerConfig`), health-polls `/healthz`, and reverse-proxies the runner's HTTP + WebSocket endpoints straight through.

The runner has three surfaces, and `client.py` drives all of them over **one** reserved session — the persistent-session pattern (like `streamdiffusion-ws`): reserve once, a background payment pump funds it per second, and you send **native multipart** and **binary WS frames** straight over the proxied `app_url`. Because payment is on a timer (not per-call), the request body can be anything — **no base64, and no JSON-only `call_runner`** in the path.

| `client.py` flag | Surface                         | Transport                                                  |
| ---------------- | ------------------------------- | ---------------------------------------------------------- |
| _(default)_      | bounded, non-live transcription | `POST /v1/audio/transcriptions` (multipart)                |
| `--stream`       | live true-streaming             | `WS /v1/audio/transcriptions/stream` (binary PCM)          |
| `--live`         | live stateful session           | `POST …/live/sessions` → `/{id}/audio` → `GET` → `/finish` |
| `--all`          | all three, on the one session   | —                                                          |

## 1. Build the image

The runner isn't published to a registry yet, so build it from its own repo (one time):

```sh
git clone https://github.com/moatus/audio-diarized-transcription-runner
cd audio-diarized-transcription-runner
./build-images.sh build          # -> moatus/audio-diarized-transcription-runner:v0.1.0
```

That's the image `docker-compose.yml` here references (override with `REGISTRY` / `TAG` in `.env`).

## 2. Run the stack

Brings up the runner **and** a go-livepeer orchestrator with the runner statically registered:

```sh
cd audio-diarized-transcription
docker compose up -d
until curl -fsS http://localhost:8080/healthz; do sleep 2; done; echo
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # "moatus/audio-diarized-transcription"
```

## 3. Test it

`client.py` reserves one Livepeer session and drives the runner through the orchestrator (grep `# Livepeer:` for the three SDK calls). Grab a 2-speaker sample first:

```sh
uv sync                                                       # installs the SDK's session-payments branch
curl -fsSL -o sample.wav https://nemo-public.s3.us-east-2.amazonaws.com/an4_diarize_test.wav

uv run client.py sample.wav --num-speakers 2                  # bounded (non-live)
uv run client.py sample.wav --num-speakers 2 --all            # bounded + streaming + live-session, one session
```

`--all` output (verified end-to-end through the orchestrator):

```
[bounded] speakers=2
speaker_0: eleven twenty seven fifty seven
speaker_1: october twenty four nineteen seventy

[stream]
 ~speaker_0: Eleven
 ~speaker_0: twenty seven
 ~speaker_1: fifty seven.
 ~speaker_1: October twenty four nineteen
  speaker_1: seventy.
 finished

[live] chunks=1 speakers=1
  speaker_0: eleven twenty seven
  …
```

### Sanity check without Livepeer

To hit the runner directly (no orchestrator, no session), point `client.py` at it — or use plain `curl`:

```sh
uv run client.py sample.wav --direct http://localhost:8080 --num-speakers 2

curl -s http://localhost:8080/v1/audio/transcriptions \
  -F file=@sample.wav -F model=nemo-diarized-transcription-meeting-v0 \
  -F response_format=verbose_json -F diarization=true \
  -F 'timestamp_granularities[]=segment' | jq '.speaker_labeled_text, .diarization.speaker_count'
```

Drop `-F diarization=true` and it behaves like a plain OpenAI transcription (text only) — that's the "additive" design.

## Run on-chain (paid)

Everything above is **offchain (free)** — no wallet, no payments. To run **on-chain**, layer `docker-compose.onchain.yml`: it adds a remote signer and re-points the orchestrator on-chain, so the runner advertises the price from `runners.json` and the client pays per second through the signer. Needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in NETWORK, ETH_RPC_URL, keystore paths, accounts, pricing
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d
uv run client.py sample.wav --signer http://localhost:7936 --all
```

Only the client gains `--signer`; the runner and the flow are otherwise unchanged. `start_payments()` (a no-op offchain) now funds the held session per second via the signer, so the same multipart/WS/live-session calls settle on-chain.

## 4. Tear down

```sh
docker compose down                    # add -v to also drop the model cache volume
# on-chain: docker compose -f docker-compose.yml -f docker-compose.onchain.yml down
```

## Notes

- **One job at a time.** `MAX_QUEUE_SIZE=1` by default — a second concurrent call returns `429 {"type":"queue_full"}`. Raise `NEMO_MAX_QUEUE_SIZE` in `.env` for parallelism (each job needs its own VRAM headroom).
- **First call is slow** (~1 min) while NeMo weights download into the `audio-diarized-transcription-models` volume; they persist across restarts.
- **Streaming models reload per WS connection (~7 s)** and the runner buffers frames meanwhile, so `--stream` holds `--settle` seconds before `finish` to let them drain (bump it for longer clips). Pre-warming the streaming pipeline at startup would remove that wait.
- **Response shapes differ across surfaces:** the bounded route returns `speaker_labeled_text`; the live-session `finish` returns the transcript under `segments`. The live path also uses a lighter energy-VAD diarizer, so its speaker split can differ from the bounded NeMo-meeting route on short clips.

> [!NOTE]
> This example rides the SDK's `rs/live-runner-session-payments` branch (pinned in `pyproject.toml`), not merged into `ja/live-runner` yet — same situation as the streaming branch. Both transports (bounded multipart + WS streaming) and the stateful live-session API are verified end-to-end through the orchestrator session; go-livepeer byte-forwards all of it, so no SDK change is needed for this path.
