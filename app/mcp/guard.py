"""The 401 that starts the whole OAuth conversation.

Without it a client that has never seen this server has nothing to go on. The tools
already refuse to run without a token, but they refuse *inside* the protocol — the HTTP
response is a perfectly successful 200 carrying an error, and a connector reads that as
"the server works, it just does not want to talk". It never asks who to log in with.

So the check moves one layer out: no valid bearer token, no MCP at all, and the refusal
carries the address of the metadata document that explains how to get one.
"""

from __future__ import annotations

import json

from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.core.security import AuthError, authenticate
from app.db import session_scope


def _token_accepted(token: str | None) -> bool:
    if not token:
        return False
    try:
        with session_scope() as session:
            authenticate(session, token)
    except AuthError:
        return False
    return True


def _challenge() -> str:
    """RFC 6750 plus RFC 9728: the scheme, what is needed, and where to read about it."""
    issuer = get_settings().issuer
    parts = ['Bearer error="invalid_token"', 'scope="read write_facts"']
    if issuer:
        parts.insert(
            1, f'resource_metadata="{issuer}/.well-known/oauth-protected-resource/mcp"'
        )
    return ", ".join(parts)


class RequireBearer:
    """Wraps the MCP application. Everything below it may assume a caller."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                raw = value.decode("latin-1")
                break
        token = raw[7:].strip() if raw[:7].lower() == "bearer " else None

        if not await run_in_threadpool(_token_accepted, token):
            body = json.dumps(
                {"error": "invalid_token",
                 "error_description": "A token is required. See the metadata document."}
            ).encode()
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", _challenge().encode("latin-1")),
                    (b"cache-control", b"no-store"),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)
