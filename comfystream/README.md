# ComfyStream app (workflow-driven video + analyze)

A ComfyUI live-video app on the Livepeer network. It consumes the published
[`livepeer/comfystream`](https://hub.docker.com/r/livepeer/comfystream) image —
the `comfystream` package and ComfyUI workspace stay in that image. This folder
is only the Livepeer integration: dynamic registration, trickle channels, and
the analyze / start_stream HTTP surface.

|              |                                      |
| ------------ | ------------------------------------ |
| App id       | `livepeer-example/comfystream`       |
| Runner mode  | persistent (held-open session)       |
| Registration | dynamic (self-registers via the SDK) |
| Transport    | trickle + HTTP (`/analyze`)          |
| Pricing      | hour (metered per second)            |
| Port         | 8991                                 |

Prerequisites (Docker, NVIDIA GPU, `uv`, the
[`livepeer-gateway` SDK](https://pypi.org/project/livepeer-gateway/)) and the
shared setup are in the [repo README](../README.md). You also need the
`livepeer/comfystream` image (pulled as the Docker build base).

## How it's wired

ComfyStream is **installed, not vendored**. The Dockerfile starts `FROM
livepeer/comfystream:latest` and adds `livepeer-gateway` into that image's conda
env. `runner.py` imports `comfystream.Pipeline` from the package already in the
image and registers with the orchestrator.

The app is **dynamically registered**: it self-registers via `register_runner`
([runner.py](runner.py)) and exposes:

- `POST /analyze` — video-in → text-out (trickle `in` + JSONL `text`)
- `POST /start_stream` — live trickle video (optional text)
- `POST /update_stream` — mid-session workflow / resolution change
- `GET /text` — buffered text for the active session
- `GET /healthz`

The client calls it with `reserve_session` → `post_json` / `MediaPublish` →
`stop_runner_session` ([client.py](client.py)). Grep `# Livepeer:` in either
file to see the exact calls.

This is the same persistent + trickle shape as [`echo`](../echo), plus an HTTP
analyze surface driven by a ComfyUI API-format workflow JSON.

## Run offchain (free)

Start the stack and confirm the runner registered. The first build pulls the
ComfyStream image and can take a while.

```sh
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-example/comfystream registered
```

Any video works; this makes a short one if you need it:

```sh
ffmpeg -f lavfi -i testsrc=size=512x512:rate=30 -t 5 -c:v libx264 -preset ultrafast -pix_fmt yuv420p sample.mp4
uv run client.py sample.mp4 --discovery https://localhost:8935/discovery
```

`client.py` defaults to [workflows/analyze-stub-api.json](workflows/analyze-stub-api.json)
(video-in → a fixed text token). Pass `--workflow path/to/workflow.json` for a
real ComfyUI graph, and `--mode stream` for `start_stream`.

Stop the stack with `docker compose down`.

## Attach to an existing orchestrator

Same image and runner, no local go-livepeer. Point at the orch you already run
(env vars in `.env` / `.env.example`):

```sh
docker compose -f compose.existing.yml up -d --build
uv run client.py sample.mp4 --discovery "$LIVEPEER_ORCH_URL/discovery"
```

## Run on-chain (paid)

Layer `compose.onchain.yml` to run the orchestrator on-chain with a remote
signer paying for the session. This example uses **metered pricing**: `PRICE`
is USD per hour, billed per second for as long as the client holds the session.
For the required RPC and wallets see
[On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f compose.yml -f compose.onchain.yml up -d --build
uv run client.py sample.mp4 \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936
docker compose -f compose.yml -f compose.onchain.yml down
```

A metered session pays **more than once**. Use a clip of at least a few tens of
seconds if you want to see repeated payments in
`docker compose logs -f orchestrator`.
