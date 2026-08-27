"""RFC 9457 problem details for the loopback API boundary."""

from __future__ import annotations

from collections.abc import Mapping
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "authentication_required",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "request_too_large",
    422: "validation_failed",
    429: "rate_limited",
    500: "internal_error",
    502: "upstream_failure",
    503: "service_unavailable",
    504: "upstream_timeout",
}
_RETRYABLE_STATUSES = frozenset({408, 425, 429, 502, 503, 504})


def problem_payload(
    *,
    status: int,
    detail: str,
    title: str | None = None,
    code: str | None = None,
    retryable: bool | None = None,
    instance: str | None = None,
    errors: list[Mapping[str, object]] | None = None,
    extensions: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Build a bounded RFC 9457 response without rejected input or provider bodies."""

    resolved_code = code or _STATUS_CODES.get(status, f"http_{status}")
    if title is None:
        try:
            title = HTTPStatus(status).phrase
        except ValueError:
            title = "Request failed"
    payload: dict[str, object] = {
        "type": f"https://folio.local/problems/{resolved_code.replace('_', '-')}",
        "title": title,
        "status": status,
        "detail": detail,
        "code": resolved_code,
        "retryable": status in _RETRYABLE_STATUSES if retryable is None else retryable,
    }
    if instance:
        payload["instance"] = instance
    if errors:
        payload["errors"] = errors
    if extensions:
        payload.update(dict(extensions))
    return payload


def problem_response(
    request: Request,
    *,
    status: int,
    detail: str,
    title: str | None = None,
    code: str | None = None,
    retryable: bool | None = None,
    errors: list[Mapping[str, object]] | None = None,
    extensions: Mapping[str, object] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content=problem_payload(
            status=status,
            detail=detail,
            title=title,
            code=code,
            retryable=retryable,
            instance=request.url.path,
            errors=errors,
            extensions=extensions,
        ),
        headers=dict(headers) if headers else None,
    )


def _safe_http_detail(
    value: object,
) -> tuple[str, str | None, bool | None, dict[str, object]]:
    if isinstance(value, str):
        return value, None, None, {}
    if not isinstance(value, Mapping):
        return "The request failed.", None, None, {}

    message_value = value.get("message")
    detail_value = value.get("detail")
    detail = (
        message_value
        if isinstance(message_value, str) and message_value.strip()
        else detail_value
        if isinstance(detail_value, str) and detail_value.strip()
        else "The request failed."
    )
    code_value = value.get("code")
    code = code_value if isinstance(code_value, str) and code_value.strip() else None
    retryable_value = value.get("retryable")
    retryable = retryable_value if isinstance(retryable_value, bool) else None
    extensions: dict[str, object] = {}
    provider = value.get("provider")
    if isinstance(provider, str) and provider.strip():
        extensions["provider"] = provider
    return detail, code, retryable, extensions


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_problem(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        errors: list[Mapping[str, object]] = []
        for item in error.errors():
            errors.append(
                {
                    "location": [str(part) for part in item.get("loc", ())],
                    "message": str(item.get("msg", "Invalid value")),
                    "kind": str(item.get("type", "validation_error")),
                }
            )
        return problem_response(
            request,
            status=422,
            detail="The request did not match the closed API contract.",
            code="validation_failed",
            retryable=False,
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_problem(
        request: Request,
        error: StarletteHTTPException,
    ) -> JSONResponse:
        detail, code, retryable, extensions = _safe_http_detail(error.detail)
        return problem_response(
            request,
            status=error.status_code,
            detail=detail,
            code=code,
            retryable=retryable,
            extensions=extensions,
            headers=error.headers,
        )

    @app.exception_handler(KeyError)
    async def missing_resource(request: Request, _: KeyError) -> JSONResponse:
        return problem_response(
            request,
            status=404,
            detail="The requested local resource was not found.",
            code="not_found",
            retryable=False,
        )


__all__ = ["install_problem_handlers", "problem_payload", "problem_response"]
