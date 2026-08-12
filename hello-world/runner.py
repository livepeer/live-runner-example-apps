#!/usr/bin/env python3
"""hello-world app: a normal FastAPI service, made callable on the Livepeer network.

Livepeer integration (grep `# Livepeer:`):
  1. register_runner()     — announce the app to the orchestrator (startup)
  2. registration.close()  — deregister (cleanup)

/hello is an ordinary HTTP handler; being on the network doesn't change how you write
it. FastAPI derives the request/response schema from the models below and serves it at
/openapi.json, so callers can discover the interface without reading this file.
"""

from __future__ import annotations

import argparse
import logging
from contextlib import asynccontextmanager, suppress

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, Field

from livepeer_gateway.live_runner import register_runner

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8989
APP_ID = "livepeer-example/hello-world"

log = logging.getLogger("hello-world")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Runner hello-world demo.")
    parser.add_argument("--orchestrator", default="https://localhost:8935")
    parser.add_argument("--orchSecret", default="abcdef")
    parser.add_argument("--runner-url", default=f"http://{DEFAULT_HOST}:{DEFAULT_PORT}")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="Bind address (use 0.0.0.0 in containers)."
    )
    parser.add_argument(
        "--price",
        type=float,
        default=0,
        help="Runner price in USD per call (0 = free, the offchain default).",
    )
    return parser.parse_args()


class HelloRequest(BaseModel):
    name: str = Field("world", description="Who to greet.")


class HelloResponse(BaseModel):
    message: str


def build_app(args: argparse.Namespace) -> FastAPI:
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        app.state.registration = await register_runner(  # Livepeer: 1
            args.orchestrator,
            secret=args.orchSecret,
            runner_url=args.runner_url,
            app=APP_ID,
            mode="single-shot",
            price=args.price,  # USD per call
            # one flat payment per call instead of per-second metering
            unit="fixed",
        )
        log.info(
            "registered runner_id=%s orchestrator=%s",
            app.state.registration.runner_id,
            app.state.registration.orchestrator_url,
        )
        yield
        with suppress(Exception):
            await app.state.registration.close()  # Livepeer: 2

    app = FastAPI(title=APP_ID, version="0.1.0", lifespan=_lifespan)

    @app.post("/hello", response_model=HelloResponse)
    async def hello(body: HelloRequest) -> HelloResponse:
        return HelloResponse(message=f"Hello, {body.name}!")

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = _parse_args()
    uvicorn.run(build_app(args), host=args.host, port=DEFAULT_PORT, access_log=False)


if __name__ == "__main__":
    main()
