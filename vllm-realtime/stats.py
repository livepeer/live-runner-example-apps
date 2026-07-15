#!/usr/bin/env python3
"""Performance stats for the vllm-realtime example.

Why this module exists
----------------------
The SDK meters the **Trickle** path for free — ``TrickleSubscriber.get_stats()``
returns a ``TrickleSubscriberStats`` (segments delivered, sequence gaps, retries,
stall time). There is no equivalent for a **WebSocket**: an app that streams its
results out over one has to meter that path itself.

This module is the missing half, shaped like the SDK's so the two read alike:

    WebSocketMeter.get_stats()     -> WebSocketStats      # transport, the WS out path
    TranscriptionMeter.get_stats() -> TranscriptionStats  # domain, latency + throughput

Two things worth knowing if you copy this into your own app:

1. The SDK's frame counters (``MediaOutputStats.audio_frames_decoded``) belong to
   the media/AV pipeline, which decodes encoded media into frames. This example
   publishes *raw* PCM16, so there is no decoder in the path and those counters
   do not exist for us. Audio duration is derived from bytes instead.

2. ``realtime_factor`` is wall time / audio duration, so it measures the whole
   pipeline, not the model. Under the client's default realtime pacing, wall time
   can never drop below the audio duration and the factor is pinned just above
   1.0 however fast the backend is — there, read it as "did the pipeline keep
   up?". Run the client with ``--no-realtime`` to remove the pacing floor and let
   it measure real throughput; the client applies backpressure from this app's
   ``progress`` events, so publishing unpaced no longer outruns Trickle's segment
   retention. ``time_to_first_word_s`` moves with lead-in silence in the audio;
   ``finalize_tail_s`` is the stable latency figure.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

# PCM16LE mono @ 16 kHz: 2 bytes/sample × 16000 samples/s.
PCM_BYTES_PER_SECOND = 16000 * 2


def _round(value: Optional[float], places: int = 3) -> Optional[float]:
    return None if value is None else round(value, places)


# --------------------------------------------------------------------------- #
# WebSocket transport stats (the half the SDK does not provide)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class WebSocketStats:
    """Counters for the transcript WebSocket, mirroring the SDK's Trickle stats."""

    elapsed_s: float
    events_sent: int
    deltas_sent: int
    bytes_sent: int
    send_failures: int
    commands_received: int

    @property
    def send_rate_hz(self) -> float:
        return self.events_sent / self.elapsed_s if self.elapsed_s > 0 else 0.0

    def to_json(self) -> dict:
        return {
            "elapsed_s": _round(self.elapsed_s),
            "events_sent": self.events_sent,
            "deltas_sent": self.deltas_sent,
            "bytes_sent": self.bytes_sent,
            "send_failures": self.send_failures,
            "commands_received": self.commands_received,
            "send_rate_hz": _round(self.send_rate_hz),
        }

    def __str__(self) -> str:
        return (
            "WebSocketStats("
            f"elapsed_s={self.elapsed_s:.1f}, "
            f"events_sent={self.events_sent}, "
            f"deltas_sent={self.deltas_sent}, "
            f"bytes_sent={self.bytes_sent}, "
            f"send_failures={self.send_failures}, "
            f"commands_received={self.commands_received}"
            ")"
        )


class WebSocketMeter:
    """Tracks what goes out over — and comes back in on — the transcript WebSocket."""

    def __init__(self) -> None:
        self._started_at = time.time()
        self._events_sent = 0
        self._deltas_sent = 0
        self._bytes_sent = 0
        self._send_failures = 0
        self._commands_received = 0

    def record_sent(self, event: dict, nbytes: int) -> None:
        self._events_sent += 1
        self._bytes_sent += nbytes
        if event.get("type") == "delta":
            self._deltas_sent += 1

    def record_send_failure(self) -> None:
        self._send_failures += 1

    def record_command(self) -> None:
        """A client → runner message (e.g. a live session.update)."""
        self._commands_received += 1

    def get_stats(self) -> WebSocketStats:
        return WebSocketStats(
            elapsed_s=max(0.0, time.time() - self._started_at),
            events_sent=self._events_sent,
            deltas_sent=self._deltas_sent,
            bytes_sent=self._bytes_sent,
            send_failures=self._send_failures,
            commands_received=self._commands_received,
        )


# --------------------------------------------------------------------------- #
# Transcription (domain) stats
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TranscriptionStats:
    """End-to-end latency and throughput for one transcription session."""

    audio_seconds: float
    wall_seconds: float
    realtime_factor: float
    time_to_first_word_s: Optional[float]
    finalize_tail_s: Optional[float]
    deltas: int
    words: int

    def to_json(self) -> dict:
        return {
            "audio_seconds": _round(self.audio_seconds),
            "wall_seconds": _round(self.wall_seconds),
            "realtime_factor": _round(self.realtime_factor),
            "time_to_first_word_s": _round(self.time_to_first_word_s),
            "finalize_tail_s": _round(self.finalize_tail_s),
            "deltas": self.deltas,
            "words": self.words,
        }

    def __str__(self) -> str:
        return (
            "TranscriptionStats("
            f"audio_s={self.audio_seconds:.2f}, "
            f"wall_s={self.wall_seconds:.2f}, "
            f"rtf={self.realtime_factor:.2f}, "
            f"first_word_s={self.time_to_first_word_s}, "
            f"finalize_tail_s={self.finalize_tail_s}, "
            f"deltas={self.deltas}, "
            f"words={self.words}"
            ")"
        )


class TranscriptionMeter:
    """Times the audio-in → transcript-out path.

    Timestamps are taken at the runner, so they exclude the client↔orchestrator
    hop; they measure what this app is responsible for.
    """

    def __init__(self) -> None:
        self._t_first_audio: Optional[float] = None
        self._t_first_delta: Optional[float] = None
        self._t_audio_end: Optional[float] = None
        self._t_done: Optional[float] = None
        self._bytes_in = 0
        self._deltas = 0
        self._words = 0

    @property
    def bytes_in(self) -> int:
        """Audio bytes consumed so far — reported to the client as backpressure."""
        return self._bytes_in

    def mark_audio(self, nbytes: int) -> None:
        if self._t_first_audio is None:
            self._t_first_audio = time.time()
        self._bytes_in += nbytes

    def mark_delta(self) -> None:
        """A non-empty transcript delta reached the client path."""
        if self._t_first_delta is None:
            self._t_first_delta = time.time()
        self._deltas += 1

    def mark_audio_end(self) -> None:
        """Last audio byte consumed — the clock for the finalize tail starts here."""
        if self._t_audio_end is None:
            self._t_audio_end = time.time()

    def mark_done(self, transcript: str) -> None:
        if self._t_done is None:
            self._t_done = time.time()
        self._words = len(transcript.split())

    def get_stats(self) -> TranscriptionStats:
        audio_seconds = self._bytes_in / PCM_BYTES_PER_SECOND
        end = self._t_done or self._t_audio_end or time.time()
        wall = (end - self._t_first_audio) if self._t_first_audio is not None else 0.0

        first_word = None
        if self._t_first_delta is not None and self._t_first_audio is not None:
            first_word = self._t_first_delta - self._t_first_audio

        tail = None
        if self._t_done is not None and self._t_audio_end is not None:
            tail = self._t_done - self._t_audio_end

        return TranscriptionStats(
            audio_seconds=audio_seconds,
            wall_seconds=max(0.0, wall),
            realtime_factor=(wall / audio_seconds) if audio_seconds > 0 else 0.0,
            time_to_first_word_s=first_word,
            finalize_tail_s=tail,
            deltas=self._deltas,
            words=self._words,
        )
