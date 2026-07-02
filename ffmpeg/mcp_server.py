#!/usr/bin/env python3
"""Local MCP server exposing the livepeer/ffmpeg capability to Claude as tools.

Each tool reserves a session on your orchestrator, sends a *local* file to the
`livepeer/ffmpeg` runner, and writes the result back to a local path. Batch ops
on a persistent runner (single-shot payment isn't wired yet, go-livepeer#3955).

Offchain by default. For paid / Clearinghouse / PymtHouse sessions, configure
signer auth via env (see README-mcp.md and MCP-TEST-RUNBOOK.md).

  LIVEPEER_DISCOVERY          orchestrator discovery URL
  LIVEPEER_SIGNER               remote signer base URL (fallback when exchange omits signer_url)
  LIVEPEER_OIDC_URL             Auth0 issuer (device login + RFC 8693 exchange)
  LIVEPEER_BILLING_URL          Builder / PymtHouse billing API base
  LIVEPEER_OIDC_CLIENT_ID       public app client id for OIDC + exchange
  LIVEPEER_API_KEY              sk_* or pmth_cs_* for non-interactive exchange
  LIVEPEER_CLIENT_ID            public client id for API-key exchange
  LIVEPEER_M2M_CLIENT_ID        confidential client id for pmth_cs_* exchange
  LIVEPEER_EXTERNAL_USER_ID     end-user id for M2M mint
  LIVEPEER_AUTH0_M2M_CLIENT_ID  direct Auth0 M2M mint (bypasses billing when OpenMeter unavailable)
  LIVEPEER_AUTH0_M2M_CLIENT_SECRET
  LIVEPEER_OIDC_AUDIENCE        default livepeer-clearinghouse
  LIVEPEER_OIDC_SCOPES          default openid sign:job offline_access
"""
from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

try:  # long-standing public API
    from mcp.server.fastmcp import FastMCP
except ImportError:  # newer SDK rename (MCPServer == FastMCP)
    from mcp.server import MCPServer as FastMCP  # type: ignore

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session

if TYPE_CHECKING:
    from livepeer_gateway_client.signer_provider import SignerTokenProvider

log = logging.getLogger("livepeer-ffmpeg-mcp")

DISCOVERY = os.environ.get("LIVEPEER_DISCOVERY", "https://localhost:8935/discovery")
APP_ID = "livepeer/ffmpeg"
DEFAULT_OIDC_AUDIENCE = "livepeer-clearinghouse"
DEFAULT_OIDC_SCOPES = "openid sign:job offline_access"

mcp = FastMCP("livepeer-ffmpeg")


@dataclass
class SignerAuth:
    signer_url: str | None
    signer_headers: dict[str, str] | None
    provider: SignerTokenProvider | None = None
    discovery_url: str = DISCOVERY


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _resolve_signer_auth() -> SignerAuth:
    """Resolve signer URL + bearer headers from env (Clearinghouse / PymtHouse / local)."""
    signer_url = _env("LIVEPEER_SIGNER") or None
    discovery_url = DISCOVERY
    signer_headers: dict[str, str] | None = None
    provider: SignerTokenProvider | None = None

    oidc_url = _env("LIVEPEER_OIDC_URL")
    billing_url = _env("LIVEPEER_BILLING_URL")
    oidc_client_id = _env("LIVEPEER_OIDC_CLIENT_ID")
    api_key = _env("LIVEPEER_API_KEY")
    client_id = _env("LIVEPEER_CLIENT_ID") or oidc_client_id
    m2m_client_id = _env("LIVEPEER_M2M_CLIENT_ID")
    external_user_id = _env("LIVEPEER_EXTERNAL_USER_ID")
    auth0_m2m_id = _env("LIVEPEER_AUTH0_M2M_CLIENT_ID")
    auth0_m2m_secret = _env("LIVEPEER_AUTH0_M2M_CLIENT_SECRET")
    oidc_audience = _env("LIVEPEER_OIDC_AUDIENCE") or DEFAULT_OIDC_AUDIENCE
    oidc_scopes = _env("LIVEPEER_OIDC_SCOPES") or DEFAULT_OIDC_SCOPES

    # Direct Auth0 M2M bypasses billing API (useful when OpenMeter exchange is down).
    if auth0_m2m_id and auth0_m2m_secret and oidc_url:
        from livepeer_gateway_client.oidc_auth import client_credentials_token

        token = client_credentials_token(
            oidc_url,
            client_id=auth0_m2m_id,
            client_secret=auth0_m2m_secret,
            audience=oidc_audience,
            external_user_id=external_user_id or "demo-user",
        )
        signer_headers = {"Authorization": f"Bearer {token['access_token']}"}
        log.info("signer auth via direct Auth0 M2M (signer_url=%s)", signer_url)
    elif billing_url and api_key:
        from livepeer_gateway_client.signer_provider import SignerTokenProvider

        provider = SignerTokenProvider(
            billing_url=billing_url,
            api_key=api_key,
            client_id=client_id or None,
            m2m_client_id=m2m_client_id or None,
            external_user_id=external_user_id or None,
            oidc_base_url=oidc_url or None,
        )
        provider.refresh()
        signer_headers = dict(provider.headers)
        signer_url = provider.signer_url or signer_url
        if provider.discovery_url:
            discovery_url = provider.discovery_url
        log.info("signer auth via billing API exchange (signer_url=%s)", signer_url)
    elif oidc_url and billing_url and oidc_client_id:
        from livepeer_gateway_client.signer_provider import SignerTokenProvider

        provider = SignerTokenProvider(
            oidc_base_url=oidc_url,
            billing_url=billing_url,
            client_id=oidc_client_id,
            oidc_client_id=oidc_client_id,
            oidc_scopes=oidc_scopes,
            oidc_audience=oidc_audience,
            oidc_headless=True,
        )
        provider.refresh()
        signer_headers = dict(provider.headers)
        signer_url = provider.signer_url or signer_url
        if provider.discovery_url:
            discovery_url = provider.discovery_url
        log.info("signer auth via OIDC + billing exchange (signer_url=%s)", signer_url)
    elif signer_url and _env("LIVEPEER_SIGNER_TOKEN"):
        signer_headers = {"Authorization": f"Bearer {_env('LIVEPEER_SIGNER_TOKEN')}"}
        log.info("signer auth via LIVEPEER_SIGNER_TOKEN (signer_url=%s)", signer_url)
    elif signer_url:
        log.info("signer url only, no bearer headers (signer_url=%s)", signer_url)

    return SignerAuth(
        signer_url=signer_url,
        signer_headers=signer_headers,
        provider=provider,
        discovery_url=discovery_url,
    )


_AUTH: SignerAuth | None = None


def _get_auth() -> SignerAuth:
    global _AUTH
    if _AUTH is None:
        _AUTH = _resolve_signer_auth()
    return _AUTH


def _refresh_signer_auth() -> None:
    global _AUTH
    auth = _get_auth()
    if auth.provider is not None:
        auth.provider.refresh()
        auth.signer_url = auth.provider.signer_url or auth.signer_url
        auth.signer_headers = dict(auth.provider.headers)
        if auth.provider.discovery_url:
            auth.discovery_url = auth.provider.discovery_url


async def _call(payload: dict) -> dict:
    """Reserve a session, POST /run, return the runner's response data."""
    from livepeer_gateway_client.errors import is_signer_auth_error

    signer_url = _get_auth().signer_url
    signer_headers = _get_auth().signer_headers
    discovery_url = _get_auth().discovery_url

    for attempt in range(3):
        session = await reserve_session(
            discovery_url=discovery_url,
            app=APP_ID,
            signer_url=signer_url,
            signer_headers=signer_headers,
        )
        try:
            result = await call_runner(
                runner_url=session.app_url.rstrip("/") + "/run",
                payload=payload,
                signer_url=signer_url,
                signer_headers=signer_headers,
                timeout=600.0,
            )
            return result.data
        except LivepeerGatewayError as exc:
            if attempt < 2 and is_signer_auth_error(exc) and _get_auth().provider is not None:
                log.warning("signer token rejected; re-minting (attempt %d)", attempt + 1)
                _refresh_signer_auth()
                signer_url = _get_auth().signer_url
                signer_headers = _get_auth().signer_headers
                discovery_url = _get_auth().discovery_url
                continue
            raise
        finally:
            try:
                await stop_runner_session(session)
            except Exception:
                pass
    raise LivepeerGatewayError("exhausted signer auth retries")


async def _run_media(op: str, input_path: str, output_path: str, **params: object) -> str:
    """Run a media op and save the base64 output to output_path."""
    if not os.path.isfile(input_path):
        return f"error: input file not found: {input_path}"
    with open(input_path, "rb") as f:
        input_b64 = base64.b64encode(f.read()).decode()
    try:
        data = await _call({"op": op, "input_b64": input_b64, **params})
    except LivepeerGatewayError as exc:
        return f"error: {exc}"
    if "output_b64" not in data:
        return f"error: {data.get('error', data)}"
    with open(output_path, "wb") as f:
        f.write(base64.b64decode(data["output_b64"]))
    return f"wrote {output_path} ({data.get('bytes', 0)} bytes, {data.get('media_type', '?')})"


@mcp.tool()
async def ffmpeg_transcode(input_path: str, height: int = 0, quality: int = 23, output_path: str = "out.mp4") -> str:
    """Re-encode a video to H.264/mp4. height: target height in px (0 = keep the source resolution, e.g. keep 4K). quality: CRF/CQ, lower is higher quality (18 = high, 23 = default, 28 = small). Returns a status string with the output path."""
    params: dict = {"quality": quality}
    if height:
        params["height"] = height
    return await _run_media("transcode", input_path, output_path, **params)


@mcp.tool()
async def ffmpeg_clip(input_path: str, start: float, end: float, output_path: str = "clip_out.mp4") -> str:
    """Cut [start, end] seconds out of a video without re-encoding (fast). Returns a status string with the output path."""
    return await _run_media("clip", input_path, output_path, start=start, end=end)


@mcp.tool()
async def ffmpeg_thumbnail(input_path: str, at: float = 0.0, output_path: str = "thumb.jpg") -> str:
    """Extract a single JPEG frame from a video at a timestamp (seconds). Returns a status string with the output path."""
    return await _run_media("thumbnail", input_path, output_path, at=at)


@mcp.tool()
async def ffmpeg_extract_audio(input_path: str, output_path: str = "audio.m4a") -> str:
    """Extract the audio track from a video as AAC/.m4a (input must have audio). Returns a status string with the output path."""
    return await _run_media("extract_audio", input_path, output_path)


@mcp.tool()
async def ffmpeg_gif(input_path: str, fps: int = 12, height: int = 240, output_path: str = "out.gif") -> str:
    """Convert a (short) video to an animated GIF, palette-optimized. Returns a status string with the output path."""
    return await _run_media("gif", input_path, output_path, fps=fps, height=height)


@mcp.tool()
async def ffmpeg_crop(input_path: str, width: int, height: int, x: int = 0, y: int = 0, output_path: str = "crop.mp4") -> str:
    """Crop a video to width x height at offset (x, y), re-encoded H.264. Returns a status string with the output path."""
    return await _run_media("crop", input_path, output_path, width=width, height=height, x=x, y=y)


@mcp.tool()
async def ffmpeg_convert(input_path: str, format: str = "mp4", quality: int = 23, output_path: str = "") -> str:
    """Change a video's container format (mp4, mkv, or mov). mkv remuxes fast (keeps codecs); mp4/mov re-encode to H.264/AAC so any input (e.g. webm) works. quality: CRF/CQ for the re-encode, lower is higher quality. Returns a status string with the output path."""
    return await _run_media("convert", input_path, output_path or f"out.{format}", format=format, quality=quality)


@mcp.tool()
async def ffmpeg_probe(input_path: str) -> str:
    """Inspect a media file with ffprobe: returns JSON with format + stream info (codecs, resolution, duration, bitrate). Useful before deciding how to process it."""
    if not os.path.isfile(input_path):
        return f"error: input file not found: {input_path}"
    with open(input_path, "rb") as f:
        input_b64 = base64.b64encode(f.read()).decode()
    try:
        data = await _call({"op": "probe", "input_b64": input_b64})
    except LivepeerGatewayError as exc:
        return f"error: {exc}"
    if "analysis" not in data:
        return f"error: {data.get('error', data)}"
    return json.dumps(data["analysis"], indent=2)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    mcp.run()
