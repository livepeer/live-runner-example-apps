#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from contextlib import nullcontext, suppress
from pathlib import Path

import av

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish
from livepeer_gateway.http import post_json
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "http://localhost:8935/discovery"
ECHO_APP_ID = "livepeer-sample/echo"
DEFAULT_OUTPUT = "echo-out.ts"
BLUR_UPDATE_INTERVAL_S = 0.01
MAX_BLUR_RADIUS = 100


def _log(*args: object) -> None:
    print(*args, file=sys.stderr)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the proxied echo Live Runner demo.")
    parser.add_argument("input")
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--radius", type=int, default=75)
    parser.add_argument("--max-frames", type=int, default=0, help="Stop after this many input video frames (0 = full file).")
    parser.add_argument("--blur", action="store_true", help="Sweep blur radius while publishing the sample.")
    parser.add_argument("--signer", default="", help="Remote signer base URL; omit for the offchain (free) path.")
    parser.add_argument("--payment-interval", type=float, default=3.0, help="Seconds between session payments on-chain (tune to the orchestrator).")
    return parser.parse_args()


def _channel_url(echo_response: dict[str, object], name: str) -> str:
    url = echo_response.get(name)
    if not isinstance(url, str) or not url:
        raise LivepeerGatewayError(f"echo response missing {name!r} url")
    return url


async def _publish_video(
    input_path: Path,
    publish_url: str,
    *,
    max_frames: int = 0,
    app_url: str = "",
    blur: bool = False,
) -> None:
    input_ = av.open(str(input_path))
    try:
        if not input_.streams.video:
            raise LivepeerGatewayError(f"No video stream found in input file: {input_path}")
        publisher = MediaPublish(publish_url)
        prev_pts_time: float | None = None
        prev_wall: float | None = None
        next_update_pts_time: float | None = None
        blur_radius = 0
        blur_direction = 1

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
                    blur
                    and app_url
                    and current_pts_time is not None
                    and next_update_pts_time is not None
                    and current_pts_time >= next_update_pts_time
                ):
                    await post_json(f"{app_url.rstrip('/')}/update", {"mode": "blur", "radius": blur_radius})
                    if blur_radius == MAX_BLUR_RADIUS:
                        blur_direction = -1
                    elif blur_radius == 0:
                        blur_direction = 1
                    blur_radius += blur_direction
                    next_update_pts_time += BLUR_UPDATE_INTERVAL_S

                if (
                    prev_pts_time is not None
                    and prev_wall is not None
                    and current_pts_time is not None
                ):
                    delta_s = current_pts_time - prev_pts_time
                    elapsed_s = time.monotonic() - prev_wall
                    sleep_s = max(0.0, delta_s - elapsed_s)
                    if sleep_s > 0:
                        await asyncio.sleep(sleep_s)

                if current_pts_time is not None:
                    prev_pts_time = current_pts_time
                    prev_wall = time.monotonic()

                await publisher.write_frame(frame)
        finally:
            await publisher.close()
    finally:
        input_.close()


async def main() -> None:
    args = _parse_args()
    input_path = Path(args.input).expanduser()
    output_stdout = args.output.strip().lower() in {"-", "stdout"}
    output_path = None if output_stdout else Path(args.output).expanduser()
    if not input_path.exists():
        raise SystemExit(f"input file does not exist: {input_path}")

    session = None
    payment_task = None
    signer_url = args.signer.strip() or None

    try:
        session = await reserve_session(discovery_url=args.discovery, app=ECHO_APP_ID, signer_url=signer_url)
        # On-chain the orchestrator meters the session by time and drops it when
        # the balance runs dry, so keep it funded for the whole stream. No-op offchain.
        payment_task = asyncio.create_task(run_session_payments(session, interval=args.payment_interval))
        _log("runner_url:", session.runner.url if session.runner is not None else session.runner_url)
        _log("session_id:", session.session_id)
        _log("app_url:", session.app_url)

        echo = await post_json(f"{session.app_url.rstrip('/')}/echo", {"radius": args.radius})
        in_url = _channel_url(echo, "in")
        out_url = _channel_url(echo, "out")
        _log("in:", in_url)
        _log("out:", out_url)

        with nullcontext(sys.stdout.buffer) if output_stdout else output_path.open("wb") as fh:
            def _write_chunk(chunk: bytes) -> None:
                fh.write(chunk)
                if output_stdout:
                    fh.flush()

            async with MediaOutput(out_url, on_bytes=_write_chunk):
                await _publish_video(
                    input_path,
                    in_url,
                    max_frames=max(0, args.max_frames),
                    app_url=session.app_url,
                    blur=args.blur,
                )
                _log("publish complete; waiting for output to drain...")
            fh.flush()
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if payment_task is not None:
            payment_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await payment_task
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)


if __name__ == "__main__":
    asyncio.run(main())
