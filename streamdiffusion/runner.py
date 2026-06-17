#!/usr/bin/env python3
# StreamDiffusion on the NEW Livepeer runner (Josh's general-runner stack),
# attached as a STATIC runner (like the vllm example): the orchestrator is
# configured with this app's URL in runners.json and health-polls /health — no
# SDK registrar or heartbeat in the app. The SDK is used only for the trickle
# channel plumbing (create_trickle_channels) once a session starts.
#
# echo/runner.py's trickle loop with the per-frame transform swapped for realtime
# StreamDiffusion img2img. SELF-CONTAINED: inference is our own sd.py (upstream
# `streamdiffusion` only); no ai-runner image/modules.
#
# Operator lifecycle (warm-up-on-start, gated by /health):
#   boot -> state "building": compile/load TensorRT engines (build-if-missing).
#           /health returns 503, so the orchestrator's health poll won't route here.
#   ready  -> /health returns 200; the orchestrator (which already knows this
#             runner from runners.json) starts routing sessions.
#   error  -> state "error", log, exit non-zero (container errors out; health
#             never passes, so no session is routed).
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import av
from aiohttp import web

from livepeer_gateway.live_runner import create_trickle_channels
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish, MediaPublishConfig, VideoOutputConfig

from sd import StreamDiffusion, DEFAULT_MODEL, DEFAULT_PROMPT

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8900
APP_ID = "livepeer-sample/streamdiffusion"

# Model/resolution are fixed per container (engines are built for them); the
# client only changes the prompt at /stream and /update.
SD_MODEL = os.environ.get("SD_MODEL", DEFAULT_MODEL)
SD_WIDTH = int(os.environ.get("SD_WIDTH", "512"))
SD_HEIGHT = int(os.environ.get("SD_HEIGHT", "512"))
# If set, do NOT build at boot — require engines to already exist (fleets with
# prebuilt/downloaded bundles); fail fast if missing.
SD_REQUIRE_PREBUILT = os.environ.get("SD_REQUIRE_PREBUILT", "").lower() in {"1", "true", "yes"}

log = logging.getLogger("streamdiffusion-runner")

# Lifecycle state, surfaced via /status and /health.
STATE = {"state": "building", "model": SD_MODEL, "resolution": f"{SD_WIDTH}x{SD_HEIGHT}", "error": None}
SD = StreamDiffusion()                 # the single, container-wide pipeline (loaded once at warm-up)
_ORCHESTRATOR_URL = "http://localhost:8935"
session: "StreamSession | None" = None


@dataclass
class StreamSession:
    session_id: str
    in_url: str
    out_url: str
    output: MediaOutput
    publisher: MediaPublish
    prompt: str

    def to_json(self) -> dict[str, Any]:
        return {"session": self.session_id, "in": self.in_url, "out": self.out_url, "prompt": self.prompt}


def _session_id(request: web.Request) -> str:
    sid = request.headers.get("Livepeer-Session-Id", "").strip()
    if not sid:
        raise web.HTTPBadRequest(text="missing Livepeer-Session-Id header")
    return sid


# --- warm-up: build/load engines, then flip /health to ready ----------------
async def _warmup(app: web.Application) -> None:
    try:
        build = not SD_REQUIRE_PREBUILT
        log.info("warming up: model=%s %dx%d build_engines=%s (this can take many minutes)",
                 SD_MODEL, SD_WIDTH, SD_HEIGHT, build)
        # Blocking + GPU-bound; keep it off the event loop so /status stays responsive.
        await asyncio.to_thread(SD.load, model=SD_MODEL, prompt=DEFAULT_PROMPT,
                                width=SD_WIDTH, height=SD_HEIGHT, build_engines=build)
    except Exception as exc:
        STATE.update(state="error", error=f"{type(exc).__name__}: {exc}")
        log.error("engine build/load FAILED: %s", exc, exc_info=True)
        # Error out so the operator sees a failed container; /health never passes.
        os._exit(1)

    STATE["state"] = "ready"
    log.info("engines ready; /health now returns 200 — orchestrator may route sessions")


# --- status / health (the static-runner readiness signal) -------------------
async def _handle_status(request: web.Request) -> web.Response:
    return web.json_response(STATE)


async def _handle_health(request: web.Request) -> web.Response:
    if STATE["state"] != "ready":
        raise web.HTTPServiceUnavailable(text=STATE["state"])
    return web.Response(text="ok")


# --- session ---------------------------------------------------------------
async def _close_session() -> None:
    global session
    if session is None:
        return
    current, session = session, None
    with suppress(Exception):
        await current.publisher.close()
    with suppress(Exception):
        await current.output.close()


async def _handle_stream(request: web.Request) -> web.Response:
    global session
    if STATE["state"] != "ready":
        raise web.HTTPServiceUnavailable(text=f"runner not ready ({STATE['state']})")
    session_id = _session_id(request)
    if session is not None:
        if session.session_id != session_id:
            raise web.HTTPConflict(text="runner already has an active session")
        return web.json_response(session.to_json())

    channels = await create_trickle_channels(
        session_id,
        [{"name": "in", "mime_type": "video/mp2t"}, {"name": "out", "mime_type": "video/mp2t"}],
        orchestrator_url=_ORCHESTRATOR_URL,
        runner_id=request.headers.get("Livepeer-Runner-Route", "").strip(),
        session_token=request.headers.get("Livepeer-Session-Token", "").strip(),
    )
    by_name = {c["name"]: c for c in channels}
    if "in" not in by_name or "out" not in by_name:
        raise web.HTTPInternalServerError(text="orchestrator did not return in/out channels")

    body = json.loads(await request.read() or "{}")
    prompt = str(body.get("prompt", DEFAULT_PROMPT))
    await asyncio.to_thread(SD.update_prompt, prompt)  # model/res fixed; only prompt changes

    # Short GOP/segment so output trickle segments flush ~4x/sec instead of the
    # 2s default -> near-realtime latency (matched on the client's input publish).
    publisher = MediaPublish(
        by_name["out"].get("internal_url") or by_name["out"]["url"],
        config=MediaPublishConfig(
            tracks=[VideoOutputConfig(fps=30.0, keyframe_interval_s=0.25)],
            min_segment_wallclock_s=0.25,
        ),
    )

    async def _on_frame(decoded) -> None:
        if decoded.kind != "video":
            return
        rgb = decoded.frame.to_ndarray(format="rgb24")
        out_rgb = await asyncio.to_thread(SD.process, rgb)
        out = av.VideoFrame.from_ndarray(out_rgb, format="rgb24")
        out.pts = decoded.frame.pts
        out.time_base = decoded.frame.time_base
        await publisher.write_frame(out)

    # Small segment window + default LagPolicy.LATEST: when diffusion can't keep up
    # with the incoming feed, skip to the newest segment instead of working through
    # a growing backlog (which would make latency creep up the longer it runs).
    output = MediaOutput(
        by_name["in"].get("internal_url") or by_name["in"]["url"],
        on_frame=_on_frame,
        max_segments=2,
    )
    session = StreamSession(
        session_id=session_id, in_url=by_name["in"]["url"], out_url=by_name["out"]["url"],
        output=output, publisher=publisher, prompt=prompt,
    )
    for task in output.callback_tasks():
        task.add_done_callback(lambda _t: asyncio.create_task(_close_session()))
    log.info("started streamdiffusion session %s", session_id)
    return web.json_response(session.to_json())


async def _handle_update(request: web.Request) -> web.Response:
    if session is None:
        raise web.HTTPNotFound(text="session not started")
    if session.session_id != _session_id(request):
        raise web.HTTPConflict(text="runner has a different active session")
    body = json.loads(await request.read() or "{}")
    prompt = str(body.get("prompt", session.prompt))
    await asyncio.to_thread(SD.update_prompt, prompt)
    session.prompt = prompt
    return web.json_response(session.to_json())


# --- lifecycle -------------------------------------------------------------
async def _on_startup(app: web.Application) -> None:
    global _ORCHESTRATOR_URL
    _ORCHESTRATOR_URL = app["args"].orchestrator
    # Warm up in the background so /status + /health serve immediately while
    # engines build (/health stays 503 until ready).
    app["warmup"] = asyncio.create_task(_warmup(app))


async def _on_cleanup(app: web.Application) -> None:
    task = app.get("warmup")
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    await _close_session()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="StreamDiffusion static app on the new Livepeer runner.")
    # Orchestrator URL is needed only for create_trickle_channels; there is no
    # registration (static runner — the orchestrator knows us via runners.json).
    p.add_argument("--orchestrator", default="http://localhost:8935")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args()
    app = web.Application()
    app["args"] = args
    app.router.add_get("/status", _handle_status)
    app.router.add_get("/health", _handle_health)
    app.router.add_post("/stream", _handle_stream)
    app.router.add_post("/update", _handle_update)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
