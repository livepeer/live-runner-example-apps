#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp"]
# ///
"""Tiny backend for the onboarding site.

Serves the static page, exposes /config (public Auth0 SPA settings), and
/api/provision which calls the builder-api `/users` endpoint with the M2M
secret SERVER-SIDE (never in the browser) to provision the customer + $5 trial
and return the durable API key.

Env (read from the process; source John's .env before running):
  AUTH0_DOMAIN           e.g. pymthouse.us.auth0.com   (no scheme)
  AUTH0_SPA_CLIENT_ID    the SPA app client id used for the popup
  AUTH0_AUDIENCE         livepeer-clearinghouse
  BUILDER_API_URL        http://localhost:8095
  PUBLIC_CLIENT_ID       DEMO_APP_AUTH0_PUBLIC_CLIENT_ID (path param + shown in snippets)
  M2M_CLIENT_ID          DEMO_APP_AUTH0_M2M_CLIENT_ID
  M2M_CLIENT_SECRET      DEMO_APP_AUTH0_M2M_CLIENT_SECRET
  GATEWAY_URL            public OpenAI gateway base (for the snippet), optional
  PORT                   default 8088
"""
from __future__ import annotations

import os

import aiohttp
from aiohttp import web

DOMAIN = os.environ.get("AUTH0_DOMAIN", os.environ.get("OIDC_ISSUER", "").replace("https://", "").strip("/"))
SPA_CLIENT_ID = os.environ.get("AUTH0_SPA_CLIENT_ID", os.environ.get("DEMO_APP_AUTH0_PUBLIC_CLIENT_ID", ""))
AUDIENCE = os.environ.get("AUTH0_AUDIENCE", os.environ.get("DEMO_APP_AUTH0_AUDIENCE", "livepeer-clearinghouse"))
BUILDER_API_URL = os.environ.get("BUILDER_API_URL", "http://localhost:8095").rstrip("/")
PUBLIC_CLIENT_ID = os.environ.get("PUBLIC_CLIENT_ID", os.environ.get("DEMO_APP_AUTH0_PUBLIC_CLIENT_ID", ""))
M2M_ID = os.environ.get("M2M_CLIENT_ID", os.environ.get("DEMO_APP_AUTH0_M2M_CLIENT_ID", ""))
M2M_SECRET = os.environ.get("M2M_CLIENT_SECRET", os.environ.get("DEMO_APP_AUTH0_M2M_CLIENT_SECRET", ""))
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8080/v1")
MCP_URL = os.environ.get("MCP_URL", "http://localhost:9000/mcp")
HERE = os.path.dirname(os.path.abspath(__file__))


async def config(_req: web.Request) -> web.Response:
    return web.json_response({
        "domain": DOMAIN,
        "clientId": SPA_CLIENT_ID,
        "audience": AUDIENCE,
        "billingUrl": BUILDER_API_URL,
        "appClientId": PUBLIC_CLIENT_ID,   # the app the key belongs to (for the SDK exchange)
        "gatewayUrl": GATEWAY_URL,
        "mcpUrl": MCP_URL,
        # hardcoded for now — later serve from the gateway's /v1/models
        "models": [m for m in os.environ.get("MODELS", "Qwen/Qwen2.5-0.5B-Instruct").split(",") if m],
    })


async def provision(req: web.Request) -> web.Response:
    body = await req.json()
    external_user_id = str(body.get("externalUserId", "")).strip()
    email = str(body.get("email", "")).strip()
    if not external_user_id:
        return web.json_response({"error": "externalUserId required"}, status=400)

    url = f"{BUILDER_API_URL}/api/v1/apps/{PUBLIC_CLIENT_ID}/users"
    auth = aiohttp.BasicAuth(M2M_ID, M2M_SECRET)
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json={"externalUserId": external_user_id, "email": email}, auth=auth) as r:
            data = await r.json()
            # builder-api returns apiKey (once) + provisioning info; pass it through.
            return web.json_response(data, status=r.status)


async def index(_req: web.Request) -> web.Response:
    return web.FileResponse(os.path.join(HERE, "index.html"))


def main() -> None:
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/config", config)
    app.router.add_post("/api/provision", provision)
    app.router.add_static("/", HERE)  # app.js etc.
    port = int(os.environ.get("PORT", "8088"))
    print(f"onboarding site on http://localhost:{port}  (Auth0 domain={DOMAIN}, client={SPA_CLIENT_ID})")
    web.run_app(app, host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
