from __future__ import annotations

from collections.abc import AsyncIterator
from io import BytesIO

import httpx
import pytest
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import PlainTextResponse, Response

from finance_agent.api.http_security import (
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    UploadTooLarge,
    content_disposition,
    read_upload_with_limit,
)
from finance_agent.api.routes import create_router


def test_content_disposition_removes_paths_and_header_injection() -> None:
    value = content_disposition('../../owner-pack"\r\nX-Injected: yes.pdf')

    assert value.startswith('inline; filename="owner-pack_X-Injected: yes.pdf";')
    assert "\r" not in value
    assert "\n" not in value
    assert "../" not in value
    assert "filename*=UTF-8''owner-pack%22X-Injected%3A%20yes.pdf" in value


@pytest.mark.asyncio
async def test_upload_reader_accepts_exact_limit_and_rejects_next_byte() -> None:
    accepted = UploadFile(filename="accepted.csv", file=BytesIO(b"12345"))
    assert await read_upload_with_limit(accepted, max_bytes=5, chunk_bytes=2) == b"12345"

    rejected = UploadFile(filename="rejected.csv", file=BytesIO(b"123456"))
    with pytest.raises(UploadTooLarge, match="5 byte limit"):
        await read_upload_with_limit(rejected, max_bytes=5, chunk_bytes=2)


def create_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=8)
    app.add_middleware(SecurityHeadersMiddleware)

    @app.post("/echo")
    async def echo(request: Request) -> Response:
        return Response(content=await request.body(), media_type="application/octet-stream")

    @app.get("/v1/artifacts/example")
    async def artifact() -> PlainTextResponse:
        return PlainTextResponse("artifact")

    return app


@pytest.mark.asyncio
async def test_request_limit_rejects_declared_and_chunked_bodies() -> None:
    app = create_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        declared = await client.post("/echo", content=b"123456789")
        assert declared.status_code == 413
        assert declared.headers["content-type"].startswith("application/problem+json")

        async def body() -> AsyncIterator[bytes]:
            yield b"1234"
            yield b"56789"

        chunked = await client.post("/echo", content=body())
        assert chunked.status_code == 413
        assert chunked.json()["status"] == 413


@pytest.mark.asyncio
async def test_security_headers_preserve_artifact_cache_semantics() -> None:
    app = create_test_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/echo", content=b"12345678")
        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"

        artifact = await client.get("/v1/artifacts/example")
        assert artifact.status_code == 200
        assert "cache-control" not in artifact.headers
        assert artifact.headers["x-content-type-options"] == "nosniff"


class EmptyRouteServices:
    async def read_events(
        self, *, run_id: str, after_sequence: int
    ) -> tuple[object, ...]:
        return ()


@pytest.mark.asyncio
async def test_route_identifiers_and_resume_headers_fail_closed() -> None:
    app = FastAPI()
    app.include_router(create_router())
    app.state.finance_route_services = EmptyRouteServices()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        invalid_identifier = await client.get("/v1/artifacts/not-valid")
        assert invalid_identifier.status_code == 422

        negative_resume = await client.get(
            "/v1/jobs/run_koru_daily_close_20260717/events",
            headers={"Last-Event-ID": "-1"},
        )
        assert negative_resume.status_code == 400
        assert "non-negative numeric sequence" in negative_resume.json()["detail"]

        valid_resume = await client.get(
            "/v1/jobs/run_koru_daily_close_20260717/events",
            headers={"Last-Event-ID": "0"},
        )
        assert valid_resume.status_code == 200
        assert valid_resume.text == ": keep-alive\n\n"
