from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, value: str) -> None:
    destination = ROOT / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(value, encoding="utf-8")


def replace_once(value: str, old: str, new: str, *, label: str) -> str:
    count = value.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return value.replace(old, new, 1)


def patch_migrations() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    value = read(path)
    if 'name="local_scheduler_and_outbox_ack"' in value:
        return
    addition = r'''
    Migration(
        version=23,
        name="local_scheduler_and_outbox_ack",
        sql="""
        CREATE TABLE scheduler_controls (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id),
            job_type TEXT NOT NULL CHECK (job_type = 'daily_close'),
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            timezone TEXT NOT NULL,
            local_time TEXT NOT NULL CHECK (local_time GLOB '[0-2][0-9]:[0-5][0-9]'),
            quiet_start TEXT NOT NULL CHECK (quiet_start GLOB '[0-2][0-9]:[0-5][0-9]'),
            quiet_end TEXT NOT NULL CHECK (quiet_end GLOB '[0-2][0-9]:[0-5][0-9]'),
            next_run_at TEXT NOT NULL,
            lease_owner TEXT,
            lease_expires_at TEXT,
            last_started_at TEXT,
            last_completed_at TEXT,
            last_status TEXT CHECK (
                last_status IS NULL OR last_status IN ('completed', 'no_op', 'failed')
            ),
            failure_count INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE scheduler_tick_receipts (
            tick_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            worker_id TEXT NOT NULL,
            scheduled_for TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('completed', 'no_op', 'failed')),
            reason TEXT NOT NULL,
            run_id TEXT,
            receipt_id TEXT,
            next_run_at TEXT NOT NULL,
            error_json TEXT
        );

        CREATE TABLE outbox_acknowledgements (
            acknowledgement_id TEXT PRIMARY KEY,
            outbox_id TEXT NOT NULL REFERENCES outbox_messages(outbox_id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            request_id TEXT NOT NULL,
            acknowledged_at TEXT NOT NULL,
            UNIQUE (outbox_id, request_id)
        );

        CREATE INDEX scheduler_tick_workspace_started
            ON scheduler_tick_receipts(workspace_id, started_at DESC, tick_id DESC);
        CREATE INDEX outbox_ack_outbox
            ON outbox_acknowledgements(outbox_id, acknowledged_at DESC);
        """,
    ),
'''
    stripped = value.rstrip()
    if not stripped.endswith(")"):
        raise RuntimeError("migrations.py does not end with the migration tuple")
    write(path, stripped[:-1] + addition + ")\n")


def create_scheduler_module() -> None:
    write(
        "services/api/src/finance_agent/jobs/scheduler.py",
        '''"""Durable local Daily Close scheduling and bounded in-app outbox delivery."""\n\nfrom __future__ import annotations\n\nimport hashlib\nfrom dataclasses import dataclass\nfrom datetime import UTC, datetime, time, timedelta\nfrom typing import Protocol\nfrom zoneinfo import ZoneInfo, ZoneInfoNotFoundError\n\nfrom finance_agent.jobs.daily_close import DailyCloseResult, DailyCloseService\nfrom finance_agent.storage import SQLiteStore, canonical_json\n\nDEFAULT_LOCAL_TIME = "07:30"\nDEFAULT_QUIET_START = "21:00"\nDEFAULT_QUIET_END = "07:00"\nDEFAULT_TIMEZONE = "Pacific/Auckland"\n\n\nclass DailyCloseRunner(Protocol):\n    def run(self, *, requested_idempotency_key: str | None = None) -> DailyCloseResult: ...\n\n\n@dataclass(frozen=True, slots=True)\nclass SchedulerStatus:\n    workspace_id: str\n    enabled: bool\n    timezone: str\n    local_time: str\n    quiet_start: str\n    quiet_end: str\n    next_run_at: str\n    lease_owner: str | None\n    lease_expires_at: str | None\n    last_started_at: str | None\n    last_completed_at: str | None\n    last_status: str | None\n    failure_count: int\n\n    def as_contract(self) -> dict[str, object]:\n        return {\n            "workspaceId": self.workspace_id,\n            "enabled": self.enabled,\n            "timezone": self.timezone,\n            "localTime": self.local_time,\n            "quietStart": self.quiet_start,\n            "quietEnd": self.quiet_end,\n            "nextRunAt": self.next_run_at,\n            "leaseOwner": self.lease_owner,\n            "leaseExpiresAt": self.lease_expires_at,\n            "lastStartedAt": self.last_started_at,\n            "lastCompletedAt": self.last_completed_at,\n            "lastStatus": self.last_status,\n            "failureCount": self.failure_count,\n        }\n\n\n@dataclass(frozen=True, slots=True)\nclass SchedulerTickResult:\n    tick_id: str | None\n    workspace_id: str\n    status: str\n    reason: str\n    run_id: str | None\n    receipt_id: str | None\n    next_run_at: str\n\n    def as_contract(self) -> dict[str, object]:\n        return {\n            "tickId": self.tick_id,\n            "workspaceId": self.workspace_id,\n            "status": self.status,\n            "reason": self.reason,\n            "runId": self.run_id,\n            "receiptId": self.receipt_id,\n            "nextRunAt": self.next_run_at,\n        }\n\n\ndef _parse_hhmm(value: str, label: str) -> time:\n    try:\n        hour, minute = (int(part) for part in value.split(":", 1))\n        return time(hour, minute)\n    except (TypeError, ValueError) as exc:\n        raise ValueError(f"{label} must use HH:MM in 24-hour time") from exc\n\n\ndef _zone(value: str) -> ZoneInfo:\n    try:\n        return ZoneInfo(value)\n    except ZoneInfoNotFoundError as exc:\n        raise ValueError(f"unknown scheduler timezone: {value}") from exc\n\n\ndef _inside_quiet(value: time, start: time, end: time) -> bool:\n    if start == end:\n        return False\n    if start < end:\n        return start <= value < end\n    return value >= start or value < end\n\n\ndef next_scheduled_at(\n    after: datetime,\n    *,\n    timezone: str,\n    local_time: str,\n    quiet_start: str,\n    quiet_end: str,\n) -> datetime:\n    if after.tzinfo is None:\n        raise ValueError("scheduler times must be timezone-aware")\n    zone = _zone(timezone)\n    scheduled = _parse_hhmm(local_time, "local_time")\n    quiet_from = _parse_hhmm(quiet_start, "quiet_start")\n    quiet_until = _parse_hhmm(quiet_end, "quiet_end")\n    local_after = after.astimezone(zone)\n    candidate = datetime.combine(local_after.date(), scheduled, tzinfo=zone)\n    if candidate <= local_after:\n        candidate += timedelta(days=1)\n    if _inside_quiet(candidate.timetz().replace(tzinfo=None), quiet_from, quiet_until):\n        candidate_date = candidate.date()\n        if quiet_from > quiet_until and candidate.timetz().replace(tzinfo=None) >= quiet_from:\n            candidate_date += timedelta(days=1)\n        candidate = datetime.combine(candidate_date, quiet_until, tzinfo=zone)\n    return candidate.astimezone(UTC)\n\n\nclass LocalDailyCloseScheduler:\n    def __init__(\n        self,\n        store: SQLiteStore,\n        daily_close: DailyCloseRunner,\n        *,\n        worker_id: str = "scheduler_local_001",\n        lease_seconds: int = 300,\n    ) -> None:\n        if lease_seconds < 30:\n            raise ValueError("scheduler lease must be at least 30 seconds")\n        self.store = store\n        self.daily_close = daily_close\n        self.worker_id = worker_id\n        self.lease_seconds = lease_seconds\n\n    @staticmethod\n    def _status(row) -> SchedulerStatus:\n        return SchedulerStatus(\n            workspace_id=str(row["workspace_id"]),\n            enabled=bool(row["enabled"]),\n            timezone=str(row["timezone"]),\n            local_time=str(row["local_time"]),\n            quiet_start=str(row["quiet_start"]),\n            quiet_end=str(row["quiet_end"]),\n            next_run_at=str(row["next_run_at"]),\n            lease_owner=str(row["lease_owner"]) if row["lease_owner"] else None,\n            lease_expires_at=(\n                str(row["lease_expires_at"]) if row["lease_expires_at"] else None\n            ),\n            last_started_at=(\n                str(row["last_started_at"]) if row["last_started_at"] else None\n            ),\n            last_completed_at=(\n                str(row["last_completed_at"]) if row["last_completed_at"] else None\n            ),\n            last_status=str(row["last_status"]) if row["last_status"] else None,\n            failure_count=int(row["failure_count"]),\n        )\n\n    def ensure_default(self, workspace_id: str, *, now: datetime | None = None) -> None:\n        workspace = self.store.fetch_one(\n            "SELECT timezone FROM workspaces WHERE workspace_id = ?",\n            (workspace_id,),\n        )\n        if workspace is None:\n            return\n        instant = now or datetime.now(UTC)\n        timezone = str(workspace["timezone"] or DEFAULT_TIMEZONE)\n        next_run = next_scheduled_at(\n            instant, timezone=timezone, local_time=DEFAULT_LOCAL_TIME,\n            quiet_start=DEFAULT_QUIET_START, quiet_end=DEFAULT_QUIET_END,\n        )\n        with self.store.transaction() as connection:\n            connection.execute(\n                """\n                INSERT INTO scheduler_controls(\n                    workspace_id, job_type, enabled, timezone, local_time,\n                    quiet_start, quiet_end, next_run_at, updated_at\n                ) VALUES (?, 'daily_close', 1, ?, ?, ?, ?, ?, ?)\n                ON CONFLICT(workspace_id) DO NOTHING\n                """,\n                (\n                    workspace_id, timezone, DEFAULT_LOCAL_TIME, DEFAULT_QUIET_START,\n                    DEFAULT_QUIET_END, next_run.isoformat(), instant.isoformat(),\n                ),\n            )\n\n    def status(self, workspace_id: str) -> SchedulerStatus:\n        row = self.store.fetch_one(\n            "SELECT * FROM scheduler_controls WHERE workspace_id = ?",\n            (workspace_id,),\n        )\n        if row is None:\n            raise KeyError(workspace_id)\n        return self._status(row)\n\n    def configure(\n        self,\n        workspace_id: str,\n        *,\n        enabled: bool,\n        timezone: str,\n        local_time: str,\n        quiet_start: str,\n        quiet_end: str,\n        now: datetime | None = None,\n    ) -> SchedulerStatus:\n        instant = now or datetime.now(UTC)\n        _zone(timezone)\n        scheduled = _parse_hhmm(local_time, "local_time")\n        quiet_from = _parse_hhmm(quiet_start, "quiet_start")\n        quiet_until = _parse_hhmm(quiet_end, "quiet_end")\n        if _inside_quiet(scheduled, quiet_from, quiet_until):\n            raise ValueError("Daily Close local time must be outside configured quiet hours")\n        next_run = next_scheduled_at(\n            instant, timezone=timezone, local_time=local_time,\n            quiet_start=quiet_start, quiet_end=quiet_end,\n        )\n        with self.store.transaction() as connection:\n            updated = connection.execute(\n                """\n                UPDATE scheduler_controls\n                SET enabled = ?, timezone = ?, local_time = ?, quiet_start = ?,\n                    quiet_end = ?, next_run_at = ?, lease_owner = NULL,\n                    lease_expires_at = NULL, updated_at = ?\n                WHERE workspace_id = ?\n                """,\n                (\n                    int(enabled), timezone, local_time, quiet_start, quiet_end,\n                    next_run.isoformat(), instant.isoformat(), workspace_id,\n                ),\n            )\n            if updated.rowcount != 1:\n                raise KeyError(workspace_id)\n        return self.status(workspace_id)\n\n    def _claim(self, workspace_id: str, now: datetime) -> tuple[SchedulerStatus, str] | None:\n        with self.store.transaction() as connection:\n            row = connection.execute(\n                "SELECT * FROM scheduler_controls WHERE workspace_id = ?",\n                (workspace_id,),\n            ).fetchone()\n            if row is None:\n                raise KeyError(workspace_id)\n            status = self._status(row)\n            if not status.enabled:\n                return None\n            due = datetime.fromisoformat(status.next_run_at)\n            if due > now:\n                return None\n            if status.lease_expires_at is not None:\n                lease_expiry = datetime.fromisoformat(status.lease_expires_at)\n                if lease_expiry > now:\n                    return None\n            lease_token = hashlib.sha256(\n                f"{workspace_id}\\0{self.worker_id}\\0{now.isoformat()}".encode()\n            ).hexdigest()[:24]\n            connection.execute(\n                """\n                UPDATE scheduler_controls\n                SET lease_owner = ?, lease_expires_at = ?, last_started_at = ?,\n                    updated_at = ?\n                WHERE workspace_id = ?\n                """,\n                (\n                    f"{self.worker_id}:{lease_token}",\n                    (now + timedelta(seconds=self.lease_seconds)).isoformat(),\n                    now.isoformat(), now.isoformat(), workspace_id,\n                ),\n            )\n            return status, lease_token\n\n    def tick(self, workspace_id: str, *, now: datetime | None = None) -> SchedulerTickResult:\n        instant = (now or datetime.now(UTC)).astimezone(UTC)\n        current = self.status(workspace_id)\n        claimed = self._claim(workspace_id, instant)\n        if claimed is None:\n            reason = "disabled" if not current.enabled else (\n                "not_due" if datetime.fromisoformat(current.next_run_at) > instant else "leased"\n            )\n            return SchedulerTickResult(\n                tick_id=None, workspace_id=workspace_id, status="no_op", reason=reason,\n                run_id=None, receipt_id=None, next_run_at=current.next_run_at,\n            )\n        scheduled_status, lease_token = claimed\n        scheduled_for = scheduled_status.next_run_at\n        tick_id = "schedtick_" + hashlib.sha256(\n            f"{workspace_id}\\0{scheduled_for}\\0{lease_token}".encode()\n        ).hexdigest()[:24]\n        result: DailyCloseResult | None = None\n        error: Exception | None = None\n        try:\n            local_date = instant.astimezone(_zone(scheduled_status.timezone)).date().isoformat()\n            result = self.daily_close.run(\n                requested_idempotency_key=f"scheduled-daily-close:{workspace_id}:{local_date}"\n            )\n        except Exception as exc:  # persisted below without provider or credential detail\n            error = exc\n\n        completed_at = datetime.now(UTC) if now is None else instant + timedelta(milliseconds=1)\n        if error is None and result is not None:\n            status = "no_op" if result.status == "no_op" else "completed"\n            reason = "input_unchanged" if status == "no_op" else "daily_close_committed"\n            failure_count = 0\n            next_run = next_scheduled_at(\n                completed_at, timezone=scheduled_status.timezone,\n                local_time=scheduled_status.local_time,\n                quiet_start=scheduled_status.quiet_start,\n                quiet_end=scheduled_status.quiet_end,\n            )\n            run_id = result.run_id\n            receipt_id = result.receipt_id\n            error_json = None\n        else:\n            status = "failed"\n            reason = "daily_close_failed"\n            failure_count = scheduled_status.failure_count + 1\n            retry_seconds = min(3600, 60 * (2 ** min(failure_count - 1, 6)))\n            next_run = completed_at + timedelta(seconds=retry_seconds)\n            run_id = None\n            receipt_id = None\n            error_json = canonical_json({\n                "code": "scheduled_daily_close_failed",\n                "retryable": True,\n            })\n\n        with self.store.transaction() as connection:\n            owner = connection.execute(\n                "SELECT lease_owner FROM scheduler_controls WHERE workspace_id = ?",\n                (workspace_id,),\n            ).fetchone()\n            if owner is None or str(owner["lease_owner"]) != f"{self.worker_id}:{lease_token}":\n                raise RuntimeError("scheduler lease ownership changed before completion")\n            connection.execute(\n                """\n                UPDATE scheduler_controls\n                SET next_run_at = ?, lease_owner = NULL, lease_expires_at = NULL,\n                    last_completed_at = ?, last_status = ?, failure_count = ?, updated_at = ?\n                WHERE workspace_id = ?\n                """,\n                (\n                    next_run.isoformat(), completed_at.isoformat(), status, failure_count,\n                    completed_at.isoformat(), workspace_id,\n                ),\n            )\n            connection.execute(\n                """\n                INSERT INTO scheduler_tick_receipts(\n                    tick_id, workspace_id, worker_id, scheduled_for, started_at,\n                    completed_at, status, reason, run_id, receipt_id, next_run_at, error_json\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                """,\n                (\n                    tick_id, workspace_id, self.worker_id, scheduled_for, instant.isoformat(),\n                    completed_at.isoformat(), status, reason, run_id, receipt_id,\n                    next_run.isoformat(), error_json,\n                ),\n            )\n        return SchedulerTickResult(\n            tick_id=tick_id, workspace_id=workspace_id, status=status, reason=reason,\n            run_id=run_id, receipt_id=receipt_id, next_run_at=next_run.isoformat(),\n        )\n\n\nclass LocalOutboxService:\n    def __init__(self, store: SQLiteStore) -> None:\n        self.store = store\n\n    def list_pending(self, workspace_id: str, *, limit: int = 50) -> tuple[dict[str, object], ...]:\n        if limit < 1 or limit > 200:\n            raise ValueError("outbox limit must be between 1 and 200")\n        rows = self.store.fetch_all(\n            """\n            SELECT outbox_id, kind, payload_json, status, correlation_id,\n                   evidence_ids_json, created_at, attempted_at, failure_json\n            FROM outbox_messages\n            WHERE workspace_id = ? AND status IN ('queued', 'attempted', 'failed')\n            ORDER BY created_at, outbox_id LIMIT ?\n            """,\n            (workspace_id, limit),\n        )\n        import json\n        return tuple(\n            {\n                "outboxId": str(row["outbox_id"]),\n                "kind": str(row["kind"]),\n                "payload": json.loads(str(row["payload_json"])),\n                "status": str(row["status"]),\n                "correlationId": str(row["correlation_id"]),\n                "evidenceIds": json.loads(str(row["evidence_ids_json"])),\n                "createdAt": str(row["created_at"]),\n                "attemptedAt": str(row["attempted_at"]) if row["attempted_at"] else None,\n                "failure": json.loads(str(row["failure_json"])) if row["failure_json"] else None,\n            }\n            for row in rows\n        )\n\n    def acknowledge(\n        self,\n        workspace_id: str,\n        outbox_id: str,\n        request_id: str,\n        *,\n        acknowledged_at: datetime | None = None,\n    ) -> dict[str, object]:\n        instant = (acknowledged_at or datetime.now(UTC)).astimezone(UTC).isoformat()\n        acknowledgement_id = "outboxack_" + hashlib.sha256(\n            f"{outbox_id}\\0{request_id}".encode()\n        ).hexdigest()[:24]\n        with self.store.transaction() as connection:\n            existing_ack = connection.execute(\n                """\n                SELECT acknowledgement_id FROM outbox_acknowledgements\n                WHERE outbox_id = ? AND request_id = ?\n                """,\n                (outbox_id, request_id),\n            ).fetchone()\n            row = connection.execute(\n                "SELECT workspace_id, status FROM outbox_messages WHERE outbox_id = ?",\n                (outbox_id,),\n            ).fetchone()\n            if row is None:\n                raise KeyError(outbox_id)\n            if str(row["workspace_id"]) != workspace_id:\n                raise ValueError("outbox message belongs to another workspace")\n            if existing_ack is not None:\n                return {\n                    "outboxId": outbox_id, "status": "delivered",\n                    "acknowledgementId": str(existing_ack["acknowledgement_id"]),\n                }\n            if str(row["status"]) == "delivered":\n                return {\n                    "outboxId": outbox_id, "status": "already_delivered",\n                    "acknowledgementId": None,\n                }\n            connection.execute(\n                """\n                UPDATE outbox_messages\n                SET status = 'delivered', attempted_at = COALESCE(attempted_at, ?),\n                    delivered_at = ?, failure_json = NULL\n                WHERE outbox_id = ?\n                """,\n                (instant, instant, outbox_id),\n            )\n            connection.execute(\n                """\n                INSERT INTO outbox_acknowledgements(\n                    acknowledgement_id, outbox_id, workspace_id, request_id, acknowledged_at\n                ) VALUES (?, ?, ?, ?, ?)\n                """,\n                (acknowledgement_id, outbox_id, workspace_id, request_id, instant),\n            )\n        return {\n            "outboxId": outbox_id, "status": "delivered",\n            "acknowledgementId": acknowledgement_id,\n        }\n\n\n__all__ = [\n    "LocalDailyCloseScheduler", "LocalOutboxService", "SchedulerStatus",\n    "SchedulerTickResult", "next_scheduled_at",\n]\n''',
    )


def patch_route_protocol() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    value = read(path)
    anchor = """    async def create_backup(self, workspace_id: str) -> Mapping[str, object]: ...\n"""
    addition = """    async def scheduler_status(self, workspace_id: str) -> Mapping[str, object]: ...\n\n    async def configure_scheduler(\n        self,\n        *,\n        workspace_id: str,\n        enabled: bool,\n        timezone: str,\n        local_time: str,\n        quiet_start: str,\n        quiet_end: str,\n    ) -> Mapping[str, object]: ...\n\n    async def scheduler_tick(self, workspace_id: str) -> Mapping[str, object]: ...\n\n    async def list_outbox(\n        self, *, workspace_id: str, limit: int\n    ) -> Mapping[str, object]: ...\n\n    async def acknowledge_outbox(\n        self, *, workspace_id: str, outbox_id: str, request_id: str\n    ) -> Mapping[str, object]: ...\n\n"""
    if anchor not in value:
        raise RuntimeError("backup protocol anchor is missing")
    write(path, value.replace(anchor, addition + anchor, 1))


def patch_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/router.py"
    value = read(path)
    request_anchor = """class BackupRequest(RequestModel):\n"""
    request_models = """class SchedulerConfigurationRequest(RequestModel):\n    workspace_id: str = Field(alias=\"workspaceId\")\n    enabled: bool\n    timezone: str = Field(min_length=1, max_length=80)\n    local_time: str = Field(alias=\"localTime\", pattern=r\"^[0-2][0-9]:[0-5][0-9]$\")\n    quiet_start: str = Field(alias=\"quietStart\", pattern=r\"^[0-2][0-9]:[0-5][0-9]$\")\n    quiet_end: str = Field(alias=\"quietEnd\", pattern=r\"^[0-2][0-9]:[0-5][0-9]$\")\n\n\nclass SchedulerTickRequest(RequestModel):\n    workspace_id: str = Field(alias=\"workspaceId\")\n\n\nclass OutboxAcknowledgementRequest(RequestModel):\n    workspace_id: str = Field(alias=\"workspaceId\")\n    request_id: str = Field(alias=\"requestId\", min_length=1, max_length=160)\n\n\n"""
    if request_anchor not in value:
        raise RuntimeError("backup request model anchor is missing")
    value = value.replace(request_anchor, request_models + request_anchor, 1)

    route_anchor = '''    @router.post("/v1/backups", status_code=201)\n'''
    routes = '''    @router.get("/v1/scheduler/status")\n    async def scheduler_status(\n        services: Services,\n        workspace_id: Annotated[str, Query(alias="workspaceId")],\n    ) -> dict[str, object]:\n        return dict(await services.scheduler_status(workspace_id))\n\n    @router.put("/v1/scheduler/configuration")\n    async def configure_scheduler(\n        body: SchedulerConfigurationRequest,\n        services: Services,\n    ) -> dict[str, object]:\n        return dict(\n            await services.configure_scheduler(\n                workspace_id=body.workspace_id,\n                enabled=body.enabled,\n                timezone=body.timezone,\n                local_time=body.local_time,\n                quiet_start=body.quiet_start,\n                quiet_end=body.quiet_end,\n            )\n        )\n\n    @router.post("/v1/scheduler/tick")\n    async def scheduler_tick(\n        body: SchedulerTickRequest,\n        services: Services,\n    ) -> dict[str, object]:\n        return dict(await services.scheduler_tick(body.workspace_id))\n\n    @router.get("/v1/outbox")\n    async def list_outbox(\n        services: Services,\n        workspace_id: Annotated[str, Query(alias="workspaceId")],\n        limit: Annotated[int, Query(ge=1, le=200)] = 50,\n    ) -> dict[str, object]:\n        return dict(await services.list_outbox(workspace_id=workspace_id, limit=limit))\n\n    @router.post("/v1/outbox/{outbox_id}/ack")\n    async def acknowledge_outbox(\n        outbox_id: str,\n        body: OutboxAcknowledgementRequest,\n        services: Services,\n    ) -> dict[str, object]:\n        return dict(\n            await services.acknowledge_outbox(\n                workspace_id=body.workspace_id,\n                outbox_id=outbox_id,\n                request_id=body.request_id,\n            )\n        )\n\n'''
    if route_anchor not in value:
        raise RuntimeError("backup route anchor is missing")
    write(path, value.replace(route_anchor, routes + route_anchor, 1))


def patch_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    value = read(path)
    value = replace_once(
        value,
        "from finance_agent.jobs import DailyCloseResult, DailyCloseService\n",
        "from finance_agent.jobs import DailyCloseResult, DailyCloseService\nfrom finance_agent.jobs.scheduler import LocalDailyCloseScheduler, LocalOutboxService\n",
        label="scheduler imports",
    )
    value = replace_once(
        value,
        """        self.daily_close = DailyCloseService(self.engine)\n""",
        """        self.daily_close = DailyCloseService(self.engine)\n        self.scheduler = LocalDailyCloseScheduler(self.store, self.daily_close)\n        self.outbox = LocalOutboxService(self.store)\n""",
        label="scheduler composition",
    )
    value = replace_once(
        value,
        """        if auto_seed:\n            self._ensure_seeded()\n""",
        """        if auto_seed:\n            self._ensure_seeded()\n        self.scheduler.ensure_default(WORKSPACE_ID)\n""",
        label="scheduler default",
    )
    anchor = """    async def create_backup(self, workspace_id: str) -> Mapping[str, object]:\n"""
    methods = '''    async def scheduler_status(self, workspace_id: str) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        return self.scheduler.status(workspace_id).as_contract()\n\n    async def configure_scheduler(\n        self,\n        *,\n        workspace_id: str,\n        enabled: bool,\n        timezone: str,\n        local_time: str,\n        quiet_start: str,\n        quiet_end: str,\n    ) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        async with self._lock:\n            status = await asyncio.to_thread(\n                self.scheduler.configure,\n                workspace_id,\n                enabled=enabled,\n                timezone=timezone,\n                local_time=local_time,\n                quiet_start=quiet_start,\n                quiet_end=quiet_end,\n            )\n        return status.as_contract()\n\n    async def scheduler_tick(self, workspace_id: str) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        async with self._lock:\n            result = await asyncio.to_thread(self.scheduler.tick, workspace_id)\n        if result.run_id:\n            daily_close = self.store.fetch_one(\n                "SELECT result_json FROM job_runs WHERE run_id = ?",\n                (result.run_id,),\n            )\n            if daily_close is not None:\n                self.working_understanding.ensure_current(workspace_id=workspace_id)\n        return result.as_contract()\n\n    async def list_outbox(\n        self, *, workspace_id: str, limit: int\n    ) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        messages = await asyncio.to_thread(\n            self.outbox.list_pending, workspace_id, limit=limit\n        )\n        return {"workspaceId": workspace_id, "messages": list(messages)}\n\n    async def acknowledge_outbox(\n        self, *, workspace_id: str, outbox_id: str, request_id: str\n    ) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        return await asyncio.to_thread(\n            self.outbox.acknowledge, workspace_id, outbox_id, request_id\n        )\n\n'''
    if anchor not in value:
        raise RuntimeError("backup service anchor is missing")
    write(path, value.replace(anchor, methods + anchor, 1))


def create_scheduler_cli() -> None:
    write(
        "scripts/scheduler_control.py",
        '''from __future__ import annotations\n\nimport argparse\nimport json\nimport os\nimport signal\nimport time\nfrom datetime import UTC, datetime\nfrom pathlib import Path\n\nfrom finance_agent.finance import FinanceEngine\nfrom finance_agent.jobs import DailyCloseService\nfrom finance_agent.jobs.scheduler import LocalDailyCloseScheduler\nfrom finance_agent.storage import SQLiteStore\n\nROOT = Path(__file__).resolve().parents[1]\nDEFAULT_DATABASE = ROOT / "var" / "finance-agent.sqlite3"\nWORKSPACE_ID = "ws_koru_studio"\n\n\ndef database_path() -> Path:\n    configured = os.getenv("FINANCE_DATABASE_PATH")\n    return Path(configured).expanduser() if configured else DEFAULT_DATABASE\n\n\ndef compose() -> LocalDailyCloseScheduler:\n    store = SQLiteStore(database_path())\n    engine = FinanceEngine(store)\n    engine.initialise()\n    scheduler = LocalDailyCloseScheduler(store, DailyCloseService(engine), worker_id=f"scheduler_pid_{os.getpid()}")\n    scheduler.ensure_default(WORKSPACE_ID)\n    return scheduler\n\n\ndef main() -> int:\n    parser = argparse.ArgumentParser(description="Operate Folio's durable local scheduler")\n    subparsers = parser.add_subparsers(dest="command", required=True)\n    subparsers.add_parser("status")\n    subparsers.add_parser("run-once")\n    serve = subparsers.add_parser("serve")\n    serve.add_argument("--interval", type=float, default=30.0)\n    arguments = parser.parse_args()\n    scheduler = compose()\n\n    if arguments.command == "status":\n        print(json.dumps(scheduler.status(WORKSPACE_ID).as_contract(), indent=2))\n        return 0\n    if arguments.command == "run-once":\n        print(json.dumps(scheduler.tick(WORKSPACE_ID).as_contract(), indent=2))\n        return 0\n    if arguments.interval < 1 or arguments.interval > 300:\n        parser.error("serve interval must be between 1 and 300 seconds")\n\n    running = True\n\n    def stop(_signum, _frame) -> None:\n        nonlocal running\n        running = False\n\n    signal.signal(signal.SIGINT, stop)\n    signal.signal(signal.SIGTERM, stop)\n    while running:\n        result = scheduler.tick(WORKSPACE_ID, now=datetime.now(UTC))\n        if result.tick_id is not None:\n            print(json.dumps(result.as_contract(), separators=(",", ":")), flush=True)\n        deadline = time.monotonic() + arguments.interval\n        while running and time.monotonic() < deadline:\n            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))\n    return 0\n\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n''',
    )


def patch_launcher() -> None:
    path = "run"
    value = read(path)
    value = replace_once(
        value,
        """UI_PID_FILE=\"${PID_DIR}/ui.pid\"\n""",
        """UI_PID_FILE=\"${PID_DIR}/ui.pid\"\nSCHEDULER_LOG=\"${LOG_DIR}/scheduler.log\"\nSCHEDULER_PID_FILE=\"${PID_DIR}/scheduler.pid\"\n""",
        label="scheduler launcher paths",
    )
    value = replace_once(
        value,
        """DO_ELECTRON=1\nDO_STOP=0\n""",
        """DO_ELECTRON=1\nDO_SCHEDULER=1\nDO_STOP=0\n""",
        label="scheduler launcher flag",
    )
    value = replace_once(
        value,
        """    --no-electron) DO_ELECTRON=0 ;;\n""",
        """    --no-electron) DO_ELECTRON=0 ;;\n    --no-scheduler) DO_SCHEDULER=0 ;;\n""",
        label="scheduler launcher option",
    )
    value = replace_once(
        value,
        """  stop_pidfile \"$UI_PID_FILE\" \"UI\"\n""",
        """  stop_pidfile \"$UI_PID_FILE\" \"UI\"\n  stop_pidfile \"$SCHEDULER_PID_FILE\" \"scheduler\"\n""",
        label="stop scheduler process",
    )
    function_anchor = """wait_for() {\n"""
    scheduler_function = '''start_scheduler() {\n  if [[ "$DO_SCHEDULER" -ne 1 ]]; then\n    return 0\n  fi\n  if [[ -f "$SCHEDULER_PID_FILE" ]]; then\n    local scheduler_pid\n    scheduler_pid="$(cat "$SCHEDULER_PID_FILE" 2>/dev/null || true)"\n    if [[ -n "$scheduler_pid" ]] && kill -0 "$scheduler_pid" 2>/dev/null; then\n      log "Scheduler already running (pid ${scheduler_pid})"\n      return 0\n    fi\n    rm -f "$SCHEDULER_PID_FILE"\n  fi\n  : >"$SCHEDULER_LOG"\n  log "Starting durable local scheduler…"\n  nohup uv run --project services/api python scripts/scheduler_control.py serve \\\n    >>"$SCHEDULER_LOG" 2>&1 &\n  echo $! >"$SCHEDULER_PID_FILE"\n}\n\n'''
    if function_anchor not in value:
        raise RuntimeError("wait_for launcher anchor is missing")
    value = value.replace(function_anchor, scheduler_function + function_anchor, 1)
    value = replace_once(
        value,
        """wait_for \"UI\" ui_healthy 60\n\n""",
        """wait_for \"UI\" ui_healthy 60\nstart_scheduler\n\n""",
        label="start scheduler after services",
    )
    value = replace_once(
        value,
        """log \" Stop Folio run PIDs:        ./run --stop\"\n""",
        """log \" Scheduler status:           pnpm scheduler:status\"\nlog \" Stop Folio run PIDs:        ./run --stop\"\n""",
        label="scheduler launcher receipt",
    )
    write(path, value)


def patch_package_scripts() -> None:
    path = "package.json"
    value = json.loads(read(path))
    value["scripts"]["scheduler:status"] = "uv run --project services/api python scripts/scheduler_control.py status"
    value["scripts"]["scheduler:run-once"] = "uv run --project services/api python scripts/scheduler_control.py run-once"
    value["scripts"]["scheduler:serve"] = "uv run --project services/api python scripts/scheduler_control.py serve"
    write(path, json.dumps(value, indent=2) + "\n")


def add_tests() -> None:
    write(
        "services/api/tests/jobs/test_scheduler.py",
        '''from __future__ import annotations\n\nfrom datetime import UTC, datetime, timedelta\nfrom pathlib import Path\n\nimport pytest\n\nfrom finance_agent.finance import FinanceEngine\nfrom finance_agent.jobs import DailyCloseService\nfrom finance_agent.jobs.scheduler import (\n    LocalDailyCloseScheduler, LocalOutboxService, next_scheduled_at,\n)\nfrom finance_agent.storage import SQLiteStore\n\nROOT = Path(__file__).resolve().parents[4]\nCSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"\n\n\ndef seeded(tmp_path: Path):\n    store = SQLiteStore(tmp_path / "scheduler.sqlite3")\n    engine = FinanceEngine(store)\n    engine.reset_demo(CSV)\n    scheduler = LocalDailyCloseScheduler(store, DailyCloseService(engine), worker_id="worker_test")\n    scheduler.ensure_default("ws_koru_studio", now=datetime(2026, 8, 27, 0, 0, tzinfo=UTC))\n    return store, engine, scheduler\n\n\ndef force_due(store: SQLiteStore, instant: datetime) -> None:\n    with store.transaction() as connection:\n        connection.execute(\n            "UPDATE scheduler_controls SET next_run_at = ?, lease_owner = NULL, lease_expires_at = NULL",\n            ((instant - timedelta(seconds=1)).isoformat(),),\n        )\n\n\ndef test_next_schedule_respects_timezone_and_quiet_hours() -> None:\n    result = next_scheduled_at(\n        datetime(2026, 8, 27, 20, 0, tzinfo=UTC),\n        timezone="Pacific/Auckland", local_time="22:00",\n        quiet_start="21:00", quiet_end="07:00",\n    )\n    assert result.astimezone().tzinfo is not None\n    local = result.astimezone(__import__("zoneinfo").ZoneInfo("Pacific/Auckland"))\n    assert (local.hour, local.minute) == (7, 0)\n\n\ndef test_due_tick_runs_once_and_advances_the_schedule(tmp_path: Path) -> None:\n    store, _engine, scheduler = seeded(tmp_path)\n    instant = datetime(2026, 8, 27, 1, 0, tzinfo=UTC)\n    force_due(store, instant)\n\n    first = scheduler.tick("ws_koru_studio", now=instant)\n    second = scheduler.tick("ws_koru_studio", now=instant + timedelta(seconds=1))\n\n    assert first.status == "completed"\n    assert first.run_id is not None\n    assert second.status == "no_op"\n    assert second.reason == "not_due"\n    assert len(store.fetch_all("SELECT * FROM scheduler_tick_receipts")) == 1\n    assert len(store.fetch_all("SELECT * FROM job_runs")) == 1\n\n\ndef test_live_lease_prevents_a_second_worker_claim(tmp_path: Path) -> None:\n    store, _engine, scheduler = seeded(tmp_path)\n    instant = datetime(2026, 8, 27, 2, 0, tzinfo=UTC)\n    force_due(store, instant)\n    with store.transaction() as connection:\n        connection.execute(\n            "UPDATE scheduler_controls SET lease_owner = 'other', lease_expires_at = ?",\n            ((instant + timedelta(minutes=5)).isoformat(),),\n        )\n    result = scheduler.tick("ws_koru_studio", now=instant)\n    assert result.status == "no_op"\n    assert result.reason == "leased"\n    assert not store.fetch_all("SELECT * FROM scheduler_tick_receipts")\n\n\ndef test_failure_is_redacted_and_retried_with_backoff(tmp_path: Path) -> None:\n    store, _engine, scheduler = seeded(tmp_path)\n    instant = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)\n    force_due(store, instant)\n\n    class FailingClose:\n        def run(self, *, requested_idempotency_key=None):\n            raise RuntimeError("secret provider token must not persist")\n\n    failing = LocalDailyCloseScheduler(store, FailingClose(), worker_id="worker_failure")\n    result = failing.tick("ws_koru_studio", now=instant)\n    assert result.status == "failed"\n    row = store.fetch_one("SELECT failure_count, next_run_at FROM scheduler_controls")\n    assert row is not None\n    assert int(row["failure_count"]) == 1\n    assert datetime.fromisoformat(str(row["next_run_at"])) == instant + timedelta(seconds=60, milliseconds=1)\n    receipt = store.fetch_one("SELECT error_json FROM scheduler_tick_receipts")\n    assert receipt is not None\n    assert "secret provider token" not in str(receipt["error_json"])\n\n\ndef test_outbox_acknowledgement_is_bounded_and_idempotent(tmp_path: Path) -> None:\n    store, _engine, scheduler = seeded(tmp_path)\n    instant = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)\n    force_due(store, instant)\n    scheduler.tick("ws_koru_studio", now=instant)\n    service = LocalOutboxService(store)\n    messages = service.list_pending("ws_koru_studio")\n    assert len(messages) == 1\n    outbox_id = str(messages[0]["outboxId"])\n\n    first = service.acknowledge(\n        "ws_koru_studio", outbox_id, "ack_request_1", acknowledged_at=instant\n    )\n    second = service.acknowledge(\n        "ws_koru_studio", outbox_id, "ack_request_1", acknowledged_at=instant\n    )\n    assert first == second\n    assert service.list_pending("ws_koru_studio") == ()\n    assert len(store.fetch_all("SELECT * FROM outbox_acknowledgements")) == 1\n\n\ndef test_schedule_configuration_rejects_a_close_inside_quiet_hours(tmp_path: Path) -> None:\n    _store, _engine, scheduler = seeded(tmp_path)\n    with pytest.raises(ValueError, match="outside configured quiet hours"):\n        scheduler.configure(\n            "ws_koru_studio", enabled=True, timezone="Pacific/Auckland",\n            local_time="22:00", quiet_start="21:00", quiet_end="07:00",\n            now=datetime(2026, 8, 27, 5, 0, tzinfo=UTC),\n        )\n''',
    )


def main() -> None:
    patch_migrations()
    create_scheduler_module()
    patch_route_protocol()
    patch_routes()
    patch_services()
    create_scheduler_cli()
    patch_launcher()
    patch_package_scripts()
    add_tests()


if __name__ == "__main__":
    main()
