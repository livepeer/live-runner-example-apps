# Tiles app (capacity fan-out)

An image processor on the Livepeer network that shows what **capacity** does. It exposes `POST /tile`, which stylizes one image tile (a deliberately CPU-heavy transform). The client splits an image into a grid and fires **one call per tile at once**, so the runner's capacity — the number of sessions it serves concurrently — decides how many tiles process in parallel.

|              |                                      |
| ------------ | ------------------------------------ |
| App id       | `livepeer-example/tiles`             |
| Runner mode  | single-shot                          |
| Registration | dynamic (self-registers via the SDK) |
| Transport    | HTTP (base64 PNG in/out)             |
| Pricing      | fixed (one price per tile call)      |
| Port         | 8989                                 |

Prerequisites (Docker, `uv`, and the [`livepeer-gateway` SDK](https://pypi.org/project/livepeer-gateway/)) and the shared on-chain/payment setup live in the [repo README](../README.md).

## How it's wired

The app is **dynamically registered**: it self-registers with the orchestrator via `register_runner` ([runner.py](runner.py)) — advertising its **`capacity`** — and exposes `POST /tile`, reverse-proxied through the orchestrator. The client calls it with `runner_selector` → `call_runner` ([client.py](client.py)) — discover, then one **single-shot** call per tile, for the whole grid at once. There is no session to manage: the orchestrator reserves one per call and releases it when the response returns. Grep `# Livepeer:` in either file to see the exact calls. `/tile` is an ordinary stateless handler; its CPU work runs in a thread so tiles process in parallel.

## Capacity — what this shows

**Capacity is the maximum number of sessions the orchestrator will route to one runner at the same time.** The runner advertises it at registration (`register_runner(capacity=N)`, or the `capacity` field in a static `runners.json`); the orchestrator tracks the runner's live sessions and, once `capacity` are open, stops routing new ones there. Each in-flight single-shot call holds one session slot, so a further call is refused until one finishes.

This example makes that visible. The client fans out one call per tile:

- **`capacity=1`** — the runner serves one tile at a time. The other tiles' calls are refused, so the client waits and retries; tiles process **one after another**.
- **`capacity=9`** (for a 3×3 grid) — all nine calls run at once and the tiles process **in parallel**.

The output image is identical either way. **Capacity changes throughput, not the result** — which is the whole point: it is how an operator sizes a runner to the concurrency it can actually handle (GPU memory, CPU, model instances), and how the network spreads load once a runner is full.

> [!NOTE]
> When a call hits a full runner the orchestrator refuses it with HTTP 503 (insufficient capacity). The client treats that as "wait for a slot," retrying with backoff until one frees. That wait _is_ the capacity limit doing its job.

## Run offchain (free)

```sh
CAPACITY=1 docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[] | {app, capacity}'    # confirm livepeer-example/tiles registered with its capacity
uv run client.py sample.png --discovery https://localhost:8935/discovery            # note the total time (tiles serialize)
docker compose down

CAPACITY=9 docker compose up -d --build
uv run client.py sample.png --discovery https://localhost:8935/discovery            # same output, much faster (tiles parallel)
docker compose down
```

`compose.yml` brings up an orchestrator (`-useLiveRunners`) and the app; `CAPACITY` (default 4) sets the runner's advertised capacity. The client splits `sample.png` into a 3×3 grid (`--grid N` to change), processes every tile through the orchestrator, and writes `tiles-out.png`. Watch the client log: at `capacity=1` the `done` lines appear one at a time; at `capacity=9` they land together.

## Run on-chain (paid)

Layer `compose.onchain.yml` to run the orchestrator on-chain with a remote signer paying each tile call. This example showcases **fixed pricing**: `PRICE` is billed once per tile instead of metered per second, the natural fit for bounded work. For the required RPC and wallets see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
CAPACITY=9 docker compose -f compose.yml -f compose.onchain.yml up -d --build
uv run client.py sample.png \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936
docker compose -f compose.yml -f compose.onchain.yml down
```

Each tile is one paid single-shot call — the orchestrator reserves a session for it, takes one fixed payment, and releases it when the response returns — so a paid run mints a payment per tile. Keep the grid small on-chain.

## Run without Docker

Start an orchestrator built from go-livepeer `v0.9.1` or newer (see [Build from source](https://docs.livepeer.org/v1/orchestrators/guides/install-go-livepeer#build-from-source)), then the app and client directly:

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
uv run runner.py --orchestrator https://localhost:8935 --orchSecret abcdef --capacity 9
uv run client.py sample.png
```
