#!/usr/bin/env python3
"""hello-world client (single-shot): discover a runner, call the app, pay inline.

Livepeer integration (grep `# Livepeer:`):
  1. runner_selector()  — discover orchestrators advertising the app
  2. call_runner()      — call the app through the orchestrator; on the paid path
                          it answers the 402 challenge and pays inline. Single-shot
                          needs no reserve/stop: the orchestrator reserves the
                          session for this one request and releases it on return.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner
from livepeer_gateway.selection import runner_selector

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/hello-world"

log = logging.getLogger("hello-world-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the hello-world Live Runner demo (single-shot)."
    )
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument("--name", default="livepeer")
    parser.add_argument(
        "--signer", default="", help="Remote signer base URL (on-chain/paid path)."
    )
    return parser.parse_args()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    signer_url = args.signer.strip() or None
    try:
        cursor = await runner_selector(  # Livepeer: 1
            discovery_url=args.discovery, app=APP_ID
        )
        app_url = next((c.url for c in cursor.candidates), None)
        if app_url is None:
            raise LivepeerGatewayError(f"no runner discovered for app {APP_ID!r}")
        log.info("app_url=%s", app_url)

        result = await call_runner(  # Livepeer: 2 (single-shot: pays inline via the 402 challenge)
            runner_url=app_url.rstrip("/") + "/hello",
            payload={"name": args.name},
            signer_url=signer_url,
        )
        print(result.data)
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    asyncio.run(main())
