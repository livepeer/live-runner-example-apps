#!/usr/bin/env python3
# Client for the self-hosted realtime FLUX Klein app (new runner).
#
# Routed by app id like the `echo` example, so it uses the same flow
# (reserve_session + /stream), NOT the lv2v capability flow. Offchain/free
# against your local orchestrator; pass --signer for the on-chain (paid) path.
#
# Publishes a video file or a Linux v4l2 webcam into the trickle `in` channel,
# reads the FLUX-transformed output from `out`, and can re-prompt live via /update.
from __future__ import annotations

import argparse
import asyncio
import sys
import time
from contextlib import nullcontext, suppress
from pathlib import Path

import av

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.http import post_json
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish, MediaPublishConfig, VideoOutputConfig
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/flux-klein"
DEFAULT_OUTPUT = "flux-klein-out.ts"
DEFAULT_PROMPT = "a psychedelic landscape, vivid colors, intricate details"
DEFAULT_VIDEO_SIZE = "640x480"


def _log(*args: object) -> None:
    print(*args, file=sys.stderr)


def _lowlatency_publish_config(fps: float = 30.0) -> MediaPublishConfig:
    # Trickle segments rotate on keyframes; the 2s default GOP makes each segment
    # ~2s of video, which dominates end-to-end latency. Short GOP + segment floor
    # flush segments ~4x/sec for near-realtime (at the cost of more keyframes).
    return MediaPublishConfig(
        tracks=[VideoOutputConfig(fps=fps, keyframe_interval_s=0.25)],
        min_segment_wallclock_s=0.25,
    )


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run video through the self-hosted FLUX Klein app.")
    p.add_argument("input", nargs="?", default=None, help="Local mp4/mov. Omit and pass --webcam for the camera.")
    p.add_argument("--webcam", nargs="?", const="/dev/video0", default=None, metavar="DEVICE",
                   help="Capture from a Linux v4l2 webcam (default /dev/video0).")
    p.add_argument("--webcam-macos", nargs="?", const="0", default=None, metavar="INDEX",
                   help="Capture from a macOS AVFoundation camera by index (default 0). "
                        "List devices with: ffmpeg -f avfoundation -list_devices true -i \"\"")
    p.add_argument("--fps", type=float, default=30.0,
                   help="Publish rate. Frames are decimated client-side to hit it (macOS capture "
                        "runs at --capture-fps; files keep their own pacing). Setting this near the "
                        "runner's inference fps avoids sending frames it would only drop.")
    p.add_argument("--capture-fps", type=float, default=30.0,
                   help="macOS capture framerate — must be a mode the camera supports (almost always 30). "
                        "Don't confuse with --fps: AVFoundation rejects unsupported rates outright.")
    p.add_argument("--video-size", default=DEFAULT_VIDEO_SIZE, help="Webcam capture size (default 640x480, a near-universal native size; the app resizes to its engine size). On macOS this is only applied if you override it (newer Macs don't offer 640x480); otherwise the camera's native size is used.")
    p.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--output", default=DEFAULT_OUTPUT, help="Output file, or '-' for stdout (pipe to ffplay).")
    p.add_argument("--max-frames", type=int, default=0)
    p.add_argument("--reprompt", action="append", default=[], metavar="SECONDS=PROMPT",
                   help="Live prompt change, e.g. --reprompt 5=an oil painting. Repeatable. "
                        "Also accepts seed changes (--reprompt \"15=seed:42\"; seed:-1 = random) "
                        "and camera-blend changes (--reprompt \"20=blend:0.7\").")
    p.add_argument("--seed", type=int, default=None,
                   help="Noise seed. -1 = fresh random noise every frame (shimmery); any fixed "
                        "value reuses the same noise each frame (steadier, reproducible). "
                        "Omit to keep the runner's default.")
    p.add_argument("--input-blend", type=float, default=None,
                   help="Camera weight 0..1 in the feedback blend. Higher = sticks closer to the "
                        "real feed (less fantasy drift), lower = prompt takes over. "
                        "Omit to keep the runner's default (0.5).")
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
        # "seed:N" / "blend:X" entries change parameters instead of the prompt.
        if prompt.startswith("seed:"):
            payload: dict = {"seed": int(prompt.partition(":")[2])}
        elif prompt.startswith("blend:"):
            payload = {"input_blend": float(prompt.partition(":")[2])}
        else:
            payload = {"prompt": prompt}
        with suppress(Exception):
            await post_json(f"{app_url.rstrip('/')}/update", payload)
            _log(f"updated at {at:.1f}s: {payload}")


async def _publish_file(input_path: Path, publish_url: str, *, fps: float = 0.0,
                        max_frames: int = 0) -> None:
    input_ = av.open(str(input_path))
    try:
        if not input_.streams.video:
            raise LivepeerGatewayError(f"No video stream in {input_path}")
        src_fps = float(input_.streams.video[0].average_rate or 30.0)
        # Publishing faster than the runner can infer just fills the latest-frame
        # slot with frames it will drop. Decimating to ~the inference rate means
        # (almost) every published frame is actually transformed. Playback stays
        # paced to the source timestamps either way — this changes which frames are
        # sent, not how long the file takes.
        stride = max(1, round(src_fps / fps)) if fps > 0 else 1
        track_fps = fps if fps > 0 else src_fps
        if stride > 1:
            _log(f"source {src_fps:.1f} fps -> publishing every {stride} frames (~{track_fps:.1f} fps)")
        publisher = MediaPublish(publish_url, config=_lowlatency_publish_config(track_fps))
        prev_pts_time = prev_wall = None
        sent = 0
        try:
            for index, frame in enumerate(input_.decode(video=0), start=1):
                if (index - 1) % stride:
                    continue
                sent += 1
                if max_frames > 0 and sent > max_frames:
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
    input_ = av.open(device, format="v4l2",
                     container_options={"framerate": str(fps), "video_size": video_size, "input_format": "mjpeg"})
    try:
        if not input_.streams.video:
            raise LivepeerGatewayError(f"No video stream on {device}")
        publisher = MediaPublish(publish_url, config=_lowlatency_publish_config(fps))
        try:
            index = 0
            decode_errors = 0
            done = False
            # Read packets in a thread: v4l2 demux() blocks (~1/fps per packet) in C,
            # which would freeze the asyncio loop and starve MediaPublish's background
            # encode/POST task. We also skip the occasional malformed packet v4l2 emits
            # (notably the first) that the decoder rejects.
            demux = input_.demux(video=0)
            while not done:
                try:
                    packet = await asyncio.to_thread(next, demux)
                except StopIteration:
                    break
                try:
                    frames = packet.decode()
                except av.FFmpegError:
                    decode_errors += 1
                    if decode_errors > 300:
                        raise LivepeerGatewayError(f"webcam {device}: too many decode errors, giving up")
                    continue
                for frame in frames:
                    index += 1
                    if max_frames > 0 and index > max_frames:
                        done = True
                        break
                    frame.pts = None  # let MediaPublish stamp wall-clock pts
                    await publisher.write_frame(frame)
        finally:
            await publisher.close()
    finally:
        input_.close()


async def _publish_webcam_macos(device: str, publish_url: str, *, fps: float, video_size: str,
                                capture_fps: float = 30.0, max_frames: int = 0) -> None:
    # macOS AVFoundation capture. `device` is a video device index (see
    # `ffmpeg -f avfoundation -list_devices true -i ""`), e.g. "0" for the
    # built-in FaceTime camera. Video only (no audio).
    #
    # AVFoundation only accepts capture framerates the camera natively supports:
    # omitting the option makes ffmpeg request 29.97 (NTSC), which Mac cameras
    # reject, and arbitrary values like 10 fail the same way — both with
    # "[Errno 5] Input/output error". So capture at a supported rate (30 nearly
    # everywhere; override with --capture-fps) and decimate to --fps client-side.
    options = {"framerate": f"{capture_fps:g}"}
    # Newer Macs' cameras don't offer 640x480; only force a size if the user
    # overrode the default, else let AVFoundation pick the native mode.
    if video_size and video_size != DEFAULT_VIDEO_SIZE:
        options["video_size"] = video_size
    try:
        input_ = av.open(device, format="avfoundation", container_options=options)
    except OSError as exc:
        raise LivepeerGatewayError(
            f"AVFoundation rejected the capture config for device {device!r} ({exc}). "
            "The camera only accepts specific modes: try --capture-fps 30 and omit "
            "--video-size; check the device index with "
            "`ffmpeg -f avfoundation -list_devices true -i \"\"`"
        ) from exc
    try:
        if not input_.streams.video:
            raise LivepeerGatewayError(f"No video stream on AVFoundation device {device}")
        publisher = MediaPublish(publish_url, config=_lowlatency_publish_config(fps))
        try:
            index = 0
            done = False
            eagain = 0
            min_interval = 1.0 / fps if fps > 0 else 0.0
            last_sent = 0.0
            # Same threaded-demux reasoning as the v4l2 path: the capture read
            # blocks in C, so offload it to keep MediaPublish's encode/POST task alive.
            demux = input_.demux(video=0)
            while not done:
                try:
                    packet = await asyncio.to_thread(next, demux)
                    eagain = 0
                except StopIteration:
                    break
                except BlockingIOError:
                    # AVFoundation returns EAGAIN until the camera warms up, and
                    # PyAV kills the demux generator on it — back off and recreate.
                    eagain += 1
                    if eagain > 500:  # ~5s of nothing
                        raise LivepeerGatewayError(
                            f"AVFoundation device {device!r} produced no frames in ~5s — check "
                            "camera permission (System Settings > Privacy & Security > Camera) "
                            "and that the device index is correct")
                    await asyncio.sleep(0.01)
                    demux = input_.demux(video=0)
                    continue
                try:
                    frames = packet.decode()
                except av.FFmpegError:
                    continue
                for frame in frames:
                    now = time.monotonic()
                    if min_interval and now - last_sent < min_interval:
                        continue  # decimate down to the target --fps
                    last_sent = now
                    index += 1
                    if max_frames > 0 and index > max_frames:
                        done = True
                        break
                    frame.pts = None  # let MediaPublish stamp wall-clock pts
                    await publisher.write_frame(frame)
        finally:
            await publisher.close()
    finally:
        input_.close()


async def main() -> None:
    args = _parse_args()
    use_webcam = args.webcam is not None or args.webcam_macos is not None
    if not use_webcam and not args.input:
        raise SystemExit("provide an input file, --webcam (Linux), or --webcam-macos")
    input_path = None
    if not use_webcam:
        input_path = Path(args.input).expanduser()
        if not input_path.exists():
            raise SystemExit(f"input file does not exist: {input_path}")
    reprompts = _parse_reprompts(args.reprompt)

    output_stdout = args.output.strip().lower() in {"-", "stdout"}
    output_path = None if output_stdout else Path(args.output).expanduser()
    signer_url = args.signer.strip() or None

    session = reprompt_task = None
    try:
        session = await reserve_session(
            discovery_url=args.discovery, app=APP_ID, signer_url=signer_url,
            payment_interval=args.payment_interval,
        )
        session.start_payments()  # on-chain: keep the session funded for the whole stream; no-op offchain
        _log("session_id:", session.session_id, "app_url:", session.app_url)

        stream_body: dict = {"prompt": args.prompt}
        if args.seed is not None:
            stream_body["seed"] = args.seed
        if args.input_blend is not None:
            stream_body["input_blend"] = args.input_blend
        resp = await post_json(f"{session.app_url.rstrip('/')}/stream", stream_body)
        in_url, out_url = _channel_url(resp, "in"), _channel_url(resp, "out")
        _log("in:", in_url, "out:", out_url)
        if reprompts:
            reprompt_task = asyncio.create_task(_reprompt(session.app_url, reprompts))

        with nullcontext(sys.stdout.buffer) if output_stdout else output_path.open("wb") as fh:
            viewer_gone = False

            def _write_chunk(chunk: bytes) -> None:
                # The viewer (mpv/ffplay) quitting breaks the stdout pipe; stop
                # writing quietly instead of crashing the whole client.
                nonlocal viewer_gone
                if viewer_gone:
                    return
                try:
                    fh.write(chunk)
                    if output_stdout:
                        fh.flush()
                except BrokenPipeError:
                    viewer_gone = True
                    _log("viewer closed the pipe; dropping further output (ctrl-c to stop)")

            # Small window + skip-to-latest so the viewer stays on the live edge
            # instead of buffering a growing backlog (keeps latency from creeping up).
            async with MediaOutput(out_url, on_bytes=_write_chunk, max_segments=2):
                if args.webcam_macos is not None:
                    _log(f"streaming macOS camera {args.webcam_macos}; ctrl-c to stop")
                    await _publish_webcam_macos(args.webcam_macos, in_url, fps=args.fps,
                                                video_size=args.video_size,
                                                capture_fps=args.capture_fps,
                                                max_frames=max(0, args.max_frames))
                elif args.webcam is not None:
                    _log(f"streaming webcam {args.webcam} ({args.video_size}); ctrl-c to stop")
                    await _publish_webcam(args.webcam, in_url, fps=args.fps,
                                          video_size=args.video_size, max_frames=max(0, args.max_frames))
                else:
                    await _publish_file(input_path, in_url, fps=args.fps,
                                        max_frames=max(0, args.max_frames))
                _log("publish complete; waiting for output to drain...")
            with suppress(BrokenPipeError):
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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _log("interrupted; session closed")
