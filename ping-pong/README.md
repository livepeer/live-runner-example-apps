# Ping-pong app (single-shot WebSocket)

A minimal **single-shot WebSocket** demo. The runner exposes `GET /ws`; send
`{"ping": <timestamp>}` and it replies `{"pong": <timestamp>, "delta_ms": ...}`.
It shows the held-open single-shot path: the WebSocket connection **is** the one
workload, so the orchestrator reserves a session on connect and releases it on
close (no `reserve_session`/`stop`).

|              |                              |
| ------------ | ---------------------------- |
| App id       | `livepeer-example/ping-pong` |
| Runner mode  | single-shot                  |
| Registration | dynamic (`register_runner`)  |
| Transport    | WebSocket                    |
| Port         | 8991                         |

## How it's wired

The app **dynamically registers** as single-shot (`register_runner(mode="single-shot")`
in [runner.py](runner.py)) and exposes `GET /ws`, reverse-proxied through the
orchestrator. The client discovers the runner with `runner_selector` and opens a
WebSocket to the proxied URL ([client.py](client.py)) — no reserve/stop, because
a single-shot session lives for exactly one connection.

## Run offchain (free)

```sh
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[] | {app, mode}'   # confirm mode=single-shot
uv run client.py --discovery https://localhost:8935/discovery --count 5
kill %1 2>/dev/null; docker compose down
```

Expect lines like `ping-pong receiver_delta_ms=… round_trip_ms=…`.

## Run on-chain (paid) — experimental

> [!WARNING]
> On-chain single-shot **WebSocket** payment is experimental. A WebSocket upgrade
> can't answer the `402` challenge inline, so the client preflights the challenge
> over plain HTTP, puts the initial payment on the upgrade headers, and keeps the
> session funded with interval top-ups to the session-scoped `/payment` endpoint.
> This depends on:
>
> - the orchestrator handling a **paid single-shot WS upgrade** (go-livepeer#3955),
>   so use an image built from that branch (e.g. `livepeer/go-livepeer:single-shot`
>   / `pr-3983`), and
> - the payment loop in [client.py](client.py), which is a **hand-rolled stand-in**
>   for the SDK session payment streamer (livepeer-python-gateway#31 / ENG-179).

```sh
cp .env.example .env   # fill RPC, keystores, funded signer+orch accounts, price
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[] | {app, mode}'
uv run client.py --discovery https://localhost:8935/discovery --signer http://localhost:7936 --count 20
```

The client sends the initial payment on the upgrade and POSTs a top-up every
`payment_interval_ms / 2` (from the challenge) to keep the held-open session
funded. Once #31/ENG-179 land, replace the hand-rolled loop with the SDK
`start_payments()` streamer.
