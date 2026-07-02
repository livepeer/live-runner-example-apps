# /// script
# requires-python = ">=3.10"
# dependencies = ["openai>=1.0", "requests"]
# ///
"""One-command demo of the Livepeer clearinghouse loop.

Mints an account (Auth0 + $5 trial) if you don't pass a key, then makes a
drop-in OpenAI call through the gateway — paid on Livepeer and metered to that
account. Run it twice with different --user to show per-user attribution.

Examples:
  uv run demo.py                          # mint 'alice', ask the default prompt
  uv run demo.py --user bob               # mint 'bob' -> separate account/balance
  uv run demo.py --key sk_...             # reuse an existing key (no mint)
  uv run demo.py --stream --prompt "Tell me a joke"

Assumes the stack is up: gateway on :8080, builder-api on :8095.
"""
from __future__ import annotations

import argparse
import os
import sys

import requests
from openai import OpenAI

DEFAULT_ENV = "/home/ricks/development/livepeer/ch-worktrees/pr57-builder-api/.env"


def load_env(path: str) -> dict[str, str]:
    env: dict[str, str] = {}
    if os.path.isfile(path):
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k] = v.strip().strip("'").strip('"')
    return env


def mint_account(builder: str, client_id: str, m2m_id: str, m2m_secret: str, user: str, email: str) -> str:
    url = f"{builder}/api/v1/apps/{client_id}/users"
    r = requests.post(url, json={"externalUserId": user, "email": email or f"{user}@example.com"},
                      auth=(m2m_id, m2m_secret), timeout=30)
    r.raise_for_status()
    data = r.json()
    key = data.get("apiKey")
    if not key:
        sys.exit(f"mint failed: {data}")
    return key


def main() -> None:
    ap = argparse.ArgumentParser(description="Livepeer clearinghouse demo")
    ap.add_argument("--gateway", default=os.environ.get("GATEWAY_URL", "http://localhost:8080/v1"))
    ap.add_argument("--model", default=os.environ.get("MODEL", "Qwen/Qwen2.5-0.5B-Instruct"))
    ap.add_argument("--prompt", default="In one sentence, what is Livepeer?")
    ap.add_argument("--key", help="existing sk_ api key (skips minting)")
    ap.add_argument("--user", default="alice", help="account id to mint")
    ap.add_argument("--email", default="")
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("--env-file", default=DEFAULT_ENV, help="backend .env for mint creds")
    a = ap.parse_args()

    key = a.key
    if not key:
        env = load_env(a.env_file)
        port = env.get("BUILDER_API_PORT", "8095")
        builder = os.environ.get("BUILDER_API_URL", f"http://localhost:{port}")
        client_id = env.get("DEMO_APP_AUTH0_PUBLIC_CLIENT_ID")
        m2m_id = env.get("DEMO_APP_AUTH0_M2M_CLIENT_ID")
        m2m_secret = env.get("DEMO_APP_AUTH0_M2M_CLIENT_SECRET")
        if not all([client_id, m2m_id, m2m_secret]):
            sys.exit(f"pass --key, or point --env-file at a backend .env with DEMO_APP_AUTH0_* (looked at {a.env_file})")
        print(f"[mint] creating account '{a.user}' (+$5 trial) ...")
        key = mint_account(builder, client_id, m2m_id, m2m_secret, a.user, a.email)
        print(f"[mint] '{a.user}' -> key {key[:16]}...")

    print(f"[call] gateway={a.gateway} model={a.model} key={key[:16]}...")
    client = OpenAI(base_url=a.gateway, api_key=key)
    if a.stream:
        stream = client.chat.completions.create(
            model=a.model, messages=[{"role": "user", "content": a.prompt}], stream=True)
        print("[reply] ", end="", flush=True)
        for chunk in stream:
            print(chunk.choices[0].delta.content or "", end="", flush=True)
        print()
    else:
        r = client.chat.completions.create(
            model=a.model, messages=[{"role": "user", "content": a.prompt}])
        print(f"[reply] {r.choices[0].message.content}")
        if r.usage:
            print(f"[usage] prompt={r.usage.prompt_tokens} completion={r.usage.completion_tokens} total={r.usage.total_tokens}")
    print("[done] paid on Livepeer via the clearinghouse signer; usage metered to this account.")


if __name__ == "__main__":
    main()
