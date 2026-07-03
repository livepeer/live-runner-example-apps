# Live runner example apps

Example **apps** for the Livepeer network. An app is a container you build and put on the network: a normal HTTP or WebSocket service that exposes your endpoints. Orchestrators host and run your app via the **live runner**, and clients call it through the orchestrator using the [livepeer-gateway](https://github.com/livepeer/livepeer-python-gateway) Python SDK.

Live runners are not on go-livepeer `main` yet and currently live on the `ja/live-runner` branch. Until it merges, both the orchestrator image and the SDK come from that branch.

> [!IMPORTANT]
> SSE streaming (used by the [`vllm`](./vllm) example) depends on gateway PR [#25](https://github.com/livepeer/livepeer-python-gateway/pull/25), which is not yet merged. Until it lands, streaming is only available from the SDK's `rs/live-runner-streaming` branch, not `ja/live-runner`.

## Examples

| Example | Runner mode | Registration | Transport |
| ------- | ----------- | ------------ | --------- |
| [`hello-world`](./hello-world) | persistent (single-shot by nature) | dynamic | HTTP (JSON request/response) |
| [`echo`](./echo) | persistent | dynamic | trickle (realtime video) |
| [`streamdiffusion`](./streamdiffusion) | persistent | static | trickle (realtime video, needs an NVIDIA GPU) |
| [`streamdiffusion-ws`](./streamdiffusion-ws) | persistent | static | WebSocket + MJPEG (a third-party container, reverse-proxied; needs an NVIDIA GPU) |
| [`vllm`](./vllm) | persistent (single-shot by nature) | static | HTTP + SSE (OpenAI API, via a local gateway) |

More will follow. Each example is self-contained and runs **offchain** (free, no wallet); most also run **on-chain** (paid). See each README for the commands.

## Runner mode: persistent vs single-shot

The runner mode is set at registration and **defaults to `persistent`** — both the SDK's `register_runner(...)` and the static `runners.json`. The examples set it explicitly for clarity.

- **Persistent** — a held-open session billed per second of wall-clock while it's open. Best for realtime / streaming. (`echo`.)
- **Single-shot** — one request in, one response out. Best for batch / request-response work. (`hello-world`, `vllm` are single-shot by nature.)

> [!IMPORTANT]
> Single-shot payment isn't implemented yet ([go-livepeer#3955](https://github.com/livepeer/go-livepeer/issues/3955)), so `hello-world` and `vllm` register as **persistent**. On-chain that bills per second for the whole open session and overbills short calls, so keep them **offchain-only** until #3955 lands ([#5](https://github.com/livepeer/live-runner-example-apps/issues/5)).

## Registration: dynamic vs static

- **Dynamic** — the app self-registers with the orchestrator via the SDK (`register_runner`) and sends heartbeats; the orchestrator drops it when heartbeats stop. Best for apps that come and go. (`hello-world` is dynamic.)
- **Static** — the orchestrator is configured with the app's URL in a `runners.json` and polls a health endpoint; the app needs no SDK. Best for fixed, long-running deployments.

## Prerequisites

- **Docker** for the end-to-end demos. They use the `livepeer/go-livepeer:ja-live-runner` image, so there is nothing to build.
- **Python 3.12+** and [`uv`](https://docs.astral.sh/uv/) for the client.
- The **`livepeer-gateway` SDK** from the `ja/live-runner` branch (not yet on PyPI):

  ```sh
  pip install "git+https://github.com/livepeer/livepeer-python-gateway@ja/live-runner"
  ```

## Shared components

Each example is runnable on its own, but the orchestrator and signer services are defined once at the repo root and pulled into each example with Docker Compose `extends`, so examples don't duplicate them:

- `compose.orchestrator.yml` — the offchain orchestrator (`-useLiveRunners`).
- `compose.onchain.yml` — adds a remote signer and re-points the orchestrator on-chain.

## On-chain (paid) setup

On-chain runs add a **remote signer** that holds the payer wallet and mints probabilistic micropayment tickets; the orchestrator (recipient) redeems the winning ones. The setup is shared across examples:

- **Wallets live outside this repo.** Point `*_KEYSTORE_DIR` at go-livepeer keystore directories by absolute path; they are mounted read-only. Only the address and password come from `.env`, and the private keys never enter the repo.
- **`.env` is per example and gitignored.** Copy the example's `.env.example` and fill in the RPC, network, keystore paths, accounts, and pricing. It holds the keystore password, so it is never committed.
- **Pricing is in USD**, converted to wei on-chain via the price feed. The app advertises `PRICE_PER_UNIT` (whole-number USD) per `PIXELS_PER_UNIT`; keep `PIXELS_PER_UNIT` small, because large values shrink the per-unit price below 1 wei and it floors to 0 (free). The signer accepts up to `MAX_PRICE_PER_UNIT`.
- **Payments are probabilistic.** A call mints tickets that win with some probability; only winning tickets are redeemed on-chain. With default settings you will rarely see a redemption on a short run — that is expected.

## Verifying discovery

Before running a client, confirm the orchestrator actually advertises your runner with the expected price by calling `/discovery` directly:

```sh
curl -sk https://localhost:8935/discovery | jq
```

Each entry lists its `runners` with an `app`, `version`, capacity, and `price_info` (`price_per_unit` / `pixels_per_unit` in WEI). Check that your app appears and that the price matches what you configured — a `price_per_unit` of `0` means it floored to free (see the `PIXELS_PER_UNIT` note above).

## Conventions

- Apps bind to `127.0.0.1` by default (safe for local runs). In a container the compose files pass `--host=0.0.0.0` so the orchestrator can reach the app.
- Orchestrators serve a self-signed TLS cert; the SDK skips verification.
