# API-proxy app (resell an upstream API)

Wraps an existing HTTP API so it's reachable, and payable, through the Livepeer network. `POST /proxy` takes a JSON envelope describing the upstream call and returns the upstream response — the app runs no model and knows nothing about what it forwards. The demo upstream is the **Hugging Face text-to-image inference API** ([Stable Diffusion 3 medium](https://huggingface.co/stabilityai/stable-diffusion-3-medium-diffusers) by default), but `--upstream` points it at any REST API.

|              |                                          |
| ------------ | ---------------------------------------- |
| App id       | `livepeer-example/api-proxy`             |
| Runner mode  | single-shot                              |
| Registration | dynamic (self-registers via the SDK)     |
| Transport    | HTTP (JSON envelope in, JSON/base64 out) |
| Port         | 8989                                     |

Prerequisites (Docker, `uv`, and the not-yet-released `livepeer-gateway` SDK — pinned in `pyproject.toml`) and the shared on-chain/payment setup live in the [repo README](../README.md). The demo upstream additionally needs a **Hugging Face API token** (`HF_TOKEN`, from [huggingface.co → settings → tokens](https://huggingface.co/settings/tokens)) with inference-provider credits.

## How it's wired

The app is **dynamically registered**: it self-registers with the orchestrator via `register_runner` ([runner.py](runner.py)) and exposes a single `POST /proxy`, reverse-proxied through the orchestrator. Each call forwards `{"method", "path", "headers", "json"}` to `<upstream>/<path>` and returns `{"status", "headers", "body"}` for text upstream bodies or `{"status", "headers", "body_b64"}` for binary ones (a generated image, say). The client calls it with `runner_selector` → `call_runner` ([client.py](client.py)) — discover, then one **single-shot** call per request. There is no session to manage: the orchestrator reserves one per call and releases it when the response returns. Grep `# Livepeer:` in either file to see the exact calls.

## Proxying an API — what this shows

Most real apps don't host models; they call an API. This example shows that a runner can be exactly that call: the same thin proxy you would deploy anywhere, registered on the network unchanged.

The interesting part is **who holds the key**. Whoever runs the app sets `UPSTREAM_TOKEN` in its environment (the compose files feed it from `HF_TOKEN`); the orchestrator never sees it — it only routes calls and takes payment. The app injects the token as a Bearer header on every forward and drops any `Authorization` a caller sends. So callers need no API key of their own: they discover the app and pay **per call through Livepeer**, while the app's operator pays the upstream and sets `PRICE` above the per-call upstream cost. **Fixed pricing** is the natural fit: one call is one bounded unit of work, so the runner bills one flat price per call instead of metering time (compare [`vllm`](../vllm), where open-ended sessions make per-second metering the better fit).

## Run offchain (free)

```sh
HF_TOKEN=hf_... docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-example/api-proxy registered
uv run client.py --prompt "a watercolor painting of a llama writing code"
docker compose down
```

`compose.yml` brings up an orchestrator (`-useLiveRunners`) and the app (proxying `https://router.huggingface.co`). The client builds the envelope for one text-to-image call, sends it through the orchestrator, and writes `api-proxy-out.jpg`.

## Run on-chain (paid)

Layer `compose.onchain.yml` to run the orchestrator on-chain with a remote signer paying each call — one fixed payment per image. For the required RPC and wallets see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in HF_TOKEN, RPC, network, keystore paths, accounts, pricing
docker compose -f compose.yml -f compose.onchain.yml up -d --build
uv run client.py --prompt "a watercolor painting of a llama writing code" \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936
docker compose -f compose.yml -f compose.onchain.yml down
```

Each call is one paid single-shot session — the orchestrator reserves it, takes one fixed payment, and releases it when the response returns.

## Run without Docker

Start an orchestrator built from go-livepeer `v0.9.0` or newer (see [Build from source](https://docs.livepeer.org/v1/orchestrators/guides/install-go-livepeer#build-from-source)), then the app and client directly:

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
UPSTREAM_TOKEN=hf_... uv run runner.py --orchestrator https://localhost:8935 --orchSecret abcdef
uv run client.py --prompt "a watercolor painting of a llama writing code"
```
