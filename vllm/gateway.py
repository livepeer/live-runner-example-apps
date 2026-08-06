#!/usr/bin/env python3
"""Minimal local OpenAI -> Livepeer gateway.

A lightweight, single-app process you run on your host. Exposes a normal OpenAI endpoint
on localhost and forwards each request through the orchestrator to the vLLM runner,
doing Livepeer's discovery + payment handshake behind the scenes. Point ANY OpenAI
client at it -- no code changes, any language -- and it works on-chain:

    uv run gateway.py --signer http://localhost:7936 &
    export OPENAI_BASE_URL=http://localhost:8080/v1 OPENAI_API_KEY=unused
    # then plain `openai`, curl, or any SDK just works

Each request: reserve a session, forward the body, release the session. call_runner does
the 402 payment challenge internally, so the client never sees discovery or payment.
(Release matters: the runner has capacity 1, so an unreleased session would block the
next call.)

Livepeer integration (grep `# Livepeer:`):
  1. reserve_session()      — discover the runner, reserve a session
  2. call_runner()          — forward the request through the orchestrator (pays 402)
  3. stop_runner_session()  — release the session

These three calls are the *entire* Livepeer surface. They live here, and only here,
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
from contextlib import suppress

from aiohttp import web

from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session

APP_ID = "vllm/qwen2.5-0.5b-instruct"

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
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    signer_url = args.signer.strip() or None

    async def _forward(request: web.Request) -> web.StreamResponse:
        payload = await request.json()
        runner_path = request.path  # e.g. /v1/chat/completions
        session = await reserve_session(
            discovery_url=args.discovery,  # omit if the signer does discovery itself
            app=APP_ID,
            signer_url=signer_url,
        )  # Livepeer: 1

        try:
            runner_url = session.app_url.rstrip("/") + runner_path

            # When the OpenAI client asks for stream=True the runner replies with
            # text/event-stream; pipe those chunks straight through with stream=True
            # so tokens reach the client as they arrive instead of buffering the blob.
            if payload.get("stream"):
                async with await call_runner(  # Livepeer: 2 (streaming)
                    runner_url=runner_url,
                    payload=payload,
                    signer_url=signer_url,
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
                    ) in stream.aiter_bytes():  # raw bytes -> preserve SSE framing
                        await resp.write(chunk)
                    await resp.write_eof()
                    return resp

            result = await call_runner(
                runner_url=runner_url, payload=payload, signer_url=signer_url
            )  # Livepeer: 2
            return web.json_response(result.data)
        finally:
            with suppress(Exception):
                await stop_runner_session(session)  # Livepeer: 3

    app = web.Application()
    app.router.add_post("/v1/{tail:.*}", _forward)  # forward every OpenAI path
    log.info(
        "gateway on http://%s:%d/v1 -> %s (signer=%s)",
        args.host,
        args.port,
        args.discovery,
        signer_url or "none",
    )
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
