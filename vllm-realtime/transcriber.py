#!/usr/bin/env python3
"""Transcriber backends for the vllm-realtime example.

The runner stays backend-agnostic: it feeds raw PCM in, reads normalized
events out, and can push live settings updates mid-stream.

Shared interface (Protocol)
---------------------------
    open()                        # connect / start
    feed(pcm: bytes)              # PCM16LE mono 16 kHz audio chunk
    events() -> AsyncIterator     # yields {"type": "delta"|"done", "text": str}
    apply_settings(dict)          # forward a session.update mid-stream
    close()                       # no more audio; flush final transcript, end events()

Backends
--------
- ``MockTranscriber``          — no GPU, no vLLM. Fabricates plausible, time-paced
                                 transcription events from canned text so the whole
                                 Trickle + WebSocket pipeline is exercisable on a laptop.
                                 This is the default (``TRANSCRIBER=mock``).
- ``VllmRealtimeTranscriber``  — opens a *local* WebSocket to vLLM's ``/v1/realtime``
                                 endpoint (co-located in the same compose, never proxied
                                 by the orchestrator) and translates its events into ours.
                                 Use on a GPU box with ``TRANSCRIBER=vllm``.

NOTE (R1): vLLM's realtime event names are still settling. The vLLM backend
checks several candidate event keys. Validate against your vLLM build.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from contextlib import suppress
from typing import AsyncIterator, Optional, Protocol, runtime_checkable

log = logging.getLogger("vllm-realtime.transcriber")

# PCM16LE mono @ 16 kHz: 2 bytes/sample × 16000 samples/s = 32000 bytes/s.
PCM_BYTES_PER_SECOND = 16000 * 2

_END = object()  # sentinel to close events()


@runtime_checkable
class Transcriber(Protocol):
    async def open(self) -> None: ...
    async def feed(self, pcm: bytes) -> None: ...
    def events(self) -> AsyncIterator[dict]: ...
    async def apply_settings(self, settings: dict) -> None: ...
    async def close(self) -> None: ...


def make_transcriber(language: str = "") -> Transcriber:
    """Pick a backend from the TRANSCRIBER env var (default: mock)."""
    backend = os.getenv("TRANSCRIBER", "mock").strip().lower()
    if backend == "vllm":
        url = os.getenv("VLLM_REALTIME_URL", "ws://vllm:8000/v1/realtime")
        model = os.getenv(
            "VLLM_REALTIME_MODEL", "mistralai/Voxtral-Mini-4B-Realtime-2602"
        )
        log.info("using vLLM realtime backend url=%s model=%s language=%r", url, model, language)
        return VllmRealtimeTranscriber(url=url, model=model, language=language)
    log.info("using mock transcriber (no GPU required)")
    return MockTranscriber()


# --------------------------------------------------------------------------- #
# Mock backend
# --------------------------------------------------------------------------- #

_MOCK_SCRIPT = (
    "hello and welcome to the livepeer realtime transcription demo "
    "this transcript is produced by a mock backend so you can test the "
    "trickle pipeline without a gpu it streams words as your audio is "
    "consumed and reports a final result when the stream ends"
).split()

_MOCK_WORDS_PER_SECOND = 3.0


class MockTranscriber:
    """Fake transcriber: emits canned words paced to the audio duration fed."""

    def __init__(self, script: Optional[list[str]] = None) -> None:
        self._script = list(script) if script is not None else list(_MOCK_SCRIPT)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._bytes_fed = 0
        self._words_emitted = 0
        self._closed = False

    async def open(self) -> None:
        pass

    async def feed(self, pcm: bytes) -> None:
        if self._closed:
            return
        self._bytes_fed += len(pcm)
        seconds = self._bytes_fed / PCM_BYTES_PER_SECOND
        target = min(len(self._script), int(seconds * _MOCK_WORDS_PER_SECOND))
        while self._words_emitted < target:
            word = self._script[self._words_emitted]
            self._words_emitted += 1
            await self._queue.put({"type": "delta", "text": word + " "})

    async def apply_settings(self, settings: dict) -> None:
        # Mock ignores settings; log so the WS path is visibly exercised.
        log.debug("mock: ignoring session.update %s", settings)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        while self._words_emitted < len(self._script):
            word = self._script[self._words_emitted]
            self._words_emitted += 1
            await self._queue.put({"type": "delta", "text": word + " "})
        full = " ".join(self._script[: self._words_emitted])
        await self._queue.put({"type": "done", "text": full})
        await self._queue.put(_END)

    async def events(self) -> AsyncIterator[dict]:
        while True:
            item = await self._queue.get()
            if item is _END:
                return
            yield item


# --------------------------------------------------------------------------- #
# vLLM realtime backend
# --------------------------------------------------------------------------- #

# Candidate event names across vLLM realtime builds (R1).
_VLLM_DELTA_TYPES = {
    "transcription.delta",
    "response.audio_transcript.delta",
    "conversation.item.input_audio_transcription.delta",
}
_VLLM_DONE_TYPES = {
    "transcription.done",
    "transcription.completed",
    "response.audio_transcript.done",
    "conversation.item.input_audio_transcription.completed",
}


def _extract_text(event: dict) -> str:
    for key in ("delta", "text", "transcript"):
        value = event.get(key)
        if isinstance(value, str):
            return value
    return ""


class VllmRealtimeTranscriber:
    """Bridge to a local vLLM ``/v1/realtime`` WebSocket.

    The connection is local to the compose network and never proxied by the
    orchestrator, so the lack of external WebSocket passthrough is not a blocker.
    """

    def __init__(self, *, url: str, model: str, language: str = "") -> None:
        self._url = url
        self._model = model
        self._language = language
        self._ws = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._recv_task: Optional[asyncio.Task] = None
        self._closed = False
        self._started = False
        self._done_event = asyncio.Event()
        self._opened = asyncio.Event()

    async def open(self) -> None:
        import websockets  # lazy import so mock path needs no ws dep

        self._ws = await websockets.connect(self._url, max_size=None)
        # vLLM validates the model from the TOP LEVEL of the session.update
        # event (not a nested "session" object); unknown fields are ignored.
        update: dict = {"type": "session.update", "model": self._model}
        if self._language:
            update["language"] = self._language  # hint; ignored by vLLM 0.24
        await self._ws.send(json.dumps(update))
        self._recv_task = asyncio.create_task(self._recv_loop())
        self._opened.set()

    async def _recv_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    event = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                etype = event.get("type", "")
                if etype in _VLLM_DELTA_TYPES:
                    await self._queue.put({"type": "delta", "text": _extract_text(event)})
                elif etype in _VLLM_DONE_TYPES:
                    await self._queue.put({"type": "done", "text": _extract_text(event)})
                    self._done_event.set()
                elif etype == "error":
                    log.warning("vLLM realtime error: %s", event)
        except Exception as exc:
            log.warning("vLLM recv loop ended: %s", exc)
        finally:
            await self._queue.put(_END)

    async def feed(self, pcm: bytes) -> None:
        if self._closed or self._ws is None:
            return
        await self._ws.send(
            json.dumps(
                {
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm).decode("ascii"),
                }
            )
        )
        if not self._started:
            # A non-final commit starts generation; vLLM then streams deltas
            # while continuing to consume appended audio.
            self._started = True
            await self._ws.send(
                json.dumps({"type": "input_audio_buffer.commit", "final": False})
            )

    async def apply_settings(self, settings: dict) -> None:
        """Forward a session.update from the client mid-stream to vLLM."""
        # The client can send settings the instant its WS connects, which may
        # beat open() finishing — wait briefly instead of dropping the update.
        try:
            await asyncio.wait_for(self._opened.wait(), timeout=5)
        except asyncio.TimeoutError:
            log.warning("dropping session.update; vLLM connection not open: %s", settings)
            return
        if self._closed or self._ws is None:
            return
        # vLLM requires "model" on every session.update; client settings ride
        # along at the top level (unknown fields are ignored by the server).
        await self._ws.send(
            json.dumps({"type": "session.update", "model": self._model, **settings})
        )
        log.info("forwarded session.update to vLLM: %s", settings)

    async def events(self) -> AsyncIterator[dict]:
        while True:
            item = await self._queue.get()
            if item is _END:
                return
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ws is not None:
            with suppress(Exception):
                # final=True ends the audio stream; vLLM flushes the remaining
                # transcript and emits transcription.done.
                await self._ws.send(
                    json.dumps({"type": "input_audio_buffer.commit", "final": True})
                )
            with suppress(Exception):
                await asyncio.wait_for(self._done_event.wait(), timeout=60)
            with suppress(Exception):
                await self._ws.close()


# --------------------------------------------------------------------------- #
# Derived metrics
# --------------------------------------------------------------------------- #

_POSITIVE = {
    "good", "great", "love", "welcome", "happy", "nice", "thanks", "thank",
    "excellent", "awesome", "wonderful", "fast", "easy", "win", "best",
}
_NEGATIVE = {
    "bad", "hate", "slow", "broken", "error", "fail", "failed", "wrong",
    "terrible", "awful", "hard", "bug", "crash", "worst",
}


def word_count(text: str) -> int:
    return len(text.split())


def simple_sentiment(text: str) -> str:
    """Tiny lexicon sentiment: 'pos' | 'neg' | 'neu'."""
    score = 0
    for token in text.lower().split():
        word = token.strip(".,!?;:\"'")
        if word in _POSITIVE:
            score += 1
        elif word in _NEGATIVE:
            score -= 1
    if score > 0:
        return "pos"
    if score < 0:
        return "neg"
    return "neu"
