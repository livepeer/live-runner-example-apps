# START HERE — run the whole thing (Track B: your Auth0 + John's Kong + your wallet)

One `pmth_` key per account, works in OpenAI + Ollama + Claude. This doc is the single source: folder map, backend + Auth0 + wallet setup, **account creation (per user)**, runners, and the demo.

## The two folders

| Folder | What it is |
| --- | --- |
| `/home/ricks/development/livepeer/ea-worktrees/clearinghouse-demo` | **This repo** (branch `rs/clearinghouse-demo`): the fronts, runners, website, docs. |
| `/home/ricks/development/livepeer/ch-worktrees/pr57-builder-api` | **The clearinghouse backend** (John's PR #57): identity-webhook, remote-signer, builder-api, auth0-provisioner. You run this. |

### What each thing is (this repo)

| Path | Role |
| --- | --- |
| `oauth-gateway/gateway.py` | **OpenAI + Ollama** front. Per-request `pmth_` key → exchange → pay → proxy. |
| `ffmpeg/mcp_server.py` | **Claude (MCP)** front (John's, same exchange). |
| `vllm/` | Local **vLLM** runner (GPU). `gateway_authz.py` = Track A simple gateway. |
| `website/` | Onboarding site: `index.html` + `app.js` (Auth0 popup) + `server.py` (mints key server-side). |
| `docs/hosted-suite/` | Concept docs to walk people through the design. |
| `GO-LIVE.md` | Runner/gateway detail. `DEMO.md` = one-key overview. |

### What each thing is (backend)

| Path | Role |
| --- | --- |
| `auth0-provisioner/provision/bootstrap.sh` | Creates your Auth0 apps → writes `.env.livepeer`. |
| `openmeter-collector/builder-api/` | The exchange API (`:8095`): provision customer, grant $5, mint signer JWT, 402 gate. |
| `openmeter-collector/provision/bootstrap.sh` | Creates OpenMeter meters + the `$5` plan (in John's Kong). |
| `remote-signer/` | The signer (`:8081`). Drop your keystore in `remote-signer/data/keystore/`. |
| `docker-compose.yml`, `.env.example` | The stack + config template. |

## Prerequisites

- Docker + NVIDIA GPU + `uv`.
- Auth0 CLI + `jq` (for your own tenant).
- John's `.env` content (for the OpenMeter/Kong values) — you'll swap the Auth0 values for your own.
- A signer wallet keystore + password (your own on testnet, or John's funded mainnet).

---

## Part A — Your Auth0 (unblocks the OAuth you can't get from John)

```sh
# free tenant at auth0.com, then:
auth0 login
auth0 tenants use YOURTENANT.us.auth0.com
cd /home/ricks/development/livepeer/ch-worktrees/pr57-builder-api/auth0-provisioner/provision
./bootstrap.sh                              # writes .env.livepeer (your client ids + M2M secret)
./bootstrap-credentials-exchange-action.sh  # deploys + binds the credentials-exchange Action
```

Result: `.env.livepeer` holds `DEMO_APP_AUTH0_PUBLIC_CLIENT_ID`, `..._M2M_CLIENT_ID`, `..._M2M_CLIENT_SECRET`, `AUTH0_ISSUER` for **your** tenant.

## Part B — Backend config + wallet + boot

```sh
cd /home/ricks/development/livepeer/ch-worktrees/pr57-builder-api
cp /path/to/johns.env .env    # start from John's env (gitignored)
```

Edit `.env`:
- **Auth0 → yours** (from `.env.livepeer`): `OIDC_ISSUER=https://YOURTENANT.us.auth0.com/`, keep `OIDC_AUDIENCE=livepeer-clearinghouse`, and set `DEMO_APP_AUTH0_PUBLIC_CLIENT_ID` / `_M2M_CLIENT_ID` / `_M2M_CLIENT_SECRET` to your values.
- **OpenMeter → John's** (leave as-is): `OPENMETER_URL`, `OPENMETER_API_KEY=kpat_…`.
- **Signer wallet**: `SIGNER_ETH_ADDR=0xYOURADDR`, plus `SIGNER_NETWORK` + `ETH_RPC_URL` for your chain (testnet recommended: `arbitrum-sepolia` + a Sepolia RPC).

Drop the keystore in:
```sh
mkdir -p remote-signer/data/keystore
cp /path/keystore-file remote-signer/data/keystore/
printf '%s' 'KEYSTORE_PASSWORD' > remote-signer/data/.eth-password
```

Boot + verify:
```sh
docker compose up -d --build
curl -fsS -X POST http://localhost:8081/sign-orchestrator-info      # signer signs -> good
curl -fsS http://localhost:8095/api/v1/docs >/dev/null && echo "builder-api up"
cd openmeter-collector/provision && ./bootstrap.sh catalog          # $5 plan in Kong (idempotent)
cd ../..
```

---

## Part C — Account creation (per user)

Every account = one `externalUserId` → one `pmth_` key + `$5` trial. Two ways.

### C1. CLI mint (fastest, no browser)

```sh
set -a; source .env; set +a
create_account () {  # usage: create_account alice alice@example.com
  curl -sS -u "$DEMO_APP_AUTH0_M2M_CLIENT_ID:$DEMO_APP_AUTH0_M2M_CLIENT_SECRET" \
    -H "Content-Type: application/json" \
    -d "{\"externalUserId\":\"$1\",\"email\":\"$2\"}" \
    "http://localhost:8095/api/v1/apps/${DEMO_APP_AUTH0_PUBLIC_CLIENT_ID}/users"
}
create_account alice alice@example.com    # -> { "apiKey": "pmth_..." }  (Alice's key)
create_account bob   bob@example.com      # -> Bob's key
```
Idempotent per `externalUserId`: the customer + `$5` grant are created once; re-running returns the same account.

### C2. Website OAuth (self-serve, the "Sign in with Google" popup)

```sh
cd /home/ricks/development/livepeer/ea-worktrees/clearinghouse-demo/website
set -a; source /home/ricks/development/livepeer/ch-worktrees/pr57-builder-api/.env; set +a
export AUTH0_DOMAIN=YOURTENANT.us.auth0.com
export AUTH0_SPA_CLIENT_ID=$DEMO_APP_AUTH0_PUBLIC_CLIENT_ID
export BUILDER_API_URL=http://localhost:8095
export GATEWAY_URL=http://localhost:8080/v1
uv run server.py            # http://localhost:8088
```
In **your** Auth0 dashboard, add `http://localhost:8088` to the app's Allowed Callback URLs + Allowed Web Origins + Allowed Logout URLs (your tenant — no John needed). Open `http://localhost:8088`, click **Sign in with Google** → the popup provisions the account server-side and shows the `pmth_` key + copy-paste snippets.

Each person who signs in becomes their own account (`externalUserId = their Auth0 sub`), with their own `$5`.

---

## Part D — Runners (your GPU) + use it

Bring up one runner at a time (both use `:8935`).

### OpenAI (vLLM)

```sh
cd /home/ricks/development/livepeer/ea-worktrees/clearinghouse-demo/vllm
# vllm/.env: NETWORK + ETH_RPC_URL match the backend; ORCH_* = your registered orchestrator
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d orchestrator vllm

cd ../oauth-gateway
LIVEPEER_BILLING_URL=http://localhost:8095 \
LIVEPEER_CLIENT_ID=$DEMO_APP_AUTH0_PUBLIC_CLIENT_ID \
GATEWAY_APP=vllm/qwen2.5-0.5b-instruct \
GATEWAY_FORCE_DISCOVERY=https://localhost:8935/discovery \
uv run gateway.py &
```
```python
from openai import OpenAI
c = OpenAI(base_url="http://localhost:8080/v1", api_key="pmth_ALICE_KEY")
print(c.chat.completions.create(model="Qwen/Qwen2.5-0.5B-Instruct",
      messages=[{"role":"user","content":"Hello!"}]).choices[0].message.content)
```

### Ollama — same gateway, add `GATEWAY_MODEL_MAP='{"qwen2.5:0.5b":"ollama/qwen2.5-0.5b"}'` and bring up the Ollama runner instead.

### Claude (ffmpeg MCP) — same `pmth_` key

```sh
cd ../ffmpeg
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d --build orchestrator app
claude mcp add livepeer-ffmpeg \
  --env LIVEPEER_DISCOVERY=https://localhost:8935/discovery \
  --env LIVEPEER_SIGNER=http://localhost:8081 \
  --env LIVEPEER_BILLING_URL=http://localhost:8095 \
  --env LIVEPEER_CLIENT_ID=$DEMO_APP_AUTH0_PUBLIC_CLIENT_ID \
  --env LIVEPEER_API_KEY=pmth_ALICE_KEY \
  -- uv run --directory "$(pwd)" mcp_server.py
```

## Part E — The full loop

Same `pmth_ALICE_KEY` across OpenAI + Claude. Watch usage climb on Alice in John's OpenMeter/Kong dashboard. Spend the `$5` and every surface returns `402` with the upgrade link. That's the whole product: OAuth once, one key, drop-in everywhere, real Livepeer payment, one metered balance.

Troubleshooting is in `GO-LIVE.md` (discovery override, Auth0 Action, mainnet vs testnet).
