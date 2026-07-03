#!/usr/bin/env python3
"""Step-4 isolation test: does a pmth_ key exchange for a signer session?

Proves the exact path the gateway depends on (SignerTokenProvider against the
live builder-api) WITHOUT the aiohttp gateway or any GPU/runner. If this prints
a signer_url + Bearer headers, the core exchange works and the gateway will too.
A 402 here = the $5 gate firing (also a success signal: the wiring works).

Usage (from the oauth-gateway/ folder):
  export LIVEPEER_BILLING_URL=http://localhost:8095
  export LIVEPEER_CLIENT_ID=<DEMO_APP_AUTH0_PUBLIC_CLIENT_ID>
  uv run exchange_test.py pmth_ALICE_KEY
"""
import os
import sys

from livepeer_gateway_client.signer_provider import SignerTokenProvider


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: uv run exchange_test.py <pmth_key>")
        return 2
    key = sys.argv[1].strip()
    billing = os.environ.get("LIVEPEER_BILLING_URL", "http://localhost:8095").strip()
    client_id = os.environ.get("LIVEPEER_CLIENT_ID", "").strip() or None
    if not client_id:
        print("set LIVEPEER_CLIENT_ID (= DEMO_APP_AUTH0_PUBLIC_CLIENT_ID)")
        return 2

    print(f"billing_url = {billing}")
    print(f"client_id   = {client_id}")
    print(f"key         = {key[:12]}...")
    provider = SignerTokenProvider(billing_url=billing, api_key=key, client_id=client_id)
    try:
        provider.refresh()
    except Exception as exc:  # noqa: BLE001 - surface the builder-api error verbatim
        msg = str(exc)
        print("\nEXCHANGE FAILED:")
        print(" ", msg)
        if "402" in msg or "insufficient" in msg.lower():
            print("  -> this is the $5 gate firing. Wiring works; grant more credit to proceed.")
        elif "invalid_grant" in msg.lower():
            print("  -> Auth0 credentials-exchange Action likely missing/unbound (claims absent).")
        return 1

    print("\nEXCHANGE OK:")
    print("  signer_url   =", provider.signer_url)
    print("  discovery_url=", getattr(provider, "discovery_url", None))
    print("  headers      =", {k: (v[:28] + "...") for k, v in provider.headers.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
