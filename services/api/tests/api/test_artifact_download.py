"""Browser downloads require session authentication and a readable integrity receipt."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from finance_agent.api.app import create_app


@pytest.mark.asyncio
async def test_authenticated_artifact_exposes_only_the_integrity_header(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "download.sqlite3", session_token="synthetic-session")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = "/v1/artifacts/artifact_koru_owner_pack_pdf"
            assert (await client.get(path)).status_code == 401
            response = await client.get(
                path,
                headers={
                    "X-Folio-Session": "synthetic-session",
                    "Origin": "http://127.0.0.1:4173",
                },
            )
            assert response.status_code == 200
            assert response.content.startswith(b"%PDF-")
            assert response.headers["etag"] == f'"{hashlib.sha256(response.content).hexdigest()}"'
            assert response.headers["access-control-expose-headers"] == "ETag"
            assert "synthetic-session" not in str(response.headers)
    finally:
        await app.state.finance_route_services.aclose()
