#!/usr/bin/env python3
"""Stock OpenAI client — nothing Livepeer-specific in here.

This is the whole point of the example: ordinary `openai` code with a fixed
base_url and api_key. It has no idea it's talking to Livepeer. The local gateway
(gateway.py) sits at base_url and does discovery + payment, so this works
on-chain unchanged.

    docker compose up -d --build          # the network side
    uv run gateway.py &                    # the local gateway on :8080
    uv run client.py --prompt "Hello!"

Any OpenAI tool (this script, another SDK, curl) can use the same base_url.
"""
from __future__ import annotations

import argparse

from openai import OpenAI


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Chat with a vLLM Live Runner using the stock OpenAI library.")
    parser.add_argument("--base-url", default="http://localhost:8080/v1", help="The gateway's OpenAI endpoint.")
    parser.add_argument("--api-key", default="unused", help="Ignored by the gateway; the OpenAI client requires a value.")
    parser.add_argument("--model", default="Qwen/Qwen2.5-0.5B-Instruct")
    parser.add_argument("--prompt", default="In one sentence, what is the Livepeer network?")
    parser.add_argument("--stream", action="store_true", help="Stream tokens as they arrive (SSE).")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    client = OpenAI(base_url=args.base_url, api_key=args.api_key)
    messages = [{"role": "user", "content": args.prompt}]
    if args.stream:
        stream = client.chat.completions.create(model=args.model, messages=messages, stream=True)
        for chunk in stream:
            print(chunk.choices[0].delta.content or "", end="", flush=True)
        print()
        return
    completion = client.chat.completions.create(model=args.model, messages=messages)
    print(completion.choices[0].message.content)


if __name__ == "__main__":
    main()
