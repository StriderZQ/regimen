"""Grocery authentication: OAuth 2.1 with PKCE, once, by hand.

The server cannot obtain access itself and does not pretend to: it needs a browser and a
live login by the owner. So the command runs on his machine, starts a local receiver for
a minute, catches the response and writes the token to a file. The file moves to the server.

Client registration is dynamic (RFC 7591): the shop returns a `registration_endpoint`, so
no keys have to be issued in advance.

The `code_verifier` is random and lives only in this process's memory — that is the point
of PKCE: an intercepted code is useless without it.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import http.server
import secrets
import threading
import urllib.parse
import webbrowser
from dataclasses import dataclass

import httpx

from app.integrations.silpo.client import Token, save_token

DISCOVERY = "/.well-known/oauth-authorization-server"
CALLBACK_PORT = 8765
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
SCOPES = "offline_access"


@dataclass
class Endpoints:
    authorize: str
    token: str
    register: str | None


class _Catcher(http.server.BaseHTTPRequestHandler):
    """Catches the redirect carrying the code. One request and done."""

    code: str | None = None
    error: str | None = None

    def do_GET(self) -> None:  # noqa: N802 — the name comes from the base class
        query = urllib.parse.urlparse(self.path).query
        params = urllib.parse.parse_qs(query)
        _Catcher.code = (params.get("code") or [None])[0]
        _Catcher.error = (params.get("error") or [None])[0]

        body = (
            "<h2>Done. You can close this tab.</h2>"
            if _Catcher.code
            else f"<h2>Did not work: {_Catcher.error}</h2>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(f"<meta charset='utf-8'>{body}".encode())

    def log_message(self, *_args) -> None:
        """Silence: URLs carrying the code are never logged."""


def discover(base_url: str) -> Endpoints:
    root = base_url.rsplit("/mcp", 1)[0]
    response = httpx.get(root + DISCOVERY, timeout=20.0)
    response.raise_for_status()
    data = response.json()
    return Endpoints(
        authorize=data["authorization_endpoint"],
        token=data["token_endpoint"],
        register=data.get("registration_endpoint"),
    )


def register_client(endpoints: Endpoints) -> tuple[str, str | None]:
    if not endpoints.register:
        raise RuntimeError("The shop returns no registration_endpoint — a client_id is needed")
    response = httpx.post(
        endpoints.register,
        json={
            "client_name": "regimen",
            "redirect_uris": [REDIRECT_URI],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        timeout=20.0,
    )
    response.raise_for_status()
    data = response.json()
    return data["client_id"], data.get("client_secret")


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def login(base_url: str, *, timeout_s: int = 180) -> Token:
    """Run the authorisation and return a token. Requires a browser on this machine."""
    endpoints = discover(base_url)
    client_id, client_secret = register_client(endpoints)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(16)

    server = http.server.HTTPServer(("localhost", CALLBACK_PORT), _Catcher)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    url = endpoints.authorize + "?" + urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": SCOPES,
        }
    )
    print("Opening a browser. Sign in and approve the access.")
    print(f"If the browser did not open, open this manually:\n{url}\n")
    webbrowser.open(url)

    thread.join(timeout=timeout_s)
    server.server_close()

    if _Catcher.error:
        raise RuntimeError(f"The shop refused: {_Catcher.error}")
    if not _Catcher.code:
        raise RuntimeError("No code arrived: the wait timed out, or the tab was closed")

    payload = {
        "grant_type": "authorization_code",
        "code": _Catcher.code,
        "redirect_uri": REDIRECT_URI,
        "client_id": client_id,
        "code_verifier": verifier,
    }
    if client_secret:
        payload["client_secret"] = client_secret

    response = httpx.post(endpoints.token, data=payload, timeout=30.0)
    if response.status_code >= 400:
        raise RuntimeError(
            f"Code exchange refused: {response.status_code} {response.text[:200]}"
        )
    data = response.json()

    token = Token(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token"),
        expires_at=(
            dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(data["expires_in"]))
            if data.get("expires_in")
            else None
        ),
        token_url=endpoints.token,
        client_id=client_id,
    )
    save_token(token)
    return token


def describe(token: Token) -> str:
    when = (
        token.expires_at.astimezone().strftime("%d.%m %H:%M")
        if token.expires_at
        else "not given"
    )
    return (
        f"Token received. Valid until {when}. "
        + ("It can be refreshed without you." if token.refresh_token else
           "WARNING: no refresh_token was issued — you will have to sign in again.")
    )


if __name__ == "__main__":
    import sys

    from app.config import get_settings

    print(describe(login(get_settings().silpo_mcp_url)))
    print(f"Token file: {get_settings().data_dir / 'silpo-token.json'}")
    sys.exit(0)
