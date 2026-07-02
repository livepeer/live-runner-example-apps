#!/usr/bin/env python3
"""Local MCP server exposing the livepeer/ffmpeg capability to Claude as tools.

Each tool reserves a session on your orchestrator, sends a *local* file to the
`livepeer/ffmpeg` runner, and writes the result back to a local path. Batch ops
on a persistent runner (single-shot payment isn't wired yet, go-livepeer#3955).

Offchain by default; set LIVEPEER_SIGNER to pay an on-chain orchestrator (needs a
funded remote signer, like `client.py --signer`). Run via Claude Code; see
README-mcp.md. Paths resolve relative to where Claude Code launches the server.

  LIVEPEER_DISCOVERY  orchestrator discovery URL (default: https://localhost:8935/discovery)
  LIVEPEER_SIGNER     remote signer URL for the paid path (unset = offchain)
"""
from __future__ import annotations

import base64
import json
import os

try:  # long-standing public API
    from mcp.server.fastmcp import FastMCP
except ImportError:  # newer SDK rename (MCPServer == FastMCP)
    from mcp.server import MCPServer as FastMCP  # type: ignore

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session

DISCOVERY = os.environ.get("LIVEPEER_DISCOVERY", "https://localhost:8935/discovery")
SIGNER = os.environ.get("LIVEPEER_SIGNER", "").strip() or None  # set -> paid; unset -> offchain
APP_ID = "livepeer/ffmpeg"

mcp = FastMCP("livepeer-ffmpeg")


async def _call(payload: dict) -> dict:
    """Reserve a session, POST /run, return the runner's response data."""
    session = await reserve_session(discovery_url=DISCOVERY, app=APP_ID, signer_url=SIGNER)
    try:
        result = await call_runner(
            runner_url=session.app_url.rstrip("/") + "/run",
            payload=payload,
            signer_url=SIGNER,  # None = offchain; set LIVEPEER_SIGNER for the paid path
            timeout=600.0,
        )
        return result.data
    finally:
        try:
            await stop_runner_session(session)
        except Exception:
            pass


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
    mcp.run()
