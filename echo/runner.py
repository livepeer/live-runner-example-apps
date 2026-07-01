#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import av
from aiohttp import web

from livepeer_gateway.live_runner import register_runner
from livepeer_gateway.media_decode import AudioDecodedMediaFrame, VideoDecodedMediaFrame
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish

log = logging.getLogger("echo")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
MODES = frozenset({"echo", "gray", "invert", "blur"})

state: "EchoSession | None" = None


@dataclass
class ModeState:
    mode: str = "echo"
    radius: int = 7


@dataclass
class EchoSession:
    session_id: str
    in_url: str
    out_url: str
    mode: ModeState
    output: MediaOutput
    publisher: MediaPublish

    def to_json(self) -> dict[str, Any]:
        data = {
            "session": self.session_id,
            "in": self.in_url,
            "out": self.out_url,
            "mode": self.mode.mode,
        }
        if self.mode.mode == "blur":
            data["radius"] = self.mode.radius
        return data


async def _close_pipeline() -> None:
    global state
    if state is None:
        return
    current = state
    state = None
    with suppress(Exception):
        await current.publisher.close()
    with suppress(Exception):
        await current.output.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Runner echo app demo.")
    parser.add_argument("--orchestrator", default="http://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument("--host", default=DEFAULT_HOST)
    return parser.parse_args()


def _session_id(request: web.Request) -> str:
    session_id = request.headers.get("Livepeer-Session-Id", "").strip()
    if not session_id:
        raise web.HTTPBadRequest(text="missing Livepeer-Session-Id header")
    return session_id


def _parse_mode(payload: dict[str, Any]) -> ModeState:
    mode = str(payload.get("mode", "echo")).strip().lower()
    if mode not in MODES:
        raise web.HTTPBadRequest(text=f"mode must be one of {sorted(MODES)}")
    radius = payload.get("radius", 7)
    try:
        radius_int = int(radius)
    except (TypeError, ValueError) as exc:
        raise web.HTTPBadRequest(text="radius must be an integer") from exc
    return ModeState(mode=mode, radius=max(1, min(99, radius_int)))


def _odd_kernel(radius: int) -> int:
    kernel = max(1, int(radius))
    if kernel % 2 == 0:
        kernel += 1
    return min(kernel, 99)


def _transform_frame(
    decoded: AudioDecodedMediaFrame | VideoDecodedMediaFrame,
    mode: ModeState,
) -> av.VideoFrame | None:
    if decoded.kind != "video":
        return None

    frame = decoded.frame
    if mode.mode == "echo":
        return frame

    import cv2

    img = frame.to_ndarray(format="bgr24")
    if mode.mode == "gray":
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        img = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
    elif mode.mode == "invert":
        img = 255 - img
    elif mode.mode == "blur":
        kernel = _odd_kernel(mode.radius)
        img = cv2.GaussianBlur(img, (kernel, kernel), 0)

    out = av.VideoFrame.from_ndarray(img, format="bgr24")
    out.pts = frame.pts
    out.time_base = frame.time_base
    return out


async def _handle_echo(request: web.Request) -> web.Response:
    global state
    session_id = _session_id(request)

    if state is not None:
        if state.session_id != session_id:
            raise web.HTTPConflict(text="echo runner already has an active session")
        return web.json_response(state.to_json())

    # Pass the request so the SDK opens channels using the orchestrator's
    # Session-Control header, whose URLs are reachable from the runner's network.
    channels = await request.app["registration"].create_trickle_channels(
        request,
        [
            {"name": "in", "mime_type": "video/mp2t"},
            {"name": "out", "mime_type": "video/mp2t"},
        ],
    )
    by_name = {channel["name"]: channel for channel in channels}
    if "in" not in by_name or "out" not in by_name:
        raise web.HTTPInternalServerError(text="orchestrator did not return in/out channels")

    # for production apps, handle errors
    mode = _parse_mode(json.loads(await request.read()))
    # internal_url is the runner-reachable channel address (== public url on a shared network).
    publisher = MediaPublish(by_name["out"]["internal_url"])

    async def _on_frame(decoded) -> None:
        frame = _transform_frame(decoded, mode)
        if frame is not None:
            await publisher.write_frame(frame)

    output = MediaOutput(by_name["in"]["internal_url"], on_frame=_on_frame)

    # Hand public channel urls to the client, so it can send/receive media.
    state = EchoSession(
        session_id=session_id,
        in_url=by_name["in"]["url"],
        out_url=by_name["out"]["url"],
        mode=mode,
        output=output,
        publisher=publisher,
    )
    for task in output.callback_tasks():
        task.add_done_callback(lambda _task: asyncio.create_task(_close_pipeline()))
    log.info("started echo session %s", session_id)
    return web.json_response(state.to_json())


async def _handle_update(request: web.Request) -> web.Response:
    session_id = _session_id(request)
    if state is None:
        raise web.HTTPNotFound(text="echo session not started")
    if state.session_id != session_id:
        raise web.HTTPConflict(text="echo runner has a different active session")

    # for production apps, handle errors
    mode = _parse_mode(json.loads(await request.read()))
    state.mode.mode = mode.mode
    state.mode.radius = mode.radius
    return web.json_response(state.to_json())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()

    async def _on_startup(app: web.Application) -> None:
        app["registration"] = await register_runner(
            args.orchestrator,
            secret=args.orchSecret,
            runner_url=args.runner_url,
            app="livepeer-sample/echo",
            mode="persistent",  # realtime trickle streaming is a held-open session
        )
        log.info(
            "registered runner_id=%s orchestrator=%s",
            app["registration"].runner_id,
            app["registration"].orchestrator_url,
        )

    async def _on_cleanup(app: web.Application) -> None:
        await _close_pipeline()
        with suppress(Exception):
            await app["registration"].close()

    app = web.Application()
    app.router.add_post("/echo", _handle_echo)
    app.router.add_post("/update", _handle_update)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
