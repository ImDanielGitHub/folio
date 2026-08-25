from __future__ import annotations

import io

import httpx
import pytest
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import PlainTextResponse
from finance_agent.api.http_security import (
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    SessionAuthMiddleware,
    UploadTooLarge,
    content_disposition,
    read_upload_with_limit,
)
from starlette.datastructures import Headers


@pytest.mark.asyncio
async def test_rejects_declared_request_body_before_route_execution() -> None:
    app = FastAPI()
    called = False

    @app.post("/body")
    async def body(_: Request) -> dict[str, bool]:
        nonlocal called
        called = True
        return {"called": True}

    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=4)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/body", content=b"12345")
    assert response.status_code == 413
    assert called is False


@pytest.mark.asyncio
async def test_rejects_chunked_body_when_limit_is_crossed() -> None:
    app = FastAPI()

    @app.post("/body")
    async def body(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=4)

    async def chunks():
        yield b"12"
        yield b"345"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/body", content=chunks())
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_session_auth_is_optional_but_enforced_when_configured() -> None:
    app = FastAPI()

    @app.get("/private")
    async def private() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(SessionAuthMiddleware, token="secret-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        rejected = await client.get("/private")
        accepted = await client.get(
            "/private", headers={"X-Folio-Session": "secret-token"}
        )
    assert rejected.status_code == 401
    assert accepted.json() == {"ok": True}


@pytest.mark.asyncio
async def test_security_headers_are_added_without_overwriting_route_headers() -> None:
    app = FastAPI()

    @app.get("/")
    async def root() -> PlainTextResponse:
        return PlainTextResponse("ok", headers={"X-Content-Type-Options": "custom"})

    app.add_middleware(SecurityHeadersMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.headers["x-content-type-options"] == "custom"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_streamed_upload_stops_at_configured_limit() -> None:
    upload = UploadFile(filename="bank.csv", file=io.BytesIO(b"12345"))
    with pytest.raises(UploadTooLarge):
        await read_upload_with_limit(upload, max_bytes=4, chunk_bytes=2)


def test_content_disposition_removes_header_control_characters() -> None:
    value = content_disposition('owner\r\nX-Evil: yes — pack.pdf')
    headers = Headers({"Content-Disposition": value})
    assert "\r" not in headers["content-disposition"]
    assert "\n" not in headers["content-disposition"]
    assert "filename*=UTF-8''" in value
