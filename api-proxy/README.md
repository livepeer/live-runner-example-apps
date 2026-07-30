# API-proxy app (passthrough to an upstream API)

The live runner can also **pass calls through to an API that runs somewhere else** — a hosted model API, a SaaS endpoint, a service on your own infrastructure. The orchestrator operator attaches the proxy as a runner — **statically or dynamically; this example uses static** — and offers the upstream as a paid capability on the network: `POST /proxy` forwards a JSON envelope to the upstream and returns its response. The demo upstream is the **Hugging Face text-to-image inference API** ([Stable Diffusion 3 medium](https://huggingface.co/stabilityai/stable-diffusion-3-medium-diffusers) by default), but `--upstream` points it at any REST API.

|              |                                            |
| ------------ | ------------------------------------------ |
| App id       | `livepeer-example/api-proxy`               |
| Runner mode  | single-shot                                |
| Registration | static (orchestrator config + health poll) |
| Transport    | HTTP (JSON envelope in, JSON/base64 out)   |
| Pricing      | fixed (one price per call)                 |
| Port         | 8989                                       |

Prerequisites (Docker, `uv`, and the not-yet-released `livepeer-gateway` SDK — pinned in `pyproject.toml`) and the shared on-chain/payment setup live in the [repo README](../README.md). The demo upstream additionally needs a **Hugging Face API token** (`HF_TOKEN`, from [huggingface.co → settings → tokens](https://huggingface.co/settings/tokens)) with inference-provider credits.

## How it's wired

The app is attached as a **static runner**: the orchestrator reads [runners.json](runners.json) via `-liveRunnerConfig` — app id, runner URL, single-shot mode, and the fixed price — and health-polls `/health`. There is **no Livepeer code in the app**: [runner.py](runner.py) is a plain aiohttp service. Each `/proxy` call forwards `{"method", "path", "headers", "json"}` to `<upstream>/<path>` and returns `{"status", "headers", "body"}` for text upstream bodies or `{"status", "headers", "body_b64"}` for binary ones (a generated image, say). The client calls it with `runner_selector` → `call_runner` ([client.py](client.py)) — discover, then one **single-shot** call per request; the orchestrator reserves a session per call and releases it when the response returns. Grep `# Livepeer:` in client.py to see the exact calls.

## Offering an API as a capability — what this shows

Everything the operator sets lives on the operator's side. `runners.json` names the capability, the proxy's URL, and the **fixed per-call price**; the upstream credential (`UPSTREAM_TOKEN`, fed from `HF_TOKEN` by the compose files) sits in the app's environment, and the app injects it as a Bearer header on every forward — any `Authorization` a caller sends is dropped. Callers need no API key of their own: they discover the capability and pay **per call through Livepeer**, while the operator pays the upstream and prices above the per-call upstream cost.

**Fixed pricing** is the natural fit: one call is one bounded unit of work, so the runner bills one flat price per call instead of metering time.

> [!NOTE]
> Registration can also be **dynamic**: an operator tool can `register_runner` several API endpoints at runtime, each as its own priced capability, without touching the orchestrator config. See [livepeer/api-proxy](https://github.com/livepeer/api-proxy) for an example of dynamic endpoint registration, with key storage and request stats for orchestrator operators.

## Run offchain (free)

```sh
HF_TOKEN=hf_... docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-example/api-proxy registered
uv run client.py --prompt "a watercolor painting of a llama writing code"
docker compose down
```

`compose.yml` brings up an orchestrator (`-useLiveRunners -liveRunnerConfig`) and the app (proxying `https://router.huggingface.co`). The client builds the envelope for one text-to-image call, sends it through the orchestrator, and writes `api-proxy-out.jpg`.

## Run on-chain (paid)

Layer `compose.onchain.yml` to run the orchestrator on-chain with a remote signer paying each call — one fixed payment per image, at the price `runners.json` advertises. For the required RPC and wallets see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in HF_TOKEN, RPC, network, keystore paths, accounts
docker compose -f compose.yml -f compose.onchain.yml up -d --build
uv run client.py --prompt "a watercolor painting of a llama writing code" \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936
docker compose -f compose.yml -f compose.onchain.yml down
```

Each call is one paid single-shot session — the orchestrator reserves it, takes one fixed payment, and releases it when the response returns.
