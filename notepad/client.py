#!/usr/bin/env python3
"""notepad client: reserve a session, write then read, settle up.

Proves persistent HTTP: two POSTs against the same reserved session share
process-local state. gateway-web `runInference` cannot do this — it reserves,
calls once, and stops.

Livepeer integration (grep `# Livepeer:`):
  1. reserve_session()      — discover the runner, reserve a session
  2. post_json(app_url/set) — write
  3. post_json(app_url/get) — read (same session)
  4. stop_runner_session()  — end the session
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import suppress

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.http import post_json
from livepeer_gateway.live_runner import stop_runner_session
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/notepad"

log = logging.getLogger("notepad-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Write then read a notepad Live Runner session."
    )
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument("--text", default="hello from a held session")
    parser.add_argument(
        "--signer", default="", help="Remote signer base URL (on-chain/paid path)."
    )
    return parser.parse_args()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    session = None
    try:
        session = await reserve_session(  # Livepeer: 1
            discovery_url=args.discovery,
            app=APP_ID,
            signer_url=args.signer.strip() or None,
        )
        log.info("session_id=%s app_url=%s", session.session_id, session.app_url)
        async with session:
            app_url = session.app_url.rstrip("/")
            written = await post_json(  # Livepeer: 2
                f"{app_url}/set",
                {"text": args.text},
            )
            read = await post_json(f"{app_url}/get", {})  # Livepeer: 3
        print({"set": written, "get": read})
        if read.get("text") != args.text:
            raise SystemExit(f"ERROR: session did not keep state: {read}")
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)  # Livepeer: 4


if __name__ == "__main__":
    asyncio.run(main())
