# Using Livepeer AI — keys, OpenAI, curl, MCP

Assumes the stack is running: builder-api `:8095`, OpenAI gateway `:8080/v1`, remote MCP `:9000/mcp`.
Inputs you fill in: **OPENAI_URL** = `http://localhost:8080/v1`, **MCP_URL** = `http://localhost:9000/mcp`, and a **KEY** (below).

## 1. Create an account + key

```sh
cd /home/ricks/development/livepeer/ch-worktrees/pr57-builder-api
set -a; source .env; set +a
curl -sS -u "$DEMO_APP_AUTH0_M2M_CLIENT_ID:$DEMO_APP_AUTH0_M2M_CLIENT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"externalUserId":"alice","email":"alice@example.com"}' \
  "http://localhost:8095/api/v1/apps/${DEMO_APP_AUTH0_PUBLIC_CLIENT_ID}/users" | jq -r .apiKey
```
→ prints `sk_…` (the key + a $5 trial). One key works for OpenAI, curl, and MCP. Use a new `externalUserId` per account; the key is shown once.

## 2. OpenAI — via the script (key + url are inputs)

```sh
cd /home/ricks/development/livepeer/ea-worktrees/clearinghouse-demo
uv run demo.py --key sk_YOUR_KEY --prompt "What is Livepeer?"
# or point at any gateway:  uv run demo.py --key sk_... --gateway http://localhost:8080/v1
```
Minimal hardcoded version: edit `minimal_openai.py` (base_url + api_key) and `uv run --with openai python minimal_openai.py`.

## 3. OpenAI — raw curl

```sh
curl -sS http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer sk_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-0.5B-Instruct","messages":[{"role":"user","content":"Hello!"}]}' \
  | jq -r .choices[0].message.content
```

## 4. ffmpeg — via remote MCP (url + key only)

Register the remote MCP server with Claude — just a URL and a bearer token:
```sh
claude mcp add --transport http livepeer http://localhost:9000/mcp \
  --header "Authorization: Bearer sk_YOUR_KEY"
```
Then ask Claude, e.g.:
> Probe https://download.samplelib.com/mp4/sample-5s.mp4 with the livepeer ffmpeg tool.
> Transcode that URL to 240p.

Tools: `ffmpeg_probe(input_url)`, `ffmpeg_transcode(input_url, height)`, `ffmpeg_thumbnail(input_url, at)`. Input is a **URL** (a remote server can't read your local files). Each call is paid on Livepeer and metered to the account behind the key.

Quick check without Claude:
```sh
cd /home/ricks/development/livepeer/ea-worktrees/clearinghouse-demo/mcp-remote
uv run test_client.py sk_YOUR_KEY https://download.samplelib.com/mp4/sample-5s.mp4
```

---
All three surfaces authenticate the same way: `Authorization: Bearer sk_…`. Watch payments per account:
`( cd /home/ricks/development/livepeer/ch-worktrees/pr57-builder-api && docker compose logs -f remote-signer | grep --line-buffered auth_id= )`
