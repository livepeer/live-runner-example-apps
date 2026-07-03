#!/usr/bin/env python3
"""Minimal local OpenAI -> Livepeer gateway, multi-model edition.

Same idea as the vllm example's gateway, but one Ollama backend serves several
models. Each model is its own static runner app (see runners.json), so the
gateway maps the OpenAI `model` field to the matching app id, then reserves +
forwards as usual. Point ANY OpenAI client at it -- no code changes -- and it
works on-chain:

    uv run gateway.py --signer http://localhost:7936 &
    export OPENAI_BASE_URL=http://localhost:8080/v1 OPENAI_API_KEY=unused
    # then plain `openai`, curl, or any SDK, picking a model with `model=...`

Each request: reserve a session for the model's app, forward the body, release
the session. call_runner does the 402 payment challenge internally, so the
client never sees discovery or payment.
"""
from __future__ import annotations

import argparse
import logging
from contextlib import suppress

from aiohttp import web

from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session

log = logging.getLogger("ollama-gateway")

# LLM calls cold-load the model and generate on CPU/GPU; the SDK's 5s default is
# far too short. Give each forwarded request room (first call is the slowest).
REQUEST_TIMEOUT = 300.0


def _app_for_model(model: str) -> str:
    # One Ollama backend, several models; each model is registered as its own
    # static runner app `ollama/<model>` (see runners.json). The Ollama tag
    # `qwen2.5:0.5b` maps to the app id `ollama/qwen2.5-0.5b`.
    return ("ollama/" + model.replace(":", "-")).lower()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenAI-compatible gateway in front of Ollama Live Runners.")
    parser.add_argument("--discovery", default="https://localhost:8935/discovery")
    parser.add_argument("--signer", default="", help="Remote signer base URL; omit for the offchain (free) path.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    signer_url = args.signer.strip() or None

    async def _forward(request: web.Request) -> web.StreamResponse:
        payload = await request.json()
        model = payload.get("model") or ""
        if not model:
            return web.json_response({"error": "request must set `model`"}, status=400)
        runner_path = request.path  # e.g. /v1/chat/completions
        session = await reserve_session(
            discovery_url=args.discovery, app=_app_for_model(model), signer_url=signer_url
        )

        try:
            runner_url = session.app_url.rstrip("/") + runner_path

            # stream=True -> the runner replies with text/event-stream; pipe those
            # chunks straight through so tokens reach the client as they arrive.
            if payload.get("stream"):
                async with await call_runner(
                    runner_url=runner_url, payload=payload, signer_url=signer_url,
                    stream=True, timeout=REQUEST_TIMEOUT,
                ) as stream:
                    resp = web.StreamResponse(
                        status=stream.status,
                        headers={"Content-Type": stream.content_type or "text/event-stream"},
                    )
                    await resp.prepare(request)
                    async for chunk in stream.aiter_bytes():  # raw bytes -> preserve SSE framing
                        await resp.write(chunk)
                    await resp.write_eof()
                    return resp

            result = await call_runner(
                runner_url=runner_url, payload=payload, signer_url=signer_url, timeout=REQUEST_TIMEOUT
            )
            return web.json_response(result.data)
        finally:
            with suppress(Exception):
                await stop_runner_session(session)

    app = web.Application()
    app.router.add_post("/v1/{tail:.*}", _forward)  # forward every OpenAI path
    log.info("gateway on http://%s:%d/v1 -> %s (signer=%s)", args.host, args.port, args.discovery, signer_url or "none")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
