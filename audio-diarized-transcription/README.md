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

This folder tests the **app directly** — build it, run it, call it. That is the reliable path today (see [Testing through an orchestrator](#testing-through-an-orchestrator) for why the network path needs an SDK change first).

## 1. Build the image

The runner isn't published to a registry yet, so build it from its own repo (one time):

```sh
git clone https://github.com/moatus/audio-diarized-transcription-runner
cd audio-diarized-transcription-runner
./build-images.sh build          # -> moatus/audio-diarized-transcription-runner:v0.1.0
```

That is the image `docker-compose.yml` here references (override with `REGISTRY` / `TAG` in `.env`).

## 2. Run the runner

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

This runner is a passive OpenAI-compatible service with no embedded SDK, so it attaches to a go-livepeer orchestrator via **static** registration — the orchestrator reads `runners.json` (`-liveRunnerConfig`), health-polls `/healthz`, and reverse-proxies requests through. `runners.json` in this folder is ready for that (`app: moatus/audio-diarized-transcription`, capacity 1).

**Not wired up end-to-end yet**, and it's a client-side gap, not an orchestrator one: the bounded route is a **multipart file upload** (and the streaming route is **binary frames**), but the Python gateway SDK's `call_runner` only sends a JSON `payload` (`livepeer_gateway/http.py` hardcodes `Content-Type: application/json`) and expects a JSON object back. The orchestrator is a transparent reverse proxy and should forward multipart fine — the SDK just needs a non-JSON body path (`files=`/raw `content=`), a relaxed response contract, and body re-send on the 402 retry. Until that lands, test the app directly as above.
