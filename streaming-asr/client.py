#!/usr/bin/env python3
"""streaming-asr client: reserve a session, stream audio over WebSocket, settle up.

One SDK call reserves a session; from there it's a standard WebSocket — the
orchestrator proxies the upgrade straight to the app. Audio is streamed up in
real-time-paced chunks; partial/final transcripts stream back.

Livepeer integration (grep `# Livepeer:`):
  1. reserve_session()      — discover the runner, reserve a session
  2. ws_connect()           — open the proxied WebSocket to the session URL
  3. stop_runner_session()  — end the session (settles payment on-chain)

Audio must be 16 kHz mono. Convert anything with ffmpeg:
  ffmpeg -i input.mp3 -ar 16000 -ac 1 sample.wav
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import ssl
import wave
from contextlib import suppress

import aiohttp

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.live_runner import stop_runner_session
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/streaming-asr"
SAMPLE_RATE = 16000
CHUNK_MS = 100

log = logging.getLogger("streaming-asr-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream audio to a Whisper Live Runner over WebSocket."
    )
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument("--file", required=True, help="16 kHz mono WAV to stream.")
    parser.add_argument(
        "--signer", default="", help="Remote signer base URL (on-chain/paid path)."
    )
    return parser.parse_args()


def _read_pcm(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        if (
            w.getframerate() != SAMPLE_RATE
            or w.getnchannels() != 1
            or w.getsampwidth() != 2
        ):
            raise SystemExit(
                f"ERROR: {path} must be 16 kHz mono 16-bit WAV (got {w.getframerate()}Hz, "
                f"{w.getnchannels()}ch, {w.getsampwidth() * 8}-bit). Convert with ffmpeg."
            )
        return w.readframes(w.getnframes())


async def _send(ws: aiohttp.ClientWebSocketResponse, pcm: bytes) -> None:
    step = SAMPLE_RATE * 2 * CHUNK_MS // 1000  # bytes per chunk
    for i in range(0, len(pcm), step):
        await ws.send_bytes(pcm[i : i + step])
        await asyncio.sleep(CHUNK_MS / 1000)  # pace at real time
    await ws.send_str("eos")


async def _recv(ws: aiohttp.ClientWebSocketResponse) -> None:
    # Run to the end of the stream, not to the first final: a clip with several
    # sentences produces one final per utterance, and the server closes after eos.
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            break
        data = msg.json()
        marker = "FINAL" if data.get("final") else "partial"
        print(f"[{marker}] {data.get('text', '')}")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    pcm = _read_pcm(args.file)
    signer_url = args.signer.strip() or None
    session = None
    try:
        session = await reserve_session(  # Livepeer: 1
            discovery_url=args.discovery,  # omit if the signer does discovery itself
            app=APP_ID,
            signer_url=signer_url,
        )
        log.info("session_id=%s app_url=%s", session.session_id, session.app_url)
        ws_url = (
            session.app_url.replace("https://", "wss://")
            .replace("http://", "ws://")
            .rstrip("/")
            + "/transcribe"
        )
        ctx = ssl.create_default_context()  # orchestrator serves a self-signed cert
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        async with aiohttp.ClientSession() as cs:
            async with cs.ws_connect(
                ws_url, ssl=ctx, heartbeat=20
            ) as ws:  # Livepeer: 2
                await asyncio.gather(_send(ws, pcm), _recv(ws))
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)  # Livepeer: 3


if __name__ == "__main__":
    asyncio.run(main())
