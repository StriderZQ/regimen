"""FastAPI dependencies: who is calling, and with which scopes."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.security import AuthError, Identity, Scope, authenticate
from app.db import get_session


def current_identity(
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Identity:
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    try:
        return authenticate(session, token)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def require(scope: Scope):
    def dependency(identity: Identity = Depends(current_identity)) -> Identity:
        if not identity.has(scope):
            raise HTTPException(status_code=403, detail=f"Requires the {scope.value} scope")
        return identity

    return dependency
