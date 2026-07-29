#!/usr/bin/env python3
"""api-proxy client: discover a runner, proxy a text-to-image call, save the image.

Builds the generic /proxy envelope for one concrete upstream — the Hugging Face
text-to-image inference API — and decodes the binary response. Any other REST
API is the same envelope with a different method/path/json.

Livepeer integration (grep `# Livepeer:`):
  1. runner_selector()  — discover orchestrators advertising the app
  2. call_runner()      — call the app through the orchestrator; on the paid path it
                          answers the 402 payment challenge inline (one fixed payment
                          per call). Single-shot needs no reserve/stop: the
                          orchestrator reserves the session for this one request and
                          releases it when the response returns.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
from pathlib import Path

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner
from livepeer_gateway.selection import runner_selector

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/api-proxy"
DEFAULT_MODEL = "stabilityai/stable-diffusion-3-medium-diffusers"
DEFAULT_OUTPUT = "api-proxy-out.jpg"

log = logging.getLogger("api-proxy-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the api-proxy Live Runner demo.")
    parser.add_argument(
        "--prompt", default="a watercolor painting of a llama writing code"
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="Hugging Face text-to-image model (the upstream path).",
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

        # The envelope the app forwards upstream: here a Hugging Face
        # text-to-image call, but any method/path/json works.
        envelope = {
            "method": "POST",
            "path": f"/hf-inference/models/{args.model}",
            "json": {"inputs": args.prompt},
        }
        result = await call_runner(  # Livepeer: 2
            runner=runner,  # discovery metadata tells call_runner the price unit
            runner_url=runner.url.rstrip("/") + "/proxy",
            payload=envelope,
            signer_url=args.signer.strip() or None,
            timeout=120.0,  # a hosted diffusion model can take tens of seconds
        )

        status = result.data.get("status")
        if status != 200:
            body = result.data.get("body") or result.data.get("error")
            raise LivepeerGatewayError(f"upstream returned {status}: {body}")
        b64 = result.data.get("body_b64")
        if not isinstance(b64, str) or not b64:
            raise LivepeerGatewayError("upstream response was not binary (no body_b64)")
        out_path = Path(args.output).expanduser()
        out_path.write_bytes(base64.b64decode(b64))
        log.info("wrote %s", out_path)
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    asyncio.run(main())
