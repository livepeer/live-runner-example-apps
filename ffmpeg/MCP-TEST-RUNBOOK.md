# MCP + Livepeer Network Test Runbook

Validated: 2026-07-01. Branch: `rs/ffmpeg` (worktree at `live-runner-example-apps-rs-ffmpeg`).

## Prerequisites

| Item | Command / value |
|------|-----------------|
| Branch | `git fetch origin rs/ffmpeg && git worktree add ../live-runner-example-apps-rs-ffmpeg origin/rs/ffmpeg` |
| Docker stack | `cd ffmpeg && docker compose up -d --build` |
| Discovery | `curl -sk https://localhost:8935/discovery \| jq '.[].runners[].app'` → `"livepeer/ffmpeg"` |
| Test clip | `docker run --rm -v $PWD:/out jrottenberg/ffmpeg:4.4-alpine -f lavfi -i testsrc=duration=3:size=640x480:rate=24 -pix_fmt yuv420p -y /out/clip.mp4` |
| Claude Code | `claude --version` |
| Clearinghouse stack (signer path) | `docker compose up -d` in `clearinghouse` repo → Builder API `:8095`, remote-signer `127.0.0.1:8081` |

## 1. Offchain smoke test (PASS)

```sh
cd ffmpeg
docker compose up -d --build
uv run client.py --op transcode --height 480 --input clip.mp4 --output out480.mp4

claude mcp remove livepeer-ffmpeg 2>/dev/null || true
claude mcp add livepeer-ffmpeg \
  --env LIVEPEER_DISCOVERY=https://localhost:8935/discovery \
  -- uv run --directory "$(pwd)" mcp_server.py

claude -p "Transcode clip.mp4 to 480p, output mcp_out480.mp4" \
  --allowedTools "mcp__livepeer-ffmpeg__ffmpeg_transcode" < /dev/null

ffprobe -v error -show_entries stream=width,height -of csv=p=0 mcp_out480.mp4
# expect: 640,480
```

**Result:** `mcp_out480.mp4` written (21,465 bytes).

## 2. Signer URL interaction

### Precedence

1. Exchange response `signer_url` (from `{billing_url}/api/v1/apps/{client_id}/oidc/token`)
2. `LIVEPEER_SIGNER` env fallback
3. Direct Auth0 M2M when `LIVEPEER_AUTH0_M2M_*` set (bypasses billing when OpenMeter is down)

### Reachability check

```sh
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8081
# 404 is OK — signer is reachable
```

### Validation script

```sh
export LIVEPEER_SIGNER=http://localhost:8081
export LIVEPEER_OIDC_URL=https://pymthouse.us.auth0.com
export LIVEPEER_OIDC_CLIENT_ID=xEJfZBtEP0JLJtlXm9UnJrDrA9bwepLx
export LIVEPEER_BILLING_URL=http://localhost:8095
export LIVEPEER_AUTH0_M2M_CLIENT_ID=<m2m_client_id>
export LIVEPEER_AUTH0_M2M_CLIENT_SECRET=<m2m_secret>
export LIVEPEER_EXTERNAL_USER_ID=demo-user

uv run python scripts/validate_signer_session.py
```

| Check | Expected |
|-------|----------|
| `signer_reachability` | PASS (HTTP 404 on root = reachable) |
| `cached_oidc_reuse` | PASS (same access_token on second call) |
| `auth0_m2m_mint` | PASS (token_len > 0) |
| `oidc_exchange` | **FAIL** if OpenMeter misconfigured (see below) |

## 3. Clearinghouse signer-backed MCP via Builder API exchange (PASS)

Validated 2026-07-01 against `clearinghouse` branch `feat/session-exchange-openmeter-provisioning`.
The RFC 8693 exchange at `http://localhost:8095` now provisions the OpenMeter customer +
default subscription and mints the signer JWT end-to-end.

### 3a. Builder API fixes required (clearinghouse repo)

The earlier `openmeter list customers 404` was **not** a Konnect misconfig. The collector
`entrypoint.sh` rewrites `OPENMETER_URL` to the events-ingestion path (`…/openmeter/events`)
for benthos, and `builder-api` inherited the same var — so customer REST calls hit
`…/openmeter/events/customers` (404) instead of `…/openmeter/customers` (200). Fixes on the
branch:

- `openmeter-collector/entrypoint.sh` — launch `builder-api` with the **un-mutated** management
  base URL (`OPENMETER_URL="$openmeter_mgmt_url"`), keep the `/events` URL for benthos only.
  Also restored the dual-process supervisor that starts `builder-api` alongside benthos.
- `docker-compose.yml` — publish `127.0.0.1:8095:8095` and mount
  `auth0-provisioner/provision/.env.livepeer:/service/.env.livepeer:ro` (supplies
  `AUTH0_MGMT_*`; without it the entrypoint skips `builder-api`).

Rebuild: `docker compose up -d --build openmeter-collector`. Confirm both procs:
`docker exec clearinghouse-openmeter-collector-1 ps | grep -E 'benthos|builder-api'`.

### 3b. Non-interactive API-key exchange (used for MCP)

```sh
set -a; . auth0-provisioner/provision/.env.livepeer; set +a
PUB="$DEMO_APP_AUTH0_PUBLIC_CLIENT_ID"

# Create end-user → returns sk_* apiKey once (M2M Basic auth).
curl -sS -u "$DEMO_APP_AUTH0_M2M_CLIENT_ID:$DEMO_APP_AUTH0_M2M_CLIENT_SECRET" \
  -H 'Content-Type: application/json' \
  -d '{"externalUserId":"mcp-test","email":"mcp-test@example.com"}' \
  "http://localhost:8095/api/v1/apps/$PUB/users"

# Exchange the sk_* key → signer JWT + signer_url + discovery_url (HTTP 200).
curl -sS -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'grant_type=urn:ietf:params:oauth:grant-type:token-exchange' \
  --data-urlencode "subject_token=$API_KEY" \
  --data-urlencode 'subject_token_type=urn:ietf:params:oauth:token-type:access_token' \
  "http://localhost:8095/api/v1/apps/$PUB/oidc/token"
```

Exchange response: `signer_url=http://localhost:8081`, `scope` includes `sign:job`,
`discovery_url` from builder-api `DISCOVERY_URL`.

**ffmpeg discovery note:** `livepeer/ffmpeg` only exists on the **local** offchain orchestrator,
not the production network. For an ffmpeg transcode set the builder-api
`DISCOVERY_URL=https://localhost:8935/discovery` in the clearinghouse `.env` (the exchange
`discovery_url` overrides `LIVEPEER_DISCOVERY` in the MCP), then restart the collector. Use the
production discovery for streamdiffusion/vllm.

### 3c. Register + run MCP (full Builder API path)

```sh
set -a; . /home/elite/repos/clearinghouse/auth0-provisioner/provision/.env.livepeer; set +a
claude mcp add livepeer-ffmpeg-builder \
  --env LIVEPEER_DISCOVERY=https://localhost:8935/discovery \
  --env LIVEPEER_BILLING_URL=http://localhost:8095 \
  --env LIVEPEER_CLIENT_ID="$DEMO_APP_AUTH0_PUBLIC_CLIENT_ID" \
  --env LIVEPEER_API_KEY=sk_... \
  -- uv run --directory "$(pwd)" mcp_server.py

claude -p "Transcode clip.mp4 to 480p, output builder_mcp_out480.mp4" \
  --allowedTools "mcp__livepeer-ffmpeg-builder__ffmpeg_transcode" < /dev/null
ffprobe -v error -show_entries stream=width,height -of csv=p=0 builder_mcp_out480.mp4  # 640,480
```

**Result:** `builder_mcp_out480.mp4` written (21,465 bytes, 640×480). Auth resolved via
`SignerTokenProvider` billing-exchange branch — no device login, no Auth0 M2M bypass.

### Auth0 M2M direct bypass (fallback, still works)

When the Builder API/OpenMeter is unavailable, mint directly against Auth0 with
`LIVEPEER_AUTH0_M2M_CLIENT_ID` / `_SECRET` + `LIVEPEER_SIGNER=http://localhost:8081` (takes the
M2M branch in `mcp_server._resolve_signer_auth`, bypassing billing).

## 4. Auth cache + refresh (PASS)

| Test | Result |
|------|--------|
| OIDC cache reuse | Same `access_token` on consecutive `ensure_valid_token()` calls |
| Signer auth error detection | `"exp" claim expiration is past` → `is_signer_auth_error()` = true |
| MCP retry on 401 | `mcp_server._call()` re-mints via `SignerTokenProvider.refresh()` when provider is set |

Force fresh device login:

```sh
cd /path/to/livepeer-gateway
uv run examples/device_login.py \
  --issuer https://pymthouse.us.auth0.com \
  --client-id xEJfZBtEP0JLJtlXm9UnJrDrA9bwepLx
```

## 5. PymtHouse parity (PARTIAL)

PymtHouse shares the Auth0 issuer (`pymthouse.us.auth0.com`). MCP registration is **env-only** — no code changes.

| Billing endpoint | Status |
|------------------|--------|
| `http://localhost:8095` (Clearinghouse Builder API) | Up; exchange blocked by OpenMeter 404 |
| `https://staging.pymthouse.com` | 404 on `/api/v1/apps/.../oidc/token` |
| `https://pymthouse.com` | 404 on token exchange route |

**Parity verified:** same MCP server + Auth0 M2M env works under `livepeer-ffmpeg-pymthouse` name:

```sh
claude mcp add livepeer-ffmpeg-pymthouse \
  --env LIVEPEER_DISCOVERY=https://localhost:8935/discovery \
  --env LIVEPEER_SIGNER=http://localhost:8081 \
  --env LIVEPEER_OIDC_URL=https://pymthouse.us.auth0.com \
  --env LIVEPEER_AUTH0_M2M_CLIENT_ID=<m2m_id> \
  --env LIVEPEER_AUTH0_M2M_CLIENT_SECRET=<secret> \
  --env LIVEPEER_EXTERNAL_USER_ID=demo-user \
  -- uv run --directory "$(pwd)" mcp_server.py
```

When PymtHouse billing is available, swap env only:

```sh
--env LIVEPEER_BILLING_URL=https://staging.pymthouse.com \
--env LIVEPEER_CLIENT_ID=app_bf4a4dd275594713afe37052 \
--env LIVEPEER_API_KEY=pmth_demo_api_key
```

Exchange should return `signer_url` (e.g. `https://pymthouse-preview.up.railway.app`).

## Failure signatures

| Symptom | Cause | Fix |
|---------|-------|-----|
| `No such tool available` | MCP server crashed at import (auth resolve at module load) | Fixed: lazy auth in `mcp_server.py`; re-register MCP |
| `openmeter customer provisioning failed` / `list customers 404` | `builder-api` inherited the events-mutated `OPENMETER_URL` → hit `…/events/customers` | Launch `builder-api` with un-mutated mgmt base (entrypoint fix in §3a) |
| `builder-api: skipped — AUTH0_MGMT_* not set` | `.env.livepeer` not mounted into collector | Add `.env.livepeer:/service/.env.livepeer:ro` mount (compose fix in §3a) |
| `invalid_grant` on exchange | JWT `azp` ≠ path `{clientId}` | Match `LIVEPEER_OIDC_CLIENT_ID` to Auth0 app |
| Signer 401 / exp claim | Short-lived signer JWT expired | Auto-refresh via provider; or re-run device login |
| `No runners available` on production discovery | `livepeer/ffmpeg` not on network orchestrators | Use local stack for ffmpeg MCP; production for streamdiffusion/vllm |
| Container name conflict | Stale `example_apps_orchestrator` | `docker rm example_apps_orchestrator` then re-up |

## MCP env reference

| Variable | Purpose |
|----------|---------|
| `LIVEPEER_DISCOVERY` | Orchestrator discovery URL |
| `LIVEPEER_SIGNER` | Remote signer base URL (fallback) |
| `LIVEPEER_OIDC_URL` | Auth0 issuer |
| `LIVEPEER_BILLING_URL` | Builder / PymtHouse billing API |
| `LIVEPEER_OIDC_CLIENT_ID` | Public app client for device login + exchange |
| `LIVEPEER_API_KEY` | `sk_*` or `pmth_cs_*` for non-interactive exchange |
| `LIVEPEER_CLIENT_ID` | Public client for API-key exchange |
| `LIVEPEER_AUTH0_M2M_CLIENT_ID` | Direct Auth0 M2M (bypass billing) |
| `LIVEPEER_AUTH0_M2M_CLIENT_SECRET` | M2M secret |
| `LIVEPEER_EXTERNAL_USER_ID` | End-user scope for M2M mint |
| `LIVEPEER_OIDC_AUDIENCE` | Default `livepeer-clearinghouse` |

## Recommended auth mode for Claude Code

| Mode | When |
|------|------|
| **Offchain** | Local dev, no payment |
| **Device login + exchange** | Interactive dev with working Builder API |
| **Auth0 M2M direct** | Headless MCP when OpenMeter/billing exchange is down |
| **API key / pmth_cs_*** | CI / unattended when billing API is reachable |
