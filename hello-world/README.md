# Hello-world app

The smallest possible app on the Livepeer network: a synchronous request/response over HTTP. It self-registers with an orchestrator and exposes `POST /hello`, which takes `{"name": "..."}` and returns `{"message": "Hello, <name>!"}`. No video, WebSocket, or streaming — just the most common app shape.

|              |                                      |
| ------------ | ------------------------------------ |
| App id       | `livepeer-sample/hello-world`        |
| Transport    | HTTP (JSON request/response)         |
| Registration | dynamic (self-registers via the SDK) |
| Port         | 5000                                 |

Prerequisites (Docker, `uv`, and the not-yet-released `livepeer-gateway` SDK — pinned in `pyproject.toml`) and the shared on-chain/payment setup live in the [repo README](../README.md).

## Run offchain (free)

```sh
docker compose up -d --build
uv run client.py --name livepeer --discovery https://localhost:8935/discovery
# {'message': 'Hello, livepeer!'}
docker compose down
```

`docker-compose.yml` brings up an orchestrator (`-useLiveRunners`) and the app. The app self-registers, the client discovers it via `/discovery`, reserves a session, calls `POST /hello` through the orchestrator, prints the reply, and releases the session.

## Run on-chain (paid)

Layer `docker-compose.onchain.yml` to add a remote signer and run the orchestrator on-chain, so the app advertises a price and the SDK pays per call. This needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts, pricing
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build
uv run client.py --name livepeer \
  --discovery https://localhost:8935/discovery \
  --signer http://localhost:7936
# {'message': 'Hello, livepeer!'}
docker compose -f docker-compose.yml -f docker-compose.onchain.yml down
```

The app registers with a price (`--price` / `--pixels-per-unit` from `.env`) and the orchestrator advertises it in `/discovery`. The SDK client does discovery, the session, the `/hello` call, and payment itself — paying through the remote signer with **no gateway in between**. So this is the full paid stack end to end: **app + orchestrator + remote signer + SDK client**.

## Run without Docker

Start an orchestrator built from `ja/live-runner`, then the app and client directly:

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
uv run runner.py --orchestrator http://localhost:8935 --orchSecret abcdef
uv run client.py --name livepeer
# {'message': 'Hello, livepeer!'}
```
