#!/usr/bin/env python3
"""api-proxy client: discover a runner, send a prompt, stream the image back.

The request body is the Hugging Face text-to-image payload as-is
({"inputs": "<prompt>"}); the runner forwards it verbatim and the image comes
back as raw JPEG bytes, received with call_runner's streaming mode.

Livepeer integration (grep `# Livepeer:`):
  1. runner_selector()        — discover orchestrators advertising the app
  2. call_runner(stream=True) — call the app through the orchestrator and read the
                                raw response bytes; on the paid path it answers the
                                402 payment challenge inline (one fixed payment per
                                image). Single-shot needs no reserve/stop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner
from livepeer_gateway.selection import runner_selector

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/stable-diffusion-3-medium"
DEFAULT_OUTPUT = "api-proxy-out.jpg"

log = logging.getLogger("api-proxy-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the api-proxy Live Runner demo.")
    parser.add_argument(
        "--prompt", default="a watercolor painting of a llama writing code"
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="output image path")
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument(
        "--signer", default="", help="Remote signer base URL (on-chain/paid path)."
    )
    return parser.parse_args()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    try:
        cursor = await runner_selector(  # Livepeer: 1
            discovery_url=args.discovery,  # omit if the signer does discovery itself
            app=APP_ID,
        )
        runner = cursor.candidates[0]
        log.info("app_url=%s", runner.url)

        # NOTE: Streamed because the buffered path still assumes JSON; once
        # livepeer-python-gateway#51 lands it returns result.raw.
        stream = await call_runner(  # Livepeer: 2
            runner=runner,  # discovery metadata tells call_runner the price unit
            runner_url=runner.url.rstrip("/") + "/proxy",
            payload={"inputs": args.prompt},  # the HF payload, forwarded as-is
            signer_url=args.signer.strip() or None,
            stream=True,  # the image comes back as raw bytes, not JSON
        )
        async with stream:
            image = b"".join([chunk async for chunk in stream.aiter_bytes()])

        out_path = Path(args.output).expanduser()
        out_path.write_bytes(image)
        log.info("wrote %s (%d bytes, %s)", out_path, len(image), stream.content_type)
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    asyncio.run(main())
