# Clearinghouse demo: drop-in OpenAI + MCP on Livepeer, paid + metered per user

A runnable walkthrough. A developer uses an **unmodified OpenAI client** (and an **MCP agent**) with a bearer token; behind the scenes the request is authorized, paid for on Livepeer via a shared **remote signer**, and metered per user in OpenMeter. Zero crypto on the client.

This is **Track A** (the fast path): manual bearer tokens, runs on clearinghouse `main`. See [Track B](#track-b--full-oauth--5-trial--cutoff-johns-pr-57) at the bottom for the OAuth + `$5` trial + hard cutoff version.

- Concept docs for walking people through the design: [`docs/hosted-suite/`](docs/hosted-suite/README.md).
- OpenAI front: [`vllm/gateway_authz.py`](vllm/gateway_authz.py) (this repo).
- MCP front: [`ffmpeg/mcp_server_authz.py`](ffmpeg/mcp_server_authz.py) (this repo).

## What runs where

| Where | What | Repo |
| --- | --- | --- |
| clearinghouse stack (Docker) | identity-webhook + remote-signer + Redpanda + collector + OpenMeter | `livepeer/clearinghouse` (`main`) |
| network (Docker) | orchestrator + vLLM (OpenAI) **or** orchestrator + ffmpeg app (MCP) | this repo |
| host | `gateway_authz.py` (OpenAI) **or** `mcp_server_authz.py` (MCP) | this repo |
| host | the developer's OpenAI client / Claude agent | anything |

**One swap makes it "clearinghouse":** each example ships its own bundled signer on `:7936`. We **omit** it and point the host front at the **clearinghouse** signer on `:8081` — the one wired to identity + metering.

```
openai client ─Bearer sk_live_alice─▶ gateway_authz.py (host :8080)
                                         │ forwards Bearer ─▶ clearinghouse signer :8081
                                         │                      └─ identity-webhook resolves sk_ ─▶ auth_id, signs ticket
                                         ▼                         └─ Kafka ─▶ collector ─▶ OpenMeter (per-user usage)
                                   orchestrator :8935 ─▶ vLLM  (OpenAI-native inference)
```

## Prerequisites

- Docker + an **NVIDIA GPU** (vLLM needs one; the ffmpeg MCP demo is CPU-fine).
- `uv` on the host.
- A **funded remote-signer wallet** with an on-chain deposit + reserve. Use **testnet** to avoid real funds (set the same `NETWORK` + `ETH_RPC_URL` on both sides below). `ticketEV` is tiny, so mainnet cost per call is negligible, but testnet is safest for a demo.
- (Only for the usage dashboard) an **OpenMeter / Konnect** API key. The signing + inference path works without it; you just won't see metered usage.

## Part 1 — Clearinghouse (payments + identity + metering)

In a checkout of `livepeer/clearinghouse` on `main`:

```sh
cp .env.example .env
```

Set these in `.env` (manual keys = api_key mode):

```ini
WEBHOOK_SECRET=demo-secret-change-me
IDENTITY_AUTH_MODE=api_key

# Manual bearer tokens -> {clientId}:{userId} == auth_id. Add one per person.
DEMO_API_KEY=sk_live_alice
DEMO_CLIENT_ID=demo
DEMO_USER_ID=alice
DEMO_API_KEYS={"sk_live_bob":{"clientId":"demo","userId":"bob"}}

# Signer authorizes every signing request through the identity webhook.
REMOTE_SIGNER_WEBHOOK_URL=http://identity-webhook:8090/authorize
SIGNER_PORT=8081
SIGNER_NETWORK=arbitrum-sepolia            # <-- same value in the example .env below
ETH_RPC_URL=https://sepolia-rollup.arbitrum.io/rpc   # <-- same in both
SIGNER_ETH_ADDR=0xYOUR_FUNDED_SIGNER_ADDRESS
SIGNER_ETH_KEYSTORE_PATH=/data/keystore

# Only needed to SEE metered usage in a dashboard:
OPENMETER_URL=https://us.api.konghq.com/v3/openmeter
OPENMETER_API_KEY=            # your Konnect key, or leave blank to skip metering
```

Drop your funded signer keystore in place:

```sh
mkdir -p remote-signer/data/keystore
cp /path/to/your/signer/keystore/* remote-signer/data/keystore/
cp /path/to/your/.eth-password       remote-signer/data/.eth-password
```

Start it and confirm the signer can sign:

```sh
docker compose up -d --build kafka identity-webhook remote-signer openmeter-collector
curl -fsS -X POST http://localhost:8081/sign-orchestrator-info
# {"address":"0x…","signature":"0x…"}  -> keystore unlocked, signer live

# sanity-check the api_key -> auth_id resolution:
docker compose exec identity-webhook curl -sS -X POST http://localhost:8090/authorize \
  -H "Authorization: Bearer $WEBHOOK_SECRET" -H "Content-Type: application/json" \
  -d '{"headers":{"Authorization":["Bearer sk_live_alice"]}}'
# expect: "auth_id":"demo:alice"
```

(Optional, for the dashboard) provision meters + customers so usage attributes:

```sh
cd openmeter-collector/provision
./bootstrap.sh catalog
./bootstrap.sh customer demo alice "Alice"
./bootstrap.sh customer demo bob   "Bob"
```

## Part 2 — OpenAI demo (vLLM)

In this repo. Set `vllm/.env` (copy `vllm/.env.example`) with the on-chain **orchestrator** config, matching the clearinghouse network:

```ini
VLLM_MODEL=Qwen/Qwen2.5-0.5B-Instruct
NETWORK=arbitrum-sepolia                              # <-- same as clearinghouse
ETH_RPC_URL=https://sepolia-rollup.arbitrum.io/rpc    # <-- same as clearinghouse
ORCH_KEYSTORE_DIR=/abs/path/to/operator-keystore
ORCH_ETH_ACCT=0xYourOperator
ORCH_ETH_PASSWORD=your-operator-pw
ORCH_ONCHAIN_ADDR=0xYourRegisteredOrchestrator
PRICE_PER_UNIT=1
PIXELS_PER_UNIT=1000
MAX_PRICE_PER_UNIT=0.10USD
# SIGNER_* here are for the bundled signer we are NOT starting — leave blank.
```

Bring up the orchestrator + vLLM **without** the bundled signer, then run the host gateway pointed at the clearinghouse signer:

```sh
cd vllm
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d orchestrator vllm
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # expect vllm/qwen2.5-0.5b-instruct

uv run gateway_authz.py --signer http://localhost:8081 --discovery https://localhost:8935/discovery &
```

Demo with the stock `openai` library:

```python
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8080/v1", api_key="sk_live_alice")
print(client.chat.completions.create(
    model="Qwen/Qwen2.5-0.5B-Instruct",
    messages=[{"role": "user", "content": "In one sentence, what is Livepeer?"}],
).choices[0].message.content)
```

Run it again with `api_key="sk_live_bob"`. In the OpenMeter dashboard, usage lands on **Alice** and **Bob** separately. That is the whole story: drop-in OpenAI, real Livepeer payment, per-user metering.

Wrong/blank key → the gateway 401s (no `Authorization` to forward). Stop when done: `kill %1; docker compose down`.

## Part 3 — MCP demo (ffmpeg agentic)

Same payment layer, an agent instead of an OpenAI client. (Run this **after** stopping the vLLM stack — both orchestrators want `:8935`.)

```sh
cd ffmpeg
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build orchestrator app
```

Register the MCP server with Claude Code, pointing the signer at the clearinghouse and passing a key:

```sh
claude mcp add livepeer-ffmpeg \
  --env LIVEPEER_DISCOVERY=https://localhost:8935/discovery \
  --env LIVEPEER_SIGNER=http://localhost:8081 \
  --env LIVEPEER_API_KEY=sk_live_bob \
  -- uv run --directory /ABS/PATH/TO/ffmpeg mcp_server_authz.py
```

Then ask Claude: *"Clip the first 5 seconds of demo.mp4 and transcode it to 480p with the livepeer ffmpeg tool."* Each tool call is paid via the same signer and metered to **Bob**. This proves the shared payment layer bills an agentic tool call exactly like a chat completion.

## Ports

| Port | Service | Where |
| --- | --- | --- |
| `8081` | clearinghouse remote-signer | clearinghouse (loopback) |
| `8090` | identity-webhook | clearinghouse (internal) |
| `8935` | orchestrator discovery | this repo |
| `8080` | `gateway_authz.py` OpenAI endpoint | host |
| `8000` / `5000` | vLLM / ffmpeg runner | this repo (internal) |

## Troubleshooting

- **Signer 401s the gateway:** the `sk_` you sent is not in `DEMO_API_KEY`/`DEMO_API_KEYS`, or `IDENTITY_AUTH_MODE` isn't `api_key`. Re-check with the `/authorize` curl above.
- **Signer won't start / can't sign:** keystore not in `remote-signer/data/keystore` or `.eth-password` missing; `SIGNER_ETH_ADDR` wrong.
- **Orchestrator rejects payment:** `NETWORK` / `ETH_RPC_URL` differ between the two `.env` files, or the signer wallet has no on-chain deposit + reserve on that network.
- **`signer_headers` unsupported:** if the ffmpeg example's pinned `livepeer-gateway` rev predates `signer_headers`, bump it in `ffmpeg/pyproject.toml` to match `vllm/pyproject.toml`.
- **No usage in the dashboard:** `OPENMETER_API_KEY` blank, or customers not provisioned (`bootstrap.sh customer …`). Signing + inference still work without it.

## Track B — full OAuth + $5 trial + cutoff (John's PR #57)

Track A has no credit gate and no OAuth. The real product exists in John's work:

- **clearinghouse [PR #57](https://github.com/livepeer/clearinghouse/pull/57)** (`feat/session-exchange-openmeter-provisioning`) adds a **builder-api**: Auth0 user provisioning + RFC 8693 token exchange → on first exchange it upserts an OpenMeter customer, starts a subscription, **grants a trial allowance (your `$5`)**, reads the balance, and **mints a signer JWT — or returns HTTP 402 `insufficient_allowance` when credits are gone.**
- **example-apps branch `rs/ffmpeg-mcp-signer`** has a richer `mcp_server.py` whose client already performs the api_key / Auth0-M2M / OIDC token exchange via a `SignerTokenProvider`.

To graduate to Track B: merge/run PR #57's builder-api with **Auth0 tenant + OpenMeter credentials**, base the MCP front on `rs/ffmpeg-mcp-signer`, and point the OpenAI gateway at the exchange endpoint instead of forwarding the raw `sk_`. The developer then signs in with Google/Auth0, gets `$5` automatically, and is cut off at `402` with an upgrade link — exactly the flow in [`docs/hosted-suite/accounts-and-billing.md`](docs/hosted-suite/accounts-and-billing.md).
