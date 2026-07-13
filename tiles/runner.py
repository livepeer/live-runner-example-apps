#!/usr/bin/env python3
"""tiles app: a CPU-bound image processor, made callable on the Livepeer network.

Each POST /tile stylizes one image tile (a deliberately CPU-heavy transform). The
client splits an image into a grid and fans out one session per tile, so `capacity`
— the number of sessions the orchestrator routes here at once — decides how many
tiles process in parallel. See the README for the capacity demo.

Livepeer integration (grep `# Livepeer:`):
  1. register_runner()     — announce the app and its capacity (startup)
  2. registration.close()  — deregister (cleanup)

/tile is an ordinary HTTP handler; its CPU work runs in a thread (parallel tiles).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
from contextlib import suppress

import cv2
import numpy as np
from aiohttp import web

from livepeer_gateway.live_runner import register_runner

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
APP_ID = "livepeer-example/tiles"
# Tiles are base64 PNGs in JSON; a photographic tile can exceed aiohttp's 1 MB default.
MAX_REQUEST_BYTES = 32 * 1024 * 1024

log = logging.getLogger("tiles")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live Runner tiles app demo (capacity showcase)."
    )
    parser.add_argument("--orchestrator", default="https://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers)."
    )
    parser.add_argument(
        "--capacity",
        type=int,
        default=4,
        help="Max concurrent sessions the orchestrator routes here (capacity knob).",
    )
    parser.add_argument(
        "--work",
        type=int,
        default=4,
        help=(
            "Supersample factor: stylize at (work x width) x (work x height). "
            "Higher = heavier CPU per tile (raise if tiles finish too fast to "
            "see the capacity effect)."
        ),
    )
    parser.add_argument(
        "--price",
        type=int,
        default=0,
        help="Price in USD per pixels-per-unit (0 = free, the offchain default).",
    )
    parser.add_argument(
        "--pixels-per-unit",
        type=int,
        default=1,
        help="Scale factor: price is charged per this many units.",
    )
    return parser.parse_args()


def _stylize(img: np.ndarray, work: int) -> np.ndarray:
    # Supersample at `work`x, stylize, then downsample. Cost scales ~work^2, so `work`
    # is the per-tile load knob that makes the fan-out a visible win — and stays crisp.
    h, w = img.shape[:2]
    scale = max(1, work)
    big = cv2.resize(img, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
    try:
        styled = cv2.stylization(big, sigma_s=60, sigma_r=0.45)
    except cv2.error:  # photo module unavailable: cartoon-ish bilateral, also heavy
        styled = cv2.bilateralFilter(big, 9, 75, 75)
    return cv2.resize(styled, (w, h), interpolation=cv2.INTER_AREA)


def _process(png: bytes, work: int) -> bytes:
    img = cv2.imdecode(np.frombuffer(png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("could not decode tile PNG")
    ok, out = cv2.imencode(".png", _stylize(img, work))
    if not ok:
        raise ValueError("could not encode tile PNG")
    return out.tobytes()


async def _handle_tile(request: web.Request) -> web.Response:
    payload = await request.json()
    b64 = payload.get("tile")
    if not isinstance(b64, str) or not b64:
        raise web.HTTPBadRequest(text="missing 'tile' (base64 PNG)")
    # Offload the CPU-bound work to a thread so `capacity` concurrent tiles run in
    # parallel (cv2 releases the GIL); on the event loop they would serialize.
    loop = asyncio.get_running_loop()
    out = await loop.run_in_executor(
        None, _process, base64.b64decode(b64), request.app["work"]
    )
    return web.json_response({"tile": base64.b64encode(out).decode()})


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()

    async def _on_startup(app: web.Application) -> None:
        app["registration"] = await register_runner(  # Livepeer: 1
            args.orchestrator,
            secret=args.orchSecret,
            runner_url=args.runner_url,
            app=APP_ID,
            # single-shot by nature; stays persistent until single-shot payment lands
            # (go-livepeer#3955)
            mode="persistent",
            capacity=args.capacity,  # the knob this example showcases
            price_per_unit=args.price,
            pixels_per_unit=args.pixels_per_unit,
        )
        log.info(
            "registered runner_id=%s capacity=%d orchestrator=%s",
            app["registration"].runner_id,
            args.capacity,
            app["registration"].orchestrator_url,
        )

    async def _on_cleanup(app: web.Application) -> None:
        with suppress(Exception):
            await app["registration"].close()  # Livepeer: 2

    app = web.Application(client_max_size=MAX_REQUEST_BYTES)
    app["work"] = args.work
    app.router.add_post("/tile", _handle_tile)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
