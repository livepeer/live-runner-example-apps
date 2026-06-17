#!/usr/bin/env python3
"""OBS/RTMP -> live transcode -> HLS media gateway.

The operator-side piece that turns the trickle transcoder into a real live
service: it accepts an RTMP push (OBS), publishes the frames over trickle to the
transcoder, muxes the transcoded output to HLS, and serves it over HTTP. The
Livepeer SDK (discovery / session / payment / trickle) is hidden behind
RTMP-in / HLS-out — exactly what a streaming operator runs. Broadcasters push
RTMP and viewers open an HLS URL; neither touches the SDK.

    uv run gateway.py --discovery https://localhost:8935/discovery --height 360
    # OBS: Settings > Stream > Service: Custom
    #      Server:     rtmp://localhost:1935/live
    #      Stream Key: anything
    # Watch: http://localhost:8080/index.m3u8  (ffplay or any HLS player)

Single rendition, video-only — the smallest end-to-end live demo. Production
would add a rendition ladder + master playlist, audio, and a real RTMP server.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import queue
import subprocess
import threading
from contextlib import suppress

import av
from aiohttp import web

from livepeer_gateway.http import post_json
from livepeer_gateway.live_runner import stop_runner_session
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish
from livepeer_gateway.selection import reserve_session

APP_ID = "transcode/live-h264"
log = logging.getLogger("live-gateway")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="OBS/RTMP -> transcode -> HLS gateway.")
    parser.add_argument("--discovery", default="https://localhost:8935/discovery")
    parser.add_argument("--signer", default="", help="Remote signer base URL; omit for offchain.")
    parser.add_argument("--rtmp", default="rtmp://0.0.0.0:1935/live", help="RTMP address to listen on.")
    parser.add_argument("--height", type=int, default=360, help="Output rendition height.")
    parser.add_argument("--hls-port", type=int, default=8080)
    parser.add_argument("--hls-dir", default="hls")
    return parser.parse_args()


def _start_hls_muxer(hls_dir: str) -> subprocess.Popen:
    # Read transcoded MPEG-TS from stdin and segment it to HLS (copy, no re-encode).
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", "pipe:0", "-c", "copy",
         "-f", "hls", "-hls_time", "2", "-hls_list_size", "6",
         "-hls_flags", "delete_segments+append_list+omit_endlist",
         os.path.join(hls_dir, "index.m3u8")],
        stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


async def _ingest_once(args, signer_url) -> None:
    log.info("waiting for an RTMP push on %s ...", args.rtmp)
    container = av.open(args.rtmp, options={"listen": "1"})  # blocks until a publisher connects
    log.info("ingest connected")
    session = muxer = None
    # Feed the transcoded byte stream to ffmpeg's stdin from a thread so a slow
    # pipe never blocks the event loop.
    q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=256)

    def _pump(proc: subprocess.Popen) -> None:
        while True:
            chunk = q.get()
            if chunk is None:
                break
            with suppress(Exception):
                proc.stdin.write(chunk)

    try:
        session = await reserve_session(discovery_url=args.discovery, app=APP_ID, signer_url=signer_url)
        resp = await post_json(f"{session.app_url.rstrip('/')}/transcode",
                               {"profiles": [{"name": f"{args.height}p", "height": args.height}]})
        out_url = next(iter(resp["outputs"].values()))
        muxer = _start_hls_muxer(args.hls_dir)
        threading.Thread(target=_pump, args=(muxer,), daemon=True).start()
        log.info("serving HLS; transcoded -> %s/index.m3u8", args.hls_dir)

        async with MediaOutput(out_url, on_bytes=q.put):
            publisher = MediaPublish(resp["in"])
            try:
                for frame in container.decode(video=0):
                    await publisher.write_frame(frame)   # source frames; the worker resizes
            finally:
                await publisher.close()
    finally:
        q.put(None)
        if muxer is not None:
            with suppress(Exception):
                muxer.stdin.close()
            with suppress(Exception):
                muxer.wait(timeout=5)
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)
        with suppress(Exception):
            container.close()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    signer_url = args.signer.strip() or None
    os.makedirs(args.hls_dir, exist_ok=True)

    app = web.Application()
    app.router.add_static("/", args.hls_dir, show_index=True)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", args.hls_port).start()
    log.info("HLS at http://localhost:%d/index.m3u8 — push RTMP to %s", args.hls_port, args.rtmp)

    try:
        while True:  # serve one push at a time; loop for reconnects
            await _ingest_once(args, signer_url)
            log.info("stream ended; waiting for the next push")
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        asyncio.run(main())
