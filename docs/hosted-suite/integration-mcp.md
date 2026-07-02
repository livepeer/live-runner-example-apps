# Integration: remote MCP tools (agentic)

Give an agent (Claude, or any MCP client) access to Livepeer capabilities as **tools** by adding one hosted remote MCP server with a bearer token. Same account, same credits, same payment layer as the OpenAI surface.

## Prerequisite

An `sk_live_…` bearer token from the payhouse portal. See [accounts-and-billing.md](accounts-and-billing.md). It is the same token you use for the OpenAI surface.

## Add it

This is a **remote** MCP server (hosted by us), so it is added over HTTP with an `Authorization` header. Unlike the local `ffmpeg` example (a stdio server configured with `--env`), remote servers carry the bearer token as a header:

```sh
claude mcp add --transport http livepeer \
  https://mcp.blueclaw.network \
  --header "Authorization: Bearer sk_live_your_key_here"
```

Or project-scoped in `.mcp.json`:

```json
{
  "mcpServers": {
    "livepeer": {
      "type": "http",
      "url": "https://mcp.blueclaw.network",
      "headers": { "Authorization": "Bearer sk_live_your_key_here" }
    }
  }
}
```

Then just ask the agent:

```
Clip the first 5 seconds of demo.mp4 and transcode it to 480p with the livepeer tools.
```

The agent calls the Livepeer tools; each call rides through the hosted MCP gateway to a network runner, is paid for with a signed ticket tagged to your account, and the result comes back. Nothing crypto-specific reaches the agent.

## Same payment layer, shared above

The MCP gateway is just another front door onto the [shared control plane](architecture.md#part-1-the-shared-control-plane). A tool call authorizes the bearer token, gates on credit, signs a payment ticket per call (tagged with `auth_id`), invokes the runner, and meters the cost, exactly the same authorize -> pay -> meter path as an OpenAI request. When credits run out the tool call returns an error result with an upgrade link (see [accounts-and-billing](accounts-and-billing.md#5-run-out-of-credit---attach-stripe)). Write the billing logic once; both surfaces use it.

## Tools come from discovery: `description` + `input_schema`

An agent can only use a tool if it knows the tool exists, what it does, and how to call it. To make Livepeer apps self-describing to agents, discovery advertises two extra fields per app:

- **`description`** - natural-language summary of what the capability does, so the agent knows when to reach for it.
- **`input_schema`** - a JSON Schema of the call arguments, so the agent knows how to call it correctly.

Example discovery entry with the new fields:

```json
{
  "app": "livepeer/ffmpeg",
  "description": "Transcode, clip, thumbnail, and probe video/audio files with ffmpeg.",
  "input_schema": {
    "type": "object",
    "properties": {
      "operation": { "type": "string", "enum": ["transcode", "clip", "thumbnail", "probe"] },
      "input_path": { "type": "string" },
      "height": { "type": "integer", "description": "target height for transcode" },
      "start": { "type": "number" },
      "end": { "type": "number" }
    },
    "required": ["operation", "input_path"]
  },
  "price_info": { "price_per_unit": 1, "pixels_per_unit": 1000, "unit": "USD" }
}
```

With these fields, the MCP gateway **generates the tool list straight from discovery**: each advertised app becomes an MCP tool whose name, `description`, and `inputSchema` mirror the discovery entry. No per-capability glue in the gateway. Add a new runner app to the network with a `description` and `input_schema`, and it shows up as an agent tool automatically.

This is the payoff of the MCP surface: discovery is already how the network says "here is what I can do"; adding `description` + `input_schema` turns that same registry into an agent-ready tool catalog.

## Local equivalent

The single-user version is the `ffmpeg` example's `mcp_server.py`: a local stdio MCP server that exposes ffmpeg tools to Claude and forwards through your orchestrator (offchain by default, on-chain with a signer). The hosted MCP gateway is that server made remote and multi-tenant: bearer auth instead of `--env`, one shared server for every user, tools generated from discovery instead of hand-written.
