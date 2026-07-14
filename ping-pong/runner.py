#!/usr/bin/env python3
"""ping-pong app: a single-shot WebSocket demo on the Livepeer network.

The runner exposes `GET /ws`. Each `{"ping": <ts>}` message gets a
`{"pong": <ts>, "delta_ms": ...}` reply. It registers **single-shot**: the
WebSocket connection is the one held-open workload, so the orchestrator reserves
a session on connect and releases it on close.

Livepeer integration (grep `# Livepeer:`):
  1. register_runner()     — announce the app to the orchestrator (single-shot)
  2. registration.close()  — deregister (cleanup)
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from contextlib import suppress

from aiohttp import web

from livepeer_gateway.live_runner import LiveRunnerRegistration, register_runner

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8991
APP_ID = "livepeer-example/ping-pong"

log = logging.getLogger("ping-pong")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live Runner WebSocket ping/pong demo (single-shot)."
    )
    parser.add_argument("--orchestrator", default="https://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--price", type=int, default=0)
    parser.add_argument("--pixels-per-unit", type=int, default=1)
    return parser.parse_args()


def _pong_response(payload: str) -> dict[str, float]:
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("message must be a JSON object")
    ping = data.get("ping")
    if isinstance(ping, bool) or not isinstance(ping, (int, float)):
        raise ValueError("message must include a numeric ping")
    return {
        "pong": float(ping),
        "delta_ms": max(0.0, (time.time() - float(ping)) * 1000.0),
    }


async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    log.info("websocket session opened")
    try:
        async for msg in ws:
            if msg.type != web.WSMsgType.TEXT:
                continue
            try:
                await ws.send_json(_pong_response(msg.data))
            except ValueError as exc:
                await ws.send_json({"error": str(exc)})
    finally:
        # WebSocket closed: the single-shot workload is over.
        log.info("websocket session closed")
    return ws


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
            # the WebSocket connection is one held-open workload: single-shot
            mode="single-shot",
            price_per_unit=args.price,
            pixels_per_unit=args.pixels_per_unit,
        )
        log.info(
            "registered runner_id=%s orchestrator=%s",
            app["registration"].runner_id,
            app["registration"].orchestrator_url,
        )

    async def _on_cleanup(app: web.Application) -> None:
        registration = app.get("registration")
        if isinstance(registration, LiveRunnerRegistration):
            with suppress(Exception):
                await registration.close()  # Livepeer: 2

    app = web.Application()
    app.router.add_get("/ws", _handle_ws)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
