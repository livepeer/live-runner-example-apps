#!/usr/bin/env python3
"""hello-world app: a normal aiohttp service, made callable on the Livepeer network.

Livepeer integration (grep `# Livepeer:`):
  1. register_runner()     — announce the app to the orchestrator (startup)
  2. registration.close()  — deregister (cleanup)

/hello is an ordinary HTTP handler; being on the network doesn't change how you write
it.
"""

from __future__ import annotations

import argparse
import logging
from contextlib import suppress

from aiohttp import web

from livepeer_gateway.live_runner import register_runner

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
APP_ID = "livepeer-example/hello-world"

log = logging.getLogger("hello-world")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Runner hello-world demo.")
    parser.add_argument("--orchestrator", default="https://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers)."
    )
    parser.add_argument(
        "--price",
        type=float,
        default=0,
        help="Runner price in USD per call (0 = free, the offchain default).",
    )
    return parser.parse_args()


async def _handle_hello(request: web.Request) -> web.Response:
    payload = await request.json()
    name = str(payload.get("name", "world"))
    return web.json_response({"message": f"Hello, {name}!"})


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
            # one request, one response: the orchestrator reserves a session per
            # call and releases it when the response returns (go-livepeer#4000)
            mode="single-shot",
            price=args.price,  # USD per call
            # Fixed pricing: one payment per call, not per-second metering; the
            # work is bounded, so the billing model is part of the app.
            unit="fixed",
        )
        log.info(
            "registered runner_id=%s orchestrator=%s",
            app["registration"].runner_id,
            app["registration"].orchestrator_url,
        )

    async def _on_cleanup(app: web.Application) -> None:
        with suppress(Exception):
            await app["registration"].close()  # Livepeer: 2

    app = web.Application()
    app.router.add_post("/hello", _handle_hello)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
