#!/usr/bin/env python3
# Client for the self-hosted StreamDiffusion app (new runner).
#
# The app is registered as the app id below and routed by the orchestrator like
# echo, so this uses the echo client flow (reserve_session + /stream), NOT the
# lv2v capability flow. Offchain/free against your local orchestrator.
#
# Publishes a video file or a Linux v4l2 webcam into the trickle `in` channel,
# reads the diffused output from `out`, and can re-prompt live via /update.
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from contextlib import nullcontext, suppress
from pathlib import Path

import av

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import run_session_payments, stop_runner_session
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish
from livepeer_gateway.http import post_json
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-sample/streamdiffusion"
DEFAULT_OUTPUT = "streamdiffusion-out.ts"
DEFAULT_PROMPT = "a psychedelic landscape, vivid colors, intricate details"


def _log(*args: object) -> None:
    print(*args, file=sys.stderr)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run video through the self-hosted StreamDiffusion app.")
    p.add_argument("input", nargs="?", default=None, help="Local mp4/mov. Omit and pass --webcam for the camera.")
    p.add_argument("--webcam", nargs="?", const="/dev/video0", default=None, metavar="DEVICE",
                   help="Capture from a Linux v4l2 webcam (default /dev/video0).")
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--video-size", default="512x512", help="Webcam capture size (default 512x512).")
    p.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="Output file, or '-' for stdout (pipe to ffplay).")
    p.add_argument("--max-frames", type=int, default=0)
    p.add_argument("--reprompt", action="append", default=[], metavar="SECONDS=PROMPT",
                   help="Live prompt change, e.g. --reprompt 5=an oil painting. Repeatable.")
    p.add_argument("--signer", default="", help="Remote signer URL; omit for the offchain (free) local path.")
    p.add_argument("--payment-interval", type=float, default=3.0)
    return p.parse_args()


def _channel_url(resp: dict[str, object], name: str) -> str:
    url = resp.get(name)
    if not isinstance(url, str) or not url:
        raise LivepeerGatewayError(f"/stream response missing {name!r} url")
    return url


def _parse_reprompts(values: list[str]) -> list[tuple[float, str]]:
    out: list[tuple[float, str]] = []
    for v in values:
        at, _, prompt = v.partition("=")
        if not prompt:
            raise SystemExit(f"--reprompt expects SECONDS=PROMPT, got {v!r}")
        out.append((float(at), prompt))
    return sorted(out, key=lambda x: x[0])


async def _reprompt(app_url: str, schedule: list[tuple[float, str]]) -> None:
    start = time.monotonic()
    for at, prompt in schedule:
        delay = at - (time.monotonic() - start)
        if delay > 0:
            await asyncio.sleep(delay)
        with suppress(Exception):
            await post_json(f"{app_url.rstrip('/')}/update", {"prompt": prompt})
            _log(f"re-prompted at {at:.1f}s: {prompt!r}")


async def _publish_file(input_path: Path, publish_url: str, *, max_frames: int = 0) -> None:
    input_ = av.open(str(input_path))
    try:
        if not input_.streams.video:
            raise LivepeerGatewayError(f"No video stream in {input_path}")
        publisher = MediaPublish(publish_url)
        prev_pts_time = prev_wall = None
        try:
            for index, frame in enumerate(input_.decode(video=0), start=1):
                if max_frames > 0 and index > max_frames:
                    break
                cur = float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base else None
                if prev_pts_time is not None and prev_wall is not None and cur is not None:
                    sleep_s = max(0.0, (cur - prev_pts_time) - (time.monotonic() - prev_wall))
                    if sleep_s > 0:
                        await asyncio.sleep(sleep_s)
                if cur is not None:
                    prev_pts_time, prev_wall = cur, time.monotonic()
                await publisher.write_frame(frame)
        finally:
            await publisher.close()
    finally:
        input_.close()


async def _publish_webcam(device: str, publish_url: str, *, fps: float, video_size: str, max_frames: int = 0) -> None:
    input_ = av.open(device, format="v4l2", container_options={"framerate": str(fps), "video_size": video_size})
    try:
        if not input_.streams.video:
            raise LivepeerGatewayError(f"No video stream on {device}")
        publisher = MediaPublish(publish_url)
        try:
            index = 0
            for frame in input_.decode(video=0):
                index += 1
                if max_frames > 0 and index > max_frames:
                    break
                frame.pts = None  # let MediaPublish stamp wall-clock pts
                await publisher.write_frame(frame)
        finally:
            await publisher.close()
    finally:
        input_.close()


async def main() -> None:
    args = _parse_args()
    if args.webcam is None and not args.input:
        raise SystemExit("provide an input file or --webcam")
    input_path = None
    if args.webcam is None:
        input_path = Path(args.input).expanduser()
        if not input_path.exists():
            raise SystemExit(f"input file does not exist: {input_path}")
    reprompts = _parse_reprompts(args.reprompt)

    output_stdout = args.output.strip().lower() in {"-", "stdout"}
    output_path = None if output_stdout else Path(args.output).expanduser()
    signer_url = args.signer.strip() or None

    session = payment_task = reprompt_task = None
    try:
        session = await reserve_session(discovery_url=args.discovery, app=APP_ID, signer_url=signer_url)
        payment_task = asyncio.create_task(run_session_payments(session, interval=args.payment_interval))
        _log("session_id:", session.session_id, "app_url:", session.app_url)

        resp = await post_json(f"{session.app_url.rstrip('/')}/stream", {"prompt": args.prompt})
        in_url, out_url = _channel_url(resp, "in"), _channel_url(resp, "out")
        _log("in:", in_url, "out:", out_url)
        if reprompts:
            reprompt_task = asyncio.create_task(_reprompt(session.app_url, reprompts))

        with nullcontext(sys.stdout.buffer) if output_stdout else output_path.open("wb") as fh:
            def _write_chunk(chunk: bytes) -> None:
                fh.write(chunk)
                if output_stdout:
                    fh.flush()

            async with MediaOutput(out_url, on_bytes=_write_chunk):
                if args.webcam is not None:
                    _log(f"streaming webcam {args.webcam} ({args.video_size}); ctrl-c to stop")
                    await _publish_webcam(args.webcam, in_url, fps=args.fps,
                                          video_size=args.video_size, max_frames=max(0, args.max_frames))
                else:
                    await _publish_file(input_path, in_url, max_frames=max(0, args.max_frames))
                _log("publish complete; waiting for output to drain...")
            fh.flush()
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        for task in (reprompt_task, payment_task):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)


if __name__ == "__main__":
    asyncio.run(main())
