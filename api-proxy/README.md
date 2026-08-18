# API-proxy app (a runner that is pure config)

The live runner can also **pass calls through to an API that runs somewhere else** — here the **Hugging Face inference API**. This example's runner is a **stock nginx**: [nginx.conf.template](nginx.conf.template) forwards each call to its route's pinned model URL and injects the operator's token. There is **no app code at all** — the orchestrator operator offers two hosted models as **two separately priced capabilities**, with nothing but config: [Stable Diffusion 3 medium](https://huggingface.co/stabilityai/stable-diffusion-3-medium-diffusers) paints an image, [ViT base](https://huggingface.co/google/vit-base-patch16-224) reads one back.

|              |                                                                                       |
| ------------ | ------------------------------------------------------------------------------------- |
| App ids      | `livepeer-example/stable-diffusion-3-medium`, `livepeer-example/vit-base-patch16-224` |
| Runner mode  | single-shot                                                                           |
| Registration | static (orchestrator config + health poll)                                            |
| Transport    | HTTP (HF payload in, JPEG bytes or labels out)                                        |
| Pricing      | fixed, per capability (0.0001 USD per image, 0.00001 per classification)              |
| Port         | 8989                                                                                  |

Prerequisites (Docker, `uv`, and the [`livepeer-gateway` SDK](https://pypi.org/project/livepeer-gateway/)) and the shared on-chain/payment setup live in the [repo README](../README.md). The demo upstream additionally needs a **Hugging Face API token** (`HF_TOKEN`, from [huggingface.co → settings → tokens](https://huggingface.co/settings/tokens)). Both models are served by Hugging Face's own `hf-inference` provider, which is what keeps the demo cheap to run; models routed to third-party providers bill inference-provider credits instead.

## How it's wired

The apps are attached as **static runners**: the orchestrator reads [runners.json](runners.json) via `-liveRunnerConfig` — app id, runner URL, single-shot mode, and the fixed price, one entry per capability — and health-polls `/health` (an nginx `return 200`). Each `<model>/proxy` location proxies to its pinned model URL with `Authorization: Bearer <HF_TOKEN>` added. The caller's body is the [Hugging Face payload](https://huggingface.co/docs/inference-providers/tasks/text-to-image) forwarded verbatim — `{"inputs": "<prompt>"}` to paint, the same key carrying a base64 image to classify — and the answer comes back as **raw JPEG bytes** or as labels with scores. The client calls it with `runner_selector` → `call_runner` ([client.py](client.py)) — discover, then one **single-shot** call per image, reading the answer from `result.content`; the orchestrator reserves a session per call and releases it when the response returns. Grep `# Livepeer:` in client.py to see the exact calls.

**A capability is a registration, not a container.** One nginx serves both: each `runners.json` entry points its app id at its own path (`http://app:8989/sd3`), and the orchestrator preserves that path when it forwards, so the two registrations land on different `location` blocks with different pinned models and different prices. `--app` on the client picks which one to call.

**A runner answers with a JSON object or with opaque bytes.** SD3 returns an image, which needs no shape at all, but the classifier returns a bare JSON array, which is neither — so its `location` relabels the response `Content-Type` as text and the client parses it. That is the shape of every fix available to a runner that is pure config: nginx can pin, inject, and relabel, but it cannot rewrite a body. An upstream whose response shape you must change is an upstream that needs app code.

The shared `/health` is deliberate: it reports that nginx is up and nothing more. [nginx.conf.template](nginx.conf.template) says why a real upstream check does not belong there.

## Offering an API as a capability — what this shows

Everything is operator-side config. `runners.json` names each capability and sets its **fixed per-image price**; the nginx config pins the model URL and holds the credential (`HF_TOKEN`, from `.env`). The pinned URL is also the security model: the operator's credential can only be spent on exactly the models offered. The config pins the method to `POST` and drops the caller's query string too, so the body is the only thing a caller controls: they choose nothing but the prompt, and never see an API key. They discover a capability and pay **per image through Livepeer**, while the operator pays the upstream and prices above the per-image upstream cost.

**A capability is a product, not a parameter.** Classifying an image is one forward pass where painting one is a diffusion run, so it is its own app id at a tenth of the price rather than a flag on one shared endpoint. Callers pick between them the same way they pick between orchestrators: discovery filters on `app`, matched exactly, so the app id has to say what you get. Swapping a model means renaming its app id with it, and offering a third is the same change once more: one `location` block, one `runners.json` entry.

**Each entry carries its own capacity.** That is the right shape here: the two capabilities do not contend, because the work happens upstream at Hugging Face and nginx is only forwarding bytes. Capacity is exposure control rather than a local limit, since payment is taken when the session is reserved: it caps how many paid calls can be in flight against the operator's credential, so size it to the upstream quota rather than to this machine. Runners that genuinely share a resource — several models resident on one GPU — are a different problem, since the orchestrator has no way to know that two registrations sit on the same card ([go-livepeer#4015](https://github.com/livepeer/go-livepeer/issues/4015)).

**Fixed pricing** is the natural fit: one call is one bounded unit of work, so the runner bills one flat price per call instead of metering time.

## Pinned here, dynamic in livepeer/api-proxy

Both capabilities here are fixed at deploy time: two `location` blocks and two `runners.json` entries, changed by editing config and restarting. That is the whole point of a **static** runner, and it is the right trade when the offering is stable.

Registration can also be **dynamic**. [livepeer/api-proxy](https://github.com/livepeer/api-proxy) is the same one-process-many-endpoints shape, except endpoints are added at runtime with a CLI or a dashboard: one `register_runner` each, each its own priced capability, with no orchestrator config to touch and no restart. It also stores the upstream keys encrypted and reports per-endpoint request stats, which is what an operator running more than a handful of these actually needs.

## Run offchain (free)

```sh
cp .env.example .env   # fill in HF_TOKEN; ignore the on-chain block
docker compose up -d
curl -sk https://localhost:8935/discovery | jq '.[].runners[] | {app, price_info}'   # both capabilities, each with its price
uv run client.py --prompt "a watercolor painting of a llama writing code"
uv run client.py --app livepeer-example/vit-base-patch16-224 --image api-proxy-out.jpg
docker compose down
```

`compose.yml` brings up an orchestrator (`-useLiveRunners -liveRunnerConfig`) and the nginx runner, which registers as two capabilities. Each call is one paid request through the orchestrator: the first writes the image, the second sends that same file back through the other capability, which answers `llama` at 0.99. `--app` chooses the model and `--image` sends a file instead of a prompt.

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

Each call is one paid single-shot session: the orchestrator reserves it, takes one fixed payment, and releases it when the response returns. `--app` picks which price you pay. The signer's `MAX_PRICE_PER_UNIT` is one cap across both capabilities, so it has to clear the **highest** price in `runners.json`.
