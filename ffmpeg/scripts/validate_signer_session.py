#!/usr/bin/env python3
"""Validate signer URL resolution, reachability, and session exchange for MCP testing."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass


@dataclass
class SignerCheckResult:
    name: str
    passed: bool
    detail: str


def _http_reachable(url: str) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        # Signer may return 404 on root — still reachable.
        return True, f"HTTP {exc.code} (reachable)"
    except Exception as exc:
        return False, str(exc)


def check_signer_url_precedence() -> SignerCheckResult:
    """Document precedence: exchange signer_url > LIVEPEER_SIGNER env."""
    exchange_url = os.environ.get("EXCHANGE_SIGNER_URL", "").strip()
    env_url = os.environ.get("LIVEPEER_SIGNER", "").strip()
    resolved = exchange_url or env_url or None
    if not resolved:
        return SignerCheckResult(
            "signer_url_precedence",
            True,
            "offchain mode (no signer URL configured)",
        )
    detail = f"resolved={resolved!r} (exchange={exchange_url!r}, env={env_url!r})"
    return SignerCheckResult("signer_url_precedence", True, detail)


def check_signer_reachability() -> SignerCheckResult:
    signer_url = os.environ.get("LIVEPEER_SIGNER", "").strip()
    if not signer_url:
        return SignerCheckResult("signer_reachability", True, "skipped (no LIVEPEER_SIGNER)")
    ok, detail = _http_reachable(signer_url.rstrip("/"))
    return SignerCheckResult("signer_reachability", ok, f"{signer_url} -> {detail}")


def check_oidc_exchange() -> SignerCheckResult:
    billing = os.environ.get("LIVEPEER_BILLING_URL", "").strip()
    client_id = os.environ.get("LIVEPEER_OIDC_CLIENT_ID", "").strip()
    oidc_url = os.environ.get("LIVEPEER_OIDC_URL", "").strip()
    if not (billing and client_id and oidc_url):
        return SignerCheckResult("oidc_exchange", True, "skipped (OIDC+billing env not set)")
    try:
        from livepeer_gateway_client.oidc_auth import ensure_valid_token
        from livepeer_gateway_client.auth_exchange import exchange_oidc_token_for_signer

        oauth = ensure_valid_token(
            oidc_url,
            client_id=client_id,
            scopes=os.environ.get("LIVEPEER_OIDC_SCOPES", "openid sign:job offline_access"),
            audience=os.environ.get("LIVEPEER_OIDC_AUDIENCE", "livepeer-clearinghouse"),
            headless=True,
        )
        result = exchange_oidc_token_for_signer(
            billing,
            client_id,
            oauth["access_token"],
            audience=os.environ.get("LIVEPEER_OIDC_AUDIENCE", "livepeer-clearinghouse"),
        )
        os.environ["EXCHANGE_SIGNER_URL"] = result.signer_url or ""
        detail = json.dumps(
            {
                "signer_url": result.signer_url,
                "discovery_url": result.discovery_url,
                "auth_header_len": len(result.headers.get("Authorization", "")),
            }
        )
        return SignerCheckResult("oidc_exchange", True, detail)
    except Exception as exc:
        return SignerCheckResult("oidc_exchange", False, str(exc))


def check_auth0_m2m_mint() -> SignerCheckResult:
    issuer = os.environ.get("LIVEPEER_OIDC_URL", "").strip()
    m2m_id = os.environ.get("LIVEPEER_AUTH0_M2M_CLIENT_ID", "").strip()
    secret = os.environ.get("LIVEPEER_AUTH0_M2M_CLIENT_SECRET", "").strip()
    if not (issuer and m2m_id and secret):
        return SignerCheckResult("auth0_m2m_mint", True, "skipped (M2M env not set)")
    try:
        from livepeer_gateway_client.oidc_auth import client_credentials_token

        token = client_credentials_token(
            issuer,
            client_id=m2m_id,
            client_secret=secret,
            audience=os.environ.get("LIVEPEER_OIDC_AUDIENCE", "livepeer-clearinghouse"),
            external_user_id=os.environ.get("LIVEPEER_EXTERNAL_USER_ID", "demo-user"),
        )
        return SignerCheckResult(
            "auth0_m2m_mint",
            True,
            f"token_len={len(token['access_token'])}",
        )
    except Exception as exc:
        return SignerCheckResult("auth0_m2m_mint", False, str(exc))


def check_cached_oidc_reuse() -> SignerCheckResult:
    oidc_url = os.environ.get("LIVEPEER_OIDC_URL", "").strip()
    client_id = os.environ.get("LIVEPEER_OIDC_CLIENT_ID", "").strip()
    if not (oidc_url and client_id):
        return SignerCheckResult("cached_oidc_reuse", True, "skipped")
    try:
        from livepeer_gateway_client.oidc_auth import ensure_valid_token

        first = ensure_valid_token(
            oidc_url,
            client_id=client_id,
            scopes=os.environ.get("LIVEPEER_OIDC_SCOPES", "openid sign:job offline_access"),
            audience=os.environ.get("LIVEPEER_OIDC_AUDIENCE", "livepeer-clearinghouse"),
            headless=True,
        )
        second = ensure_valid_token(
            oidc_url,
            client_id=client_id,
            scopes=os.environ.get("LIVEPEER_OIDC_SCOPES", "openid sign:job offline_access"),
            audience=os.environ.get("LIVEPEER_OIDC_AUDIENCE", "livepeer-clearinghouse"),
            headless=True,
        )
        same = first.get("access_token") == second.get("access_token")
        return SignerCheckResult(
            "cached_oidc_reuse",
            same,
            "cache hit (same access_token)" if same else "cache miss (new token)",
        )
    except Exception as exc:
        return SignerCheckResult("cached_oidc_reuse", False, str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate signer session setup for MCP.")
    parser.add_argument("--json", action="store_true", help="Emit JSON results")
    args = parser.parse_args()

    checks = [
        check_signer_url_precedence(),
        check_signer_reachability(),
        check_cached_oidc_reuse(),
        check_auth0_m2m_mint(),
        check_oidc_exchange(),
        check_signer_url_precedence(),  # re-run after exchange may set EXCHANGE_SIGNER_URL
    ]

    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for check in checks:
            status = "PASS" if check.passed else "FAIL"
            print(f"[{status}] {check.name}: {check.detail}")

    failed = [c for c in checks if not c.passed]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
