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
        name="local_scheduler",
        sql="""
        CREATE TABLE scheduler_settings (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id),
            enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
            local_time TEXT NOT NULL DEFAULT '07:30'
                CHECK (length(local_time) = 5),
            timezone TEXT NOT NULL DEFAULT 'Pacific/Auckland',
            quiet_start TEXT,
            quiet_end TEXT,
            last_run_on TEXT,
            next_run_at TEXT,
            failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
            retry_after TEXT,
            updated_at TEXT NOT NULL,
            CHECK (
                (quiet_start IS NULL AND quiet_end IS NULL)
                OR (quiet_start IS NOT NULL AND quiet_end IS NOT NULL)
            )
        );

        CREATE TABLE scheduler_leases (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id),
            lease_owner TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            acquired_at TEXT NOT NULL
        );

        CREATE TABLE scheduler_receipts (
            receipt_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            tick_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'completed', 'no_op', 'disabled', 'not_due', 'quiet_hours',
                'leased', 'retry_backoff', 'failed'
            )),
            run_id TEXT,
            detail_json TEXT NOT NULL
        );

        CREATE INDEX scheduler_receipts_workspace_time
            ON scheduler_receipts(workspace_id, tick_at, receipt_id);
        """,
    ),
'''

SCHEDULER = '''"""Opt-in local Daily Close scheduler with leases, quiet hours, and backoff."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from finance_agent.storage import SQLiteStore, canonical_json


class DailyCloseRunner(Protocol):
    def run(self, *, requested_idempotency_key: str | None = None): ...


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _parse_clock(value: str, label: str) -> time:
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must use HH:MM") from exc
    if parsed.second or parsed.microsecond:
        raise ValueError(f"{label} must use HH:MM")
    return parsed


def _in_quiet_hours(current: time, start: time, end: time) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


@dataclass(frozen=True, slots=True)
class SchedulerSettings:
    workspace_id: str
    enabled: bool
    local_time: str
    timezone: str
    quiet_start: str | None
    quiet_end: str | None
    last_run_on: str | None
    next_run_at: str | None
    failure_count: int
    retry_after: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "workspaceId": self.workspace_id,
            "enabled": self.enabled,
            "localTime": self.local_time,
            "timezone": self.timezone,
            "quietStart": self.quiet_start,
            "quietEnd": self.quiet_end,
            "lastRunOn": self.last_run_on,
            "nextRunAt": self.next_run_at,
            "failureCount": self.failure_count,
            "retryAfter": self.retry_after,
        }


@dataclass(frozen=True, slots=True)
class SchedulerTickResult:
    status: str
    tick_at: str
    run_id: str | None
    receipt_id: str
    detail: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "tickAt": self.tick_at,
            "runId": self.run_id,
            "receiptId": self.receipt_id,
            "detail": self.detail,
        }


class LocalScheduler:
    def __init__(
        self,
        store: SQLiteStore,
        daily_close: DailyCloseRunner,
        *,
        workspace_id: str,
        owner_id: str = "scheduler_local_001",
    ) -> None:
        self.store = store
        self.daily_close = daily_close
        self.workspace_id = workspace_id
        self.owner_id = owner_id
        self._ensure_settings()

    def _ensure_settings(self) -> None:
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scheduler_settings(workspace_id, updated_at)
                VALUES (?, ?) ON CONFLICT(workspace_id) DO NOTHING
                """,
                (self.workspace_id, now),
            )

    def settings(self) -> SchedulerSettings:
        row = self.store.fetch_one(
            "SELECT * FROM scheduler_settings WHERE workspace_id = ?",
            (self.workspace_id,),
        )
        if row is None:
            raise RuntimeError("scheduler settings were not initialised")
        return SchedulerSettings(
            workspace_id=str(row["workspace_id"]),
            enabled=bool(row["enabled"]),
            local_time=str(row["local_time"]),
            timezone=str(row["timezone"]),
            quiet_start=str(row["quiet_start"]) if row["quiet_start"] else None,
            quiet_end=str(row["quiet_end"]) if row["quiet_end"] else None,
            last_run_on=str(row["last_run_on"]) if row["last_run_on"] else None,
            next_run_at=str(row["next_run_at"]) if row["next_run_at"] else None,
            failure_count=int(row["failure_count"]),
            retry_after=str(row["retry_after"]) if row["retry_after"] else None,
        )

    def update_settings(
        self,
        *,
        enabled: bool,
        local_time: str,
        timezone: str,
        quiet_start: str | None,
        quiet_end: str | None,
        now: datetime | None = None,
    ) -> SchedulerSettings:
        scheduled = _parse_clock(local_time, "localTime")
        try:
            zone = ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone is not available on this system") from exc
        if (quiet_start is None) != (quiet_end is None):
            raise ValueError("quietStart and quietEnd must be supplied together")
        if quiet_start is not None and quiet_end is not None:
            _parse_clock(quiet_start, "quietStart")
            _parse_clock(quiet_end, "quietEnd")
        current = (now or datetime.now(UTC)).astimezone(zone)
        next_local = datetime.combine(current.date(), scheduled, tzinfo=zone)
        if next_local <= current:
            next_local += timedelta(days=1)
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE scheduler_settings
                SET enabled = ?, local_time = ?, timezone = ?,
                    quiet_start = ?, quiet_end = ?, next_run_at = ?, updated_at = ?
                WHERE workspace_id = ?
                """,
                (
                    int(enabled),
                    scheduled.strftime("%H:%M"),
                    timezone,
                    quiet_start,
                    quiet_end,
                    next_local.astimezone(UTC).isoformat(),
                    current.astimezone(UTC).isoformat(),
                    self.workspace_id,
                ),
            )
        return self.settings()

    def _receipt(
        self,
        *,
        status: str,
        tick_at: datetime,
        run_id: str | None,
        detail: dict[str, object],
    ) -> SchedulerTickResult:
        receipt_id = _stable_id(
            "schedrcpt", self.workspace_id, tick_at.isoformat(), status, run_id or "none"
        )
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO scheduler_receipts(
                    receipt_id, workspace_id, tick_at, status, run_id, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_id) DO NOTHING
                """,
                (
                    receipt_id,
                    self.workspace_id,
                    tick_at.isoformat(),
                    status,
                    run_id,
                    canonical_json(detail),
                ),
            )
        return SchedulerTickResult(status, tick_at.isoformat(), run_id, receipt_id, detail)

    def _acquire_lease(self, now: datetime) -> bool:
        expires = now + timedelta(minutes=5)
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM scheduler_leases WHERE workspace_id = ?",
                (self.workspace_id,),
            ).fetchone()
            if existing is not None:
                existing_expiry = datetime.fromisoformat(str(existing["expires_at"]))
                if existing_expiry > now and str(existing["lease_owner"]) != self.owner_id:
                    return False
            connection.execute(
                """
                INSERT INTO scheduler_leases(
                    workspace_id, lease_owner, expires_at, acquired_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    lease_owner = excluded.lease_owner,
                    expires_at = excluded.expires_at,
                    acquired_at = excluded.acquired_at
                """,
                (self.workspace_id, self.owner_id, expires.isoformat(), now.isoformat()),
            )
        return True

    def _release_lease(self) -> None:
        with self.store.transaction() as connection:
            connection.execute(
                """
                DELETE FROM scheduler_leases
                WHERE workspace_id = ? AND lease_owner = ?
                """,
                (self.workspace_id, self.owner_id),
            )

    def tick(self, *, now: datetime | None = None) -> SchedulerTickResult:
        tick_at = (now or datetime.now(UTC)).astimezone(UTC)
        settings = self.settings()
        if not settings.enabled:
            return self._receipt(status="disabled", tick_at=tick_at, run_id=None, detail={})
        if settings.retry_after and datetime.fromisoformat(settings.retry_after) > tick_at:
            return self._receipt(
                status="retry_backoff",
                tick_at=tick_at,
                run_id=None,
                detail={"retryAfter": settings.retry_after},
            )
        zone = ZoneInfo(settings.timezone)
        local_now = tick_at.astimezone(zone)
        local_day = local_now.date().isoformat()
        scheduled_time = _parse_clock(settings.local_time, "localTime")
        if settings.last_run_on == local_day:
            return self._receipt(status="no_op", tick_at=tick_at, run_id=None, detail={"reason": "already_ran_today"})
        if local_now.time() < scheduled_time:
            return self._receipt(status="not_due", tick_at=tick_at, run_id=None, detail={"scheduledLocalTime": settings.local_time})
        if settings.quiet_start and settings.quiet_end:
            quiet_start = _parse_clock(settings.quiet_start, "quietStart")
            quiet_end = _parse_clock(settings.quiet_end, "quietEnd")
            if _in_quiet_hours(local_now.time(), quiet_start, quiet_end):
                return self._receipt(
                    status="quiet_hours",
                    tick_at=tick_at,
                    run_id=None,
                    detail={"quietStart": settings.quiet_start, "quietEnd": settings.quiet_end},
                )
        if not self._acquire_lease(tick_at):
            return self._receipt(status="leased", tick_at=tick_at, run_id=None, detail={})
        try:
            result = self.daily_close.run(
                requested_idempotency_key=f"scheduled-daily-close:{self.workspace_id}:{local_day}"
            )
            next_local = datetime.combine(
                local_now.date() + timedelta(days=1), scheduled_time, tzinfo=zone
            )
            with self.store.transaction() as connection:
                connection.execute(
                    """
                    UPDATE scheduler_settings
                    SET last_run_on = ?, next_run_at = ?, failure_count = 0,
                        retry_after = NULL, updated_at = ?
                    WHERE workspace_id = ?
                    """,
                    (
                        local_day,
                        next_local.astimezone(UTC).isoformat(),
                        tick_at.isoformat(),
                        self.workspace_id,
                    ),
                )
            return self._receipt(
                status="completed" if result.status == "completed" else "no_op",
                tick_at=tick_at,
                run_id=result.run_id,
                detail={"dailyCloseStatus": result.status, "receiptId": result.receipt_id},
            )
        except Exception as exc:
            failure_count = settings.failure_count + 1
            delay = min(3600, 60 * (2 ** min(failure_count - 1, 6)))
            retry_after = tick_at + timedelta(seconds=delay)
            with self.store.transaction() as connection:
                connection.execute(
                    """
                    UPDATE scheduler_settings
                    SET failure_count = ?, retry_after = ?, updated_at = ?
                    WHERE workspace_id = ?
                    """,
                    (
                        failure_count,
                        retry_after.isoformat(),
                        tick_at.isoformat(),
                        self.workspace_id,
                    ),
                )
            return self._receipt(
                status="failed",
                tick_at=tick_at,
                run_id=None,
                detail={
                    "errorType": type(exc).__name__,
                    "retryAfter": retry_after.isoformat(),
                },
            )
        finally:
            self._release_lease()
'''

SERVICE_METHODS = '''    async def scheduler_settings(self) -> Mapping[str, object]:
        return self.scheduler.settings().as_dict()

    async def update_scheduler_settings(
        self,
        *,
        enabled: bool,
        local_time: str,
        timezone: str,
        quiet_start: str | None,
        quiet_end: str | None,
    ) -> Mapping[str, object]:
        async with self._scheduler_tick_lock:
            settings = await asyncio.to_thread(
                self.scheduler.update_settings,
                enabled=enabled,
                local_time=local_time,
                timezone=timezone,
                quiet_start=quiet_start,
                quiet_end=quiet_end,
            )
        return settings.as_dict()

    async def scheduler_tick(self) -> Mapping[str, object]:
        async with self._scheduler_tick_lock:
            result = await asyncio.to_thread(self.scheduler.tick)
        if result.run_id:
            daily_result = self.store.fetch_one(
                "SELECT result_json FROM job_runs WHERE run_id = ?", (result.run_id,)
            )
            if daily_result is not None:
                from finance_agent.jobs import DailyCloseResult

                raw = json.loads(str(daily_result["result_json"]))
                self._register_daily_close_events(
                    DailyCloseResult(
                        run_id=result.run_id,
                        receipt_id=str(raw.get("receiptId")),
                        snapshot_id=str(raw.get("snapshotId")),
                        status=str(raw.get("status", "completed")),
                        new_findings=0,
                        new_artifacts=0,
                        new_owner_messages=0,
                        close_turn_id=str(raw.get("closeTurnId")),
                    )
                )
        return result.as_dict()
'''

LOOP_FUNCTION = '''

async def run_scheduler_loop(
    services: LocalRouteServices,
    *,
    interval_seconds: float = 30.0,
) -> None:
    while True:
        try:
            await services.scheduler_tick()
        except Exception:
            # Tick failures are persisted by LocalScheduler; the process loop remains alive.
            pass
        await asyncio.sleep(interval_seconds)
'''

ROUTE_MODELS = '''

class SchedulerSettingsRequest(RequestModel):
    enabled: bool
    local_time: str = Field(alias="localTime", pattern=r"^\\d{2}:\\d{2}$")
    timezone: str = Field(min_length=1, max_length=100)
    quiet_start: str | None = Field(default=None, alias="quietStart", pattern=r"^\\d{2}:\\d{2}$")
    quiet_end: str | None = Field(default=None, alias="quietEnd", pattern=r"^\\d{2}:\\d{2}$")
'''

ROUTES = '''    @router.get("/v1/workspaces/{workspace_id}/scheduler")
    async def get_scheduler_settings(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        if workspace_id != "ws_koru_studio":
            raise HTTPException(status_code=404, detail="workspace not found")
        return dict(await services.scheduler_settings())

    @router.post("/v1/workspaces/{workspace_id}/scheduler")
    async def set_scheduler_settings(
        workspace_id: PathIdentifier,
        body: SchedulerSettingsRequest,
        services: Services,
    ) -> dict[str, object]:
        if workspace_id != "ws_koru_studio":
            raise HTTPException(status_code=404, detail="workspace not found")
        try:
            return dict(
                await services.update_scheduler_settings(
                    enabled=body.enabled,
                    local_time=body.local_time,
                    timezone=body.timezone,
                    quiet_start=body.quiet_start,
                    quiet_end=body.quiet_end,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/workspaces/{workspace_id}/scheduler/tick")
    async def tick_scheduler(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        if workspace_id != "ws_koru_studio":
            raise HTTPException(status_code=404, detail="workspace not found")
        return dict(await services.scheduler_tick())

'''

TESTS = '''from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.jobs.scheduler import LocalScheduler
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


class FakeClose:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[str | None] = []
        self.fail = fail

    def run(self, *, requested_idempotency_key: str | None = None):
        self.calls.append(requested_idempotency_key)
        if self.fail:
            raise RuntimeError("synthetic close failure")

        class Result:
            status = "completed"
            run_id = "run_scheduled_test"
            receipt_id = "receipt_scheduled_test"

        return Result()


def scheduler(tmp_path: Path, close: FakeClose | None = None) -> tuple[LocalScheduler, FakeClose]:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    runner = close or FakeClose()
    return LocalScheduler(store, runner, workspace_id="ws_koru_studio"), runner


def test_scheduler_is_opt_in_and_runs_only_once_per_local_day(tmp_path: Path) -> None:
    value, close = scheduler(tmp_path)
    morning = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)  # 08:00 NZ next day
    disabled = value.tick(now=morning)
    assert disabled.status == "disabled"
    assert close.calls == []
    value.update_settings(
        enabled=True,
        local_time="07:30",
        timezone="Pacific/Auckland",
        quiet_start=None,
        quiet_end=None,
        now=morning,
    )
    first = value.tick(now=morning)
    second = value.tick(now=morning)
    assert first.status == "completed"
    assert second.status == "no_op"
    assert len(close.calls) == 1
    assert close.calls[0] == "scheduled-daily-close:ws_koru_studio:2026-08-27"


def test_scheduler_respects_quiet_hours(tmp_path: Path) -> None:
    value, close = scheduler(tmp_path)
    now = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    value.update_settings(
        enabled=True,
        local_time="07:30",
        timezone="Pacific/Auckland",
        quiet_start="22:00",
        quiet_end="09:00",
        now=now,
    )
    result = value.tick(now=now)
    assert result.status == "quiet_hours"
    assert close.calls == []


def test_scheduler_persists_bounded_retry_backoff(tmp_path: Path) -> None:
    value, close = scheduler(tmp_path, FakeClose(fail=True))
    now = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    value.update_settings(
        enabled=True,
        local_time="07:30",
        timezone="Pacific/Auckland",
        quiet_start=None,
        quiet_end=None,
        now=now,
    )
    failed = value.tick(now=now)
    retry = value.tick(now=now)
    assert failed.status == "failed"
    assert retry.status == "retry_backoff"
    assert value.settings().failure_count == 1
    assert len(close.calls) == 1


def test_scheduler_lease_blocks_another_worker(tmp_path: Path) -> None:
    value, close = scheduler(tmp_path)
    now = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)
    value.update_settings(
        enabled=True,
        local_time="07:30",
        timezone="Pacific/Auckland",
        quiet_start=None,
        quiet_end=None,
        now=now,
    )
    with value.store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO scheduler_leases(
                workspace_id, lease_owner, expires_at, acquired_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                "ws_koru_studio",
                "another_worker",
                "2026-08-26T20:05:00+00:00",
                "2026-08-26T20:00:00+00:00",
            ),
        )
    result = value.tick(now=now)
    assert result.status == "leased"
    assert close.calls == []
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


def add_scheduler_module() -> None:
    write("services/api/src/finance_agent/jobs/scheduler.py", SCHEDULER)
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.jobs import DailyCloseResult, DailyCloseService\n"
    if marker not in content:
        raise RuntimeError("DailyClose import marker missing")
    content = content.replace(
        marker,
        marker + "from finance_agent.jobs.scheduler import LocalScheduler\n",
        1,
    )
    content = content.replace(
        "        self.daily_close = DailyCloseService(self.engine)\n",
        "        self.daily_close = DailyCloseService(self.engine)\n"
        "        self.scheduler = LocalScheduler(\n"
        "            self.store, self.daily_close, workspace_id=WORKSPACE_ID\n"
        "        )\n",
        1,
    )
    content = content.replace(
        "        self._scheduler_tick_lock",
        "        self._scheduler_tick_lock",
        1,
    ) if "self._scheduler_tick_lock" in content else content.replace(
        "        self._lock = asyncio.Lock()\n",
        "        self._lock = asyncio.Lock()\n"
        "        self._scheduler_tick_lock = asyncio.Lock()\n",
        1,
    )
    recompose_marker = "        self.daily_close = DailyCloseService(self.engine)\n"
    first = content.find(recompose_marker)
    second = content.find(recompose_marker, first + len(recompose_marker))
    if second >= 0 and "self.scheduler = LocalScheduler" not in content[second:second + 300]:
        insertion = (
            recompose_marker
            + "        self.scheduler = LocalScheduler(\n"
            + "            self.store, self.daily_close, workspace_id=WORKSPACE_ID\n"
            + "        )\n"
        )
        content = content[:second] + content[second:].replace(recompose_marker, insertion, 1)
    write(path, content)
    insert_method_before(path, "LocalRouteServices", "_recompose_after_restore", SERVICE_METHODS)


def update_routes_and_protocol() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def create_workspace_backup(\n"
    addition = '''    async def scheduler_settings(self) -> Mapping[str, object]: ...\n\n    async def update_scheduler_settings(\n        self, *, enabled: bool, local_time: str, timezone: str,\n        quiet_start: str | None, quiet_end: str | None\n    ) -> Mapping[str, object]: ...\n\n    async def scheduler_tick(self) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("backup protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass RestoreBackupRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("RestoreBackupRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.post("/v1/workspaces/{workspace_id}/backups", status_code=201)\n'
    if route_marker not in content:
        raise RuntimeError("backup route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def wire_background_loop() -> None:
    path = "services/api/src/finance_agent/api/app.py"
    content = read(path)
    if "import asyncio" not in content:
        content = content.replace("from __future__ import annotations\n", "from __future__ import annotations\n\nimport asyncio\n", 1)
    if "async def run_scheduler_loop" not in content:
        marker = "\n\ndef create_app("
        if marker not in content:
            raise RuntimeError("create_app marker missing")
        content = content.replace(marker, LOOP_FUNCTION + marker, 1)
    old = '''    @asynccontextmanager\n    async def lifespan(_: FastAPI) -> AsyncIterator[None]:\n        yield\n        await services.aclose()\n'''
    new = '''    @asynccontextmanager\n    async def lifespan(_: FastAPI) -> AsyncIterator[None]:\n        interval = max(5.0, float(os.getenv("FOLIO_SCHEDULER_INTERVAL_SECONDS", "30")))\n        scheduler_task = asyncio.create_task(\n            run_scheduler_loop(services, interval_seconds=interval)\n        )\n        try:\n            yield\n        finally:\n            scheduler_task.cancel()\n            try:\n                await scheduler_task\n            except asyncio.CancelledError:\n                pass\n            await services.aclose()\n'''
    if old not in content:
        raise RuntimeError("lifespan block changed")
    content = content.replace(old, new, 1)
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/jobs/test_local_scheduler.py", TESTS)
    path = ".env.example"
    content = read(path)
    if "FOLIO_SCHEDULER_INTERVAL_SECONDS" not in content:
        content += "\n# Lightweight local scheduler poll interval. Settings remain disabled by default.\nFOLIO_SCHEDULER_INTERVAL_SECONDS=30\n"
        write(path, content)
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 7: opt-in durable local scheduler\n\n- Every workspace has explicit scheduler settings that default to disabled.\n- Scheduled Daily Close uses a per-day idempotency key and a five-minute SQLite lease.\n- Quiet hours, last-run state, next-run time, and bounded exponential retry state are durable.\n- A lightweight local process loop survives individual tick failures and makes no external call.\n- Scheduler receipts distinguish completed, no-op, disabled, not-due, quiet, leased, backoff, and failed states.\n- No notification is described as delivered merely because an outbox item exists.\n'''
    if "## Stack 7: opt-in durable local scheduler" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration()
    add_scheduler_module()
    update_routes_and_protocol()
    wire_background_loop()
    add_tests_and_docs()
    print("local scheduler changes applied")


if __name__ == "__main__":
    main()
