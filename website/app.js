// Basic Livepeer AI onboarding: Auth0 popup -> provision (backend, M2M) -> show key + snippets.
let auth0Client = null;
let CFG = {};

const $ = (id) => document.getElementById(id);

async function boot() {
  CFG = await (await fetch("/config")).json();
  auth0Client = await auth0.createAuth0Client({
    domain: CFG.domain,
    clientId: CFG.clientId,
    authorizationParams: { audience: CFG.audience, scope: "openid profile email" },
  });
  if (await auth0Client.isAuthenticated()) await onSignedIn();
  renderSnippets("pmth_your_key_here");
}

$("login").onclick = async () => {
  try {
    await auth0Client.loginWithPopup();   // <-- the Auth0 popup (Google, etc.)
    await onSignedIn();
  } catch (e) {
    alert("Login failed: " + e.message + "\n(Is this origin allow-listed in the Auth0 app?)");
  }
};

$("logout").onclick = () => auth0Client.logout({ logoutParams: { returnTo: window.location.origin } });

async function onSignedIn() {
  const user = await auth0Client.getUser();
  $("signed-out").classList.add("hidden");
  $("signed-in").classList.remove("hidden");
  $("who").textContent = user.email || user.sub;

  // Provision + mint the durable key on the backend (M2M secret stays server-side).
  // No token needed — the backend mints via M2M; getTokenSilently would require an
  // API grant the SPA app may not have, and its failure must not block the mint.
  let data = {};
  try {
    const res = await fetch("/api/provision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ externalUserId: user.sub, email: user.email }),
    });
    data = await res.json();
  } catch (e) {
    data = { error: String(e) };
  }
  $("key-card").classList.remove("hidden");
  if (data.apiKey) {
    $("apikey").textContent = data.apiKey;
    renderSnippets(data.apiKey);
  } else {
    // key is returned once at signup; a repeat login for the same account has none
    $("apikey").textContent = "(key already issued for this account — it is shown once at signup)";
  }
  $("bal").textContent = data.balance != null ? `$${data.balance}` : "$5.00";
}

$("copy").onclick = () => navigator.clipboard.writeText($("apikey").textContent);

document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".pane").forEach((x) => x.classList.add("hidden"));
    t.classList.add("active");
    document.querySelector(`.pane[data-p="${t.dataset.t}"]`).classList.remove("hidden");
  };
});

function renderSnippets(key) {
  const base = CFG.gatewayUrl || "https://openai.livepeer.example/v1";
  $("snip-openai").textContent =
`from openai import OpenAI
client = OpenAI(base_url="${base}", api_key="${key}")
client.chat.completions.create(
    model="Qwen/Qwen2.5-0.5B-Instruct",
    messages=[{"role": "user", "content": "Hello!"}],
)`;
  $("snip-claude").textContent =
`claude mcp add --transport http livepeer ${CFG.mcpUrl || "http://localhost:9000/mcp"} \\
  --header "Authorization: Bearer ${key}"`;

  $("snip-sdk").textContent =
`import asyncio
from livepeer_gateway.selection import reserve_session
from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway_client.signer_provider import SignerTokenProvider

async def main():
    # 1. exchange your key for a signer session (grants $5 / gates at 402)
    p = SignerTokenProvider(billing_url="${CFG.billingUrl || "http://localhost:8095"}",
                            api_key="${key}",
                            client_id="${CFG.appClientId || ""}")
    p.refresh()

    # 2. reserve a paid session on an app, 3. call the runner directly
    session = await reserve_session(discovery_url=p.discovery_url,
                                    app="vllm/qwen2.5-0.5b-instruct",
                                    signer_url=p.signer_url, signer_headers=p.headers)
    try:
        result = await call_runner(
            runner_url=session.app_url.rstrip("/") + "/v1/chat/completions",
            payload={"model": "Qwen/Qwen2.5-0.5B-Instruct",
                     "messages": [{"role": "user", "content": "Hello!"}]},
            signer_url=p.signer_url, signer_headers=p.headers)
        print(result.data["choices"][0]["message"]["content"])
    finally:
        await stop_runner_session(session)

asyncio.run(main())`;

  // Available models (hardcoded for now; later from the gateway /v1/models)
  const models = (CFG.models && CFG.models.length) ? CFG.models : ["Qwen/Qwen2.5-0.5B-Instruct"];
  $("models-list").textContent = models.join(", ");

  // MCP tools this server exposes (self-described by the MCP server)
  const tools = [
    ["ffmpeg_transcode", "transcode a video URL (optional target height)"],
    ["ffmpeg_clip", "cut a segment [start, end] seconds"],
    ["ffmpeg_thumbnail", "grab a JPEG thumbnail at a timestamp"],
    ["ffmpeg_extract_audio", "extract the audio track (m4a)"],
    ["ffmpeg_gif", "make an animated GIF"],
    ["ffmpeg_crop", "crop to width x height at (x, y)"],
    ["ffmpeg_convert", "convert to another container/format"],
    ["ffmpeg_probe", "probe format / streams / duration"],
  ];
  $("mcp-tools").innerHTML = tools.map(function (t) {
    return "<li><code>" + t[0] + "</code> — " + t[1] + "</li>";
  }).join("");
}

boot();
