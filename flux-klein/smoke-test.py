#!/usr/bin/env python3
"""End-to-end smoke test for the flux-klein realtime app (app-examples PR #32).

Exercises the deployed runner on a live orchestrator over the trickle protocol:

    /discovery  -> assert livepeer-example/flux-klein is registered
    reserve_session (offchain / free, no signer)
    POST /stream {prompt, seed, input_blend} -> {in, out} trickle urls
    publish a short test clip into `in`, read FLUX output from `out`
    POST /update mid-stream (live re-prompt)         [optional]
    GET  /stats                                      (report inference fps)
    tear the session down

Pass criteria: a session is reserved, output bytes stream back, and the received
MPEG-TS decodes to >= 1 frame. Exits non-zero on any failure.

Run from the flux-klein/ directory with its venv (see TESTING.md):

    cd flux-klein && uv sync
    .venv/bin/python smoke-test.py
    .venv/bin/python smoke-test.py --input clip.mp4 --reprompt "2=an oil painting" --keep-output
    .venv/bin/python smoke-test.py --webcam --frames 240 --stats-interval 2

Offchain (free) path against the orchestrator — no signer/payments needed. The
same venv runs the interactive client.py; this script is the automated check.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
import av

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.http import post_json
from livepeer_gateway.live_runner import stop_runner_session
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import (
    MediaPublish,
    MediaPublishConfig,
    VideoOutputConfig,
)
from livepeer_gateway.selection import reserve_session

APP_ID = "livepeer-example/flux-klein"
DEFAULT_DISCOVERY = "http://154.61.61.108:8787/discovery"
DEFAULT_PROMPT = "a psychedelic landscape, vivid colors, intricate details"


def _log(*a: object) -> None:
    print(*a, file=sys.stderr, flush=True)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--discovery",
        default=DEFAULT_DISCOVERY,
        help=f"orchestrator discovery url (default {DEFAULT_DISCOVERY})",
    )
    p.add_argument(
        "--input",
        default=None,
        help="input mp4/mov, or '-' for an MPEG-TS stream on stdin (e.g. `ffmpeg ... -f mpegts -`). "
        "Omit (and don't pass --webcam) to generate an ffmpeg testsrc clip.",
    )
    p.add_argument(
        "--webcam",
        nargs="?",
        const="/dev/video0",
        default=None,
        metavar="DEVICE",
        help="capture from a Linux v4l2 webcam (default /dev/video0)",
    )
    p.add_argument(
        "--webcam-macos",
        nargs="?",
        const="0",
        default=None,
        metavar="INDEX",
        help="capture from a macOS AVFoundation camera by index (default 0); "
        'list with: ffmpeg -f avfoundation -list_devices true -i ""',
    )
    p.add_argument(
        "--video-size", default="640x480", help="webcam capture size (default 640x480)"
    )
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument(
        "--seed", type=int, default=None, help="noise seed (-1 = fresh noise per frame)"
    )
    p.add_argument(
        "--input-blend",
        type=float,
        default=None,
        help="camera weight 0..1 in the feedback blend",
    )
    p.add_argument(
        "--frames",
        type=int,
        default=90,
        help="max frames to publish (default 90 = 3s @30fps). Also sets the generated clip length. "
        "0 = unbounded (run a webcam/stdin stream until ctrl-c or EOF).",
    )
    p.add_argument(
        "--fps", type=float, default=30.0, help="publish/capture rate (default 30)"
    )
    p.add_argument(
        "--reprompt",
        action="append",
        default=[],
        metavar="SECONDS=PROMPT",
        help='live change, e.g. --reprompt "2=an oil painting" (also seed:N / blend:X). Repeatable.',
    )
    p.add_argument(
        "--output",
        default="flux-klein-out.ts",
        help="where to write the returned stream",
    )
    p.add_argument(
        "--keep-output",
        action="store_true",
        help="keep the output .ts (default: delete after decode check)",
    )
    p.add_argument(
        "--drain",
        type=float,
        default=20.0,
        help="seconds to keep reading output after publishing ends, waiting for trailing segments (default 20)",
    )
    p.add_argument(
        "--stats-interval",
        type=float,
        default=0.0,
        help="poll GET /stats every N seconds during the stream (0 = only once at the end)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="overall wall-clock budget in seconds",
    )
    return p.parse_args()


def _lowlatency_config(fps: float) -> MediaPublishConfig:
    # Short GOP + segment floor => trickle flushes ~4x/sec for near-realtime,
    # mirroring flux-klein/client.py.
    return MediaPublishConfig(
        tracks=[VideoOutputConfig(fps=fps, keyframe_interval_s=0.25)],
        min_segment_wallclock_s=0.25,
    )


def _channel_url(resp: dict, name: str) -> str:
    url = resp.get(name)
    if not isinstance(url, str) or not url:
        raise LivepeerGatewayError(
            f"/stream response missing {name!r} url; got keys {list(resp)}"
        )
    return url


def _parse_reprompts(values: list[str]) -> list[tuple[float, dict]]:
    out: list[tuple[float, dict]] = []
    for v in values:
        at, _, val = v.partition("=")
        if not val:
            raise SystemExit(f"--reprompt expects SECONDS=PROMPT, got {v!r}")
        if val.startswith("seed:"):
            payload: dict = {"seed": int(val.partition(":")[2])}
        elif val.startswith("blend:"):
            payload = {"input_blend": float(val.partition(":")[2])}
        else:
            payload = {"prompt": val}
        out.append((float(at), payload))
    return sorted(out, key=lambda x: x[0])


def _make_test_clip(frames: int, fps: float) -> Path:
    if not shutil.which("ffmpeg"):
        raise SystemExit(
            "ffmpeg not found; pass --input <file> instead of using the generated clip"
        )
    dur = max(1.0, frames / fps)
    tmp = Path(tempfile.mkdtemp(prefix="flux-klein-")) / "testsrc.mp4"
    cmd = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=640x480:rate={fps:g}",
        "-t",
        f"{dur:g}",
        "-pix_fmt",
        "yuv420p",
        str(tmp),
    ]
    subprocess.run(cmd, check=True)
    _log(f"generated test clip: {tmp} ({dur:g}s, {fps:g}fps)")
    return tmp


async def _check_discovery(discovery_url: str) -> None:
    async with aiohttp.ClientSession() as s:
        async with s.get(discovery_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
            r.raise_for_status()
            orchs = await r.json()
    runners = [
        rn for o in orchs for rn in o.get("runners", []) if rn.get("app") == APP_ID
    ]
    if not runners:
        apps = sorted({rn.get("app") for o in orchs for rn in o.get("runners", [])})
        raise SystemExit(f"FAIL: {APP_ID!r} not in discovery. Registered apps: {apps}")
    for rn in runners:
        _log(
            f"discovery: {APP_ID} mode={rn.get('mode')} "
            f"capacity_used={rn.get('capacity_used')} available={rn.get('capacity_available')}"
        )
    if all(rn.get("capacity_available") == 0 for rn in runners):
        _log(
            "WARN: flux-klein reports 0 available capacity (slot busy or a prior session leaked); "
            "reserve may fail."
        )


async def _reprompt(app_url: str, schedule: list[tuple[float, dict]]) -> None:
    start = time.monotonic()
    for at, payload in schedule:
        delay = at - (time.monotonic() - start)
        if delay > 0:
            await asyncio.sleep(delay)
        with contextlib.suppress(Exception):
            await post_json(f"{app_url.rstrip('/')}/update", payload)
            _log(f"update @ {at:.1f}s -> {payload}")


async def _get_stats(app_url: str) -> dict | None:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"{app_url.rstrip('/')}/stats", timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                if r.status != 200:
                    return None
                return await r.json()
    except Exception:
        return None


async def _poll_stats(app_url: str, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        stats = await _get_stats(app_url)
        if stats is not None:
            sess = stats.get("session")
            _log(
                f"stats: state={stats.get('state')} "
                + (f"session={json.dumps(sess)}" if sess else "session=null")
            )


async def _publish_file(
    input_path_or_dash: str, publish_url: str, *, fps: float, max_frames: int
) -> int:
    """Publish a local file, or an MPEG-TS stream on stdin when input is '-'."""
    live = input_path_or_dash == "-"
    container = (
        av.open("pipe:0", format="mpegts") if live else av.open(input_path_or_dash)
    )
    sent = 0
    try:
        if not container.streams.video:
            raise LivepeerGatewayError(f"no video stream in {input_path_or_dash}")
        pub = MediaPublish(publish_url, config=_lowlatency_config(fps))
        prev_pts = prev_wall = None
        try:
            for frame in container.decode(video=0):
                if max_frames and sent >= max_frames:
                    break
                # Files carry their own pacing; a live TS stream arrives at wall-clock rate.
                cur = (
                    float(frame.pts * frame.time_base)
                    if frame.pts is not None and frame.time_base
                    else None
                )
                if (
                    not live
                    and prev_pts is not None
                    and prev_wall is not None
                    and cur is not None
                ):
                    sleep_s = max(
                        0.0, (cur - prev_pts) - (time.monotonic() - prev_wall)
                    )
                    if sleep_s > 0:
                        await asyncio.sleep(sleep_s)
                if cur is not None:
                    prev_pts, prev_wall = cur, time.monotonic()
                await pub.write_frame(frame)
                sent += 1
        finally:
            await pub.close()
    finally:
        container.close()
    return sent


async def _publish_webcam(
    device: str, publish_url: str, *, fps: float, video_size: str, max_frames: int
) -> int:
    """Linux v4l2 capture. Reads packets off-thread so the blocking demux can't stall asyncio."""
    container = av.open(
        device,
        format="v4l2",
        container_options={
            "framerate": str(fps),
            "video_size": video_size,
            "input_format": "mjpeg",
        },
    )
    sent = 0
    try:
        if not container.streams.video:
            raise LivepeerGatewayError(f"no video stream on {device}")
        pub = MediaPublish(publish_url, config=_lowlatency_config(fps))
        demux = container.demux(video=0)
        decode_errors = 0
        try:
            while True:
                if max_frames and sent >= max_frames:
                    break
                try:
                    packet = await asyncio.to_thread(next, demux)
                except StopIteration:
                    break
                try:
                    frames = packet.decode()
                except av.FFmpegError:
                    decode_errors += 1
                    if decode_errors > 300:
                        raise LivepeerGatewayError(
                            f"webcam {device}: too many decode errors"
                        )
                    continue
                for frame in frames:
                    if max_frames and sent >= max_frames:
                        break
                    frame.pts = None  # MediaPublish stamps wall-clock pts
                    await pub.write_frame(frame)
                    sent += 1
        finally:
            await pub.close()
    finally:
        container.close()
    return sent


async def _publish_webcam_macos(
    index: str, publish_url: str, *, fps: float, video_size: str, max_frames: int
) -> int:
    """macOS AVFoundation capture (video only). Capture at a supported rate, decimate to --fps."""
    options = {"framerate": f"{fps:g}"}
    if video_size and video_size != "640x480":
        options["video_size"] = video_size
    try:
        container = av.open(index, format="avfoundation", container_options=options)
    except OSError as exc:
        raise LivepeerGatewayError(
            f"AVFoundation rejected device {index!r} ({exc}); try omitting --video-size and "
            'check the index with `ffmpeg -f avfoundation -list_devices true -i ""`'
        ) from exc
    sent = 0
    try:
        if not container.streams.video:
            raise LivepeerGatewayError(
                f"no video stream on AVFoundation device {index}"
            )
        pub = MediaPublish(publish_url, config=_lowlatency_config(fps))
        demux = container.demux(video=0)
        min_interval = 1.0 / fps if fps > 0 else 0.0
        last_sent = 0.0
        try:
            while True:
                if max_frames and sent >= max_frames:
                    break
                try:
                    packet = await asyncio.to_thread(next, demux)
                except StopIteration:
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
                    demux = container.demux(video=0)
                    continue
                try:
                    frames = packet.decode()
                except av.FFmpegError:
                    continue
                for frame in frames:
                    now = time.monotonic()
                    if min_interval and now - last_sent < min_interval:
                        continue
                    last_sent = now
                    if max_frames and sent >= max_frames:
                        break
                    frame.pts = None
                    await pub.write_frame(frame)
                    sent += 1
        finally:
            await pub.close()
    finally:
        container.close()
    return sent


def _decode_frame_count(path: Path) -> int:
    try:
        c = av.open(str(path))
    except Exception as exc:
        _log(f"could not open output for decode: {exc}")
        return 0
    n = 0
    try:
        if not c.streams.video:
            return 0
        for _ in c.decode(video=0):
            n += 1
    except Exception as exc:
        _log(f"decode stopped after {n} frames: {exc}")
    finally:
        c.close()
    return n


async def run(args: argparse.Namespace) -> int:
    await _check_discovery(args.discovery)

    reprompts = _parse_reprompts(args.reprompt)
    use_webcam = args.webcam is not None or args.webcam_macos is not None
    stdin = (not use_webcam) and args.input is not None and args.input.strip() == "-"
    generated = (not use_webcam) and args.input is None
    max_frames = max(0, args.frames)

    input_path = None
    if generated:
        input_path = _make_test_clip(args.frames, args.fps)
    elif not use_webcam and not stdin:
        input_path = Path(args.input).expanduser()
        if not input_path.exists():
            raise SystemExit(f"input file does not exist: {input_path}")

    out_path = Path(args.output).expanduser()
    bytes_out = 0
    first_byte_at: float | None = None
    last_byte_at: float = 0.0

    def _on_bytes(chunk: bytes) -> None:
        nonlocal bytes_out, first_byte_at, last_byte_at
        if first_byte_at is None:
            first_byte_at = time.monotonic()
            _log(f"first output bytes after {first_byte_at - t0:.1f}s")
        last_byte_at = time.monotonic()
        bytes_out += len(chunk)
        fh.write(chunk)

    session = reprompt_task = stats_task = None
    t0 = time.monotonic()
    try:
        session = await reserve_session(discovery_url=args.discovery, app=APP_ID)
        _log(f"reserved session_id={session.session_id} app_url={session.app_url}")

        body: dict = {"prompt": args.prompt}
        if args.seed is not None:
            body["seed"] = args.seed
        if args.input_blend is not None:
            body["input_blend"] = args.input_blend
        resp = await post_json(f"{session.app_url.rstrip('/')}/stream", body)
        in_url, out_url = _channel_url(resp, "in"), _channel_url(resp, "out")
        _log(f"stream started  in={in_url}  out={out_url}")

        if reprompts:
            reprompt_task = asyncio.create_task(_reprompt(session.app_url, reprompts))
        if args.stats_interval > 0:
            stats_task = asyncio.create_task(
                _poll_stats(session.app_url, args.stats_interval)
            )

        with out_path.open("wb") as fh:  # noqa: F841 (used via closure)
            async with MediaOutput(out_url, on_bytes=_on_bytes, max_segments=2):
                if args.webcam_macos is not None:
                    _log(f"streaming macOS camera {args.webcam_macos}; ctrl-c to stop")
                    sent = await _publish_webcam_macos(
                        args.webcam_macos,
                        in_url,
                        fps=args.fps,
                        video_size=args.video_size,
                        max_frames=max_frames,
                    )
                elif args.webcam is not None:
                    _log(
                        f"streaming webcam {args.webcam} ({args.video_size}); ctrl-c to stop"
                    )
                    sent = await _publish_webcam(
                        args.webcam,
                        in_url,
                        fps=args.fps,
                        video_size=args.video_size,
                        max_frames=max_frames,
                    )
                else:
                    src = "-" if stdin else str(input_path)
                    sent = await _publish_file(
                        src, in_url, fps=args.fps, max_frames=max_frames
                    )
                _log(
                    f"published {sent} frames; draining output up to {args.drain:g}s..."
                )
                # Keep the subscription open so trailing/late segments (the runner
                # warms up and is slower than realtime) still arrive after publish.
                drain_deadline = time.monotonic() + args.drain
                while time.monotonic() < drain_deadline:
                    await asyncio.sleep(0.5)
                    # Stop early once output has arrived and then gone quiet for ~2s.
                    if bytes_out > 0 and time.monotonic() - last_byte_at > 2.0:
                        break
            fh.flush()

        stats = await _get_stats(session.app_url)
        if stats:
            _log("stats: " + json.dumps(stats, indent=2))

        frames_decoded = _decode_frame_count(out_path)
        elapsed = time.monotonic() - t0
        _log(
            f"received {bytes_out} bytes -> decoded {frames_decoded} frames in {elapsed:.1f}s"
        )

        ok = bytes_out > 0 and frames_decoded > 0
        print(
            "PASS" if ok else "FAIL",
            "flux-klein smoke test",
            f"(sent={sent} frames, out={bytes_out}B, decoded={frames_decoded} frames)",
        )
        return 0 if ok else 1
    finally:
        for task in (reprompt_task, stats_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if session is not None:
            with contextlib.suppress(Exception):
                await stop_runner_session(session)
        if generated:
            with contextlib.suppress(Exception):
                shutil.rmtree(input_path.parent, ignore_errors=True)
        if not args.keep_output:
            with contextlib.suppress(Exception):
                out_path.unlink()


def main() -> None:
    args = _parse_args()
    try:
        rc = asyncio.run(asyncio.wait_for(run(args), timeout=args.timeout))
    except asyncio.TimeoutError:
        _log(f"FAIL: exceeded --timeout {args.timeout}s")
        rc = 1
    except LivepeerGatewayError as exc:
        _log(f"FAIL: {exc}")
        rc = 1
    except KeyboardInterrupt:
        _log("interrupted")
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
