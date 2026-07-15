#!/usr/bin/env python3
"""vllm-realtime runner: realtime speech transcription over Trickle (in) + WebSocket (out).

Endpoints
---------
POST /transcribe
    Mint a Trickle audio-input channel, start the transcription pipeline.
    Returns {"in": <trickle_url>, "ws": "/ws"}.

GET /ws
    WebSocket: the client receives transcript + metrics as JSON events and may
    send {"type": "session.update", "session": {...}} mid-stream to adjust
    settings (language, model, etc.) without stopping the stream.

    {"type": "progress", "audio_bytes": N} reports how much audio has actually
    been read off the Trickle channel. The client uses it as backpressure: the
    channel keeps no backlog, so it must not publish past what we have consumed.

    The last event before close is {"type": "stats", ...}: Trickle transport
    counters (from the SDK) plus WebSocket and latency counters (metered by this
    app — see stats.py, the SDK does not meter WebSockets).

Architecture
------------
The orchestrator proxies both Trickle segments and WebSocket upgrades, so the
client uses Trickle only for the audio upload path (where chunked, paced
segments are natural) and a standard WebSocket for the output path (where
bidirectional, event-oriented framing fits transcript deltas and settings
updates).

    client ──PCM16 Trickle──> orchestrator ──> TrickleSubscriber
                                                 │
                                       Transcriber (local vLLM ws | mock)
                                                 │
    client <──WS JSON events── orchestrator <── event_queue
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from typing import Optional

from aiohttp import web

from livepeer_gateway.live_runner import register_runner
from livepeer_gateway.trickle_subscriber import TrickleSubscriber, TrickleSubscriberStats

from stats import TranscriptionMeter, WebSocketMeter
from transcriber import Transcriber, make_transcriber, simple_sentiment, word_count

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
APP_ID = "livepeer-sample/vllm-realtime"

IN_MIME = "audio/raw"

# Sentinel pushed to the event queue when the pipeline is finished.
_WS_DONE = object()

log = logging.getLogger("vllm-realtime")


@dataclass
class TranscribeSession:
    session_id: str
    in_url: str
    event_queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    task: Optional[asyncio.Task] = None
    transcriber: Optional[Transcriber] = None
    # The SDK meters Trickle for us; the WS and the latency numbers are ours.
    meter: TranscriptionMeter = field(default_factory=TranscriptionMeter)
    ws_meter: WebSocketMeter = field(default_factory=WebSocketMeter)

    def to_json(self) -> dict:
        return {"session": self.session_id, "in": self.in_url, "ws": "/ws"}


# Single active session (capacity 1).
state: Optional[TranscribeSession] = None


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Runner vllm-realtime demo.")
    parser.add_argument("--orchestrator", default="http://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers).")
    parser.add_argument("--price", type=int, default=0)
    parser.add_argument("--pixels-per-unit", type=int, default=1)
    return parser.parse_args()


def _session_id(request: web.Request) -> str:
    return request.headers.get("Livepeer-Session-Id", "").strip()


async def _pump_audio(sub: TrickleSubscriber, session: TranscribeSession) -> None:
    """Read PCM segments off the Trickle input channel and feed the transcriber."""
    while True:
        segment = await sub.next()
        if segment is None:
            break
        reader = segment.make_reader()
        while True:
            chunk = await reader.read()
            if chunk is None:
                break
            session.meter.mark_audio(len(chunk))
            await session.transcriber.feed(chunk)
        with suppress(Exception):
            await segment.close()
        # Report ingest progress after every segment. Trickle deletes unread
        # segments the moment the publisher closes, so a client that publishes
        # faster than we consume would lose most of its audio. This lets the
        # client hold the stream open until we have actually caught up.
        await session.event_queue.put(
            {"type": "progress", "audio_bytes": session.meter.bytes_in}
        )


async def _drain_events(session: TranscribeSession) -> None:
    """Read normalized transcriber events, compute metrics, push to the WS queue."""
    transcript = ""
    async for event in session.transcriber.events():
        if event["type"] == "delta":
            if not event.get("text"):
                # Voxtral emits empty-text deltas between words; skip them
                # rather than spamming clients with no-op events.
                continue
            transcript += event.get("text", "")
            session.meter.mark_delta()
        else:
            transcript = event.get("text", "") or transcript
        clean = transcript.strip()
        if event["type"] != "delta":
            session.meter.mark_done(clean)
        await session.event_queue.put(
            {
                "type": event["type"],
                "delta": event.get("text", "") if event["type"] == "delta" else "",
                "transcript": clean,
                "word_count": word_count(clean),
                "sentiment": simple_sentiment(clean),
            }
        )


def _stats_event(
    session: TranscribeSession, trickle_stats: Optional[TrickleSubscriberStats]
) -> dict:
    """Final event of a session: Trickle in (from the SDK) + WS out (ours)."""
    transcription = session.meter.get_stats()
    websocket = session.ws_meter.get_stats()
    trickle = None
    if trickle_stats is not None:
        trickle = asdict(trickle_stats)
        trickle["elapsed_s"] = round(trickle["elapsed_s"], 3)
    log.info("stats %s", transcription)
    log.info("stats %s", websocket)
    if trickle_stats is not None:
        log.info("stats %s", trickle_stats)
    return {
        "type": "stats",
        "transcription": transcription.to_json(),
        "websocket": websocket.to_json(),
        "trickle": trickle,
    }


async def _run_pipeline(session: TranscribeSession, in_url: str) -> None:
    """Wire input audio → transcriber → metrics → WS event queue."""
    drain: Optional[asyncio.Task] = None
    trickle_stats: Optional[TrickleSubscriberStats] = None
    try:
        await session.transcriber.open()
        drain = asyncio.create_task(_drain_events(session))
        # Default start_seq joins at the live edge, which is the only option:
        # the channel keeps no backlog, so an earlier index answers 470 (no data)
        # rather than replaying. Audio is not droppable the way stale video
        # frames are, so the client must not move the edge forward until we have
        # read the current segment — see the progress events below.
        async with TrickleSubscriber(in_url) as sub:
            try:
                await _pump_audio(sub, session)
            finally:
                # Read the transport counters while the subscriber is still open,
                # so a mid-stream failure still reports how far the audio got.
                trickle_stats = sub.get_stats()
    except Exception:
        log.exception("transcription pipeline failed")
    finally:
        # Audio is done; the finalize tail is measured from here to the final
        # transcript, so mark it before close() triggers the flush.
        session.meter.mark_audio_end()
        with suppress(Exception):
            await session.transcriber.close()
        if drain is not None:
            try:
                await drain
            except Exception:
                # A dead drain means transcript events silently stop reaching
                # clients — surface it instead of swallowing it.
                log.exception("event drain task failed")
        await session.event_queue.put(_stats_event(session, trickle_stats))
        # Signal the WS drain coroutine that no more events are coming.
        await session.event_queue.put(_WS_DONE)
        log.info("transcription pipeline finished")


async def _drain_queue_to_ws(session: TranscribeSession, ws: web.WebSocketResponse) -> None:
    """Forward queued transcript events to the WebSocket client, metering as we go."""
    try:
        while True:
            event = await session.event_queue.get()
            if event is _WS_DONE:
                await ws.close()
                return
            # Serialize here rather than send_json so the payload size is
            # measurable — the SDK counts Trickle bytes for us, not these.
            payload = json.dumps(event)
            try:
                await ws.send_str(payload)
                session.ws_meter.record_sent(event, len(payload))
            except Exception:
                session.ws_meter.record_send_failure()
    except asyncio.CancelledError:
        pass


async def _handle_transcribe(request: web.Request) -> web.Response:
    global state
    session_id = _session_id(request)

    if state is not None:
        if state.session_id != session_id:
            raise web.HTTPConflict(text="vllm-realtime runner already has an active session")
        return web.json_response(state.to_json())

    try:
        body = await request.json()
    except Exception:
        body = {}
    language: str = body.get("language", "")

    # Pass the request so the SDK opens channels using the orchestrator's
    # Session-Control header, whose URLs are reachable from the runner's network.
    channels = await request.app["registration"].create_trickle_channels(
        request,
        [{"name": "in", "mime_type": IN_MIME}],
    )
    if not channels or "url" not in channels[0]:
        raise web.HTTPInternalServerError(text="orchestrator did not return in channel")
    # Public URL goes to the client; internal_url is the runner-reachable
    # address (== public url when runner and orchestrator share a network).
    in_url = channels[0]["url"]
    in_internal_url = channels[0].get("internal_url", in_url)

    event_queue: asyncio.Queue = asyncio.Queue()
    transcriber = make_transcriber(language=language)

    session = TranscribeSession(
        session_id=session_id,
        in_url=in_url,
        event_queue=event_queue,
        transcriber=transcriber,
    )
    task = asyncio.create_task(_run_pipeline(session, in_internal_url))
    task.add_done_callback(_clear_state)
    session.task = task
    state = session

    log.info("started transcription session %s language=%r", session_id, language)
    return web.json_response(state.to_json())


async def _handle_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket output: stream transcript events; accept session.update settings."""
    if state is None:
        raise web.HTTPNotFound(text="no active transcription session")

    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    session = state
    drain_task = asyncio.create_task(_drain_queue_to_ws(session, ws))

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                log.info("ws command received: %.200s", msg.data)
                session.ws_meter.record_command()
                with suppress(Exception):
                    cmd = json.loads(msg.data)
                    if cmd.get("type") == "session.update" and session.transcriber is not None:
                        await session.transcriber.apply_settings(cmd.get("session", {}))
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        drain_task.cancel()
        with suppress(Exception):
            await drain_task

    return ws


def _clear_state(_task: asyncio.Task) -> None:
    global state
    state = None


async def _handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()

    async def _on_startup(app: web.Application) -> None:
        app["registration"] = await register_runner(
            args.orchestrator,
            secret=args.orchSecret,
            runner_url=args.runner_url,
            app=APP_ID,
            price_per_unit=args.price,
            pixels_per_unit=args.pixels_per_unit,
        )
        log.info(
            "registered runner_id=%s orchestrator=%s",
            app["registration"].runner_id,
            app["registration"].orchestrator_url,
        )

    async def _on_cleanup(app: web.Application) -> None:
        if state is not None:
            if state.task is not None:
                state.task.cancel()
                with suppress(Exception):
                    await state.task
            if state.transcriber is not None:
                with suppress(Exception):
                    await state.transcriber.close()
        with suppress(Exception):
            await app["registration"].close()

    app = web.Application()
    app.router.add_post("/transcribe", _handle_transcribe)
    app.router.add_get("/ws", _handle_ws)
    app.router.add_get("/healthz", _handle_health)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT, print=None)


if __name__ == "__main__":
    main()
