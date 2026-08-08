#!/usr/bin/env python3
"""Minimal local OpenAI -> Livepeer gateway, multi-model edition.

Same idea as the vllm example's gateway, but one Ollama container serves several
models and each is its own Live Runner app. So the gateway does one extra thing:
it maps the OpenAI `model` field onto an app id, and answers `GET /v1/models` from
**discovery** rather than from any one container -- on a network, "which models are
available" is a network question.

    uv run gateway.py --signer http://localhost:7936 &
    export OPENAI_BASE_URL=http://localhost:8080/v1 OPENAI_API_KEY=unused
    # then plain `openai`, curl, or any SDK, picking a model with `model=...`

Livepeer integration (grep `# Livepeer:`):
  1. discover_runners()  -- list the models the network advertises (free: no session)
  2. runner_selector()   -- find runners for the chosen model's app
  3. call_runner()       -- forward the request through the orchestrator (pays 402)

Listing is free because /discovery is a plain GET. Only the forward reserves a
session, and because the runners are single-shot and metered, that call pays for as
long as the generation runs.
"""

from __future__ import annotations

import argparse
import json
import logging

from aiohttp import web

from livepeer_gateway.discovery import discover_runners
from livepeer_gateway.errors import LivepeerHTTPError
from livepeer_gateway.live_runner import call_runner
from livepeer_gateway.selection import runner_selector

APP_NAMESPACE = "ollama"
# A generation runs far longer than the SDK's 5s default, and a metered single-shot
# call is meant to pay for as long as it takes.
REQUEST_TIMEOUT = 300.0

log = logging.getLogger("ollama-gateway")


def _app_id(model: str) -> str:
    # Mirrors registrar.py: `llama3.2:1b` -> `ollama/llama3.2-1b`.
    return f"{APP_NAMESPACE}/{model.strip().lower().replace(':', '-')}"


def _model_name(runner: dict[str, object]) -> str:
    """The exact name to send Ollama, which the registrar put in metadata.

    The app id is a slug and cannot be reversed (`-` may have been `:`), so the
    registrar advertises the real name. This is what metadata is for: app-specific
    data the network doesn't model but a caller needs.
    """
    raw = runner.get("metadata")
    if isinstance(raw, str) and raw:
        try:
            name = json.loads(raw).get("model")
        except json.JSONDecodeError:
            name = None
        if isinstance(name, str) and name:
            return name
    # Fall back to the app id's suffix; wrong for names whose `-` was a `:`.
    app = runner.get("app")
    return str(app).split("/", 1)[-1] if isinstance(app, str) else ""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenAI-compatible gateway in front of Ollama Live Runners."
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

    async def _discovered_models() -> list[dict[str, object]]:
        # No app filter: discovery matches app ids exactly, with no prefix support,
        # so the family is selected here instead.
        entries = await discover_runners(discovery_url=args.discovery)  # Livepeer: 1
        seen: dict[str, dict[str, object]] = {}
        for entry in entries:
            for runner in entry.get("runners", []):
                app = runner.get("app")
                if isinstance(app, str) and app.startswith(f"{APP_NAMESPACE}/"):
                    seen.setdefault(_model_name(runner), runner)
        return [
            {"model": name, "runner": r} for name, r in sorted(seen.items()) if name
        ]

    async def _list_models(request: web.Request) -> web.StreamResponse:
        found = await _discovered_models()
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": item["model"],
                        "object": "model",
                        "owned_by": APP_NAMESPACE,
                    }
                    for item in found
                ],
            }
        )

    async def _forward(request: web.Request) -> web.StreamResponse:
        payload = await request.json() if request.can_read_body else {}
        runner_path = request.path  # e.g. /v1/chat/completions
        model = str(payload.get("model", "")).strip()
        if not model:
            raise web.HTTPBadRequest(text="request must name a model")

        cursor = await runner_selector(  # Livepeer: 2
            discovery_url=args.discovery,  # omit if the signer does discovery itself
            app=_app_id(model),
        )
        runner = cursor.candidates[0]
        runner_url = runner.url.rstrip("/") + runner_path
        # The app id is a slug; Ollama wants its own name back.
        payload["model"] = _model_name(runner.raw)

        # When the OpenAI client asks for stream=True the runner replies with
        # text/event-stream; pipe those chunks straight through so tokens reach the
        # client as they arrive instead of buffering the blob.
        if payload.get("stream"):
            async with await call_runner(  # Livepeer: 3 (streaming)
                runner=runner,  # discovery metadata tells call_runner the price unit
                runner_url=runner_url,
                payload=payload,
                signer_url=signer_url,
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

        result = await call_runner(  # Livepeer: 3
            runner=runner,  # discovery metadata tells call_runner the price unit
            runner_url=runner_url,
            payload=payload,
            signer_url=signer_url,
            timeout=REQUEST_TIMEOUT,
        )
        return web.json_response(result.data)

    async def _forward_or_error(request: web.Request) -> web.StreamResponse:
        # A single-shot call holds a capacity slot for its duration, so a busy runner
        # answers 503. Hand that back as JSON an OpenAI client can read.
        try:
            return await _forward(request)
        except LivepeerHTTPError as exc:
            return web.json_response(
                {"error": {"message": str(exc), "type": "livepeer_error"}},
                status=exc.status_code,
            )

    app = web.Application()
    # /v1/models is answered from discovery, so listing costs nothing. Everything
    # else is forwarded, and that is what reserves a session and pays.
    app.router.add_get("/v1/models", _list_models)
    app.router.add_route("*", "/v1/{tail:.*}", _forward_or_error)
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
