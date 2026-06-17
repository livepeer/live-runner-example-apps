#!/usr/bin/env python3
"""Stream a video through the realtime transcoder and save each rendition.

Mirrors the SDK `echo` example: reserve a session, POST a profile ladder to get
the in + per-rendition trickle channels, publish the source file's frames
(real-time paced) to `in`, and write each output channel to out-<name>.ts.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from contextlib import AsyncExitStack, suppress
from pathlib import Path

import av

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.http import post_json
from livepeer_gateway.live_runner import stop_runner_session
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "http://localhost:8935/discovery"
APP_ID = "transcode/live-h264"

log = logging.getLogger("live-transcode-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Realtime multi-rendition transcode through a Live Runner.")
    parser.add_argument("input", help="A file path, or a live URL (rtmp://, srt://, http://).")
    parser.add_argument("--listen", action="store_true",
                        help="Treat input as an address to LISTEN on for a push, e.g. rtmp://0.0.0.0:1935/live.")
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument("--heights", default="360", help="Comma-separated rendition heights, e.g. 720,360.")
    parser.add_argument("--profiles", default="",
                        help='Full profile ladder as JSON, e.g. \'[{"name":"720p","height":720,"fps":30,"codec":"libx264"}]\'. Overrides --heights.')
    parser.add_argument("--output-prefix", default="out", help="Writes <prefix>-<name>.ts per rendition.")
    parser.add_argument("--signer", default="", help="Remote signer base URL (on-chain/paid path).")
    return parser.parse_args()


async def _publish_video(source: str, publish_url: str, *, listen: bool = False, pace: bool = True) -> None:
    # listen=True waits for a push (e.g. an RTMP/SRT publisher) on `source`.
    open_opts = {"listen": "1"} if listen else None
    if listen:
        log.info("waiting for a push on %s ...", source)
    container = av.open(source, options=open_opts)
    try:
        if not container.streams.video:
            raise LivepeerGatewayError(f"no video stream in {source}")
        publisher = MediaPublish(publish_url)
        prev_pts_time = prev_wall = None
        try:
            for frame in container.decode(video=0):
                if pace:  # a live source already arrives in real time; only pace files
                    pts_time = float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base else None
                    if prev_pts_time is not None and prev_wall is not None and pts_time is not None:
                        sleep_s = max(0.0, (pts_time - prev_pts_time) - (time.monotonic() - prev_wall))
                        if sleep_s:
                            await asyncio.sleep(sleep_s)
                    if pts_time is not None:
                        prev_pts_time, prev_wall = pts_time, time.monotonic()
                await publisher.write_frame(frame)
        finally:
            await publisher.close()
    finally:
        container.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        raise SystemExit(f"input file does not exist: {input_path}")
    signer_url = args.signer.strip() or None
    if args.profiles.strip():
        profiles = json.loads(args.profiles)
    else:
        profiles = [{"name": f"{h.strip()}p", "height": int(h)} for h in args.heights.split(",") if h.strip()]
    session = None
    try:
        session = await reserve_session(discovery_url=args.discovery, app=APP_ID, signer_url=signer_url)
        log.info("session_id=%s", session.session_id)
        resp = await post_json(f"{session.app_url.rstrip('/')}/transcode", {"profiles": profiles})
        outputs: dict[str, str] = resp["outputs"]
        files = {}
        try:
            async with AsyncExitStack() as stack:
                for name, url in outputs.items():
                    fh = open(f"{args.output_prefix}-{name}.ts", "wb")
                    files[name] = fh
                    await stack.enter_async_context(MediaOutput(url, on_bytes=fh.write))
                await _publish_video(input_path, resp["in"])
                log.info("publish complete; draining %d rendition(s)...", len(outputs))
        finally:
            for fh in files.values():
                fh.close()
        for name in outputs:
            log.info("wrote %s-%s.ts", args.output_prefix, name)
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)


if __name__ == "__main__":
    asyncio.run(main())
