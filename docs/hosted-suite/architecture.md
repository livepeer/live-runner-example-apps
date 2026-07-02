# Architecture: shared control plane + two fronts

The consumer runs **nothing** but their normal client. Everything else is operated by us (the shared control plane) or is part of the Livepeer network. The auth/payment/credit/billing logic is written once and reused by every integration surface.

## Part 1: the shared control plane

These components are integration-agnostic. Whether a request arrives as an OpenAI call or an MCP tool call, it flows through the same identity, payment, and metering path.

| Component | Name | Runs where | Role |
| --- | --- | --- | --- |
| **Payhouse portal** | `payhouse` | Hosted by us | Web UI + API. OAuth sign-in (Google, GitHub, ...), free-credit grant, API-key minting, Stripe attach, usage dashboard. |
| **Identity webhook** | `identity-webhook` | Hosted by us | Resolves `Bearer sk_…` (or an OAuth JWT) to `auth_id = "{client_id}:{usage_subject}"`, and confirms the caller still has credit. From the `clearinghouse` repo. |
| **Remote signer** | `remote-signer` | Hosted by us | A go-livepeer instance holding the funded Livepeer deposit. Signs a payment ticket per request, tagged with the caller's `auth_id`. |
| **Event bus** | Redpanda / Kafka | Hosted by us | Carries `create_signed_ticket` events from signer to collector. |
| **Metering collector** | `openmeter-collector` | Hosted by us | Converts each ticket's network fee to USD micros, applies markup, posts a metered event to OpenMeter keyed by `auth_id`. |
| **Metering + billing** | OpenMeter / Konnect | Hosted (SaaS) | Per-customer usage, credit balances, entitlements. Source of truth for "does this user still have credit?". |
| **Stripe** | Stripe | External SaaS | Charges the card. Prepaid top-ups or postpaid usage invoices. |

The one job of every front door is: **take the bearer token, authorize + bill through this layer, then forward the real work to the network.**

## Part 2: the fronts

Each front is a thin adapter that speaks one protocol on the outside and the shared control plane on the inside.

| Front | Name | Speaks | Backs onto |
| --- | --- | --- | --- |
| **OpenAI gateway** | `gateway` | OpenAI HTTP API (`/v1/chat/completions`, `/v1/models`) | vLLM / Ollama runners |
| **MCP gateway** | `mcp-gateway` | MCP over HTTP (remote MCP server) | Tool-style runners (ffmpeg, ...) |

Both are the multi-tenant, hosted evolution of the local components in this repo: the OpenAI gateway is the [vLLM example `gateway.py`](../../vllm/gateway.py); the MCP gateway is the ffmpeg example's `mcp_server.py` made remote and multi-tenant.

## Part 3: the Livepeer network

| Component | Runs where | Role |
| --- | --- | --- |
| `orchestrator` + `vllm` / `ollama` | Livepeer network | OpenAI-native inference. Orchestrator reverse-proxies to the runner and is paid per request. |
| `orchestrator` + `ffmpeg` (and future tool runners) | Livepeer network | Capability runners exposed as agent tools. |

## The picture

```text
DEVELOPER MACHINE
  OpenAI client ─┐                             ┌─ Claude / MCP agent
                 │ Bearer sk_live_…            │ Bearer sk_live_…
=================│=========== HOSTED (we run) =│=================================
                 ▼                             ▼
          ┌────────────┐                ┌────────────┐
          │  OpenAI    │                │    MCP     │        payhouse portal
          │  gateway   │                │  gateway   │◀────── (OAuth + credits + Stripe)
          └─────┬──────┘                └─────┬──────┘         mints sk_ / grants credit
                │        1. authorize + gate  │
                └──────────────┬──────────────┘
                               ▼
                     ┌──────────────────┐        ┌─────────────────┐
                     │ identity-webhook │        │  OpenMeter      │
                     │ sk_ -> auth_id   │───────▶│  credits /      │──▶ Stripe
                     │ + balance gate   │        │  entitlements   │
                     └──────────────────┘        └────────▲────────┘
                               │ 2. pay                   │ meter (auth_id)
                               ▼                          │
                     ┌─────────────────┐   Kafka   ┌──────────────────┐
                     │  remote-signer  │──────────▶│openmeter-collector│
                     │ pooled deposit, │           │ fee -> USD micros │
                     │ signs per auth_id│          └──────────────────┘
                     └─────────────────┘
                               │ 3. forward work
                               ▼
        Livepeer network: orchestrators + runners (vLLM / Ollama / ffmpeg)
```

## Request lifecycle (identical shape for both fronts)

1. **Client -> front.** The client sends its request with `Authorization: Bearer sk_live_…`. The OpenAI SDK and `claude mcp add --header` both attach this automatically.
2. **Authorize + gate.** The front calls `identity-webhook /authorize` with the bearer token. The webhook resolves it to `auth_id` and confirms the OpenMeter balance/entitlement is positive. Unknown key -> `401`. Out of credit -> `402` with an upgrade message (see [accounts-and-billing](accounts-and-billing.md)).
3. **Discover + reserve.** The front selects a healthy orchestrator advertising the requested app and reserves a session.
4. **Pay.** The front asks the `remote-signer` to sign a payment ticket for this request, tagged with `auth_id`. The signer draws on the single pooled deposit and emits a `create_signed_ticket` event to Kafka.
5. **Forward work.** OpenAI front: proxy the body to the runner's `/v1/chat/completions` (streaming piped through as SSE). MCP front: invoke the runner for the called tool and return the tool result.
6. **Meter.** The collector consumes the ticket event, converts the fee to USD micros, and posts a metered event to OpenMeter keyed by `auth_id`. That debits the user's credit and feeds billing.
7. **Respond + release.** The front returns the protocol-native response and releases the session.

Steps 2, 4, and 6 are the shared control plane. Only steps 1, 5, and 7 differ per front, and only in wire format.

## Pooled deposit, per-user accounting

There is one funded Livepeer deposit on the `remote-signer`, shared across every user and both surfaces. On-chain, the network sees a single payer. Off-chain, every request carries an `auth_id`, so OpenMeter attributes cost, credit, and billing per user. Crypto stays on one operator-held account; users only ever see USD credits and an API key. This is the clearinghouse model.

## Built vs. to-build

| Piece | Status |
| --- | --- |
| Orchestrator + vLLM / Ollama / ffmpeg runners on the network | Built (see the examples) |
| Local single-user OpenAI gateway (`gateway.py`) | Built (`vllm` example) |
| Local single-user MCP server (`mcp_server.py`) | Built (`ffmpeg` example) |
| Remote signer, Kafka, OpenMeter collector, metering | Built (`clearinghouse` repo) |
| identity-webhook: `Bearer sk_…` / OIDC -> `auth_id` | Built (`clearinghouse` repo) |
| **Hosted OpenAI gateway** with public `/v1` + per-request bearer auth | To build (promote `gateway.py`, call `/authorize`) |
| **Hosted MCP gateway** (remote MCP server, bearer auth, multi-tenant) | To build (promote `mcp_server.py`) |
| **Balance / entitlement gate** on `/authorize` | To build (add OpenMeter entitlement check) |
| **Payhouse portal**: OAuth sign-in, free credits, key minting, Stripe | To build |
| **Discovery `description` + `input_schema`** on each app | To build (see [integration-mcp](integration-mcp.md)) |

The hard parts (payment, metering, identity) exist and are shared. What remains is the account/UX layer plus the two thin front doors.
