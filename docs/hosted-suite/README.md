# Hosted Livepeer AI suite: drop-in OpenAI + MCP

Livepeer's GPU network, served behind the tools developers already use. One account, one bearer token, one shared payment layer. Two integration surfaces on top of it:

- **OpenAI-compatible LLMs** - point the `openai` library at our gateway and call `chat.completions`.
- **Remote MCP tools (agentic)** - `claude mcp add` a hosted MCP server and let an agent call Livepeer capabilities (ffmpeg, and more) as tools.

The developer touches no crypto, no discovery, no payment. They sign in, get a bearer token, and use their normal client.

## The shared idea

Everything below the client is the **same control plane** for both surfaces:

```text
                       ┌───────────────────────────────────────────┐
   Bearer sk_live_…    │        SHARED CONTROL PLANE (we run)       │
  ───────────────────▶ │  identity (sk_ -> user)  ·  balance gate   │
                       │  remote signer (pay)     ·  metering       │
                       │  payhouse (OAuth + credits + Stripe)       │
                       └───────────────────────────────────────────┘
                          ▲                              ▲
                          │                              │
                 ┌────────────────┐            ┌──────────────────┐
   OpenAI client │  OpenAI front  │            │    MCP front     │ Claude / any MCP agent
   ─────────────▶│  /v1/chat/...  │            │  remote MCP URL  │◀─────────────────────
                 └────────────────┘            └──────────────────┘
                          │                              │
                          ▼                              ▼
                    Livepeer network: orchestrators + runners (vLLM / Ollama / ffmpeg)
```

The **auth, payment, credit, and billing logic is written once** and shared. Each integration is just a different front door onto it: one speaks the OpenAI HTTP API, the other speaks MCP. Add a new surface later (plain REST, another agent protocol) and it reuses the same layer.

## The three layers

| Layer | What it is | Lives where |
| --- | --- | --- |
| **Consumer** | Any OpenAI client or MCP-capable agent. Sends `Authorization: Bearer sk_…`. | Developer's machine |
| **Shared control plane** | payhouse portal, routing gateway(s), identity, remote signer, metering, billing. | Run by us |
| **Livepeer network** | Orchestrators + runners doing the actual work. | Decentralized, paid per request |

## Documents

Read in order:

1. [`architecture.md`](architecture.md) - the shared control plane described once, then how each front plugs into it. What runs where, what is called what, and the request lifecycle.
2. [`accounts-and-billing.md`](accounts-and-billing.md) - the developer journey shared by both surfaces: OAuth sign-in, free credits, bearer token, and the Stripe upgrade prompt when credits run out.
3. [`integration-openai.md`](integration-openai.md) - the OpenAI LLM surface, with runnable code.
4. [`integration-mcp.md`](integration-mcp.md) - the remote MCP surface via `claude mcp add`, and the discovery `description` + `input_schema` fields that let an agent understand each capability.

## Relationship to the runner examples

The self-hosted, single-user versions of both surfaces already ship in this repo:

- [`../../vllm`](../../vllm) - OpenAI-compatible LLM behind a local `gateway.py` that does discovery + payment. (`ollama` adds multi-model.)
- `ffmpeg` (see the `ffmpeg` example) - a local **MCP** server exposing ffmpeg tools to Claude, offchain by default, on-chain with a signer.

The hosted suite is those local components promoted to a **multi-tenant, hosted** control plane, plus accounts, credits, and Stripe. If you can run the vLLM and ffmpeg examples, you already understand both data paths; this folder only adds the shared account and billing layer around them.

> Build status: the network, payment (remote signer), and metering (OpenMeter) layers exist today in the [`clearinghouse`](https://github.com/livepeer/clearinghouse) repo, and both local fronts exist as examples. The hosted gateways, per-request bearer auth, the payhouse portal (OAuth + Stripe), and the discovery `description` / `input_schema` fields are what this design adds. See [`architecture.md`](architecture.md) for the built vs. to-build split.
