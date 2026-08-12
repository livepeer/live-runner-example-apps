# Realtime transcription app (WebSocket speech-to-text)

Realtime speech-to-text on the Livepeer network over a **WebSocket** — the client streams audio _up_ and gets transcripts streamed _back_ on one socket. This is the example where WebSockets are genuinely required: HTTP can't stream audio upstream, and SSE is one-directional (server→client). The app is a small aiohttp server wrapping `faster-whisper` that **self-registers** (dynamic) — the dynamic, WebSocket counterpart to the static HTTP vLLM example.

|              |                                           |
| ------------ | ----------------------------------------- |
| App id       | `livepeer-example/realtime-transcription` |
| Runner mode  | persistent (held-open WebSocket session)  |
| Registration | dynamic (self-registers via the SDK)      |
| Model        | `large-v3-turbo` (faster-whisper, fixed)  |
| Transport    | WebSocket (`/transcribe`)                 |
| Port         | 8989                                      |

**Requires an NVIDIA GPU.** The model is fixed at `large-v3-turbo`: it swaps large-v3's 32-layer decoder for 4, so it runs far below realtime on a 3090 while staying near large-v3 quality. The device is pinned with it (`cuda`/`float16`) rather than exposed as a flag: on CPU the model loads but falls behind a live stream, which is the one thing this example is about. Prerequisites (Docker, `uv`, the [SDK](https://pypi.org/project/livepeer-gateway/)) and the shared on-chain/payment setup are in the [repo README](../README.md).

## How it's wired

The app is **dynamically registered**: it self-registers with the orchestrator via `register_runner` ([runner.py](runner.py)) and exposes a `GET /transcribe` WebSocket, whose upgrade the orchestrator proxies straight through — your app speaks standard WS, nothing Livepeer-specific in the socket. The client calls it with `reserve_session` → `ws_connect` → `stop_runner_session` ([client.py](client.py)) — reserve a session, open a `wss://` socket to the session URL, stream audio up / transcripts back, release. Grep `# Livepeer:` in either file to see the exact calls.

On-chain, the reserved session is the billing unit: `reserve_session` pays at reserve and the meter runs while the socket is open — continuous connection = continuous billing, the right model for live audio.

**Staying realtime is the constraint,** and it is why the model is a constant rather than a setting. The app degrades by stretching out partials rather than dropping audio, so a model that cannot keep pace buys accuracy with unbounded lag instead of failing loudly. Serving a different model is a different app, with its own price and its own app id — which is why the id names the capability (`realtime-transcription`) and not the technique.

**Realtime design:** the receive loop only appends audio (never blocking on the model); a background worker transcribes the _current utterance_ (bounded to 15s) every ~0.5s, emits partials, and finalizes on trailing silence or max length — so cost stays bounded no matter how long the stream runs, instead of re-transcribing an ever-growing buffer. It uses the low-latency Whisper preset (`beam_size=1`, no cross-segment conditioning). For production-grade streaming you'd reach for a LocalAgreement approach (whisper_streaming / WhisperLive).

Wire protocol on `/transcribe`:

- client → server: binary frames of **16 kHz mono PCM (int16)**
- client → server: text `eos` to finish
- server → client: JSON `{"text": "...", "final": false|true, "start": <sec>, "end": <sec>}`

Cumulative, not incremental: each partial carries the whole utterance so far and may revise earlier words, so a client replaces rather than appends. That matches Deepgram and Vosk, and it is the honest shape for a decoder that re-runs over the buffer. Delta protocols (OpenAI's `transcript.text.delta`) only become correct once decoding is append-only, which is what the LocalAgreement approach below buys you.

## Audio

Input must be **16 kHz mono WAV**. Fetch 21s of NASA podcast speech, public domain under 17 U.S.C. 105:

```sh
curl -sL https://images-assets.nasa.gov/audio/Ep401_Artemis_II_Launch/Ep401_Artemis_II_Launch~128k.mp3 \
  | ffmpeg -ss 900 -t 21 -i pipe:0 -ar 16000 -ac 1 sample.wav
```

Or bring your own, replacing `input.mp3` / picking your capture device:

```sh
ffmpeg -i input.mp3 -ar 16000 -ac 1 sample.wav                 # convert
ffmpeg -f alsa -i default -ar 16000 -ac 1 -t 20 sample.wav     # record (macOS: -f avfoundation -i :0)
```

Use a clip with a couple of sentences and a pause between them: the app finalizes on trailing silence, so that is what shows partials turning into finals more than once. The NASA clip is trimmed to three such sentences.

Or skip the file and talk into a microphone: pass `-` and pipe raw PCM in, which streams until you Ctrl-C.

```sh
ffmpeg -f alsa -i default -ar 16000 -ac 1 -f s16le - \
  | uv run client.py --discovery https://localhost:8935/discovery -
```

(macOS: `-f avfoundation -i :0`. If `default` fails, name the device: `arecord -l` then `-i plughw:1,0`.)

## Run offchain (free)

```sh
docker compose up -d --build      # first run downloads the whisper model
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-example/realtime-transcription registered
uv run client.py --discovery https://localhost:8935/discovery sample.wav
docker compose down
```

The client reserves a session, opens a WebSocket through the orchestrator, streams the WAV in real-time-paced chunks, and prints partial transcripts as they arrive plus a final one per utterance.

## Run on-chain (paid)

Layer `compose.onchain.yml` to add a remote signer and run the orchestrator on-chain. Needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f compose.yml -f compose.onchain.yml up -d --build
uv run client.py --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936 sample.wav
docker compose -f compose.yml -f compose.onchain.yml down
```

`reserve_session` pays for the session through the remote signer; the WebSocket then streams over it. Because the session is metered, keep clips short for the demo — a long-lived socket keeps billing for its duration.

## Run without Docker

Start an orchestrator built from go-livepeer `v0.9.1` or newer (see [Build from source](https://docs.livepeer.org/v1/orchestrators/guides/install-go-livepeer#build-from-source)), then the app and client directly (the app needs `faster-whisper` installed):

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
uv run runner.py --orchestrator https://localhost:8935 --orchSecret abcdef
uv run client.py sample.wav
```
