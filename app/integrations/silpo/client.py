"""A client for the grocery MCP server: JSON-RPC over streamable HTTP.

Why a hand-written client rather than a library: exactly one scenario is needed — call a
tool and get JSON back. The MCP library brings a session model and process supervision
with it; two methods are enough here.

The token lives **outside the repository and outside the image**, in a file on the data
volume, mode 0600. Refreshing is the only operation that overwrites it, and it is atomic:
write to a temporary file, then rename. A half-written token would mean losing access
with no way back.

OAuth 2.1 with PKCE is passed once by a human, in a browser. The server can refresh a
token but never obtain the first one: that cannot be done without a person, and
pretending otherwise is harmful.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

TOKEN_FILENAME = "silpo-token.json"
PROTOCOL_VERSION = "2025-06-18"


class SilpoError(Exception):
    """An error to be put into words: the network, the token, or the shop refusing."""


class SilpoAuthError(SilpoError):
    """The token is dead. Only a human fixes this — it is a case of its own."""


@dataclass
class Token:
    access_token: str
    refresh_token: str | None = None
    expires_at: dt.datetime | None = None
    token_url: str | None = None
    client_id: str | None = None

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        # A minute of slack: refreshing slightly early is cheaper than failing mid-call
        return self.expires_at <= dt.datetime.now(dt.UTC) + dt.timedelta(minutes=1)


def token_path() -> Path:
    return get_settings().data_dir / TOKEN_FILENAME


def load_token(path: Path | None = None) -> Token:
    path = path or token_path()
    if not path.exists():
        raise SilpoAuthError(
            f"No token file at {path}. A human passes the grocery authorisation; "
            f"the server cannot do it for them."
        )
    raw = json.loads(path.read_text())
    expires = raw.get("expires_at")
    return Token(
        access_token=raw["access_token"],
        refresh_token=raw.get("refresh_token"),
        expires_at=dt.datetime.fromisoformat(expires) if expires else None,
        token_url=raw.get("token_url"),
        client_id=raw.get("client_id"),
    )


def save_token(token: Token, path: Path | None = None) -> None:
    """An atomic write: a temporary file first, then a rename."""
    path = path or token_path()
    payload = {
        "access_token": token.access_token,
        "refresh_token": token.refresh_token,
        "expires_at": token.expires_at.isoformat() if token.expires_at else None,
        "token_url": token.token_url,
        "client_id": token.client_id,
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


class SilpoClient:
    """Calling grocery MCP tools. One object is one session."""

    def __init__(
        self,
        *,
        token: Token | None = None,
        url: str | None = None,
        http: httpx.Client | None = None,
        token_file: Path | None = None,
    ) -> None:
        settings = get_settings()
        self.url = url or settings.silpo_mcp_url
        self.token_file = token_file
        self._token = token or load_token(token_file)
        self._http = http or httpx.Client(timeout=30.0)
        self._session_id: str | None = None
        self._request_id = 0

    # --- token ----------------------------------------------------------------

    def _refresh(self) -> None:
        token = self._token
        if not token.refresh_token or not token.token_url:
            raise SilpoAuthError(
                "The token expired and there is nothing to refresh it with: no refresh_token. "
                "A human must authorise again."
            )
        response = self._http.post(
            token.token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
                **({"client_id": token.client_id} if token.client_id else {}),
            },
        )
        if response.status_code >= 400:
            raise SilpoAuthError(f"Token refresh was refused: {response.status_code}")
        data = response.json()
        self._token = Token(
            access_token=data["access_token"],
            # The shop may rotate the refresh token: the old one dies afterwards
            refresh_token=data.get("refresh_token") or token.refresh_token,
            expires_at=(
                dt.datetime.now(dt.UTC) + dt.timedelta(seconds=int(data["expires_in"]))
                if data.get("expires_in")
                else None
            ),
            token_url=token.token_url,
            client_id=token.client_id,
        )
        save_token(self._token, self.token_file)
        log.info("Grocery token refreshed")

    # --- transport -------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self._token.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        if self._token.expired:
            self._refresh()

        self._request_id += 1
        body = {"jsonrpc": "2.0", "id": self._request_id, "method": method}
        if params is not None:
            body["params"] = params

        response = self._http.post(self.url, json=body, headers=self._headers())
        if response.status_code == 401:
            self._refresh()
            response = self._http.post(self.url, json=body, headers=self._headers())
        if response.status_code >= 400:
            raise SilpoError(f"{method}: HTTP {response.status_code} {response.text[:200]}")

        if session_id := response.headers.get("Mcp-Session-Id"):
            self._session_id = session_id

        payload = _parse_body(response)
        if "error" in payload:
            raise SilpoError(f"{method}: {payload['error'].get('message', payload['error'])}")
        return payload.get("result", {})

    def initialize(self) -> dict:
        result = self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "regimen", "version": "1.0"},
            },
        )
        self._notify("notifications/initialized")
        return result

    def _notify(self, method: str) -> None:
        try:
            self._http.post(
                self.url,
                json={"jsonrpc": "2.0", "method": method},
                headers=self._headers(),
            )
        except httpx.HTTPError as exc:  # a notification failing is not critical
            log.debug("Notification %s did not go out: %s", method, exc)

    def call(self, tool: str, arguments: dict | None = None) -> Any:
        """Call a tool and return the unpacked JSON response."""
        if self._session_id is None:
            self.initialize()
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments or {}})
        if result.get("isError"):
            raise SilpoError(f"{tool}: {_text_of(result)}")
        return _unpack(result)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SilpoClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def _parse_body(response: httpx.Response) -> dict:
    """The response arrives either as JSON or as an SSE stream — both must work."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" not in content_type:
        return response.json()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            chunk = line[5:].strip()
            if chunk:
                return json.loads(chunk)
    raise SilpoError("Empty stream in the MCP response")


def _text_of(result: dict) -> str:
    return " ".join(
        block.get("text", "") for block in result.get("content", []) if isinstance(block, dict)
    )


def _unpack(result: dict) -> Any:
    """MCP returns the result either as a structure or as text with JSON inside."""
    if "structuredContent" in result:
        return result["structuredContent"]
    text = _text_of(result).strip()
    if not text:
        return result.get("content", [])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
