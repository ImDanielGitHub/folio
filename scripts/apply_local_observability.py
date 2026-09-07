from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


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


MIGRATION = '''    Migration(
        version={version},
        name="local_operation_metrics",
        sql="""
        CREATE TABLE operation_metrics (
            operation_id TEXT PRIMARY KEY,
            workspace_id TEXT REFERENCES workspaces(workspace_id),
            category TEXT NOT NULL CHECK (category IN (
                'api', 'database', 'job', 'model', 'connector', 'artifact', 'system'
            )),
            operation TEXT NOT NULL CHECK (length(operation) BETWEEN 1 AND 240),
            started_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
            status TEXT NOT NULL CHECK (status IN ('completed', 'failed', 'cancelled')),
            request_id TEXT,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX operation_metrics_workspace_time
            ON operation_metrics(workspace_id, created_at, operation_id);
        CREATE INDEX operation_metrics_slow
            ON operation_metrics(category, operation, duration_ms DESC, created_at);
        """,
    ),
'''

OBSERVABILITY = '''"""Local-only, redacted operational measurements for Folio."""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from finance_agent.storage import SQLiteStore, canonical_json

MAX_METADATA_ITEMS = 32
MAX_METADATA_TEXT = 200
SENSITIVE_MARKERS = (
    "token", "secret", "password", "apikey", "api_key", "authorization",
    "cookie", "content", "body", "prompt", "statement", "document", "source",
    "description", "filename", "account", "email", "phone", "address",
)
MetricSink = Callable[..., Awaitable[None]]


def _now() -> datetime:
    return datetime.now(UTC)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _normalise_key(value: object) -> str:
    return "".join(character for character in str(value).lower() if character.isalnum() or character == "_")


def sanitise_metadata(value: Mapping[str, object] | None) -> dict[str, object]:
    if not value:
        return {}
    result: dict[str, object] = {}
    for index, (raw_key, raw_value) in enumerate(sorted(value.items(), key=lambda item: str(item[0]))):
        if index >= MAX_METADATA_ITEMS:
            result["truncated"] = True
            break
        key = str(raw_key)[:80]
        normalised = _normalise_key(key)
        if any(marker in normalised for marker in SENSITIVE_MARKERS):
            result[key] = "[redacted]"
            continue
        if raw_value is None or isinstance(raw_value, bool | int | float):
            result[key] = raw_value
        elif isinstance(raw_value, str):
            result[key] = raw_value[:MAX_METADATA_TEXT]
        elif isinstance(raw_value, Sequence) and not isinstance(raw_value, str | bytes | bytearray):
            result[key] = len(raw_value)
        elif isinstance(raw_value, Mapping):
            result[key] = {"itemCount": len(raw_value)}
        else:
            result[key] = type(raw_value).__name__
    return result


@dataclass(frozen=True, slots=True)
class OperationSummary:
    operation_count: int
    failed_count: int
    p50_ms: int
    p95_ms: int
    maximum_ms: int
    slow_operations: tuple[dict[str, object], ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "operationCount": self.operation_count,
            "failedCount": self.failed_count,
            "p50Ms": self.p50_ms,
            "p95Ms": self.p95_ms,
            "maximumMs": self.maximum_ms,
            "slowOperations": list(self.slow_operations),
        }


class LocalOperationMetrics:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def record(
        self,
        *,
        category: str,
        operation: str,
        started_at: str,
        duration_ms: int,
        status: str,
        workspace_id: str | None = None,
        request_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> str:
        if category not in {
            "api", "database", "job", "model", "connector", "artifact", "system"
        }:
            raise ValueError("unsupported operation category")
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("unsupported operation status")
        if duration_ms < 0:
            raise ValueError("duration must be non-negative")
        bounded_operation = operation.strip()[:240]
        if not bounded_operation:
            raise ValueError("operation must not be blank")
        metadata_value = sanitise_metadata(metadata)
        created_at = _now().isoformat()
        operation_id = _stable_id(
            "opmetric",
            category,
            bounded_operation,
            started_at,
            request_id or "none",
            str(duration_ms),
        )
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO operation_metrics(
                    operation_id, workspace_id, category, operation, started_at,
                    duration_ms, status, request_id, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(operation_id) DO NOTHING
                """,
                (
                    operation_id,
                    workspace_id,
                    category,
                    bounded_operation,
                    started_at,
                    int(duration_ms),
                    status,
                    request_id,
                    canonical_json(metadata_value),
                    created_at,
                ),
            )
        return operation_id

    @contextmanager
    def measure(
        self,
        category: str,
        operation: str,
        *,
        workspace_id: str | None = None,
        request_id: str | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> Iterator[None]:
        started = _now()
        monotonic = time.monotonic()
        status = "completed"
        failure: dict[str, object] = {}
        try:
            yield
        except BaseException as exc:
            status = "cancelled" if type(exc).__name__ == "CancelledError" else "failed"
            failure = {"errorType": type(exc).__name__}
            raise
        finally:
            duration_ms = max(0, int((time.monotonic() - monotonic) * 1000))
            self.record(
                category=category,
                operation=operation,
                started_at=started.isoformat(),
                duration_ms=duration_ms,
                status=status,
                workspace_id=workspace_id,
                request_id=request_id,
                metadata={**sanitise_metadata(metadata), **failure},
            )

    def summary(
        self,
        *,
        workspace_id: str | None,
        since_hours: int = 24,
        slow_limit: int = 20,
    ) -> OperationSummary:
        if not 1 <= since_hours <= 24 * 90:
            raise ValueError("sinceHours must be between 1 and 2160")
        if not 1 <= slow_limit <= 100:
            raise ValueError("slowLimit must be between 1 and 100")
        since = (_now() - timedelta(hours=since_hours)).isoformat()
        if workspace_id is None:
            rows = self.store.fetch_all(
                """
                SELECT category, operation, duration_ms, status, request_id,
                       metadata_json, started_at
                FROM operation_metrics WHERE created_at >= ?
                ORDER BY duration_ms DESC, created_at DESC
                """,
                (since,),
            )
        else:
            rows = self.store.fetch_all(
                """
                SELECT category, operation, duration_ms, status, request_id,
                       metadata_json, started_at
                FROM operation_metrics
                WHERE created_at >= ? AND (workspace_id = ? OR workspace_id IS NULL)
                ORDER BY duration_ms DESC, created_at DESC
                """,
                (since, workspace_id),
            )
        durations = sorted(int(row["duration_ms"]) for row in rows)

        def percentile(fraction: float) -> int:
            if not durations:
                return 0
            index = min(len(durations) - 1, max(0, math.ceil(len(durations) * fraction) - 1))
            return durations[index]

        slow = tuple(
            {
                "category": str(row["category"]),
                "operation": str(row["operation"]),
                "durationMs": int(row["duration_ms"]),
                "status": str(row["status"]),
                "requestId": str(row["request_id"]) if row["request_id"] else None,
                "metadata": json.loads(str(row["metadata_json"])),
                "startedAt": str(row["started_at"]),
            }
            for row in rows[:slow_limit]
        )
        return OperationSummary(
            operation_count=len(rows),
            failed_count=sum(str(row["status"]) == "failed" for row in rows),
            p50_ms=percentile(0.50),
            p95_ms=percentile(0.95),
            maximum_ms=max(durations, default=0),
            slow_operations=slow,
        )


class OperationTelemetryMiddleware:
    def __init__(self, app: ASGIApp, *, sink: MetricSink) -> None:
        self.app = app
        self.sink = sink

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))[:200]
        started = _now()
        monotonic = time.monotonic()
        status_code = 500
        outcome = "completed"
        error_type: str | None = None

        async def tracked_send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, tracked_send)
            if status_code >= 500:
                outcome = "failed"
        except BaseException as exc:
            outcome = "cancelled" if type(exc).__name__ == "CancelledError" else "failed"
            error_type = type(exc).__name__
            raise
        finally:
            state = scope.get("state") or {}
            duration_ms = max(0, int((time.monotonic() - monotonic) * 1000))
            await self.sink(
                category="api",
                operation=f"{method} {path}",
                started_at=started.isoformat(),
                duration_ms=duration_ms,
                status=outcome,
                workspace_id=state.get("workspace_id"),
                request_id=state.get("request_id"),
                metadata={
                    "statusCode": status_code,
                    "clientOrigin": state.get("client_origin", "unknown"),
                    **({"errorType": error_type} if error_type else {}),
                },
            )
'''

SERVICE_METHODS = '''    async def record_operation_metric(
        self,
        *,
        category: str,
        operation: str,
        started_at: str,
        duration_ms: int,
        status: str,
        workspace_id: str | None,
        request_id: str | None,
        metadata: Mapping[str, object] | None,
    ) -> None:
        LocalOperationMetrics(self.store).record(
            category=category,
            operation=operation,
            started_at=started_at,
            duration_ms=duration_ms,
            status=status,
            workspace_id=workspace_id,
            request_id=request_id,
            metadata=metadata,
        )

    async def operation_metrics_summary(
        self,
        *,
        workspace_id: str | None,
        since_hours: int,
        slow_limit: int,
    ) -> Mapping[str, object]:
        return LocalOperationMetrics(self.store).summary(
            workspace_id=workspace_id,
            since_hours=since_hours,
            slow_limit=slow_limit,
        ).as_dict()
'''

ROUTE = '''    @router.get(
        "/v1/diagnostics/operations",
        dependencies=[Depends(require_development_routes)],
    )
    async def operation_metrics_summary(
        services: Services,
        workspace_id: Annotated[str | None, Query(alias="workspaceId")] = None,
        since_hours: Annotated[int, Query(alias="sinceHours", ge=1, le=2160)] = 24,
        slow_limit: Annotated[int, Query(alias="slowLimit", ge=1, le=100)] = 20,
    ) -> dict[str, object]:
        return dict(
            await services.operation_metrics_summary(
                workspace_id=workspace_id,
                since_hours=since_hours,
                slow_limit=slow_limit,
            )
        )

'''

PERFORMANCE_SCRIPT = '''from __future__ import annotations

import json
import statistics
import tempfile
import time
from pathlib import Path

from finance_agent.api.services import LocalRouteServices

SNAPSHOT_ITERATIONS = 80
P95_BUDGET_MS = 250.0
MAX_SNAPSHOT_BYTES = 2_000_000
MAX_DATABASE_BYTES = 100_000_000


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(len(ordered) * fraction + 0.999999) - 1))
    return ordered[index]


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="folio-performance-") as directory:
        database = Path(directory) / "folio.sqlite3"
        services = LocalRouteServices(database, auto_seed=True)
        timings: list[float] = []
        snapshot = None
        for _ in range(SNAPSHOT_ITERATIONS):
            started = time.perf_counter()
            snapshot = services.workspace_snapshot_sync("ws_koru_studio")
            timings.append((time.perf_counter() - started) * 1000)
        assert snapshot is not None
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode()
        p95 = percentile(timings, 0.95)
        result = {
            "iterations": SNAPSHOT_ITERATIONS,
            "medianMs": round(statistics.median(timings), 3),
            "p95Ms": round(p95, 3),
            "maximumMs": round(max(timings), 3),
            "snapshotBytes": len(encoded),
            "databaseBytes": database.stat().st_size,
            "p95BudgetMs": P95_BUDGET_MS,
        }
        if p95 > P95_BUDGET_MS:
            raise AssertionError(f"snapshot p95 {p95:.3f}ms exceeds {P95_BUDGET_MS:.1f}ms")
        if len(encoded) > MAX_SNAPSHOT_BYTES:
            raise AssertionError("workspace snapshot exceeds the payload budget")
        if database.stat().st_size > MAX_DATABASE_BYTES:
            raise AssertionError("seeded database exceeds the size budget")
        print(json.dumps({"status": "PASS", **result}, indent=2))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

TESTS = '''from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from finance_agent.api.app import create_app
from finance_agent.finance import FinanceEngine
from finance_agent.observability import LocalOperationMetrics, sanitise_metadata
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def test_metadata_redaction_never_persists_sensitive_values() -> None:
    value = sanitise_metadata(
        {
            "statusCode": 200,
            "apiToken": "secret-value",
            "requestBody": "bank contents",
            "ownerStatement": "private text",
            "safeLabel": "daily-close",
            "rows": [1, 2, 3],
        }
    )
    assert value["statusCode"] == 200
    assert value["apiToken"] == "[redacted]"
    assert value["requestBody"] == "[redacted]"
    assert value["ownerStatement"] == "[redacted]"
    assert value["safeLabel"] == "daily-close"
    assert value["rows"] == 3


def test_metric_context_records_success_and_failure_without_messages(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    metrics = LocalOperationMetrics(store)
    with metrics.measure(
        "job",
        "daily_close",
        workspace_id="ws_koru_studio",
        metadata={"documentContent": "never store me", "stageCount": 10},
    ):
        pass
    with pytest.raises(RuntimeError):
        with metrics.measure("system", "failing-operation"):
            raise RuntimeError("private failure text")
    rows = store.fetch_all("SELECT * FROM operation_metrics ORDER BY created_at")
    assert [row["status"] for row in rows] == ["completed", "failed"]
    metadata = json.loads(str(rows[0]["metadata_json"]))
    assert metadata["documentContent"] == "[redacted]"
    assert "private failure text" not in " ".join(str(row["metadata_json"]) for row in rows)


@pytest.mark.asyncio
async def test_api_middleware_records_request_timing_and_request_id(tmp_path: Path) -> None:
    database = tmp_path / "folio.sqlite3"
    app = create_app(
        database_path=database,
        development_routes=True,
        session_token=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/health",
            headers={"X-Request-ID": "req_metric_health_123", "X-Folio-Client": "automation"},
        )
        assert response.status_code == 200
        diagnostics = await client.get(
            "/v1/diagnostics/operations",
            params={"sinceHours": 1},
        )
    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert payload["operationCount"] >= 1
    health_rows = [
        value for value in payload["slowOperations"]
        if value["operation"] == "GET /health"
    ]
    assert health_rows
    assert health_rows[0]["requestId"] == "req_metric_health_123"
    assert health_rows[0]["metadata"]["clientOrigin"] == "automation"
'''


def add_migration() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    content = read(path)
    versions = [int(value) for value in re.findall(r"version=(\d+)", content)]
    version = max(versions) + 1
    closing = content.rfind("\n)")
    if closing < 0:
        raise RuntimeError("MIGRATIONS tuple close not found")
    prefix = content[:closing].rstrip()
    if not prefix.endswith(","):
        prefix += ","
    write(path, prefix + "\n" + MIGRATION.format(version=version) + content[closing:])


def add_observability_module() -> None:
    write("services/api/src/finance_agent/observability.py", OBSERVABILITY)
    write("scripts/performance_budget.py", PERFORMANCE_SCRIPT)
    write("services/api/tests/api/test_local_observability.py", TESTS)


def update_services_and_app() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.models.router import ModelModeRouter\n"
    import_line = "from finance_agent.observability import LocalOperationMetrics\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("model router import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "reconciliation_report", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/app.py"
    content = read(path)
    marker = "from finance_agent.api.request_context import RequestContextMiddleware\n"
    import_line = "from finance_agent.observability import OperationTelemetryMiddleware\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("request context import marker missing")
        content = content.replace(marker, marker + import_line, 1)
    state_marker = "    value.state.finance_route_services = services\n"
    middleware = (
        "    value.add_middleware(\n"
        "        OperationTelemetryMiddleware, sink=services.record_operation_metric\n"
        "    )\n"
    )
    if "OperationTelemetryMiddleware, sink=" not in content:
        if state_marker not in content:
            raise RuntimeError("app state marker missing")
        content = content.replace(state_marker, middleware + state_marker, 1)
    write(path, content)


def update_protocol_and_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def reconciliation_report(\n"
    addition = '''    async def record_operation_metric(\n        self, *, category: str, operation: str, started_at: str,\n        duration_ms: int, status: str, workspace_id: str | None,\n        request_id: str | None, metadata: Mapping[str, object] | None\n    ) -> None: ...\n\n    async def operation_metrics_summary(\n        self, *, workspace_id: str | None, since_hours: int, slow_limit: int\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("reconciliation protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    marker = '    @router.get("/v1/workspaces/{workspace_id}/reconciliation")\n'
    if marker not in content:
        raise RuntimeError("reconciliation route marker missing")
    content = content.replace(marker, ROUTE + marker, 1)
    write(path, content)


def update_scripts_and_docs() -> None:
    path = "package.json"
    value = json.loads(read(path))
    scripts = value["scripts"]
    scripts["performance:budget"] = "uv run --project services/api python scripts/performance_budget.py"
    if "performance:budget" not in scripts["verify"]:
        scripts["verify"] += " && pnpm performance:budget"
    write(path, json.dumps(value, indent=2) + "\n")

    write("docs/OBSERVABILITY.md", '''# Local observability and performance budgets\n\nFolio records local operation timings in SQLite. It does not transmit telemetry. API metrics retain method, path, status, duration, request ID and a bounded client-origin label. They never retain request or response bodies, prompts, owner statements, source descriptions, filenames, credentials, headers or cookies. Metadata keys with sensitive markers are replaced with `[redacted]`.\n\nThe development-only operations endpoint reports counts, failures, percentiles and bounded slow-operation metadata. It is not exposed when production routes are disabled. Request timing is evidence that a route completed locally, not proof that an external provider or owner received a result.\n\n`pnpm performance:budget` creates a fresh synthetic workspace, measures repeated snapshot construction and enforces generous p95, payload-size and database-size ceilings. These budgets catch major regressions; they are not a substitute for production traces on representative owner data.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 18: local observability and performance budgets\n\n- API operations record local-only duration, status, request ID and bounded non-sensitive metadata.\n- Sensitive metadata keys and error messages are never persisted.\n- Development diagnostics expose counts, failures, percentiles and slow operations.\n- A synthetic performance gate enforces snapshot p95, payload-size and database-size budgets.\n- Observability remains local and no telemetry egress is introduced.\n- Synthetic performance evidence is not described as production-scale proof.\n'''
    if "## Stack 18: local observability and performance budgets" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration()
    add_observability_module()
    update_services_and_app()
    update_protocol_and_routes()
    update_scripts_and_docs()
    print("local observability changes applied")


if __name__ == "__main__":
    main()
