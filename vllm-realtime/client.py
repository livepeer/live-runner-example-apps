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
6. Drain transcript events from the WebSocket until "done", then print the
   "stats" event the runner sends last (see stats.py)
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
from typing import Optional

import aiohttp

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.http import post_json
from livepeer_gateway.live_runner import stop_runner_session
from livepeer_gateway.selection import reserve_session
from livepeer_gateway.trickle_publisher import TricklePublisher, TricklePublisherStats

DEFAULT_DISCOVERY = "http://localhost:8935/discovery"
APP_ID = "livepeer-sample/vllm-realtime"

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2  # PCM16 mono
FRAME_SECONDS = 0.04               # 40 ms frames per Trickle write
SEGMENT_SECONDS = 0.5              # one Trickle segment per ~0.5 s of audio
IN_MIME = "audio/raw"

# How long to wait for the runner to finish reading what we published before
# closing the stream anyway. Generous: a cold backend can lag well behind.
DRAIN_TIMEOUT_SECONDS = 60.0

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
        help="Publish as fast as the runner can consume instead of pacing to "
             "wall clock. Removes the pacing floor, so the real-time factor "
             "measures actual backend throughput.",
    )
    parser.add_argument(
        "--language", default="",
        help="Language hint sent as a live session.update after connecting. This "
             "demonstrates the mid-stream settings-update transport, not a language "
             "switch: Voxtral on vLLM 0.24 ignores the language field (and the mock "
             "backend ignores all settings), so the value rides the path but does "
             "not change the transcript.",
    )
    parser.add_argument(
        "--signer", default="",
        help="Remote signer base URL for the on-chain (paid) path.",
    )
    parser.add_argument(
        "--insecure", action="store_true",
        help="Skip TLS verification on the transcript WebSocket. Local dev only, "
             "for an orchestrator with a self-signed cert; never in production.",
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


def _make_ssl_ctx(insecure: bool) -> ssl.SSLContext:
    """TLS context for the transcript WebSocket.

    Certificate and hostname verification are ON by default. Pass --insecure
    only for local development against an orchestrator with a self-signed cert;
    never against a public deployment.

    Note: this governs the WebSocket this client opens directly. The Livepeer
    gateway SDK currently hardcodes unverified TLS for its own HTTP/Trickle
    calls (livepeer_gateway/http.py), so --insecure cannot be enforced across
    the whole path from here — see FEEDBACK.md.
    """
    ctx = ssl.create_default_context()
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class _Progress:
    """How much of our published audio the runner reports it has consumed.

    Trickle deletes unread segments the instant the publisher closes, so
    publishing faster than the runner consumes silently loses audio. The runner
    reports its ingest position over the WebSocket; we wait for it to catch up
    before closing the stream. That turns "publish and hope" into backpressure.
    """

    def __init__(self) -> None:
        self.consumed = 0
        self._bump = asyncio.Event()

    def update(self, consumed: int) -> None:
        self.consumed = consumed
        self._bump.set()

    async def wait_for(self, target: int, timeout: float) -> bool:
        """Block until the runner has consumed `target` bytes. False on timeout."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.consumed < target:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            self._bump.clear()
            if self.consumed >= target:
                return True
            with suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._bump.wait(), timeout=remaining)
        return True


def _print_stats(data: dict, publisher: Optional[TricklePublisherStats]) -> None:
    """Render the whole path: our publish side, then the runner's stats event."""
    t = data.get("transcription") or {}
    w = data.get("websocket") or {}
    k = data.get("trickle") or {}

    print("\n──── performance ────")
    print(f"  audio                {t.get('audio_seconds')} s")
    print(f"  wall clock           {t.get('wall_seconds')} s")
    print(f"  real-time factor     {t.get('realtime_factor')}x")
    print(f"  time to first word   {t.get('time_to_first_word_s')} s")
    print(f"  finalize tail        {t.get('finalize_tail_s')} s")
    print(f"  words / deltas       {t.get('words')} / {t.get('deltas')}")

    # Three hops, in order: we publish -> the runner ingests -> it streams back.
    # The first two come free from the SDK; the third we count ourselves.
    if publisher is not None:
        rate = (
            publisher.bytes_submitted_to_transport / publisher.elapsed_s
            if publisher.elapsed_s > 0
            else 0.0
        )
        print(
            "\n  trickle publish (SDK)  "
            f"segments={publisher.segments_completed}/{publisher.segments_started} "
            f"bytes={publisher.bytes_submitted_to_transport} "
            f"({rate / 1000:.0f} kB/s) posts_ok={publisher.post_success} "
            f"failed={publisher.segments_failed} retries={publisher.post_retries_no_body_consumed}"
        )
    print(
        "  trickle ingest  (SDK)  "
        f"segments={k.get('segments_delivered')} seq_gaps={k.get('seq_gap_events')} "
        f"retries={k.get('get_retries')} failures={k.get('get_failures')} "
        f"stall={k.get('wait_ms_total')}ms"
    )
    print(
        "  websocket out   (app)  "
        f"events={w.get('events_sent')} deltas={w.get('deltas_sent')} "
        f"bytes={w.get('bytes_sent')} failures={w.get('send_failures')} "
        f"cmds_in={w.get('commands_received')}"
    )
    print(
        "\n  note: audio is paced to real time by default, so wall clock cannot drop\n"
        '  below the audio duration — the real-time factor reads as "the pipeline\n'
        '  kept up", not as backend speed. Re-run with --no-realtime to remove the\n'
        "  pacing floor and measure actual throughput."
    )


async def _read_transcript(
    ws: aiohttp.ClientWebSocketResponse,
    done: asyncio.Event,
    progress: _Progress,
    stats_sink: dict,
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
            if kind == "progress":
                # Ingest position, not transcript — drives backpressure only.
                progress.update(data.get("audio_bytes", 0))
                continue
            if kind == "stats":
                # Sent after "done"; last event of the session. Hand it back to
                # main, which pairs it with our publish-side stats and prints.
                stats_sink.update(data)
                break
            if kind == "done":
                print(
                    f"\n[done] {transcript!r}  "
                    f"words={data.get('word_count')} sentiment={data.get('sentiment')}"
                )
                continue
            print(
                f"[delta] +{data.get('delta', '').strip()!r}  "
                f"words={data.get('word_count')} sentiment={data.get('sentiment')}"
            )
    except Exception as exc:
        log.warning("transcript reader ended: %s", exc)
    finally:
        done.set()


async def _publish_pcm(
    in_url: str, pcm: bytes, realtime: bool, progress: _Progress
) -> TricklePublisherStats:
    """Publish PCM to the Trickle input channel, ~0.5 s per segment, 40 ms per frame.

    A Trickle channel keeps no backlog — a subscriber reads the live edge, and an
    earlier index answers 470 rather than replaying. So publishing a segment
    before the runner has read the previous one does not queue it, it destroys
    it. After each segment we wait for the runner to report that it consumed
    everything so far, which keeps at most one segment in flight and makes the
    runner's ingest rate the publish rate. Under realtime pacing the wait is a
    no-op (we are already slower than the backend); it is what lets --no-realtime
    run flat out without shredding the audio.
    """
    frame_bytes = int(FRAME_SECONDS * BYTES_PER_SECOND)
    seg_bytes = int(SEGMENT_SECONDS * BYTES_PER_SECOND)
    published = 0
    async with TricklePublisher(in_url, IN_MIME) as pub:
        for seg_start in range(0, len(pcm), seg_bytes):
            segment = pcm[seg_start : seg_start + seg_bytes]
            writer = await pub.next()
            async with writer:
                for off in range(0, len(segment), frame_bytes):
                    await writer.write(segment[off : off + frame_bytes])
                    if realtime:
                        await asyncio.sleep(FRAME_SECONDS)
            published += len(segment)

            if not await progress.wait_for(published, timeout=DRAIN_TIMEOUT_SECONDS):
                log.warning(
                    "runner stalled at %d/%d bytes after %.0fs; stopping publish "
                    "rather than overwriting audio it has not read",
                    progress.consumed, published, DRAIN_TIMEOUT_SECONDS,
                )
                break

    # Read after close so the counters include the final segment flush.
    return pub.get_stats()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args()
    signer_url = args.signer.strip() or None

    pcm = _load_pcm(args.input) if args.input else _synthesize_pcm(args.seconds)
    log.info("audio: %.1f s (%d bytes)", len(pcm) / BYTES_PER_SECOND, len(pcm))

    ssl_ctx = _make_ssl_ctx(args.insecure)
    session = None
    try:
        # On the paid path, reserve_session answers the 402 challenge AND keeps
        # the session funded: it starts a background task that sends continuing
        # payments for as long as we hold the session, not just the one-shot
        # reservation. We keep `session` for the whole stream and release it in
        # the finally below (stop_runner_session stops funding, then frees the
        # reservation), so a long transcription cannot lapse for non-payment.
        # Continuing payments need an orchestrator with the session-scoped
        # payment URL (go-livepeer #4008, in the sha-cc49228 image); on v0.9.0
        # only the reservation payment is possible.
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
                progress = _Progress()
                stats_sink: dict = {}
                reader = asyncio.create_task(
                    _read_transcript(ws, done, progress, stats_sink)
                )

                publisher_stats = await _publish_pcm(
                    in_url, pcm, realtime=not args.no_realtime, progress=progress
                )

                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(done.wait(), timeout=30)
                reader.cancel()
                with suppress(Exception):
                    await reader

                if stats_sink:
                    _print_stats(stats_sink, publisher_stats)
                else:
                    log.warning("no stats event received from the runner")

    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    finally:
        if session is not None:
            with suppress(Exception):
                await stop_runner_session(session)


if __name__ == "__main__":
    asyncio.run(main())
