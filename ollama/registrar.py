#!/usr/bin/env python3
"""Registrar sidecar: one Ollama container, one Live Runner app per model.

Ollama has no Livepeer code in it -- it is the stock upstream image. This process
sits beside it and does the registering, which is the shape most real deployments
take: you wrap software you did not write.

It asks Ollama what it has (`GET /api/tags`) and registers each model as its own
app, so every model is separately discoverable and separately priced. Nothing here
is hardcoded except operator policy (prices), because which models exist is a fact
about the container, not a choice.

Livepeer integration (grep `# Livepeer:`):
  1. register_runner() x N  -- one capability per model, all pointing at Ollama

Capacity is the subtle part. Each registration carries its own counter and the
orchestrator does not know they share a GPU (go-livepeer#4015), so the numbers here
are sized to add up: the SUM across registrations is what the hardware must support,
which is why it is derived from OLLAMA_NUM_PARALLEL rather than set per model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os

import aiohttp

from livepeer_gateway.live_runner import register_runner

APP_NAMESPACE = "ollama"
log = logging.getLogger("ollama-registrar")


def _app_id(model: str) -> str:
    # `llama3.2:1b` -> `ollama/llama3.2-1b`. Lossy on purpose: app ids are stable
    # slugs, so the exact name a caller must send travels in metadata instead.
    return f"{APP_NAMESPACE}/{model.strip().lower().replace(':', '-')}"


def _parse_prices(raw: str) -> dict[str, float]:
    # "qwen2.5:0.5b=0.01,llama3.2:1b=0.02" -- operator policy, so it is config.
    prices: dict[str, float] = {}
    for item in raw.split(","):
        if "=" in item:
            name, _, value = item.partition("=")
            prices[name.strip()] = float(value)
    return prices


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register Ollama models as Live Runners."
    )
    parser.add_argument("--orchestrator", default="https://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--ollama-url", default="http://ollama:11434")
    parser.add_argument(
        "--parallel",
        type=int,
        default=int(os.environ.get("OLLAMA_NUM_PARALLEL", "2")),
        help="Concurrent generations the container can really run; split across models.",
    )
    parser.add_argument(
        "--prices",
        default=os.environ.get("PRICES", ""),
        help="model=usd_per_hour pairs, comma separated. Unlisted models use --price.",
    )
    parser.add_argument("--price", type=float, default=0.0, help="Fallback USD/hour.")
    return parser.parse_args()


async def _installed_models(session: aiohttp.ClientSession, base_url: str) -> list[str]:
    # Wait for Ollama and the puller: an empty list means nothing is pulled yet.
    for _ in range(60):
        try:
            async with session.get(f"{base_url.rstrip('/')}/api/tags") as resp:
                data = await resp.json()
            models = [m["name"] for m in data.get("models", []) if m.get("name")]
            if models:
                return sorted(models)
        except aiohttp.ClientError:
            pass
        await asyncio.sleep(5)
    raise SystemExit(f"ERROR: no models pulled at {base_url} after 5 minutes")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    prices = _parse_prices(args.prices)

    async with aiohttp.ClientSession() as session:
        models = await _installed_models(session, args.ollama_url)

    # Split the container's real concurrency across the models it serves, so the
    # advertised total matches the hardware instead of multiplying by model count.
    # capacity 0 is not expressible (the orchestrator coerces it to 1), so with more
    # models than parallel slots the total unavoidably overshoots -- say so loudly
    # rather than quietly advertising capacity the GPU does not have.
    per_model = max(1, args.parallel // len(models))
    advertised = per_model * len(models)
    if advertised > args.parallel:
        log.warning(
            "advertising %d slots across %d models but the container runs %d at once: "
            "raise OLLAMA_NUM_PARALLEL to at least the model count, or pull fewer "
            "models. The orchestrator cannot know these share a GPU "
            "(https://github.com/livepeer/go-livepeer/issues/4015).",
            advertised,
            len(models),
            args.parallel,
        )
    registrations = []
    for model in models:
        registrations.append(
            await register_runner(  # Livepeer: 1
                args.orchestrator,
                secret=args.orchSecret,
                runner_url=args.ollama_url,
                app=_app_id(model),
                mode="single-shot",  # one request in, one response out
                price=prices.get(model, args.price),  # USD/hour, metered while it runs
                capacity=per_model,
                # The slug cannot be reversed, so hand callers the exact name to send.
                metadata=json.dumps({"model": model}),
            )
        )
        log.info("registered %s as %s (capacity=%d)", model, _app_id(model), per_model)

    log.info("%d model(s) registered; %d slots advertised", len(models), advertised)
    try:
        await asyncio.Event().wait()  # heartbeats run in the background
    finally:
        for registration in registrations:
            await registration.close()


if __name__ == "__main__":
    asyncio.run(main())
