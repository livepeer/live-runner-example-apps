#!/usr/bin/env python3
"""hello-world client: discover a runner, call the app, pay inline.

Livepeer integration (grep `# Livepeer:`):
  1. runner_selector()  — discover orchestrators advertising the app
  2. call_runner()      — call the app through the orchestrator; on the paid path it
                          answers the 402 payment challenge inline.

Single-shot runners (`…/app`) take the request directly. Persistent runners
(`…/session`) need reserve → call → stop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import suppress
from typing import Any

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.http import get_json
from livepeer_gateway.live_runner import (
    LiveRunnerSession,
    call_runner,
    stop_runner_session,
)
from livepeer_gateway.selection import runner_selector
from livepeer_gateway.token import parse_token

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/hello-world"

log = logging.getLogger("hello-world-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the hello-world Live Runner demo."
    )
    parser.add_argument(
        "--discovery",
        default=None,
        help=f"Discovery URL (default: {DEFAULT_DISCOVERY}, or token discovery).",
    )
    parser.add_argument("--name", default="livepeer")
    parser.add_argument(
        "--signer", default="", help="Remote signer base URL (on-chain/paid path)."
    )
    parser.add_argument(
        "--api-key",
        default="",
        help="Bearer credential for the signer (Authorization header).",
    )
    parser.add_argument(
        "--token",
        default="",
        help="PymtHouse / python-gateway base64 token (signer + headers + discovery).",
    )
    return parser.parse_args()


def _resolve_auth(
    args: argparse.Namespace,
) -> tuple[str, str | None, dict[str, str] | None, dict[str, str] | None]:
    signer_url = args.signer.strip() or None
    signer_headers: dict[str, str] | None = None
    discovery_headers: dict[str, str] | None = None
    discovery_url = args.discovery

    raw = args.token.strip()
    if raw:
        token: dict[str, Any] = parse_token(raw)
        if token.get("signer") is not None:
            signer_url = token["signer"]
        if token.get("signer_headers") is not None:
            signer_headers = dict(token["signer_headers"])
        if token.get("discovery_headers") is not None:
            discovery_headers = dict(token["discovery_headers"])
        if discovery_url is None and token.get("discovery") is not None:
            discovery_url = token["discovery"]
    elif args.api_key.strip():
        signer_headers = {"Authorization": f"Bearer {args.api_key.strip()}"}

    return discovery_url or DEFAULT_DISCOVERY, signer_url, signer_headers, discovery_headers


def _raw_discovery_orchestrators(data: list[Any], *, app: str) -> list[str]:
    """Extract orch addresses from discovery-service `/v1/discovery/raw`."""
    orchs: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        caps = item.get("capabilities")
        if not isinstance(address, str) or not address.strip():
            continue
        if not isinstance(caps, list) or app not in caps:
            continue
        orchs.append(address.strip())
    return orchs


async def _select_runner(
    *,
    discovery_url: str,
    signer_url: str | None,
    signer_headers: dict[str, str] | None,
    discovery_headers: dict[str, str] | None,
):
    data = await get_json(discovery_url, headers=discovery_headers)
    if isinstance(data, list) and data and isinstance(data[0], dict) and "capabilities" in data[0]:
        orchs = _raw_discovery_orchestrators(data, app=APP_ID)
        if not orchs:
            raise LivepeerGatewayError(
                f"no orchestrators advertise app {APP_ID!r} in {discovery_url}"
            )
        return await runner_selector(  # Livepeer: 1
            orchestrators=orchs,
            app=APP_ID,
            signer_url=signer_url,
            signer_headers=signer_headers,
        )

    return await runner_selector(  # Livepeer: 1
        discovery_url=discovery_url,
        discovery_headers=discovery_headers,
        app=APP_ID,
        signer_url=signer_url,
        signer_headers=signer_headers,
    )


def _is_single_shot(runner) -> bool:
    if runner.mode == "single-shot":
        return True
    return runner.url.rstrip("/").endswith("/app")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    discovery_url, signer_url, signer_headers, discovery_headers = _resolve_auth(args)
    session: LiveRunnerSession | None = None
    try:
        cursor = await _select_runner(
            discovery_url=discovery_url,
            signer_url=signer_url,
            signer_headers=signer_headers,
            discovery_headers=discovery_headers,
        )
        runner = cursor.candidates[0]
        log.info("runner_url=%s mode=%s", runner.url, runner.mode or "?")

        if _is_single_shot(runner):
            result = await call_runner(  # Livepeer: 2
                runner=runner,
                runner_url=runner.url.rstrip("/") + "/hello",
                payload={"name": args.name},
                signer_url=signer_url,
                signer_headers=signer_headers,
            )
        else:
            reserved = await call_runner(  # Livepeer: 2a reserve
                runner=runner,
                runner_url=runner.url,
                signer_url=signer_url,
                signer_headers=signer_headers,
            )
            app_url = reserved.data.get("app_url")
            session_id = reserved.session_id or reserved.data.get("session_id")
            if not isinstance(app_url, str) or not app_url.strip():
                raise LivepeerGatewayError("reserve response missing app_url")
            if not isinstance(session_id, str) or not session_id.strip():
                raise LivepeerGatewayError("reserve response missing session_id")
            session = LiveRunnerSession(
                session_id=session_id.strip(),
                app_url=app_url.strip(),
                runner_url=runner.url,
                runner=runner,
                payment_session=reserved.payment_session,
            )
            log.info("session_id=%s app_url=%s", session.session_id, session.app_url)
            result = await call_runner(  # Livepeer: 2b call
                runner=runner,
                runner_url=session.app_url.rstrip("/") + "/hello",
                payload={"name": args.name},
                signer_url=signer_url,
                signer_headers=signer_headers,
            )
        print(result.data)
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)  # Livepeer: 3


if __name__ == "__main__":
    asyncio.run(main())
