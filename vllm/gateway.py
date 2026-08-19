#!/usr/bin/env python3
"""Minimal local OpenAI -> Livepeer gateway.

A lightweight, single-app process you run on your host. Exposes a normal OpenAI endpoint
on localhost and forwards each request through the orchestrator to the vLLM runner,
doing Livepeer's discovery + payment handshake behind the scenes. Point ANY OpenAI
client at it -- no code changes, any language -- and it works on-chain:

    uv run gateway.py --signer http://localhost:7936 &
    export OPENAI_BASE_URL=http://localhost:18080/v1 OPENAI_API_KEY=unused
    # then plain `openai`, curl, or any SDK just works

Each request: discover the runner, forward the body. The runner is single-shot, so the
orchestrator reserves a session for the call and releases it when the response returns --
the gateway manages no session at all. call_runner does the 402 payment challenge
internally, so the client never sees discovery or payment. Pricing is metered, so the
call keeps paying for as long as it runs, which for a long generation is the point.

Livepeer integration (grep `# Livepeer:`):
  1. runner_selector()  — discover the runner advertising this app
  2. call_runner()      — forward the request through the orchestrator (pays 402)

These two calls are the *entire* Livepeer surface. They live here, and only here,
because an OpenAI client can't do discovery or settle payments itself — so `client.py`
(and any OpenAI SDK/curl) stays 100% stock, unaware of Livepeer.

Registration is static (no register_runner): the orchestrator reads `runners.json` via
-liveRunnerConfig and health-polls the runner. The vLLM container is a third-party image
with zero Livepeer code, which is exactly why it's static — there's no app to put a
register_runner in (contrast hello-world/echo). See compose.yml.
"""

from __future__ import annotations

import argparse
import logging
import os

from aiohttp import web

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner
from livepeer_gateway.selection import runner_selector

APP_ID = "vllm/qwen2.5-0.5b-instruct"
# Avoid :8080 — commonly used by PymtHouse / remote signer locally.
DEFAULT_GATEWAY_PORT = int(os.environ.get("GATEWAY_PORT", "18080"))
REQUEST_TIMEOUT = 300.0  # a generation outruns the SDK's 5s default

log = logging.getLogger("vllm-gateway")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenAI-compatible gateway in front of a vLLM Live Runner."
    )
    parser.add_argument("--discovery", default="https://localhost:8935/discovery")
    parser.add_argument(
        "--signer",
        default="",
        help="Remote signer base URL; omit for the offchain (free) path.",
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Bearer credential for the signer (Authorization header).",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_GATEWAY_PORT)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    signer_url = args.signer.strip() or None
    signer_headers = None
    if args.api_key.strip():
        signer_headers = {"Authorization": f"Bearer {args.api_key.strip()}"}

    async def _forward(request: web.Request) -> web.StreamResponse:
        # GET /v1/models carries no body; everything else posts JSON.
        payload = await request.json() if request.can_read_body else {}
        runner_path = request.path  # e.g. /v1/chat/completions
        cursor = await runner_selector(  # Livepeer: 1
            discovery_url=args.discovery,  # omit if the signer does discovery itself
            app=APP_ID,
        )
        runner = cursor.candidates[0]
        runner_url = runner.url.rstrip("/") + runner_path

        # When the OpenAI client asks for stream=True the runner replies with
        # text/event-stream; pipe those chunks straight through with stream=True
        # so tokens reach the client as they arrive instead of buffering the blob.
        if payload.get("stream"):
            async with await call_runner(  # Livepeer: 2 (streaming)
                runner=runner,  # discovery metadata tells call_runner the price unit
                runner_url=runner_url,
                payload=payload,
                signer_url=signer_url,
                signer_headers=signer_headers,
                method=request.method,
                timeout=REQUEST_TIMEOUT,
                stream=True,
            ) as stream:
                resp = web.StreamResponse(
                    status=stream.status,
                    headers={
                        "Content-Type": stream.content_type or "text/event-stream"
                    },
                )
                await resp.prepare(request)
                async for (
                    chunk
                ) in stream.aiter_bytes():  # raw bytes -> keep SSE framing
                    await resp.write(chunk)
                await resp.write_eof()
                return resp

        result = await call_runner(  # Livepeer: 2
            runner=runner,  # discovery metadata tells call_runner the price unit
            runner_url=runner_url,
            payload=payload,
            signer_url=signer_url,
            signer_headers=signer_headers,
            method=request.method,
            timeout=REQUEST_TIMEOUT,
        )
        return web.json_response(result.data)

    async def _forward_or_error(request: web.Request) -> web.StreamResponse:
        # SDK errors as JSON, not aiohttp's HTML 500: upstream status if it has one
        # (503 = busy runner), else 502.
        try:
            return await _forward(request)
        except LivepeerGatewayError as exc:
            return web.json_response(
                {"error": {"message": str(exc), "type": "livepeer_error"}},
                status=getattr(exc, "status_code", 502),
            )

    app = web.Application()
    # Every verb, not just POST: an OpenAI client lists models with GET /v1/models,
    # itself a billable single-shot call (a production gateway would cache it).
    app.router.add_route("*", "/v1/{tail:.*}", _forward_or_error)
    log.info(
        "gateway on http://%s:%d/v1 -> %s (signer=%s api_key=%s)",
        args.host,
        args.port,
        args.discovery,
        signer_url or "none",
        "set" if signer_headers else "none",
    )
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
