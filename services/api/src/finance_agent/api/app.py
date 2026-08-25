"""Loopback-only FastAPI composition root for the Folio local service."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from finance_agent.api.http_security import (
    MAX_REQUEST_BODY_BYTES,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    SessionAuthMiddleware,
)
from finance_agent.api.routes import create_router
from finance_agent.api.services import LocalRouteServices

ROOT = Path(__file__).resolve().parents[5]
DEFAULT_DATABASE = ROOT / "var" / "finance-agent.sqlite3"


def create_app(
    *,
    database_path: str | Path | None = None,
    auto_seed: bool = True,
    session_token: str | None = None,
) -> FastAPI:
    services = LocalRouteServices(database_path or DEFAULT_DATABASE, auto_seed=auto_seed)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await services.aclose()

    value = FastAPI(
        title="Folio Local Finance API",
        version="0.2.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    value.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
    value.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:4173",
            "http://localhost:4173",
            "http://127.0.0.1:4174",
            "http://localhost:4174",
            "app://folio",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Last-Event-ID", "X-Folio-Session"],
    )
    value.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "test", "testserver"],
    )
    value.add_middleware(
        SessionAuthMiddleware,
        token=session_token if session_token is not None else os.getenv("FOLIO_SESSION_TOKEN"),
    )
    value.add_middleware(SecurityHeadersMiddleware)
    value.include_router(create_router())
    value.state.finance_route_services = services
    return value


app = create_app()


__all__ = ["app", "create_app"]
