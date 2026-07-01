# vLLM app (OpenAI-compatible LLM)

Runs an OpenAI-compatible LLM on the Livepeer network and consumes it with the **unmodified `openai` library**. The official `vllm/vllm-openai` image serves the model; the orchestrator is **statically attached** to it — no SDK, no registrar, no glue code on the app side. A lightweight **local gateway** (`gateway.py`) runs on your host and does Livepeer's discovery + payment, so the client is plain OpenAI code that knows nothing about Livepeer.

|              |                                            |
| ------------ | ------------------------------------------ |
| App id       | `vllm/qwen2.5-0.5b-instruct`               |
| Runner mode  | single-shot                                |
| Registration | static (orchestrator config + health poll) |
| Transport    | HTTP + SSE (OpenAI `/v1/chat/completions`) |
| Port         | 8000 (vLLM), 8080 (gateway)                |

**Requires an NVIDIA GPU** for vLLM. The default model (`Qwen/Qwen2.5-0.5B-Instruct`) is tiny so it fits a modest card can be overridden with `VLLM_MODEL`. Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared on-chain/payment setup are in the [repo README](../README.md).

## How it's wired

vLLM is a **static runner**: the orchestrator reads `runners.json` via `-liveRunnerConfig`, health-polls `http://vllm:8000/health`, and reverse-proxies OpenAI requests straight to vLLM — no registrar, no heartbeat, no SDK in the app, nothing to build.

Two sides:

- **Network (compose):** the `orchestrator` (configured with `runners.json`) + the official `vllm` image. The on-chain overlay adds a `signer`.
- **Consumer (host):** the local gateway (`gateway.py`) — an OpenAI endpoint on `:8080` that discovers the runner and (on-chain) pays via the signer — plus any OpenAI client (`client.py`, another SDK, `curl`).

The local gateway is a *client-side* component, so it runs on the host like the client, not in the infra compose.

## Run offchain (free)

```sh
docker compose up -d                   # pulls the vLLM + orchestrator images (slow first time)
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # confirm vllm/qwen2.5-0.5b-instruct registered
uv run gateway.py &                    # OpenAI endpoint on http://localhost:8080/v1
uv run client.py --prompt "In one sentence, what is Livepeer?"
kill %1; docker compose down           # stop the gateway, then the stack
```

`client.py` is stock `openai` with `base_url=http://localhost:8080/v1` (the `api_key` is ignored — it just needs *a* value). Pass a `--model` that matches `VLLM_MODEL`, the name vLLM serves under. Any OpenAI tool works the same way — e.g. `curl`:

```sh
curl http://localhost:8080/v1/chat/completions \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"hi"}]}'
```

### Call it directly (no gateway)

As a **single-shot** runner with `"routing": "label"`, vLLM is reachable straight through the orchestrator's proxy (`/apps/<label>/app/<path>`) — no `gateway.py`, no SDK, not even the OpenAI client. This path skips discovery and payment, so it is **offchain-only**:

```sh
curl -Nk https://localhost:8935/apps/vllm/app/v1/chat/completions \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","stream":true,"messages":[{"role":"user","content":"hi"}]}'
```

It also shows the orchestrator reverse-proxy is streaming-transparent: the runner's `text/event-stream` passes straight through, no buffering.

### Streaming (SSE)

> [!IMPORTANT]
> SSE streaming depends on gateway PR [#25](https://github.com/livepeer/livepeer-python-gateway/pull/25), which is not yet merged. This example pins the SDK to its branch (`rs/live-runner-streaming` in `pyproject.toml`); until it lands, streaming is only available from that branch, not `ja/live-runner`.

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

> [!IMPORTANT]
> Single-shot on-chain payment is not implemented yet ([go-livepeer#3955](https://github.com/livepeer/go-livepeer/issues/3955)). Until it lands, vLLM is **offchain-only**, and the direct route above skips payment. This section — the paid path and the per-call billing model — is finalized once single-shot billing ships.
