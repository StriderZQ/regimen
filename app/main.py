"""The service entry point."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api import oauth_api, pantry_api
from app.config import get_settings
from app.db import SessionLocal
from app.mcp import server as mcp_server
from app.mcp.guard import RequireBearer

settings = get_settings()
logging.basicConfig(
    level=settings.log_level,
    format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
)

# MCP brings its own lifespan (protocol sessions) and it must be handed to FastAPI,
# otherwise the adapter mounts but does not work.
mcp_app = mcp_server.http_app()

app = FastAPI(
    title="Regimen",
    description="Self-hosted pantry, nutrition and training service",
    version="0.1.0",
    docs_url="/docs" if settings.environment != "prod" else None,
    lifespan=mcp_app.lifespan,
)

app.include_router(pantry_api.router)
# Discovery, registration and the approval page. Mounted at the root because the
# well-known documents have to live exactly where the RFCs say they do
app.include_router(oauth_api.router)

# The door for agents: anything that speaks MCP. A bearer token is demanded at the HTTP
# layer, not inside the protocol, so a client that has none learns where to get one
app.mount("/mcp", RequireBearer(mcp_app))


@app.get("/healthz", tags=["ops"])
def healthz() -> JSONResponse:
    """Healthy means the database answers.

    A check that does not touch the database once reported "all good" on a service with
    no schema: the container counted as alive while every request failed.
    """
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1 FROM principals LIMIT 1"))
    except Exception as exc:  # noqa: BLE001 — the reason belongs in the response
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]},
        )
    return JSONResponse({"ok": True, "environment": settings.environment})


def run() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app", host=settings.host, port=settings.port, log_level=settings.log_level.lower()
    )


if __name__ == "__main__":
    run()
