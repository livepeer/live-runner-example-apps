#!/usr/bin/env python3
"""notepad app: persistent HTTP with per-session state, no streaming transport.

A held-open session keeps one string in memory. POST /set writes it, POST /get
reads it. That is the gap the other examples leave: persistent + ordinary HTTP.

Livepeer integration (grep `# Livepeer:`):
  1. register_runner()     — announce the app (mode=persistent)
  2. registration.close()  — deregister (cleanup)

/set and /get are ordinary HTTP handlers. The orchestrator pins this process to
the reserved session, so process-local state *is* session state.
"""

from __future__ import annotations

import argparse
import logging
from contextlib import suppress

from aiohttp import web

from livepeer_gateway.live_runner import register_runner

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
APP_ID = "livepeer-example/notepad"

log = logging.getLogger("notepad")

_note = ""
_revision = 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Live Runner persistent-HTTP notepad demo."
    )
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
        help="Runner price in USD per hour (0 = free, the offchain default).",
    )
    return parser.parse_args()


async def _handle_set(request: web.Request) -> web.Response:
    global _note, _revision
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise web.HTTPBadRequest(text="body must be a JSON object")
    _note = str(payload.get("text", ""))
    _revision += 1
    return web.json_response({"text": _note, "revision": _revision})


async def _handle_get(_request: web.Request) -> web.Response:
    return web.json_response({"text": _note, "revision": _revision})


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
            mode="persistent",
            price=args.price,  # USD per hour, metered while the session is held
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
    app.router.add_post("/set", _handle_set)
    app.router.add_post("/get", _handle_get)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
