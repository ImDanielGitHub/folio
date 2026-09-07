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
        name="statement_balance_reconciliation",
        sql="""
        CREATE TABLE statement_reconciliation_revisions (
            reconciliation_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            account_id TEXT NOT NULL REFERENCES accounts(account_id),
            source_item_id TEXT NOT NULL REFERENCES source_items(source_item_id),
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            opening_balance_minor INTEGER NOT NULL,
            stated_closing_balance_minor INTEGER NOT NULL,
            posted_activity_minor INTEGER NOT NULL,
            calculated_closing_balance_minor INTEGER NOT NULL,
            discrepancy_minor INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN (
                'draft', 'confirmed', 'discrepancy_acknowledged', 'superseded'
            )),
            actor TEXT NOT NULL CHECK (actor IN ('owner', 'accountant', 'system')),
            reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
            evidence_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (reconciliation_id, revision),
            CHECK (period_start <= period_end)
        );

        CREATE INDEX statement_reconciliation_workspace
            ON statement_reconciliation_revisions(
                workspace_id, account_id, period_end DESC, revision DESC
            );
        """,
    ),
'''

MODULE = '''"""Exact statement balance roll-forward with explicit confirmation states."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:24]}"


@dataclass(frozen=True, slots=True)
class StatementReconciliation:
    reconciliation_id: str
    revision: int
    workspace_id: str
    account_id: str
    source_item_id: str
    period_start: str
    period_end: str
    opening_balance_minor: int
    stated_closing_balance_minor: int
    posted_activity_minor: int
    calculated_closing_balance_minor: int
    discrepancy_minor: int
    status: str
    actor: str
    reason: str
    evidence_ids: tuple[str, ...]
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "reconciliationId": self.reconciliation_id,
            "revision": self.revision,
            "workspaceId": self.workspace_id,
            "accountId": self.account_id,
            "sourceItemId": self.source_item_id,
            "periodStart": self.period_start,
            "periodEnd": self.period_end,
            "currency": "NZD",
            "openingBalanceMinor": self.opening_balance_minor,
            "statedClosingBalanceMinor": self.stated_closing_balance_minor,
            "postedActivityMinor": self.posted_activity_minor,
            "calculatedClosingBalanceMinor": self.calculated_closing_balance_minor,
            "discrepancyMinor": self.discrepancy_minor,
            "status": self.status,
            "actor": self.actor,
            "reason": self.reason,
            "evidenceIds": list(self.evidence_ids),
            "createdAt": self.created_at,
            "exactlyReconciled": (
                self.status == "confirmed" and self.discrepancy_minor == 0
            ),
        }


class StatementReconciliationService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _row(row: Any) -> StatementReconciliation:
        return StatementReconciliation(
            reconciliation_id=str(row["reconciliation_id"]),
            revision=int(row["revision"]),
            workspace_id=str(row["workspace_id"]),
            account_id=str(row["account_id"]),
            source_item_id=str(row["source_item_id"]),
            period_start=str(row["period_start"]),
            period_end=str(row["period_end"]),
            opening_balance_minor=int(row["opening_balance_minor"]),
            stated_closing_balance_minor=int(row["stated_closing_balance_minor"]),
            posted_activity_minor=int(row["posted_activity_minor"]),
            calculated_closing_balance_minor=int(
                row["calculated_closing_balance_minor"]
            ),
            discrepancy_minor=int(row["discrepancy_minor"]),
            status=str(row["status"]),
            actor=str(row["actor"]),
            reason=str(row["reason"]),
            evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
            created_at=str(row["created_at"]),
        )

    def _source_activity(
        self,
        *,
        workspace_id: str,
        account_id: str,
        source_item_id: str,
        period_start: str,
        period_end: str,
    ) -> tuple[int, tuple[str, ...], int, int]:
        account = self.store.fetch_one(
            "SELECT account_id FROM accounts WHERE workspace_id = ? AND account_id = ?",
            (workspace_id, account_id),
        )
        if account is None:
            raise KeyError(account_id)
        source = self.store.fetch_one(
            "SELECT source_item_id FROM source_items WHERE workspace_id = ? AND source_item_id = ?",
            (workspace_id, source_item_id),
        )
        if source is None:
            raise KeyError(source_item_id)
        row = self.store.fetch_one(
            """
            SELECT
                COALESCE(SUM(CASE WHEN r.source_status = 'posted' THEN r.amount_minor ELSE 0 END), 0) AS activity,
                COUNT(*) AS row_count,
                COALESCE(SUM(CASE WHEN r.source_status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_count
            FROM source_rows r
            WHERE r.source_item_id = ? AND r.account_id = ?
              AND r.occurred_on BETWEEN ? AND ?
            """,
            (source_item_id, account_id, period_start, period_end),
        )
        evidence = self.store.fetch_all(
            """
            SELECT e.evidence_id
            FROM evidence_links e
            LEFT JOIN source_rows r ON r.source_row_id = e.source_row_id
            WHERE e.workspace_id = ? AND e.source_item_id = ?
              AND (
                e.source_row_id IS NULL
                OR (r.account_id = ? AND r.occurred_on BETWEEN ? AND ?)
              )
            ORDER BY e.created_at, e.evidence_id
            """,
            (workspace_id, source_item_id, account_id, period_start, period_end),
        )
        return (
            int(row["activity"]),
            tuple(dict.fromkeys(str(value["evidence_id"]) for value in evidence)),
            int(row["row_count"]),
            int(row["pending_count"]),
        )

    def prepare(
        self,
        *,
        workspace_id: str,
        account_id: str,
        source_item_id: str,
        period_start: str,
        period_end: str,
        opening_balance_minor: int,
        stated_closing_balance_minor: int,
        actor: str,
        reason: str,
    ) -> dict[str, object]:
        try:
            start = date.fromisoformat(period_start)
            end = date.fromisoformat(period_end)
        except ValueError as exc:
            raise ValueError("statement period dates must use YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("statement period start must be on or before end")
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (opening_balance_minor, stated_closing_balance_minor)
        ):
            raise ValueError("statement balances must use integer minor units")
        if actor not in {"owner", "accountant", "system"}:
            raise ValueError("unsupported reconciliation actor")
        reason_value = reason.strip()
        if not reason_value:
            raise ValueError("reconciliation reason must not be blank")
        activity, evidence_ids, row_count, pending_count = self._source_activity(
            workspace_id=workspace_id,
            account_id=account_id,
            source_item_id=source_item_id,
            period_start=start.isoformat(),
            period_end=end.isoformat(),
        )
        if row_count == 0:
            raise ValueError("statement period contains no source rows for this account")
        calculated = opening_balance_minor + activity
        discrepancy = stated_closing_balance_minor - calculated
        reconciliation_id = _stable_id(
            "statementrecon",
            workspace_id,
            account_id,
            source_item_id,
            start.isoformat(),
            end.isoformat(),
        )
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS revision FROM statement_reconciliation_revisions WHERE reconciliation_id = ?",
                (reconciliation_id,),
            ).fetchone()
            revision = int(current["revision"]) + 1
            connection.execute(
                """
                UPDATE statement_reconciliation_revisions SET status = 'superseded'
                WHERE reconciliation_id = ? AND status IN (
                    'draft', 'confirmed', 'discrepancy_acknowledged'
                )
                """,
                (reconciliation_id,),
            )
            connection.execute(
                """
                INSERT INTO statement_reconciliation_revisions(
                    reconciliation_id, revision, workspace_id, account_id,
                    source_item_id, period_start, period_end,
                    opening_balance_minor, stated_closing_balance_minor,
                    posted_activity_minor, calculated_closing_balance_minor,
                    discrepancy_minor, status, actor, reason,
                    evidence_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?)
                """,
                (
                    reconciliation_id,
                    revision,
                    workspace_id,
                    account_id,
                    source_item_id,
                    start.isoformat(),
                    end.isoformat(),
                    opening_balance_minor,
                    stated_closing_balance_minor,
                    activity,
                    calculated,
                    discrepancy,
                    actor,
                    reason_value[:500],
                    canonical_json(list(evidence_ids)),
                    now,
                ),
            )
        value = self.get(workspace_id, reconciliation_id)
        return {
            **value.as_dict(),
            "sourceRowCount": row_count,
            "pendingRowCountExcluded": pending_count,
            "confirmationAllowed": discrepancy == 0,
        }

    def get(
        self, workspace_id: str, reconciliation_id: str
    ) -> StatementReconciliation:
        row = self.store.fetch_one(
            """
            SELECT * FROM statement_reconciliation_revisions
            WHERE workspace_id = ? AND reconciliation_id = ?
            ORDER BY revision DESC LIMIT 1
            """,
            (workspace_id, reconciliation_id),
        )
        if row is None:
            raise KeyError(reconciliation_id)
        return self._row(row)

    def list(self, workspace_id: str) -> tuple[StatementReconciliation, ...]:
        rows = self.store.fetch_all(
            """
            SELECT r.* FROM statement_reconciliation_revisions r
            WHERE r.workspace_id = ? AND r.revision = (
                SELECT MAX(r2.revision) FROM statement_reconciliation_revisions r2
                WHERE r2.reconciliation_id = r.reconciliation_id
            )
            ORDER BY r.period_end DESC, r.reconciliation_id
            """,
            (workspace_id,),
        )
        return tuple(self._row(row) for row in rows)

    def decide(
        self,
        *,
        workspace_id: str,
        reconciliation_id: str,
        action: str,
        actor: str,
        reason: str,
    ) -> StatementReconciliation:
        current = self.get(workspace_id, reconciliation_id)
        if current.status != "draft":
            if (
                action == "confirm" and current.status == "confirmed"
                or action == "acknowledge_discrepancy"
                and current.status == "discrepancy_acknowledged"
            ):
                return current
            raise ValueError("only a draft reconciliation can be decided")
        if action == "confirm":
            if current.discrepancy_minor != 0:
                raise ValueError(
                    "statement cannot be confirmed while discrepancyMinor is non-zero"
                )
            status = "confirmed"
        elif action == "acknowledge_discrepancy":
            if current.discrepancy_minor == 0:
                raise ValueError("an exactly reconciled statement has no discrepancy to acknowledge")
            status = "discrepancy_acknowledged"
        else:
            raise ValueError("unsupported reconciliation action")
        if actor not in {"owner", "accountant", "system"}:
            raise ValueError("unsupported reconciliation actor")
        reason_value = reason.strip()
        if not reason_value:
            raise ValueError("reconciliation decision reason must not be blank")
        now = datetime.now(UTC).isoformat()
        revision = current.revision + 1
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE statement_reconciliation_revisions SET status = 'superseded' WHERE reconciliation_id = ? AND revision = ?",
                (reconciliation_id, current.revision),
            )
            connection.execute(
                """
                INSERT INTO statement_reconciliation_revisions(
                    reconciliation_id, revision, workspace_id, account_id,
                    source_item_id, period_start, period_end,
                    opening_balance_minor, stated_closing_balance_minor,
                    posted_activity_minor, calculated_closing_balance_minor,
                    discrepancy_minor, status, actor, reason,
                    evidence_ids_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reconciliation_id,
                    revision,
                    workspace_id,
                    current.account_id,
                    current.source_item_id,
                    current.period_start,
                    current.period_end,
                    current.opening_balance_minor,
                    current.stated_closing_balance_minor,
                    current.posted_activity_minor,
                    current.calculated_closing_balance_minor,
                    current.discrepancy_minor,
                    status,
                    actor,
                    reason_value[:500],
                    canonical_json(list(current.evidence_ids)),
                    now,
                ),
            )
        return self.get(workspace_id, reconciliation_id)
'''

SERVICE_METHODS = '''    async def prepare_statement_reconciliation(
        self,
        *,
        workspace_id: str,
        account_id: str,
        source_item_id: str,
        period_start: str,
        period_end: str,
        opening_balance_minor: int,
        stated_closing_balance_minor: int,
        actor: str,
        reason: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return StatementReconciliationService(self.store).prepare(
            workspace_id=workspace_id,
            account_id=account_id,
            source_item_id=source_item_id,
            period_start=period_start,
            period_end=period_end,
            opening_balance_minor=opening_balance_minor,
            stated_closing_balance_minor=stated_closing_balance_minor,
            actor=actor,
            reason=reason,
        )

    async def list_statement_reconciliations(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return tuple(
            value.as_dict()
            for value in StatementReconciliationService(self.store).list(workspace_id)
        )

    async def decide_statement_reconciliation(
        self,
        *,
        workspace_id: str,
        reconciliation_id: str,
        action: str,
        actor: str,
        reason: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = StatementReconciliationService(self.store).decide(
                workspace_id=workspace_id,
                reconciliation_id=reconciliation_id,
                action=action,
                actor=actor,
                reason=reason,
            )
        return value.as_dict()
'''

ROUTE_MODELS = '''

class StatementReconciliationRequest(RequestModel):
    account_id: str = Field(alias="accountId", pattern=IDENTIFIER_PATTERN)
    source_item_id: str = Field(alias="sourceItemId", pattern=IDENTIFIER_PATTERN)
    period_start: date = Field(alias="periodStart")
    period_end: date = Field(alias="periodEnd")
    opening_balance_minor: int = Field(alias="openingBalanceMinor")
    stated_closing_balance_minor: int = Field(alias="statedClosingBalanceMinor")
    actor: str = Field(default="owner", pattern=r"^(owner|accountant|system)$")
    reason: str = Field(min_length=1, max_length=500)


class StatementReconciliationDecisionRequest(RequestModel):
    action: str = Field(pattern=r"^(confirm|acknowledge_discrepancy)$")
    actor: str = Field(default="owner", pattern=r"^(owner|accountant|system)$")
    reason: str = Field(min_length=1, max_length=500)
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/statement-reconciliations")
    async def prepare_statement_reconciliation(
        workspace_id: PathIdentifier,
        body: StatementReconciliationRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.prepare_statement_reconciliation(
                    workspace_id=workspace_id,
                    account_id=body.account_id,
                    source_item_id=body.source_item_id,
                    period_start=body.period_start.isoformat(),
                    period_end=body.period_end.isoformat(),
                    opening_balance_minor=body.opening_balance_minor,
                    stated_closing_balance_minor=body.stated_closing_balance_minor,
                    actor=body.actor,
                    reason=body.reason,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="account or source item not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/workspaces/{workspace_id}/statement-reconciliations")
    async def list_statement_reconciliations(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        values = await services.list_statement_reconciliations(
            workspace_id=workspace_id
        )
        return {"workspaceId": workspace_id, "reconciliations": list(values)}

    @router.post(
        "/v1/workspaces/{workspace_id}/statement-reconciliations/{reconciliation_id}/decide"
    )
    async def decide_statement_reconciliation(
        workspace_id: PathIdentifier,
        reconciliation_id: PathIdentifier,
        body: StatementReconciliationDecisionRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.decide_statement_reconciliation(
                    workspace_id=workspace_id,
                    reconciliation_id=reconciliation_id,
                    action=body.action,
                    actor=body.actor,
                    reason=body.reason,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="statement reconciliation not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

from pathlib import Path

import pytest

from finance_agent.finance import FinanceEngine
from finance_agent.finance.statement_reconciliation import StatementReconciliationService
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    DailyCloseService(engine).run()
    return store, StatementReconciliationService(store)


def posted_activity(store: SQLiteStore) -> int:
    row = store.fetch_one(
        """
        SELECT COALESCE(SUM(CASE WHEN source_status = 'posted' THEN amount_minor ELSE 0 END), 0) AS total
        FROM source_rows
        WHERE source_item_id = 'src_koru_bank_csv_20260717'
          AND account_id = 'acct_koru_business'
          AND occurred_on BETWEEN '2026-07-01' AND '2026-07-31'
        """
    )
    return int(row["total"])


def test_exact_roll_forward_can_be_confirmed_with_source_evidence(tmp_path: Path) -> None:
    store, service = setup(tmp_path)
    activity = posted_activity(store)
    draft = service.prepare(
        workspace_id="ws_koru_studio",
        account_id="acct_koru_business",
        source_item_id="src_koru_bank_csv_20260717",
        period_start="2026-07-01",
        period_end="2026-07-31",
        opening_balance_minor=100000,
        stated_closing_balance_minor=100000 + activity,
        actor="owner",
        reason="Compare the July statement balances.",
    )
    assert draft["postedActivityMinor"] == activity
    assert draft["discrepancyMinor"] == 0
    assert draft["confirmationAllowed"] is True
    assert draft["evidenceIds"]
    confirmed = service.decide(
        workspace_id="ws_koru_studio",
        reconciliation_id=str(draft["reconciliationId"]),
        action="confirm",
        actor="owner",
        reason="Owner confirmed the opening and closing balances.",
    )
    assert confirmed.status == "confirmed"
    assert confirmed.as_dict()["exactlyReconciled"] is True
    rows = store.fetch_all(
        "SELECT revision, status FROM statement_reconciliation_revisions WHERE reconciliation_id = ? ORDER BY revision",
        (draft["reconciliationId"],),
    )
    assert [(int(row["revision"]), str(row["status"])) for row in rows] == [
        (1, "superseded"), (2, "confirmed")
    ]


def test_nonzero_discrepancy_cannot_be_called_reconciled(tmp_path: Path) -> None:
    store, service = setup(tmp_path)
    activity = posted_activity(store)
    draft = service.prepare(
        workspace_id="ws_koru_studio",
        account_id="acct_koru_business",
        source_item_id="src_koru_bank_csv_20260717",
        period_start="2026-07-01",
        period_end="2026-07-31",
        opening_balance_minor=0,
        stated_closing_balance_minor=activity + 123,
        actor="accountant",
        reason="Investigate a NZD 1.23 difference.",
    )
    assert draft["discrepancyMinor"] == 123
    assert draft["confirmationAllowed"] is False
    with pytest.raises(ValueError, match="non-zero"):
        service.decide(
            workspace_id="ws_koru_studio",
            reconciliation_id=str(draft["reconciliationId"]),
            action="confirm",
            actor="accountant",
            reason="Incorrect confirmation attempt.",
        )
    acknowledged = service.decide(
        workspace_id="ws_koru_studio",
        reconciliation_id=str(draft["reconciliationId"]),
        action="acknowledge_discrepancy",
        actor="accountant",
        reason="Difference remains open for source follow-up.",
    )
    assert acknowledged.status == "discrepancy_acknowledged"
    assert acknowledged.as_dict()["exactlyReconciled"] is False


def test_pending_rows_are_excluded_and_reported(tmp_path: Path) -> None:
    _store, service = setup(tmp_path)
    draft = service.prepare(
        workspace_id="ws_koru_studio",
        account_id="acct_koru_business",
        source_item_id="src_koru_bank_csv_20260717",
        period_start="2026-07-01",
        period_end="2026-07-31",
        opening_balance_minor=0,
        stated_closing_balance_minor=0,
        actor="owner",
        reason="Preview pending-row handling.",
    )
    assert draft["pendingRowCountExcluded"] >= 1


def test_account_and_source_must_belong_to_workspace(tmp_path: Path) -> None:
    _store, service = setup(tmp_path)
    with pytest.raises(KeyError):
        service.prepare(
            workspace_id="ws_koru_studio",
            account_id="acct_missing",
            source_item_id="src_koru_bank_csv_20260717",
            period_start="2026-07-01",
            period_end="2026-07-31",
            opening_balance_minor=0,
            stated_closing_balance_minor=0,
            actor="owner",
            reason="Invalid account.",
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
    write(
        "services/api/src/finance_agent/finance/statement_reconciliation.py",
        MODULE,
    )


def update_service_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.transfers import InternalTransferService\n"
    import_line = "from finance_agent.finance.statement_reconciliation import StatementReconciliationService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("transfer service import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "scan_internal_transfers", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def scan_internal_transfers(\n"
    addition = '''    async def prepare_statement_reconciliation(\n        self, *, workspace_id: str, account_id: str, source_item_id: str,\n        period_start: str, period_end: str, opening_balance_minor: int,\n        stated_closing_balance_minor: int, actor: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n    async def list_statement_reconciliations(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def decide_statement_reconciliation(\n        self, *, workspace_id: str, reconciliation_id: str, action: str,\n        actor: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("transfer protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass TransferDecisionRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("TransferDecisionRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.post("/v1/workspaces/{workspace_id}/transfers/scan")\n'
    if route_marker not in content:
        raise RuntimeError("transfer route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def update_audit_state_identity() -> None:
    path = "services/api/src/finance_agent/audit_trail.py"
    content = read(path)
    kind_marker = '        "internal_transfer",\n'
    if '"statement_reconciliation"' not in content:
        if kind_marker not in content:
            raise RuntimeError("internal transfer audit kind marker missing")
        content = content.replace(
            kind_marker,
            kind_marker + '        "statement_reconciliation",\n',
            1,
        )
    optional_marker = '        if self._table_exists("transfer_match_events"):\n'
    block = '''        if self._table_exists("statement_reconciliation_revisions"):
            for row in self.store.fetch_all(
                "SELECT * FROM statement_reconciliation_revisions WHERE workspace_id = ? ORDER BY created_at, reconciliation_id, revision",
                (workspace_id,),
            ):
                yield AuditEvent(
                    event_id=_stable_id("audit", str(row["reconciliation_id"]), str(row["revision"])),
                    workspace_id=workspace_id,
                    kind="statement_reconciliation",
                    action="statement_balance_revision",
                    status=str(row["status"]),
                    occurred_at=str(row["created_at"]),
                    actor=str(row["actor"]),
                    correlation_id=None,
                    subject_type="statement_reconciliation",
                    subject_id=str(row["reconciliation_id"]),
                    evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
                    metadata={
                        "revision": int(row["revision"]),
                        "accountId": str(row["account_id"]),
                        "sourceItemId": str(row["source_item_id"]),
                        "periodStart": str(row["period_start"]),
                        "periodEnd": str(row["period_end"]),
                        "discrepancyMinor": int(row["discrepancy_minor"]),
                        "reasonIncluded": False,
                    },
                )
'''
    if "statement_balance_revision" not in content:
        if optional_marker not in content:
            raise RuntimeError("transfer optional audit marker missing")
        content = content.replace(optional_marker, block + optional_marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/storage/state_identity.py"
    content = read(path)
    marker = '''    transfer_events = _rows(
        store,
        """
        SELECT event_id, candidate_id, event_type, debit_transaction_id,
               credit_transaction_id, before_json, after_json,
               evidence_ids_json, occurred_at, reverses_event_id
        FROM transfer_match_events
        WHERE workspace_id = ? ORDER BY occurred_at, event_id
        """,
        (workspace_id,),
    )
'''
    addition = marker + '''    statement_reconciliations = _rows(
        store,
        """
        SELECT reconciliation_id, revision, account_id, source_item_id,
               period_start, period_end, opening_balance_minor,
               stated_closing_balance_minor, posted_activity_minor,
               calculated_closing_balance_minor, discrepancy_minor,
               status, actor, evidence_ids_json, created_at
        FROM statement_reconciliation_revisions
        WHERE workspace_id = ? ORDER BY reconciliation_id, revision
        """,
        (workspace_id,),
    )
'''
    if "statement_reconciliations = _rows(" not in content:
        if marker not in content:
            raise RuntimeError("transfer identity marker missing")
        content = content.replace(marker, addition, 1)
    payload_marker = '        "transferEvents": transfer_events,\n'
    if '"statementReconciliations": statement_reconciliations' not in content:
        if payload_marker not in content:
            raise RuntimeError("transfer identity payload missing")
        content = content.replace(
            payload_marker,
            payload_marker
            + '        "statementReconciliations": statement_reconciliations,\n',
            1,
        )
    write(path, content)


def tests_docs() -> None:
    write(
        "services/api/tests/finance/test_statement_balance_reconciliation.py",
        TESTS,
    )
    write("docs/STATEMENT_RECONCILIATION.md", '''# Statement balance reconciliation\n\nA statement reconciliation binds one workspace account, one immutable source item and one inclusive date period to owner/accountant-provided opening and closing balances. Folio sums only posted source rows, excludes and reports pending rows, and calculates `opening + posted activity = calculated closing`. Discrepancy is `stated closing - calculated closing`. All values use integer NZD minor units and retain source/row evidence.\n\nPreparation appends a draft revision. Exact zero-discrepancy drafts may be confirmed. Non-zero drafts cannot be called reconciled; they may only be marked `discrepancy_acknowledged`, preserving the open difference. New preparation or decision appends a revision and supersedes the prior one.\n\nThis proves an internal arithmetic comparison against the selected source bytes. It does not prove the statement is complete, that external accounting books agree, or that an accountant reviewed the result.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 40: evidence-backed statement balance reconciliation\n\n- One account/source/period roll-forward uses exact opening, activity and closing amounts.\n- Pending rows are excluded and disclosed.\n- Zero discrepancy may be confirmed; non-zero discrepancy cannot be labelled reconciled.\n- Discrepancy acknowledgement remains a separate visible state.\n- Every preparation and decision appends a revision with actor, reason and evidence.\n- Internal arithmetic does not claim external books or accountant acceptance.\n'''
    if "## Stack 40: evidence-backed statement balance reconciliation" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_service_protocol_routes()
    update_audit_state_identity()
    tests_docs()
    print("statement balance reconciliation changes applied")


if __name__ == "__main__":
    main()
