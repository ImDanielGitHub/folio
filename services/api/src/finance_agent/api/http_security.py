"""HTTP boundary hardening shared by the loopback API and its route handlers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, cast
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
}


class UploadTooLarge(ValueError):
    """Raised when a streamed upload crosses its configured byte limit."""


class _RequestBodyTooLarge(RuntimeError):
    """Internal sentinel used to stop a chunked request before route parsing."""


async def read_upload_with_limit(
    upload: UploadFile,
    *,
    max_bytes: int = MAX_CSV_BYTES,
    chunk_bytes: int = UPLOAD_CHUNK_BYTES,
) -> bytes:
    """Read an upload incrementally and stop as soon as it exceeds ``max_bytes``."""
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
    """Build a header-safe Content-Disposition value with a UTF-8 filename."""
    if disposition not in {"inline", "attachment"}:
        raise ValueError("disposition must be inline or attachment")

    leaf = filename.replace("\\", "/").rsplit("/", 1)[-1]
    leaf = "".join(character for character in leaf if character >= " " and character != "\x7f")
    leaf = leaf.strip() or "artifact"
    leaf = leaf[:180]

    ascii_fallback = leaf.encode("ascii", "replace").decode("ascii")
    ascii_fallback = ascii_fallback.replace('"', "_").replace("\\", "_")
    encoded = quote(leaf, safe="!#$&+-.^_`|~")
    return (
        f'{disposition}; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{encoded}"
    )


class RequestBodyLimitMiddleware:
    """Reject oversized HTTP request bodies, including chunked bodies."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int = MAX_REQUEST_BODY_BYTES,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = Headers(scope=scope).get("content-length")
        if content_length is not None:
            try:
                declared_bytes = int(content_length)
            except ValueError:
                await self._send_problem(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    detail="Content-Length must be a non-negative integer.",
                )
                return
            if declared_bytes < 0:
                await self._send_problem(
                    scope,
                    receive,
                    send,
                    status_code=400,
                    detail="Content-Length must be a non-negative integer.",
                )
                return
            if declared_bytes > self.max_bytes:
                await self._send_problem(
                    scope,
                    receive,
                    send,
                    status_code=413,
                    detail=f"Request body exceeds the {self.max_bytes} byte limit.",
                )
                return

        received_bytes = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                body = cast(bytes, message.get("body", b""))
                received_bytes += len(body)
                if received_bytes > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracked_send)
        except _RequestBodyTooLarge:
            if response_started:
                raise
            await self._send_problem(
                scope,
                receive,
                send,
                status_code=413,
                detail=f"Request body exceeds the {self.max_bytes} byte limit.",
            )

    @staticmethod
    async def _send_problem(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            media_type="application/problem+json",
            content={
                "type": "https://finance-agent.local/problems/request-body",
                "title": "Invalid request body",
                "status": status_code,
                "detail": detail,
            },
        )
        await response(scope, receive, send)


class SecurityHeadersMiddleware:
    """Attach conservative browser security headers to every API response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", ""))

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw_headers = list(message.get("headers", []))
                existing = {name.lower() for name, _ in raw_headers}
                for name, value in _SECURITY_HEADERS.items():
                    if name not in existing:
                        raw_headers.append((name, value))
                if (
                    b"cache-control" not in existing
                    and not path.startswith("/v1/artifacts/")
                ):
                    raw_headers.append((b"cache-control", b"no-store"))
                message["headers"] = raw_headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


__all__ = [
    "IDENTIFIER_MAX_LENGTH",
    "IDENTIFIER_MIN_LENGTH",
    "IDENTIFIER_PATTERN",
    "MAX_CSV_BYTES",
    "MAX_REQUEST_BODY_BYTES",
    "RequestBodyLimitMiddleware",
    "SecurityHeadersMiddleware",
    "UploadTooLarge",
    "content_disposition",
    "read_upload_with_limit",
]
