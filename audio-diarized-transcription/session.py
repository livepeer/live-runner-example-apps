#!/usr/bin/env python3
"""One Livepeer session, both transports — the schema that fits this runner.

The bounded `client.py` talks straight to the runner. This driver goes *through
the orchestrator*: it reserves ONE persistent session and then, over the single
proxied `app_url`, runs both of the runner's surfaces —

  - HTTP multipart : POST {app_url}/v1/audio/transcriptions        (bounded, diarized)
  - WebSocket      : WS   {app_url}/v1/audio/transcriptions/stream  (true streaming)

No `call_runner`, no base64. `aiohttp` sends **native multipart** and **binary WS
frames** straight through the orchestrator (it byte-forwards), while a background
**payment pump** funds the session per second — decoupled from the request bodies.
This is the `streamdiffusion-ws` pattern applied to audio: one reservation, HTTP
and WebSocket side by side, billed as one held session.

Livepeer integration (grep `# Livepeer:`):
  1. reserve_session()   — discover + reserve a (paid) session
  2. start_payments()    — keep it funded on a timer (no-op offchain)
  3. raw aiohttp on app_url — multipart POST and/or ws_connect (no call_runner)
  4. session.aclose()    — stop payments + release the session

    uv run session.py sample.wav            # bounded diarized transcription
    uv run session.py sample.wav --stream    # + true streaming, same session

Offchain/free by default; pass --signer <url> for the on-chain paid path.
Requires a 16 kHz mono 16-bit WAV for --stream (the runner's PCM contract);
convert with:  ffmpeg -i in.mp3 -ar 16000 -ac 1 -c:a pcm_s16le sample.wav
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import wave
from contextlib import suppress
from pathlib import Path

import aiohttp

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.selection import reserve_session

APP_ID = "moatus/audio-diarized-transcription"
DEFAULT_DISCOVERY = "https://localhost:8935/discovery"

log = logging.getLogger("audio-diarized-transcription-session")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Drive the runner through one Livepeer session."
    )
    p.add_argument("audio_path")
    p.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    p.add_argument(
        "--signer", default="", help="Remote signer URL; omit for offchain (free)."
    )
    p.add_argument("--payment-interval", type=float, default=3.0)
    p.add_argument("--num-speakers", type=int, help="Exact speaker count, if known.")
    p.add_argument(
        "--stream",
        action="store_true",
        help="Also run the WS stream on the same session.",
    )
    p.add_argument(
        "--settle",
        type=float,
        default=12.0,
        help="Seconds to drain before WS finish (per-connection model load).",
    )
    return p.parse_args()


async def _bounded(
    http: aiohttp.ClientSession, base: str, path: Path, num_speakers: int | None
) -> None:
    # HTTP multipart over the proxied session — the OpenAI-compatible bounded route.
    form = aiohttp.FormData()
    form.add_field(
        "file",
        path.read_bytes(),
        filename=path.name,
        content_type="application/octet-stream",
    )
    form.add_field("model", "nemo-diarized-transcription-meeting-v0")
    form.add_field("response_format", "verbose_json")
    form.add_field("diarization", "true")
    form.add_field("timestamp_granularities[]", "segment")
    form.add_field("timestamp_granularities[]", "word")
    if num_speakers:
        form.add_field("num_speakers", str(num_speakers))

    log.info("bounded: POST %s/v1/audio/transcriptions (multipart)", base)
    async with http.post(f"{base}/v1/audio/transcriptions", data=form) as resp:
        if resp.status != 200:
            raise LivepeerGatewayError(
                f"bounded call failed: {resp.status} {await resp.text()}"
            )
        body = await resp.json()

    diar = body.get("diarization", {})
    print(f"\nspeakers detected: {diar.get('speaker_count')}")
    print("speaker-labeled transcript:")
    print(body.get("speaker_labeled_text", "").rstrip())


async def _stream(
    http: aiohttp.ClientSession, base: str, path: Path, settle_seconds: float
) -> None:
    # WebSocket over the SAME proxied session — binary 16 kHz mono int16 PCM frames.
    wf = wave.open(str(path), "rb")
    if (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) != (16000, 1, 2):
        raise LivepeerGatewayError(
            "stream needs 16 kHz mono 16-bit WAV; convert with "
            "`ffmpeg -i in -ar 16000 -ac 1 -c:a pcm_s16le sample.wav`"
        )

    log.info("stream: WS %s/v1/audio/transcriptions/stream", base)
    ws = await http.ws_connect(f"{base}/v1/audio/transcriptions/stream", max_msg_size=0)

    async def _send() -> None:
        # 0.08 s frames (1280 samples * 2 bytes), paced in real time — the runner's
        # true-streaming contract. Larger/faster chunks starve the streaming models.
        while True:
            frames = wf.readframes(1280)
            if not frames:
                break
            await ws.send_bytes(frames)
            await asyncio.sleep(0.08)
        # The runner (re)loads the streaming models per connection (~several seconds)
        # and buffers frames meanwhile. Let that drain and emit before we finalize —
        # sending finish too early makes the server close with no transcript.
        await asyncio.sleep(settle_seconds)
        await ws.send_json({"type": "finish"})

    sender = asyncio.create_task(_send())
    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            event = msg.json()
            kind = event.get("event_type")  # NB: runner uses event_type, not type
            if kind == "transcript.segment" and event.get("text"):
                tag = "~" if event.get("is_provisional") else " "
                print(f"[stream]{tag}{event.get('speaker', '')}: {event['text']}")
            elif kind == "transcript.session.finished":
                print("[stream] finished")
                break
    finally:
        sender.cancel()
        with suppress(Exception):
            await sender
        with suppress(Exception):
            await ws.close()


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    path = Path(args.audio_path)
    if not path.is_file():
        raise SystemExit(f"no such file: {path}")

    session = None
    try:
        session = await reserve_session(  # Livepeer: 1
            discovery_url=args.discovery,
            app=APP_ID,
            signer_url=args.signer.strip() or None,
            payment_interval=args.payment_interval,
        )
        session.start_payments()  # Livepeer: 2 — timer-based, no-op offchain
        base = session.app_url.rstrip("/")
        log.info("session_id=%s app_url=%s", session.session_id, base)

        # ssl=False: the orchestrator proxy serves a self-signed cert on localhost.
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as http:  # Livepeer: 3
            await _bounded(http, base, path, args.num_speakers)
            if args.stream:
                await _stream(http, base, path, args.settle)
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await session.aclose()  # Livepeer: 4


if __name__ == "__main__":
    asyncio.run(main())
