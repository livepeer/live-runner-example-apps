#!/usr/bin/env python3
"""Remote HTTP MCP server for Livepeer ffmpeg — auth by bearer token in the header.

The true "remote MCP" pattern: Claude connects with a URL + a bearer token,
  claude mcp add --transport http livepeer http://HOST:9000/mcp \
    --header "Authorization: Bearer sk_YOUR_KEY"
No env, no local process. Each tool call reads the caller's key from the
Authorization header, exchanges it at the builder-api for a signer session
(grant/402 gate included), and runs the ffmpeg op paid on Livepeer, metered to
that account.

Because a REMOTE server can't see the client's local files, tools take an
input **URL** (the server fetches it), not a local path.

Env (server-level):
  LIVEPEER_BILLING_URL   builder-api base (default http://localhost:8095)
  LIVEPEER_CLIENT_ID     public app client id (tenant)
  LIVEPEER_DISCOVERY     fallback discovery if the exchange omits one
  MCP_PORT               listen port (default 9000)
"""
from __future__ import annotations

import base64
import contextvars
import os

import requests
import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware

from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session
from livepeer_gateway_client.signer_provider import SignerTokenProvider

BILLING = os.environ.get("LIVEPEER_BILLING_URL", "http://localhost:8095").rstrip("/")
CLIENT_ID = os.environ.get("LIVEPEER_CLIENT_ID", "").strip() or None
FALLBACK_DISCOVERY = os.environ.get("LIVEPEER_DISCOVERY", "https://localhost:8935/discovery")
APP_ID = "livepeer/ffmpeg"
PORT = int(os.environ.get("MCP_PORT", "9000"))

# Per-request bearer token, set by middleware, read by tools.
_current_key: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_key", default=None)

mcp = FastMCP("livepeer-ffmpeg")


class BearerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            _current_key.set(auth[7:].strip())
        else:
            _current_key.set(None)
        return await call_next(request)


def _signer_auth():
    """Exchange the caller's bearer key for a signer session (grant/402 here)."""
    key = _current_key.get()
    if not key:
        raise RuntimeError("missing bearer token (add --header 'Authorization: Bearer sk_...')")
    p = SignerTokenProvider(billing_url=BILLING, api_key=key, client_id=CLIENT_ID)
    p.refresh()
    discovery = getattr(p, "discovery_url", None) or FALLBACK_DISCOVERY
    return p.signer_url, dict(p.headers), discovery


async def _run_op(input_url: str, op: str, **params) -> str:
    try:
        signer_url, signer_headers, discovery = _signer_auth()
    except Exception as exc:
        return f"error: {exc}"
    try:
        blob = requests.get(input_url, timeout=120).content
    except Exception as exc:
        return f"error: could not fetch input_url: {exc}"
    payload = {"op": op, "input_b64": base64.b64encode(blob).decode(), **params}
    session = await reserve_session(discovery_url=discovery, app=APP_ID,
                                    signer_url=signer_url, signer_headers=signer_headers)
    try:
        result = await call_runner(runner_url=session.app_url.rstrip("/") + "/run", payload=payload,
                                   signer_url=signer_url, signer_headers=signer_headers, timeout=600.0)
        data = result.data
    except Exception as exc:
        return f"error: runner call failed: {exc}"
    finally:
        try:
            await stop_runner_session(session)
        except Exception:
            pass
    if op == "probe":
        return f"probe (paid on Livepeer): {data}"
    if "output_b64" not in data:
        return f"error: {data.get('error', data)}"
    return (f"{op} done on Livepeer: {data.get('bytes', 0)} bytes "
            f"({data.get('media_type', '?')}) — paid + metered to your account")


@mcp.tool()
async def ffmpeg_transcode(input_url: str, height: int = 480) -> str:
    """Transcode a video (by URL) to `height` px, running paid on the Livepeer network."""
    return await _run_op(input_url, "transcode", height=height)


@mcp.tool()
async def ffmpeg_thumbnail(input_url: str, at: float = 0.0) -> str:
    """Grab a JPEG thumbnail at `at` seconds from a video URL, paid on Livepeer."""
    return await _run_op(input_url, "thumbnail", at=at)


@mcp.tool()
async def ffmpeg_probe(input_url: str) -> str:
    """Probe a media URL (format, streams, duration), paid on Livepeer."""
    return await _run_op(input_url, "probe")


def main() -> None:
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = PORT
    app = mcp.streamable_http_app()          # Starlette app, MCP mounted at /mcp
    app.add_middleware(BearerMiddleware)
    print(f"remote MCP (streamable-http) on http://127.0.0.1:{PORT}/mcp  billing={BILLING} client_id={CLIENT_ID}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
