#!/usr/bin/env python3
"""Client for the unified transcoder — batch (VOD) or live (trickle), one session.

Sends the go-livepeer native profile ladder (`{"profiles": [...]}`) to whichever
surface you pick:

  batch (default): reserve -> call_runner POST /transcode -> write each rendition
  live (--live)  : reserve -> POST /transcode/live -> publish frames -> write .ts

    # batch: two renditions from a file (base64 in, files out)
    uv run client.py clip.mp4 --profiles '[{"name":"720p","height":720},{"name":"360p","height":360}]'
    uv run client.py clip.mp4 --heights 720,360           # shorthand

    # batch at scale: object-storage URLs, bytes never touch the control plane
    uv run client.py --input-url https://.../src.mp4 \
        --profiles '[{"name":"720p","height":720}]' \
        --output-urls '{"720p":"https://.../720p.mp4?sig=..."}'

    # live: real-time trickle ladder
    uv run client.py clip.mp4 --live --heights 720,360

Profiles are native JsonProfile objects (name/width/height/bitrate/fps/gop/
profile/encoder/quality). Offchain/free by default; --signer for on-chain.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import time
from contextlib import AsyncExitStack, suppress
from pathlib import Path

import av

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.http import post_json
from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "transcode/ffmpeg"

log = logging.getLogger("transcode-client")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Transcode a video through the unified Live Runner.")
    p.add_argument("input", nargs="?", help="Input video file (batch: base64; live: source to publish).")
    p.add_argument("--input-url", default="", help="Batch: source URL the runner fetches (object storage).")
    p.add_argument("--output-urls", default="", help='Batch: JSON {"<name>": "<presigned PUT url>"} to upload renditions.')
    p.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    p.add_argument("--heights", default="720,360", help="Comma-separated rendition heights (shorthand).")
    p.add_argument("--profiles", default="", help="Full native profile ladder as JSON. Overrides --heights.")
    p.add_argument("--output-prefix", default="out", help="Batch/live: writes <prefix>-<name>.<ext>.")
    p.add_argument("--live", action="store_true", help="Use the live trickle surface instead of batch.")
    p.add_argument("--signer", default="", help="Remote signer base URL (on-chain/paid path).")
    return p.parse_args()


def _profiles(args: argparse.Namespace) -> list[dict]:
    if args.profiles.strip():
        return json.loads(args.profiles)
    return [{"name": f"{h.strip()}p", "height": int(h)} for h in args.heights.split(",") if h.strip()]


async def _run_batch(session, args, profiles, signer_url) -> None:
    payload: dict = {"profiles": profiles}
    if args.input_url:
        payload["input_url"] = args.input_url
    else:
        payload["video_b64"] = base64.b64encode(Path(args.input).read_bytes()).decode()
    if args.output_urls.strip():
        payload["output_urls"] = json.loads(args.output_urls)

    result = await call_runner(
        runner_url=session.app_url.rstrip("/") + "/transcode",
        payload=payload, signer_url=signer_url, timeout=3600.0,
    )
    data = result.data
    if "renditions" not in data:
        raise SystemExit(f"ERROR: {data.get('error', data)}")
    for r in data["renditions"]:
        line = f"  {r['name']}: {r['encoder']} {r['width']}x{r['height']} {r['bytes']} bytes"
        if "output_b64" in r:
            out = f"{args.output_prefix}-{r['name']}.{r['ext']}"
            Path(out).write_bytes(base64.b64decode(r["output_b64"]))
            line += f" -> {out}"
        else:
            line += f" -> {r['output_url']}"
        print(line)


async def _publish(source: str, url: str) -> None:
    container = av.open(source)
    try:
        publisher = MediaPublish(url)
        prev_t = prev_wall = None
        try:
            for frame in container.decode(video=0):
                t = float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base else None
                if prev_t is not None and prev_wall is not None and t is not None:
                    sleep_s = max(0.0, (t - prev_t) - (time.monotonic() - prev_wall))
                    if sleep_s:
                        await asyncio.sleep(sleep_s)
                if t is not None:
                    prev_t, prev_wall = t, time.monotonic()
                await publisher.write_frame(frame)
        finally:
            await publisher.close()
    finally:
        container.close()


async def _run_live(session, args, profiles) -> None:
    resp = await post_json(f"{session.app_url.rstrip('/')}/transcode/live", {"profiles": profiles})
    outputs: dict[str, str] = resp["outputs"]
    files = {}
    try:
        async with AsyncExitStack() as stack:
            for name, url in outputs.items():
                fh = open(f"{args.output_prefix}-{name}.ts", "wb")
                files[name] = fh
                await stack.enter_async_context(MediaOutput(url, on_bytes=fh.write))
            await _publish(args.input, resp["in"])
            log.info("publish complete; draining %d rendition(s)...", len(outputs))
    finally:
        for fh in files.values():
            fh.close()
    for name in outputs:
        print(f"  wrote {args.output_prefix}-{name}.ts")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    if not args.input and not args.input_url:
        raise SystemExit("give an input file (batch/live) or --input-url (batch)")
    if args.live and not args.input:
        raise SystemExit("--live needs a source file to publish")
    signer_url = args.signer.strip() or None
    profiles = _profiles(args)

    session = None
    try:
        session = await reserve_session(discovery_url=args.discovery, app=APP_ID, signer_url=signer_url)
        log.info("session_id=%s app=%s", session.session_id, APP_ID)
        if args.live:
            await _run_live(session, args, profiles)
        else:
            await _run_batch(session, args, profiles, signer_url)
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)


if __name__ == "__main__":
    asyncio.run(main())
