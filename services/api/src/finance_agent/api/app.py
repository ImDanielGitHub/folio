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
    OriginGuardMiddleware,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    SessionAuthMiddleware,
)
from finance_agent.api.problems import install_problem_handlers
from finance_agent.api.routes import create_router
from finance_agent.api.services import LocalRouteServices

ROOT = Path(__file__).resolve().parents[5]
DEFAULT_DATABASE = ROOT / "var" / "finance-agent.sqlite3"
ALLOWED_RENDERER_ORIGINS = frozenset(
    {
        "http://127.0.0.1:4173",
        "http://localhost:4173",
        "http://127.0.0.1:4174",
        "http://localhost:4174",
        "app://folio",
    }
)
RUNTIME_MODES = frozenset({"development", "demo", "production"})


def configured_database_path() -> Path:
    raw = os.getenv("FINANCE_DATABASE_PATH", "").strip()
    if not raw:
        return DEFAULT_DATABASE.resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate.resolve()


def configured_runtime_mode() -> str:
    value = os.getenv("FOLIO_RUNTIME_MODE", "development").strip().lower()
    if value not in RUNTIME_MODES:
        raise ValueError(
            "FOLIO_RUNTIME_MODE must be development, demo, or production"
        )
    return value


def create_app(
    *,
    database_path: str | Path | None = None,
    auto_seed: bool = True,
    session_token: str | None = None,
    runtime_mode: str | None = None,
) -> FastAPI:
    selected_mode = (runtime_mode or configured_runtime_mode()).strip().lower()
    if selected_mode not in RUNTIME_MODES:
        raise ValueError("runtime_mode must be development, demo, or production")
    selected_database = (
        Path(database_path).expanduser().resolve()
        if database_path is not None
        else configured_database_path()
    )
    services = LocalRouteServices(selected_database, auto_seed=auto_seed)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await services.aclose()

    expose_development_routes = selected_mode in {"development", "demo"}
    value = FastAPI(
        title="Folio Local Finance API",
        version="0.3.0",
        docs_url="/docs" if expose_development_routes else None,
        openapi_url="/openapi.json" if expose_development_routes else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    value.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
    install_problem_handlers(value)
    value.add_middleware(
        CORSMiddleware,
        allow_origins=sorted(ALLOWED_RENDERER_ORIGINS),
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
    value.add_middleware(
        OriginGuardMiddleware,
        allowed_origins=ALLOWED_RENDERER_ORIGINS,
    )
    value.add_middleware(SecurityHeadersMiddleware)
    value.include_router(
        create_router(
            enable_demo=expose_development_routes,
            enable_diagnostics=expose_development_routes,
        )
    )
    value.state.finance_route_services = services
    value.state.folio_runtime_mode = selected_mode
    return value


app = create_app()


__all__ = [
    "ALLOWED_RENDERER_ORIGINS",
    "app",
    "configured_database_path",
    "configured_runtime_mode",
    "create_app",
]
