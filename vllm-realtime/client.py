#!/usr/bin/env python3
"""vllm-realtime client: stream audio in over Trickle, receive transcript over WebSocket.

Flow
----
1. reserve_session via discovery (optionally on-chain with --signer)
2. POST /transcribe {language} → {in: <trickle_url>, ws: "/ws"}
3. Connect WebSocket to wss://orchestrator/ws to receive transcript events
4. Optionally send {"type": "session.update", "session": {...}} mid-stream
   to adjust settings (language etc.) without stopping the stream
5. Publish PCM16/16 kHz audio to the Trickle in channel, paced to real time
6. Drain transcript events from the WebSocket until "done"
7. Release the session

Pass --signer <url> for the on-chain (paid) path.
Pass --language <code> to set the transcription language (e.g. "en", "fr").
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import ssl
import struct
import wave
from contextlib import suppress

import aiohttp

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.http import post_json
from livepeer_gateway.live_runner import stop_runner_session
from livepeer_gateway.selection import reserve_session
from livepeer_gateway.trickle_publisher import TricklePublisher

DEFAULT_DISCOVERY = "http://localhost:8935/discovery"
APP_ID = "livepeer-sample/vllm-realtime"

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # PCM16 mono
FRAME_SECONDS = 0.04               # 40 ms frames per Trickle write
SEGMENT_SECONDS = 0.5              # one Trickle segment per ~0.5 s of audio
IN_MIME = "audio/raw"

log = logging.getLogger("vllm-realtime-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the vllm-realtime Live Runner demo.")
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument(
        "--input", default="",
        help="Path to a 16 kHz mono 16-bit PCM WAV. Omit to synthesize a test tone.",
    )
    parser.add_argument(
        "--seconds", type=float, default=6.0,
        help="Seconds of synthesized audio when --input is omitted.",
    )
    parser.add_argument(
        "--no-realtime", action="store_true",
        help="Publish as fast as possible instead of pacing to wall clock.",
    )
    parser.add_argument(
        "--language", default="",
        help="Transcription language code (e.g. 'en', 'fr'). Sent as a live "
             "session.update after connecting — demonstrates mid-stream settings.",
    )
    parser.add_argument(
        "--signer", default="",
        help="Remote signer base URL for the on-chain (paid) path.",
    )
    return parser.parse_args()


def _load_pcm(path: str) -> bytes:
    """Read a WAV as raw PCM16 mono bytes; warn if format differs from 16 kHz/mono/16-bit."""
    with wave.open(path, "rb") as wav:
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        rate = wav.getframerate()
        frames = wav.readframes(wav.getnframes())
    if (channels, width, rate) != (1, 2, SAMPLE_RATE):
        log.warning(
            "input is %d ch / %d-bit / %d Hz; expected mono/16-bit/16 kHz. "
            "Fine for mock; resample for real vLLM.",
            channels, width * 8, rate,
        )
    return frames


def _synthesize_pcm(seconds: float) -> bytes:
    """A quiet 220 Hz tone — gives the pipeline real bytes to move (mock-friendly)."""
    total = int(seconds * SAMPLE_RATE)
    out = bytearray()
    for n in range(total):
        sample = int(3000 * math.sin(2 * math.pi * 220 * n / SAMPLE_RATE))
        out += struct.pack("<h", sample)
    return bytes(out)


def _make_ssl_ctx() -> ssl.SSLContext:
    """TLS context that skips verification — orchestrator uses a self-signed cert."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _read_transcript(
    ws: aiohttp.ClientWebSocketResponse,
    done: asyncio.Event,
) -> None:
    """Print transcript + metrics as JSON events arrive on the WebSocket."""
    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                break
            try:
                data = json.loads(msg.data)
            except ValueError:
                continue
            kind = data.get("type")
            transcript = data.get("transcript", "")
            if kind == "done":
                print(
                    f"\n[done] {transcript!r}  "
                    f"words={data.get('word_count')} sentiment={data.get('sentiment')}"
                )
                break
            print(
                f"[delta] +{data.get('delta', '').strip()!r}  "
                f"words={data.get('word_count')} sentiment={data.get('sentiment')}"
            )
    except Exception as exc:
        log.warning("transcript reader ended: %s", exc)
    finally:
        done.set()


async def _publish_pcm(in_url: str, pcm: bytes, realtime: bool) -> None:
    """Publish PCM to the Trickle input channel, ~0.5 s per segment, 40 ms per frame."""
    frame_bytes = int(FRAME_SECONDS * BYTES_PER_SECOND)
    seg_bytes = int(SEGMENT_SECONDS * BYTES_PER_SECOND)
    async with TricklePublisher(in_url, IN_MIME) as pub:
        for seg_start in range(0, len(pcm), seg_bytes):
            segment = pcm[seg_start : seg_start + seg_bytes]
            writer = await pub.next()
            async with writer:
                for off in range(0, len(segment), frame_bytes):
                    await writer.write(segment[off : off + frame_bytes])
                    if realtime:
                        await asyncio.sleep(FRAME_SECONDS)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    signer_url = args.signer.strip() or None

    pcm = _load_pcm(args.input) if args.input else _synthesize_pcm(args.seconds)
    log.info("audio: %.1f s (%d bytes)", len(pcm) / BYTES_PER_SECOND, len(pcm))

    ssl_ctx = _make_ssl_ctx()
    session = None
    try:
        session = await reserve_session(
            discovery_url=args.discovery,
            app=APP_ID,
            signer_url=signer_url,
        )
        log.info("session_id=%s app_url=%s", session.session_id, session.app_url)

        # POST /transcribe with optional language — runner mints the Trickle in channel.
        result = await post_json(
            session.app_url.rstrip("/") + "/transcribe",
            {"language": args.language},
        )
        in_url = result["in"]

        # Build the WebSocket URL from the session's app_url (orchestrator proxies it).
        ws_url = (
            session.app_url
            .replace("https://", "wss://")
            .replace("http://", "ws://")
            .rstrip("/") + "/ws"
        )
        log.info("trickle in=%s  ws=%s", in_url, ws_url)

        async with aiohttp.ClientSession() as http:
            async with http.ws_connect(ws_url, ssl=ssl_ctx, heartbeat=20) as ws:
                # Send a live session.update immediately after connecting.
                # This demonstrates mid-stream settings adjustment: language can be
                # changed here (or at any point while audio is still flowing) without
                # restarting the stream.
                if args.language:
                    await ws.send_str(
                        json.dumps({
                            "type": "session.update",
                            "session": {"language": args.language},
                        })
                    )
                    log.info("sent live session.update language=%r", args.language)

                done = asyncio.Event()
                reader = asyncio.create_task(_read_transcript(ws, done))

                await _publish_pcm(in_url, pcm, realtime=not args.no_realtime)

                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(done.wait(), timeout=30)
                reader.cancel()
                with suppress(Exception):
                    await reader

    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)


if __name__ == "__main__":
    asyncio.run(main())
