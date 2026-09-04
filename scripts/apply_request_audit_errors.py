from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    content = read(path)
    count = content.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:120]!r}")
    write(path, content.replace(old, new, 1))


def insert_once(path: str, marker: str, addition: str, *, before: bool = False) -> None:
    content = read(path)
    count = content.count(marker)
    if count != 1:
        raise RuntimeError(f"{path}: expected one marker, found {count}: {marker[:120]!r}")
    replacement = addition + marker if before else marker + addition
    write(path, content.replace(marker, replacement, 1))


def replace_class(path: str, name: str, replacement: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    candidate = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name),
        None,
    )
    if candidate is None or candidate.end_lineno is None:
        raise RuntimeError(f"{path}: class {name} not found")
    lines = content.splitlines(keepends=True)
    start = candidate.lineno - 1
    while start > 0 and lines[start - 1].lstrip().startswith("@"):
        start -= 1
    write(
        path,
        "".join(lines[:start])
        + replacement.rstrip()
        + "\n\n"
        + "".join(lines[candidate.end_lineno :]),
    )


def insert_method_before(path: str, class_name: str, before_name: str, method: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    before = next(
        node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


CONNECTOR_ERROR = '''class ConnectorError(RuntimeError):
    """Typed provider failure suitable for stable problem responses."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "provider_failure",
        status_code: int = 502,
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.retryable = retryable

    @classmethod
    def unconfigured(cls, provider: str) -> "ConnectorError":
        return cls(
            f"{provider.title()} is disabled or unconfigured",
            code="provider_unconfigured",
            status_code=409,
            retryable=False,
        )

    @classmethod
    def invalid_request(cls, message: str) -> "ConnectorError":
        return cls(
            message,
            code="provider_request_invalid",
            status_code=422,
            retryable=False,
        )

    def as_problem(self, *, request_id: str | None = None) -> dict[str, object]:
        value: dict[str, object] = {
            "type": f"https://folio.local/problems/{self.code}",
            "title": self.code.replace("_", " ").title(),
            "status": self.status_code,
            "detail": str(self),
            "code": self.code,
            "retryable": self.retryable,
        }
        if request_id:
            value["requestId"] = request_id
        return value
'''

REQUEST_CONTEXT = '''"""Request identity and origin receipts for the loopback API."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import UTC, datetime
from uuid import uuid4

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_PATTERN = re.compile(r"^req_[A-Za-z0-9_-]{8,96}$")
CLIENT_ORIGINS = frozenset({"desktop", "cli", "automation", "unknown"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
AuditSink = Callable[..., Awaitable[None]]


def _now() -> str:
    return datetime.now(UTC).isoformat()


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, audit: AuditSink) -> None:
        self.app = app
        self.audit = audit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        supplied_id = headers.get("x-request-id")
        if supplied_id is not None and not REQUEST_ID_PATTERN.fullmatch(supplied_id):
            response = JSONResponse(
                status_code=400,
                media_type="application/problem+json",
                content={
                    "type": "https://folio.local/problems/invalid_request_id",
                    "title": "Invalid Request Id",
                    "status": 400,
                    "detail": "X-Request-ID must use the req_ prefix and safe characters.",
                    "code": "invalid_request_id",
                    "retryable": False,
                },
            )
            await response(scope, receive, send)
            return
        request_id = supplied_id or f"req_{uuid4().hex}"
        raw_client = (headers.get("x-folio-client") or "unknown").strip().lower()
        client_origin = raw_client if raw_client in CLIENT_ORIGINS else "unknown"
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["client_origin"] = client_origin
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        started_at = _now()
        is_mutation = method in UNSAFE_METHODS and path.startswith("/v1/")
        if is_mutation:
            await self.audit(
                request_id=request_id,
                method=method,
                path=path,
                client_origin=client_origin,
                started_at=started_at,
                status_code=None,
                completed_at=None,
            )

        async def contextual_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                if is_mutation:
                    with suppress(Exception):
                        await self.audit(
                            request_id=request_id,
                            method=method,
                            path=path,
                            client_origin=client_origin,
                            started_at=started_at,
                            status_code=status_code,
                            completed_at=_now(),
                        )
                mutable = MutableHeaders(scope=message)
                mutable["X-Request-ID"] = request_id
                mutable["X-Folio-Client"] = client_origin
            await send(message)

        await self.app(scope, receive, contextual_send)
'''

AUDIT_METHOD = '''    async def record_request_audit(
        self,
        *,
        request_id: str,
        method: str,
        path: str,
        client_origin: str,
        started_at: str,
        status_code: int | None,
        completed_at: str | None,
    ) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO request_audit_events(
                    request_id, workspace_id, method, path, client_origin,
                    started_at, status_code, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    status_code = excluded.status_code,
                    completed_at = excluded.completed_at
                """,
                (
                    request_id,
                    WORKSPACE_ID,
                    method,
                    path,
                    client_origin,
                    started_at,
                    status_code,
                    completed_at,
                ),
            )
'''

MIGRATION_SQL = '''    Migration(
        version={version},
        name="request_origin_audit",
        sql="""
        CREATE TABLE request_audit_events (
            request_id TEXT PRIMARY KEY,
            workspace_id TEXT REFERENCES workspaces(workspace_id),
            method TEXT NOT NULL CHECK (method IN ('POST', 'PUT', 'PATCH', 'DELETE')),
            path TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 500),
            client_origin TEXT NOT NULL
                CHECK (client_origin IN ('desktop', 'cli', 'automation', 'unknown')),
            started_at TEXT NOT NULL,
            status_code INTEGER CHECK (
                status_code IS NULL OR (status_code >= 100 AND status_code <= 599)
            ),
            completed_at TEXT
        );

        CREATE INDEX request_audit_workspace_time
            ON request_audit_events(workspace_id, started_at, request_id);
        """,
    ),
'''

TESTS = '''from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from finance_agent.api.app import create_app
from finance_agent.connectors.base import ConnectorError
from finance_agent.storage import SQLiteStore


@pytest.mark.asyncio
async def test_unconfigured_connector_returns_typed_problem(tmp_path: Path) -> None:
    app = create_app(
        database_path=tmp_path / "folio.sqlite3",
        development_routes=True,
        session_token=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/connectors/plaid/link-token",
            headers={
                "X-Request-ID": "req_connector_unconfigured",
                "X-Folio-Client": "cli",
            },
        )
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "https://folio.local/problems/provider_unconfigured",
        "title": "Provider Unconfigured",
        "status": 409,
        "detail": "Plaid is disabled or unconfigured",
        "code": "provider_unconfigured",
        "retryable": False,
        "requestId": "req_connector_unconfigured",
    }


def test_connector_error_defaults_remain_backwards_compatible() -> None:
    error = ConnectorError("temporary provider failure")
    assert error.code == "provider_failure"
    assert error.status_code == 502
    assert error.retryable is True


@pytest.mark.asyncio
async def test_mutation_request_id_and_client_origin_are_persisted(tmp_path: Path) -> None:
    database = tmp_path / "folio.sqlite3"
    app = create_app(
        database_path=database,
        development_routes=True,
        session_token=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/demo/reset",
            json={"workspaceId": "ws_koru_studio"},
            headers={
                "X-Request-ID": "req_audit_origin_123",
                "X-Folio-Client": "desktop",
            },
        )
    assert response.status_code == 200
    assert response.headers["x-request-id"] == "req_audit_origin_123"
    assert response.headers["x-folio-client"] == "desktop"
    row = SQLiteStore(database).fetch_one(
        "SELECT * FROM request_audit_events WHERE request_id = ?",
        ("req_audit_origin_123",),
    )
    assert row is not None
    assert str(row["method"]) == "POST"
    assert str(row["path"]) == "/v1/demo/reset"
    assert str(row["client_origin"]) == "desktop"
    assert int(row["status_code"]) == 200
    assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_rejected_authenticated_mutation_still_has_origin_receipt(tmp_path: Path) -> None:
    database = tmp_path / "folio.sqlite3"
    app = create_app(
        database_path=database,
        development_routes=True,
        session_token="secret",
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/demo/reset",
            json={"workspaceId": "ws_koru_studio"},
            headers={"X-Request-ID": "req_rejected_mutation"},
        )
    assert response.status_code == 401
    row = SQLiteStore(database).fetch_one(
        "SELECT * FROM request_audit_events WHERE request_id = ?",
        ("req_rejected_mutation",),
    )
    assert row is not None
    assert int(row["status_code"]) == 401
    assert str(row["client_origin"]) == "unknown"


@pytest.mark.asyncio
async def test_invalid_request_id_fails_before_mutation(tmp_path: Path) -> None:
    database = tmp_path / "folio.sqlite3"
    app = create_app(
        database_path=database,
        development_routes=True,
        session_token=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/demo/reset",
            json={"workspaceId": "ws_koru_studio"},
            headers={"X-Request-ID": "bad id with spaces"},
        )
    assert response.status_code == 400
    assert SQLiteStore(database).fetch_all("SELECT * FROM request_audit_events") == []
'''


def type_connector_errors() -> None:
    path = "services/api/src/finance_agent/connectors/base.py"
    replace_class(path, "ConnectorError", CONNECTOR_ERROR)

    path = "services/api/src/finance_agent/connectors/akahu.py"
    content = read(path).replace(
        'raise ConnectorError("Akahu is disabled or unconfigured")',
        'raise ConnectorError.unconfigured("akahu")',
    )
    write(path, content)

    path = "services/api/src/finance_agent/connectors/plaid.py"
    content = read(path).replace(
        'raise ConnectorError("Plaid is disabled or unconfigured")',
        'raise ConnectorError.unconfigured("plaid")',
    )
    content = content.replace(
        'raise ConnectorError(\n            "Plaid sync requires PLAID_ACCESS_TOKEN or a Link public_token"\n        )',
        'raise ConnectorError(\n            "Plaid sync requires PLAID_ACCESS_TOKEN or a Link public_token",\n            code="provider_credentials_required",\n            status_code=409,\n            retryable=False,\n        )',
    )
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    patterns = (
        (
            r'except ConnectorError as exc:\n\s+status = 409 if str\(exc\) == "Akahu is disabled or unconfigured" else 502\n\s+raise HTTPException\(status_code=status, detail=str\(exc\)\) from exc',
            'except ConnectorError:\n            raise',
        ),
        (
            r'except ConnectorError as exc:\n\s+status = 409 if "disabled or unconfigured" in str\(exc\) else 502\n\s+raise HTTPException\(status_code=status, detail=str\(exc\)\) from exc',
            'except ConnectorError:\n            raise',
        ),
    )
    for pattern, replacement in patterns:
        content, count = re.subn(pattern, replacement, content)
        if count == 0:
            raise RuntimeError(f"router connector exception pattern not found: {pattern}")
    write(path, content)


def add_request_audit_migration() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    content = read(path)
    versions = [int(value) for value in re.findall(r"version=(\d+)", content)]
    version = max(versions) + 1
    closing = content.rfind("\n)")
    if closing < 0:
        raise RuntimeError("could not find MIGRATIONS tuple close")
    prefix = content[:closing].rstrip()
    if not prefix.endswith(","):
        prefix += ","
    write(path, prefix + "\n" + MIGRATION_SQL.format(version=version) + content[closing:])


def add_request_context() -> None:
    write("services/api/src/finance_agent/api/request_context.py", REQUEST_CONTEXT)
    path = "services/api/src/finance_agent/api/services.py"
    insert_method_before(path, "LocalRouteServices", "health", AUDIT_METHOD)

    path = "services/api/src/finance_agent/api/app.py"
    content = read(path)
    if "from fastapi import FastAPI, Request" not in content:
        content = content.replace("from fastapi import FastAPI\n", "from fastapi import FastAPI, Request\n", 1)
    if "from fastapi.responses import JSONResponse" not in content:
        content = content.replace(
            "from fastapi.middleware.trustedhost import TrustedHostMiddleware\n",
            "from fastapi.middleware.trustedhost import TrustedHostMiddleware\nfrom fastapi.responses import JSONResponse\n",
            1,
        )
    if "from finance_agent.api.request_context import RequestContextMiddleware" not in content:
        content = content.replace(
            "from finance_agent.api.routes import create_router\n",
            "from finance_agent.api.request_context import RequestContextMiddleware\nfrom finance_agent.api.routes import create_router\n",
            1,
        )
    if "from finance_agent.connectors.base import ConnectorError" not in content:
        content = content.replace(
            "from finance_agent.api.services import LocalRouteServices\n",
            "from finance_agent.api.services import LocalRouteServices\nfrom finance_agent.connectors.base import ConnectorError\n",
            1,
        )
    handler = '''\n\nasync def connector_error_response(\n    request: Request, exc: ConnectorError\n) -> JSONResponse:\n    request_id = getattr(request.state, "request_id", None)\n    return JSONResponse(\n        status_code=exc.status_code,\n        media_type="application/problem+json",\n        content=exc.as_problem(request_id=request_id),\n    )\n'''
    marker = "\n\ndef create_app("
    if "async def connector_error_response" not in content:
        if marker not in content:
            raise RuntimeError("create_app marker missing")
        content = content.replace(marker, handler + marker, 1)
    if "value.add_exception_handler(ConnectorError" not in content:
        content = content.replace(
            "    value.include_router(create_router())\n",
            "    value.add_exception_handler(ConnectorError, connector_error_response)\n"
            "    value.include_router(create_router())\n",
            1,
        )
    if "RequestContextMiddleware" not in content.split("value.include_router", 1)[1]:
        content = content.replace(
            "    value.state.session_auth_required = bool(\n",
            "    value.add_middleware(\n"
            "        RequestContextMiddleware, audit=services.record_request_audit\n"
            "    )\n"
            "    value.state.session_auth_required = bool(\n",
            1,
        )
    write(path, content)

    path = "apps/desktop/src/transport.ts"
    content = read(path)
    if '"X-Folio-Client": "desktop"' not in content:
        content = content.replace(
            'const sessionHeaders = (): Record<string, string> => SESSION_TOKEN\n  ? { "X-Folio-Session": SESSION_TOKEN }\n  : {};',
            'const sessionHeaders = (): Record<string, string> => ({\n  "X-Folio-Client": window.financeDesktop ? "desktop" : "cli",\n  ...(SESSION_TOKEN ? { "X-Folio-Session": SESSION_TOKEN } : {}),\n});',
            1,
        )
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/api/test_connector_errors_and_request_audit.py", TESTS)
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 3: typed failures and mutation-origin receipts\n\n- Connector failures now carry stable codes, HTTP status, and retryability.\n- Provider failures render as `application/problem+json` with the current request ID.\n- Every unsafe `/v1` request receives a validated request ID and client-origin label.\n- Mutation attempts and their final HTTP status are persisted without request bodies or secrets.\n- Rejected authenticated mutations are retained as audit evidence.\n'''
    if "## Stack 3: typed failures and mutation-origin receipts" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    type_connector_errors()
    add_request_audit_migration()
    add_request_context()
    add_tests_and_docs()
    print("request audit and typed connector changes applied")


if __name__ == "__main__":
    main()
