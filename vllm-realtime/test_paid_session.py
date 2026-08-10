#!/usr/bin/env python3
"""Regression tests for the paid-session lifecycle in client.py.

The on-chain path must *keep the session funded for the whole stream and then
release it*, not just make the one-shot reservation payment. In the current SDK
that means: reserve_session (which starts background funding) is called with the
signer, the returned session object is held for the duration, and
stop_runner_session (which stops funding, then frees the reservation) runs on the
way out — including when the stream fails partway.

These tests pin that contract without a chain or an orchestrator by mocking the
SDK boundary. Run with:  uv run python -m unittest test_paid_session
"""
from __future__ import annotations

import contextlib
import sys
import unittest
from unittest import mock

import client


def _fake_session() -> mock.Mock:
    session = mock.Mock(name="LiveRunnerSession")
    session.session_id = "sess-test"
    session.app_url = "https://orch.test/apps/runner/session/sess-test/app"
    return session


class _FakeWS:
    """Transcript WebSocket that yields no messages, so the reader ends at once."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_str(self, payload: str) -> None:
        self.sent.append(payload)

    def __aiter__(self) -> "_FakeWS":
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self) -> None:
        pass


class _FakeWSCtx:
    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    async def __aenter__(self) -> _FakeWS:
        return self._ws

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeHTTP:
    """Stand-in for aiohttp.ClientSession(): its ws_connect hands back _FakeWS."""

    def __init__(self, ws: _FakeWS) -> None:
        self._ws = ws

    def ws_connect(self, *args: object, **kwargs: object) -> _FakeWSCtx:
        return _FakeWSCtx(self._ws)

    async def __aenter__(self) -> "_FakeHTTP":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


# Paid path: a signer is supplied and the audio is tiny so the run is instant.
_ARGV = ["client.py", "--signer", "http://signer.test", "--seconds", "0.1"]


class PaidSessionLifecycle(unittest.IsolatedAsyncioTestCase):
    @contextlib.contextmanager
    def _patched(self, session: mock.Mock, publish: mock.AsyncMock):
        """Patch the SDK + aiohttp boundary; yield (reserve, stop) mocks."""
        ws = _FakeWS()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(sys, "argv", _ARGV))
            reserve = stack.enter_context(
                mock.patch.object(client, "reserve_session", mock.AsyncMock(return_value=session))
            )
            stack.enter_context(
                mock.patch.object(client, "post_json", mock.AsyncMock(return_value={"in": "trickle://in"}))
            )
            stop = stack.enter_context(
                mock.patch.object(client, "stop_runner_session", mock.AsyncMock())
            )
            stack.enter_context(mock.patch.object(client, "_publish_pcm", publish))
            stack.enter_context(
                mock.patch.object(client.aiohttp, "ClientSession", lambda *a, **k: _FakeHTTP(ws))
            )
            yield reserve, stop

    async def test_paid_session_reserved_with_signer_and_released_once(self) -> None:
        session = _fake_session()
        publish = mock.AsyncMock(return_value=mock.Mock(name="TricklePublisherStats"))
        with self._patched(session, publish) as (reserve, stop):
            await client.main()

        # The paid path must hand the signer to reserve_session...
        self.assertEqual(reserve.call_args.kwargs.get("signer_url"), "http://signer.test")
        # ...and release the *same* session object exactly once (stops funding).
        stop.assert_awaited_once_with(session)

    async def test_paid_session_released_even_when_stream_fails(self) -> None:
        session = _fake_session()
        publish = mock.AsyncMock(side_effect=RuntimeError("backend died mid-stream"))
        with self._patched(session, publish) as (_reserve, stop):
            with self.assertRaises(RuntimeError):
                await client.main()

        # A crash mid-stream must not leak a funded session: the finally still
        # releases it (funding stops, reservation is freed).
        stop.assert_awaited_once_with(session)


if __name__ == "__main__":
    unittest.main()
