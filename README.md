# Live runner example apps

Example **apps** for the Livepeer **live runner** — go-livepeer's new way to run any app on the network. You ship a normal HTTP / WebSocket / video service; an orchestrator hosts it, and clients reach it through the orchestrator with the [livepeer-gateway](https://github.com/livepeer/livepeer-python-gateway) SDK.

The point is to **swap the compute without changing your app**: the same container and the same client run whether it's served by your laptop, one orchestrator, or a whole market of them — the network decides who runs it and (on-chain) settles payment. Write the app once; move the compute freely.

> [!NOTE]
> Live runners aren't on go-livepeer `main` yet — they live on the `ja/live-runner` branch. Until it merges, both the orchestrator image and the SDK come from that branch.

## Communication schemas

The orchestrator is a **transparent reverse proxy**: every endpoint you expose is passed through to your app unchanged, so you write an ordinary service and it works on the network as-is. Any transport works:

- **HTTP** request/response — the common case. (`hello-world`)
- **HTTP + SSE** — streamed / token responses. (`vllm`)
- **Trickle** — continuous realtime video in/out. (`echo`)
- **WebSocket** — long-lived bidirectional sessions. (external: `scope`)

> [!IMPORTANT]
> SSE streaming (used by `vllm`) depends on gateway PR [#25](https://github.com/livepeer/livepeer-python-gateway/pull/25), not yet merged. Until it lands, streaming is only on the SDK's `rs/live-runner-streaming` branch, not `ja/live-runner`.

## Examples

| Example                        | Goal                                            | Mode                               | Registration | Transport   |
| ------------------------------ | ----------------------------------------------- | ---------------------------------- | ------------ | ----------- |
| [`hello-world`](./hello-world) | The simplest app: one request, one response     | persistent (single-shot by nature) | dynamic      | HTTP (JSON) |
| [`echo`](./echo)               | Realtime video, transformed and echoed back     | persistent                         | dynamic      | trickle     |
| [`vllm`](./vllm)               | Drop-in OpenAI API; the client stays unmodified | persistent (single-shot by nature) | static       | HTTP + SSE  |

Start with `hello-world` (the smallest end-to-end path); the others each layer on one new idea. More will follow, including a full example that exercises every feature. Each is self-contained and runs **offchain** (free, no wallet); most also run **on-chain** (paid) — see each README.

## Runner modes

Set at registration; **defaults to `persistent`** (both `register_runner(...)` and `runners.json`). The examples set it explicitly.

- **Persistent** — a held-open session billed per second of wall-clock. Best for realtime / streaming. (`echo`)
- **Single-shot** — one request in, one response out. Best for batch / request-response. (`hello-world`, `vllm` are single-shot by nature.)

> [!IMPORTANT]
> Single-shot payment isn't implemented yet ([go-livepeer#3955](https://github.com/livepeer/go-livepeer/issues/3955)), so `hello-world` and `vllm` register as **persistent**. On-chain that bills per second for the whole open session and overbills short calls — keep them **offchain-only** until #3955 lands ([#5](https://github.com/livepeer/live-runner-example-apps/issues/5)).

## Registration

- **Dynamic** — the app self-registers via the SDK (`register_runner`) and heartbeats; the orchestrator drops it when heartbeats stop. Best for apps that come and go. (`hello-world`, `echo`)
- **Static** — the orchestrator is configured with the app's URL in a `runners.json` and health-polls it; the app needs no SDK. Best for fixed, long-running deployments. (`vllm`)

## Calling your app

The client side is the same shape for every app — **discover → reserve → call → release**:

1. **Discover** the app via the orchestrator's `/discovery`.
2. **Reserve** a session (`reserve_session`).
3. **Call** it — one `call_runner`, streamed frames, or a WebSocket, depending on transport.
4. **Release** the session (`stop_runner_session`), which settles payment on-chain.

Each example's `client.py` shows its exact calls — grep `# Livepeer:` to find them.

## External examples

Apps that integrate the live runner and live in their own repos — production deployments and standalone examples alike:

| Project                                                                    | What it is                                       | Transport           |
| -------------------------------------------------------------------------- | ------------------------------------------------ | ------------------- |
| [daydreamlive/scope](https://github.com/daydreamlive/scope/tree/ja/runner) | Real-time AI video with downloadable LoRA models | WebSocket + trickle |

Built one? Open a PR to list it here.

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
