#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

import httpx


TOKEN_URL = (
    "https://open.feishu.cn/open-apis/auth/v3/"
    "tenant_access_token/internal"
)


def verify_credential(
    app_id: str,
    app_secret: str,
) -> tuple[bool, str]:
    try:
        response = httpx.post(
            TOKEN_URL,
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=15,
        )
        payload = response.json()
    except (httpx.HTTPError, json.JSONDecodeError) as exc:
        return (
            False,
            f"credential verification request failed: {type(exc).__name__}",
        )
    if (
        response.status_code != 200
        or payload.get("code") != 0
        or not payload.get("tenant_access_token")
    ):
        return (
            False,
            (
                "credential rejected: "
                f"http={response.status_code} "
                f"code={payload.get('code')} "
                f"msg={payload.get('msg', '')}"
            ),
        )
    return True, "credential_verified"


def main() -> int:
    if len(sys.argv) != 2 or not sys.argv[1].startswith("cli_"):
        print("usage: verify-feishu-credential.py APP_ID", file=sys.stderr)
        return 2
    app_id = sys.argv[1]
    app_secret = sys.stdin.read().strip()
    if not 8 <= len(app_secret) <= 256 or any(
        character.isspace() for character in app_secret
    ):
        print("invalid App Secret input", file=sys.stderr)
        return 2
    verified, message = verify_credential(app_id, app_secret)
    print(message, file=sys.stdout if verified else sys.stderr)
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
