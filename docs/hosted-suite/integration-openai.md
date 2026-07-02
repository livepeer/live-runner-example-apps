# Integration: OpenAI-compatible LLMs

Point the unmodified `openai` library at our hosted gateway and call it like any OpenAI endpoint. Two lines of config; everything Livepeer-specific is server-side.

## Prerequisite

An `sk_live_…` bearer token from the payhouse portal. See [accounts-and-billing.md](accounts-and-billing.md).

## Use it

```python
from openai import OpenAI

client = OpenAI(
    base_url="https://openai.blueclaw.network/v1",  # hosted OpenAI gateway
    api_key="sk_live_your_key_here",                 # your payhouse bearer token
)

response = client.chat.completions.create(
    model="Qwen/Qwen3-30B-A3B-FP8",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(response.choices[0].message.content)
```

The SDK sends `Authorization: Bearer sk_live_…` on every request automatically. That header is what the gateway authorizes and bills against.

Streaming works the same way, piped straight through as SSE:

```python
stream = client.chat.completions.create(
    model="Qwen/Qwen3-30B-A3B-FP8",
    messages=[{"role": "user", "content": "Write a haiku about GPUs."}],
    stream=True,
)
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")
```

Any OpenAI-speaking tool works with the same two settings, for example curl:

```sh
curl https://openai.blueclaw.network/v1/chat/completions \
  -H "Authorization: Bearer sk_live_your_key_here" \
  -d '{"model":"Qwen/Qwen3-30B-A3B-FP8","messages":[{"role":"user","content":"hi"}]}'
```

List available models:

```sh
curl https://openai.blueclaw.network/v1/models \
  -H "Authorization: Bearer sk_live_your_key_here"
```

## What happens per call

`client -> OpenAI gateway -> shared control plane -> orchestrator -> vLLM/Ollama`. The gateway authorizes the token, gates on credit, signs a payment ticket tagged with your `auth_id`, proxies the request to a network runner (which serves a native OpenAI API), meters the cost, and returns the response. See the [request lifecycle](architecture.md#request-lifecycle-identical-shape-for-both-fronts).

## Local equivalent

The single-user version of this exact path is the [`vllm`](../../vllm) example: a local `gateway.py` on your host, plain `openai` client, `base_url=http://localhost:8080/v1`. The hosted gateway is that same component made multi-tenant with a public URL and bearer auth. The `ollama` example adds multi-model selection via `--model`.
