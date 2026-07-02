# Accounts and billing (shared by every integration)

This journey is the same whether the developer ends up using the OpenAI surface, the MCP surface, or both. They do it once, get one bearer token, and that token works everywhere.

## The journey

### 1. Sign in with an existing account (OAuth)

The developer goes to the **payhouse portal** and signs in with an account they already have: Google, GitHub, or any supported OAuth / OIDC provider. No new password, no wallet, no crypto.

Behind the scenes this creates their payhouse account and a metering customer in OpenMeter keyed by their `auth_id`.

### 2. Get free starter credits

On first sign-in the account is granted a **free credit balance** (for example a few dollars of usage). This is a trial entitlement in OpenMeter. It lets the developer try both the LLM and MCP surfaces immediately, before attaching any payment method.

### 3. Get a bearer token

The portal mints an API key: `sk_live_…`. This is the **bearer token** every client uses. The developer copies it once.

- For the **OpenAI** surface it is the `api_key`.
- For the **MCP** surface it is the `Authorization: Bearer` header value.

The key maps to their account, so all usage across both surfaces draws from the same credit balance.

### 4. Use it

Paste the token into a normal client and it just works. No SDK, no wallet, no Livepeer-specific code. See [integration-openai.md](integration-openai.md) and [integration-mcp.md](integration-mcp.md).

### 5. Run out of credit -> attach Stripe

While credits last, requests succeed. When the balance reaches zero, the shared control plane's balance gate (`identity-webhook` + OpenMeter entitlement) starts refusing new requests.

The refusal is a normal, machine-readable error the client already understands, carrying a human message and an upgrade link:

- **OpenAI surface:** HTTP `402 Payment Required` with an OpenAI-style error body:

  ```json
  {
    "error": {
      "type": "insufficient_quota",
      "code": "credits_exhausted",
      "message": "Your free credits are used up. Add a payment method to continue: https://payhouse.blueclaw.network/billing"
    }
  }
  ```

- **MCP surface:** the tool call returns an error result with the same message and link, so the agent (and the human reading the transcript) sees exactly how to continue.

The developer clicks the link, lands in payhouse, and **attaches Stripe** (adds a card). From there they choose prepaid top-ups or postpaid usage billing. The same `sk_live_…` token keeps working; only the funding source changed.

## The flow at a glance

```text
OAuth sign-in ──▶ payhouse account + free credits ──▶ mint sk_live_… token
      │                                                       │
      │                                                       ▼
      │                                        use on OpenAI and/or MCP surface
      │                                                       │
      │                              credits > 0? ──yes──▶ request succeeds, usage metered
      │                                    │
      │                                    no
      │                                    ▼
      └────────────── 402 / tool error + link ──▶ attach Stripe in payhouse ──▶ keep using same token
```

## Why OAuth for sign-in but a bearer token for calls

Two different jobs:

- **OAuth** authenticates the human to the **portal** so they can claim free credits, see usage, and manage billing. It uses an account they already trust.
- **The `sk_live_…` bearer token** authenticates each **API/agent call**. It is a long-lived, copy-pasteable credential that any OpenAI or MCP client can carry with no login flow.

Under the hood the identity webhook can accept either an `sk_` API key (`api_key` mode) or an OAuth JWT directly (`oidc` mode); the portal-minted `sk_` key is the clean default for developer clients because it drops into `OpenAI(api_key=...)` and `claude mcp add --header` without an interactive login.

## What's built vs. to-build

The metering, credit balances, entitlements, and per-user attribution (`auth_id`) exist today in the `clearinghouse` stack (OpenMeter + collector + identity-webhook). The **payhouse portal** (OAuth sign-in, free-credit grant, key minting, Stripe attach) and the **balance gate** that turns "credit = 0" into the `402` / tool-error above are the pieces this design adds.
