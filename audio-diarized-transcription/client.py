#!/usr/bin/env python3
"""Client for the audio-diarized-transcription runner — one session, every surface.

Reserves ONE Livepeer session and, over the single proxied `app_url`, drives the
runner the way it's meant to run on the network. It can exercise all three of the
runner's surfaces; a background payment pump funds the session per second,
decoupled from the request bodies (so native multipart and binary WS frames go
straight through — no base64, no JSON-only `call_runner` in the way).

  default    non-live bounded transcription   POST /v1/audio/transcriptions        (multipart)
  --stream   live true-streaming              WS   /v1/audio/transcriptions/stream  (binary PCM)
  --live     live stateful session            POST …/live/sessions  →  /{id}/audio  →  GET  →  /finish
  --all      all three, on the one session

Livepeer integration (grep `# Livepeer:`):
  1. reserve_session()   — discover + reserve a (paid) session
  2. start_payments()    — keep it funded on a timer (no-op offchain)
  3. raw aiohttp on app_url — multipart POST / ws_connect / stateful HTTP (no call_runner)
  4. session.aclose()    — stop payments + release the session

    uv run client.py sample.wav                 # bounded, through the orchestrator
    uv run client.py sample.wav --all            # every surface, one session
    uv run client.py sample.wav --direct http://localhost:8080   # skip Livepeer (sanity check)

Offchain/free by default; pass --signer <url> for the on-chain paid path.
--stream/--live/--all need a 16 kHz mono 16-bit WAV (the streaming PCM contract):
    ffmpeg -i in.mp3 -ar 16000 -ac 1 -c:a pcm_s16le sample.wav
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import uuid
import wave
from contextlib import suppress
from pathlib import Path

import aiohttp

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.selection import reserve_session

APP_ID = "moatus/audio-diarized-transcription"
DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
TX = "/v1/audio/transcriptions"
LIVE = "/v1/audio/diarized-transcriptions/live/sessions"

log = logging.getLogger("audio-diarized-transcription-client")


async def _bounded(
    http: aiohttp.ClientSession, base: str, wav: bytes, num_speakers: int | None
) -> None:
    # Non-live, OpenAI-compatible bounded route — native multipart over the session.
    form = aiohttp.FormData()
    form.add_field(
        "file", wav, filename="sample.wav", content_type="application/octet-stream"
    )
    form.add_field("model", "nemo-diarized-transcription-meeting-v0")
    form.add_field("response_format", "verbose_json")
    form.add_field("diarization", "true")
    form.add_field("timestamp_granularities[]", "segment")
    if num_speakers:
        form.add_field("num_speakers", str(num_speakers))
    log.info("bounded: POST %s", base + TX)
    async with http.post(f"{base}{TX}", data=form) as r:
        if r.status != 200:
            raise LivepeerGatewayError(f"bounded failed: {r.status} {await r.text()}")
        body = await r.json()
    print(f"\n[bounded] speakers={body.get('diarization', {}).get('speaker_count')}")
    print(body.get("speaker_labeled_text", "").rstrip())


async def _stream(
    http: aiohttp.ClientSession, base: str, wav_path: str, settle: float
) -> None:
    # Live true-streaming — binary 16 kHz mono int16 PCM frames over the same session.
    wf = wave.open(wav_path, "rb")
    if (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) != (16000, 1, 2):
        raise LivepeerGatewayError("stream needs 16 kHz mono 16-bit WAV (see --help)")
    log.info("stream: WS %s", base + TX + "/stream")
    ws = await http.ws_connect(f"{base}{TX}/stream", max_msg_size=0)

    async def _send() -> None:
        while True:
            frames = wf.readframes(1280)  # 0.08 s, paced in real time
            if not frames:
                break
            await ws.send_bytes(frames)
            await asyncio.sleep(0.08)
        # The runner (re)loads the streaming models per connection (~7 s) and buffers
        # frames meanwhile; let that drain before finalizing.
        await asyncio.sleep(settle)
        await ws.send_json({"type": "finish"})

    sender = asyncio.create_task(_send())
    print("\n[stream]")
    try:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            e = msg.json()
            kind = e.get("event_type")  # NB: runner uses event_type, not type
            if kind == "transcript.segment" and e.get("text"):
                tag = "~" if e.get("is_provisional") else " "
                print(f" {tag}{e.get('speaker', '')}: {e['text']}")
            elif kind == "transcript.session.finished":
                print(" finished")
                break
    finally:
        sender.cancel()
        with suppress(Exception):
            await sender
        with suppress(Exception):
            await ws.close()


async def _live(
    http: aiohttp.ClientSession, base: str, wav: bytes, num_speakers: int | None
) -> None:
    # Live stateful session — create -> ingest chunk(s) -> snapshot -> finish, all on one id.
    sid = f"client-{uuid.uuid4().hex[:8]}"
    log.info("live: create/ingest/get/finish session %s", sid)
    async with http.post(
        f"{base}{LIVE}",
        json={
            "session_id": sid,
            "preset": "meeting",
            "num_speakers": num_speakers or 2,
        },
    ) as r:
        r.raise_for_status()
    form = aiohttp.FormData()
    form.add_field(
        "file", wav, filename="sample.wav", content_type="application/octet-stream"
    )
    form.add_field("sequence_index", "0")
    async with http.post(f"{base}{LIVE}/{sid}/audio", data=form) as r:
        r.raise_for_status()
    async with http.get(f"{base}{LIVE}/{sid}") as r:
        snap = await r.json()
    async with http.post(
        f"{base}{LIVE}/{sid}/finish",
        json={"run_final_transcription": True, "include_words": True},
    ) as r:
        fin = await r.json()
    print(
        f"\n[live] chunks={snap.get('chunk_count')} speakers={fin.get('speaker_count')}"
    )
    for seg in fin.get("segments", []):
        if seg.get("text"):
            print(f"  {seg.get('speaker', '')}: {seg['text']}")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("audio_path")
    p.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    p.add_argument(
        "--signer", default="", help="Remote signer URL; omit for offchain (free)."
    )
    p.add_argument(
        "--direct",
        default="",
        help="Hit this runner URL directly, skipping Livepeer (e.g. http://localhost:8080).",
    )
    p.add_argument("--payment-interval", type=float, default=3.0)
    p.add_argument("--num-speakers", type=int, help="Exact speaker count, if known.")
    p.add_argument("--stream", action="store_true", help="Also run the live WS stream.")
    p.add_argument(
        "--live", action="store_true", help="Also run the stateful live-session flow."
    )
    p.add_argument(
        "--all", action="store_true", help="Run bounded + stream + live on one session."
    )
    p.add_argument(
        "--settle", type=float, default=14.0, help="Seconds to drain before WS finish."
    )
    args = p.parse_args()

    wav_path = args.audio_path
    if not Path(wav_path).is_file():
        raise SystemExit(f"no such file: {wav_path}")
    wav = Path(wav_path).read_bytes()
    do_stream = args.stream or args.all
    do_live = args.live or args.all

    async def drive(base: str) -> None:
        connector = aiohttp.TCPConnector(ssl=False)  # self-signed orchestrator cert
        async with aiohttp.ClientSession(connector=connector) as http:
            await _bounded(http, base, wav, args.num_speakers)
            if do_stream:
                await _stream(http, base, wav_path, args.settle)
            if do_live:
                await _live(http, base, wav, args.num_speakers)

    try:
        if args.direct.strip():
            await drive(args.direct.strip().rstrip("/"))
            return
        session = await reserve_session(  # Livepeer: 1
            discovery_url=args.discovery,
            app=APP_ID,
            signer_url=args.signer.strip() or None,
            payment_interval=args.payment_interval,
        )
        session.start_payments()  # Livepeer: 2 — timer-based, no-op offchain
        log.info("session_id=%s app_url=%s", session.session_id, session.app_url)
        try:
            await drive(
                session.app_url.rstrip("/")
            )  # Livepeer: 3 — raw HTTP/WS, no call_runner
        finally:
            with suppress(Exception):
                await session.aclose()  # Livepeer: 4
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc


if __name__ == "__main__":
    asyncio.run(main())
