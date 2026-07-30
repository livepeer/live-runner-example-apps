# API-proxy app (a runner that is pure config)

The live runner can also **pass calls through to an API that runs somewhere else** — here the **Hugging Face text-to-image inference API**. This example's runner is a **stock nginx**: [nginx.conf.template](nginx.conf.template) forwards each call to one pinned model URL and injects the operator's token. There is **no app code at all** — the orchestrator operator offers a hosted model ([Stable Diffusion 3 medium](https://huggingface.co/stabilityai/stable-diffusion-3-medium-diffusers) by default) as a paid capability with two config files.

|              |                                              |
| ------------ | -------------------------------------------- |
| App id       | `livepeer-example/stable-diffusion-3-medium` |
| Runner mode  | single-shot                                  |
| Registration | static (orchestrator config + health poll)   |
| Transport    | HTTP (HF payload in, JPEG bytes out)         |
| Pricing      | fixed (one price per call)                   |
| Port         | 8989                                         |

Prerequisites (Docker, `uv`, and the not-yet-released `livepeer-gateway` SDK — pinned in `pyproject.toml`) and the shared on-chain/payment setup live in the [repo README](../README.md). The demo upstream additionally needs a **Hugging Face API token** (`HF_TOKEN`, from [huggingface.co → settings → tokens](https://huggingface.co/settings/tokens)) with inference-provider credits.

## How it's wired

The app is attached as a **static runner**: the orchestrator reads [runners.json](runners.json) via `-liveRunnerConfig` — app id, runner URL, single-shot mode, and the fixed price — and health-polls `/health` (an nginx `return 200`). The `/proxy` location proxies to the pinned model URL (`MODEL` in [compose.yml](compose.yml)) with `Authorization: Bearer <HF_TOKEN>` added. The caller's body is the [Hugging Face text-to-image payload](https://huggingface.co/docs/inference-providers/tasks/text-to-image) forwarded verbatim — `{"inputs": "<prompt>"}` — and the image comes back as **raw JPEG bytes**. The client calls it with `runner_selector` → `call_runner` ([client.py](client.py)) — discover, then one **single-shot** call per image, reading the bytes from `result.content`; the orchestrator reserves a session per call and releases it when the response returns. Grep `# Livepeer:` in client.py to see the exact calls.

## Offering an API as a capability — what this shows

Everything is operator-side config. `runners.json` names the capability and sets the **fixed per-image price**; the nginx config pins the model URL and holds the credential (`HF_TOKEN`, from `.env`). The pinned URL is also the security model: the operator's credential can only be spent on exactly the offered model. The config pins the method to `POST` and drops the caller's query string too, so the body is the only thing a caller controls: they choose nothing but the prompt, and never see an API key. They discover the capability and pay **per image through Livepeer**, while the operator pays the upstream and prices above the per-image upstream cost.

The app id names the model, not the proxy, because that is what callers discover: they match it exactly, so it has to say what they get. Swapping `MODEL` means renaming the app id with it.

Offering a second model is more config, not code: one more `runners.json` entry (its own app id and price) plus one more nginx service with a different `MODEL`.

**Fixed pricing** is the natural fit: one call is one bounded unit of work, so the runner bills one flat price per call instead of metering time.

> [!NOTE]
> Registration can also be **dynamic**: an operator tool can `register_runner` several API endpoints at runtime, each as its own priced capability, without touching the orchestrator config. See [livepeer/api-proxy](https://github.com/livepeer/api-proxy) for an example of dynamic endpoint registration, with key storage and request stats for orchestrator operators.

## Run offchain (free)

```sh
cp .env.example .env   # fill in HF_TOKEN; ignore the on-chain block
docker compose up -d
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-example/stable-diffusion-3-medium registered
uv run client.py --prompt "a watercolor painting of a llama writing code"
docker compose down
```

`compose.yml` brings up an orchestrator (`-useLiveRunners -liveRunnerConfig`) and the nginx runner. The client sends one prompt through the orchestrator and writes `api-proxy-out.jpg`.

## Run on-chain (paid)

Layer `compose.onchain.yml` to run the orchestrator on-chain with a remote signer paying each call — one fixed payment per image, at the price `runners.json` advertises. For the required RPC and wallets see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in HF_TOKEN, RPC, network, keystore paths, accounts
docker compose -f compose.yml -f compose.onchain.yml up -d
uv run client.py --prompt "a watercolor painting of a llama writing code" \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936
docker compose -f compose.yml -f compose.onchain.yml down
```

Each call is one paid single-shot session — the orchestrator reserves it, takes one fixed payment, and releases it when the response returns.
