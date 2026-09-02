#!/usr/bin/env python3
"""Consume a Console session-handoff envelope and stream to /transcribe.

Spike / proof-of-concept. Console would reserve with startFunding=false, return
session URLs plus a LivePaymentSession snapshot plus a signer JWT. This client
drives the WebSocket and the 3-second funding loop. Do not treat a signer JWT
as production-safe until it is scoped to one manifest — see
gateway-web/docs/stream-session-handoff.md.

Envelope (JSON file or stdin):

  {
    "session_id": "…",
    "app_url": "https://orch/…/app",
    "control_url": "https://orch/…/control/…",
    "endpoint": "/transcribe",
    "payment": {
      "type": "live",
      "challenge": {
        "paymentParams": "…",
        "manifestId": "…",
        "paymentUrl": "https://orch/…/pay"
      },
      "app": "livepeer-example/realtime-transcription",
      "maxPrice": {"price": 0.1, "currency": "usd", "unit": "hour"},
      "state": {"n": 1}
    },
    "signer_url": "https://signer…",
    "signer_token": "eyJ…"
  }

Usage:
  uv run handoff_client.py envelope.json sample.wav
  uv run handoff_client.py envelope.json -
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import ssl
import sys
from contextlib import suppress
from pathlib import Path
from typing import Any

import aiohttp

from client import _read_pcm, _recv, _send

PAYMENT_INTERVAL_S = 3.0

log = logging.getLogger("handoff-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream audio using a reserved session handoff envelope."
    )
    parser.add_argument(
        "envelope",
        help="Path to the handoff JSON, or - to read it from stdin.",
    )
    parser.add_argument(
        "input",
        help=(
            "16 kHz mono WAV to stream, or - to read raw PCM from stdin "
            "(cannot combine with envelope on stdin)."
        ),
    )
    return parser.parse_args()


def _load_envelope(path: str) -> dict[str, Any]:
    if path.strip() == "-":
        raw = sys.stdin.read()
    else:
        raw = Path(path).expanduser().read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise SystemExit("ERROR: envelope must be a JSON object")
    return data


def _challenge_field(challenge: dict[str, Any], camel: str, snake: str) -> str:
    value = challenge.get(camel, challenge.get(snake, ""))
    return value.strip() if isinstance(value, str) else ""


class PaymentLoop:
    """Rehydrate a gateway-web LivePaymentSession snapshot over HTTP."""

    def __init__(
        self, envelope: dict[str, Any], session: aiohttp.ClientSession
    ) -> None:
        payment = envelope.get("payment")
        if not isinstance(payment, dict):
            raise SystemExit("ERROR: envelope missing payment snapshot")
        challenge = payment.get("challenge")
        if not isinstance(challenge, dict):
            raise SystemExit("ERROR: envelope.payment.challenge must be an object")
        signer_url = str(envelope.get("signer_url") or "").rstrip("/")
        token = str(envelope.get("signer_token") or "").strip()
        if not signer_url or not token:
            raise SystemExit("ERROR: envelope needs signer_url and signer_token")
        self._http = session
        self._signer_url = signer_url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._type = str(payment.get("type") or "live")
        self._app = payment.get("app")
        self._max_price = payment.get("maxPrice", payment.get("max_price"))
        self._state = payment.get("state")
        self._payment_params = _challenge_field(
            challenge, "paymentParams", "payment_params"
        )
        self._manifest_id = _challenge_field(challenge, "manifestId", "manifest_id")
        self._payment_url = _challenge_field(challenge, "paymentUrl", "payment_url")
        if not self._payment_params or not self._manifest_id or not self._payment_url:
            raise SystemExit(
                "ERROR: challenge missing paymentParams/manifestId/paymentUrl"
            )

    async def run(self, stop: asyncio.Event) -> None:
        # First interval payment waits — reserve already paid the 402.
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=PAYMENT_INTERVAL_S)
                return
            except TimeoutError:
                pass
            try:
                await self._send_payment()
            except Exception:
                log.exception("funding cycle failed")

    async def _send_payment(self) -> None:
        payload: dict[str, Any] = {
            "orchestrator": self._payment_params,
            "type": self._type,
            "ManifestID": self._manifest_id,
        }
        if self._app:
            payload["app"] = self._app
        if self._max_price is not None:
            payload["maxPrice"] = self._max_price
        if self._state is not None:
            payload["state"] = self._state
        async with self._http.post(
            f"{self._signer_url}/generate-live-payment",
            json=payload,
            headers=self._headers,
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200:
                raise RuntimeError(f"generate-live-payment HTTP {resp.status}: {body}")
        payment = body.get("payment")
        seg = body.get("segCreds")
        state = body.get("state")
        if not isinstance(payment, str) or not payment:
            raise RuntimeError("generate-live-payment missing payment")
        if not isinstance(seg, str) or not seg:
            raise RuntimeError("generate-live-payment missing segCreds")
        if isinstance(state, dict):
            self._state = state
        headers = {"Livepeer-Payment": payment, "Livepeer-Segment": seg}
        async with self._http.post(
            self._payment_url, headers=headers, ssl=_orch_ssl()
        ) as pay:
            if pay.status >= 400:
                text = await pay.text()
                raise RuntimeError(f"payment POST HTTP {pay.status}: {text[:200]}")


def _orch_ssl() -> ssl.SSLContext | bool:
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _ws_url(app_url: str, endpoint: str) -> str:
    base = app_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    path = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    return base + path


async def _stop_session(http: aiohttp.ClientSession, control_url: str) -> None:
    url = control_url.rstrip("/") + "/stop"
    with suppress(Exception):
        async with http.post(url, ssl=_orch_ssl()) as resp:
            log.info("stop %s -> %s", url, resp.status)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    if args.envelope.strip() == "-" and args.input.strip() == "-":
        raise SystemExit("ERROR: envelope and audio cannot both be stdin")
    envelope = _load_envelope(args.envelope)
    app_url = str(envelope.get("app_url") or "").strip()
    control_url = str(envelope.get("control_url") or "").strip()
    endpoint = str(envelope.get("endpoint") or "/transcribe").strip()
    if not app_url or not control_url:
        raise SystemExit("ERROR: envelope needs app_url and control_url")

    live = args.input.strip() == "-"
    pcm = b"" if live else _read_pcm(str(Path(args.input).expanduser()))
    ws_url = _ws_url(app_url, endpoint)
    log.info("session_id=%s ws=%s", envelope.get("session_id"), ws_url)

    stop = asyncio.Event()
    ssl_ctx: ssl.SSLContext | bool = (
        _orch_ssl() if ws_url.startswith("wss://") else True
    )
    async with aiohttp.ClientSession() as http:
        funding = PaymentLoop(envelope, http)
        fund_task = asyncio.create_task(funding.run(stop))
        try:
            async with http.ws_connect(ws_url, ssl=ssl_ctx, heartbeat=20) as ws:
                await asyncio.gather(_send(ws, pcm, live=live), _recv(ws))
        finally:
            stop.set()
            fund_task.cancel()
            with suppress(asyncio.CancelledError):
                await fund_task
            await _stop_session(http, control_url)


if __name__ == "__main__":
    asyncio.run(main())
