#!/usr/bin/env python3
"""ping-pong client (single-shot WebSocket): discover a runner and open a WS.

Offchain (no --signer):
  1. runner_selector()  — discover the app's proxied URL
  2. ws_connect()       — open the WebSocket and exchange ping/pong

On-chain (--signer): a WebSocket upgrade cannot answer the 402 challenge inline,
so the client (a) preflights the challenge over plain HTTP, (b) puts the initial
payment on the upgrade request headers, and (c) keeps the held-open session
funded by POSTing top-ups to the session-scoped /payment endpoint on an interval.

  *** The on-chain path here is a hand-rolled prototype of what the SDK's session
  *** payment streamer (livepeer-python-gateway#31 / ENG-179) will do once it is
  *** repointed at the single-shot session-scoped payment URL. It also assumes the
  *** orchestrator supports a paid single-shot WS upgrade (go-livepeer#3955). Treat
  *** it as experimental until both land.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time
from urllib.parse import urlsplit

import aiohttp

from livepeer_gateway.errors import LivepeerGatewayError
from livepeer_gateway.selection import runner_selector

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/ping-pong"

log = logging.getLogger("ping-pong-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the WebSocket ping/pong Live Runner demo (single-shot)."
    )
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument(
        "--count", type=int, default=10, help="Pings to send (0 = until closed)."
    )
    parser.add_argument(
        "--signer", default="", help="Remote signer base URL (on-chain/paid path)."
    )
    return parser.parse_args()


async def _select_app_url(discovery_url: str) -> str:
    cursor = await runner_selector(
        discovery_url=discovery_url, app=APP_ID
    )  # Livepeer: 1
    for candidate in cursor.candidates:
        return candidate.url
    raise LivepeerGatewayError(f"no runner discovered for app {APP_ID!r}")


# --- on-chain payment plumbing (experimental; see module docstring) ------------


def _session_payment_url(app_url: str, manifest_id: str) -> str:
    """Derive .../apps/{runner}/session/{manifest}/payment from the app URL."""
    parts = urlsplit(app_url)
    segs = [s for s in parts.path.split("/") if s]
    # app path is /apps/{runner}/app[/...]; take the runner id after "apps"
    runner_id = segs[segs.index("apps") + 1]
    return f"{parts.scheme}://{parts.netloc}/apps/{runner_id}/session/{manifest_id}/payment"


async def _onchain_headers(app_url: str, ws_url: str, signer_url: str):
    """Preflight the 402 challenge and build the initial payment headers."""
    from livepeer_gateway.remote_signer import LivePaymentSession, get_signer_info

    signer = await get_signer_info(signer_url, None)
    payer = str(signer.address)

    async with aiohttp.ClientSession() as http:
        # Plain GET (no upgrade, no payment) -> 402 challenge JSON.
        async with http.get(
            ws_url, headers={"Livepeer-Payer-Address": payer}, ssl=False
        ) as resp:
            if resp.status != 402:
                raise LivepeerGatewayError(
                    f"expected 402 challenge, got {resp.status}: {await resp.text()}"
                )
            challenge = await resp.json()

    session = LivePaymentSession(
        signer_url=signer_url,
        signer_headers=None,
        type="lv2v",
        payment_params=challenge["payment_params"],
        manifest_id=challenge["manifest_id"],
        orchestrator_url=challenge["orchestrator"],
    )
    session._payer = payer  # stash for header building
    interval_s = max(1.0, float(challenge.get("payment_interval_ms", 5000)) / 1000.0)
    pay_url = _session_payment_url(app_url, challenge["manifest_id"])
    return session, payer, interval_s, pay_url


async def _payment_streamer(session, payer, interval_s, pay_url):
    """Top up the held-open session below the server tick (hardcoded #31 stand-in)."""
    # A small margin under the server interval so a top-up always lands first.
    period = max(0.5, interval_s * 0.5)
    async with aiohttp.ClientSession() as http:
        while True:
            payment = await session.get_payment()
            headers = {
                "Livepeer-Payer-Address": payer,
                "Livepeer-Payment": payment.payment,
                "Livepeer-Segment": payment.seg_creds or "",
            }
            with contextlib.suppress(Exception):
                async with http.post(pay_url, headers=headers, ssl=False) as r:
                    log.info("payment top-up -> %s (%s)", r.status, pay_url)
            await asyncio.sleep(period)


# --- ping/pong over the WebSocket ---------------------------------------------


async def _run(ws_url: str, *, count: int, headers: dict | None) -> None:
    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(
            ws_url, headers=headers, ssl=False
        ) as ws:  # Livepeer: 2
            print(f"connected: {ws_url}", file=sys.stderr)
            sent = 0
            while count <= 0 or sent < count:
                ping = time.time()
                await ws.send_json({"ping": ping})
                sent += 1
                msg = json.loads((await ws.receive()).data)
                rtt_ms = (time.time() - ping) * 1000.0
                print(
                    "ping-pong receiver_delta_ms={:.2f} round_trip_ms={:.2f}".format(
                        float(msg.get("delta_ms", -1)), rtt_ms
                    )
                )
                await asyncio.sleep(max(0.0, 1.0 - (time.time() - ping)))


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    signer_url = args.signer.strip() or None

    app_url = await _select_app_url(args.discovery)
    ws_url = app_url.rstrip("/") + "/ws"

    if signer_url is None:
        # Offchain: just open the WebSocket.
        await _run(ws_url, count=max(0, args.count), headers=None)
        return

    # On-chain: preflight + initial payment on the upgrade + interval top-ups.
    session, payer, interval_s, pay_url = await _onchain_headers(
        app_url, ws_url, signer_url
    )
    first = await session.get_payment()
    upgrade_headers = {
        "Livepeer-Payer-Address": payer,
        "Livepeer-Payment": first.payment,
        "Livepeer-Segment": first.seg_creds or "",
    }
    streamer = asyncio.create_task(
        _payment_streamer(session, payer, interval_s, pay_url)
    )
    try:
        await _run(ws_url, count=max(0, args.count), headers=upgrade_headers)
    finally:
        streamer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await streamer


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
