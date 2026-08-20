#!/usr/bin/env python3
"""Unified transcoding Live Runner — one container, batch (VOD) + live (trickle).

Replaces the separate `transcode-vod` and `transcode-live` examples with a single
image that serves both surfaces off one ffmpeg engine, using go-livepeer's native
`{"profiles": [...]}` contract so it drops in for the native transcoding pipeline:

  POST /transcode        batch/VOD  — a file/URL in, one rendition per profile out
  POST /transcode/live   live       — trickle: create `in` + one channel per profile

Both take the same native profile ladder (see profiles.py). The app self-registers
(dynamic) with capacity > 1, so one orchestrator hosts several concurrent jobs.

Batch wire protocol on POST /transcode:
  request : {"input_url"|"video_b64": ..., "profiles": [<JsonProfile>, ...],
             "output_urls": {"<name>": "<presigned PUT url>", ...}}   # output_urls optional
  response: {"renditions": [{"name","width","height","encoder","bytes",
             "output_url"|"output_b64"}, ...]}

Use input_url/output_urls (object storage) at scale — base64 is only for small
test clips (it inflates ~33% and buffers the whole file in memory).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import itertools
import json
import logging
import os
import subprocess
import tempfile
from contextlib import suppress

import av  # noqa: F401 -- used in the _resize type annotation
import engine
import profiles as prof
from aiohttp import ClientSession, ClientTimeout, web

from livepeer_gateway.live_runner import LiveRunnerGPU, create_trickle_channels, register_runner
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish, MediaPublishConfig, VideoOutputConfig

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8990
APP_ID = "transcode/ffmpeg"

log = logging.getLogger("transcode")
sessions: dict = {}


def _detect_gpu_count() -> int:
    """Count visible NVIDIA GPUs so batch jobs can round-robin across all of them.

    Without this, every job lands on whatever device index the encoder library
    picks by default (typically 0), leaving the rest of a multi-GPU box idle.
    """
    try:
        out = subprocess.run(["nvidia-smi", "-L"], check=True, capture_output=True, text=True)
        n = len([l for l in out.stdout.splitlines() if l.strip()])
        return n or 1
    except Exception:
        return 1


GPU_COUNT = _detect_gpu_count()
_gpu_cycle = itertools.cycle(range(GPU_COUNT))


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Unified batch + live transcoding Live Runner.")
    p.add_argument("--orchestrator", default="http://localhost:8935")
    p.add_argument("--orchSecret", default="abcdef")
    p.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    p.add_argument("--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers).")
    p.add_argument("--capacity", type=int, default=4, help="Max concurrent jobs.")
    p.add_argument("--av1-encoder", default="libsvtav1", help="Encoder for encoder=AV1 (libsvtav1 cpu, av1_nvenc/av1_vaapi gpu).")
    p.add_argument("--h264-encoder", default="libx264", help="Encoder for encoder=H264/default (libx264 cpu, h264_nvenc/h264_vaapi gpu).")
    p.add_argument("--h265-encoder", default="libx265", help="Encoder for encoder=H265/HEVC (libx265 cpu, hevc_nvenc/hevc_vaapi gpu).")
    p.add_argument("--price", type=int, default=0, help="Price in USD per pixels-per-unit (0 = free).")
    p.add_argument("--pixels-per-unit", type=int, default=1, help="Scale factor for the price.")
    p.add_argument("--no-register", action="store_true",
                   help="Don't self-register; attach via runners.json (static) — the orchestrator health-polls /healthz.")
    return p.parse_args()


# ------------------------------------------------------------------ batch (VOD)

async def _download(url: str, dst: str) -> None:
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=30)) as s:
        async with s.get(url) as r:
            r.raise_for_status()
            with open(dst, "wb") as f:
                async for chunk in r.content.iter_chunked(1 << 20):
                    f.write(chunk)


async def _upload(url: str, src: str) -> None:
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=30)) as s:
        with open(src, "rb") as f:
            async with s.put(url, data=f) as r:
                r.raise_for_status()


async def _handle_batch(request: web.Request) -> web.Response:
    payload = await request.json()
    raw_profiles = payload.get("profiles")
    if not raw_profiles and payload.get("height"):
        raw_profiles = [{"height": int(payload["height"])}]  # single-rendition shorthand
    if not raw_profiles:
        raise web.HTTPBadRequest(text="missing 'profiles'")
    output_urls = payload.get("output_urls") or {}

    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "in")
        if payload.get("input_url"):
            await _download(payload["input_url"], src)
        elif payload.get("video_b64"):
            with open(src, "wb") as f:
                f.write(base64.b64decode(payload["video_b64"]))
        else:
            raise web.HTTPBadRequest(text="need 'input_url' or 'video_b64'")

        out_dir = os.path.join(d, "out")
        os.makedirs(out_dir, exist_ok=True)
        gpu_index = next(_gpu_cycle)
        log.info("batch: transcoding on gpu %d/%d", gpu_index, GPU_COUNT)
        try:
            renditions = await asyncio.to_thread(
                engine.transcode_file, src, raw_profiles, out_dir,
                request.app["av1_encoder"], request.app["h264_encoder"], request.app["h265_encoder"], gpu_index,
            )
        except (RuntimeError, ValueError) as exc:
            return web.json_response({"error": str(exc)}, status=400)

        result = []
        for r in renditions:
            entry = {k: r[k] for k in ("name", "ext", "width", "height", "encoder", "bytes")}
            if r["name"] in output_urls:
                await _upload(output_urls[r["name"]], r["path"])
                entry["output_url"] = output_urls[r["name"]]
            else:
                with open(r["path"], "rb") as f:
                    entry["output_b64"] = base64.b64encode(f.read()).decode()
            result.append(entry)
    log.info("batch: %d renditions %s", len(result), [r["name"] for r in result])
    return web.json_response({"renditions": result})


# ------------------------------------------------------------------ live (trickle)
# Ported from the transcode-live example, using the native profile contract.

def _resize(frame: "av.VideoFrame", height: int, width: int) -> "av.VideoFrame":
    if width <= 0:
        width = max(2, round(frame.width * height / frame.height) & ~1)
    if height <= 0:
        height = max(2, round(frame.height * width / frame.width) & ~1)
    if frame.height == height and frame.width == width:
        return frame
    out = frame.reformat(width=width, height=height)
    out.pts, out.time_base = frame.pts, frame.time_base
    return out


async def _handle_live(request: web.Request) -> web.Response:
    session_id = request.headers.get("Livepeer-Session-Id", "").strip()
    if not session_id:
        raise web.HTTPBadRequest(text="missing Livepeer-Session-Id header")
    if session_id in sessions:
        return web.json_response(sessions[session_id])

    payload = json.loads(await request.read() or "{}")
    raw = payload.get("profiles") or [{"height": 360}]
    profs = [prof.normalize(p, av1_encoder=request.app["av1_encoder"],
                             h264_encoder=request.app["h264_encoder"],
                             h265_encoder=request.app["h265_encoder"]) for p in raw]

    reqs = [{"name": "in", "mime_type": "video/mp2t"}]
    reqs += [{"name": p["name"], "mime_type": "video/mp2t"} for p in profs]
    channels = await create_trickle_channels(
        session_id, reqs,
        orchestrator_url=request.app["args"].orchestrator,
        runner_id=request.headers.get("Livepeer-Runner-Route", "").strip(),
        session_token=request.headers.get("Livepeer-Session-Token", "").strip(),
    )
    by_name = {c["name"]: c for c in channels}

    def internal(name: str) -> str:  # our own pub/sub uses the in-container url
        return by_name[name].get("internal_url") or by_name[name]["url"]

    # Honors fps + codec + bitrate + profile from the native contract (bitrate/
    # profile need the SDK's per-track encoder-options support — livepeer-python-
    # gateway#35, pinned in pyproject/Dockerfile). gop is still segment-driven.
    publishers = {
        p["name"]: MediaPublish(internal(p["name"]), config=MediaPublishConfig(tracks=[
            VideoOutputConfig(
                fps=float(p["fps"]) if p["fps"] else None,
                codec=p["encoder"],
                bit_rate=p["bitrate"] or None,
                profile=prof.ffmpeg_profile(p),
            )
        ]))
        for p in profs
    }
    last_pub: dict[str, float] = {}

    async def _on_frame(decoded) -> None:
        if decoded.kind != "video":
            return
        frame = decoded.frame
        t = float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base else None
        for p in profs:
            if p["fps"] and t is not None:
                last = last_pub.get(p["name"])
                if last is not None and t - last < (1.0 / p["fps"]) - 1e-6:
                    continue
                last_pub[p["name"]] = t
            await publishers[p["name"]].write_frame(_resize(frame, p["height"], p["width"]))

    output = MediaOutput(internal("in"), on_frame=_on_frame)
    info = {"session": session_id, "in": by_name["in"]["url"],
            "outputs": {p["name"]: by_name[p["name"]]["url"] for p in profs}}
    sessions[session_id] = info

    async def _close(sid: str) -> None:
        sessions.pop(sid, None)
        for pub in publishers.values():
            with suppress(Exception):
                await pub.close()
        with suppress(Exception):
            await output.close()
        log.info("closed live session %s", sid)

    for task in output.callback_tasks():
        task.add_done_callback(lambda _t, sid=session_id: asyncio.create_task(_close(sid)))
    log.info("live session %s -> %s", session_id, [p["name"] for p in profs])
    return web.json_response(info)


# ------------------------------------------------------------------ health

async def _handle_health(request: web.Request) -> web.Response:
    # Static registration health-polls this; dynamic doesn't need it but it's cheap.
    return web.json_response({
        "status": "ok",
        "app": APP_ID,
        "capacity": request.app["args"].capacity,
        "sessions": len(sessions),
    })


# ------------------------------------------------------------------ app

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()

    async def _on_startup(app: web.Application) -> None:
        if args.no_register:
            log.info("static mode: not self-registering; attach via runners.json (health_url=/healthz)")
            return
        # register_runner auto-detects only NVIDIA GPUs (pynvml/torch/nvidia-smi),
        # so a non-NVIDIA GPU (e.g. Intel Arc on the VA-API path) is advertised to
        # discovery via env instead — otherwise the runner shows with no GPU.
        gpu = None
        if os.environ.get("RUNNER_GPU_NAME"):
            gpu = LiveRunnerGPU(
                id=os.environ.get("RUNNER_GPU_ID", ""),
                name=os.environ["RUNNER_GPU_NAME"],
                vram_mb=int(os.environ.get("RUNNER_GPU_VRAM_MB", "0") or 0),
            )
        app["registration"] = await register_runner(
            args.orchestrator, secret=args.orchSecret, runner_url=args.runner_url, app=APP_ID,
            capacity=args.capacity, price_per_unit=args.price, pixels_per_unit=args.pixels_per_unit,
            gpu=gpu, auto_detect_gpu=(gpu is None),
        )
        log.info("registered runner_id=%s app=%s capacity=%d", app["registration"].runner_id, APP_ID, args.capacity)

    async def _on_cleanup(app: web.Application) -> None:
        if app.get("registration"):
            with suppress(Exception):
                await app["registration"].close()

    app = web.Application(client_max_size=1024 * 1024 * 1024)  # 1 GiB (base64 test clips)
    app["args"] = args
    app["av1_encoder"] = args.av1_encoder
    app["h264_encoder"] = args.h264_encoder
    app["h265_encoder"] = args.h265_encoder
    app.router.add_get("/healthz", _handle_health)
    app.router.add_post("/transcode", _handle_batch)
    app.router.add_post("/transcode/live", _handle_live)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT, print=None)


if __name__ == "__main__":
    main()
