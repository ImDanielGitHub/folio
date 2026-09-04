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
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    before = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


def replace_method(path: str, class_name: str, name: str, replacement: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    candidate = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    if candidate.end_lineno is None:
        raise RuntimeError(f"{path}: method {class_name}.{name} has no end line")
    lines = content.splitlines(keepends=True)
    start = candidate.lineno - 1
    while start > 0 and lines[start - 1].lstrip().startswith("@"):
        start -= 1
    write(path, "".join(lines[:start]) + replacement.rstrip() + "\n\n" + "".join(lines[candidate.end_lineno:]))


def replace_function_calls(path: str, function_name: str, replacement_name: str) -> int:
    content = read(path)
    needle = f"{function_name}("
    cursor = 0
    pieces: list[str] = []
    count = 0
    while True:
        start = content.find(needle, cursor)
        if start < 0:
            pieces.append(content[cursor:])
            break
        pieces.append(content[cursor:start])
        depth = 1
        index = start + len(needle)
        quote: str | None = None
        escaped = False
        while index < len(content) and depth:
            character = content[index]
            if quote is not None:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
            else:
                if character in {"'", '"'}:
                    quote = character
                elif character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
            index += 1
        if depth:
            raise RuntimeError(f"unbalanced call to {function_name}")
        arguments = content[start + len(needle): index - 1]
        pieces.append(f"{replacement_name}(connection, {arguments})")
        count += 1
        cursor = index
    write(path, "".join(pieces))
    return count


MIGRATION = '''    Migration(
        version={version},
        name="persisted_cash_commitments",
        sql="""
        CREATE TABLE cash_commitments (
            commitment_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            label TEXT NOT NULL CHECK (length(trim(label)) BETWEEN 1 AND 240),
            amount_minor INTEGER NOT NULL,
            currency TEXT NOT NULL CHECK (currency = 'NZD'),
            due_on TEXT NOT NULL,
            recurrence TEXT NOT NULL DEFAULT 'none'
                CHECK (recurrence IN ('none', 'weekly', 'monthly')),
            recurrence_count INTEGER NOT NULL DEFAULT 1
                CHECK (recurrence_count BETWEEN 1 AND 120),
            status TEXT NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned', 'confirmed', 'cancelled', 'completed')),
            source TEXT NOT NULL CHECK (source IN (
                'owner', 'document', 'connector', 'deterministic', 'import'
            )),
            evidence_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE cash_scenario_revisions (
            scenario_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            title TEXT NOT NULL,
            base_as_of TEXT NOT NULL,
            horizon_days INTEGER NOT NULL CHECK (horizon_days BETWEEN 1 AND 365),
            overrides_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            created_at TEXT NOT NULL,
            PRIMARY KEY (scenario_id, revision)
        );

        CREATE INDEX cash_commitments_due
            ON cash_commitments(workspace_id, status, due_on, commitment_id);
        CREATE INDEX cash_scenario_workspace_time
            ON cash_scenario_revisions(workspace_id, created_at, scenario_id, revision);
        """,
    ),
'''

COMMITMENTS = '''"""Persisted cash commitments and deterministic workspace scenarios."""

from __future__ import annotations

import calendar
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from finance_agent.finance.domain import CashForecast, ForecastEvent
from finance_agent.finance.forecast import project_cash
from finance_agent.storage import SQLiteStore, canonical_json

Recurrence = Literal["none", "weekly", "monthly"]
CommitmentStatus = Literal["planned", "confirmed", "cancelled", "completed"]

DEMO_COMMITMENTS: tuple[dict[str, object], ...] = (
    {"commitmentId": "commit_koru_client_payment", "label": "Expected client payment", "amountMinor": 125000, "dueOn": "2026-07-28", "status": "confirmed", "evidenceIds": ["evd_koru_bank_csv"]},
    {"commitmentId": "commit_koru_studio_rent", "label": "Studio rent", "amountMinor": -120000, "dueOn": "2026-07-31", "status": "confirmed", "evidenceIds": ["evd_koru_bank_csv"]},
    {"commitmentId": "commit_koru_adobe", "label": "Adobe", "amountMinor": -8999, "dueOn": "2026-08-03", "status": "confirmed", "evidenceIds": ["evd_koru_bank_csv"]},
    {"commitmentId": "commit_koru_xero", "label": "Xero", "amountMinor": -7500, "dueOn": "2026-08-04", "status": "confirmed", "evidenceIds": ["evd_koru_bank_csv"]},
    {"commitmentId": "commit_koru_laptop", "label": "Planned laptop", "amountMinor": -300000, "dueOn": "2026-08-07", "status": "planned", "evidenceIds": ["evd_koru_forecast_30d"]},
    {"commitmentId": "commit_koru_figma", "label": "Figma", "amountMinor": -3000, "dueOn": "2026-08-10", "status": "confirmed", "evidenceIds": ["evd_koru_bank_csv"]},
)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _month_later(value: date) -> date:
    month = value.month + 1
    year = value.year
    if month == 13:
        month = 1
        year += 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def seed_demo_commitments(connection: sqlite3.Connection, workspace_id: str) -> None:
    created_at = "2026-07-17T08:00:00+12:00"
    for value in DEMO_COMMITMENTS:
        connection.execute(
            """
            INSERT INTO cash_commitments(
                commitment_id, workspace_id, label, amount_minor, currency,
                due_on, recurrence, recurrence_count, status, source,
                evidence_ids_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'NZD', ?, 'none', 1, ?, 'deterministic', ?, ?, ?)
            ON CONFLICT(commitment_id) DO NOTHING
            """,
            (
                value["commitmentId"],
                workspace_id,
                value["label"],
                value["amountMinor"],
                value["dueOn"],
                value["status"],
                canonical_json(value["evidenceIds"]),
                created_at,
                created_at,
            ),
        )


@dataclass(frozen=True, slots=True)
class CashCommitment:
    commitment_id: str
    workspace_id: str
    label: str
    amount_minor: int
    currency: str
    due_on: str
    recurrence: str
    recurrence_count: int
    status: str
    source: str
    evidence_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "commitmentId": self.commitment_id,
            "workspaceId": self.workspace_id,
            "label": self.label,
            "amountMinor": self.amount_minor,
            "currency": self.currency,
            "dueOn": self.due_on,
            "recurrence": self.recurrence,
            "recurrenceCount": self.recurrence_count,
            "status": self.status,
            "source": self.source,
            "evidenceIds": list(self.evidence_ids),
        }


class WorkspaceCommitmentService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _row(value: sqlite3.Row) -> CashCommitment:
        return CashCommitment(
            commitment_id=str(value["commitment_id"]),
            workspace_id=str(value["workspace_id"]),
            label=str(value["label"]),
            amount_minor=int(value["amount_minor"]),
            currency=str(value["currency"]),
            due_on=str(value["due_on"]),
            recurrence=str(value["recurrence"]),
            recurrence_count=int(value["recurrence_count"]),
            status=str(value["status"]),
            source=str(value["source"]),
            evidence_ids=tuple(json.loads(str(value["evidence_ids_json"]))),
        )

    def list(self, workspace_id: str, *, include_cancelled: bool = False) -> tuple[CashCommitment, ...]:
        clause = "" if include_cancelled else "AND status != 'cancelled'"
        rows = self.store.fetch_all(
            f"""
            SELECT * FROM cash_commitments
            WHERE workspace_id = ? {clause}
            ORDER BY due_on, commitment_id
            """,
            (workspace_id,),
        )
        return tuple(self._row(row) for row in rows)

    def upsert(
        self,
        *,
        workspace_id: str,
        commitment_id: str | None,
        label: str,
        amount_minor: int,
        due_on: str,
        recurrence: Recurrence = "none",
        recurrence_count: int = 1,
        status: CommitmentStatus = "planned",
        source: str = "owner",
        evidence_ids: tuple[str, ...] = (),
    ) -> CashCommitment:
        if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            raise TypeError("commitment amount must use integer minor units")
        if not label.strip():
            raise ValueError("commitment label must not be blank")
        try:
            due = date.fromisoformat(due_on)
        except ValueError as exc:
            raise ValueError("commitment dueOn must use YYYY-MM-DD") from exc
        if recurrence not in {"none", "weekly", "monthly"}:
            raise ValueError("unsupported recurrence")
        if not 1 <= recurrence_count <= 120:
            raise ValueError("recurrenceCount must be between 1 and 120")
        if status not in {"planned", "confirmed", "cancelled", "completed"}:
            raise ValueError("unsupported commitment status")
        if source not in {"owner", "document", "connector", "deterministic", "import"}:
            raise ValueError("unsupported commitment source")
        now = datetime.now(UTC).isoformat()
        value_id = commitment_id or _stable_id(
            "commit", workspace_id, label.strip(), due.isoformat(), str(amount_minor), now
        )
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT workspace_id FROM cash_commitments WHERE commitment_id = ?",
                (value_id,),
            ).fetchone()
            if existing is not None and str(existing["workspace_id"]) != workspace_id:
                raise ValueError("commitment belongs to another workspace")
            connection.execute(
                """
                INSERT INTO cash_commitments(
                    commitment_id, workspace_id, label, amount_minor, currency,
                    due_on, recurrence, recurrence_count, status, source,
                    evidence_ids_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'NZD', ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(commitment_id) DO UPDATE SET
                    label = excluded.label,
                    amount_minor = excluded.amount_minor,
                    due_on = excluded.due_on,
                    recurrence = excluded.recurrence,
                    recurrence_count = excluded.recurrence_count,
                    status = excluded.status,
                    source = excluded.source,
                    evidence_ids_json = excluded.evidence_ids_json,
                    updated_at = excluded.updated_at
                """,
                (
                    value_id,
                    workspace_id,
                    label.strip()[:240],
                    amount_minor,
                    due.isoformat(),
                    recurrence,
                    recurrence_count,
                    status,
                    source,
                    canonical_json(list(dict.fromkeys(evidence_ids))),
                    now,
                    now,
                ),
            )
        row = self.store.fetch_one(
            "SELECT * FROM cash_commitments WHERE commitment_id = ?", (value_id,)
        )
        if row is None:
            raise RuntimeError("commitment was not persisted")
        return self._row(row)

    def cancel(self, *, workspace_id: str, commitment_id: str) -> CashCommitment:
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE cash_commitments SET status = 'cancelled', updated_at = ?
                WHERE workspace_id = ? AND commitment_id = ?
                """,
                (now, workspace_id, commitment_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(commitment_id)
        row = self.store.fetch_one(
            "SELECT * FROM cash_commitments WHERE commitment_id = ?", (commitment_id,)
        )
        assert row is not None
        return self._row(row)

    @staticmethod
    def _expanded_events(
        commitments: tuple[CashCommitment, ...],
        *,
        start: date,
        end: date,
        delay_days: dict[str, int] | None = None,
        excluded_ids: frozenset[str] = frozenset(),
    ) -> tuple[ForecastEvent, ...]:
        delay_days = delay_days or {}
        events: list[ForecastEvent] = []
        for commitment in commitments:
            if commitment.commitment_id in excluded_ids:
                continue
            due = date.fromisoformat(commitment.due_on) + timedelta(
                days=delay_days.get(commitment.commitment_id, 0)
            )
            for occurrence in range(commitment.recurrence_count):
                if start < due <= end:
                    label = commitment.label
                    if commitment.recurrence != "none":
                        label = f"{label} ({occurrence + 1})"
                    events.append(
                        ForecastEvent(
                            due.isoformat(), label, commitment.amount_minor
                        )
                    )
                if commitment.recurrence == "weekly":
                    due += timedelta(days=7)
                elif commitment.recurrence == "monthly":
                    due = _month_later(due)
                else:
                    break
        return tuple(sorted(events, key=lambda event: (event.date, event.label)))

    def forecast(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        current_balance_minor: int,
        protected_reserve_minor: int,
        horizon_days: int = 30,
        delay_days: dict[str, int] | None = None,
        excluded_ids: frozenset[str] = frozenset(),
    ) -> CashForecast:
        if not 1 <= horizon_days <= 365:
            raise ValueError("horizonDays must be between 1 and 365")
        workspace = connection.execute(
            "SELECT data_through FROM workspaces WHERE workspace_id = ?",
            (workspace_id,),
        ).fetchone()
        if workspace is None:
            raise KeyError(workspace_id)
        start = date.fromisoformat(str(workspace["data_through"])[:10])
        end = start + timedelta(days=horizon_days)
        rows = connection.execute(
            """
            SELECT * FROM cash_commitments
            WHERE workspace_id = ? AND status IN ('planned', 'confirmed')
            ORDER BY due_on, commitment_id
            """,
            (workspace_id,),
        ).fetchall()
        commitments = tuple(self._row(row) for row in rows)
        events = self._expanded_events(
            commitments,
            start=start,
            end=end,
            delay_days=delay_days,
            excluded_ids=excluded_ids,
        )
        assumptions = tuple(
            f"{commitment.label}: {commitment.status}; due {commitment.due_on}; "
            f"{commitment.recurrence} recurrence x{commitment.recurrence_count}."
            for commitment in commitments
            if commitment.commitment_id not in excluded_ids
        ) or ("No planned or confirmed cash commitments fall within this horizon.",)
        return project_cash(
            current_balance_minor=current_balance_minor,
            protected_reserve_minor=protected_reserve_minor,
            start_date=start.isoformat(),
            events=events,
            assumptions=assumptions,
            alternative_excluded_label="__none__",
        )

    def scenario(
        self,
        *,
        workspace_id: str,
        title: str,
        horizon_days: int,
        delay_days: dict[str, int] | None = None,
        excluded_ids: frozenset[str] = frozenset(),
        commit: bool = False,
    ) -> dict[str, Any]:
        with self.store.connect() as connection:
            workspace = connection.execute(
                """
                SELECT protected_reserve_minor, data_through
                FROM workspaces WHERE workspace_id = ?
                """,
                (workspace_id,),
            ).fetchone()
            if workspace is None:
                raise KeyError(workspace_id)
            balance_row = connection.execute(
                """
                SELECT COALESCE(SUM(amount_minor), 0) AS balance
                FROM transactions WHERE workspace_id = ? AND status = 'posted'
                """,
                (workspace_id,),
            ).fetchone()
            baseline = self.forecast(
                connection,
                workspace_id=workspace_id,
                current_balance_minor=int(balance_row["balance"]),
                protected_reserve_minor=int(workspace["protected_reserve_minor"]),
                horizon_days=horizon_days,
            )
            alternative = self.forecast(
                connection,
                workspace_id=workspace_id,
                current_balance_minor=int(balance_row["balance"]),
                protected_reserve_minor=int(workspace["protected_reserve_minor"]),
                horizon_days=horizon_days,
                delay_days=delay_days,
                excluded_ids=excluded_ids,
            )
        overrides = {
            "delayDays": delay_days or {},
            "excludedCommitmentIds": sorted(excluded_ids),
        }
        payload = {
            "scenarioVersion": "cash.scenario@1",
            "workspaceId": workspace_id,
            "title": title.strip()[:240] or "Cash scenario",
            "baseAsOf": str(workspace["data_through"]),
            "horizonDays": horizon_days,
            "currency": "NZD",
            "baseline": {
                "lowPointMinor": baseline.low_point_minor,
                "reserveShortfallMinor": baseline.reserve_shortfall_minor,
                "points": [point.as_contract() for point in baseline.points],
                "assumptions": list(baseline.assumptions),
            },
            "alternative": {
                "lowPointMinor": alternative.low_point_minor,
                "reserveShortfallMinor": alternative.reserve_shortfall_minor,
                "points": [point.as_contract() for point in alternative.points],
                "assumptions": list(alternative.assumptions),
            },
            "overrides": overrides,
        }
        encoded = canonical_json(payload)
        scenario_id = _stable_id("scenario", workspace_id, title, encoded)
        if commit:
            now = datetime.now(UTC).isoformat()
            with self.store.transaction() as connection:
                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(revision), 0) AS revision
                    FROM cash_scenario_revisions WHERE scenario_id = ?
                    """,
                    (scenario_id,),
                ).fetchone()
                revision = int(row["revision"]) + 1
                connection.execute(
                    """
                    INSERT INTO cash_scenario_revisions(
                        scenario_id, revision, workspace_id, title, base_as_of,
                        horizon_days, overrides_json, payload_json, content_hash,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scenario_id,
                        revision,
                        workspace_id,
                        payload["title"],
                        payload["baseAsOf"],
                        horizon_days,
                        canonical_json(overrides),
                        encoded,
                        hashlib.sha256(encoded.encode()).hexdigest(),
                        now,
                    ),
                )
            payload["scenarioId"] = scenario_id
            payload["revision"] = revision
        return payload
'''

ENGINE_METHOD = '''    def _workspace_forecast(
        self,
        connection: sqlite3.Connection,
        current_balance_minor: int,
        protected_reserve_minor: int,
    ) -> CashForecast:
        return WorkspaceCommitmentService(self.store).forecast(
            connection,
            workspace_id=WORKSPACE_ID,
            current_balance_minor=current_balance_minor,
            protected_reserve_minor=protected_reserve_minor,
            horizon_days=30,
        )
'''

SERVICE_METHODS = '''    async def list_cash_commitments(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return tuple(
            commitment.as_dict()
            for commitment in WorkspaceCommitmentService(self.store).list(workspace_id)
        )

    async def upsert_cash_commitment(
        self,
        *,
        workspace_id: str,
        commitment_id: str | None,
        label: str,
        amount_minor: int,
        due_on: str,
        recurrence: str,
        recurrence_count: int,
        status: str,
        evidence_ids: tuple[str, ...],
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            commitment = WorkspaceCommitmentService(self.store).upsert(
                workspace_id=workspace_id,
                commitment_id=commitment_id,
                label=label,
                amount_minor=amount_minor,
                due_on=due_on,
                recurrence=recurrence,  # type: ignore[arg-type]
                recurrence_count=recurrence_count,
                status=status,  # type: ignore[arg-type]
                source="owner",
                evidence_ids=evidence_ids,
            )
            result = self.daily_close.run()
            self.working_understanding.ensure_current(workspace_id=workspace_id)
            self._register_daily_close_events(result)
            snapshot = self.workspace_snapshot_sync(workspace_id)
        return {
            **commitment.as_dict(),
            "snapshotId": snapshot["snapshotId"],
            "dailyCloseRunId": result.run_id,
        }

    async def cancel_cash_commitment(
        self, *, workspace_id: str, commitment_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            commitment = WorkspaceCommitmentService(self.store).cancel(
                workspace_id=workspace_id,
                commitment_id=commitment_id,
            )
            result = self.daily_close.run()
            self.working_understanding.ensure_current(workspace_id=workspace_id)
            self._register_daily_close_events(result)
            snapshot = self.workspace_snapshot_sync(workspace_id)
        return {
            **commitment.as_dict(),
            "snapshotId": snapshot["snapshotId"],
            "dailyCloseRunId": result.run_id,
        }

    async def run_workspace_cash_scenario(
        self,
        *,
        workspace_id: str,
        title: str,
        horizon_days: int,
        delay_days: Mapping[str, int],
        excluded_ids: tuple[str, ...],
        commit: bool,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return WorkspaceCommitmentService(self.store).scenario(
            workspace_id=workspace_id,
            title=title,
            horizon_days=horizon_days,
            delay_days={key: int(value) for key, value in delay_days.items()},
            excluded_ids=frozenset(excluded_ids),
            commit=commit,
        )
'''

ROUTE_MODELS = '''

class CashCommitmentRequest(RequestModel):
    commitment_id: str | None = Field(
        default=None, alias="commitmentId", pattern=IDENTIFIER_PATTERN
    )
    label: str = Field(min_length=1, max_length=240)
    amount_minor: int = Field(alias="amountMinor")
    due_on: date = Field(alias="dueOn")
    recurrence: str = Field(default="none", pattern=r"^(none|weekly|monthly)$")
    recurrence_count: int = Field(default=1, alias="recurrenceCount", ge=1, le=120)
    status: str = Field(default="planned", pattern=r"^(planned|confirmed|cancelled|completed)$")
    evidence_ids: list[str] = Field(default_factory=list, alias="evidenceIds", max_length=100)


class CashScenarioRequest(RequestModel):
    title: str = Field(min_length=1, max_length=240)
    horizon_days: int = Field(default=30, alias="horizonDays", ge=1, le=365)
    delay_days: dict[str, int] = Field(default_factory=dict, alias="delayDays")
    excluded_commitment_ids: list[str] = Field(
        default_factory=list, alias="excludedCommitmentIds", max_length=100
    )
    commit: bool = False
'''

ROUTES = '''    @router.get("/v1/workspaces/{workspace_id}/cash-commitments")
    async def list_cash_commitments(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        values = await services.list_cash_commitments(workspace_id=workspace_id)
        return {"workspaceId": workspace_id, "commitments": list(values)}

    @router.post("/v1/workspaces/{workspace_id}/cash-commitments")
    async def upsert_cash_commitment(
        workspace_id: PathIdentifier,
        body: CashCommitmentRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.upsert_cash_commitment(
                    workspace_id=workspace_id,
                    commitment_id=body.commitment_id,
                    label=body.label,
                    amount_minor=body.amount_minor,
                    due_on=body.due_on.isoformat(),
                    recurrence=body.recurrence,
                    recurrence_count=body.recurrence_count,
                    status=body.status,
                    evidence_ids=tuple(body.evidence_ids),
                )
            )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/workspaces/{workspace_id}/cash-commitments/{commitment_id}/cancel")
    async def cancel_cash_commitment(
        workspace_id: PathIdentifier,
        commitment_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.cancel_cash_commitment(
                    workspace_id=workspace_id,
                    commitment_id=commitment_id,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="commitment not found") from exc

    @router.post("/v1/workspaces/{workspace_id}/cash-scenarios")
    async def run_workspace_cash_scenario(
        workspace_id: PathIdentifier,
        body: CashScenarioRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.run_workspace_cash_scenario(
                    workspace_id=workspace_id,
                    title=body.title,
                    horizon_days=body.horizon_days,
                    delay_days=body.delay_days,
                    excluded_ids=tuple(body.excluded_commitment_ids),
                    commit=body.commit,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.finance.commitments import WorkspaceCommitmentService
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore
from finance_agent.storage.state_identity import material_state_hash

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def seeded(tmp_path: Path) -> tuple[SQLiteStore, FinanceEngine, WorkspaceCommitmentService]:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    DailyCloseService(engine).run()
    return store, engine, WorkspaceCommitmentService(store)


def test_demo_commitments_reproduce_the_existing_forecast(tmp_path: Path) -> None:
    store, engine, commitments = seeded(tmp_path)
    values = commitments.list("ws_koru_studio")
    assert [value.commitment_id for value in values] == [
        "commit_koru_client_payment",
        "commit_koru_studio_rent",
        "commit_koru_adobe",
        "commit_koru_xero",
        "commit_koru_laptop",
        "commit_koru_figma",
    ]
    snapshot = engine.get_snapshot()
    assert snapshot["totals"]["projectedLowPointMinor"] == 190077
    assert snapshot["totals"]["reserveShortfallMinor"] == 9923


def test_commitment_change_updates_material_identity_and_daily_close(tmp_path: Path) -> None:
    store, engine, commitments = seeded(tmp_path)
    before = material_state_hash(store, workspace_id="ws_koru_studio")
    commitments.upsert(
        workspace_id="ws_koru_studio",
        commitment_id="commit_new_invoice",
        label="Expected second client payment",
        amount_minor=200000,
        due_on="2026-08-01",
        status="confirmed",
    )
    after = material_state_hash(store, workspace_id="ws_koru_studio")
    assert after != before
    result = DailyCloseService(engine).run()
    assert result.status == "completed"
    assert engine.get_snapshot()["totals"]["projectedLowPointMinor"] == 390077


def test_recurrence_expands_within_horizon_and_cancel_removes_it(tmp_path: Path) -> None:
    store, _engine, commitments = seeded(tmp_path)
    recurring = commitments.upsert(
        workspace_id="ws_koru_studio",
        commitment_id="commit_weekly_cost",
        label="Weekly contractor cost",
        amount_minor=-10000,
        due_on="2026-07-20",
        recurrence="weekly",
        recurrence_count=4,
        status="confirmed",
    )
    with store.connect() as connection:
        forecast = commitments.forecast(
            connection,
            workspace_id="ws_koru_studio",
            current_balance_minor=504576,
            protected_reserve_minor=200000,
        )
    labels = [point.label for point in forecast.points]
    assert "Weekly contractor cost (1)" in labels
    assert "Weekly contractor cost (4)" in labels
    commitments.cancel(
        workspace_id="ws_koru_studio",
        commitment_id=recurring.commitment_id,
    )
    with store.connect() as connection:
        cancelled = commitments.forecast(
            connection,
            workspace_id="ws_koru_studio",
            current_balance_minor=504576,
            protected_reserve_minor=200000,
        )
    assert all("Weekly contractor" not in point.label for point in cancelled.points)


def test_scenario_delays_or_excludes_named_commitments_without_mutating_baseline(tmp_path: Path) -> None:
    _store, _engine, commitments = seeded(tmp_path)
    delayed = commitments.scenario(
        workspace_id="ws_koru_studio",
        title="Delay laptop by 14 days",
        horizon_days=30,
        delay_days={"commit_koru_laptop": 14},
        commit=True,
    )
    assert delayed["baseline"]["lowPointMinor"] == 190077
    assert delayed["alternative"]["lowPointMinor"] > delayed["baseline"]["lowPointMinor"]
    excluded = commitments.scenario(
        workspace_id="ws_koru_studio",
        title="Remove laptop",
        horizon_days=30,
        excluded_ids=frozenset({"commit_koru_laptop"}),
    )
    assert excluded["alternative"]["lowPointMinor"] == 490077
    assert commitments.list("ws_koru_studio")[4].status == "planned"
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


def add_commitment_module() -> None:
    write("services/api/src/finance_agent/finance/commitments.py", COMMITMENTS)
    path = "services/api/src/finance_agent/finance/service.py"
    content = read(path)
    marker = "from .classification import (\n"
    import_line = "from .commitments import WorkspaceCommitmentService, seed_demo_commitments\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("classification import marker missing")
        content = content.replace(marker, import_line + marker, 1)
    seed_marker = "                (WORKSPACE_ID, POLICY_VERSION, DEMO_CREATED_AT),\n            )\n"
    if "seed_demo_commitments(connection, WORKSPACE_ID)" not in content:
        if seed_marker not in content:
            raise RuntimeError("job definition seed marker missing")
        content = content.replace(
            seed_marker,
            seed_marker + "            seed_demo_commitments(connection, WORKSPACE_ID)\n",
            1,
        )
    write(path, content)
    insert_method_before(path, "FinanceEngine", "preview_state", ENGINE_METHOD)
    count = replace_function_calls(path, "koru_30_day_forecast", "self._workspace_forecast")
    if count < 1:
        raise RuntimeError("no legacy forecast calls were replaced")


def update_state_identity() -> None:
    path = "services/api/src/finance_agent/storage/state_identity.py"
    content = read(path)
    marker = '''    provider_events = _rows(\n        store,\n        """\n        SELECT event_id, provider, provider_account_id, provider_transaction_id,\n               change_type, provider_cursor, mapping_version, payload_hash, received_at\n        FROM provider_transaction_events\n        WHERE workspace_id = ?\n        ORDER BY provider, provider_account_id, provider_transaction_id, received_at, event_id\n        """,\n        (workspace_id,),\n    )\n'''
    addition = marker + '''    commitments = _rows(\n        store,\n        """\n        SELECT commitment_id, label, amount_minor, currency, due_on, recurrence,\n               recurrence_count, status, source, evidence_ids_json, updated_at\n        FROM cash_commitments\n        WHERE workspace_id = ?\n        ORDER BY due_on, commitment_id\n        """,\n        (workspace_id,),\n    )\n'''
    if "commitments = _rows(" not in content:
        if marker not in content:
            raise RuntimeError("provider events identity marker missing")
        content = content.replace(marker, addition, 1)
    payload_marker = '        "providerEvents": provider_events,\n'
    if '"cashCommitments": commitments' not in content:
        if payload_marker not in content:
            raise RuntimeError("state identity payload marker missing")
        content = content.replace(
            payload_marker,
            payload_marker + '        "cashCommitments": commitments,\n',
            1,
        )
    write(path, content)


def update_api() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance import FinanceEngine, FinanceStateError, FinanceTotals\n"
    import_line = "from finance_agent.finance.commitments import WorkspaceCommitmentService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("finance import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "record_operation_metric", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def record_operation_metric(\n"
    addition = '''    async def list_cash_commitments(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def upsert_cash_commitment(\n        self, *, workspace_id: str, commitment_id: str | None, label: str,\n        amount_minor: int, due_on: str, recurrence: str, recurrence_count: int,\n        status: str, evidence_ids: tuple[str, ...]\n    ) -> Mapping[str, object]: ...\n\n    async def cancel_cash_commitment(\n        self, *, workspace_id: str, commitment_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def run_workspace_cash_scenario(\n        self, *, workspace_id: str, title: str, horizon_days: int,\n        delay_days: Mapping[str, int], excluded_ids: tuple[str, ...], commit: bool\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("observability protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass CompleteNotificationRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("notification request marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.get(\n        "/v1/diagnostics/operations",\n'
    if route_marker not in content:
        raise RuntimeError("operation diagnostics marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/finance/test_persisted_cash_commitments.py", TESTS)
    write("docs/CASH_COMMITMENTS.md", '''# Cash commitments and scenarios\n\nFolio forecasts cash from persisted commitments rather than source-code constants. A commitment has an exact NZD minor-unit amount, due date, bounded recurrence, explicit status, source class and evidence identifiers. Planned and confirmed commitments affect the forecast; cancelled and completed commitments do not.\n\nDaily Close identity includes commitments, so a change creates a fresh deterministic close rather than reusing stale results. Scenarios can delay or exclude named commitments without mutating the baseline. Committed scenario revisions preserve the exact overrides, points, assumptions and content hash.\n\nA cash scenario is a transparent calculation over known commitments, not predictive certainty. Folio does not infer that an invoice will be paid, a recurring expense will continue, or a planned purchase will happen unless the corresponding commitment remains explicit.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 19: persisted cash commitments and general scenarios\n\n- The Koru forecast inputs move from source-code constants into persisted commitments.\n- Commitments carry exact amounts, due dates, bounded recurrence, status, source and evidence.\n- Commitment changes alter Daily Close material identity and rebuild deterministic projections.\n- Scenarios delay or exclude named commitments without mutating the baseline.\n- Scenario revisions preserve overrides, points, assumptions and content hashes.\n- Forecasts remain transparent calculations over explicit commitments rather than predictions.\n'''
    if "## Stack 19: persisted cash commitments and general scenarios" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration()
    add_commitment_module()
    update_state_identity()
    update_api()
    add_tests_and_docs()
    print("persisted cash commitment changes applied")


if __name__ == "__main__":
    main()
