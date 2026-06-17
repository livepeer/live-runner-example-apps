# Ollama app (multi-model OpenAI-compatible LLM)

Runs **several** LLMs from a single Ollama server on the Livepeer network and consumes them with the **unmodified `openai` library**. The official `ollama/ollama` image serves the models; the orchestrator is **statically attached** to each one via `runners.json`. A lightweight **local gateway** (`gateway.py`) runs on your host, maps the requested `model` to its runner app, and does Livepeer's discovery + payment, so the client is plain OpenAI code that knows nothing about Livepeer.

|              |                                                  |
| ------------ | ------------------------------------------------ |
| App ids      | `ollama/qwen2.5-0.5b`, `ollama/llama3.2-1b`      |
| Transport    | HTTP (OpenAI `/v1/chat/completions`)             |
| Registration | static (orchestrator config + health poll)       |
| Port         | 11434 (Ollama), 8080 (gateway)                   |

Ollama runs on **CPU or GPU** (the compose file requests a GPU; drop the `deploy` block for CPU-only). Models are small so they fit a modest machine. Prerequisites (Docker, `uv`, the not-yet-released SDK) and the shared on-chain/payment setup are in the [repo README](../README.md).

## How it's wired

Each model is its own **static runner**: `runners.json` lists one app per model, all pointing at the same Ollama backend (`http://ollama:11434`). The orchestrator health-polls the backend, advertises each healthy app on `/discovery`, and reverse-proxies requests straight through — no registrar, no heartbeat, no SDK in the app.

The `app` path is the discovery/selection key — here it's what separates the models. The gateway maps the OpenAI `model` to its app id (`qwen2.5:0.5b` -> `ollama/qwen2.5-0.5b`), reserves that runner, forwards the body unchanged (Ollama still sees the real tag), and releases. One OpenAI endpoint fronts every model; switch with `--model`.

## Limitation: no shared-resource declaration

One Ollama server hosts every model, but each `runners.json` entry has its **own** `capacity` and there's **no way to say these apps share one GPU/backend**. So two models at `capacity: 2` advertise **4** slots that all land on the same process and VRAM — Ollama swaps models in and out, so concurrent requests serialize and real throughput is well below the advertised sum. (Pricing is per-app too, with no shared accounting.)

Until the config gains a shared-resource concept: keep each `capacity` conservative (sized to what the GPU holds co-resident), or run **one backend per model** so each `capacity` maps to real, isolated resources.

> [!TIP]
> Keeping models warm competes for the same VRAM: a longer `keep_alive` only helps if the models fit co-resident — otherwise loading one evicts another. On a shared backend you trade cold-start latency against co-residence.

## Run offchain (free)

```sh
docker compose up -d                   # pulls the Ollama + orchestrator images and the models (slow first time)
curl -sk https://localhost:8935/discovery | jq '.[].runners'   # confirm both models registered
uv run gateway.py &                    # OpenAI endpoint on http://localhost:8080/v1
uv run client.py --model qwen2.5:0.5b --prompt "In one sentence, what is Livepeer?"
uv run client.py --model llama3.2:1b  --prompt "In one sentence, what is Livepeer?"
kill %1; docker compose down           # stop the gateway, then the stack
```

`client.py` is stock `openai` with `base_url=http://localhost:8080/v1`. Pass a `--model` listed in `runners.json`. Any OpenAI tool works the same way — e.g. `curl`:

```sh
curl http://localhost:8080/v1/chat/completions \
  -d '{"model":"llama3.2:1b","messages":[{"role":"user","content":"hi"}]}'
```

> [!NOTE]
> The first call to a model is a **cold load** — Ollama reads the weights into memory and starts the runner before generating, so it is slow. Later calls reuse the resident model and are fast; it stays loaded ~5 min after the last request (`keep_alive`). In production, ping models periodically (or raise `keep_alive`) so users don't hit a cold-start spike after idle.

### Streaming (SSE)

Set `stream: true` and tokens arrive as they're generated; the gateway forwards the runner's `text/event-stream` straight through (`call_runner(..., stream=True)`), no buffering.

```sh
uv run client.py --stream --model qwen2.5:0.5b --prompt "write a haiku about GPUs"
```

## Run on-chain (paid)

Layer `docker-compose.onchain.yml` to add a remote signer and run the orchestrator on-chain, so each model advertises the price from `runners.json` and the gateway pays per call. Needs an Ethereum RPC, a funded signer wallet (deposit + reserve), and an orchestrator wallet — see [On-chain (paid) setup](../README.md#on-chain-paid-setup) in the repo README.

```sh
cp .env.example .env   # fill in RPC, network, keystore paths, accounts
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d
uv run gateway.py --signer http://localhost:7936 &     # gateway pays per request
uv run client.py --model qwen2.5:0.5b --prompt "In one sentence, what is Livepeer?"
uv run client.py --model llama3.2:1b  --prompt "In one sentence, what is Livepeer?"
kill %1; docker compose -f docker-compose.yml -f docker-compose.onchain.yml down
```

The client is **unchanged** — only the gateway gets `--signer`; it pays per call through the remote signer, so the consumer never sees discovery or payment.

## Adding a model

Pull the model, copy a `runners.json` entry (change `label`/`app`; app id = tag with `:` → `-`), and restart the orchestrator so it re-reads the config. For a permanent model, also add the pull to the `ollama-pull` command in `docker-compose.yml`.

```sh
docker compose exec ollama ollama pull mistral:7b      # then add an "ollama/mistral-7b" entry to runners.json
docker compose up -d --force-recreate orchestrator     # re-reads runners.json; new app shows on /discovery
uv run client.py --model mistral:7b --prompt "hi"
```

A **GGUF model from HuggingFace** is the same flow, but alias it to a clean name first (then register that name). `ollama create` pulls the `FROM` source itself, so this one command downloads and aliases:

```sh
docker compose exec ollama sh -c "printf 'FROM hf.co/USER/REPO-GGUF:Q4_K_M\n' > /tmp/MF && ollama create mymodel:tag -f /tmp/MF"
```

> [!NOTE]
> Big models want `capacity: 1` and enough VRAM; a GGUF needs an Ollama build new enough for its architecture. Models published as standard HF weights belong on the [vllm](../vllm) example instead.
