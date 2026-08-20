#!/usr/bin/env python3
"""api-proxy client: discover a runner, send a prompt, get the image back.

The request body is the Hugging Face payload as-is ({"inputs": "<prompt>"} to
paint, {"inputs": "<base64 image>"} to classify); the runner forwards it
verbatim. The image comes back as raw JPEG bytes in `result.content`, the labels
as JSON.

The operator offers two models, so `--app` picks which one to call. Discovery
matches app ids exactly, which is the whole reason each model has its own: it is
what a caller selects and pays for. `--image` feeds a file to the classifier, so
the picture SD3 just painted is what ViT reads back.

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
import base64
import logging
from pathlib import Path

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner
from livepeer_gateway.selection import runner_selector

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
DEFAULT_APP = "livepeer-example/stable-diffusion-3-medium"
VIT_APP = "livepeer-example/vit-base-patch16-224"  # the other one this demo offers
DEFAULT_OUTPUT = "api-proxy-out.jpg"

log = logging.getLogger("api-proxy-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send a prompt to an api-proxy Live Runner and save the image."
    )
    parser.add_argument(
        "--prompt", default="a watercolor painting of a llama writing code"
    )
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="output image path")
    parser.add_argument(
        "--image",
        default="",
        help=f"image to classify instead of a prompt to paint (use with --app {VIT_APP})",
    )
    parser.add_argument(
        "--app",
        default=DEFAULT_APP,
        help=f"app id to call; this demo also offers {VIT_APP}",
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

        # Both capabilities take the same HF payload; only what goes in
        # `inputs` differs, a prompt to paint or an image to read.
        if args.image:
            source = Path(args.image).expanduser().read_bytes()
            inputs = base64.b64encode(source).decode()
        else:
            inputs = args.prompt

        result = await call_runner(  # Livepeer: 2
            runner=runner,  # discovery metadata tells call_runner the price unit
            runner_url=runner.url.rstrip("/") + "/proxy",
            payload={"inputs": inputs},  # the HF payload, forwarded as-is
            signer_url=args.signer.strip() or None,
            timeout=180,  # a hosted diffusion model can take tens of seconds
        )
        body = result.content or b""  # both answers arrive unparsed, not as JSON

        # A painter answers with an image, a classifier with labels and scores.
        if args.image:
            log.info("labels: %s", body.decode())
            return

        out_path = Path(args.output).expanduser()
        out_path.write_bytes(body)
        log.info("wrote %s (%d bytes, %s)", out_path, len(body), result.content_type)
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    asyncio.run(main())
