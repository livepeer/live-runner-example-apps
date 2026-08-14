# API-proxy app (a runner that is pure config)

The live runner can also **pass calls through to an API that runs somewhere else** — here the **Hugging Face text-to-image inference API**. This example's runner is a **stock nginx**: [nginx.conf.template](nginx.conf.template) forwards each call to its route's pinned model URL and injects the operator's token. There is **no app code at all** — the orchestrator operator offers two hosted models ([Stable Diffusion 3 medium](https://huggingface.co/stabilityai/stable-diffusion-3-medium-diffusers) and [FLUX.1 schnell](https://huggingface.co/black-forest-labs/FLUX.1-schnell)) as **two separately priced capabilities**, with nothing but config.

|              |                                                                                 |
| ------------ | ------------------------------------------------------------------------------- |
| App ids      | `livepeer-example/stable-diffusion-3-medium`, `livepeer-example/flux-1-schnell` |
| Runner mode  | single-shot                                                                     |
| Registration | static (orchestrator config + health poll)                                      |
| Transport    | HTTP (HF payload in, JPEG bytes out)                                            |
| Pricing      | fixed, per capability (0.0001 and 0.00004 USD per image)                        |
| Port         | 8989                                                                            |

Prerequisites (Docker, `uv`, and the [`livepeer-gateway` SDK](https://pypi.org/project/livepeer-gateway/)) and the shared on-chain/payment setup live in the [repo README](../README.md). The demo upstream additionally needs a **Hugging Face API token** (`HF_TOKEN`, from [huggingface.co → settings → tokens](https://huggingface.co/settings/tokens)) with inference-provider credits.

## How it's wired

The apps are attached as **static runners**: the orchestrator reads [runners.json](runners.json) via `-liveRunnerConfig` — app id, runner URL, single-shot mode, and the fixed price, one entry per capability — and health-polls `/health` (an nginx `return 200`). Each `<model>/proxy` location proxies to its pinned model URL (`MODEL_SD3` and `MODEL_FLUX` in [compose.yml](compose.yml)) with `Authorization: Bearer <HF_TOKEN>` added. The caller's body is the [Hugging Face text-to-image payload](https://huggingface.co/docs/inference-providers/tasks/text-to-image) forwarded verbatim — `{"inputs": "<prompt>"}` — and the image comes back as **raw JPEG bytes**. The client calls it with `runner_selector` → `call_runner` ([client.py](client.py)) — discover, then one **single-shot** call per image, reading the bytes from `result.content`; the orchestrator reserves a session per call and releases it when the response returns. Grep `# Livepeer:` in client.py to see the exact calls.

**A capability is a registration, not a container.** One nginx serves both: each `runners.json` entry points its app id at its own path (`http://app:8989/sd3`), and the orchestrator preserves that path when it forwards, so the two registrations land on different `location` blocks with different pinned models and different prices. `--app` on the client picks which one to call.

The shared `/health` is deliberate. `return 200` only ever reported that nginx is up, never that an upstream model or the token is still good, so a copy per capability would claim a precision it does not have.

## Offering an API as a capability — what this shows

Everything is operator-side config. `runners.json` names each capability and sets its **fixed per-image price**; the nginx config pins the model URL and holds the credential (`HF_TOKEN`, from `.env`). The pinned URL is also the security model: the operator's credential can only be spent on exactly the models offered. The config pins the method to `POST` and drops the caller's query string too, so the body is the only thing a caller controls: they choose nothing but the prompt, and never see an API key. They discover a capability and pay **per image through Livepeer**, while the operator pays the upstream and prices above the per-image upstream cost.

**A capability is a product, not a parameter.** FLUX.1 schnell is faster and cheaper than SD3 medium, so it is its own app id at its own price rather than a flag on one shared endpoint. Callers pick between them the same way they pick between orchestrators: discovery filters on `app`, matched exactly, so the app id has to say what you get. Swapping a `MODEL_*` means renaming its app id with it, and offering a third model is the same change once more — one `location` block, one `runners.json` entry.

**Each entry carries its own capacity.** That is the right shape here: the two capabilities do not contend, because the work happens upstream at Hugging Face and nginx is only forwarding bytes. Runners that genuinely share a resource — several models resident on one GPU — are a different problem, since the orchestrator has no way to know that two registrations sit on the same card ([go-livepeer#4015](https://github.com/livepeer/go-livepeer/issues/4015)).

**Fixed pricing** is the natural fit: one call is one bounded unit of work, so the runner bills one flat price per call instead of metering time.

## Pinned here, dynamic in livepeer/api-proxy

Both capabilities here are fixed at deploy time: two `location` blocks and two `runners.json` entries, changed by editing config and restarting. That is the whole point of a **static** runner, and it is the right trade when the offering is stable.

Registration can also be **dynamic**. [livepeer/api-proxy](https://github.com/livepeer/api-proxy) is the same one-process-many-endpoints shape, except endpoints are added at runtime with a CLI or a dashboard — one `register_runner` each, each its own priced capability, with no orchestrator config to touch and no restart. It also stores the upstream keys encrypted and reports per-endpoint request stats, which is what an operator running more than a handful of these actually needs.

Same idea, opposite ends of the same axis: pin the routes and read the config, or enable endpoints as you go.

## Run offchain (free)

```sh
cp .env.example .env   # fill in HF_TOKEN; ignore the on-chain block
docker compose up -d
curl -sk https://localhost:8935/discovery | jq '.[].runners[] | {app, price_info}'   # both capabilities, each with its price
uv run client.py --prompt "a watercolor painting of a llama writing code"
uv run client.py --app livepeer-example/flux-1-schnell \
  --prompt "a watercolor painting of a llama writing code" --output flux-out.jpg
docker compose down
```

`compose.yml` brings up an orchestrator (`-useLiveRunners -liveRunnerConfig`) and the nginx runner, which registers as two capabilities. The client sends one prompt through the orchestrator per call and writes the image; `--app` chooses the model and `--output` keeps the two results apart.

## Run on-chain (paid)

Layer `compose.onchain.yml` to run the orchestrator on-chain with a remote signer paying each call — one fixed payment per image, at the price `runners.json` advertises for the capability called. For the required RPC and wallets see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in HF_TOKEN, RPC, network, keystore paths, accounts
docker compose -f compose.yml -f compose.onchain.yml up -d
uv run client.py --prompt "a watercolor painting of a llama writing code" \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936
docker compose -f compose.yml -f compose.onchain.yml down
```

Each call is one paid single-shot session — the orchestrator reserves it, takes one fixed payment, and releases it when the response returns. Call the cheaper capability with `--app livepeer-example/flux-1-schnell` and the payment is smaller, which is the whole reason each model is its own app. The signer's `MAX_PRICE_PER_UNIT` is a single cap across capabilities, so it has to clear the **highest** price in `runners.json`.
