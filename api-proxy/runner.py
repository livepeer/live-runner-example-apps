#!/usr/bin/env python3
"""api-proxy app: forward requests to an upstream HTTP API, made callable on Livepeer.

Wraps any existing REST API so it can be reached, and paid for, through the
Livepeer network. `POST /proxy` takes a JSON envelope describing the upstream
call ({"method", "path", "headers", "json"}) and returns the upstream response
({"status", "headers", "body"} for text, {"status", "headers", "body_b64"} for
binary). The upstream credential stays here, server-side: set UPSTREAM_TOKEN and
the app injects it as a Bearer token on every forward — callers pay Livepeer per
call and never see an API key.

Livepeer integration (grep `# Livepeer:`):
  1. register_runner()     — announce the app to the orchestrator (startup)
  2. registration.close()  — deregister (cleanup)
"""

from __future__ import annotations

import argparse
import base64
import logging
import os
from contextlib import suppress

import aiohttp
from aiohttp import web

from livepeer_gateway.live_runner import register_runner

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
DEFAULT_UPSTREAM = "https://router.huggingface.co"
APP_ID = "livepeer-example/api-proxy"
UPSTREAM_TIMEOUT = 120  # a hosted diffusion model can take tens of seconds
_TEXT_TYPES = ("text/", "application/json", "application/xml", "application/x-ndjson")

log = logging.getLogger("api-proxy")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Runner api-proxy demo.")
    parser.add_argument("--orchestrator", default="https://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers)."
    )
    parser.add_argument(
        "--upstream",
        default=DEFAULT_UPSTREAM,
        help="Base URL of the API to proxy requests to.",
    )
    parser.add_argument(
        "--price",
        type=float,
        default=0,
        help="Runner price in USD per call (0 = free, the offchain default).",
    )
    return parser.parse_args()


async def _handle_proxy(request: web.Request) -> web.Response:
    payload = await request.json()
    method = str(payload.get("method", "GET")).upper()
    path = str(payload.get("path", "/"))
    body = payload.get("json")

    # The operator's credential, not the caller's: drop any Authorization the
    # caller sent and inject the stored token instead.
    headers = {
        k: v
        for k, v in (payload.get("headers") or {}).items()
        if k.lower() != "authorization"
    }
    token = request.app["token"]
    if token:
        headers["Authorization"] = f"Bearer {token}"

    upstream = request.app["upstream"].rstrip("/") + "/" + path.lstrip("/")
    log.info("proxy %s %s", method, upstream)

    session: aiohttp.ClientSession = request.app["session"]
    try:
        async with session.request(
            method,
            upstream,
            headers=headers,
            json=body,
            timeout=aiohttp.ClientTimeout(total=UPSTREAM_TIMEOUT),
        ) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = await resp.read()
    except aiohttp.ClientError as exc:
        return web.json_response({"error": str(exc)}, status=502)

    # Text-ish upstream bodies pass through as a string; anything else (e.g. a
    # generated image) is base64 so the envelope stays plain JSON.
    result: dict[str, object] = {"status": resp.status, "headers": dict(resp.headers)}
    if content_type.startswith(_TEXT_TYPES) or "+json" in content_type:
        result["body"] = raw.decode(errors="replace")
    else:
        result["body_b64"] = base64.b64encode(raw).decode()
    return web.json_response(result)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()

    async def _on_startup(app: web.Application) -> None:
        app["session"] = aiohttp.ClientSession()
        app["registration"] = await register_runner(  # Livepeer: 1
            args.orchestrator,
            secret=args.orchSecret,
            runner_url=args.runner_url,
            app=APP_ID,
            mode="single-shot",
            price=args.price,  # USD per call
            # one flat payment per call instead of per-second metering
            unit="fixed",
        )
        log.info(
            "registered runner_id=%s orchestrator=%s upstream=%s",
            app["registration"].runner_id,
            app["registration"].orchestrator_url,
            app["upstream"],
        )

    async def _on_cleanup(app: web.Application) -> None:
        with suppress(Exception):
            await app["registration"].close()  # Livepeer: 2
        with suppress(Exception):
            await app["session"].close()

    app = web.Application()
    app["upstream"] = args.upstream
    app["token"] = os.environ.get("UPSTREAM_TOKEN", "")
    app.router.add_post("/proxy", _handle_proxy)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
