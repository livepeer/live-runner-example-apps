#!/usr/bin/env python3
"""realtime-transcription app: speech-to-text, made live on the Livepeer network.

Receives audio over a WebSocket and streams transcripts back on the same socket
— the case HTTP can't serve (no upstream stream) and SSE can't either
(one-directional). The receive loop only appends audio; a background worker
transcribes the current utterance and finalizes it on trailing silence.

Wire protocol on /transcribe:
  client -> server: binary frames of 16 kHz mono PCM (int16), then text "eos"
  server -> client: JSON {"text": ..., "final": bool, "start": sec, "end": sec}

Livepeer integration (grep `# Livepeer:`):
  1. register_runner()     — announce the app to the orchestrator (startup)
  2. registration.close()  — deregister (cleanup)

/transcribe is an ordinary aiohttp WebSocket handler; being on the network doesn't
change how you write it.
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
DEFAULT_PORT = 8989
APP_ID = "livepeer-example/realtime-transcription"

# Not flags: the app id names the capability, so a different model is a different app.
WHISPER_MODEL = "large-v3-turbo"
DEVICE = "cuda"
COMPUTE_TYPE = "float16"

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2  # int16 mono
STEP_SEC = 0.5  # emit a partial at most this often
MAX_SEGMENT_SEC = 15.0  # force-finalize a segment this long (bounds cost)
SILENCE_SEC = 0.5  # trailing silence that ends an utterance
SILENCE_RMS = 350.0  # int16 RMS below this = silence
MIN_SEGMENT_SEC = 0.3  # below this Whisper hallucinates on near-silence

_model = None


def _load_model() -> None:
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # heavy import; defer until startup

        _model = WhisperModel(WHISPER_MODEL, device=DEVICE, compute_type=COMPUTE_TYPE)
        log.info(
            "loaded whisper model=%s device=%s compute=%s",
            WHISPER_MODEL,
            DEVICE,
            COMPUTE_TYPE,
        )


def _samples(pcm: bytes) -> np.ndarray:
    # A chunk can end mid-sample, so drop a trailing odd byte rather than
    # letting frombuffer raise on a buffer that isn't a multiple of 2.
    return np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)


def _rms(pcm: bytes) -> float:
    # Loudness of this window; below SILENCE_RMS counts as silence.
    a = _samples(pcm).astype(np.float32)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


def _transcribe(pcm: bytes) -> str:
    if len(pcm) < int(BYTES_PER_SEC * MIN_SEGMENT_SEC):
        return ""
    audio = _samples(pcm).astype(np.float32) / 32768.0
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
    offset = 0  # bytes finalized so far = where the current utterance starts

    def _message(text: str, n: int, *, final: bool) -> dict[str, object]:
        # Timestamps are seconds into the stream, so a client can align a
        # transcript with the audio it sent (subtitles, seeking).
        return {
            "text": text,
            "final": final,
            "start": round(offset / BYTES_PER_SEC, 2),
            "end": round((offset + n) / BYTES_PER_SEC, 2),
        }

    async def _worker() -> None:
        nonlocal seg, spoke, offset
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
                    await ws.send_json(_message(text, n, final=True))
                del seg[
                    :n
                ]  # drop finalized audio; keep anything appended during inference
                offset += n
                spoke = False
            elif text:
                await ws.send_json(_message(text, n, final=False))

    worker = asyncio.create_task(_worker())
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                seg.extend(msg.data)
                if _rms(msg.data) >= SILENCE_RMS:
                    spoke = True
            elif msg.type == web.WSMsgType.TEXT and msg.data.strip() == "eos":
                # Stop the worker first, or a partial it is mid-way through can
                # land after the closing final and read as the last transcript.
                worker.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await worker
                text = await asyncio.to_thread(_transcribe, bytes(seg))
                if text:
                    await ws.send_json(_message(text, len(seg), final=True))
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
    parser = argparse.ArgumentParser(
        description="Live Runner realtime-transcription app demo (WebSocket showcase)."
    )
    parser.add_argument("--orchestrator", default="https://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers)."
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
    _load_model()  # fail fast if the model or the GPU is missing

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
    web.run_app(app, host=args.host, port=DEFAULT_PORT)


if __name__ == "__main__":
    main()
