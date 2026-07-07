#!/usr/bin/env python3
"""hello-world client: reserve an orchestrator session, call the app, settle up.

Livepeer integration (grep `# Livepeer:`):
  1. reserve_session()      — discover orchestrators advertising the app, reserve one
                             (to be removed once #4 lands)
  2. call_runner()          — invoke the app through the orchestrator
  3. stop_runner_session()  — end the session (settles payment on-chain)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import suppress

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "http://localhost:8935/discovery"
APP_ID = "livepeer-example/hello-world"

log = logging.getLogger("hello-world-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the hello-world Live Runner demo."
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
    session = None
    try:
        session = await reserve_session(  # Livepeer: 1 (to be removed once #4 lands)
            discovery_url=args.discovery,
            app=APP_ID,
            signer_url=signer_url,
        )
        log.info("session_id=%s app_url=%s", session.session_id, session.app_url)

        result = await call_runner(  # Livepeer: 2
            runner_url=session.app_url.rstrip("/") + "/hello",
            payload={"name": args.name},
            signer_url=signer_url,
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
