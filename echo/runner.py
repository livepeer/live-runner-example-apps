#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import av
from aiohttp import web

from livepeer_gateway.live_runner import create_trickle_channels, register_runner
from livepeer_gateway.media_decode import AudioDecodedMediaFrame, VideoDecodedMediaFrame
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
MODES = frozenset({"echo", "gray", "invert", "blur"})

# Internal control-plane URL the orchestrator is reachable at from this container;
# set at startup from --orchestrator. Used for trickle channel creation instead of
# the request's Livepeer-Session-Control header (which is the public 127.0.0.1 addr).
_ORCHESTRATOR_URL = "http://localhost:8935"

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
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--price", type=int, default=0, help="Price in USD per pixels-per-unit (0 = free, the offchain default).")
    parser.add_argument("--pixels-per-unit", type=int, default=1, help="Scale factor: price is charged per this many units.")
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

    channels = await create_trickle_channels(
        session_id,
        [
            {"name": "in", "mime_type": "video/mp2t"},
            {"name": "out", "mime_type": "video/mp2t"},
        ],
        orchestrator_url=_ORCHESTRATOR_URL,
        runner_id=request.headers.get("Livepeer-Runner-Route", "").strip(),
        session_token=request.headers.get("Livepeer-Session-Token", "").strip(),
    )
    by_name = {channel["name"]: channel for channel in channels}
    if "in" not in by_name or "out" not in by_name:
        raise web.HTTPInternalServerError(text="orchestrator did not return in/out channels")

    # for production apps, handle errors
    mode = _parse_mode(json.loads(await request.read()))
    # The runner reads/writes its channels from inside the container, so it uses
    # internal_url (reachable on the compose network); the public url is handed
    # back to the client below.
    publisher = MediaPublish(by_name["out"].get("internal_url") or by_name["out"]["url"])

    async def _on_frame(decoded) -> None:
        frame = _transform_frame(decoded, mode)
        if frame is not None:
            await publisher.write_frame(frame)

    output = MediaOutput(by_name["in"].get("internal_url") or by_name["in"]["url"], on_frame=_on_frame)

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
    print(f"started echo session {session_id}")
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


async def _on_cleanup(app: web.Application) -> None:
    await _close_pipeline()


async def _on_startup(app: web.Application) -> None:
    global _ORCHESTRATOR_URL
    args = _parse_args()
    _ORCHESTRATOR_URL = args.orchestrator
    registration = await register_runner(
        args.orchestrator,
        secret=args.orchSecret,
        runner_url=args.runner_url,
        app="livepeer-sample/echo",
        price_per_unit=args.price,
        pixels_per_unit=args.pixels_per_unit,
    )
    print(
        f"runner_id={registration.runner_id} orchestrator={registration.orchestrator_url}"
    )


def main() -> None:
    args = _parse_args()
    app = web.Application()
    app.router.add_post("/echo", _handle_echo)
    app.router.add_post("/update", _handle_update)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
