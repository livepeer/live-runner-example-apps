#!/usr/bin/env python3
"""Realtime multi-rendition transcoding over trickle — a Live Runner app.

The live pattern (Josh's `echo` example) extended two ways:
  * multi-rendition: the client requests a profile ladder; the worker decodes
    the input once and fans out to one output channel per rendition.
  * multi-session: the worker keeps a per-session pipeline and registers with
    capacity > 1, so one orchestrator can host several concurrent streams.

PyAV does the decode/encode (ffmpeg under the hood). Video-only, like `echo`.

Flow per session:
  client POSTs /transcode {"profiles": [{"name":"720p","height":720}, ...]}
  -> worker creates an `in` channel + one channel per profile
  -> MediaOutput decodes `in`; each frame is rescaled per profile and
     re-encoded to that profile's channel via MediaPublish
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field

import av
from aiohttp import web

from livepeer_gateway.live_runner import create_trickle_channels, register_runner
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish, MediaPublishConfig, VideoOutputConfig

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8990
APP_ID = "transcode/live-h264"

log = logging.getLogger("live-transcode")
sessions: "dict[str, Pipeline]" = {}


@dataclass
class Pipeline:
    session_id: str
    in_url: str                      # public url for the client to publish to
    outputs: dict[str, str]          # profile name -> public url
    output: MediaOutput
    publishers: dict[str, MediaPublish] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        return {"session": self.session_id, "in": self.in_url, "outputs": self.outputs}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Realtime multi-rendition trickle transcoder.")
    parser.add_argument("--orchestrator", default="http://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers).")
    parser.add_argument("--height", type=int, default=360, help="Default rendition height when the client sends none.")
    parser.add_argument("--capacity", type=int, default=4, help="Max concurrent transcode sessions.")
    parser.add_argument("--price", type=int, default=0, help="Price in USD per pixels-per-unit (0 = free).")
    parser.add_argument("--pixels-per-unit", type=int, default=1, help="Scale factor for the price.")
    return parser.parse_args()


def _resize(frame: av.VideoFrame, height: int) -> av.VideoFrame:
    if frame.height == height:
        return frame
    width = max(2, round(frame.width * height / frame.height) & ~1)  # even width, keep aspect
    out = frame.reformat(width=width, height=height)
    out.pts = frame.pts
    out.time_base = frame.time_base
    return out


def _profiles_from_payload(payload: dict, default_height: int) -> list[dict]:
    raw = payload.get("profiles")
    if not raw:
        raw = [{"height": default_height}]
    profiles = []
    for p in raw:
        height = int(p["height"])
        profiles.append({
            "name": str(p.get("name") or f"{height}p"),
            "height": height,
            "fps": p.get("fps"),                       # None -> keep source fps
            "codec": str(p.get("codec") or "libx264"),
            # NB: bitrate is intentionally not here -- the SDK's VideoOutputConfig
            # has no bit_rate field yet, so per-rendition bitrate can't be honored.
        })
    return profiles


def _publisher_for(url: str, profile: dict) -> MediaPublish:
    fps = profile.get("fps")
    config = MediaPublishConfig(tracks=[VideoOutputConfig(
        fps=float(fps) if fps else None,
        codec=profile["codec"],
    )])
    return MediaPublish(url, config=config)


async def _close_pipeline(session_id: str) -> None:
    pipeline = sessions.pop(session_id, None)
    if pipeline is None:
        return
    for pub in pipeline.publishers.values():
        with suppress(Exception):
            await pub.close()
    with suppress(Exception):
        await pipeline.output.close()
    log.info("closed session %s", session_id)


async def _handle_transcode(request: web.Request) -> web.Response:
    session_id = request.headers.get("Livepeer-Session-Id", "").strip()
    if not session_id:
        raise web.HTTPBadRequest(text="missing Livepeer-Session-Id header")
    if session_id in sessions:
        return web.json_response(sessions[session_id].to_json())

    payload = json.loads(await request.read() or "{}")
    profiles = _profiles_from_payload(payload, request.app["height"])

    # in channel + one output channel per rendition. Use internal_url for our own
    # pub/sub (resolvable in-container) and the public url for the client; create
    # via the known orchestrator_url, not the request's 127.0.0.1 control header.
    channel_reqs = [{"name": "in", "mime_type": "video/mp2t"}]
    channel_reqs += [{"name": p["name"], "mime_type": "video/mp2t"} for p in profiles]
    channels = await create_trickle_channels(
        session_id,
        channel_reqs,
        orchestrator_url=request.app["args"].orchestrator,
        runner_id=request.headers.get("Livepeer-Runner-Route", "").strip(),
        session_token=request.headers.get("Livepeer-Session-Token", "").strip(),
    )
    by_name = {c["name"]: c for c in channels}

    def _internal(name: str) -> str:
        return by_name[name].get("internal_url") or by_name[name]["url"]

    publishers = {p["name"]: _publisher_for(_internal(p["name"]), p) for p in profiles}
    last_pub: dict[str, float] = {}  # per-profile last published pts time, for fps drop

    async def _on_frame(decoded) -> None:
        if decoded.kind != "video":
            return  # video-only, like echo
        frame = decoded.frame
        pts_time = float(frame.pts * frame.time_base) if frame.pts is not None and frame.time_base else None
        for p in profiles:
            fps = p.get("fps")
            if fps and pts_time is not None:
                # drop frames to hit the target fps (the encoder fps is only a hint)
                last = last_pub.get(p["name"])
                if last is not None and pts_time - last < (1.0 / float(fps)) - 1e-6:
                    continue
                last_pub[p["name"]] = pts_time
            await publishers[p["name"]].write_frame(_resize(frame, p["height"]))

    output = MediaOutput(_internal("in"), on_frame=_on_frame)
    pipeline = Pipeline(
        session_id=session_id,
        in_url=by_name["in"]["url"],
        outputs={p["name"]: by_name[p["name"]]["url"] for p in profiles},
        output=output,
        publishers=publishers,
    )
    sessions[session_id] = pipeline
    for task in output.callback_tasks():
        task.add_done_callback(lambda _t, sid=session_id: asyncio.create_task(_close_pipeline(sid)))
    log.info("session %s -> %s (%d active)", session_id, [p["name"] for p in profiles], len(sessions))
    return web.json_response(pipeline.to_json())


async def _on_startup(app: web.Application) -> None:
    args = app["args"]
    registration = await register_runner(
        args.orchestrator, secret=args.orchSecret, runner_url=args.runner_url, app=APP_ID,
        capacity=args.capacity, price_per_unit=args.price, pixels_per_unit=args.pixels_per_unit,
    )
    app["registration"] = registration
    log.info("registered runner_id=%s app=%s capacity=%d", registration.runner_id, APP_ID, args.capacity)


async def _on_cleanup(app: web.Application) -> None:
    for sid in list(sessions):
        await _close_pipeline(sid)
    with suppress(Exception):
        await app["registration"].close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    app = web.Application()
    app["args"] = args
    app["height"] = args.height
    app.router.add_post("/transcode", _handle_transcode)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT, print=None)


if __name__ == "__main__":
    main()
