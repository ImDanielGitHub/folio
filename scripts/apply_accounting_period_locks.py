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
        name="accounting_period_locks",
        sql="""
        CREATE TABLE accounting_period_revisions (
            period_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('open', 'soft_locked', 'hard_locked')),
            actor TEXT NOT NULL CHECK (actor IN ('owner', 'accountant', 'system')),
            reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
            created_at TEXT NOT NULL,
            PRIMARY KEY (period_id, revision),
            CHECK (period_start <= period_end)
        );

        CREATE TABLE accounting_period_close_receipts (
            receipt_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            period_id TEXT NOT NULL,
            period_revision INTEGER NOT NULL,
            material_state_hash TEXT NOT NULL CHECK (length(material_state_hash) = 64),
            totals_json TEXT NOT NULL,
            source_digests_json TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (period_id, period_revision),
            FOREIGN KEY (period_id, period_revision)
                REFERENCES accounting_period_revisions(period_id, revision)
        );

        CREATE INDEX accounting_period_workspace_dates
            ON accounting_period_revisions(
                workspace_id, period_start, period_end, revision DESC
            );

        CREATE TRIGGER transactions_no_insert_hard_locked_period
        BEFORE INSERT ON transactions
        WHEN EXISTS (
            SELECT 1 FROM accounting_period_revisions p
            WHERE p.workspace_id = NEW.workspace_id
              AND p.period_start <= NEW.occurred_on
              AND p.period_end >= NEW.occurred_on
              AND p.revision = (
                SELECT MAX(p2.revision) FROM accounting_period_revisions p2
                WHERE p2.period_id = p.period_id
              )
              AND p.status = 'hard_locked'
        )
        BEGIN
            SELECT RAISE(ABORT, 'accounting period is hard locked');
        END;

        CREATE TRIGGER transactions_no_material_update_hard_locked_period
        BEFORE UPDATE OF occurred_on, amount_minor, currency, status,
            classification, category, classification_source, rule_id,
            duplicate_of_transaction_id
        ON transactions
        WHEN EXISTS (
            SELECT 1 FROM accounting_period_revisions p
            WHERE p.workspace_id = OLD.workspace_id
              AND p.period_start <= OLD.occurred_on
              AND p.period_end >= OLD.occurred_on
              AND p.revision = (
                SELECT MAX(p2.revision) FROM accounting_period_revisions p2
                WHERE p2.period_id = p.period_id
              )
              AND p.status = 'hard_locked'
        )
        BEGIN
            SELECT RAISE(ABORT, 'accounting period is hard locked');
        END;
        """,
    ),
'''

MODULE = '''"""Append-only accounting period locks and exact close receipts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json
from finance_agent.storage.state_identity import material_state_hash


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


@dataclass(frozen=True, slots=True)
class AccountingPeriod:
    period_id: str
    revision: int
    workspace_id: str
    period_start: str
    period_end: str
    status: str
    actor: str
    reason: str
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "periodId": self.period_id,
            "revision": self.revision,
            "workspaceId": self.workspace_id,
            "periodStart": self.period_start,
            "periodEnd": self.period_end,
            "status": self.status,
            "actor": self.actor,
            "reason": self.reason,
            "createdAt": self.created_at,
        }


class AccountingPeriodService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _period(row: Any) -> AccountingPeriod:
        return AccountingPeriod(
            period_id=str(row["period_id"]),
            revision=int(row["revision"]),
            workspace_id=str(row["workspace_id"]),
            period_start=str(row["period_start"]),
            period_end=str(row["period_end"]),
            status=str(row["status"]),
            actor=str(row["actor"]),
            reason=str(row["reason"]),
            created_at=str(row["created_at"]),
        )

    def latest(self, workspace_id: str) -> tuple[AccountingPeriod, ...]:
        rows = self.store.fetch_all(
            """
            SELECT p.* FROM accounting_period_revisions p
            WHERE p.workspace_id = ? AND p.revision = (
                SELECT MAX(p2.revision) FROM accounting_period_revisions p2
                WHERE p2.period_id = p.period_id
            )
            ORDER BY p.period_start DESC, p.period_id
            """,
            (workspace_id,),
        )
        return tuple(self._period(row) for row in rows)

    def period_for_date(self, workspace_id: str, occurred_on: str) -> AccountingPeriod | None:
        date.fromisoformat(occurred_on)
        row = self.store.fetch_one(
            """
            SELECT p.* FROM accounting_period_revisions p
            WHERE p.workspace_id = ?
              AND p.period_start <= ? AND p.period_end >= ?
              AND p.revision = (
                SELECT MAX(p2.revision) FROM accounting_period_revisions p2
                WHERE p2.period_id = p.period_id
              )
            ORDER BY CASE p.status
                WHEN 'hard_locked' THEN 3
                WHEN 'soft_locked' THEN 2
                ELSE 1 END DESC,
                p.period_start DESC
            LIMIT 1
            """,
            (workspace_id, occurred_on, occurred_on),
        )
        return None if row is None else self._period(row)

    def assert_mutable(self, workspace_id: str, occurred_on: str) -> None:
        period = self.period_for_date(workspace_id, occurred_on)
        if period and period.status == "hard_locked":
            raise PermissionError(
                f"accounting period {period.period_start} to {period.period_end} is hard locked"
            )

    def set_status(
        self,
        *,
        workspace_id: str,
        period_start: str,
        period_end: str,
        status: str,
        actor: str,
        reason: str,
    ) -> dict[str, object]:
        try:
            start = date.fromisoformat(period_start)
            end = date.fromisoformat(period_end)
        except ValueError as exc:
            raise ValueError("accounting period dates must use YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("accounting period start must be on or before end")
        if status not in {"open", "soft_locked", "hard_locked"}:
            raise ValueError("unsupported accounting period status")
        if actor not in {"owner", "accountant", "system"}:
            raise ValueError("unsupported accounting period actor")
        reason_value = reason.strip()
        if not reason_value:
            raise ValueError("accounting period reason must not be blank")
        period_id = _stable_id(
            "period", workspace_id, start.isoformat(), end.isoformat()
        )
        overlapping = self.store.fetch_all(
            """
            SELECT p.period_id FROM accounting_period_revisions p
            WHERE p.workspace_id = ? AND p.period_id != ?
              AND p.revision = (
                SELECT MAX(p2.revision) FROM accounting_period_revisions p2
                WHERE p2.period_id = p.period_id
              )
              AND NOT (p.period_end < ? OR p.period_start > ?)
            """,
            (workspace_id, period_id, start.isoformat(), end.isoformat()),
        )
        if overlapping:
            raise ValueError("accounting periods must not overlap")
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS revision FROM accounting_period_revisions WHERE period_id = ?",
                (period_id,),
            ).fetchone()
            revision = int(row["revision"]) + 1
            connection.execute(
                """
                INSERT INTO accounting_period_revisions(
                    period_id, revision, workspace_id, period_start, period_end,
                    status, actor, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    period_id,
                    revision,
                    workspace_id,
                    start.isoformat(),
                    end.isoformat(),
                    status,
                    actor,
                    reason_value[:500],
                    now,
                ),
            )
        period_row = self.store.fetch_one(
            "SELECT * FROM accounting_period_revisions WHERE period_id = ? AND revision = ?",
            (period_id, revision),
        )
        assert period_row is not None
        period = self._period(period_row)
        receipt = self._close_receipt(period) if status == "hard_locked" else None
        return {**period.as_dict(), "closeReceipt": receipt}

    def _close_receipt(self, period: AccountingPeriod) -> dict[str, object]:
        totals_row = self.store.fetch_one(
            """
            SELECT
                COALESCE(SUM(amount_minor), 0) AS net,
                COALESCE(SUM(CASE WHEN amount_minor > 0 THEN amount_minor ELSE 0 END), 0) AS inflows,
                COALESCE(SUM(CASE WHEN amount_minor < 0 THEN ABS(amount_minor) ELSE 0 END), 0) AS outflows,
                COUNT(*) AS transaction_count,
                COALESCE(SUM(CASE WHEN classification = 'unresolved' AND amount_minor < 0 THEN ABS(amount_minor) ELSE 0 END), 0) AS unresolved
            FROM transactions
            WHERE workspace_id = ? AND status = 'posted'
              AND occurred_on BETWEEN ? AND ?
            """,
            (period.workspace_id, period.period_start, period.period_end),
        )
        sources = self.store.fetch_all(
            """
            SELECT DISTINCT s.source_item_id, s.digest, s.mapping_version
            FROM source_items s
            JOIN source_rows r ON r.source_item_id = s.source_item_id
            JOIN transactions t ON t.source_row_id = r.source_row_id
            WHERE t.workspace_id = ? AND t.occurred_on BETWEEN ? AND ?
            ORDER BY s.source_item_id
            """,
            (period.workspace_id, period.period_start, period.period_end),
        )
        evidence_rows = self.store.fetch_all(
            """
            SELECT evidence_id FROM transactions
            WHERE workspace_id = ? AND occurred_on BETWEEN ? AND ?
            ORDER BY occurred_on, transaction_id
            """,
            (period.workspace_id, period.period_start, period.period_end),
        )
        totals = {
            "currency": "NZD",
            "netMinor": int(totals_row["net"]),
            "inflowMinor": int(totals_row["inflows"]),
            "outflowMinor": int(totals_row["outflows"]),
            "unresolvedExpenseMinor": int(totals_row["unresolved"]),
            "transactionCount": int(totals_row["transaction_count"]),
        }
        source_digests = [
            {
                "sourceItemId": str(row["source_item_id"]),
                "digest": str(row["digest"]),
                "mappingVersion": str(row["mapping_version"]),
            }
            for row in sources
        ]
        evidence_ids = list(
            dict.fromkeys(str(row["evidence_id"]) for row in evidence_rows)
        )
        state_hash = material_state_hash(
            self.store, workspace_id=period.workspace_id
        )
        receipt_id = _stable_id(
            "periodrcpt", period.period_id, str(period.revision), state_hash
        )
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO accounting_period_close_receipts(
                    receipt_id, workspace_id, period_id, period_revision,
                    material_state_hash, totals_json, source_digests_json,
                    evidence_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    period.workspace_id,
                    period.period_id,
                    period.revision,
                    state_hash,
                    canonical_json(totals),
                    canonical_json(source_digests),
                    canonical_json(evidence_ids),
                    now,
                ),
            )
        return {
            "receiptId": receipt_id,
            "materialStateHash": state_hash,
            "totals": totals,
            "sourceDigests": source_digests,
            "evidenceIds": evidence_ids,
            "createdAt": now,
        }
'''

SERVICE_METHODS = '''    async def list_accounting_periods(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return tuple(
            value.as_dict()
            for value in AccountingPeriodService(self.store).latest(workspace_id)
        )

    async def set_accounting_period_status(
        self,
        *,
        workspace_id: str,
        period_start: str,
        period_end: str,
        status: str,
        actor: str,
        reason: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return AccountingPeriodService(self.store).set_status(
                workspace_id=workspace_id,
                period_start=period_start,
                period_end=period_end,
                status=status,
                actor=actor,
                reason=reason,
            )
'''

ROUTE_MODEL = '''

class AccountingPeriodRequest(RequestModel):
    period_start: date = Field(alias="periodStart")
    period_end: date = Field(alias="periodEnd")
    status: str = Field(pattern=r"^(open|soft_locked|hard_locked)$")
    actor: str = Field(default="owner", pattern=r"^(owner|accountant|system)$")
    reason: str = Field(min_length=1, max_length=500)
'''

ROUTES = '''    @router.get("/v1/workspaces/{workspace_id}/accounting-periods")
    async def list_accounting_periods(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        values = await services.list_accounting_periods(workspace_id=workspace_id)
        return {"workspaceId": workspace_id, "periods": list(values)}

    @router.post("/v1/workspaces/{workspace_id}/accounting-periods")
    async def set_accounting_period_status(
        workspace_id: PathIdentifier,
        body: AccountingPeriodRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.set_accounting_period_status(
                    workspace_id=workspace_id,
                    period_start=body.period_start.isoformat(),
                    period_end=body.period_end.isoformat(),
                    status=body.status,
                    actor=body.actor,
                    reason=body.reason,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from finance_agent.finance import FinanceEngine
from finance_agent.finance.accounting_periods import AccountingPeriodService
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    DailyCloseService(engine).run()
    return store, engine, AccountingPeriodService(store)


def test_soft_lock_is_append_only_warning_not_database_block(tmp_path: Path) -> None:
    store, _engine, periods = setup(tmp_path)
    value = periods.set_status(
        workspace_id="ws_koru_studio",
        period_start="2026-07-01",
        period_end="2026-07-31",
        status="soft_locked",
        actor="accountant",
        reason="Prepared for review.",
    )
    assert value["revision"] == 1
    assert value["closeReceipt"] is None
    with store.transaction() as connection:
        connection.execute(
            "UPDATE transactions SET category = 'reviewed' WHERE transaction_id = 'txn_koru_006'"
        )
    assert periods.period_for_date("ws_koru_studio", "2026-07-14").status == "soft_locked"


def test_hard_lock_blocks_material_updates_and_backdated_inserts_at_sqlite_boundary(tmp_path: Path) -> None:
    store, _engine, periods = setup(tmp_path)
    value = periods.set_status(
        workspace_id="ws_koru_studio",
        period_start="2026-07-01",
        period_end="2026-07-31",
        status="hard_locked",
        actor="owner",
        reason="Owner approved the July close.",
    )
    assert value["closeReceipt"]["receiptId"].startswith("periodrcpt_")
    assert value["closeReceipt"]["totals"]["transactionCount"] > 0
    assert value["closeReceipt"]["sourceDigests"]
    with pytest.raises(sqlite3.IntegrityError, match="hard locked"):
        with store.transaction() as connection:
            connection.execute(
                "UPDATE transactions SET category = 'changed' WHERE transaction_id = 'txn_koru_006'"
            )
    original = store.fetch_one(
        "SELECT * FROM transactions WHERE transaction_id = 'txn_koru_006'"
    )
    with pytest.raises(sqlite3.IntegrityError, match="hard locked"):
        with store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO transactions(
                    transaction_id, workspace_id, account_id, source_row_id,
                    evidence_id, occurred_on, description, amount_minor, currency,
                    source_status, status, classification, category,
                    classification_source, rule_id, duplicate_of_transaction_id,
                    created_at, updated_at
                ) VALUES (
                    'txn_locked_insert', ?, ?, ?, ?, '2026-07-20', 'Backdated',
                    100, 'NZD', 'posted', 'posted', 'business', 'income',
                    'deterministic', NULL, NULL, '2026-08-26T00:00:00+00:00',
                    '2026-08-26T00:00:00+00:00'
                )
                """,
                (
                    original["workspace_id"],
                    original["account_id"],
                    original["source_row_id"],
                    original["evidence_id"],
                ),
            )


def test_unlock_appends_revision_and_allows_later_change(tmp_path: Path) -> None:
    store, _engine, periods = setup(tmp_path)
    locked = periods.set_status(
        workspace_id="ws_koru_studio",
        period_start="2026-07-01",
        period_end="2026-07-31",
        status="hard_locked",
        actor="owner",
        reason="Close July.",
    )
    opened = periods.set_status(
        workspace_id="ws_koru_studio",
        period_start="2026-07-01",
        period_end="2026-07-31",
        status="open",
        actor="owner",
        reason="Reopen to correct source evidence.",
    )
    assert locked["periodId"] == opened["periodId"]
    assert opened["revision"] == 2
    with store.transaction() as connection:
        connection.execute(
            "UPDATE transactions SET category = 'reopened_change' WHERE transaction_id = 'txn_koru_006'"
        )
    rows = store.fetch_all(
        "SELECT revision, status FROM accounting_period_revisions WHERE period_id = ? ORDER BY revision",
        (locked["periodId"],),
    )
    assert [(int(row["revision"]), str(row["status"])) for row in rows] == [
        (1, "hard_locked"), (2, "open")
    ]
    assert len(store.fetch_all("SELECT * FROM accounting_period_close_receipts")) == 1


def test_overlapping_periods_fail_closed(tmp_path: Path) -> None:
    _store, _engine, periods = setup(tmp_path)
    periods.set_status(
        workspace_id="ws_koru_studio",
        period_start="2026-07-01",
        period_end="2026-07-31",
        status="open",
        actor="owner",
        reason="July period.",
    )
    with pytest.raises(ValueError, match="must not overlap"):
        periods.set_status(
            workspace_id="ws_koru_studio",
            period_start="2026-07-15",
            period_end="2026-08-15",
            status="open",
            actor="owner",
            reason="Overlapping period.",
        )
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
    write("services/api/src/finance_agent/finance/accounting_periods.py", MODULE)


def update_service_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.budgets import BudgetReservePolicyService\n"
    import_line = "from finance_agent.finance.accounting_periods import AccountingPeriodService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("budget import marker missing")
        content = content.replace(marker, import_line + marker, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "search_workspace", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def search_workspace(\n"
    addition = '''    async def list_accounting_periods(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def set_accounting_period_status(\n        self, *, workspace_id: str, period_start: str, period_end: str,\n        status: str, actor: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("search protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass CategoryBudgetRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("CategoryBudgetRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODEL + model_marker, 1)
    route_marker = '    @router.get("/v1/workspaces/{workspace_id}/search")\n'
    if route_marker not in content:
        raise RuntimeError("search route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def update_audit_and_state_identity() -> None:
    path = "services/api/src/finance_agent/audit_trail.py"
    content = read(path)
    kind_marker = '        "backup_restore",\n'
    if '"accounting_period"' not in content:
        if kind_marker not in content:
            raise RuntimeError("audit kind marker missing")
        content = content.replace(kind_marker, kind_marker + '        "accounting_period",\n', 1)
    optional_marker = '        if self._table_exists("invoice_settlement_events"):\n'
    block = '''        if self._table_exists("accounting_period_revisions"):
            for row in self.store.fetch_all(
                """
                SELECT p.* FROM accounting_period_revisions p
                WHERE p.workspace_id = ?
                ORDER BY p.created_at, p.period_id, p.revision
                """,
                (workspace_id,),
            ):
                receipt = self.store.fetch_one(
                    """
                    SELECT receipt_id, material_state_hash
                    FROM accounting_period_close_receipts
                    WHERE period_id = ? AND period_revision = ?
                    """,
                    (row["period_id"], row["revision"]),
                )
                yield AuditEvent(
                    event_id=_stable_id("audit", str(row["period_id"]), str(row["revision"])),
                    workspace_id=workspace_id,
                    kind="accounting_period",
                    action="period_status_revision",
                    status=str(row["status"]),
                    occurred_at=str(row["created_at"]),
                    actor=str(row["actor"]),
                    correlation_id=None,
                    subject_type="accounting_period",
                    subject_id=str(row["period_id"]),
                    evidence_ids=(),
                    metadata={
                        "revision": int(row["revision"]),
                        "periodStart": str(row["period_start"]),
                        "periodEnd": str(row["period_end"]),
                        "closeReceiptId": str(receipt["receipt_id"]) if receipt else None,
                        "materialStateHash": str(receipt["material_state_hash"]) if receipt else None,
                        "reasonIncluded": False,
                    },
                )
'''
    if "period_status_revision" not in content:
        if optional_marker not in content:
            raise RuntimeError("audit optional marker missing")
        content = content.replace(optional_marker, block + optional_marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/storage/state_identity.py"
    content = read(path)
    marker = '''    reserve_policies = _rows(
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
    addition = marker + '''    accounting_periods = _rows(
        store,
        """
        SELECT period_id, revision, period_start, period_end, status,
               actor, reason, created_at
        FROM accounting_period_revisions
        WHERE workspace_id = ? ORDER BY period_id, revision
        """,
        (workspace_id,),
    )
'''
    if "accounting_periods = _rows(" not in content:
        if marker not in content:
            raise RuntimeError("reserve identity marker missing")
        content = content.replace(marker, addition, 1)
    payload_marker = '        "reservePolicies": reserve_policies,\n'
    if '"accountingPeriods": accounting_periods' not in content:
        if payload_marker not in content:
            raise RuntimeError("reserve identity payload missing")
        content = content.replace(
            payload_marker,
            payload_marker + '        "accountingPeriods": accounting_periods,\n',
            1,
        )
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/finance/test_accounting_period_locks.py", TESTS)
    write("docs/ACCOUNTING_PERIODS.md", '''# Accounting periods and lock dates\n\nAccounting periods are explicit inclusive date ranges with append-only `open`, `soft_locked` and `hard_locked` revisions. Periods cannot overlap. Soft lock is a visible review state and does not alter data. Hard lock creates a close receipt containing the material-state hash, exact period inflows/outflows/net/unresolved totals, source digests and transaction evidence.\n\nSQLite triggers reject new transactions dated inside a hard-locked period and reject material transaction updates, including amounts, status, classification, category, rules and duplicate links. This protects every code path, including undo and connector/import paths. Unlocking appends an `open` revision with actor and reason; it never deletes the earlier lock or receipt.\n\nA hard lock is an internal bookkeeping control. It does not prove an accountant reviewed the period, a GST return was filed, external books were locked or financial statements were issued.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 36: append-only accounting periods and lock dates\n\n- Non-overlapping periods have open, soft-lock and hard-lock revisions.\n- Soft lock is a warning state; hard lock is enforced by SQLite triggers.\n- Backdated inserts and material transaction updates fail inside hard-locked dates.\n- Every hard lock records exact totals, material-state hash, source digests and evidence.\n- Unlock appends an actor/reason revision and preserves the prior receipt.\n- Internal locking does not claim external accountant, tax or ledger acceptance.\n'''
    if "## Stack 36: append-only accounting periods and lock dates" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_service_protocol_routes()
    update_audit_and_state_identity()
    tests_docs()
    print("accounting period lock changes applied")


if __name__ == "__main__":
    main()
