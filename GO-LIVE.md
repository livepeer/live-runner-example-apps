# Go live: full loop on your PC (local vLLM + ffmpeg, John's shared creds)

Runs the clearinghouse control plane **yourself**, pointed at John's **shared** Auth0 + OpenMeter, with **local** GPU runners. One `pmth_` key works across OpenAI, Ollama, and Claude.

Architecture recap: you run the control plane + runners; John hosts only the SaaS (Auth0, OpenMeter) and a discovery network you don't need for the local path.

## Prerequisites you provide

- Docker + NVIDIA GPU + `uv`.
- John's `.env` content (already have it).
- The **signer keystore + password** for the funded mainnet wallet → `remote-signer/data/keystore/` + `remote-signer/data/.eth-password`.
- A **registered mainnet orchestrator** (address + operator keystore with a little ETH for gas) for the local runners — `ORCH_*` in the example `.env`. (Mainnet, real funds; `ticketEV` is tiny.)

## Terminals

1. control plane (docker) · 2. runner (docker) · 3. gateway (host) · Claude Code for MCP.

## 1. Control plane — `ch-worktrees/pr57-builder-api`

```sh
cd ../../ch-worktrees/pr57-builder-api
cp /path/to/johns.env .env                 # gitignored; keep secrets out of git
mkdir -p remote-signer/data/keystore
cp /path/keystore/* remote-signer/data/keystore/
printf '%s' 'YOUR_KEYSTORE_PASSWORD' > remote-signer/data/.eth-password

docker compose up -d --build
curl -fsS -X POST http://localhost:8081/sign-orchestrator-info   # signer signs -> good
curl -fsS http://localhost:8095/api/v1/docs >/dev/null && echo "builder-api up"
cd openmeter-collector/provision && ./bootstrap.sh catalog        # $5 plan (if not already)
cd ../..
```

## 2. Mint one account key (the onboarding step)

```sh
set -a; source .env; set +a
curl -sS -u "$DEMO_APP_AUTH0_M2M_CLIENT_ID:$DEMO_APP_AUTH0_M2M_CLIENT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"externalUserId":"alice","email":"alice@example.com"}' \
  "http://localhost:8095/api/v1/apps/${DEMO_APP_AUTH0_PUBLIC_CLIENT_ID}/users"
# -> { "apiKey": "pmth_..." }  == KEY (used everywhere below)
```

## 3. OpenAI demo (local vLLM)

```sh
cd ../../ea-worktrees/clearinghouse-demo/vllm
# vllm/.env: NETWORK=arbitrum-one-mainnet, ETH_RPC_URL, ORCH_* (your registered orch), PRICE_PER_UNIT=1
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d orchestrator vllm
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # vllm/qwen2.5-0.5b-instruct
```

Gateway (terminal 3):

```sh
cd ../oauth-gateway
export LIVEPEER_BILLING_URL=http://localhost:8095
export LIVEPEER_CLIENT_ID=xEJfZBtEP0JLJtlXm9UnJrDrA9bwepLx   # DEMO_APP_AUTH0_PUBLIC_CLIENT_ID
export GATEWAY_APP=vllm/qwen2.5-0.5b-instruct
export GATEWAY_FORCE_DISCOVERY=https://localhost:8935/discovery   # pin the LOCAL runner
uv run gateway.py &
```

```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8080/v1", api_key="pmth_ALICE_KEY")
print(c.chat.completions.create(model="Qwen/Qwen2.5-0.5B-Instruct",
      messages=[{"role":"user","content":"Hello!"}]).choices[0].message.content)
```

Usage appears on Alice in OpenMeter. Ollama is the same gateway: bring up the Ollama runner instead and set `GATEWAY_MODEL_MAP='{"qwen2.5:0.5b":"ollama/qwen2.5-0.5b"}'`.

## 4. Claude demo (local ffmpeg MCP) — same key

Stop the vLLM stack first (frees `:8935`): `cd ../vllm && docker compose ... down`.

```sh
cd ../ffmpeg
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build orchestrator app

claude mcp add livepeer-ffmpeg \
  --env LIVEPEER_DISCOVERY=https://localhost:8935/discovery \
  --env LIVEPEER_SIGNER=http://localhost:8081 \
  --env LIVEPEER_BILLING_URL=http://localhost:8095 \
  --env LIVEPEER_CLIENT_ID=xEJfZBtEP0JLJtlXm9UnJrDrA9bwepLx \
  --env LIVEPEER_API_KEY=pmth_ALICE_KEY \
  -- uv run --directory "$(pwd)" mcp_server.py
```

Ask Claude: *"Transcode demo.mp4 to 480p with the livepeer ffmpeg tool."* Same key → same account → same balance.

## 5. The full loop

Same `pmth_ALICE_KEY` in OpenAI and Claude; OpenMeter shows combined usage on Alice; spend the `$5` and both surfaces return `402` with the upgrade link.

## Gotchas

- **Exchange may hand back a hosted discovery URL.** For local runners, `GATEWAY_FORCE_DISCOVERY` (gateway) pins localhost. For John's `mcp_server.py`, if a tool call routes to the hosted network instead of your local ffmpeg, the exchange returned a `discovery_url` — set `LIVEPEER_DISCOVERY` and confirm it isn't overridden.
- **Auth0 credentials-exchange Action** must be deployed (JWT needs `external_user_id` + `app_client_id`). If the exchange returns `invalid_grant`, that Action is missing.
- Mainnet, real funds. Self-signed orchestrator cert on `:8935` is handled by the SDK.
