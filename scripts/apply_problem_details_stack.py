from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("FOLIO_ROOT", Path.cwd())).resolve()


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


write(
    "services/api/src/finance_agent/api/problems.py",
    '''"""RFC 9457 problem details for the local API boundary."""

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
    code: str | None = None,
    retryable: bool | None = None,
    instance: str | None = None,
    errors: list[Mapping[str, object]] | None = None,
    extensions: Mapping[str, object] | None = None,
) -> dict[str, object]:
    resolved_code = code or _STATUS_CODES.get(status, f"http_{status}")
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
    code: str | None = None,
    retryable: bool | None = None,
    errors: list[Mapping[str, object]] | None = None,
    extensions: Mapping[str, object] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content=problem_payload(
            status=status,
            detail=detail,
            code=code,
            retryable=retryable,
            instance=request.url.path,
            errors=errors,
            extensions=extensions,
        ),
    )


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
        detail = error.detail if isinstance(error.detail, str) else "The request failed."
        return problem_response(
            request,
            status=error.status_code,
            detail=detail,
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
''',
)

app_path = "services/api/src/finance_agent/api/app.py"
content = read(app_path)
if "from finance_agent.api.problems import install_problem_handlers" not in content:
    anchor = "from finance_agent.api.routes import create_router\n"
    if anchor not in content:
        raise RuntimeError("FastAPI route import anchor is missing")
    content = content.replace(
        anchor,
        "from finance_agent.api.problems import install_problem_handlers\n" + anchor,
        1,
    )
if "    install_problem_handlers(value)\n" not in content:
    anchor = "    value.add_middleware(\n"
    position = content.find(anchor)
    if position < 0:
        raise RuntimeError("FastAPI middleware anchor is missing")
    content = content[:position] + "    install_problem_handlers(value)\n" + content[position:]
write(app_path, content)

security_path = "services/api/src/finance_agent/api/http_security.py"
content = read(security_path)
if "from finance_agent.api.problems import problem_payload" not in content:
    anchor = "from fastapi import UploadFile\n"
    if anchor not in content:
        raise RuntimeError("HTTP security import anchor is missing")
    content = content.replace(
        anchor,
        anchor + "\nfrom finance_agent.api.problems import problem_payload\n",
        1,
    )
content, count = re.subn(
    r'content=\{"detail": detail\},',
    'content=problem_payload(status=status_code, detail=detail),',
    content,
    count=1,
)
if count != 1 and "content=problem_payload(status=status_code, detail=detail)," not in content:
    raise RuntimeError("HTTP middleware problem body anchor is missing")
write(security_path, content)

router_path = "services/api/src/finance_agent/api/routes/router.py"
content = read(router_path)
old = '''                    "status": 409,
                    "detail": str(gap),
                    "runId": gap.run_id,
'''
new = '''                    "status": 409,
                    "detail": str(gap),
                    "code": "run_event_sequence_gap",
                    "retryable": True,
                    "runId": gap.run_id,
'''
if old in content:
    content = content.replace(old, new, 1)
elif '"code": "run_event_sequence_gap"' not in content:
    raise RuntimeError("Sequence-gap problem response anchor is missing")
write(router_path, content)

write(
    "services/api/tests/api/test_problem_details.py",
    '''from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from finance_agent.api.app import create_app


@pytest.mark.asyncio
async def test_validation_errors_use_problem_details_without_echoing_input(
    tmp_path: Path,
) -> None:
    app = create_app(database_path=tmp_path / "validation.sqlite3", auto_seed=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/jobs/daily-close",
            json={"secret": "do-not-echo"},
        )
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
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        workspace = await client.get("/v1/workspaces/ws_missing/snapshot")
        route = await client.get("/does-not-exist")
    await app.state.finance_route_services.aclose()

    for response in (workspace, route):
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["code"] == "not_found"
        assert response.json()["retryable"] is False
''',
)

print("Applied RFC 9457 problem-details stack")
