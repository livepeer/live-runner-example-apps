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
import time

import requests
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.base import BaseHTTPMiddleware

from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session
from livepeer_gateway_client.signer_provider import SignerTokenProvider

BILLING = os.environ.get("LIVEPEER_BILLING_URL", "http://localhost:8095").rstrip("/")
CLIENT_ID = os.environ.get("LIVEPEER_CLIENT_ID", "").strip() or None
FALLBACK_DISCOVERY = os.environ.get("LIVEPEER_DISCOVERY", "https://localhost:8935/discovery")
APP_ID = "livepeer/ffmpeg"
PORT = int(os.environ.get("MCP_PORT", "9000"))
# The MCP server runs on the operator's machine, so it writes outputs here and
# returns the path (images are also returned inline for Claude to display).
OUTPUT_DIR = os.path.abspath(os.environ.get("MCP_OUTPUT_DIR", "mcp-outputs"))
EXT = {"transcode": "mp4", "clip": "mp4", "crop": "mp4", "convert": "mp4",
       "gif": "gif", "thumbnail": "jpg", "extract_audio": "m4a"}

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


async def _run_op(input_url: str, op: str, **params):
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

    raw = base64.b64decode(data["output_b64"])
    media = data.get("media_type", "")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    path = os.path.join(OUTPUT_DIR, f"{op}_{int(time.time()*1000)}.{EXT.get(op, 'bin')}")
    with open(path, "wb") as f:
        f.write(raw)
    note = f"{op} done on Livepeer: {len(raw)} bytes ({media}) — paid + metered. Saved to {path}"
    # Return images inline so Claude can display them; note carries the saved path.
    if media.startswith("image/"):
        from mcp.server.fastmcp import Image
        return [Image(data=raw, format=EXT.get(op, "png")), note]
    return note


@mcp.tool()
async def ffmpeg_transcode(input_url: str, height: int = 0, quality: int = 23):
    """Transcode a video (by URL) on Livepeer. height=0 keeps source; lower quality = better."""
    params: dict = {"quality": quality}
    if height:
        params["height"] = height
    return await _run_op(input_url, "transcode", **params)


@mcp.tool()
async def ffmpeg_clip(input_url: str, start: float, end: float):
    """Cut the segment [start, end] seconds from a video URL, paid on Livepeer."""
    return await _run_op(input_url, "clip", start=start, end=end)


@mcp.tool()
async def ffmpeg_thumbnail(input_url: str, at: float = 0.0):
    """Grab a JPEG thumbnail at `at` seconds from a video URL, paid on Livepeer."""
    return await _run_op(input_url, "thumbnail", at=at)


@mcp.tool()
async def ffmpeg_extract_audio(input_url: str):
    """Extract the audio track (m4a) from a video URL, paid on Livepeer."""
    return await _run_op(input_url, "extract_audio")


@mcp.tool()
async def ffmpeg_gif(input_url: str, fps: int = 12, height: int = 240):
    """Make an animated GIF from a video URL, paid on Livepeer."""
    return await _run_op(input_url, "gif", fps=fps, height=height)


@mcp.tool()
async def ffmpeg_crop(input_url: str, width: int, height: int, x: int = 0, y: int = 0):
    """Crop a video URL to width x height at offset (x, y), paid on Livepeer."""
    return await _run_op(input_url, "crop", width=width, height=height, x=x, y=y)


@mcp.tool()
async def ffmpeg_convert(input_url: str, format: str = "mp4", quality: int = 23):
    """Convert a video URL to another container/format, paid on Livepeer."""
    return await _run_op(input_url, "convert", format=format, quality=quality)


@mcp.tool()
async def ffmpeg_probe(input_url: str):
    """Probe a media URL (format, streams, duration), paid on Livepeer."""
    return await _run_op(input_url, "probe")


def main() -> None:
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = PORT
    # Behind a tunnel the Host header isn't localhost, which trips the SDK's
    # DNS-rebinding protection (421). Auth here is the bearer token, not browser
    # cookies, so it's safe to relax it for tunnel/public use.
    if os.environ.get("MCP_ALLOW_TUNNEL", "").lower() in ("1", "true", "yes"):
        mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    app = mcp.streamable_http_app()          # Starlette app, MCP mounted at /mcp
    app.add_middleware(BearerMiddleware)
    print(f"remote MCP (streamable-http) on http://127.0.0.1:{PORT}/mcp  billing={BILLING} client_id={CLIENT_ID}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="info")


if __name__ == "__main__":
    main()
