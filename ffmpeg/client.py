#!/usr/bin/env python3
"""Call the ffmpeg tool runner: pick an op, send a file, save the result.

Mirrors hello-world / vod-transcode: reserve a session, then one `call_runner`
POST to /run. The input file is base64'd into the JSON request, so the standard
buffered call path (and its payment) carries it — fine for short clips.

  uv run client.py --op transcode --height 480 --input clip.mp4 --output out.mp4
  uv run client.py --op clip --start 1 --end 3   --input clip.mp4 --output cut.mp4
  uv run client.py --op thumbnail --at 1.5        --input clip.mp4 --output thumb.jpg
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
from contextlib import suppress

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"  # orch serves HTTPS on 8935
APP_ID = "livepeer/ffmpeg"
DEFAULT_OUTPUT = {
    "transcode": "out.mp4", "clip": "out.mp4", "thumbnail": "out.jpg",
    "extract_audio": "out.m4a", "gif": "out.gif", "crop": "out.mp4",
}  # convert -> out.<format> (computed); probe writes no file

log = logging.getLogger("ffmpeg-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an ffmpeg op through a Live Runner.")
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument("--op", default="transcode",
                        choices=["transcode", "clip", "thumbnail", "extract_audio",
                                 "gif", "crop", "convert", "probe"])
    parser.add_argument("--input", required=True, help="Input media file.")
    parser.add_argument("--output", default=None, help="Output path (default depends on op).")
    # op params (only the ones relevant to --op are sent)
    parser.add_argument("--height", type=int, help="transcode/gif/crop: output height.")
    parser.add_argument("--encoder", help="transcode: libx264 (cpu) or h264_nvenc (gpu).")
    parser.add_argument("--quality", type=int, help="transcode/convert: CRF/CQ, lower=higher quality (18 high, 23 default).")
    parser.add_argument("--start", type=float, help="clip: start seconds.")
    parser.add_argument("--end", type=float, help="clip: end seconds.")
    parser.add_argument("--at", type=float, help="thumbnail: timestamp seconds.")
    parser.add_argument("--fps", type=int, help="gif: frames per second.")
    parser.add_argument("--width", type=int, help="crop: width (px).")
    parser.add_argument("--x", type=int, help="crop: left offset (px).")
    parser.add_argument("--y", type=int, help="crop: top offset (px).")
    parser.add_argument("--format", help="convert: target container (mkv/mov).")
    parser.add_argument("--signer", default="", help="Remote signer base URL (on-chain/paid path).")
    return parser.parse_args()


def _payload(args: argparse.Namespace, input_b64: str) -> dict:
    payload: dict = {"op": args.op, "input_b64": input_b64}
    for key in ("height", "encoder", "quality", "start", "end", "at", "fps", "width", "x", "y", "format"):
        val = getattr(args, key)
        if val is not None:
            payload[key] = val
    return payload


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    signer_url = args.signer.strip() or None
    output = args.output or (
        f"out.{args.format or 'mp4'}" if args.op == "convert" else DEFAULT_OUTPUT.get(args.op)
    )
    with open(args.input, "rb") as f:
        input_b64 = base64.b64encode(f.read()).decode()
    session = None
    try:
        session = await reserve_session(discovery_url=args.discovery, app=APP_ID, signer_url=signer_url)
        log.info("session_id=%s op=%s", session.session_id, args.op)
        result = await call_runner(
            runner_url=session.app_url.rstrip("/") + "/run",
            payload=_payload(args, input_b64),
            signer_url=signer_url,
            timeout=600.0,  # a media op can take a while
        )
        data = result.data
        if "analysis" in data:  # probe — print metadata, write no file
            print(json.dumps(data["analysis"], indent=2))
            return
        if "output_b64" not in data:
            raise SystemExit(f"ERROR: {data.get('error', data)}")
        with open(output, "wb") as f:
            f.write(base64.b64decode(data["output_b64"]))
        log.info("wrote %s (%d bytes, %s)", output, data.get("bytes", 0), data.get("media_type", "?"))
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)


if __name__ == "__main__":
    asyncio.run(main())
