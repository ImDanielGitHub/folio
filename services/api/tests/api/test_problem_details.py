from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from finance_agent.api.app import create_app
from finance_agent.connectors.base import provider_http_error


@pytest.mark.asyncio
async def test_validation_errors_use_problem_details_without_echoing_input(
    tmp_path: Path,
) -> None:
    app = create_app(database_path=tmp_path / "validation.sqlite3", auto_seed=True)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/jobs/daily-close",
                headers={"Origin": "http://test"},
                json={"secret": "do-not-echo"},
            )
    finally:
        await app.state.finance_route_services.aclose()

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    payload = response.json()
    assert payload["type"] == "https://folio.local/problems/validation-failed"
    assert payload["code"] == "validation_failed"
    assert payload["retryable"] is False
    assert payload["instance"] == "/v1/jobs/daily-close"
    assert payload["errors"]
    assert "do-not-echo" not in response.text


@pytest.mark.asyncio
async def test_unknown_workspace_and_route_use_problem_details(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "missing.sqlite3", auto_seed=True)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            workspace = await client.get("/v1/workspaces/ws_missing/snapshot")
            route = await client.get("/does-not-exist")
    finally:
        await app.state.finance_route_services.aclose()

    for response in (workspace, route):
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "not_found"
        assert response.json()["retryable"] is False


class _RateLimitedPlaidService:
    async def sync_plaid(self, *, public_token: str | None = None) -> dict[str, object]:
        del public_token
        raise provider_http_error("Plaid", 429)


@pytest.mark.asyncio
async def test_typed_connector_problem_preserves_safe_failure_metadata(
    tmp_path: Path,
) -> None:
    app = create_app(database_path=tmp_path / "connector.sqlite3", auto_seed=True)
    original = app.state.finance_route_services
    app.state.finance_route_services = _RateLimitedPlaidService()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/connectors/plaid/sync",
                headers={"Origin": "http://test"},
                json={},
            )
    finally:
        await original.aclose()

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    payload: dict[str, Any] = response.json()
    assert payload["code"] == "provider_rate_limited"
    assert payload["detail"] == "Plaid rate limited the request"
    assert payload["retryable"] is True
    assert payload["provider"] == "plaid"
    assert "detail" not in payload or not isinstance(payload.get("detail"), dict)


class _ExplodingSnapshotService:
    async def workspace_snapshot(self, workspace_id: str) -> dict[str, object]:
        del workspace_id
        raise RuntimeError("sensitive database internals")


@pytest.mark.asyncio
async def test_unexpected_errors_use_generic_problem_details(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "unexpected.sqlite3", auto_seed=True)
    original = app.state.finance_route_services
    app.state.finance_route_services = _ExplodingSnapshotService()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.get("/v1/workspaces/ws_koru_studio/snapshot")
    finally:
        await original.aclose()

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    payload = response.json()
    assert payload["code"] == "internal_error"
    assert payload["retryable"] is False
    assert payload["detail"] == (
        "The local Folio service could not complete the request."
    )
    assert "sensitive database internals" not in response.text
