#!/usr/bin/env python3
"""tiles client: split an image, fan out one single-shot call per tile, stitch the results.

Fires all tile calls concurrently; the runner's `capacity` gates how many run at once.
At capacity=1 tiles serialize; at capacity=N they process in parallel and the whole
image finishes far faster. Same output either way — capacity changes speed, not result.

Livepeer integration (grep `# Livepeer:`):
  1. runner_selector()  — discover orchestrators advertising the app
  2. call_runner()      — run one tile through the orchestrator; on the paid path it
                          answers the 402 payment challenge inline (one fixed payment
                          per tile). Single-shot needs no reserve/stop: the orchestrator
                          reserves a session for the call and releases it on return.

A call is refused with HTTP 503 while the runner is at capacity, so it retries with
backoff until a slot frees; that wait is exactly the capacity limit doing its job.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import random
import time
from pathlib import Path
from typing import Iterator

import cv2
import numpy as np

from livepeer_gateway.errors import (
    LivepeerGatewayError,
    LivepeerHTTPError,
    NoOrchestratorAvailableError,
    NoRunnerAvailableError,
)
from livepeer_gateway.live_runner import LiveRunnerCallResult, call_runner
from livepeer_gateway.selection import runner_selector

DEFAULT_DISCOVERY = "https://localhost:8935/discovery"
APP_ID = "livepeer-example/tiles"
DEFAULT_OUTPUT = "tiles-out.png"

log = logging.getLogger("tiles-client")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Split an image, stylize each tile on a Live Runner, stitch it back."
    )
    parser.add_argument("input", help="input image file (png/jpg)")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="output image path")
    parser.add_argument(
        "--grid",
        type=int,
        default=3,
        help="split into GRID x GRID tiles (default 3 = 9 tiles)",
    )
    parser.add_argument("--discovery", default=DEFAULT_DISCOVERY)
    parser.add_argument(
        "--signer", default="", help="Remote signer base URL (on-chain/paid path)."
    )
    parser.add_argument(
        "--slot-timeout",
        type=float,
        default=120.0,
        help="Max seconds to wait for a capacity slot per tile.",
    )
    parser.add_argument(
        "--dump-tiles",
        action="store_true",
        help=(
            "Write each input + processed part to ./tiles-parts/ (gitignored) "
            "to inspect the split."
        ),
    )
    return parser.parse_args()


def _split(
    img: np.ndarray, grid: int
) -> Iterator[tuple[int, int, int, int, np.ndarray]]:
    # np.array_split handles uneven sizes; yields (row, col, y0, x0, tile).
    y0 = 0
    for r, row in enumerate(np.array_split(img, grid, axis=0)):
        x0 = 0
        for c, tile in enumerate(np.array_split(row, grid, axis=1)):
            yield r, c, y0, x0, tile
            x0 += tile.shape[1]
        y0 += row.shape[0]


async def _call_tile_with_retry(
    *,
    r: int,
    c: int,
    payload: dict[str, str],
    discovery_url: str,
    signer_url: str | None,
    deadline: float,
) -> LiveRunnerCallResult:
    # A full runner refuses the call with 503; wait for a slot instead of failing the
    # tile. Re-discover each attempt so a restarted runner is picked up.
    cap = 0.25
    waiting = False
    while True:
        try:
            cursor = await runner_selector(  # Livepeer: 1
                discovery_url=discovery_url,  # omit if the signer does discovery itself
                app=APP_ID,
            )
            runner = cursor.candidates[0]
            return await call_runner(  # Livepeer: 2
                runner=runner,  # discovery metadata tells call_runner the price unit
                runner_url=runner.url.rstrip("/") + "/tile",
                payload=payload,
                signer_url=signer_url,
                timeout=60.0,
            )
        except (
            NoRunnerAvailableError,
            NoOrchestratorAvailableError,
            LivepeerHTTPError,
        ) as exc:
            # Anything but "insufficient capacity" (503) is a real error, not a
            # full runner.
            if isinstance(exc, LivepeerHTTPError) and exc.status_code != 503:
                raise
            if time.monotonic() >= deadline:
                raise
            if not waiting:
                # Warn once per tile; the reason tells a full runner from a dead one.
                log.warning(
                    "tile (%d,%d) no slot yet, retrying until --slot-timeout: %s",
                    r,
                    c,
                    exc,
                )
                waiting = True
            # Full jitter: sleep a random slice of the (growing) window so many
            # tiles retrying at once spread out instead of stampeding in lockstep.
            await asyncio.sleep(random.uniform(0, cap))
            cap = min(cap * 2, 2.0)


async def _process_tile(
    *,
    r: int,
    c: int,
    tile: np.ndarray,
    discovery_url: str,
    signer_url: str | None,
    t0: float,
    slot_timeout: float,
    dump_dir: Path | None,
) -> np.ndarray:
    # Encode the tile as PNG for the JSON payload.
    ok, png = cv2.imencode(".png", tile)
    if not ok:
        raise LivepeerGatewayError(f"tile ({r},{c}): could not encode PNG")

    # One single-shot call per tile (waits out capacity); the orchestrator reserves
    # and releases the session around the call.
    result = await _call_tile_with_retry(
        r=r,
        c=c,
        payload={"tile": base64.b64encode(png.tobytes()).decode()},
        discovery_url=discovery_url,
        signer_url=signer_url,
        deadline=time.monotonic() + slot_timeout,
    )

    # Pull the processed tile out of the response.
    out_b64 = result.data.get("tile")
    if not isinstance(out_b64, str) or not out_b64:
        raise LivepeerGatewayError(f"tile ({r},{c}): response missing 'tile'")
    log.info("tile (%d,%d) done (+%.1fs)", r, c, time.monotonic() - t0)

    # Decode base64 PNG back to pixels, refitting to the tile's exact size.
    out = cv2.imdecode(
        np.frombuffer(base64.b64decode(out_b64), dtype=np.uint8), cv2.IMREAD_COLOR
    )
    if out is None:
        raise LivepeerGatewayError(f"tile ({r},{c}): undecodable image from runner")
    h, w = tile.shape[:2]
    if out.shape[:2] != (h, w):
        out = cv2.resize(out, (w, h))

    if dump_dir is not None:
        cv2.imwrite(str(dump_dir / f"tile_r{r}_c{c}_in.png"), tile)
        cv2.imwrite(str(dump_dir / f"tile_r{r}_c{c}_out.png"), out)
    return out


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    grid = max(1, args.grid)

    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        raise SystemExit(f"input file does not exist: {input_path}")
    img = cv2.imread(str(input_path), cv2.IMREAD_COLOR)
    if img is None:
        raise SystemExit(f"could not read image: {input_path}")

    pieces = list(_split(img, grid))
    log.info(
        "split %s into %d tiles (%dx%d grid); fanning out one call per tile",
        input_path.name,
        len(pieces),
        grid,
        grid,
    )

    dump_dir = Path("tiles-parts") if args.dump_tiles else None
    if dump_dir is not None:
        dump_dir.mkdir(exist_ok=True)

    t0 = time.monotonic()
    try:
        results = await asyncio.gather(
            *[
                _process_tile(
                    r=r,
                    c=c,
                    tile=tile,
                    discovery_url=args.discovery,
                    signer_url=args.signer.strip() or None,
                    t0=t0,
                    slot_timeout=args.slot_timeout,
                    dump_dir=dump_dir,
                )
                for (r, c, _y0, _x0, tile) in pieces
            ]
        )
    except LivepeerGatewayError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc

    canvas = img.copy()
    for (_r, _c, y0, x0, tile), out in zip(pieces, results):
        h, w = tile.shape[:2]
        canvas[y0 : y0 + h, x0 : x0 + w] = out

    out_path = Path(args.output).expanduser()
    cv2.imwrite(str(out_path), canvas)
    log.info(
        "wrote %s — %d tiles in %.1fs (raise the runner's --capacity to parallelize)",
        out_path,
        len(pieces),
        time.monotonic() - t0,
    )


if __name__ == "__main__":
    asyncio.run(main())
