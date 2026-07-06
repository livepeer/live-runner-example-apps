# Audio diarized transcription (speaker separation + transcription)

Speech-to-text with speaker labels, from [moatus/audio-diarized-transcription-runner](https://github.com/moatus/audio-diarized-transcription-runner). Two capabilities that work **together** in one response: NVIDIA NeMo **transcription** (ASR) plus **speaker diarization** (who spoke when). Diarization is additive on an OpenAI-compatible route — turn it on and every segment and word gains a `speaker`.

|              |                                                       |
| ------------ | ----------------------------------------------------- |
| App id       | `moatus/audio-diarized-transcription`                 |
| Capability   | `openai:audio-transcriptions`                         |
| Registration | static (orchestrator config + health poll)            |
| Transport    | HTTP multipart (bounded) · WebSocket (true streaming) |
| Port         | 8080                                                  |

**Requires an NVIDIA GPU.** The first request downloads NeMo weights (VAD, TitaNet, ASR) into a Docker volume, so it takes ~1 minute; later requests are fast.

Two ways to test, both from `docker compose up -d`:

- **Direct** ([§3](#3-test--transcription--diarization-together)) — `client.py` hits the runner on `:8080`, no orchestrator. Standard-library only; the quickest check.
- **Through the orchestrator** ([below](#testing-through-an-orchestrator)) — `session.py` reserves one Livepeer session and drives both transports over it, the way this runner is meant to run on the network.

## 1. Build the image

The runner isn't published to a registry yet, so build it from its own repo (one time):

```sh
git clone https://github.com/moatus/audio-diarized-transcription-runner
cd audio-diarized-transcription-runner
./build-images.sh build          # -> moatus/audio-diarized-transcription-runner:v0.1.0
```

That is the image `docker-compose.yml` here references (override with `REGISTRY` / `TAG` in `.env`).

## 2. Run the stack

Brings up the runner **and** a go-livepeer orchestrator with the runner statically registered (needed only for the orchestrator path in §"Testing through an orchestrator"; the direct test in §3 just uses the runner):

```sh
cd audio-diarized-transcription           # this folder
docker compose up -d
until curl -fsS http://localhost:8080/healthz; do sleep 2; done; echo
```

Healthy output confirms the GPU is visible:

```json
{
  "status": "ok",
  "device": "cuda",
  "cuda_available": true,
  "cuda_device_name": "NVIDIA GeForce RTX 3090"
}
```

## 3. Test — transcription + diarization together

Grab a 2-speaker sample (or use your own audio), then call the runner. The included `client.py` is **standard-library only** (no venv, no pip):

```sh
curl -fsSL -o sample.wav https://nemo-public.s3.us-east-2.amazonaws.com/an4_diarize_test.wav
python3 client.py sample.wav --num-speakers 2
```

Expected — the transcript **split by speaker**:

```
speakers detected: 2

speaker-labeled transcript:
speaker_0: eleven twenty seven fifty seven
speaker_1: october twenty four nineteen seventy

segments:
  [  0.07-  2.60] speaker_0: eleven twenty seven fifty seven
  [  3.08-  5.16] speaker_1: october twenty four nineteen seventy
```

`python3 client.py sample.wav --raw` dumps the full JSON (per-word speakers, timestamps, subtitle artifacts, usage).

Prefer `curl`? Same call, raw:

```sh
curl -s http://localhost:8080/v1/audio/transcriptions \
  -F file=@sample.wav \
  -F model=nemo-diarized-transcription-meeting-v0 \
  -F response_format=verbose_json \
  -F diarization=true \
  -F 'timestamp_granularities[]=segment' \
  -F 'timestamp_granularities[]=word' | jq '.speaker_labeled_text, .diarization.speaker_count'
```

Drop `-F diarization=true` and it behaves like a plain OpenAI transcription (text only, no speakers) — that's the "additive" design.

### Live streaming (WebSocket)

The runner also exposes a true-streaming route at `WS /v1/audio/transcriptions/stream` (binary 16 kHz mono PCM in, incremental `speaker.update` / `transcript.segment` events out). The runner repo ships a smoke client for it:

```sh
# from the runner repo
python scripts/audio_diarized_transcription_streaming_smoke.py sample.wav --require-transcript
```

## 4. Tear down

```sh
docker compose down                    # add -v to also drop the model cache volume
```

## Notes

- **One job at a time.** `MAX_QUEUE_SIZE=1` by default — a second concurrent call returns `429 {"type":"queue_full"}`. Raise `NEMO_MAX_QUEUE_SIZE` in `.env` for parallelism (each job needs its own VRAM headroom).
- **First call is slow** (~1 min) while NeMo weights download into the `audio-diarized-transcription-models` volume; they persist across restarts.
- Models used for the bounded route: VAD `vad_multilingual_marblenet`, speaker embeddings `titanet_large`, ASR `stt_en_conformer_ctc_large`.

## Testing through an orchestrator

`docker compose up -d` (above) already brings up a go-livepeer orchestrator with this runner **statically registered** — it reads `runners.json` (`-liveRunnerConfig`), health-polls `/healthz`, and reverse-proxies the runner's HTTP + WebSocket endpoints. Confirm it's advertised:

```sh
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # "moatus/audio-diarized-transcription"
```

`session.py` then drives it the way this runner actually wants to be driven — **one persistent session, both transports** (the `streamdiffusion-ws` pattern). It reserves a single session and, over that one proxied `app_url`, sends **native multipart** to the bounded route and (with `--stream`) **binary PCM** to the WebSocket. A background payment pump funds the session per second, decoupled from the request bodies — so there's **no base64, and no `call_runner` JSON limit** in the way.

```sh
uv sync                                   # installs the SDK's session-payments branch
uv run session.py sample.wav --num-speakers 2            # bounded, through the orchestrator
uv run session.py sample.wav --num-speakers 2 --stream    # + WS stream on the same session
```

Bounded output (verified through the orchestrator):

```
speakers detected: 2
speaker-labeled transcript:
speaker_0: eleven twenty seven fifty seven
speaker_1: october twenty four nineteen seventy
```

### Why this, not single-shot `call_runner`

`call_runner` is JSON-only (`livepeer_gateway/http.py` hardcodes `Content-Type: application/json`) — it can't carry a multipart upload or binary frames. But you don't need it: on the session-payments path you drive raw `aiohttp` against `app_url` and the pump pays on a timer. go-livepeer byte-forwards the body, so **native multipart works today with no SDK change** (base64-in-JSON would cost ~33% bloat and break OpenAI compatibility for nothing). Adding multipart to `call_runner` is only a convenience for the single-shot helper.

> [!NOTE]
> Rides the SDK's `rs/live-runner-session-payments` branch (pinned in `pyproject.toml`), not merged into `ja/live-runner` yet — same situation as the streaming branch.
>
> **Status:** the **bounded multipart** call through the orchestrator session is verified end-to-end. The **`--stream`** WS path is verified against the runner directly (`ws://…:8080`, and the runner's own `…_streaming_smoke.py`), but _through the orchestrator_ it currently connects and loads models yet doesn't relay events back for this runner's true-streaming route — a go-livepeer WS-proxy interaction still being chased (`streamdiffusion-ws` proves WS-through-proxy works in general). One caveat regardless of transport: the runner **(re)loads the streaming models per WS connection (~7 s)** and buffers frames meanwhile, so `--settle` holds before `finish` to let them drain.
