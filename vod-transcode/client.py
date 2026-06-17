#!/usr/bin/env python3
"""Send a video to the VOD transcoding runner and save the result.

Mirrors hello-world: reserve a session, then one `call_runner` POST. The video
is base64'd into the JSON request, so the standard buffered call path (and its
payment) carries it — fine for short clips.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import logging
from contextlib import suppress

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "http://localhost:8935/discovery"
APP_ID = "transcode/h264-720p"

log = logging.getLogger("vod-transcode-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Transcode a video through a Live Runner.")
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument("--input", required=True, help="Input video file.")
    parser.add_argument("--output", default="out.mp4", help="Where to write the transcoded video.")
    parser.add_argument("--height", type=int, default=720, help="Output height.")
    parser.add_argument("--signer", default="", help="Remote signer base URL (on-chain/paid path).")
    return parser.parse_args()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    signer_url = args.signer.strip() or None
    with open(args.input, "rb") as f:
        video_b64 = base64.b64encode(f.read()).decode()
    session = None
    try:
        session = await reserve_session(discovery_url=args.discovery, app=APP_ID, signer_url=signer_url)
        log.info("session_id=%s", session.session_id)
        result = await call_runner(
            runner_url=session.app_url.rstrip("/") + "/transcode",
            payload={"video_b64": video_b64, "height": args.height},
            signer_url=signer_url,
            timeout=600.0,  # transcoding a clip can take a while
        )
        data = result.data
        if "output_b64" not in data:
            raise SystemExit(f"ERROR: {data.get('error', data)}")
        with open(args.output, "wb") as f:
            f.write(base64.b64decode(data["output_b64"]))
        log.info("wrote %s (%d bytes)", args.output, data.get("bytes", 0))
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)


if __name__ == "__main__":
    asyncio.run(main())
