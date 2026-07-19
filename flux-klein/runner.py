#!/usr/bin/env python3
# Realtime FLUX Klein app for the new Livepeer runner, attached as a STATIC
# runner (like vllm): the orchestrator reads runners.json via
# -liveRunnerConfig and health-polls /health — no SDK registrar or heartbeat in
# the app. The SDK is used only for the trickle channel plumbing once a session
# starts.
#
# Same trickle loop shape as the `echo` example, with the per-frame transform
# swapped for FLUX.2-klein-4B img2img + a Krea-style feedback loop
# (flux_klein.py). SELF-
# CONTAINED: inference is our own flux_klein.py (diffusers only); NOT the
# Daydream Scope platform (scope-flux-klein is only a reference for the recipe).
#
# Operator lifecycle (warm-up-on-start, gated by /health):
#   boot  -> state "building": load the FLUX pipeline + download weights.
#             /health returns 503, so the orchestrator won't route here.
#   ready -> /health returns 200; the orchestrator (which knows this runner from
#             runners.json) starts routing sessions.
#   error -> state "error", log, exit non-zero; health never passes.
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import av
from aiohttp import web

from livepeer_gateway.live_runner import create_trickle_channels
from livepeer_gateway.media_output import MediaOutput
from livepeer_gateway.media_publish import MediaPublish, MediaPublishConfig, VideoOutputConfig

from flux_klein import (
    FluxKleinModel,
    DEFAULT_MAX_TEXT_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_PROMPT,
    DEFAULT_WIDTH,
    DEFAULT_HEIGHT,
    DEFAULT_STEPS,
    DEFAULT_GUIDANCE,
    DEFAULT_FEEDBACK,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8720
APP_ID = "livepeer-example/flux-klein"

# Model/resolution are fixed per container; the client only changes the prompt.
FLUX_MODEL = os.environ.get("FLUX_MODEL", DEFAULT_MODEL)
FLUX_WIDTH = int(os.environ.get("FLUX_WIDTH", str(DEFAULT_WIDTH)))
FLUX_HEIGHT = int(os.environ.get("FLUX_HEIGHT", str(DEFAULT_HEIGHT)))
FLUX_STEPS = int(os.environ.get("FLUX_STEPS", str(DEFAULT_STEPS)))
FLUX_GUIDANCE = float(os.environ.get("FLUX_GUIDANCE", str(DEFAULT_GUIDANCE)))
FLUX_FEEDBACK = float(os.environ.get("FLUX_FEEDBACK", str(DEFAULT_FEEDBACK)))
FLUX_SEED = int(os.environ.get("FLUX_SEED", "-1"))  # -1 random; fixed = steadier output
FLUX_INPUT_BLEND = float(os.environ.get("FLUX_INPUT_BLEND", "0.5"))  # camera weight 0..1;
# higher = anchors to the real feed (less "fantasy" drift), lower = prompt takes over
FLUX_CPU_OFFLOAD = os.environ.get("FLUX_CPU_OFFLOAD", "").lower() in {"1", "true", "yes"}
FLUX_BATCH = max(1, int(os.environ.get("FLUX_BATCH", "1")))  # frames per GPU pass; 1 =
# unchanged behaviour. 2 buys throughput (the GPU is under-occupied at 384x384) at the
# cost of one extra frame of latency and stride-2 feedback. See README.
# torch.compile mode for the transformer + VAE; unset disables it. Takes a
# torch.compile mode verbatim ("default", "max-autotune", "reduce-overhead").
# Any mode trades minutes of warm-up for faster frames; "max-autotune" is the
# fastest and what the README's throughput numbers were measured with.
FLUX_COMPILE = os.environ.get("FLUX_COMPILE", "").strip() or None

log = logging.getLogger("flux-klein-runner")

STATE = {"state": "building", "model": FLUX_MODEL, "resolution": f"{FLUX_WIDTH}x{FLUX_HEIGHT}", "error": None}
MODEL = FluxKleinModel()                # single, container-wide pipeline (loaded once at warm-up)
_ORCHESTRATOR_URL = "http://localhost:8935"
session: "StreamSession | None" = None
# Guards the whole /stream handler: the session check and the assignment are
# separated by awaits, so two concurrent requests could otherwise both create a
# session and orphan the first one's worker/publisher.
_session_lock = asyncio.Lock()


@dataclass
class StreamSession:
    session_id: str
    in_url: str
    out_url: str
    output: MediaOutput
    publisher: MediaPublish
    prompt: str
    worker: "asyncio.Task | None" = None
    inference: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"session": self.session_id, "in": self.in_url, "out": self.out_url, "prompt": self.prompt}


def _session_id(request: web.Request) -> str:
    sid = request.headers.get("Livepeer-Session-Id", "").strip()
    if not sid:
        raise web.HTTPBadRequest(text="missing Livepeer-Session-Id header")
    return sid


# --- warm-up: load the pipeline, then flip /health to ready -----------------
async def _warmup(app: web.Application) -> None:
    try:
        log.info("warming up: model=%s %dx%d steps=%d feedback=%.2f batch=%d compile=%s "
                 "(may download weights)",
                 FLUX_MODEL, FLUX_WIDTH, FLUX_HEIGHT, FLUX_STEPS, FLUX_FEEDBACK,
                 FLUX_BATCH, FLUX_COMPILE)
        # Blocking + GPU-bound; keep it off the event loop so /status stays responsive.
        await asyncio.to_thread(
            MODEL.load,
            model=FLUX_MODEL, prompt=DEFAULT_PROMPT,
            width=FLUX_WIDTH, height=FLUX_HEIGHT, steps=FLUX_STEPS,
            guidance=FLUX_GUIDANCE, feedback_strength=FLUX_FEEDBACK,
            seed=FLUX_SEED, input_blend=FLUX_INPUT_BLEND, batch=FLUX_BATCH,
            enable_cpu_offload=FLUX_CPU_OFFLOAD,
            compile_mode=FLUX_COMPILE,
        )
    except Exception as exc:
        STATE.update(state="error", error=f"{type(exc).__name__}: {exc}")
        log.error("model load FAILED: %s", exc, exc_info=True)
        os._exit(1)

    STATE["state"] = "ready"
    log.info("FLUX Klein ready; /health now returns 200 — orchestrator may route sessions")


# --- status / health (static-runner readiness signal) -----------------------
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
    if current.worker is not None:
        current.worker.cancel()
        try:
            await current.worker
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # don't discard the reason the worker died
            log.error("worker task ended with error: %s", exc, exc_info=True)
    with suppress(Exception):
        await current.publisher.close()
    with suppress(Exception):
        await current.output.close()
    # Drop the feedback state so the next session starts from a fresh full
    # inference instead of continuing this client's imagery.
    MODEL.reset()
    log.info("closed session %s", current.session_id)


async def _handle_stream(request: web.Request) -> web.Response:
    # Serialized: the "is there a session?" check and the assignment below are
    # separated by several awaits.
    async with _session_lock:
        return await _start_session(request)


async def _start_session(request: web.Request) -> web.Response:
    global session
    if STATE["state"] != "ready":
        raise web.HTTPServiceUnavailable(text=f"runner not ready ({STATE['state']})")
    session_id = _session_id(request)
    if session is not None:
        if session.session_id != session_id:
            raise web.HTTPConflict(text="runner already has an active session")
        return web.json_response(session.to_json())

    MODEL.reset()  # defensive: never inherit a previous session's feedback state

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
    MODEL.update_prompt(prompt)  # model/res fixed per container; this is one assignment
    if "seed" in body:
        MODEL.update_seed(int(body["seed"]))  # -1 random; fixed = steadier output
    if "input_blend" in body:
        MODEL.update_input_blend(float(body["input_blend"]))

    # Short GOP/segment so output trickle segments flush frequently -> lower latency.
    # (FLUX Klein is a few fps, not 30, so segments carry fewer frames each.)
    publisher = MediaPublish(
        by_name["out"].get("internal_url") or by_name["out"]["url"],
        config=MediaPublishConfig(
            tracks=[VideoOutputConfig(fps=30.0, keyframe_interval_s=0.25)],
            min_segment_wallclock_s=0.25,
        ),
    )

    # FLUX Klein runs at a few fps while input arrives at ~30fps. Decouple decode
    # from inference with a latest-frame slot: _on_frame only stashes the newest
    # frame(s) (never blocks the decode loop), and the worker below processes
    # whatever is newest whenever the GPU is free. If inference were awaited
    # inside _on_frame, decode would back up behind it and end-to-end latency
    # would grow without bound (frames queue instead of being skipped).
    # The slot holds the newest FLUX_BATCH frames (just one at the default FLUX_BATCH=1,
    # i.e. the original single-frame slot). Anything older is dropped outright.
    latest: list[Any] = []
    frame_ready = asyncio.Event()
    inference_stats: dict[str, Any] = {
        "frames_processed": 0, "total_inference_s": 0.0, "last_inference_s": None,
        "last_batch_size": None, "errors": 0, "started_at": time.monotonic(),
    }

    async def _on_frame(decoded) -> None:
        if decoded.kind != "video":
            return
        latest.append(decoded.frame)
        del latest[:-FLUX_BATCH]  # keep only the newest N; older frames are skipped
        # Signalled on the FIRST frame, not once the batch is full: if the input stalls
        # (or the stream ends) with a partial batch buffered, waiting for N more frames
        # would stall output forever.
        frame_ready.set()

    def _transform(frames: "list[Any]") -> "list[av.VideoFrame]":
        """Decode -> model -> encode for a whole batch. Runs entirely in a worker
        thread: the colour conversions are full-frame sws_scale work and would
        otherwise stall the event loop (starving MediaPublish's POST task) between
        inferences. Each output carries ITS OWN source pts/time_base — reusing one
        frame's timestamps for the batch would wreck playback timing."""
        rgbs = [f.to_ndarray(format="rgb24") for f in frames]
        outs = []
        for src, out_rgb in zip(frames, MODEL.process_batch(rgbs)):
            out = av.VideoFrame.from_ndarray(out_rgb, format="rgb24")
            out.pts = src.pts
            out.time_base = src.time_base
            outs.append(out)
        return outs

    async def _process_latest() -> None:
        while True:
            await frame_ready.wait()
            frame_ready.clear()
            frames, latest[:] = list(latest), []   # take whatever is buffered (1..N)
            if not frames:
                continue
            t0 = time.perf_counter()
            try:
                outs = await asyncio.to_thread(_transform, frames)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Without this the task dies silently and the client only sees the
                # channel vanish — which looks exactly like a dead-viewer bug.
                STATE["error"] = f"{type(exc).__name__}: {exc}"
                inference_stats["errors"] += 1
                log.error("frame processing failed: %s", exc, exc_info=True)
                raise
            dt = time.perf_counter() - t0
            inference_stats["frames_processed"] += len(frames)
            inference_stats["total_inference_s"] += dt
            # Per-FRAME time, so avg/last stay comparable across batch sizes (and keep
            # matching /stats' inference_fps) rather than jumping with the batch.
            inference_stats["last_inference_s"] = round(dt / len(frames), 3)
            inference_stats["last_batch_size"] = len(frames)
            for out in outs:  # publish in source order
                await publisher.write_frame(out)

    # Segment-level skipping too: small window + default LagPolicy.LATEST keeps
    # the reader on the live edge if decode itself ever falls behind.
    output = MediaOutput(
        by_name["in"].get("internal_url") or by_name["in"]["url"],
        on_frame=_on_frame,
        max_segments=2,
    )
    worker = asyncio.create_task(_process_latest())
    session = StreamSession(
        session_id=session_id, in_url=by_name["in"]["url"], out_url=by_name["out"]["url"],
        output=output, publisher=publisher, prompt=prompt, worker=worker,
        inference=inference_stats,
    )
    worker.add_done_callback(lambda _t: asyncio.create_task(_close_session()))
    for task in output.callback_tasks():
        task.add_done_callback(lambda _t: asyncio.create_task(_close_session()))
    log.info("started flux-klein session %s", session_id)
    return web.json_response(session.to_json())


def _stats_dict(obj: Any) -> Any:
    """Best-effort conversion of SDK stats objects (dataclasses / plain objects)
    into JSON-serializable dicts, recursing into nested stats."""
    if obj is None or isinstance(obj, (int, float, str, bool)):
        return obj
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _stats_dict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _stats_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_stats_dict(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: _stats_dict(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


async def _handle_stats(request: web.Request) -> web.Response:
    """Session statistics: trickle input/output stats from the SDK plus our own
    inference metrics. input fps = video_frames_decoded / elapsed_s;
    encode fps = track frames_in / elapsed_s; frames_skipped = decoded - processed
    (what the latest-frame slot dropped because the GPU was busy)."""
    if session is None:
        return web.json_response({"state": STATE["state"], "session": None})

    in_stats = session.output.get_stats()
    if asyncio.iscoroutine(in_stats):
        in_stats = await in_stats
    out_stats = session.publisher.get_stats()
    if asyncio.iscoroutine(out_stats):
        out_stats = await out_stats

    input_ = _stats_dict(in_stats) or {}
    output_ = _stats_dict(out_stats) or {}

    # Computed rates on top of the raw dumps.
    in_elapsed = float(getattr(in_stats, "elapsed_s", 0) or 0)
    decoded = int(getattr(in_stats, "video_frames_decoded", 0) or 0)
    if in_elapsed > 0:
        input_["input_fps"] = round(decoded / in_elapsed, 2)

    out_elapsed = float(getattr(out_stats, "elapsed_s", 0) or 0)
    for i, track in enumerate(getattr(out_stats, "track_queue_stats", None) or []):
        frames_in = int(getattr(track, "frames_in", 0) or 0)
        if out_elapsed > 0 and isinstance(output_.get("track_queue_stats"), list):
            output_["track_queue_stats"][i]["encode_fps"] = round(frames_in / out_elapsed, 2)

    inference = {k: v for k, v in session.inference.items() if k != "started_at"}
    processed = inference.get("frames_processed", 0)
    session_elapsed = time.monotonic() - session.inference.get("started_at", time.monotonic())
    if processed:
        inference["avg_inference_s"] = round(inference.pop("total_inference_s", 0.0) / processed, 3)
        if session_elapsed > 0:
            inference["inference_fps"] = round(processed / session_elapsed, 2)
    if decoded:
        inference["frames_skipped"] = max(0, decoded - processed)

    return web.json_response(
        {
            "state": STATE["state"],
            "session": session.session_id,
            "prompt": session.prompt,
            "model": FLUX_MODEL,
            "resolution": f"{FLUX_WIDTH}x{FLUX_HEIGHT}",
            "steps": FLUX_STEPS,
            "feedback_strength": FLUX_FEEDBACK,
            # int(steps * feedback) is what actually runs per frame — the number that
            # drives fps. Surfaced so tuning doesn't require doing the arithmetic.
            "refine_steps": min(int(FLUX_STEPS * FLUX_FEEDBACK), FLUX_STEPS),
            "max_text_tokens": DEFAULT_MAX_TEXT_TOKENS,
            # Configured batch; inference.last_batch_size shows what the worker actually
            # got last pass (smaller when the input can't keep the buffer full).
            "batch": FLUX_BATCH,
            "seed": MODEL.seed,
            "input_blend": MODEL.input_blend,
            "compile": FLUX_COMPILE,
            "input": input_,
            "output": output_,
            "inference": inference,
        },
        dumps=lambda o: json.dumps(o, default=str),
    )


async def _handle_update(request: web.Request) -> web.Response:
    if session is None:
        raise web.HTTPNotFound(text="session not started")
    if session.session_id != _session_id(request):
        raise web.HTTPConflict(text="runner has a different active session")
    body = json.loads(await request.read() or "{}")
    prompt = str(body.get("prompt", session.prompt))
    MODEL.update_prompt(prompt)
    session.prompt = prompt
    if "seed" in body:
        MODEL.update_seed(int(body["seed"]))
    if "input_blend" in body:
        MODEL.update_input_blend(float(body["input_blend"]))
    return web.json_response(session.to_json() | {"seed": MODEL.seed, "input_blend": MODEL.input_blend})


# --- lifecycle -------------------------------------------------------------
async def _on_startup(app: web.Application) -> None:
    global _ORCHESTRATOR_URL
    _ORCHESTRATOR_URL = app["args"].orchestrator
    # Warm up in the background so /status + /health serve immediately while the
    # pipeline loads (/health stays 503 until ready).
    app["warmup"] = asyncio.create_task(_warmup(app))


async def _on_cleanup(app: web.Application) -> None:
    task = app.get("warmup")
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
    await _close_session()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Realtime FLUX Klein static app on the new Livepeer runner.")
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
    app.router.add_get("/stats", _handle_stats)
    app.router.add_post("/stream", _handle_stream)
    app.router.add_post("/update", _handle_update)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
