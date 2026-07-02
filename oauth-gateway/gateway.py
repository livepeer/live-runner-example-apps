#!/usr/bin/env python3
"""Multi-tenant OpenAI/Ollama gateway with clearinghouse OAuth-key exchange.

The hosted sibling of John's ffmpeg MCP server. A developer signs in once (OAuth
via the clearinghouse builder-api), gets a durable `pmth_*` API key, and pastes
it into a STOCK OpenAI or Ollama client as the api_key. This gateway, per
request:

  1. reads the caller's key from `Authorization: Bearer pmth_...`
  2. exchanges it at the builder-api for a short-lived signer-session JWT
     (this is where the $5 trial is granted and a 402 is returned when the
     balance is exhausted)
  3. reserves + pays a Livepeer session with that JWT and proxies the request
     to the runner (vLLM or Ollama, both OpenAI-native).

Same interface as offchain: `OpenAI(base_url=..., api_key="pmth_...")`. The key
is per-user, so one hosted gateway serves everyone; usage meters per user via
the auth_id the signer stamps.

Env:
  LIVEPEER_BILLING_URL   builder-api base, e.g. http://localhost:8095
  LIVEPEER_CLIENT_ID     public app client id (tenant) for the key exchange
  LIVEPEER_DISCOVERY     fallback discovery URL if the exchange omits one
  LIVEPEER_SIGNER        fallback signer URL if the exchange omits one
  GATEWAY_APP            single app id to front (default vllm/qwen2.5-0.5b-instruct)
  GATEWAY_MODEL_MAP      optional JSON {"<openai model>": "<app id>"} for Ollama-style
                         multi-model routing; overrides GATEWAY_APP when the
                         requested model matches.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from contextlib import suppress

from aiohttp import web

from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session
from livepeer_gateway_client.signer_provider import SignerTokenProvider

try:
    from livepeer_gateway_client.errors import is_signer_auth_error
except Exception:  # pragma: no cover - older client lib
    def is_signer_auth_error(_exc: object) -> bool:
        return False

log = logging.getLogger("oauth-gateway")

BILLING_URL = os.environ.get("LIVEPEER_BILLING_URL", "").strip()
CLIENT_ID = os.environ.get("LIVEPEER_CLIENT_ID", "").strip() or None
# FORCE wins over whatever the exchange returns — set it to pin local runners
# (the builder-api exchange may hand back a hosted-network discovery URL).
FORCE_DISCOVERY = os.environ.get("GATEWAY_FORCE_DISCOVERY", "").strip() or None
FALLBACK_DISCOVERY = os.environ.get("LIVEPEER_DISCOVERY", "https://localhost:8935/discovery").strip()
FALLBACK_SIGNER = os.environ.get("LIVEPEER_SIGNER", "").strip() or None
DEFAULT_APP = os.environ.get("GATEWAY_APP", "vllm/qwen2.5-0.5b-instruct").strip()
MODEL_MAP: dict[str, str] = json.loads(os.environ.get("GATEWAY_MODEL_MAP", "{}") or "{}")


def _bearer(request: web.Request) -> str | None:
    value = request.headers.get("Authorization", "")
    if value.lower().startswith("bearer "):
        return value[7:].strip() or None
    return None


def _app_for(payload: dict) -> str:
    """OpenAI `model` -> Livepeer app id (Ollama-style multi-model), else default."""
    model = str(payload.get("model", "")).strip()
    return MODEL_MAP.get(model, DEFAULT_APP)


def _error(status: int, code: str, message: str) -> web.Response:
    return web.json_response({"error": {"type": "invalid_request_error", "code": code, "message": message}}, status=status)


async def _resolve(api_key: str):
    """Exchange the user's key for signer auth. Mirrors John's mcp_server flow.

    Returns (provider, signer_url, signer_headers, discovery_url). Raises on a
    402/insufficient-allowance from the builder-api, which we surface to the
    client unchanged.
    """
    provider = SignerTokenProvider(billing_url=BILLING_URL, api_key=api_key, client_id=CLIENT_ID)
    provider.refresh()
    signer_url = provider.signer_url or FALLBACK_SIGNER
    discovery_url = FORCE_DISCOVERY or getattr(provider, "discovery_url", None) or FALLBACK_DISCOVERY
    return provider, signer_url, dict(provider.headers), discovery_url


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Multi-tenant OpenAI/Ollama gateway with clearinghouse key exchange.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    if not BILLING_URL:
        raise SystemExit("LIVEPEER_BILLING_URL is required (builder-api base URL).")

    async def _forward(request: web.Request) -> web.StreamResponse:
        api_key = _bearer(request)
        if not api_key:
            return _error(401, "missing_api_key", "Missing Authorization: Bearer <api_key>.")

        payload = await request.json()
        runner_path = request.path  # e.g. /v1/chat/completions
        app_id = _app_for(payload)

        # Exchange the user's key for a signer session (grants trial / gates at 402).
        try:
            provider, signer_url, signer_headers, discovery_url = await _resolve(api_key)
        except Exception as exc:  # builder-api 402 / invalid key surface here
            msg = str(exc)
            if "402" in msg or "insufficient" in msg.lower():
                return web.json_response(
                    {"error": {"type": "insufficient_quota", "code": "credits_exhausted", "message": msg}},
                    status=402,
                )
            return _error(401, "exchange_failed", f"Key exchange failed: {msg}")

        session = await reserve_session(
            discovery_url=discovery_url, app=app_id, signer_url=signer_url, signer_headers=signer_headers
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
    log.info("oauth gateway on http://%s:%d/v1 (billing=%s, client_id=%s)", args.host, args.port, BILLING_URL, CLIENT_ID)
    web.run_app(app, host=args.host, port=args.port, print=None)


if __name__ == "__main__":
    main()
