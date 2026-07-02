# Clearinghouse demo (Track B): OAuth once, one key, works in OpenAI + Ollama + Claude

A developer signs in with OAuth, gets a `$5` trial and **one API key**, and uses Livepeer through **stock clients** with that single key:

- **OpenAI** — `OpenAI(base_url=..., api_key="pmth_...")`
- **Ollama** — same gateway, pick the model
- **Claude (MCP)** — `claude mcp add ... --env LIVEPEER_API_KEY=pmth_...`

One account, one key, one `$5` balance. Every surface exchanges that key for a short-lived signer session, pays on Livepeer, and meters back to the same account. When the trial is spent, every surface returns `402` with an upgrade link.

> One key everywhere. The same `pmth_*` string works in all three clients — usage from any of them draws from the same balance. The gateway is "multi-user" only in the sense that one deployed gateway serves everyone's keys; you personally use exactly one key.

## Pieces

| Where | What | Source |
| --- | --- | --- |
| clearinghouse (Docker) | identity-webhook (oidc) + remote-signer + Redpanda + collector + **builder-api** + OpenMeter | clearinghouse **PR #57** (`feat/session-exchange-openmeter-provisioning`) |
| Auth0 (SaaS) | OAuth login + user provisioning + signer-JWT mint | provisioned by `auth0-provisioner/bootstrap.sh` |
| OpenMeter / Konnect (SaaS) | customers, `$5` trial grant, balance, 402 gate | your Konnect account |
| network (Docker) | orchestrator + vLLM / Ollama / ffmpeg runner | this repo |
| host | OAuth gateway (OpenAI/Ollama) / John's MCP server (Claude) | this repo |

The client → exchange → pay flow (identical for all three surfaces):

```
stock client ─Bearer pmth_KEY─▶ front (OpenAI gateway | Ollama gateway | MCP server)
                                   │ exchange pmth_KEY at builder-api :8095
                                   │   └─ OpenMeter: upsert customer, grant $5, read balance
                                   │        └─ 402 if empty, else mint signer JWT (auth_id)
                                   ▼
                             remote-signer :8081 ── signs ticket (auth_id) ─▶ Kafka ─▶ collector ─▶ OpenMeter
                                   │
                                   ▼
                             orchestrator :8935 ─▶ runner (vLLM / Ollama / ffmpeg)
```

## What I built vs. what you plug in

**Built (in this branch):**
- `oauth-gateway/gateway.py` — the OpenAI **and** Ollama front. Reads the per-request `pmth_*` key, exchanges it via `SignerTokenProvider` (John's exact pattern + pinned client-lib rev), pays, proxies. Model routing for Ollama via `GATEWAY_MODEL_MAP`.
- `ffmpeg/` — John's `rs/ffmpeg-mcp-signer` MCP server (the Claude surface), which already does the same exchange.
- Backend present at `../../ch-worktrees/pr57-builder-api` (clearinghouse PR #57 checked out).

**You plug in (credentials Track B requires — I cannot provision these):**
- An **Auth0 tenant** (for OAuth + signer-JWT mint) and its M2M credentials.
- An **OpenMeter / Konnect** API key (for customers, the `$5` grant, and the 402 gate).
- Your **funded signer wallet** (keystore + on-chain deposit/reserve; testnet is fine).

Track B does not boot without Auth0 + OpenMeter. Everything up to those is done.

## Setup

### 1. Backend (clearinghouse PR #57)

Use the checked-out worktree: `cd ../../ch-worktrees/pr57-builder-api`.

```sh
cp .env.example .env
```

Set `.env` for the exchange path (OIDC mode + builder-api + signer):

```ini
WEBHOOK_SECRET=demo-secret-change-me
# JWT verification for the exchanged Auth0 tokens:
IDENTITY_AUTH_MODE=oidc
OIDC_ISSUER=https://YOUR_TENANT.us.auth0.com/
OIDC_AUDIENCE=livepeer-clearinghouse
OIDC_CLIENT_CLAIM=app_client_id
OIDC_SUBJECT_CLAIM=external_user_id
OIDC_SUBJECT_TYPE=external_user_id

# Signer (your funded wallet; testnet to avoid real funds):
REMOTE_SIGNER_WEBHOOK_URL=http://identity-webhook:8090/authorize
SIGNER_PORT=8081
SIGNER_NETWORK=arbitrum-sepolia
ETH_RPC_URL=https://sepolia-rollup.arbitrum.io/rpc
SIGNER_ETH_ADDR=0xYOUR_FUNDED_SIGNER

# OpenMeter (trial grant + balance + 402 gate):
OPENMETER_URL=https://us.api.konghq.com/v3/openmeter
OPENMETER_API_KEY=YOUR_KONNECT_KEY
```

Provision Auth0 (creates the M2M apps, the public app client id, and writes `auth0-provisioner/provision/.env.livepeer`), deploy the credentials-exchange Action per `openmeter-collector/builder-api/README.md`, then drop your signer keystore and start:

```sh
./auth0-provisioner/provision/bootstrap.sh          # needs Auth0 mgmt creds
mkdir -p remote-signer/data/keystore && cp /path/keystore/* remote-signer/data/keystore/
cp /path/.eth-password remote-signer/data/.eth-password

docker compose up -d --build     # kafka, identity-webhook, remote-signer, openmeter-collector (+builder-api)
curl -fsS -X POST http://localhost:8081/sign-orchestrator-info   # signer alive
```

Provision the OpenMeter catalog (defines the `$5` trial plan):

```sh
cd openmeter-collector/provision && ./bootstrap.sh catalog
```

### 2. Mint an account (OAuth → one key)

The onboarding step that gives a user their `$5` and their single key. Interactively this is the OAuth login in a portal; headless, hit the builder-api users endpoint (M2M):

```sh
set -a; source openmeter-collector/.env; set +a
curl -sS -u "$AUTH0_SIGNER_M2M_CLIENT_ID:$AUTH0_SIGNER_M2M_CLIENT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"externalUserId":"alice","email":"alice@example.com"}' \
  "http://localhost:8095/api/v1/apps/${DEMO_APP_AUTH0_PUBLIC_CLIENT_ID}/users"
# -> returns { "apiKey": "pmth_..." }  (shown once). This is Alice's ONE key.
```

Note `DEMO_APP_AUTH0_PUBLIC_CLIENT_ID` — that is the `LIVEPEER_CLIENT_ID` every front uses.

### 3. Runner (this repo)

Bring up one runner at a time (all use `:8935`). vLLM for OpenAI, Ollama for Ollama, ffmpeg for MCP. Omit the bundled signer — the front points at the clearinghouse signer.

```sh
cd vllm && docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d orchestrator vllm
```

Set `vllm/.env` orchestrator config to the **same** `NETWORK` + `ETH_RPC_URL` as the clearinghouse `.env` (see `vllm/.env.example`).

## Use it — same `pmth_` key in all three

### OpenAI (vLLM)

```sh
cd oauth-gateway
export LIVEPEER_BILLING_URL=http://localhost:8095
export LIVEPEER_CLIENT_ID=$DEMO_APP_AUTH0_PUBLIC_CLIENT_ID
export GATEWAY_APP=vllm/qwen2.5-0.5b-instruct
uv run gateway.py &        # OpenAI endpoint on :8080
```
```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8080/v1", api_key="pmth_ALICE_KEY")
print(c.chat.completions.create(model="Qwen/Qwen2.5-0.5B-Instruct",
      messages=[{"role":"user","content":"Hello!"}]).choices[0].message.content)
```

### Ollama (same gateway, model routing)

Bring up the Ollama runner instead of vLLM, then run the same gateway with a model map:

```sh
export GATEWAY_MODEL_MAP='{"qwen2.5:0.5b":"ollama/qwen2.5-0.5b","llama3.2:1b":"ollama/llama3.2-1b"}'
uv run gateway.py &
```
```python
c = OpenAI(base_url="http://localhost:8080/v1", api_key="pmth_ALICE_KEY")   # SAME key
c.chat.completions.create(model="qwen2.5:0.5b", messages=[{"role":"user","content":"hi"}])
```

### Claude (MCP)

```sh
claude mcp add livepeer-ffmpeg \
  --env LIVEPEER_DISCOVERY=https://localhost:8935/discovery \
  --env LIVEPEER_SIGNER=http://localhost:8081 \
  --env LIVEPEER_BILLING_URL=http://localhost:8095 \
  --env LIVEPEER_CLIENT_ID=$DEMO_APP_AUTH0_PUBLIC_CLIENT_ID \
  --env LIVEPEER_API_KEY=pmth_ALICE_KEY \
  -- uv run --directory /ABS/PATH/TO/ffmpeg mcp_server.py
```

All three exchange the same `pmth_ALICE_KEY` → same `auth_id` → same `$5` balance. Spend it down and every surface starts returning `402` with the upgrade link. That is the whole Track B story: OAuth once, one key, drop-in everywhere, real payment, one metered balance.

## Concept docs for walking people through it

`docs/hosted-suite/` — the architecture and the accounts/billing flow in prose.

## Fallback: Track A (no OAuth, main only)

If you want a zero-credential quick version (manual `sk_` keys, no `$5` gate, clearinghouse `main` in `api_key` mode), use `vllm/gateway_authz.py` with `--signer http://localhost:8081`; see the git history of this file for the full Track A runbook.
