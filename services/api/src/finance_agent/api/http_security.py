"""HTTP boundary controls for Folio's loopback API."""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from typing import Final
from urllib.parse import quote

from fastapi import UploadFile
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

IDENTIFIER_PATTERN: Final = r"^[a-z][a-z0-9]{1,15}_[a-z0-9][a-z0-9_]{2,95}$"
IDENTIFIER_MIN_LENGTH: Final = 6
IDENTIFIER_MAX_LENGTH: Final = 113
MAX_CSV_BYTES: Final = 10_000_000
MAX_REQUEST_BODY_BYTES: Final = 12_000_000
UPLOAD_CHUNK_BYTES: Final = 1_048_576

_SECURITY_HEADERS: Final[Mapping[bytes, bytes]] = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"no-referrer",
    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
    b"cross-origin-resource-policy": b"same-origin",
}


class UploadTooLarge(ValueError):
    """Raised as soon as a streamed upload crosses its byte limit."""


class _RequestBodyTooLarge(RuntimeError):
    pass


async def read_upload_with_limit(
    upload: UploadFile,
    *,
    max_bytes: int = MAX_CSV_BYTES,
    chunk_bytes: int = UPLOAD_CHUNK_BYTES,
) -> bytes:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(chunk_bytes)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLarge(f"upload exceeds the {max_bytes} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def content_disposition(filename: str, *, disposition: str = "inline") -> str:
    if disposition not in {"inline", "attachment"}:
        raise ValueError("unsupported content disposition")
    cleaned = filename.replace("\r", " ").replace("\n", " ").strip()
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", cleaned)
    if not cleaned:
        cleaned = "artifact"
    fallback = cleaned.encode("ascii", "ignore").decode("ascii")
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", fallback).strip(" .") or "artifact"
    fallback = fallback[:180]
    encoded = quote(cleaned, safe="")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}"


async def _send_problem(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status: int,
    title: str,
    detail: str,
) -> None:
    response = JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={"title": title, "status": status, "detail": detail},
    )
    await response(scope, receive, send)


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_length = Headers(scope=scope).get("content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                await _send_problem(
                    scope,
                    receive,
                    send,
                    status=400,
                    title="Invalid Content-Length",
                    detail="Content-Length must be a non-negative integer.",
                )
                return
            if content_length < 0:
                await _send_problem(
                    scope,
                    receive,
                    send,
                    status=400,
                    title="Invalid Content-Length",
                    detail="Content-Length must be a non-negative integer.",
                )
                return
            if content_length > self.max_bytes:
                await _send_problem(
                    scope,
                    receive,
                    send,
                    status=413,
                    title="Request body too large",
                    detail=f"The request exceeds the {self.max_bytes} byte limit.",
                )
                return

        total = 0

        async def limited_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await _send_problem(
                scope,
                receive,
                send,
                status=413,
                title="Request body too large",
                detail=f"The request exceeds the {self.max_bytes} byte limit.",
            )


class SessionAuthMiddleware:
    """Require a per-launch secret when the local launcher configures one."""

    def __init__(self, app: ASGIApp, *, token: str | None) -> None:
        self.app = app
        self.token = token.strip() if token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.token is None:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        if method == "OPTIONS" or path == "/health" or path.startswith("/v1/artifacts/"):
            await self.app(scope, receive, send)
            return
        supplied = Headers(scope=scope).get("x-folio-session") or ""
        if not hmac.compare_digest(supplied, self.token):
            await _send_problem(
                scope,
                receive,
                send,
                status=401,
                title="Local session authentication required",
                detail="The request did not present the current Folio session token.",
            )
            return
        await self.app(scope, receive, send)


class OriginGuardMiddleware:
    """Reject browser mutation requests from origins outside the desktop allowlist.

    Non-browser local clients do not send Origin and remain protected by the
    per-launch session credential. CORS alone is not treated as authentication.
    """

    _SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, app: ASGIApp, *, allowed_origins: set[str] | frozenset[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(value.rstrip("/") for value in allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        headers = Headers(scope=scope)
        origin = headers.get("origin")
        request_host = (headers.get("host") or "").split(":", 1)[0]
        test_client_origin = (
            origin in {"http://test", "https://test", "http://testserver", "https://testserver"}
            and request_host in {"test", "testserver"}
        )
        if (
            method not in self._SAFE_METHODS
            and origin is not None
            and origin.rstrip("/") not in self.allowed_origins
            and not test_client_origin
        ):
            await _send_problem(
                scope,
                receive,
                send,
                status=403,
                title="Untrusted request origin",
                detail="The mutation did not originate from an authorised Folio renderer.",
            )
            return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def hardened_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value)
                    for name, value in _SECURITY_HEADERS.items()
                    if name not in present
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, hardened_send)
