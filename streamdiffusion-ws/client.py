#!/usr/bin/env python3
"""streamdiffusion-ws client: drive the reverse-proxied StreamDiffusion app.

The contrast with the trickle examples (echo, streamdiffusion): the runner here
is daydream's StreamDiffusion realtime-img2img server, run UNMODIFIED. It speaks
no Livepeer protocol — the orchestrator just reverse-proxies its native HTTP/WS
endpoints. So there is no runner.py / sd.py in this example; the app IS their
container, and this client drives its protocol through the proxied app_url:
  - WS  {app_url}/api/ws/{uuid}      input : control + JPEG frames
  - GET {app_url}/api/stream/{uuid}  output: MJPEG (opening it also drives the
                                             server's per-frame "send_frame" pump)
  - POST {app_url}/api/blending      set the prompt (not a per-frame field here)

By default the prompt **auto-cycles** every --prompt-interval seconds from a
curated, SFW bank (prompts.py) — a hands-free art-style slideshow. Pass --prompt
to pin one instead.

Livepeer integration (grep `# Livepeer:`):
  1. reserve_session()   — discover the runner, reserve a (paid) session
  2. ws_connect()        — drive the app's native WS + MJPEG through the proxied app_url
  3. session.aclose()    — end the session (stops payments)

Input JPEGs are read as an MJPEG stream on stdin (pipe ffmpeg); the diffused MJPEG
is written to stdout (pipe to a player). Offchain/free by default.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from contextlib import suppress

import aiohttp

import prompts
from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.selection import reserve_session

DEFAULT_DISCOVERY = "http://localhost:8935/discovery"
APP_ID = "livepeer-sample/streamdiffusion-ws"
# Per-frame params the app keeps (prompt/seed/steps are set over REST, not per frame).
FRAME_PARAMS = {"resolution": "512x512 (1:1)", "width": 512, "height": 512}
SOI, EOI = b"\xff\xd8", b"\xff\xd9"  # JPEG start/end-of-image markers

log = logging.getLogger("streamdiffusion-ws-client")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run video through the reverse-proxied StreamDiffusion app.")
    p.add_argument("input", help="- to read an MJPEG stream from stdin (pipe ffmpeg's image2pipe/mjpeg)")
    p.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    p.add_argument("--prompt", default="", help="Pin a fixed prompt (disables auto-cycling).")
    p.add_argument("--prompt-interval", type=float, default=60.0, help="Seconds between auto-prompt changes.")
    p.add_argument("--output", default="-", help="output MJPEG file, or - for stdout (pipe to ffplay -f mjpeg)")
    p.add_argument("--signer", default="", help="remote signer URL; omit for the offchain (free) path")
    p.add_argument("--payment-interval", type=float, default=3.0)
    return p.parse_args()


def _split_jpegs(buf: bytearray) -> list[bytes]:
    # Pull every complete JPEG (SOI..EOI) out of buf, leaving the partial tail behind.
    frames: list[bytes] = []
    while True:
        start = buf.find(SOI)
        if start < 0:
            break
        end = buf.find(EOI, start + 2)
        if end < 0:
            del buf[:start]  # drop bytes before the SOI; keep the partial frame
            break
        frames.append(bytes(buf[start : end + 2]))
        del buf[: end + 2]
    return frames


async def _stdin_reader() -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    proto = asyncio.StreamReaderProtocol(reader)
    await asyncio.get_event_loop().connect_read_pipe(lambda: proto, sys.stdin.buffer)
    return reader


async def _pump_input(reader: asyncio.StreamReader, state: dict) -> None:
    # Keep only the LATEST decoded input JPEG; the WS loop sends it on demand. This is
    # a natural drop-to-latest, so a slow diffuser never builds an input backlog.
    buf = bytearray()
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            break
        buf += chunk
        for frame in _split_jpegs(buf):
            state["latest"] = frame
    state["eof"] = True


async def _set_prompt(http: aiohttp.ClientSession, base: str, prompt: str) -> None:
    with suppress(Exception):
        await http.post(f"{base}/api/blending", json={"prompt_list": [[prompt, 1.0]]})
        log.info("prompt -> %r", prompt)


async def _cycle_prompts(http: aiohttp.ClientSession, base: str, interval: float) -> None:
    # Hands-free art-style slideshow: rotate through the curated SFW bank.
    prev = None
    while True:
        await asyncio.sleep(interval)
        prev = prompts.random_prompt(prev)
        await _set_prompt(http, base, prev)


async def _publish(ws: aiohttp.ClientWebSocketResponse, state: dict) -> None:
    # The server drives the cadence: each "send_frame" it sends (from the output
    # stream's pump) we answer with {"status":"next_frame"} -> params -> the latest
    # input JPEG. Empty bytes when none yet: the server just re-requests.
    async for msg in ws:
        if msg.type != aiohttp.WSMsgType.TEXT:
            continue
        status = msg.json().get("status")
        if status == "send_frame":
            await ws.send_json({"status": "next_frame"})
            await ws.send_json(FRAME_PARAMS)
            await ws.send_bytes(state.get("latest") or b"")
        elif status in ("timeout", "error"):
            log.info("server ended stream: %s", msg.data)
            break


async def _read_output(http: aiohttp.ClientSession, base: str, uid: str, write) -> None:
    # Opening the stream lazily builds the pipeline (first run compiles TensorRT engines,
    # which can take minutes), then drives the frame pump. No read timeout for that reason.
    url = f"{base}/api/stream/{uid}"
    async with http.get(url, timeout=aiohttp.ClientTimeout(total=None, sock_read=None)) as resp:
        log.info("output stream open: %s (%s)", url, resp.status)
        buf = bytearray()
        async for chunk in resp.content.iter_chunked(65536):
            buf += chunk
            for frame in _split_jpegs(buf):  # pull JPEGs straight out of the multipart body
                write(frame)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    if args.input.strip() != "-":
        raise SystemExit("this example reads an MJPEG stream on stdin; pass - as the input")

    auto = not args.prompt.strip()
    first_prompt = args.prompt.strip() or prompts.random_prompt()

    output_stdout = args.output.strip() in {"-", "stdout"}
    fh = sys.stdout.buffer if output_stdout else open(args.output, "wb")

    def write(jpeg: bytes) -> None:
        fh.write(jpeg)
        if output_stdout:
            fh.flush()

    session = None
    try:
        session = await reserve_session(  # Livepeer: 1
            discovery_url=args.discovery,
            app=APP_ID,
            signer_url=args.signer.strip() or None,
            payment_interval=args.payment_interval,
        )
        session.start_payments()  # on-chain: keep the session funded; no-op offchain
        base = session.app_url.rstrip("/")
        uid = str(uuid.uuid4())  # FastAPI validates the path as a real UUID
        log.info("session_id=%s app_url=%s", session.session_id, base)

        # ssl=False: the orchestrator proxy uses a self-signed cert on localhost.
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as http:
            await _set_prompt(http, base, first_prompt)
            log.info("auto-cycling every %ss" % args.prompt_interval if auto else "prompt pinned")

            # Connect the WS up front (surfaces a failure immediately), then run the
            # input pump, the output-stream reader, the frame publisher, and (auto)
            # the prompt cycler together.
            ws = await http.ws_connect(f"{base}/api/ws/{uid}", max_msg_size=0)  # Livepeer: 2
            log.info("ws connected")
            state: dict = {"latest": None, "eof": False}
            reader = await _stdin_reader()
            pump = asyncio.create_task(_pump_input(reader, state))
            workers = [
                asyncio.create_task(_read_output(http, base, uid, write)),
                asyncio.create_task(_publish(ws, state)),
            ]
            if auto:
                workers.append(asyncio.create_task(_cycle_prompts(http, base, args.prompt_interval)))
            try:
                await pump  # runs until the input stream (stdin) hits EOF
            finally:
                for t in workers:
                    t.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
                with suppress(Exception):
                    await ws.close()
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if not output_stdout:
            fh.close()
        if session is not None:
            with suppress(Exception):
                await session.aclose()  # Livepeer: 3


if __name__ == "__main__":
    asyncio.run(main())
