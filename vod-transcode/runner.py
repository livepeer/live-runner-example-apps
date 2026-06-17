#!/usr/bin/env python3
"""VOD transcoding by wrapping ffmpeg — a Live Runner app.

The simplest, fully-feasible transcoding shape: HTTP request/response, no live
media plane. The client POSTs a (small) video, the app runs ffmpeg to scale it
to a target height with H.264, and returns the result. It self-registers
(dynamic), like hello-world.

Wire protocol on POST /transcode:
  request : {"video_b64": "...", "height": 720}
  response: {"output_b64": "...", "bytes": N}

Video is base64'd into JSON so it rides the standard buffered call path (and its
payment). That's fine for short clips; production would pass URLs + object
storage instead of inlining bytes.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import subprocess
import tempfile
from contextlib import suppress

from aiohttp import web

from livepeer_gateway.live_runner import register_runner

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
APP_ID = "transcode/h264-720p"

log = logging.getLogger("vod-transcode")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="VOD ffmpeg transcoding Live Runner.")
    parser.add_argument("--orchestrator", default="http://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers).")
    parser.add_argument("--height", type=int, default=720, help="Output height; width keeps aspect.")
    parser.add_argument("--encoder", default="libx264", help="Video encoder (libx264 cpu, h264_nvenc gpu).")
    parser.add_argument("--price", type=int, default=0, help="Price in USD per pixels-per-unit (0 = free).")
    parser.add_argument("--pixels-per-unit", type=int, default=1, help="Scale factor for the price.")
    return parser.parse_args()


def _transcode(data: bytes, height: int, encoder: str) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in")
        dst = os.path.join(d, "out.mp4")
        with open(src, "wb") as f:
            f.write(data)
        cmd = [
            "ffmpeg", "-y", "-i", src,
            "-vf", f"scale=-2:{height}",
            "-c:v", encoder, "-preset", "veryfast",
            "-c:a", "aac",
            "-movflags", "+faststart",
            dst,
        ]
        subprocess.run(cmd, check=True, capture_output=True)
        with open(dst, "rb") as f:
            return f.read()


async def _handle_transcode(request: web.Request) -> web.Response:
    payload = await request.json()
    data = base64.b64decode(payload["video_b64"])
    height = int(payload.get("height", request.app["height"]))
    log.info("transcoding %d bytes -> %dp", len(data), height)
    try:
        out = await asyncio.to_thread(_transcode, data, height, request.app["encoder"])
    except subprocess.CalledProcessError as exc:
        msg = exc.stderr.decode(errors="replace")[-500:]
        return web.json_response({"error": f"ffmpeg failed: {msg}"}, status=400)
    return web.json_response({"output_b64": base64.b64encode(out).decode(), "bytes": len(out)})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()

    async def _on_startup(app: web.Application) -> None:
        app["registration"] = await register_runner(
            args.orchestrator,
            secret=args.orchSecret,
            runner_url=args.runner_url,
            app=APP_ID,
            price_per_unit=args.price,
            pixels_per_unit=args.pixels_per_unit,
        )
        log.info("registered runner_id=%s app=%s", app["registration"].runner_id, APP_ID)

    async def _on_cleanup(app: web.Application) -> None:
        with suppress(Exception):
            await app["registration"].close()

    app = web.Application(client_max_size=512 * 1024 * 1024)  # allow modest video uploads
    app["height"] = args.height
    app["encoder"] = args.encoder
    app.router.add_post("/transcode", _handle_transcode)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT, print=None)


if __name__ == "__main__":
    main()
