# Hello-world app

The smallest possible app on the Livepeer network: a synchronous request/response over HTTP. It self-registers with an orchestrator and exposes `POST /hello`, which takes `{"name": "..."}` and returns `{"message": "Hello, <name>!"}`. No video, WebSocket, or streaming — just the most common app shape.

|              |                                      |
| ------------ | ------------------------------------ |
| App id       | `livepeer-example/hello-world`       |
| Runner mode  | single-shot                          |
| Registration | dynamic (self-registers via the SDK) |
| Transport    | HTTP (JSON request/response)         |
| Port         | 8989                                 |

## Call a live network runner (paid) — start here

One command. Discover a public `livepeer-example/hello-world` runner, pay via PymtHouse, print the response.

1. Install deps from this directory (`uv` + the SDK pin in `pyproject.toml`):

```sh
cd hello-world
uv sync
```

2. Get a PymtHouse **sdkToken** (Dashboard → your app → copy the base64 gateway token). It embeds signer URL, auth header, and discovery.

```sh
export PYMTHOUSE_TOKEN='paste-your-base64-sdkToken-here'
```

3. Run:

```sh
uv run client.py --token "$PYMTHOUSE_TOKEN" --name livepeer
# {'message': 'Hello, livepeer!'}
```

That is the full path: discovery → reserve (if needed) → pay the 402 challenge → `POST /hello` → release.

**What to expect on today's public runner:** discovery may still show `mode: "persistent"` and `price_info.unit: "seconds"` (signer payment type `live`). Local `runner.py` already registers `price_unit="fixed"` for when that orch is redeployed; the client handles both persistent and single-shot.

> Tip: if pymthouse briefly fails with `failed to reach endpoint: …/generate-live-payment`, retry — the signer is reachable; transient Railway blips show up as empty `CancelledError` wrappers.

---

Prerequisites for local Docker demos (and the shared on-chain compose setup) live in the [repo README](../README.md).

## How it's wired

The app is **dynamically registered**: it self-registers with the orchestrator via `register_runner` ([runner.py](runner.py)) and exposes a single `POST /hello`, reverse-proxied through the orchestrator. The client calls it with `runner_selector` → `call_runner` ([client.py](client.py)) — discover, then one call. On single-shot runners the orchestrator reserves a session for the call and releases it when the response returns; on persistent runners the client reserves/stops explicitly. On the paid path `call_runner` answers the 402 payment challenge inline. Grep `# Livepeer:` in either file to see the exact calls.

## Run offchain locally (free)

```sh
docker compose up -d --build
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm livepeer-example/hello-world registered
uv run client.py --name livepeer --discovery https://localhost:8935/discovery
# {'message': 'Hello, livepeer!'}
docker compose down
```

`compose.yml` brings up an orchestrator (`-useLiveRunners`) and the app; the commands above run the client against it.

## Run on-chain locally (paid)

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

The app registers with a price (`--price` from `.env`, in USD billed once per call — **fixed pricing**, the natural fit for bounded request/response work) and the orchestrator advertises it in `/discovery`.

## Run without Docker

Start an orchestrator built from `ja/live-runner` (see [Build from source](https://docs.livepeer.org/v1/orchestrators/guides/install-go-livepeer#build-from-source)), then the app and client directly:

```sh
./livepeer -orchestrator -useLiveRunners -serviceAddr localhost:8935 -orchSecret abcdef -v 6
uv run runner.py --orchestrator https://localhost:8935 --orchSecret abcdef
uv run client.py --name livepeer
# {'message': 'Hello, livepeer!'}
```
