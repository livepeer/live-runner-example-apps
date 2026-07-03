#!/usr/bin/env python3
# Client for the self-hosted StreamDiffusion app (new runner).
#
# Mirrors the echo client: reserve a session, open trickle in/out via /stream,
# publish an input video, read the diffused output, and re-prompt live over
# /update. The input is a file, or "-" to read an MPEG-TS stream from stdin, so
# anything ffmpeg produces (a test pattern, a file, a webcam) can be piped in and
# the diffused result viewed live (e.g. via ffplay). Offchain/free by default.
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
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import (
    MediaPublish,
    MediaPublishConfig,
    VideoOutputConfig,
)
from livepeer_gateway.http import post_json
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-sample/streamdiffusion"
DEFAULT_OUTPUT = "streamdiffusion-out.ts"
DEFAULT_PROMPT = "a psychedelic landscape, vivid colors, intricate details"

log = logging.getLogger("streamdiffusion-client")


def _lowlatency_publish_config(fps: float = 30.0) -> MediaPublishConfig:
    # Diffusion runs below the input frame rate, so the runner often falls behind
    # and skips to the live edge (LagPolicy.LATEST). That skip is per-segment, so
    # short segments keep the recovery close to live instead of ~2s (the default
    # GOP). Costs more keyframes; worth it for a lagging realtime consumer.
    return MediaPublishConfig(
        tracks=[VideoOutputConfig(fps=fps, keyframe_interval_s=0.25)],
        min_segment_wallclock_s=0.25,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run video through the self-hosted StreamDiffusion app."
    )
    p.add_argument(
        "input",
        help="input video file, or - to read an MPEG-TS stream from stdin (e.g. piped from ffmpeg)",
    )
    p.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="output file for the diffused stream, or - for stdout (e.g. piped to ffplay)",
    )
    p.add_argument(
        "--max-frames",
        type=int,
        default=0,
        help="Stop after this many input frames (0 = full input).",
    )
    p.add_argument(
        "--reprompt",
        action="append",
        default=[],
        metavar="SECONDS=PROMPT",
        help="Scheduled live prompt change, e.g. --reprompt 6=an oil painting. Repeatable.",
    )
    p.add_argument(
        "--signer",
        default="",
        help="Remote signer URL; omit for the offchain (free) local path.",
    )
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
            log.info("re-prompted at %.1fs: %r", at, prompt)


async def _publish_video(
    input_source: str, publish_url: str, *, max_frames: int = 0
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
        publisher = MediaPublish(publish_url)
        # publisher = MediaPublish(publish_url, config=_lowlatency_publish_config())
        prev_pts_time = prev_wall = None
        loop_start = decode_gap_ms = write_ms = 0.0
        prev_iter = None
        try:
            for index, frame in enumerate(input_.decode(video=0), start=1):
                iter_t = time.monotonic()
                if prev_iter is not None:
                    decode_gap_ms += (
                        iter_t - prev_iter
                    ) * 1000.0  # time to get the next decoded frame
                if loop_start == 0.0:
                    loop_start = iter_t
                if max_frames > 0 and index > max_frames:
                    break
                cur = (
                    float(frame.pts * frame.time_base)
                    if frame.pts is not None and frame.time_base
                    else None
                )
                # Pace a file to realtime (a live stdin source paces itself, so sleep_s=0).
                # Always await the sleep (sleep(0) still yields) so the async output
                # reader / prompt POSTs aren't starved by the blocking decode.
                sleep_s = 0.0
                if (
                    not live
                    and prev_pts_time is not None
                    and prev_wall is not None
                    and cur is not None
                ):
                    sleep_s = max(
                        0.0, (cur - prev_pts_time) - (time.monotonic() - prev_wall)
                    )
                if cur is not None:
                    prev_pts_time, prev_wall = cur, time.monotonic()
                w0 = time.monotonic()
                await publisher.write_frame(frame)
                write_ms += (time.monotonic() - w0) * 1000.0
                await asyncio.sleep(sleep_s)
                prev_iter = time.monotonic()
                if index % 30 == 0:
                    el = prev_iter - loop_start
                    tr = None
                    with suppress(Exception):
                        ps = publisher.get_stats()
                        tr = ps.track_queue_stats[0] if ps.track_queue_stats else None
                    log.info(
                        f"CLIENT publish: {index/el:.1f} fps | decode_gap_avg={decode_gap_ms/index:.1f}ms "
                        f"write_avg={write_ms/index:.1f}ms | pub_in={getattr(tr,'frames_in',None)} "
                        f"drop={getattr(tr,'frames_dropped_overflow',None)} qdepth={getattr(tr,'queue_depth',None)}"
                    )
        finally:
            await publisher.close()
    finally:
        input_.close()


async def main() -> None:
    # Logs go to stderr, so stdout stays clean for `--output -`.
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    output_stdout = args.output.strip().lower() in {"-", "stdout"}
    output_path = None if output_stdout else Path(args.output).expanduser()
    input_source = args.input.strip()
    if input_source != "-":
        input_path = Path(input_source).expanduser()
        if not input_path.exists():
            raise SystemExit(f"input file does not exist: {input_path}")
        input_source = str(input_path)
    reprompts = _parse_reprompts(args.reprompt)
    signer_url = args.signer.strip() or None

    session = reprompt_task = None
    try:
        session = await reserve_session(
            discovery_url=args.discovery,
            app=APP_ID,
            signer_url=signer_url,
            payment_interval=args.payment_interval,
        )
        session.start_payments()  # on-chain: keep the session funded for the whole stream; no-op offchain
        log.info("session_id=%s app_url=%s", session.session_id, session.app_url)

        resp = await post_json(
            f"{session.app_url.rstrip('/')}/stream", {"prompt": args.prompt}
        )
        in_url, out_url = _channel_url(resp, "in"), _channel_url(resp, "out")
        log.info("in=%s out=%s", in_url, out_url)
        if reprompts:
            reprompt_task = asyncio.create_task(_reprompt(session.app_url, reprompts))

        with (
            nullcontext(sys.stdout.buffer) if output_stdout else output_path.open("wb")
        ) as fh:

            def _write_chunk(chunk: bytes) -> None:
                fh.write(chunk)
                if output_stdout:
                    fh.flush()

            # Read every diffused segment the runner produced (default window, like
            # echo). Do NOT skip-to-latest here: under player backpressure a tight
            # window splices the MPEG-TS mid-stream (ffplay: "Packet corrupt") and
            # drops frames the runner already computed. Frame-dropping belongs on the
            # runner's INPUT (it can't diffuse at 30fps); the player's own -framedrop
            # keeps the display current without splicing the stream.
            async with MediaOutput(out_url, on_bytes=_write_chunk):
                await _publish_video(
                    input_source, in_url, max_frames=max(0, args.max_frames)
                )
                log.info("publish complete; waiting for output to drain...")
            fh.flush()
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if reprompt_task is not None:
            reprompt_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await reprompt_task
        if session is not None:
            # aclose() cancels the payment loop (if any) and stops the runner session.
            with suppress(Exception):
                await session.aclose()


if __name__ == "__main__":
    asyncio.run(main())
