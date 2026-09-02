# Notepad app (persistent HTTP)

A held-open **HTTP** session that keeps one string in memory. `POST /set` writes it; `POST /get` reads it back. This is the missing cell in the examples table: **persistent + HTTP**, with no WebSocket and no trickle. It is also the regression target for Console `run_capability` with `endpoint` — and the reason that tool cannot reuse session state: each `runInference` reserves, calls once, and stops.

|              |                                               |
| ------------ | --------------------------------------------- |
| App id       | `livepeer-example/notepad`                    |
| Runner mode  | persistent (held-open HTTP session)           |
| Registration | dynamic (self-registers via the SDK)          |
| Transport    | HTTP (JSON request/response, two round-trips) |
| Pricing      | hour (metered while the session is held)      |
| Port         | 8989                                          |

Prerequisites (Docker, `uv`, and the [`livepeer-gateway` SDK](https://pypi.org/project/livepeer-gateway/)) and the shared on-chain/payment setup live in the [repo README](../README.md).

## How it's wired

The app is **dynamically registered** with `mode="persistent"` ([runner.py](runner.py)). Process-local `_note` **is** session state, because the orchestrator pins this runner to the reserved session. The client calls it with `reserve_session` → `POST /set` → `POST /get` → `stop_runner_session` ([client.py](client.py)). Grep `# Livepeer:` in either file to see the exact calls.

`run_capability` on Console (gateway-web `runInference`) can hit `/set` **or** `/get` with `endpoint`, but not both on the same session. Use this client when you need the two-call proof.

## Run offchain (free)

> [!TIP]
> Built locally by the compose file below, or run the published [`ghcr.io/livepeer/runner-example-notepad`](https://github.com/livepeer/runner-app-examples/pkgs/container/runner-example-notepad) with `docker compose up -d --pull always` — see [Images](../README.md#images).

```sh
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[] | {app, mode}'
uv run client.py --text "hello from a held session"
# {'set': {'text': 'hello from a held session', 'revision': 1}, 'get': {'text': 'hello from a held session', 'revision': 1}}
docker compose down
```

## Run on-chain (paid)

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f compose.yml -f compose.onchain.yml up -d --build
uv run client.py --text "hello" \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936
docker compose -f compose.yml -f compose.onchain.yml down
```

The session is **metered** (`unit` defaults to `hour`) for as long as the client holds it. Keep the demo short.

## Run without Docker

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
uv run runner.py --orchestrator https://localhost:8935 --orchSecret abcdef
uv run client.py --text "hello from a held session"
```

## Console MCP

```
run_capability({
  capability: "livepeer-example/notepad",
  endpoint: "/set",
  inputs: { text: "hello" }
})
```

That pays a full reserve for one POST and then stops. A second `run_capability` to `/get` is a **new** session and returns empty text. That is expected.
