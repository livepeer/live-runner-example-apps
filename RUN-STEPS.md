# RUN-STEPS — staged execution (do in order, verify each before the next)

The step-by-step path to a working loop. Each step lists the **folder**, the **commands**, and the **pass-check**. If a step fails, stop there — you know exactly which piece. Reference: `START-HERE.md` (map), `GO-LIVE.md` (runner detail).

Absolute folders:
- BACKEND = `/home/ricks/development/livepeer/ch-worktrees/pr57-builder-api`
- DEMO = `/home/ricks/development/livepeer/ea-worktrees/clearinghouse-demo`

| Step | Folder | Goal | Pass-check |
|---|---|---|---|
| 0 | anywhere | tooling + inputs | `auth0 --version` prints |
| 1 | BACKEND/auth0-provisioner/provision | your Auth0 tenant | `.env.livepeer` written |
| 2 | BACKEND | boot control plane | signer signs + builder-api up |
| 3 | BACKEND | mint an account | returns `pmth_…` |
| 4 | DEMO/oauth-gateway | prove the exchange | `EXCHANGE OK` |
| 5 | DEMO/vllm → DEMO/oauth-gateway | first paid call | OpenAI reply |
| 6 | DEMO/ffmpeg | Claude tool call | same key works |

---

## Step 0 — Prep · folder: anywhere

Install the Auth0 CLI from the release binary (this is the method that works;
the bare install script drops the binary in the current dir, so `auth0` isn't
found on PATH):

```sh
mkdir -p ~/.local/bin
VER=$(curl -s https://api.github.com/repos/auth0/auth0-cli/releases/latest | grep -m1 '"tag_name"' | cut -d'"' -f4)
curl -sSL "https://github.com/auth0/auth0-cli/releases/download/${VER}/auth0-cli_${VER#v}_Linux_x86_64.tar.gz" | tar xz -C ~/.local/bin auth0
chmod +x ~/.local/bin/auth0
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
export PATH="$HOME/.local/bin:$PATH"
auth0 --version              # e.g. auth0 version 1.32.0

jq --version                 # sudo apt install -y jq  if missing
```

> IMPORTANT: `export PATH="$HOME/.local/bin:$PATH"` must be run in the SAME
> terminal you continue in (or open a new one after the `.bashrc` line above),
> or `auth0` / `bootstrap.sh` will report "auth0 CLI required".
Have ready: John's `.env` saved to a file; a **chain** choice (Arbitrum Sepolia testnet recommended — no real funds).
**Pass:** `auth0 --version` and `jq --version` both print.

## Step 1 — Your Auth0 · folder: BACKEND/auth0-provisioner/provision

### 1a. Create a free Auth0 account + tenant (one-time, in the browser)

1. Go to https://auth0.com/signup and sign up (free plan is enough). You can sign up with Google/GitHub.
2. During setup Auth0 creates your first **tenant**. Pick a name + region; the region sets the domain suffix (`us`/`eu`/`au`). Your tenant domain looks like `yourname.us.auth0.com` — **note it exactly**, that's `YOURTENANT.us.auth0.com` below.
3. Verify your email if prompted.
4. (Optional) Enable a Google social connection: Dashboard → Authentication → Social → Google, toggle on — so end users can "Sign in with Google" later. Username/password works without this.

You do NOT create any apps/APIs by hand — `bootstrap.sh` does that in 1b.

### 1b. Log in from the CLI + provision

```sh
cd /home/ricks/development/livepeer/ch-worktrees/pr57-builder-api/auth0-provisioner/provision
# Log in AS A USER and request grant-management scopes up front (the default
# session lacks update:client_grants, which bootstrap.sh needs):
auth0 login --scopes "create:client_grants,read:client_grants,update:client_grants,delete:client_grants"
auth0 tenants use YOURTENANT.us.auth0.com     # e.g. dev-xxxx.eu.auth0.com
./bootstrap.sh
./bootstrap-credentials-exchange-action.sh
cat .env.livepeer                    # note the DEMO_APP_AUTH0_* ids + secret + AUTH0_ISSUER
```
**Pass:** `.env.livepeer` exists and has `DEMO_APP_AUTH0_PUBLIC_CLIENT_ID`, `_M2M_CLIENT_ID`, `_M2M_CLIENT_SECRET`.

## Step 2 — Backend config + boot · folder: BACKEND

```sh
cd /home/ricks/development/livepeer/ch-worktrees/pr57-builder-api
```

**2a. `.env`** — your `.env` mixes 3 sources: **your Auth0** (from `.env.livepeer`), **John's OpenMeter** (unchanged), **your wallet**. Start from John's env as `.env`, then set:
```
IDENTITY_AUTH_MODE=oidc
OIDC_ISSUER=https://YOURTENANT.eu.auth0.com/            # = AUTH0_ISSUER in auth0-provisioner/provision/.env.livepeer
OIDC_AUDIENCE=livepeer-clearinghouse                     # unchanged
DEMO_CLIENT_ID / DEMO_APP_AUTH0_PUBLIC_CLIENT_ID / _M2M_CLIENT_ID / _M2M_CLIENT_SECRET   # = your .env.livepeer values
OPENMETER_URL / OPENMETER_API_KEY                        # John's — unchanged
SIGNER_ETH_ADDR=0xYOURADDR                               # your signer wallet
SIGNER_NETWORK=arbitrum-one-mainnet | arbitrum-sepolia   # your wallet's chain
ETH_RPC_URL=<RPC for that chain>
```

**2b. Wallet keystore (point to your existing folder — no copy).** The signer reads `/data/keystore`. Bind-mount your existing keystore dir there with a compose override, and set the address + password.

Create `docker-compose.override.yml` in BACKEND:
```yaml
services:
  remote-signer:
    volumes:
      - /home/ricks/.lpData/arbitrum-one-mainnet/keystore:/data/keystore:ro   # your existing keystore dir
```
Then the password + address:
```sh
printf '%s' 'YOUR_KEYSTORE_PASSWORD' > remote-signer/data/.eth-password
# in .env:  SIGNER_ETH_ADDR=0x<the address you're using>
#           SIGNER_ETH_KEYSTORE_PATH=/data/keystore
```
The folder can hold many keys — the signer picks the one matching `SIGNER_ETH_ADDR`. Find your keystores: `find $HOME -name 'UTC--*' 2>/dev/null`. (Deposit/reserve funding only matters at Step 5; a valid keystore + password + the right address is enough to boot here.)

**2c. Boot + verify:**
```sh
docker compose up -d --build
curl -fsS -X POST http://localhost:8081/sign-orchestrator-info
curl -fsS http://localhost:8095/api/v1/docs >/dev/null && echo "builder-api up"
( cd openmeter-collector/provision && ./bootstrap.sh catalog )
```
**Pass:** signer returns `{"address":…,"signature":…}` and `builder-api up` prints.

## Step 3 — Mint an account · folder: BACKEND

```sh
set -a; source .env; set +a
curl -sS -u "$DEMO_APP_AUTH0_M2M_CLIENT_ID:$DEMO_APP_AUTH0_M2M_CLIENT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"externalUserId":"alice","email":"alice@example.com"}' \
  "http://localhost:8095/api/v1/apps/${DEMO_APP_AUTH0_PUBLIC_CLIENT_ID}/users"
```
**Pass:** response contains `"apiKey":"pmth_…"`. Save it as ALICE_KEY.

## Step 4 — Exchange test (gateway-independent) · folder: DEMO/oauth-gateway

```sh
export LIVEPEER_BILLING_URL=http://localhost:8095
export LIVEPEER_CLIENT_ID=<DEMO_APP_AUTH0_PUBLIC_CLIENT_ID>
uv run exchange_test.py pmth_ALICE_KEY
```
**Pass:** prints `EXCHANGE OK` with a `signer_url` + Bearer `headers`. (A `402` here = the $5 gate working — also a good signal.)

## Step 5 — vLLM + gateway · folders: DEMO/vllm then DEMO/oauth-gateway

```sh
# vllm/.env: NETWORK + ETH_RPC_URL match the backend; ORCH_* = your registered orchestrator
cd DEMO/vllm
docker compose -f docker-compose.yml -f docker-compose.onchain.yml up -d orchestrator vllm
curl -sk https://localhost:8935/discovery | jq '.[].runners[].app'   # vllm/qwen2.5-0.5b-instruct

cd ../oauth-gateway
LIVEPEER_BILLING_URL=http://localhost:8095 LIVEPEER_CLIENT_ID=<PUBLIC_CLIENT_ID> \
GATEWAY_APP=vllm/qwen2.5-0.5b-instruct GATEWAY_FORCE_DISCOVERY=https://localhost:8935/discovery \
uv run gateway.py &

python -c "from openai import OpenAI; print(OpenAI(base_url='http://localhost:8080/v1', api_key='pmth_ALICE_KEY').chat.completions.create(model='Qwen/Qwen2.5-0.5B-Instruct', messages=[{'role':'user','content':'Hello!'}]).choices[0].message.content)"
```
**Pass:** a completion prints; usage appears on Alice in OpenMeter.

## Step 6 — ffmpeg MCP (same key) · folder: DEMO/ffmpeg

Stop the vLLM stack first (frees `:8935`). Then bring up ffmpeg and `claude mcp add …` per `START-HERE.md` Part D, using `LIVEPEER_API_KEY=pmth_ALICE_KEY`.
**Pass:** Claude runs a transcode; usage lands on the same Alice balance.
