# /// script
# requires-python = ">=3.12"
# dependencies = ["requests", "livepeer-gateway", "livepeer-gateway-client"]
# ///
#
# [tool.uv.sources] can't live in PEP-723 metadata, so pin via --with when needed;
# easiest is to run from a checkout that has the pinned sources, e.g.:
#   uv run --project oauth-gateway python sdk_example.py --key sk_...
#
"""Raw Livepeer SDK example — the layer OpenAI and MCP wrap.

1. exchange your key for a signer session (grant/402 gate),
2. list the capabilities/app names the network advertises (discovery),
3. reserve a session for one app and call its runner directly.

  uv run --project oauth-gateway --with requests python sdk_example.py --key sk_YOUR_KEY
  uv run --project oauth-gateway --with requests python sdk_example.py --key sk_... --app vllm/qwen2.5-0.5b-instruct
"""
from __future__ import annotations

import argparse
import asyncio
import os

import requests
import urllib3

from livepeer_gateway.live_runner import call_runner, stop_runner_session
from livepeer_gateway.selection import reserve_session
from livepeer_gateway_client.signer_provider import SignerTokenProvider

urllib3.disable_warnings()  # self-signed orchestrator cert on :8935

BILLING = os.environ.get("LIVEPEER_BILLING_URL", "http://localhost:8095")
CLIENT_ID = os.environ.get("LIVEPEER_CLIENT_ID", "OZFJrZRxbv2prI5VotOivrlmuRhR1ySo")
DISCOVERY = os.environ.get("LIVEPEER_DISCOVERY", "https://localhost:8935/discovery")


def list_capabilities(discovery_url: str) -> list[str]:
    data = requests.get(discovery_url, verify=False, timeout=10).json()
    apps = []
    for orch in data:
        for r in orch.get("runners", []):
            apps.append(r["app"])
    return apps


async def main() -> None:
    ap = argparse.ArgumentParser(description="Raw Livepeer SDK: list capabilities, reserve session, call runner")
    ap.add_argument("--key", required=True, help="your sk_ key")
    ap.add_argument("--app", help="app id to call (default: first LLM app)")
    ap.add_argument("--prompt", default="In one sentence, what is Livepeer?")
    args = ap.parse_args()

    # 1. exchange key -> signer session (this is where $5 grant / 402 gate happens)
    provider = SignerTokenProvider(billing_url=BILLING, api_key=args.key, client_id=CLIENT_ID)
    provider.refresh()
    signer_url = provider.signer_url
    signer_headers = dict(provider.headers)
    discovery = getattr(provider, "discovery_url", None) or DISCOVERY
    print(f"[exchange] signer={signer_url}  discovery={discovery}")

    # 2. list capabilities / app names directly from discovery
    apps = list_capabilities(discovery)
    print(f"[capabilities] {apps}")

    # 3. reserve a session and call the runner directly
    app = args.app or next((a for a in apps if "ffmpeg" not in a), apps[0])
    print(f"[reserve] app={app}")
    session = await reserve_session(discovery_url=discovery, app=app, signer_url=signer_url, signer_headers=signer_headers)
    try:
        result = await call_runner(
            runner_url=session.app_url.rstrip("/") + "/v1/chat/completions",
            payload={"model": "Qwen/Qwen2.5-0.5B-Instruct", "messages": [{"role": "user", "content": args.prompt}]},
            signer_url=signer_url, signer_headers=signer_headers, timeout=120.0,
        )
        print("[reply]", result.data["choices"][0]["message"]["content"])
    finally:
        try:
            await stop_runner_session(session)
        except Exception:
            pass
    print("[done] paid on Livepeer via your signer session; usage metered to your account.")


if __name__ == "__main__":
    asyncio.run(main())
