#!/usr/bin/env python3
"""realtime-transcription app: realtime speech-to-text over WebSocket — a Live Runner app.

The WebSocket showcase: the client streams raw audio *up* and gets transcripts
streamed *back* over one socket — something HTTP can't do (no upstream stream)
and SSE can't do (one-directional). It self-registers (dynamic), so it embeds
the SDK and is its own server, like hello-world.

Realtime design: the receive loop only appends audio, never blocking on the
model. A background worker transcribes the *current utterance* (bounded to
MAX_SEGMENT_SEC) every STEP_SEC, emits partials, and finalizes on trailing
silence (energy VAD) or max length — so cost stays bounded no matter how long
the stream runs (vs. re-transcribing an ever-growing buffer).

Livepeer integration (grep `# Livepeer:`):
  1. register_runner()     — announce the app to the orchestrator (startup)
  2. registration.close()  — deregister (cleanup)

/transcribe is an ordinary aiohttp WebSocket handler; the orchestrator proxies
the upgrade straight through — nothing Livepeer-specific in the socket itself.

Wire protocol on /transcribe:
  client -> server: binary frames of 16 kHz mono PCM (int16)
  client -> server: text "eos" to finish
  server -> client: JSON {"text": "...", "final": false|true}
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import suppress

import numpy as np
from aiohttp import web

from livepeer_gateway.live_runner import register_runner

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5005
APP_ID = "livepeer-example/realtime-transcription"
# Fixed, not a knob: this example is about realtime transcription, so it pins the
# model that is both accurate and fast enough to keep pace. large-v3-turbo swaps
# large-v3's 32-layer decoder for 4, so it runs far below realtime on a 3090 while
# staying near large-v3 quality. Serving a different model is a different app, with
# its own price and app id, not a setting on this one.
WHISPER_MODEL = "large-v3-turbo"

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2  # int16 mono
STEP_SEC = 0.5  # emit a partial at most this often
MAX_SEGMENT_SEC = 15.0  # force-finalize a segment this long (bounds cost)
SILENCE_SEC = 0.5  # trailing silence that ends an utterance
SILENCE_RMS = 350.0  # int16 RMS below this = silence
MIN_SEGMENT_SEC = 0.3  # don't transcribe shorter than this

_model = None


def _load_model(name: str, device: str, compute_type: str) -> None:
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # heavy import; defer until startup

        _model = WhisperModel(name, device=device, compute_type=compute_type)
        log.info(
            "loaded whisper model=%s device=%s compute=%s", name, device, compute_type
        )


def _rms(pcm: bytes) -> float:
    if not pcm:
        return 0.0
    a = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def _transcribe(pcm: bytes) -> str:
    if len(pcm) < int(BYTES_PER_SEC * MIN_SEGMENT_SEC):
        return ""
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    # Greedy (beam_size=1) + no cross-segment conditioning = the low-latency preset.
    segments, _ = _model.transcribe(
        audio,
        language="en",
        vad_filter=True,
        beam_size=1,
        condition_on_previous_text=False,
    )
    return " ".join(s.text.strip() for s in segments).strip()


log = logging.getLogger("realtime-transcription")


async def _handle_transcribe(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    log.info("transcription socket opened")

    seg = bytearray()  # current utterance PCM; the worker trims finalized audio
    spoke = False

    async def _worker() -> None:
        nonlocal seg, spoke
        while True:
            await asyncio.sleep(STEP_SEC)
            n = len(seg)
            if n < int(BYTES_PER_SEC * MIN_SEGMENT_SEC):
                continue
            chunk = bytes(seg[:n])
            tail_silent = _rms(chunk[-int(BYTES_PER_SEC * SILENCE_SEC) :]) < SILENCE_RMS
            text = await asyncio.to_thread(_transcribe, chunk)
            finalize = (spoke and tail_silent) or n >= int(
                BYTES_PER_SEC * MAX_SEGMENT_SEC
            )
            if finalize:
                if text:
                    await ws.send_json({"text": text, "final": True})
                del seg[
                    :n
                ]  # drop finalized audio; keep anything appended during inference
                spoke = False
            elif text:
                await ws.send_json({"text": text, "final": False})

    worker = asyncio.create_task(_worker())
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                seg.extend(msg.data)
                if _rms(msg.data) >= SILENCE_RMS:
                    spoke = True
            elif msg.type == web.WSMsgType.TEXT and msg.data.strip() == "eos":
                text = await asyncio.to_thread(_transcribe, bytes(seg))
                await ws.send_json({"text": text, "final": True})
                break
            elif msg.type == web.WSMsgType.ERROR:
                log.warning("ws error: %s", ws.exception())
                break
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await worker
    log.info("transcription socket closed")
    return ws


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Streaming Whisper ASR Live Runner.")
    parser.add_argument("--orchestrator", default="https://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers)."
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="cpu (default, runs anywhere) or cuda (low latency).",
    )
    parser.add_argument(
        "--compute-type", default="int8", help="int8 (cpu) or float16 (cuda)."
    )
    parser.add_argument(
        "--price",
        type=float,
        default=0,
        help="Runner price in USD per hour (0 = free).",
    )
    return parser.parse_args()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    _load_model(
        WHISPER_MODEL, args.device, args.compute_type
    )  # fail fast if the model is missing

    async def _on_startup(app: web.Application) -> None:
        app["registration"] = await register_runner(  # Livepeer: 1
            args.orchestrator,
            secret=args.orchSecret,
            runner_url=args.runner_url,
            app=APP_ID,
            mode="persistent",  # the WebSocket is a held-open session
            price=args.price,  # decimal USD/hour
        )
        log.info(
            "registered runner_id=%s app=%s", app["registration"].runner_id, APP_ID
        )

    async def _on_cleanup(app: web.Application) -> None:
        with suppress(Exception):
            await app["registration"].close()  # Livepeer: 2

    app = web.Application()
    app.router.add_get("/transcribe", _handle_transcribe)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT, print=None)


if __name__ == "__main__":
    main()
