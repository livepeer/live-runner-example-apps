#!/usr/bin/env python3
"""Demo OpenAI -> Livepeer gateway WITH per-user bearer auth.

Same as the vLLM example gateway.py, plus one change: it forwards the caller's
`Authorization: Bearer sk_...` header to the remote signer. The signer's
`-remoteSignerWebhookUrl` (the clearinghouse identity-webhook) resolves that
token to an `auth_id` and tags every signed ticket with it, so OpenMeter meters
usage per user. Manual bearer tokens: add keys in the identity-webhook's
DEMO_API_KEYS map; each maps to a {clientId,userId} == auth_id.

Run:
    uv run gateway_authz.py --signer http://localhost:7936 &
    export OPENAI_BASE_URL=http://localhost:8080/v1
    # client sends OPENAI_API_KEY=sk_live_demo -> forwarded to signer -> auth_id
"""
from __future__ import annotations

import argparse
import logging
from contextlib import suppress

from aiohttp import web

from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session

APP_ID = "vllm/qwen2.5-0.5b-instruct"

log = logging.getLogger("vllm-gateway-authz")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OpenAI gateway with per-user bearer auth in front of a vLLM Live Runner.")
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
        runner_path = request.path  # e.g. /v1/chat/completions

        # --- the only change vs. the stock gateway ---
        # Forward the caller's OpenAI api_key (sent as `Authorization: Bearer sk_...`)
        # to the signer. The signer's identity webhook resolves it to auth_id and
        # tags the payment ticket, so usage meters per user. No key -> 401.
        user_auth = request.headers.get("Authorization")
        if signer_url and not user_auth:
            return web.json_response(
                {"error": {"type": "invalid_request_error", "code": "missing_api_key",
                           "message": "Missing Authorization: Bearer <api_key>."}},
                status=401,
            )
        signer_headers = {"Authorization": user_auth} if user_auth else None
        # ---------------------------------------------

        session = await reserve_session(
            discovery_url=args.discovery, app=APP_ID,
            signer_url=signer_url, signer_headers=signer_headers,
        )

        try:
            runner_url = session.app_url.rstrip("/") + runner_path

            if payload.get("stream"):
                async with await call_runner(
                    runner_url=runner_url, payload=payload,
                    signer_url=signer_url, signer_headers=signer_headers, stream=True,
                ) as stream:
                    resp = web.StreamResponse(
                        status=stream.status,
                        headers={"Content-Type": stream.content_type or "text/event-stream"},
                    )
                    await resp.prepare(request)
                    async for chunk in stream.aiter_bytes():
                        await resp.write(chunk)
                    await resp.write_eof()
                    return resp

            result = await call_runner(
                runner_url=runner_url, payload=payload,
                signer_url=signer_url, signer_headers=signer_headers,
            )
            return web.json_response(result.data)
        finally:
            with suppress(Exception):
                await stop_runner_session(session)

    app = web.Application()
    app.router.add_post("/v1/{tail:.*}", _forward)
    log.info("authz gateway on http://%s:%d/v1 -> %s (signer=%s)", args.host, args.port, args.discovery, signer_url or "none")
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
