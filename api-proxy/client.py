#!/usr/bin/env python3
"""api-proxy client: discover a runner, send a prompt, get the image back.

The request body is the Hugging Face text-to-image payload as-is
({"inputs": "<prompt>"}); the runner forwards it verbatim and the image comes
back as raw JPEG bytes in `result.content`.

The operator offers two models, so `--app` picks which one to call. Discovery
matches app ids exactly, which is the whole reason each model has its own: it is
what a caller selects and pays for.

Livepeer integration (grep `# Livepeer:`):
  1. runner_selector() — discover orchestrators advertising the app
  2. call_runner()     — call the app through the orchestrator; a non-JSON response
                         arrives in `result.content`, and on the paid path the call
                         answers the 402 payment challenge inline (one fixed payment
                         per image). Single-shot needs no reserve/stop.
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
DEFAULT_APP = "livepeer-example/stable-diffusion-3-medium"
FLUX_APP = "livepeer-example/flux-1-schnell"  # the other one this demo offers
DEFAULT_OUTPUT = "api-proxy-out.jpg"

log = logging.getLogger("api-proxy-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the api-proxy Live Runner demo.")
    parser.add_argument(
        "--prompt", default="a watercolor painting of a llama writing code"
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="output image path")
    parser.add_argument(
        "--app",
        default=DEFAULT_APP,
        help=f"app id to call; this demo also offers {FLUX_APP}",
    )
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
            app=args.app,
        )
        runner = cursor.candidates[0]
        log.info("app=%s app_url=%s", args.app, runner.url)

        result = await call_runner(  # Livepeer: 2
            runner=runner,  # discovery metadata tells call_runner the price unit
            runner_url=runner.url.rstrip("/") + "/proxy",
            payload={"inputs": args.prompt},  # the HF payload, forwarded as-is
            signer_url=args.signer.strip() or None,
            timeout=180,  # a hosted diffusion model can take tens of seconds
        )
        image = result.content or b""  # the image comes back as raw bytes, not JSON

        out_path = Path(args.output).expanduser()
        out_path.write_bytes(image)
        log.info("wrote %s (%d bytes, %s)", out_path, len(image), result.content_type)
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    asyncio.run(main())
