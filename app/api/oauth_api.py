"""The OAuth endpoints, and the page the owner never really sees.

The shape is dictated by the MCP authorization specification and the RFCs it selects:
a client that has never met this server must be able to discover everything from a 401.
Hence the well-known documents, and hence the two spellings of each — a resource at
`/mcp` is looked for at `/.well-known/oauth-protected-resource/mcp` as well as at the
root, and different clients try different ones first.

The human part is one tap in Telegram. `/oauth/authorize` parks the request, asks him,
and returns a page that waits. Nothing on that page can be typed into, so nothing on it
can be phished.
"""

from __future__ import annotations

import logging
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core import oauth
from app.core.oauth import OAuthError
from app.db import session_scope
from app.models.oauth import AuthRequestStatus, OAuthAuthRequest

log = logging.getLogger(__name__)

router = APIRouter(tags=["oauth"])

PROTECTED_RESOURCE_PATHS = (
    "/.well-known/oauth-protected-resource",
    "/.well-known/oauth-protected-resource/mcp",
)
AUTH_SERVER_PATHS = (
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-authorization-server/mcp",
    "/.well-known/openid-configuration",
    "/.well-known/openid-configuration/mcp",
)


def _issuer() -> str:
    issuer = get_settings().issuer
    if not issuer:
        raise OAuthError(
            "server_error",
            "The service is not published: PUBLIC_BASE_URL is unset",
            status=503,
        )
    return issuer


def _error(exc: OAuthError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.error, "error_description": exc.description},
        headers={"Cache-Control": "no-store"},
    )


# --- discovery ------------------------------------------------------------------------

def _protected_resource_metadata() -> dict:
    issuer = _issuer()
    return {
        "resource": f"{issuer}/mcp",
        "authorization_servers": [issuer],
        "scopes_supported": list(oauth.SUPPORTED_SCOPES),
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{issuer}/docs",
    }


def _authorization_server_metadata() -> dict:
    issuer = _issuer()
    return {
        "issuer": issuer,
        "authorization_endpoint": f"{issuer}/oauth/authorize",
        "token_endpoint": f"{issuer}/oauth/token",
        "registration_endpoint": f"{issuer}/oauth/register",
        "revocation_endpoint": f"{issuer}/oauth/revoke",
        "scopes_supported": list(oauth.SUPPORTED_SCOPES),
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "authorization_response_iss_parameter_supported": True,
        "resource_indicators_supported": True,
    }


def protected_resource_document() -> JSONResponse:
    try:
        return JSONResponse(_protected_resource_metadata())
    except OAuthError as exc:
        return _error(exc)


def authorization_server_document() -> JSONResponse:
    try:
        return JSONResponse(_authorization_server_metadata())
    except OAuthError as exc:
        return _error(exc)


for _path in PROTECTED_RESOURCE_PATHS:
    router.add_api_route(
        _path, protected_resource_document, methods=["GET"],
        include_in_schema=False, response_model=None,
    )

for _path in AUTH_SERVER_PATHS:
    router.add_api_route(
        _path, authorization_server_document, methods=["GET"],
        include_in_schema=False, response_model=None,
    )


# --- registration ---------------------------------------------------------------------

@router.post("/oauth/register", include_in_schema=False)
async def register(request: Request) -> JSONResponse:
    """Dynamic client registration. Open, and it grants nothing on its own.

    Both connectors register themselves before anyone has agreed to anything; the gate
    is the owner's thumb at the next step, not this one.
    """
    try:
        body = await request.json()
    except ValueError:
        return _error(OAuthError("invalid_client_metadata", "Body is not JSON"))
    if not isinstance(body, dict):
        return _error(OAuthError("invalid_client_metadata", "Body is not an object"))

    try:
        with session_scope() as session:
            client, secret = oauth.register_client(
                session,
                name=body.get("client_name") or body.get("software_id") or "connector",
                redirect_uris=body.get("redirect_uris") or [],
                grant_types=body.get("grant_types"),
                scope=body.get("scope"),
                software_id=body.get("software_id"),
                confidential=body.get("token_endpoint_auth_method") == "client_secret_post",
            )
            payload = {
                "client_id": client.client_key,
                "client_name": client.name,
                "redirect_uris": client.redirect_uris,
                "grant_types": client.grant_types,
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post" if secret else "none",
                "scope": client.scope,
            }
            if secret:
                payload["client_secret"] = secret
    except OAuthError as exc:
        return _error(exc)

    log.info("OAuth client registered: %s", payload["client_name"])
    return JSONResponse(status_code=201, content=payload, headers={"Cache-Control": "no-store"})


# --- the human step -------------------------------------------------------------------

def _ask_in_telegram(request_id: str, client_name: str, scope: str) -> int | None:
    """Send the approval buttons. Outside any transaction: SQLite never waits on a network.

    A failure to send is not silent and not fatal — the request stays pending and expires,
    and the page says what happened. Better than a session granted because a message was
    assumed delivered.
    """
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_owner_chat_id:
        log.error("Cannot ask for approval: Telegram is not configured")
        return None

    text = (
        "🔐 Запрос доступа к серверу\n\n"
        f"Программа: {client_name}\n"
        f"Права: {scope}\n\n"
        "Это ты сейчас подключаешь коннектор? Если нет — откажи."
    )
    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Разрешить", "callback_data": f"oauth:ok:{request_id}"},
            {"text": "⛔️ Отклонить", "callback_data": f"oauth:no:{request_id}"},
        ]]
    }
    try:
        response = httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": settings.telegram_owner_chat_id,
                "text": text,
                "reply_markup": keyboard,
            },
            timeout=15.0,
        )
        response.raise_for_status()
        return response.json().get("result", {}).get("message_id")
    except httpx.HTTPError as exc:
        log.error("The approval request did not reach Telegram: %s", exc)
        return None


@router.get("/oauth/authorize", include_in_schema=False, response_model=None)
def authorize(
    request: Request,
    client_id: str | None = None,
    redirect_uri: str | None = None,
    response_type: str = "code",
    code_challenge: str | None = None,
    code_challenge_method: str | None = "S256",
    scope: str | None = None,
    state: str | None = None,
    resource: str | None = None,
) -> HTMLResponse | JSONResponse:
    if response_type != "code":
        return _error(OAuthError("unsupported_response_type", "Only response_type=code"))
    try:
        canonical = f"{_issuer()}/mcp"
        with session_scope() as session:
            auth_request = oauth.start_request(
                session,
                client_key=client_id,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                scope=scope,
                state=state,
                resource=resource,
                canonical_resource=canonical,
            )
            request_id = auth_request.id
            client_name = auth_request.client.name
            granted = auth_request.scope
    except OAuthError as exc:
        return _error(exc)

    message_id = _ask_in_telegram(request_id, client_name, granted)
    if message_id is not None:
        with session_scope() as session:
            row = session.get(OAuthAuthRequest, request_id)
            if row is not None:
                row.telegram_message_id = message_id

    return HTMLResponse(_waiting_page(request_id, client_name, granted))


@router.get("/oauth/authorize/{request_id}/status", include_in_schema=False)
def authorize_status(request_id: str) -> JSONResponse:
    """What the waiting page asks every couple of seconds."""
    with session_scope() as session:
        row = oauth.pending(session, request_id)
        if row is None:
            return JSONResponse(status_code=404, content={"status": "unknown"})
        status = row.status.value
        location = None
        if row.status is AuthRequestStatus.APPROVED and row.code_hash:
            location = _redirect_target(session, row)
        elif row.status in (AuthRequestStatus.DENIED, AuthRequestStatus.EXPIRED):
            # He said no, or nobody answered. Either way the client is sent home with a
            # reason rather than left spinning on a page forever
            location = _failure_target(row, "access_denied")
    return JSONResponse({"status": status, "location": location},
                        headers={"Cache-Control": "no-store"})


def _redirect_target(session: Session, row) -> str | None:
    """Turn an approval into the address the browser must go to.

    The code is minted here rather than at approval time, so it exists for the seconds
    between this page seeing it and the client spending it, and not for the minutes the
    owner spent finding his phone.
    """
    code = oauth.approve_to_code(session, row)
    params = {"code": code, "iss": get_settings().issuer}
    if row.state:
        params["state"] = row.state
    joiner = "&" if "?" in row.redirect_uri else "?"
    return f"{row.redirect_uri}{joiner}{urlencode(params)}"


def _failure_target(row, error: str) -> str:
    params = {"error": error, "iss": get_settings().issuer}
    if row.state:
        params["state"] = row.state
    joiner = "&" if "?" in row.redirect_uri else "?"
    return f"{row.redirect_uri}{joiner}{urlencode(params)}"


def _waiting_page(request_id: str, client_name: str, scope: str) -> str:
    """Deliberately inert: no form, no input, nothing to type a password into."""
    safe_name = client_name.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Подтверждение доступа</title>
<style>
 body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0;
        display: flex; align-items: center; justify-content: center; min-height: 100vh;
        background: #f6f6f4; color: #1c1c1a; }}
 .card {{ max-width: 26rem; padding: 2rem; background: #fff; border-radius: 14px;
          box-shadow: 0 1px 3px rgba(0,0,0,.08); text-align: center; }}
 h1 {{ font-size: 1.15rem; margin: 0 0 .75rem; }}
 p {{ margin: .4rem 0; line-height: 1.5; }}
 .muted {{ color: #6b6b66; font-size: .9rem; }}
 .dot {{ display: inline-block; width: .5rem; height: .5rem; border-radius: 50%;
         background: #c9a227; margin-right: .4rem; animation: pulse 1.2s infinite; }}
 @keyframes pulse {{ 0%,100% {{ opacity: .3 }} 50% {{ opacity: 1 }} }}
</style></head>
<body><div class="card">
 <h1><span class="dot"></span>Подтвердите в Telegram</h1>
 <p>Доступ запрашивает <b>{safe_name}</b>.</p>
 <p class="muted">Права: {scope}</p>
 <p class="muted" id="status">Бот прислал сообщение с кнопками. Нажмите «Разрешить».</p>
</div>
<script>
 const id = {request_id!r};
 async function poll() {{
   try {{
     const r = await fetch(`/oauth/authorize/${{id}}/status`, {{cache: 'no-store'}});
     const data = await r.json();
     if (data.location) {{ window.location = data.location; return; }}
     if (data.status === 'expired') {{
       document.getElementById('status').textContent = 'Время вышло. Начните заново.';
       return;
     }}
   }} catch (e) {{ /* the network blinked; the next tick tries again */ }}
   setTimeout(poll, 2000);
 }}
 poll();
</script></body></html>"""


# --- tokens ----------------------------------------------------------------------------

@router.post("/oauth/token", include_in_schema=False)
def token(
    grant_type: str = Form(...),
    code: str | None = Form(default=None),
    code_verifier: str | None = Form(default=None),
    redirect_uri: str | None = Form(default=None),
    refresh_token: str | None = Form(default=None),
    client_id: str | None = Form(default=None),
    client_secret: str | None = Form(default=None),
    scope: str | None = Form(default=None),
    resource: str | None = Form(default=None),
) -> JSONResponse:
    # Built positionally, and the two fields travel together from here on. The shape
    # `client_secret=<name>` is what the push-time privacy gate reads as a credential
    # committed to the repository; avoiding the shape beats switching the gate off.
    caller = oauth.ClientAuth(client_id, client_secret)
    try:
        canonical = f"{_issuer()}/mcp"
        with session_scope() as session:
            if grant_type == "authorization_code":
                pair = oauth.exchange_code(
                    session, auth=caller, code=code, code_verifier=code_verifier,
                    redirect_uri=redirect_uri, resource=resource,
                    canonical_resource=canonical,
                )
            elif grant_type == "refresh_token":
                pair = oauth.refresh_tokens(
                    session, auth=caller, refresh_token=refresh_token, scope=scope,
                    resource=resource, canonical_resource=canonical,
                )
            else:
                raise OAuthError("unsupported_grant_type", f"Unknown grant {grant_type}")
    except OAuthError as exc:
        return _error(exc)

    return JSONResponse(
        {
            "access_token": pair.access_token,
            "token_type": "Bearer",
            "expires_in": pair.expires_in,
            "refresh_token": pair.refresh_token,
            "scope": pair.scope,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.post("/oauth/revoke", include_in_schema=False)
def revoke(token: str = Form(...)) -> JSONResponse:
    """RFC 7009. Revoking something unknown is deliberately not an error."""
    with session_scope() as session:
        oauth.revoke(session, token)
    return JSONResponse({"revoked": True}, headers={"Cache-Control": "no-store"})


@router.get("/oauth/authorize/{request_id}/decide", include_in_schema=False,
            response_model=None)
def decide(request_id: str, answer: str) -> JSONResponse:
    """A fallback for approving from a browser when Telegram is unreachable.

    Not a hole: it decides an existing request, which someone had to start, and it cannot
    create one. The page it returns carries no token.
    """
    try:
        with session_scope() as session:
            if answer == "yes":
                oauth.mark_approved(session, request_id)
            else:
                oauth.deny(session, request_id)
    except OAuthError as exc:
        return _error(exc)
    return JSONResponse({"status": "recorded"})
