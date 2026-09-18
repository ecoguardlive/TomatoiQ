"""Minimal dependency-free session authentication for deployment.

Set TOMATOIQ_AUTH_REQUIRED=true, TOMATOIQ_ADMIN_USERNAME and
TOMATOIQ_ADMIN_PASSWORD before exposing the service. Tokens are signed and
short-lived.

Two ways to present a token, checked in this order:
  1. `Authorization: Bearer <token>` header -- for scripts/API clients.
  2. An HttpOnly session cookie set by /api/auth/login -- what the PWA
     itself uses. This is also what makes the /api/live WebSocket work
     under auth: browsers cannot attach a custom header to a WebSocket
     handshake, but they DO send cookies on it automatically (same-origin),
     so a cookie is the only mechanism that authenticates both HTTP and
     WebSocket requests from the same login.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import HTTPException
from starlette.requests import HTTPConnection

try:
    # Only docker-compose's `env_file:` actually loads a .env file on its
    # own -- a plain `python -m uvicorn pwa_server:app` does NOT read .env
    # automatically, so without this, everything in .env silently has no
    # effect at all outside Docker (auth looks "enabled" in your config but
    # TOMATOIQ_AUTH_REQUIRED etc. are simply never set). python-dotenv is
    # already pulled in transitively by uvicorn[standard]; this just also
    # uses it explicitly so a plain local run picks up .env too.
    #
    # usecwd=True: python-dotenv's default search anchors on this file's
    # (auth.py's) own location via stack inspection, not on the directory
    # the server is actually started from -- usecwd=True makes it look for
    # .env in the current working directory instead, which is what anyone
    # running `cd tomatoiq && python -m uvicorn pwa_server:app` expects.
    from dotenv import load_dotenv, find_dotenv
    load_dotenv(find_dotenv(usecwd=True))
except ImportError:
    pass

COOKIE_NAME = "tomatoiq_session"

_SECRET = os.getenv("TOMATOIQ_AUTH_SECRET", "")
_REQUIRED = os.getenv("TOMATOIQ_AUTH_REQUIRED", "false").lower() == "true"
_USERNAME = os.getenv("TOMATOIQ_ADMIN_USERNAME", "")
_PASSWORD = os.getenv("TOMATOIQ_ADMIN_PASSWORD", "")
# Only disable for local plain-HTTP testing -- a session cookie sent over
# plain HTTP can be read by anyone on the network path.
_COOKIE_SECURE = os.getenv("TOMATOIQ_COOKIE_SECURE", "true").lower() == "true"


def auth_enabled() -> bool:
    return _REQUIRED


def cookie_secure() -> bool:
    return _COOKIE_SECURE


def validate_deployment_auth() -> None:
    if _REQUIRED and (not _SECRET or not _USERNAME or not _PASSWORD):
        raise RuntimeError("TOMATOIQ_AUTH_REQUIRED requires TOMATOIQ_AUTH_SECRET, TOMATOIQ_ADMIN_USERNAME, and TOMATOIQ_ADMIN_PASSWORD")


def authenticate(username: str, password: str) -> bool:
    return bool(_USERNAME) and secrets.compare_digest(username, _USERNAME) and secrets.compare_digest(password, _PASSWORD)


def issue_token(username: str, ttl_seconds: int = 3600) -> str:
    payload = {"sub": username, "exp": int(time.time()) + ttl_seconds}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{signature}"


def _verify_token(token: str) -> str:
    """Returns the username the token was issued for, or raises HTTPException."""
    try:
        raw, signature = token.rsplit(".", 1)
        expected = hmac.new(_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if int(payload["exp"]) < time.time():
            raise ValueError("expired")
        return str(payload["sub"])
    except (ValueError, KeyError, json.JSONDecodeError):
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def _extract_token(connection: HTTPConnection) -> str | None:
    header = connection.headers.get("authorization")
    if header and header.startswith("Bearer "):
        return header[7:]
    return connection.cookies.get(COOKIE_NAME)


def require_user(connection: HTTPConnection) -> str:
    """Validates an incoming HTTP request OR WebSocket handshake. Works with
    either connection type since Starlette's Request and WebSocket both
    inherit from HTTPConnection and expose the same .headers/.cookies."""
    if not _REQUIRED:
        return "development"
    token = _extract_token(connection)
    if not token:
        raise HTTPException(status_code=401, detail="Authentication required")
    return _verify_token(token)
