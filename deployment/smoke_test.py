#!/usr/bin/env python3
"""Smoke-test a deployed RAVAN backend.

    python deployment/smoke_test.py https://ravan-ai.onrender.com
    python deployment/smoke_test.py https://ravan-ai.onrender.com --token … --chat

Checks /health, then optionally one real turn and the request-id dedupe. It
prints the reply text, never the token, and sends nothing anywhere else.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid

def request(base: str, path: str, body: dict | None, token: str, timeout: float):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method="POST" if data else "GET")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.status, json.loads(res.read().decode())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="Backend origin, e.g. https://ravan-ai.onrender.com")
    ap.add_argument("--token", default="", help="RAVAN_API_TOKEN, if the server requires one")
    ap.add_argument("--chat", action="store_true", help="Also send one real message (uses quota)")
    ap.add_argument("--timeout", type=float, default=45.0)
    args = ap.parse_args()

    try:
        status, health = request(args.base, "/health", None, "", args.timeout)
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"health: FAILED ({exc})")
        print("The free instance may be waking up; retry in ~30 s.")
        return 1

    print(f"health: {status} mode={health.get('mode')} ai_configured={health.get('ai_configured')} "
          f"auth_required={health.get('auth_required')}")
    if health.get("mode") != "live":
        print("note: mock mode - replies are canned.")
    if health.get("auth_required") and not args.token:
        print("note: this server needs a token; --token is required for /api/chat.")

    if not args.chat:
        return 0

    body = {
        "session_id": "smoke-" + uuid.uuid4().hex[:12],
        "request_id": uuid.uuid4().hex,
        "text": "Reply with one short sentence confirming you can hear me.",
        "local_time": "2026-01-01T12:00:00Z",
    }
    try:
        status, first = request(args.base, "/api/chat", body, args.token, args.timeout)
    except urllib.error.HTTPError as exc:
        print(f"chat: HTTP {exc.code} - {exc.read().decode()[:300]}")
        return 1
    except (urllib.error.URLError, TimeoutError) as exc:
        print(f"chat: FAILED ({exc})")
        return 1

    reply = (first.get("reply") or "").strip()
    print(f"chat: {status} mode={first.get('mode')} replies={1 if reply else 0} "
          f"visemes={len(first.get('visemes') or [])} calls={len(first.get('calls') or [])}")
    print("reply:", reply[:300] or "(empty)")

    status, again = request(args.base, "/api/chat", body, args.token, args.timeout)
    same = json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)
    print("dedupe:", "PASS (same request_id returned the cached reply)" if same else "FAIL")
    return 0 if same else 1


if __name__ == "__main__":
    sys.exit(main())
