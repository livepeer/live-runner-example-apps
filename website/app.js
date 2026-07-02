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
  const token = await auth0Client.getTokenSilently();
  $("signed-out").classList.add("hidden");
  $("signed-in").classList.remove("hidden");
  $("who").textContent = user.email || user.sub;

  // Provision + mint the durable key on the backend (M2M secret stays server-side).
  const res = await fetch("/api/provision", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: "Bearer " + token },
    body: JSON.stringify({ externalUserId: user.sub, email: user.email }),
  });
  const data = await res.json();
  if (data.apiKey) {
    $("key-card").classList.remove("hidden");
    $("apikey").textContent = data.apiKey;
    renderSnippets(data.apiKey);
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
  $("snip-ollama").textContent =
`# Same key, pick a model
from openai import OpenAI
client = OpenAI(base_url="${base}", api_key="${key}")
client.chat.completions.create(model="qwen2.5:0.5b",
    messages=[{"role": "user", "content": "Hello!"}])`;
  $("snip-claude").textContent =
`claude mcp add livepeer-ffmpeg \\
  --env LIVEPEER_BILLING_URL=${CFG.billingUrl || "http://localhost:8095"} \\
  --env LIVEPEER_CLIENT_ID=${CFG.clientId} \\
  --env LIVEPEER_API_KEY=${key} \\
  --env LIVEPEER_DISCOVERY=https://localhost:8935/discovery \\
  --env LIVEPEER_SIGNER=http://localhost:8081 \\
  -- uv run --directory /ABS/PATH/TO/ffmpeg mcp_server.py`;
}

boot();
