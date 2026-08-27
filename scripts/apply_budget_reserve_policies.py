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
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    before = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


MIGRATION = '''    Migration(
        version={version},
        name="budget_and_reserve_policies",
        sql="""
        CREATE TABLE category_budget_policy_revisions (
            policy_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            category TEXT NOT NULL CHECK (length(trim(category)) BETWEEN 1 AND 200),
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            limit_minor INTEGER NOT NULL CHECK (limit_minor >= 0),
            warning_basis_points INTEGER NOT NULL CHECK (
                warning_basis_points BETWEEN 1 AND 10000
            ),
            status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'cancelled')),
            source TEXT NOT NULL CHECK (source IN ('owner', 'accountant', 'import')),
            evidence_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (policy_id, revision),
            CHECK (period_start <= period_end)
        );

        CREATE UNIQUE INDEX category_budget_one_active
            ON category_budget_policy_revisions(workspace_id, category, period_start, period_end)
            WHERE status = 'active';

        CREATE TABLE reserve_policy_revisions (
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            protected_reserve_minor INTEGER NOT NULL CHECK (protected_reserve_minor >= 0),
            rationale TEXT NOT NULL CHECK (length(trim(rationale)) BETWEEN 1 AND 500),
            source TEXT NOT NULL CHECK (source IN ('owner', 'accountant', 'import')),
            evidence_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, revision)
        );

        CREATE INDEX category_budget_workspace_period
            ON category_budget_policy_revisions(
                workspace_id, period_start, period_end, category, revision
            );
        """,
    ),
'''

MODULE = '''"""Versioned budget and protected-reserve policies with deterministic reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    policy_id: str
    revision: int
    workspace_id: str
    category: str
    period_start: str
    period_end: str
    limit_minor: int
    warning_basis_points: int
    status: str
    source: str
    evidence_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "policyId": self.policy_id,
            "revision": self.revision,
            "workspaceId": self.workspace_id,
            "category": self.category,
            "periodStart": self.period_start,
            "periodEnd": self.period_end,
            "limitMinor": self.limit_minor,
            "warningBasisPoints": self.warning_basis_points,
            "status": self.status,
            "source": self.source,
            "evidenceIds": list(self.evidence_ids),
        }


class BudgetReservePolicyService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _budget(row: Any) -> BudgetPolicy:
        return BudgetPolicy(
            policy_id=str(row["policy_id"]),
            revision=int(row["revision"]),
            workspace_id=str(row["workspace_id"]),
            category=str(row["category"]),
            period_start=str(row["period_start"]),
            period_end=str(row["period_end"]),
            limit_minor=int(row["limit_minor"]),
            warning_basis_points=int(row["warning_basis_points"]),
            status=str(row["status"]),
            source=str(row["source"]),
            evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
        )

    def set_budget(
        self,
        *,
        workspace_id: str,
        category: str,
        period_start: str,
        period_end: str,
        limit_minor: int,
        warning_basis_points: int = 8000,
        source: str = "owner",
        evidence_ids: tuple[str, ...] = (),
    ) -> BudgetPolicy:
        category_value = category.strip()
        if not category_value:
            raise ValueError("budget category must not be blank")
        try:
            start = date.fromisoformat(period_start)
            end = date.fromisoformat(period_end)
        except ValueError as exc:
            raise ValueError("budget dates must use YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("budget period start must be on or before end")
        if isinstance(limit_minor, bool) or not isinstance(limit_minor, int) or limit_minor < 0:
            raise ValueError("budget limitMinor must be a non-negative integer")
        if not 1 <= warning_basis_points <= 10000:
            raise ValueError("warningBasisPoints must be between 1 and 10000")
        if source not in {"owner", "accountant", "import"}:
            raise ValueError("unsupported budget source")
        policy_id = _stable_id(
            "budget", workspace_id, category_value, start.isoformat(), end.isoformat()
        )
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            current = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS revision
                FROM category_budget_policy_revisions WHERE policy_id = ?
                """,
                (policy_id,),
            ).fetchone()
            revision = int(current["revision"]) + 1
            connection.execute(
                """
                UPDATE category_budget_policy_revisions SET status = 'superseded'
                WHERE policy_id = ? AND status = 'active'
                """,
                (policy_id,),
            )
            connection.execute(
                """
                INSERT INTO category_budget_policy_revisions(
                    policy_id, revision, workspace_id, category, period_start,
                    period_end, limit_minor, warning_basis_points, status,
                    source, evidence_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
                """,
                (
                    policy_id,
                    revision,
                    workspace_id,
                    category_value[:200],
                    start.isoformat(),
                    end.isoformat(),
                    limit_minor,
                    warning_basis_points,
                    source,
                    canonical_json(list(dict.fromkeys(evidence_ids))),
                    now,
                ),
            )
        row = self.store.fetch_one(
            "SELECT * FROM category_budget_policy_revisions WHERE policy_id = ? AND revision = ?",
            (policy_id, revision),
        )
        assert row is not None
        return self._budget(row)

    def cancel_budget(self, *, workspace_id: str, policy_id: str) -> BudgetPolicy:
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            current = connection.execute(
                """
                SELECT * FROM category_budget_policy_revisions
                WHERE workspace_id = ? AND policy_id = ? AND status = 'active'
                ORDER BY revision DESC LIMIT 1
                """,
                (workspace_id, policy_id),
            ).fetchone()
            if current is None:
                raise KeyError(policy_id)
            connection.execute(
                "UPDATE category_budget_policy_revisions SET status = 'superseded' WHERE policy_id = ? AND revision = ?",
                (policy_id, current["revision"]),
            )
            revision = int(current["revision"]) + 1
            connection.execute(
                """
                INSERT INTO category_budget_policy_revisions(
                    policy_id, revision, workspace_id, category, period_start,
                    period_end, limit_minor, warning_basis_points, status,
                    source, evidence_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'cancelled', ?, ?, ?)
                """,
                (
                    policy_id,
                    revision,
                    workspace_id,
                    current["category"],
                    current["period_start"],
                    current["period_end"],
                    current["limit_minor"],
                    current["warning_basis_points"],
                    current["source"],
                    current["evidence_ids_json"],
                    now,
                ),
            )
        row = self.store.fetch_one(
            "SELECT * FROM category_budget_policy_revisions WHERE policy_id = ? AND revision = ?",
            (policy_id, revision),
        )
        assert row is not None
        return self._budget(row)

    def set_reserve(
        self,
        *,
        workspace_id: str,
        protected_reserve_minor: int,
        rationale: str,
        source: str = "owner",
        evidence_ids: tuple[str, ...] = (),
    ) -> dict[str, object]:
        if (
            isinstance(protected_reserve_minor, bool)
            or not isinstance(protected_reserve_minor, int)
            or protected_reserve_minor < 0
        ):
            raise ValueError("protectedReserveMinor must be a non-negative integer")
        rationale_value = rationale.strip()
        if not rationale_value:
            raise ValueError("reserve rationale must not be blank")
        if source not in {"owner", "accountant", "import"}:
            raise ValueError("unsupported reserve source")
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS revision FROM reserve_policy_revisions WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            revision = int(row["revision"]) + 1
            cursor = connection.execute(
                """
                UPDATE workspaces SET protected_reserve_minor = ?, updated_at = ?
                WHERE workspace_id = ?
                """,
                (protected_reserve_minor, now, workspace_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(workspace_id)
            connection.execute(
                """
                INSERT INTO reserve_policy_revisions(
                    workspace_id, revision, protected_reserve_minor, rationale,
                    source, evidence_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    revision,
                    protected_reserve_minor,
                    rationale_value[:500],
                    source,
                    canonical_json(list(dict.fromkeys(evidence_ids))),
                    now,
                ),
            )
        return {
            "workspaceId": workspace_id,
            "revision": revision,
            "protectedReserveMinor": protected_reserve_minor,
            "rationale": rationale_value[:500],
            "source": source,
            "evidenceIds": list(dict.fromkeys(evidence_ids)),
            "createdAt": now,
        }

    def report(
        self,
        *,
        workspace_id: str,
        as_of: str | None = None,
    ) -> dict[str, object]:
        target = date.fromisoformat(as_of) if as_of else datetime.now(UTC).date()
        policies = self.store.fetch_all(
            """
            SELECT * FROM category_budget_policy_revisions
            WHERE workspace_id = ? AND status = 'active'
              AND period_start <= ? AND period_end >= ?
            ORDER BY category, policy_id
            """,
            (workspace_id, target.isoformat(), target.isoformat()),
        )
        items: list[dict[str, object]] = []
        evidence: set[str] = set()
        for row in policies:
            spent = self.store.fetch_one(
                """
                SELECT COALESCE(SUM(ABS(amount_minor)), 0) AS spent
                FROM transactions
                WHERE workspace_id = ? AND status = 'posted'
                  AND source_status = 'posted' AND amount_minor < 0
                  AND category = ? AND occurred_on BETWEEN ? AND ?
                """,
                (
                    workspace_id,
                    row["category"],
                    row["period_start"],
                    row["period_end"],
                ),
            )
            transaction_evidence = self.store.fetch_all(
                """
                SELECT evidence_id FROM transactions
                WHERE workspace_id = ? AND status = 'posted'
                  AND source_status = 'posted' AND amount_minor < 0
                  AND category = ? AND occurred_on BETWEEN ? AND ?
                ORDER BY occurred_on, transaction_id
                """,
                (
                    workspace_id,
                    row["category"],
                    row["period_start"],
                    row["period_end"],
                ),
            )
            spent_minor = int(spent["spent"] if spent else 0)
            limit_minor = int(row["limit_minor"])
            warning_minor = (limit_minor * int(row["warning_basis_points"])) // 10000
            status = (
                "breached"
                if spent_minor > limit_minor
                else "warning"
                if spent_minor >= warning_minor
                else "within_budget"
            )
            policy_evidence = json.loads(str(row["evidence_ids_json"]))
            item_evidence = list(
                dict.fromkeys(
                    [*policy_evidence, *[str(value["evidence_id"]) for value in transaction_evidence]]
                )
            )
            evidence.update(item_evidence)
            items.append(
                {
                    "policyId": str(row["policy_id"]),
                    "revision": int(row["revision"]),
                    "category": str(row["category"]),
                    "periodStart": str(row["period_start"]),
                    "periodEnd": str(row["period_end"]),
                    "limitMinor": limit_minor,
                    "warningMinor": warning_minor,
                    "spentMinor": spent_minor,
                    "remainingMinor": limit_minor - spent_minor,
                    "usageBasisPoints": (
                        min(1000000, (spent_minor * 10000) // limit_minor)
                        if limit_minor
                        else 10000 if spent_minor else 0
                    ),
                    "status": status,
                    "evidenceIds": item_evidence,
                }
            )
        reserve = self.store.fetch_one(
            "SELECT protected_reserve_minor FROM workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        latest_reserve = self.store.fetch_one(
            """
            SELECT * FROM reserve_policy_revisions
            WHERE workspace_id = ? ORDER BY revision DESC LIMIT 1
            """,
            (workspace_id,),
        )
        return {
            "workspaceId": workspace_id,
            "asOf": target.isoformat(),
            "currency": "NZD",
            "budgets": items,
            "summary": {
                "activeBudgetCount": len(items),
                "warningCount": sum(item["status"] == "warning" for item in items),
                "breachedCount": sum(item["status"] == "breached" for item in items),
            },
            "reservePolicy": {
                "protectedReserveMinor": int(reserve["protected_reserve_minor"]) if reserve else 0,
                "revision": int(latest_reserve["revision"]) if latest_reserve else None,
                "rationale": str(latest_reserve["rationale"]) if latest_reserve else None,
                "source": str(latest_reserve["source"]) if latest_reserve else "legacy_workspace_value",
                "evidenceIds": (
                    json.loads(str(latest_reserve["evidence_ids_json"]))
                    if latest_reserve else []
                ),
            },
            "evidenceIds": sorted(evidence),
            "calculatedLocally": True,
        }
'''

SERVICE_METHODS = '''    async def set_category_budget(
        self,
        *,
        workspace_id: str,
        category: str,
        period_start: str,
        period_end: str,
        limit_minor: int,
        warning_basis_points: int,
        source: str,
        evidence_ids: tuple[str, ...],
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = BudgetReservePolicyService(self.store).set_budget(
                workspace_id=workspace_id,
                category=category,
                period_start=period_start,
                period_end=period_end,
                limit_minor=limit_minor,
                warning_basis_points=warning_basis_points,
                source=source,
                evidence_ids=evidence_ids,
            )
            result = self.daily_close.run()
            self._register_daily_close_events(result)
        return {**value.as_dict(), "dailyCloseRunId": result.run_id}

    async def cancel_category_budget(
        self, *, workspace_id: str, policy_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = BudgetReservePolicyService(self.store).cancel_budget(
                workspace_id=workspace_id, policy_id=policy_id
            )
            result = self.daily_close.run()
            self._register_daily_close_events(result)
        return {**value.as_dict(), "dailyCloseRunId": result.run_id}

    async def set_reserve_policy(
        self,
        *,
        workspace_id: str,
        protected_reserve_minor: int,
        rationale: str,
        source: str,
        evidence_ids: tuple[str, ...],
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = BudgetReservePolicyService(self.store).set_reserve(
                workspace_id=workspace_id,
                protected_reserve_minor=protected_reserve_minor,
                rationale=rationale,
                source=source,
                evidence_ids=evidence_ids,
            )
            result = self.daily_close.run()
            self._register_daily_close_events(result)
        return {**value, "dailyCloseRunId": result.run_id}

    async def budget_reserve_report(
        self, *, workspace_id: str, as_of: str | None
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return BudgetReservePolicyService(self.store).report(
            workspace_id=workspace_id, as_of=as_of
        )
'''

ROUTE_MODELS = '''

class CategoryBudgetRequest(RequestModel):
    category: str = Field(min_length=1, max_length=200)
    period_start: date = Field(alias="periodStart")
    period_end: date = Field(alias="periodEnd")
    limit_minor: int = Field(alias="limitMinor", ge=0)
    warning_basis_points: int = Field(
        default=8000, alias="warningBasisPoints", ge=1, le=10000
    )
    source: str = Field(default="owner", pattern=r"^(owner|accountant|import)$")
    evidence_ids: list[str] = Field(default_factory=list, alias="evidenceIds", max_length=100)


class ReservePolicyRequest(RequestModel):
    protected_reserve_minor: int = Field(alias="protectedReserveMinor", ge=0)
    rationale: str = Field(min_length=1, max_length=500)
    source: str = Field(default="owner", pattern=r"^(owner|accountant|import)$")
    evidence_ids: list[str] = Field(default_factory=list, alias="evidenceIds", max_length=100)
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/budgets")
    async def set_category_budget(
        workspace_id: PathIdentifier,
        body: CategoryBudgetRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.set_category_budget(
                    workspace_id=workspace_id,
                    category=body.category,
                    period_start=body.period_start.isoformat(),
                    period_end=body.period_end.isoformat(),
                    limit_minor=body.limit_minor,
                    warning_basis_points=body.warning_basis_points,
                    source=body.source,
                    evidence_ids=tuple(body.evidence_ids),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/workspaces/{workspace_id}/budgets/{policy_id}/cancel")
    async def cancel_category_budget(
        workspace_id: PathIdentifier,
        policy_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.cancel_category_budget(
                    workspace_id=workspace_id, policy_id=policy_id
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="budget policy not found") from exc

    @router.post("/v1/workspaces/{workspace_id}/reserve-policy")
    async def set_reserve_policy(
        workspace_id: PathIdentifier,
        body: ReservePolicyRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.set_reserve_policy(
                    workspace_id=workspace_id,
                    protected_reserve_minor=body.protected_reserve_minor,
                    rationale=body.rationale,
                    source=body.source,
                    evidence_ids=tuple(body.evidence_ids),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/workspaces/{workspace_id}/budget-report")
    async def budget_reserve_report(
        workspace_id: PathIdentifier,
        services: Services,
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
    ) -> dict[str, object]:
        return dict(
            await services.budget_reserve_report(
                workspace_id=workspace_id,
                as_of=as_of.isoformat() if as_of else None,
            )
        )

'''

TESTS = '''from __future__ import annotations

from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.finance.budgets import BudgetReservePolicyService
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore
from finance_agent.storage.state_identity import material_state_hash

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def service(tmp_path: Path) -> tuple[SQLiteStore, FinanceEngine, BudgetReservePolicyService]:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    DailyCloseService(engine).run()
    return store, engine, BudgetReservePolicyService(store)


def test_budget_versions_are_append_only_and_latest_active(tmp_path: Path) -> None:
    store, _engine, policies = service(tmp_path)
    first = policies.set_budget(
        workspace_id="ws_koru_studio",
        category="software_subscriptions",
        period_start="2026-07-01",
        period_end="2026-07-31",
        limit_minor=25000,
        evidence_ids=("evd_koru_bank_csv",),
    )
    second = policies.set_budget(
        workspace_id="ws_koru_studio",
        category="software_subscriptions",
        period_start="2026-07-01",
        period_end="2026-07-31",
        limit_minor=15000,
        warning_basis_points=7500,
    )
    assert first.policy_id == second.policy_id
    assert (first.revision, second.revision) == (1, 2)
    rows = store.fetch_all(
        "SELECT revision, status FROM category_budget_policy_revisions WHERE policy_id = ? ORDER BY revision",
        (first.policy_id,),
    )
    assert [(int(row["revision"]), str(row["status"])) for row in rows] == [
        (1, "superseded"), (2, "active")
    ]


def test_report_uses_posted_category_spend_and_evidence(tmp_path: Path) -> None:
    _store, _engine, policies = service(tmp_path)
    policies.set_budget(
        workspace_id="ws_koru_studio",
        category="software_subscriptions",
        period_start="2026-07-01",
        period_end="2026-07-31",
        limit_minor=15000,
        warning_basis_points=8000,
    )
    report = policies.report(
        workspace_id="ws_koru_studio", as_of="2026-07-17"
    )
    item = report["budgets"][0]
    assert item["spentMinor"] == 19499
    assert item["status"] == "breached"
    assert item["remainingMinor"] == -4499
    assert item["evidenceIds"]
    assert report["summary"]["breachedCount"] == 1
    assert report["calculatedLocally"] is True


def test_reserve_change_updates_workspace_forecast_and_material_identity(tmp_path: Path) -> None:
    store, engine, policies = service(tmp_path)
    before = material_state_hash(store, workspace_id="ws_koru_studio")
    value = policies.set_reserve(
        workspace_id="ws_koru_studio",
        protected_reserve_minor=300000,
        rationale="Keep at least one month of operating cash.",
        evidence_ids=("evd_koru_bank_csv",),
    )
    after = material_state_hash(store, workspace_id="ws_koru_studio")
    assert value["revision"] == 1
    assert after != before
    result = DailyCloseService(engine).run()
    assert result.status == "completed"
    snapshot = engine.get_snapshot()
    assert snapshot["totals"]["protectedReserveMinor"] == 300000
    assert snapshot["totals"]["reserveShortfallMinor"] == 109923


def test_cancelled_budget_disappears_from_active_report(tmp_path: Path) -> None:
    _store, _engine, policies = service(tmp_path)
    budget = policies.set_budget(
        workspace_id="ws_koru_studio",
        category="studio_rent",
        period_start="2026-07-01",
        period_end="2026-07-31",
        limit_minor=130000,
    )
    cancelled = policies.cancel_budget(
        workspace_id="ws_koru_studio", policy_id=budget.policy_id
    )
    assert cancelled.status == "cancelled"
    report = policies.report(
        workspace_id="ws_koru_studio", as_of="2026-07-17"
    )
    assert report["budgets"] == []
'''


def add_migration_module() -> None:
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
    write("services/api/src/finance_agent/finance/budgets.py", MODULE)


def update_state_identity() -> None:
    path = "services/api/src/finance_agent/storage/state_identity.py"
    content = read(path)
    marker = '''    settlements = _rows(
        store,
        """
        SELECT event_id, invoice_id, transaction_id, candidate_id, actor,
               reason, evidence_ids_json, occurred_at
        FROM invoice_settlement_events
        WHERE workspace_id = ?
        ORDER BY occurred_at, event_id
        """,
        (workspace_id,),
    )
'''
    addition = marker + '''    budget_policies = _rows(
        store,
        """
        SELECT policy_id, revision, category, period_start, period_end,
               limit_minor, warning_basis_points, status, source,
               evidence_ids_json, created_at
        FROM category_budget_policy_revisions
        WHERE workspace_id = ?
        ORDER BY policy_id, revision
        """,
        (workspace_id,),
    )
    reserve_policies = _rows(
        store,
        """
        SELECT revision, protected_reserve_minor, rationale, source,
               evidence_ids_json, created_at
        FROM reserve_policy_revisions
        WHERE workspace_id = ? ORDER BY revision
        """,
        (workspace_id,),
    )
'''
    if "budget_policies = _rows(" not in content:
        if marker not in content:
            raise RuntimeError("settlement identity marker missing")
        content = content.replace(marker, addition, 1)
    payload_marker = '        "invoiceSettlements": settlements,\n'
    if '"budgetPolicies": budget_policies' not in content:
        if payload_marker not in content:
            raise RuntimeError("settlement payload marker missing")
        content = content.replace(
            payload_marker,
            payload_marker
            + '        "budgetPolicies": budget_policies,\n'
            + '        "reservePolicies": reserve_policies,\n',
            1,
        )
    write(path, content)


def update_services_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.receivables import ReceivablesService\n"
    import_line = "from finance_agent.finance.budgets import BudgetReservePolicyService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("receivables import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "scan_receivable_candidates", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def scan_receivable_candidates(\n"
    addition = '''    async def set_category_budget(\n        self, *, workspace_id: str, category: str, period_start: str,\n        period_end: str, limit_minor: int, warning_basis_points: int,\n        source: str, evidence_ids: tuple[str, ...]\n    ) -> Mapping[str, object]: ...\n\n    async def cancel_category_budget(\n        self, *, workspace_id: str, policy_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def set_reserve_policy(\n        self, *, workspace_id: str, protected_reserve_minor: int,\n        rationale: str, source: str, evidence_ids: tuple[str, ...]\n    ) -> Mapping[str, object]: ...\n\n    async def budget_reserve_report(\n        self, *, workspace_id: str, as_of: str | None\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("receivables protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass SettlementConfirmationRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("SettlementConfirmationRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.post("/v1/workspaces/{workspace_id}/receivables/scan")\n'
    if route_marker not in content:
        raise RuntimeError("receivables route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/finance/test_budget_reserve_policies.py", TESTS)
    write("docs/BUDGETS_AND_RESERVES.md", '''# Budgets and protected reserve policies\n\nFolio stores category budgets as append-only policy revisions. A policy applies to one named category and one inclusive date period, uses integer NZD minor units, and records its warning threshold, source and evidence. Replacing a budget supersedes the prior revision; cancellation appends a cancelled revision.\n\nBudget reports sum only posted expense transactions in the selected category and period. They expose the exact limit, warning amount, spend, remaining amount, usage basis points, status and linked evidence. `within_budget`, `warning` and `breached` are deterministic comparisons, not model opinions.\n\nProtected reserve changes also append a policy revision with rationale and evidence, then update the workspace's deterministic reserve value. A policy change alters Daily Close material identity and recomputes the cash forecast. Folio does not infer an appropriate budget or reserve without an owner, accountant or imported policy.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 28: versioned budget and reserve policies\n\n- Category budgets are period-scoped, exact-money, sourced and append-only.\n- Replacements supersede prior revisions and cancellation is explicit.\n- Reports use posted category spend and retain transaction/policy evidence.\n- Warning and breach states are deterministic comparisons.\n- Reserve changes carry rationale and evidence and rebuild Daily Close projections.\n- Folio does not invent budgets or reserves without an explicit policy source.\n'''
    if "## Stack 28: versioned budget and reserve policies" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_state_identity()
    update_services_protocol_routes()
    tests_docs()
    print("budget and reserve policy changes applied")


if __name__ == "__main__":
    main()
