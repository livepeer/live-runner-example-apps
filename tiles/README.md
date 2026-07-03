# Tiles app (capacity fan-out)

An image processor on the Livepeer network that shows what **capacity** does. It exposes `POST /tile`, which stylizes one image tile (a deliberately CPU-heavy transform). The client splits an image into a grid and opens **one session per tile at once**, so the runner's capacity — the number of sessions it serves concurrently — decides how many tiles process in parallel.

|              |                                      |
| ------------ | ------------------------------------ |
| App id       | `livepeer-sample/tiles`              |
| Runner mode  | persistent (single-shot by nature)   |
| Registration | dynamic (self-registers via the SDK) |
| Transport    | HTTP (base64 PNG in/out)             |
| Port         | 8989                                 |

Prerequisites (Docker, `uv`, and the not-yet-released `livepeer-gateway` SDK — pinned in `pyproject.toml`) and the shared on-chain/payment setup live in the [repo README](../README.md).

## How it's wired

The app self-registers (`register_runner`) with a `capacity` and exposes `POST /tile`, reverse-proxied through the orchestrator. Per tile the client runs the base flow — discover → reserve → call → release — but for the whole grid at once. `/tile` itself is an ordinary stateless handler; the only Livepeer-specific lines are register/deregister on the runner and reserve/call/release on the client (grep `# Livepeer:`).

## Capacity — what this shows

**Capacity is the maximum number of sessions the orchestrator will route to one runner at the same time.** The runner advertises it at registration (`register_runner(capacity=N)`, or the `capacity` field in a static `runners.json`); the orchestrator tracks the runner's live sessions and, once `capacity` are open, stops routing new ones there — a further reserve is refused until a session ends.

This example makes that visible. The client fans out one session per tile:

- **`capacity=1`** — the runner serves one tile at a time. The other tiles' reserves are refused, so the client waits and retries; tiles process **one after another**.
- **`capacity=9`** (for a 3×3 grid) — all nine sessions open at once and the tiles process **in parallel**.

The output image is identical either way. **Capacity changes throughput, not the result** — which is the whole point: it is how an operator sizes a runner to the concurrency it can actually handle (GPU memory, CPU, model instances), and how the network spreads load once a runner is full.

> [!NOTE]
> When a reserve hits a full runner the SDK raises `NoRunnerAvailableError`. The client treats that as "wait for a slot," retrying with backoff until one frees. That wait *is* the capacity limit doing its job.

## Run offchain (free)

```sh
CAPACITY=1 docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[] | {app, capacity}'   # confirm tiles + its capacity
uv run client.py sample.png --discovery https://localhost:8935/discovery            # note the total time (tiles serialize)
docker compose down

CAPACITY=9 docker compose up -d --build
uv run client.py sample.png --discovery https://localhost:8935/discovery            # same output, much faster (tiles parallel)
docker compose down
```

`docker-compose.yml` brings up an orchestrator (`-useLiveRunners`) and the app; `CAPACITY` (default 4) sets the runner's advertised capacity. The client splits `sample.png` into a 3×3 grid (`--grid N` to change), processes every tile through the orchestrator, and writes `tiles-out.png`. Watch the client log: at `capacity=1` the `reserved` lines appear one at a time; at `capacity=9` they land together.

## Run on-chain (paid)

Layer `docker-compose.onchain.yml` to add a remote signer and run the orchestrator on-chain, so each tile call is paid through the signer. This needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
CAPACITY=9 docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build
uv run client.py sample.png \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936
docker compose -f docker-compose.yml -f docker-compose.onchain.yml down
```

Each tile is its own reserve → pay → call → release, so a paid run mints a payment per tile. Keep the grid small on-chain.

> [!IMPORTANT]
> Single-shot payment isn't implemented yet ([go-livepeer#3955](https://github.com/livepeer/go-livepeer/issues/3955)), so `tiles` registers as **persistent** and bills per second of the (brief) open session. Prefer offchain until #3955 lands.

## Run without Docker

Start an orchestrator built from `ja/live-runner`, then the app and client directly:

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
uv run runner.py --orchestrator http://localhost:8935 --orchSecret abcdef --capacity 9
uv run client.py sample.png
```
