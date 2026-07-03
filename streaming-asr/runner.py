#!/usr/bin/env python3
"""Streaming speech-to-text over WebSocket — a Live Runner app.

This is the WebSocket showcase: the client streams raw audio *up* and gets
transcripts streamed *back* over one socket — something HTTP can't do (no
upstream stream) and SSE can't do (one-directional). It self-registers
(dynamic), so it embeds the SDK and is its own server, like hello-world.

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
from faster_whisper import WhisperModel

from livepeer_gateway.live_runner import register_runner

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5005
APP_ID = "whisper/base.en"
SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2  # int16 mono
PARTIAL_EVERY_SEC = 1.0          # re-transcribe the buffer this often

log = logging.getLogger("streaming-asr")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Streaming Whisper ASR Live Runner.")
    parser.add_argument("--orchestrator", default="http://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers).")
    parser.add_argument("--model", default="base.en", help="faster-whisper model size.")
    parser.add_argument("--device", default="cpu", help="cpu (default, runs anywhere) or cuda (low latency).")
    parser.add_argument("--compute-type", default="int8", help="int8 (cpu) or float16 (cuda).")
    parser.add_argument("--price", type=int, default=0, help="Price in USD per pixels-per-unit (0 = free).")
    parser.add_argument("--pixels-per-unit", type=int, default=1, help="Scale factor for the price.")
    return parser.parse_args()


def _transcribe(model: WhisperModel, pcm: bytes) -> str:
    if len(pcm) < BYTES_PER_SEC // 4:  # < 0.25s, not worth it
        return ""
    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(audio, language="en", vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


async def _handle_transcribe(request: web.Request) -> web.WebSocketResponse:
    model: WhisperModel = request.app["model"]
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)
    log.info("transcription socket opened")
    buf = bytearray()
    last_transcribed = 0
    busy = False
    async for msg in ws:
        if msg.type == web.WSMsgType.BINARY:
            buf.extend(msg.data)
            if not busy and len(buf) - last_transcribed >= BYTES_PER_SEC * PARTIAL_EVERY_SEC:
                busy = True
                text = await asyncio.to_thread(_transcribe, model, bytes(buf))
                last_transcribed = len(buf)
                busy = False
                await ws.send_json({"text": text, "final": False})
        elif msg.type == web.WSMsgType.TEXT and msg.data.strip() == "eos":
            text = await asyncio.to_thread(_transcribe, model, bytes(buf))
            await ws.send_json({"text": text, "final": True})
            break
        elif msg.type == web.WSMsgType.ERROR:
            log.warning("ws error: %s", ws.exception())
            break
    log.info("transcription socket closed")
    return ws


async def _on_startup(app: web.Application) -> None:
    args = app["args"]
    log.info("loading whisper model=%s device=%s", args.model, args.device)
    app["model"] = WhisperModel(args.model, device=args.device, compute_type=args.compute_type)
    app["registration"] = await register_runner(
        args.orchestrator,
        secret=args.orchSecret,
        runner_url=args.runner_url,
        app=APP_ID,
        price_per_unit=args.price,
        pixels_per_unit=args.pixels_per_unit,
    )
    log.info("registered runner_id=%s app=%s", app["registration"].runner_id, APP_ID)


async def _on_cleanup(app: web.Application) -> None:
    with suppress(Exception):
        await app["registration"].close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    app = web.Application()
    app["args"] = args
    app.router.add_get("/transcribe", _handle_transcribe)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    web.run_app(app, host=args.host, port=DEFAULT_PORT, print=None)


if __name__ == "__main__":
    main()
