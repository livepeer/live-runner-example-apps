#!/usr/bin/env python3
"""echo app: a realtime trickle video service, made live on the Livepeer network.

Receives a live video stream over trickle `in`/`out` channels, optionally transforms
each frame (gray / invert / blur), and echoes it back.

Livepeer integration (grep `# Livepeer:`):
  1. register_runner()          — announce the app to the orchestrator (startup)
  2. create_trickle_channels()  — open the session's trickle in/out channels
  3. registration.close()       — deregister (cleanup)

Media I/O over trickle uses MediaOutput (read frames) and MediaPublish (write frames).
/echo and /update are ordinary HTTP handlers; being on the network doesn't change how
you write them.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import Any, Literal

import av
import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel, Field

from livepeer_gateway.live_runner import register_runner
from livepeer_gateway.media_decode import AudioDecodedMediaFrame, VideoDecodedMediaFrame
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import (
    AudioOutputConfig,
    MediaPublish,
    MediaPublishConfig,
    VideoOutputConfig,
)

log = logging.getLogger("echo")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
# "robot" multiplies each sample by a sine at this frequency; the sample count is
# unchanged, so audio stays in sync with video.
ROBOT_HZ = 220.0

state: EchoSession | None = None


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
    parser.add_argument("--orchestrator", default="https://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument(
        "--price",
        type=float,
        default=0,
        help="Runner price in USD per hour (0 = free, the offchain default).",
    )
    return parser.parse_args()


def _session_id(request: Request) -> str:
    session_id = request.headers.get("Livepeer-Session-Id", "").strip()
    if not session_id:
        raise HTTPException(
            status_code=400, detail="missing Livepeer-Session-Id header"
        )
    return session_id


class EchoRequest(BaseModel):
    mode: Literal["echo", "gray", "invert", "blur", "robot"] = "echo"
    # The client sweeps 0..100; clamped rather than rejected, as before.
    radius: int = Field(7, description="Blur strength; blur mode only.")
    audio: bool = Field(False, description="Publish an audio track (robot needs one).")


class UpdateRequest(BaseModel):
    mode: Literal["echo", "gray", "invert", "blur", "robot"] = "echo"
    radius: int = 7


class SessionResponse(BaseModel):
    session: str
    in_: str = Field(..., alias="in")
    out: str
    mode: str
    radius: int | None = None

    model_config = {"populate_by_name": True}


def _odd_kernel(radius: int) -> int:
    kernel = max(1, int(radius))
    if kernel % 2 == 0:
        kernel += 1
    return min(kernel, 99)


def _robot_audio(frame: av.AudioFrame) -> av.AudioFrame:
    # sample[i] *= sin(2*pi*ROBOT_HZ*t[i]). The carrier phase comes from the frame's
    # own timestamp, so it stays continuous across frames (no clicks) without keeping
    # state, and |carrier| <= 1 means it cannot clip.
    samples = frame.to_ndarray()
    t0 = float(frame.pts * frame.time_base) if frame.pts is not None else 0.0
    t = t0 + np.arange(samples.shape[-1], dtype=np.float32) / frame.sample_rate
    carrier = np.sin(2.0 * np.pi * ROBOT_HZ * t).astype(np.float32)
    out = av.AudioFrame.from_ndarray(
        (samples.astype(np.float32) * carrier).astype(samples.dtype),
        format=frame.format.name,
        layout=frame.layout.name,
    )
    out.sample_rate = frame.sample_rate
    out.pts = frame.pts
    out.time_base = frame.time_base
    return out


def _transform_frame(
    decoded: AudioDecodedMediaFrame | VideoDecodedMediaFrame,
    mode: ModeState,
) -> av.VideoFrame | av.AudioFrame | None:
    frame = decoded.frame
    if decoded.kind == "audio":
        # Audio rides along untouched; only "robot" transforms it.
        return _robot_audio(frame) if mode.mode == "robot" else frame
    if decoded.kind != "video":
        return None

    if mode.mode in ("echo", "robot"):  # robot changes audio only
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


def build_app(args: argparse.Namespace) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        app.state.registration = await register_runner(  # Livepeer: 1
            args.orchestrator,
            secret=args.orchSecret,
            runner_url=args.runner_url,
            app="livepeer-example/echo",
            mode="persistent",
            price=args.price,
        )
        log.info(
            "registered runner_id=%s orchestrator=%s",
            app.state.registration.runner_id,
            app.state.registration.orchestrator_url,
        )
        yield
        await _close_pipeline()
        with suppress(Exception):
            await app.state.registration.close()  # Livepeer: 3

    app = FastAPI(title="livepeer-example/echo", version="0.1.0", lifespan=_lifespan)

    @app.post("/echo", response_model=SessionResponse, response_model_by_alias=True)
    async def echo(body: EchoRequest, request: Request) -> dict[str, Any]:
        global state
        session_id = _session_id(request)

        if state is not None:
            if state.session_id != session_id:
                raise HTTPException(409, "echo runner already has an active session")
            return state.to_json()

        # Pass the request so the SDK opens channels using the orchestrator's
        # Session-Control header, whose URLs are reachable from the runner's network.
        channels = (
            await request.app.state.registration.create_trickle_channels(  # Livepeer: 2
                request,
                [
                    {"name": "in", "mime_type": "video/mp2t"},
                    {"name": "out", "mime_type": "video/mp2t"},
                ],
            )
        )
        by_name = {channel["name"]: channel for channel in channels}
        if "in" not in by_name or "out" not in by_name:
            raise HTTPException(500, "orchestrator did not return in/out channels")

        mode = ModeState(mode=body.mode, radius=max(1, min(99, body.radius)))
        # Tracks are declared upfront and the container waits for a first frame on
        # each, so only declare audio when the client says it is sending some.
        tracks: list[VideoOutputConfig | AudioOutputConfig] = [VideoOutputConfig()]
        if body.audio:
            tracks.append(AudioOutputConfig())
        # internal_url: runner-reachable address (same as public url on a shared net).
        publisher = MediaPublish(
            by_name["out"].get("internal_url", by_name["out"]["url"]),
            config=MediaPublishConfig(tracks=tracks),
        )

        async def _on_frame(decoded) -> None:
            frame = _transform_frame(decoded, mode)
            if frame is not None:
                await publisher.write_frame(frame)

        output = MediaOutput(
            by_name["in"].get("internal_url", by_name["in"]["url"]), on_frame=_on_frame
        )

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
            task.add_done_callback(lambda _t: asyncio.create_task(_close_pipeline()))
        log.info("started echo session %s", session_id)
        return state.to_json()

    @app.post("/update", response_model=SessionResponse, response_model_by_alias=True)
    async def update(body: UpdateRequest, request: Request) -> dict[str, Any]:
        session_id = _session_id(request)
        if state is None:
            raise HTTPException(404, "echo session not started")
        if state.session_id != session_id:
            raise HTTPException(409, "echo runner has a different active session")
        state.mode.mode = body.mode
        state.mode.radius = max(1, min(99, body.radius))
        return state.to_json()

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    uvicorn.run(build_app(args), host=args.host, port=DEFAULT_PORT, access_log=False)


if __name__ == "__main__":
    main()
