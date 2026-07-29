#!/usr/bin/env python3
"""api-proxy client: discover a runner, send a prompt, download the image.

The request body is the fal.ai text-to-image payload as-is
({"prompt": "<prompt>"}); the runner forwards it verbatim. fal responds with
JSON containing a CDN URL for the generated image, so this is an ordinary
call_runner call — the client then downloads the image from the URL.

Livepeer integration (grep `# Livepeer:`):
  1. runner_selector()  — discover orchestrators advertising the app
  2. call_runner()      — call the app through the orchestrator; on the paid path it
                          answers the 402 payment challenge inline (one fixed payment
                          per image). Single-shot needs no reserve/stop.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

import aiohttp

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner
from livepeer_gateway.selection import runner_selector

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/api-proxy"
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
            discovery_url=args.discovery, app=APP_ID
        )
        runner = cursor.candidates[0]
        log.info("app_url=%s", runner.url)

        result = await call_runner(  # Livepeer: 2
            runner=runner,  # discovery metadata tells call_runner the price unit
            runner_url=runner.url.rstrip("/") + "/proxy",
            payload={"prompt": args.prompt},  # the fal payload, forwarded as-is
            signer_url=args.signer.strip() or None,
            timeout=120.0,  # a hosted diffusion model can take tens of seconds
        )

        images = result.data.get("images") or []
        url = images[0].get("url") if images and isinstance(images[0], dict) else None
        if not url:
            raise LivepeerGatewayError(f"response missing image url: {result.data}")
        log.info("image_url=%s", url)

        # The generated image lives on fal's CDN; fetch it directly.
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                resp.raise_for_status()
                image = await resp.read()

        out_path = Path(args.output).expanduser()
        out_path.write_bytes(image)
        log.info("wrote %s (%d bytes)", out_path, len(image))
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    asyncio.run(main())
