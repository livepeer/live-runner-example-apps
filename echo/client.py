#!/usr/bin/env python3
"""echo client: reserve a session, stream video through the runner, settle up.

Publishes video frames into the runner's trickle `in` channel and reads the transformed
frames back from `out`. Input/output can be files or stdin/stdout pipes, so you can
chain `ffmpeg -> client -> ffplay`.

Livepeer integration (grep `# Livepeer:`):
  1. reserve_session()        — discover the runner, reserve a session. On the paid
                                path it answers the 402 challenge and then keeps the
                                session funded in the background, because the price is
                                metered: the orchestrator debits every few seconds and
                                releases the session on the first debit it cannot cover.
  2. MediaPublish/MediaOutput — publish frames to `in`, read echoed frames from `out`
  3. stop_runner_session()    — end the session. Leaving the `async with` stops the
                                funding first, so nothing is paid for a session that
                                is about to go away.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from contextlib import nullcontext, suppress
from pathlib import Path

import av

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import stop_runner_session
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish
from livepeer_gateway.http import post_json
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/echo"
DEFAULT_OUTPUT = "echo-out.ts"
MAX_BLUR_RADIUS = 100
MODES = ("echo", "gray", "invert", "blur")

log = logging.getLogger("echo-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the proxied echo Live Runner demo."
    )
    parser.add_argument(
        "input",
        help=(
            "Input video file, or - to read an MPEG-TS stream from stdin "
            "(e.g. piped from ffmpeg)"
        ),
    )
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument(
        "--signer", default="", help="Remote signer base URL (on-chain/paid path)."
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=(
            "Output file for the echoed stream, or - for stdout (e.g. piped to ffplay)"
        ),
    )
    parser.add_argument("--radius", type=int, default=75)
    parser.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="Stop after this many input video frames (0 = full file).",
    )
    parser.add_argument(
        "--mode",
        choices=MODES,
        default="echo",
        help=(
            "Transform the runner applies: echo (passthrough), gray, invert, or blur. "
            "blur sweeps the radius; the rest are static."
        ),
    )
    parser.add_argument(
        "--blur-period",
        type=float,
        default=2.0,
        help="Seconds per blur sweep cycle (0->max->0); only used with --mode blur.",
    )
    return parser.parse_args()


def _channel_url(echo_response: dict[str, object], name: str) -> str:
    url = echo_response.get(name)
    if not isinstance(url, str) or not url:
        raise LivepeerGatewayError(f"echo response missing {name!r} url")
    return url


async def _publish_video(
    input_source: str,
    publish_url: str,
    *,
    max_frames: int = 0,
    app_url: str = "",
    mode: str = "echo",
    blur_period: float = 2.0,
) -> None:
    # "-" = a live MPEG-TS stream on stdin; read it via libav's "pipe:0" rather than
    # sys.stdin.buffer, whose read() blocks for a full buffer and stalls until EOF.
    live = input_source == "-"
    input_ = av.open("pipe:0", format="mpegts") if live else av.open(input_source)
    try:
        if not input_.streams.video:
            raise LivepeerGatewayError(
                f"No video stream found in input: {input_source}"
            )
        publisher = MediaPublish(publish_url)  # Livepeer: 2 (publish frames)
        prev_pts_time: float | None = None
        prev_wall: float | None = None
        next_update_pts_time: float | None = None
        blur_radius = 0
        blur_direction = 1
        # blur sweeps 0->max->0 (2*MAX steps); spread one full cycle over blur_period.
        update_interval = blur_period / (2 * MAX_BLUR_RADIUS)

        try:
            for index, frame in enumerate(input_.decode(video=0), start=1):
                if max_frames > 0 and index > max_frames:
                    break
                current_pts_time = None
                if frame.pts is not None and frame.time_base is not None:
                    current_pts_time = float(frame.pts * frame.time_base)
                    if next_update_pts_time is None:
                        next_update_pts_time = current_pts_time

                while (
                    mode == "blur"
                    and app_url
                    and current_pts_time is not None
                    and next_update_pts_time is not None
                    and current_pts_time >= next_update_pts_time
                ):
                    await post_json(
                        f"{app_url.rstrip('/')}/update",
                        {"mode": "blur", "radius": blur_radius},
                    )
                    if blur_radius == MAX_BLUR_RADIUS:
                        blur_direction = -1
                    elif blur_radius == 0:
                        blur_direction = 1
                    blur_radius += blur_direction
                    next_update_pts_time += update_interval

                # Pace files to realtime (live self-paces, so sleep_s=0). sleep(0) still
                # yields, so async POSTs/reads aren't starved by the blocking decode.
                sleep_s = 0.0
                if (
                    not live
                    and prev_pts_time is not None
                    and prev_wall is not None
                    and current_pts_time is not None
                ):
                    sleep_s = max(
                        0.0,
                        (current_pts_time - prev_pts_time)
                        - (time.monotonic() - prev_wall),
                    )

                if current_pts_time is not None:
                    prev_pts_time = current_pts_time
                    prev_wall = time.monotonic()

                await publisher.write_frame(frame)
                await asyncio.sleep(sleep_s)
        finally:
            await publisher.close()
    finally:
        input_.close()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    output_stdout = args.output.strip() == "-"
    output_path = None if output_stdout else Path(args.output).expanduser()
    input_source = args.input.strip()
    if input_source != "-":
        input_path = Path(input_source).expanduser()
        if not input_path.exists():
            raise SystemExit(f"input file does not exist: {input_path}")
        input_source = str(input_path)

    session = None

    try:
        session = await reserve_session(  # Livepeer: 1
            discovery_url=args.discovery,  # omit if the signer does discovery itself
            app=APP_ID,
            signer_url=args.signer.strip() or None,
        )
        log.info("session_id=%s app_url=%s", session.session_id, session.app_url)

        # The session funds itself while it is held; leaving this block stops that.
        async with session:
            echo = await post_json(
                f"{session.app_url.rstrip('/')}/echo",
                {"radius": args.radius, "mode": args.mode},
            )
            in_url = _channel_url(echo, "in")
            out_url = _channel_url(echo, "out")
            log.info("in=%s out=%s", in_url, out_url)

            with (
                nullcontext(sys.stdout.buffer)
                if output_stdout
                else output_path.open("wb")
            ) as fh:

                def _write_chunk(chunk: bytes) -> None:
                    fh.write(chunk)
                    if output_stdout:
                        fh.flush()

                async with MediaOutput(
                    out_url, on_bytes=_write_chunk
                ):  # Livepeer: 2 (read echoed frames)
                    await _publish_video(
                        input_source,
                        in_url,
                        max_frames=max(0, args.max_frames),
                        app_url=session.app_url,
                        mode=args.mode,
                        blur_period=args.blur_period,
                    )
                    log.info("publish complete; waiting for output to drain...")
                fh.flush()
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)  # Livepeer: 3


if __name__ == "__main__":
    asyncio.run(main())
