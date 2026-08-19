# Hello-world app

The smallest possible app on the Livepeer network: a synchronous request/response over HTTP. It self-registers with an orchestrator and exposes `POST /hello`, which takes `{"name": "..."}` and returns `{"message": "Hello, <name>!"}`. No video, WebSocket, or streaming — just the most common app shape.

|              |                                      |
| ------------ | ------------------------------------ |
| App id       | `livepeer-example/hello-world`       |
| Runner mode  | single-shot                          |
| Registration | dynamic (self-registers via the SDK) |
| Transport    | HTTP (JSON request/response)         |
| Pricing      | fixed (one price per call)           |
| Port         | 8989                                 |

Prerequisites (Docker, `uv`, and the [`livepeer-gateway` SDK](https://pypi.org/project/livepeer-gateway/)) and the shared on-chain/payment setup live in the [repo README](../README.md).

## How it's wired

The app is **dynamically registered**: it self-registers with the orchestrator via `register_runner` ([runner.py](runner.py)) and exposes a single `POST /hello`, reverse-proxied through the orchestrator. The client calls it with `runner_selector` → `call_runner` ([client.py](client.py)) — discover, then one call. Because the runner is **single-shot**, there is no session to manage: the orchestrator reserves one for the call and releases it when the response returns, and on the paid path `call_runner` answers the 402 payment challenge inline. Grep `# Livepeer:` in either file to see the exact calls. This is the base flow every other example builds on.

## Run offchain (free)

> [!TIP]
> Built locally by the compose file below, or run the published [`ghcr.io/livepeer/runner-example-hello-world`](https://github.com/livepeer/runner-app-examples/pkgs/container/runner-example-hello-world) instead by setting `APP_IMAGE` and `APP_PULL_POLICY=always` — the package is public, so no login; see [Images](../README.md#images).

```sh
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-example/hello-world registered
uv run client.py --name livepeer --discovery https://localhost:8935/discovery
# {'message': 'Hello, livepeer!'}
docker compose down
```

`compose.yml` brings up an orchestrator (`-useLiveRunners`) and the app; the commands above run the client against it.

## Run on-chain (paid)

Layer `compose.onchain.yml` to add a remote signer and run the orchestrator on-chain, so the app advertises a price and the SDK pays per call. This needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f compose.yml -f compose.onchain.yml up -d --build
uv run client.py --name livepeer \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936
# {'message': 'Hello, livepeer!'}
docker compose -f compose.yml -f compose.onchain.yml down
```

The app registers with a price (`--price` from `.env`, in USD billed once per call — **fixed pricing**, the natural fit for bounded request/response work) and the orchestrator advertises it in `/discovery`. The SDK client does discovery, the `/hello` call, and payment itself — paying through the remote signer with **no gateway in between**. So this is the full paid stack end to end: **app + orchestrator + remote signer + SDK client**.

## Run without Docker

Start an orchestrator built from go-livepeer `v0.9.1` or newer (see [Build from source](https://docs.livepeer.org/v1/orchestrators/guides/install-go-livepeer#build-from-source)), then the app and client directly:

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
uv run runner.py --orchestrator https://localhost:8935 --orchSecret abcdef
uv run client.py --name livepeer
# {'message': 'Hello, livepeer!'}
```
