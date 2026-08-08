# Ollama app (one container, several priced models)

One Ollama container serving several models, each registered as **its own Live Runner app** with its own price. This is the example about **multiple capabilities from one process**: every other example here registers exactly once. Ollama itself is the stock upstream image with no Livepeer code in it — a **registrar sidecar** does the registering, which is what wrapping software you did not write looks like.

|              |                                                  |
| ------------ | ------------------------------------------------ |
| App ids      | `ollama/<model>`, one per pulled model           |
| Runner mode  | single-shot (one session per call)               |
| Registration | dynamic (a sidecar self-registers via the SDK)   |
| Transport    | HTTP + SSE (OpenAI `/v1/chat/completions`)       |
| Pricing      | hour (metered per second of the call), per model |
| Port         | 11434 (Ollama), 8080 (gateway)                   |

Runs on GPU or CPU — drop the `deploy` block in `compose.yml` for CPU-only, and expect it to be slow. Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared on-chain/payment setup are in the [repo README](../README.md).

## How it's wired

Three moving parts, and only one of them knows about Livepeer:

- **Ollama** — the stock image. Never modified, never aware of anything.
- **`registrar.py`** — a sidecar that asks Ollama what it has (`GET /api/tags`) and calls `register_runner` once per model, each pointing at the same Ollama URL.
- **`gateway.py`** — a host-side OpenAI endpoint that maps the OpenAI `model` field onto an app id and forwards. Any OpenAI client works against it unchanged.

**Which models exist is discovered; what they cost is configured.** The registrar never has a model list of its own: `ollama pull` something and it appears on the network at the next start. Prices are operator policy, so they come from `PRICES` in `.env`.

**One model, one app id, one price.** A runner carries exactly one `mode` and one `price_info`, and discovery can only filter on `app`, so several capabilities means several registrations. `qwen2.5:0.5b` and `llama3.2:1b` are genuinely different products at different prices, and a caller picks between them the same way they pick between orchestrators.

**The app id is the model name, verbatim.** `llama3.2:1b` registers as `ollama/llama3.2:1b` — go-livepeer only requires an app id be non-empty and trimmed, so there is no reason to slug it. Keeping it exact makes the mapping reversible in both directions, which is why nothing here needs a `metadata` field to restate the name: what you discover is what you send.

**Listing models is free.** `GET /v1/models` is answered from `/discovery`, a plain GET with no session and no payment, so it reports what **the network** offers rather than what one container holds. Only the forward reserves a session — and because the runners are single-shot and metered, that call pays for as long as the generation runs.

## Capacity, and why it is hand-sized

`OLLAMA_NUM_PARALLEL` says how many generations the container will really run at once. The registrar divides that across the models it registers, so the **sum** of the advertised capacities equals what the hardware can do.

That arithmetic is manual on purpose. Each registration carries its own capacity counter, and **the orchestrator does not know they share a GPU** — see [Improvements this example is waiting on](#improvements-this-example-is-waiting-on). Register two models at `capacity: 1` each on a box that can only run one generation, and the orchestrator will cheerfully admit two sessions; both callers pay and both get contention instead of the 503 that would have been correct.

Both models stay resident under `OLLAMA_KEEP_ALIVE`, so a request never waits for a swap.

## Run offchain (free)

```sh
docker compose up -d --build   # pulls the models, then registers one app per model
curl -sk https://localhost:8935/discovery | jq '.[].runners[] | {app, capacity, price_info}'
uv run gateway.py --discovery https://localhost:8935/discovery &   # OpenAI endpoint on :8080
uv run client.py --model qwen2.5:0.5b --prompt "In one sentence, what is Livepeer?"
uv run client.py --model llama3.2:1b --prompt "write a haiku about GPUs"
kill %1; docker compose down    # stop the gateway, then the stack
```

Ask the network what it serves, with a stock OpenAI client:

```sh
curl -s http://localhost:8080/v1/models | jq '.data[].id'
# "llama3.2:1b"
# "qwen2.5:0.5b"
```

Add a model without touching any config:

```sh
docker compose exec ollama ollama pull gemma3:270m
docker compose restart registrar     # picks it up from /api/tags
```

It registers at the fallback price, since `PRICES` does not mention it.

## Run on-chain (paid)

Layer `compose.onchain.yml` to add a remote signer and run the orchestrator on-chain. Needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, prices
docker compose -f compose.yml -f compose.onchain.yml up -d --build
uv run gateway.py --signer http://localhost:7936 --discovery https://localhost:8935/discovery &
uv run client.py --model llama3.2:1b --prompt "In one sentence, what is Livepeer?"
kill %1; docker compose -f compose.yml -f compose.onchain.yml down
```

There is no `runners.json` here: registration is dynamic, so each model's price comes from the registrar's `PRICES`. Pick a costlier model and the caller pays more, which is the whole reason each model is its own app.

## Improvements this example is waiting on

Two gaps are tracked in [go-livepeer#4015](https://github.com/livepeer/go-livepeer/issues/4015), and both are visible here:

- **Capacity is tracked per registration.** Nothing tells the orchestrator that these runners share one GPU, so the advertised total is whatever the registrations happen to add up to. This example sizes that sum by hand from `OLLAMA_NUM_PARALLEL`; a `pool` field on the heartbeat would let the orchestrator enforce it instead, and refuse the surplus with a 503.
- **Only one GPU can be described.** `LiveRunnerGPU` is a single struct, so a two-card host advertises one card and half its VRAM.

A third piece is needed for the version that tracks capacity **dynamically** rather than by arithmetic — flipping every registration's `status` off a shared semaphore as work starts and finishes. That works today in principle, but the SDK exposes neither a public setter for `status` nor a way to force an immediate heartbeat, so it would mean reaching into private attributes. Not something an example should teach.

## Run without Docker

Start an orchestrator built from go-livepeer `v0.9.0` or newer (see [Build from source](https://docs.livepeer.org/v1/orchestrators/guides/install-go-livepeer#build-from-source)) and an Ollama server, then the registrar, gateway, and client directly:

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
ollama serve &
ollama pull qwen2.5:0.5b
uv run registrar.py --orchestrator https://localhost:8935 --orchSecret abcdef \
  --ollama-url http://localhost:11434 &
uv run gateway.py &
uv run client.py --model qwen2.5:0.5b --prompt "Hello!"
```
