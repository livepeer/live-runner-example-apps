# vLLM app (OpenAI-compatible LLM)

Runs an OpenAI-compatible LLM on the Livepeer network and consumes it with the **unmodified `openai` library**. The official `vllm/vllm-openai` image serves the model; the orchestrator is **statically attached** to it — no SDK, no registrar, no glue code on the app side. A lightweight **local gateway** (`gateway.py`) runs on your host and does Livepeer's discovery + payment, so the client is plain OpenAI code that knows nothing about Livepeer.

|              |                                            |
| ------------ | ------------------------------------------ |
| App id       | `vllm/qwen2.5-0.5b-instruct`               |
| Runner mode  | persistent (single-shot by nature)         |
| Registration | static (orchestrator config + health poll) |
| Transport    | HTTP + SSE (OpenAI `/v1/chat/completions`) |
| Port         | 8000 (vLLM), 8080 (gateway)                |

**Requires an NVIDIA GPU** for vLLM. The default model (`Qwen/Qwen2.5-0.5B-Instruct`) is tiny so it fits a modest card can be overridden with `VLLM_MODEL`. Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared on-chain/payment setup are in the [repo README](../README.md).

> [!NOTE]
> This app is single-shot by nature but currently registers as **persistent**. It will switch to **single-shot** once [#5](https://github.com/livepeer/live-runner-app-examples/issues/5) lands.

## How it's wired

vLLM is a **static runner**: the orchestrator reads `runners.json` via `-liveRunnerConfig`, health-polls `http://vllm:8000/health`, and reverse-proxies OpenAI requests straight to vLLM — no registrar, no heartbeat, no SDK in the app, nothing to build.

Two sides:

- **Network (compose):** the `orchestrator` (configured with `runners.json`) + the official `vllm` image. The on-chain overlay adds a `signer`.
- **Consumer (host):** the local gateway (`gateway.py`) — an OpenAI endpoint on `:8080` that discovers the runner and (on-chain) pays via the signer — plus any OpenAI client (`client.py`, another SDK, `curl`).

The local gateway is a _client-side_ component, so it runs on the host like the client, not in the infra compose.

The gateway is the **only** Livepeer-aware piece in the whole path — and it's tiny: three SDK calls, `reserve_session` → `call_runner` → `stop_runner_session` (grep `# Livepeer:` in [gateway.py](gateway.py)). They exist _purely_ because an OpenAI client has no idea how to discover a runner or settle Livepeer's payments. Move that glue into the gateway and everything else — `client.py`, any OpenAI SDK, `curl` — stays 100% stock OpenAI, oblivious to Livepeer.

## Run offchain (free)

```sh
docker compose up -d                   # pulls the vLLM + orchestrator images (slow first time)
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm vllm/qwen2.5-0.5b-instruct registered
uv run gateway.py --discovery https://localhost:8935/discovery &   # OpenAI endpoint on http://localhost:8080/v1
uv run client.py --prompt "In one sentence, what is Livepeer?"
kill %1; docker compose down           # stop the gateway, then the stack
```

`client.py` is stock `openai` with `base_url=http://localhost:8080/v1` (the `api_key` is ignored — it just needs _a_ value). Pass a `--model` that matches `VLLM_MODEL`, the name vLLM serves under. Any OpenAI tool works the same way — e.g. `curl`:

```sh
curl http://localhost:8080/v1/chat/completions \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"hi"}]}'
```

### Streaming (SSE)

Set `stream: true` and tokens arrive as they're generated instead of in one blob — the gateway forwards the runner's `text/event-stream` straight through (`call_runner(..., stream=True)`), no buffering. Payment is unchanged: the 402 challenge is handled before any tokens flow.

```sh
uv run client.py --stream --prompt "write a haiku about GPUs"   # prints tokens live
```

Or watch the raw SSE frames with `curl -N` (disables curl buffering):

```sh
curl -N http://localhost:8080/v1/chat/completions \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","stream":true,"messages":[{"role":"user","content":"hi"}]}'
# data: {...}\n data: {...}\n ... data: [DONE]
```

## Run on-chain (paid)

Layer `compose.onchain.yml` to add a remote signer and run the orchestrator on-chain, so vLLM advertises the price from `runners.json` and the gateway pays per call. Needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts
docker compose -f compose.yml -f compose.onchain.yml up -d
uv run gateway.py --signer http://localhost:7936 --discovery https://localhost:8935/discovery &   # gateway pays per request
uv run client.py --prompt "In one sentence, what is Livepeer?"
kill %1; docker compose -f compose.yml -f compose.onchain.yml down
```

The client is **unchanged** — only the gateway gets `--signer`; it pays per call through the remote signer, so the consumer never sees discovery or payment. The price is set in `runners.json` (see the comments in `.env.example`).

Pricing note: the orchestrator meters compute per **second**, not per token. Probabilistic payments are made up front, so token counts can't drive protocol pricing. Per-token billing is left to the signer/gateway layer, which sees `usage` in every response and can bill users per token while paying the orchestrator per second.
