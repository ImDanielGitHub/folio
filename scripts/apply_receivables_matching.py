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
        name="receivables_matching",
        sql="""
        CREATE TABLE invoice_payment_candidates (
            candidate_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            invoice_id TEXT NOT NULL REFERENCES sales_invoices(invoice_id),
            transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
            score_basis_points INTEGER NOT NULL CHECK (
                score_basis_points BETWEEN 0 AND 10000
            ),
            factors_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),
            created_at TEXT NOT NULL,
            decided_at TEXT,
            UNIQUE (workspace_id, invoice_id, transaction_id)
        );

        CREATE TABLE invoice_settlement_events (
            event_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            invoice_id TEXT NOT NULL REFERENCES sales_invoices(invoice_id),
            transaction_id TEXT NOT NULL UNIQUE REFERENCES transactions(transaction_id),
            candidate_id TEXT NOT NULL REFERENCES invoice_payment_candidates(candidate_id),
            actor TEXT NOT NULL CHECK (actor = 'owner'),
            reason TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            UNIQUE (workspace_id, invoice_id)
        );

        CREATE INDEX invoice_payment_candidates_status
            ON invoice_payment_candidates(workspace_id, status, created_at);
        """,
    ),
'''

RECEIVABLES = '''"""Deterministic receivables candidates with explicit owner settlement."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

from finance_agent.finance.commitments import WorkspaceCommitmentService
from finance_agent.storage import SQLiteStore, canonical_json

MIN_CANDIDATE_SCORE = 8000


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _normalise(value: str) -> str:
    return " ".join("".join(character if character.isalnum() else " " for character in value.upper()).split())


class ReceivablesService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def _invoice_totals(self, invoice_id: str) -> dict[str, int]:
        row = self.store.fetch_one(
            """
            SELECT payload_json FROM sales_invoice_revisions
            WHERE invoice_id = ? ORDER BY revision DESC LIMIT 1
            """,
            (invoice_id,),
        )
        if row is None:
            raise KeyError(invoice_id)
        payload = json.loads(str(row["payload_json"]))
        totals = payload.get("totals")
        if not isinstance(totals, dict):
            raise ValueError("invoice revision has no deterministic totals")
        return {
            "netMinor": int(totals["netMinor"]),
            "gstMinor": int(totals["gstMinor"]),
            "grossMinor": int(totals["grossMinor"]),
        }

    def scan(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        invoices = self.store.fetch_all(
            """
            SELECT invoice_id, invoice_number, issue_date, due_date, buyer_name
            FROM sales_invoices
            WHERE workspace_id = ? AND status = 'issued'
              AND invoice_id NOT IN (
                SELECT invoice_id FROM invoice_settlement_events WHERE workspace_id = ?
              )
            ORDER BY issue_date, invoice_id
            """,
            (workspace_id, workspace_id),
        )
        transactions = self.store.fetch_all(
            """
            SELECT transaction_id, occurred_on, description, amount_minor,
                   currency, evidence_id
            FROM transactions
            WHERE workspace_id = ? AND status = 'posted'
              AND source_status = 'posted' AND amount_minor > 0
              AND transaction_id NOT IN (
                SELECT transaction_id FROM invoice_settlement_events WHERE workspace_id = ?
              )
            ORDER BY occurred_on, transaction_id
            """,
            (workspace_id, workspace_id),
        )
        candidates: list[dict[str, object]] = []
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            for invoice in invoices:
                totals = self._invoice_totals(str(invoice["invoice_id"]))
                issue = date.fromisoformat(str(invoice["issue_date"]))
                latest = date.fromisoformat(str(invoice["due_date"])) + timedelta(days=30)
                number = _normalise(str(invoice["invoice_number"]))
                buyer = _normalise(str(invoice["buyer_name"]))
                for transaction in transactions:
                    occurred = date.fromisoformat(str(transaction["occurred_on"]))
                    if not issue <= occurred <= latest:
                        continue
                    if int(transaction["amount_minor"]) != totals["grossMinor"]:
                        continue
                    description = _normalise(str(transaction["description"]))
                    number_match = bool(number and number in description)
                    buyer_match = bool(buyer and buyer in description)
                    score = 8000 + (1500 if number_match else 0) + (500 if buyer_match else 0)
                    score = min(score, 10000)
                    if score < MIN_CANDIDATE_SCORE:
                        continue
                    factors = {
                        "exactGrossAmount": True,
                        "currency": str(transaction["currency"]),
                        "dateWithinIssueAndDuePlus30Days": True,
                        "invoiceNumberInDescription": number_match,
                        "buyerNameInDescription": buyer_match,
                        "invoiceGrossMinor": totals["grossMinor"],
                    }
                    candidate_id = _stable_id(
                        "paycandidate",
                        workspace_id,
                        str(invoice["invoice_id"]),
                        str(transaction["transaction_id"]),
                    )
                    connection.execute(
                        """
                        INSERT INTO invoice_payment_candidates(
                            candidate_id, workspace_id, invoice_id, transaction_id,
                            score_basis_points, factors_json, status, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)
                        ON CONFLICT(workspace_id, invoice_id, transaction_id)
                        DO UPDATE SET
                            score_basis_points = excluded.score_basis_points,
                            factors_json = excluded.factors_json
                        """,
                        (
                            candidate_id,
                            workspace_id,
                            invoice["invoice_id"],
                            transaction["transaction_id"],
                            score,
                            canonical_json(factors),
                            now,
                        ),
                    )
                    candidates.append(
                        {
                            "candidateId": candidate_id,
                            "invoiceId": str(invoice["invoice_id"]),
                            "invoiceNumber": str(invoice["invoice_number"]),
                            "transactionId": str(transaction["transaction_id"]),
                            "occurredOn": str(transaction["occurred_on"]),
                            "description": str(transaction["description"]),
                            "amountMinor": int(transaction["amount_minor"]),
                            "currency": str(transaction["currency"]),
                            "evidenceIds": [str(transaction["evidence_id"])],
                            "scoreBasisPoints": score,
                            "factors": factors,
                            "status": "proposed",
                        }
                    )
        return tuple(
            sorted(
                candidates,
                key=lambda value: (
                    -int(value["scoreBasisPoints"]),
                    str(value["invoiceId"]),
                    str(value["transactionId"]),
                ),
            )
        )

    def list_candidates(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        rows = self.store.fetch_all(
            """
            SELECT c.*, i.invoice_number, t.occurred_on, t.description,
                   t.amount_minor, t.currency, t.evidence_id
            FROM invoice_payment_candidates c
            JOIN sales_invoices i ON i.invoice_id = c.invoice_id
            JOIN transactions t ON t.transaction_id = c.transaction_id
            WHERE c.workspace_id = ?
            ORDER BY c.created_at DESC, c.candidate_id
            """,
            (workspace_id,),
        )
        return tuple(
            {
                "candidateId": str(row["candidate_id"]),
                "invoiceId": str(row["invoice_id"]),
                "invoiceNumber": str(row["invoice_number"]),
                "transactionId": str(row["transaction_id"]),
                "occurredOn": str(row["occurred_on"]),
                "description": str(row["description"]),
                "amountMinor": int(row["amount_minor"]),
                "currency": str(row["currency"]),
                "evidenceIds": [str(row["evidence_id"])],
                "scoreBasisPoints": int(row["score_basis_points"]),
                "factors": json.loads(str(row["factors_json"])),
                "status": str(row["status"]),
                "createdAt": str(row["created_at"]),
                "decidedAt": str(row["decided_at"]) if row["decided_at"] else None,
            }
            for row in rows
        )

    def confirm(
        self,
        *,
        workspace_id: str,
        candidate_id: str,
        reason: str,
    ) -> dict[str, object]:
        reason_value = reason.strip()
        if not reason_value:
            raise ValueError("settlement reason is required")
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            candidate = connection.execute(
                """
                SELECT c.*, t.evidence_id
                FROM invoice_payment_candidates c
                JOIN transactions t ON t.transaction_id = c.transaction_id
                WHERE c.workspace_id = ? AND c.candidate_id = ?
                """,
                (workspace_id, candidate_id),
            ).fetchone()
            if candidate is None:
                raise KeyError(candidate_id)
            if str(candidate["status"]) == "confirmed":
                existing = connection.execute(
                    "SELECT * FROM invoice_settlement_events WHERE candidate_id = ?",
                    (candidate_id,),
                ).fetchone()
                if existing is None:
                    raise RuntimeError("confirmed candidate has no settlement event")
                return {
                    "eventId": str(existing["event_id"]),
                    "invoiceId": str(existing["invoice_id"]),
                    "transactionId": str(existing["transaction_id"]),
                    "status": "confirmed",
                    "idempotentReplay": True,
                }
            if str(candidate["status"]) != "proposed":
                raise ValueError("only a proposed candidate can be confirmed")
            conflict = connection.execute(
                """
                SELECT event_id FROM invoice_settlement_events
                WHERE workspace_id = ? AND (
                    invoice_id = ? OR transaction_id = ?
                )
                """,
                (
                    workspace_id,
                    candidate["invoice_id"],
                    candidate["transaction_id"],
                ),
            ).fetchone()
            if conflict is not None:
                raise ValueError("invoice or transaction is already settled")
            event_id = _stable_id(
                "settlement",
                workspace_id,
                str(candidate["invoice_id"]),
                str(candidate["transaction_id"]),
            )
            evidence_ids = [str(candidate["evidence_id"])]
            connection.execute(
                """
                INSERT INTO invoice_settlement_events(
                    event_id, workspace_id, invoice_id, transaction_id,
                    candidate_id, actor, reason, evidence_ids_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, 'owner', ?, ?, ?)
                """,
                (
                    event_id,
                    workspace_id,
                    candidate["invoice_id"],
                    candidate["transaction_id"],
                    candidate_id,
                    reason_value[:500],
                    canonical_json(evidence_ids),
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE invoice_payment_candidates
                SET status = 'confirmed', decided_at = ?
                WHERE candidate_id = ?
                """,
                (now, candidate_id),
            )
            connection.execute(
                """
                UPDATE cash_commitments
                SET status = 'completed', updated_at = ?
                WHERE workspace_id = ? AND commitment_id = ?
                """,
                (now, workspace_id, f"commit_{candidate['invoice_id']}"),
            )
        return {
            "eventId": event_id,
            "invoiceId": str(candidate["invoice_id"]),
            "transactionId": str(candidate["transaction_id"]),
            "candidateId": candidate_id,
            "status": "confirmed",
            "settledByOwner": True,
            "evidenceIds": evidence_ids,
            "occurredAt": now,
        }

    def reject(
        self,
        *,
        workspace_id: str,
        candidate_id: str,
    ) -> dict[str, object]:
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE invoice_payment_candidates
                SET status = 'rejected', decided_at = ?
                WHERE workspace_id = ? AND candidate_id = ? AND status = 'proposed'
                """,
                (now, workspace_id, candidate_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(candidate_id)
        return {"candidateId": candidate_id, "status": "rejected", "decidedAt": now}

    def settlement_status(self, workspace_id: str, invoice_id: str) -> dict[str, object]:
        event = self.store.fetch_one(
            """
            SELECT * FROM invoice_settlement_events
            WHERE workspace_id = ? AND invoice_id = ?
            """,
            (workspace_id, invoice_id),
        )
        if event is not None:
            return {
                "status": "confirmed",
                "eventId": str(event["event_id"]),
                "transactionId": str(event["transaction_id"]),
                "occurredAt": str(event["occurred_at"]),
                "evidenceIds": json.loads(str(event["evidence_ids_json"])),
            }
        proposed = self.store.fetch_one(
            """
            SELECT candidate_id FROM invoice_payment_candidates
            WHERE workspace_id = ? AND invoice_id = ? AND status = 'proposed'
            ORDER BY score_basis_points DESC, created_at LIMIT 1
            """,
            (workspace_id, invoice_id),
        )
        return {
            "status": "candidate" if proposed else "unmatched",
            "candidateId": str(proposed["candidate_id"]) if proposed else None,
        }
'''

LIST_INVOICES = '''    async def list_invoices(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        invoices = SalesInvoiceService(self.store).list(workspace_id)
        receivables = ReceivablesService(self.store)
        return tuple(
            {
                **invoice,
                "settlement": receivables.settlement_status(
                    workspace_id, str(invoice["invoiceId"])
                ),
            }
            for invoice in invoices
        )
'''

ISSUE_INVOICE = '''    async def issue_invoice(
        self, *, workspace_id: str, invoice_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            issued = SalesInvoiceService(self.store).issue(
                workspace_id=workspace_id, invoice_id=invoice_id
            )
            totals = issued.get("totals")
            if not isinstance(totals, Mapping):
                raise RuntimeError("issued invoice has no deterministic totals")
            WorkspaceCommitmentService(self.store).upsert(
                workspace_id=workspace_id,
                commitment_id=f"commit_{invoice_id}",
                label=f"Expected invoice {issued['invoiceNumber']} payment",
                amount_minor=int(totals["grossMinor"]),
                due_on=str(issued["dueDate"]),
                recurrence="none",
                recurrence_count=1,
                status="planned",
                source="deterministic",
                evidence_ids=(),
            )
            result = self.daily_close.run()
            self.working_understanding.ensure_current(workspace_id=workspace_id)
            self._register_daily_close_events(result)
        return {
            **issued,
            "cashCommitmentId": f"commit_{invoice_id}",
            "dailyCloseRunId": result.run_id,
        }
'''

SERVICE_METHODS = '''    async def scan_receivable_candidates(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return ReceivablesService(self.store).scan(workspace_id)

    async def list_receivable_candidates(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return ReceivablesService(self.store).list_candidates(workspace_id)

    async def confirm_receivable_candidate(
        self,
        *,
        workspace_id: str,
        candidate_id: str,
        reason: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = ReceivablesService(self.store).confirm(
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                reason=reason,
            )
            result = self.daily_close.run()
            self.working_understanding.ensure_current(workspace_id=workspace_id)
            self._register_daily_close_events(result)
        return {**value, "dailyCloseRunId": result.run_id}

    async def reject_receivable_candidate(
        self, *, workspace_id: str, candidate_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return ReceivablesService(self.store).reject(
                workspace_id=workspace_id,
                candidate_id=candidate_id,
            )
'''

ROUTE_MODEL = '''

class SettlementConfirmationRequest(RequestModel):
    reason: str = Field(min_length=1, max_length=500)
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/receivables/scan")
    async def scan_receivable_candidates(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        candidates = await services.scan_receivable_candidates(
            workspace_id=workspace_id
        )
        return {"workspaceId": workspace_id, "candidates": list(candidates)}

    @router.get("/v1/workspaces/{workspace_id}/receivables/candidates")
    async def list_receivable_candidates(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        candidates = await services.list_receivable_candidates(
            workspace_id=workspace_id
        )
        return {"workspaceId": workspace_id, "candidates": list(candidates)}

    @router.post(
        "/v1/workspaces/{workspace_id}/receivables/candidates/{candidate_id}/confirm"
    )
    async def confirm_receivable_candidate(
        workspace_id: PathIdentifier,
        candidate_id: PathIdentifier,
        body: SettlementConfirmationRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.confirm_receivable_candidate(
                    workspace_id=workspace_id,
                    candidate_id=candidate_id,
                    reason=body.reason,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="receivable candidate not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post(
        "/v1/workspaces/{workspace_id}/receivables/candidates/{candidate_id}/reject"
    )
    async def reject_receivable_candidate(
        workspace_id: PathIdentifier,
        candidate_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.reject_receivable_candidate(
                    workspace_id=workspace_id,
                    candidate_id=candidate_id,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="receivable candidate not found") from exc

'''

TESTS = '''from __future__ import annotations

from pathlib import Path

import pytest

from finance_agent.finance import FinanceEngine
from finance_agent.finance.invoices import SalesInvoiceService
from finance_agent.finance.receivables import ReceivablesService
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    DailyCloseService(engine).run()
    invoices = SalesInvoiceService(store)
    invoice = invoices.save_draft(
        workspace_id="ws_koru_studio",
        invoice_id=None,
        invoice_number="INV-7250",
        seller_name="Koru Studio",
        seller_nzbn=None,
        buyer_name="Acme",
        buyer_nzbn=None,
        issue_date="2026-07-01",
        due_date="2026-07-20",
        notes=None,
        lines=(
            {
                "description": "Design services",
                "quantityMillis": 1000,
                "unitPriceMinor": 630435,
                "taxTreatment": "standard",
            },
        ),
    )
    invoices.issue(workspace_id="ws_koru_studio", invoice_id=invoice.invoice_id)
    return store, engine, invoice


def test_scan_creates_exact_amount_candidate_but_does_not_settle(tmp_path: Path) -> None:
    store, _engine, invoice = setup(tmp_path)
    values = ReceivablesService(store).scan("ws_koru_studio")
    assert len(values) == 1
    candidate = values[0]
    assert candidate["invoiceId"] == invoice.invoice_id
    assert candidate["transactionId"] == "txn_koru_001"
    assert candidate["amountMinor"] == 725000
    assert candidate["status"] == "proposed"
    assert ReceivablesService(store).settlement_status(
        "ws_koru_studio", invoice.invoice_id
    )["status"] == "candidate"
    assert store.fetch_all("SELECT * FROM invoice_settlement_events") == []


def test_owner_confirmation_is_one_to_one_append_only_and_completes_commitment(tmp_path: Path) -> None:
    store, _engine, invoice = setup(tmp_path)
    commitments = __import__(
        "finance_agent.finance.commitments", fromlist=["WorkspaceCommitmentService"]
    ).WorkspaceCommitmentService(store)
    commitments.upsert(
        workspace_id="ws_koru_studio",
        commitment_id=f"commit_{invoice.invoice_id}",
        label="Expected invoice payment",
        amount_minor=725000,
        due_on="2026-07-20",
        status="planned",
    )
    candidate = ReceivablesService(store).scan("ws_koru_studio")[0]
    confirmed = ReceivablesService(store).confirm(
        workspace_id="ws_koru_studio",
        candidate_id=str(candidate["candidateId"]),
        reason="Owner confirmed this bank credit settled INV-7250.",
    )
    assert confirmed["settledByOwner"] is True
    assert confirmed["evidenceIds"]
    replay = ReceivablesService(store).confirm(
        workspace_id="ws_koru_studio",
        candidate_id=str(candidate["candidateId"]),
        reason="Repeated click",
    )
    assert replay["idempotentReplay"] is True
    events = store.fetch_all("SELECT * FROM invoice_settlement_events")
    assert len(events) == 1
    commitment = store.fetch_one(
        "SELECT status FROM cash_commitments WHERE commitment_id = ?",
        (f"commit_{invoice.invoice_id}",),
    )
    assert str(commitment["status"]) == "completed"


def test_rejection_does_not_settle_or_complete_expected_cash(tmp_path: Path) -> None:
    store, _engine, invoice = setup(tmp_path)
    candidate = ReceivablesService(store).scan("ws_koru_studio")[0]
    rejected = ReceivablesService(store).reject(
        workspace_id="ws_koru_studio",
        candidate_id=str(candidate["candidateId"]),
    )
    assert rejected["status"] == "rejected"
    assert store.fetch_all("SELECT * FROM invoice_settlement_events") == []
    assert ReceivablesService(store).settlement_status(
        "ws_koru_studio", invoice.invoice_id
    )["status"] == "unmatched"


def test_amount_or_date_mismatch_produces_no_candidate(tmp_path: Path) -> None:
    store, _engine, _invoice = setup(tmp_path)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE transactions SET amount_minor = 724999 WHERE transaction_id = 'txn_koru_001'"
        )
    assert ReceivablesService(store).scan("ws_koru_studio") == ()
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
    write("services/api/src/finance_agent/finance/receivables.py", RECEIVABLES)


def update_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.invoices import SalesInvoiceService\n"
    import_line = "from finance_agent.finance.receivables import ReceivablesService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("invoice service import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    replace_method(path, "LocalRouteServices", "list_invoices", LIST_INVOICES)
    replace_method(path, "LocalRouteServices", "issue_invoice", ISSUE_INVOICE)
    insert_method_before(path, "LocalRouteServices", "poll_telegram_live", SERVICE_METHODS)


def update_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def poll_telegram_live(\n"
    addition = '''    async def scan_receivable_candidates(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def list_receivable_candidates(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def confirm_receivable_candidate(\n        self, *, workspace_id: str, candidate_id: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n    async def reject_receivable_candidate(\n        self, *, workspace_id: str, candidate_id: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("Telegram protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass EgressConsentRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("EgressConsentRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODEL + model_marker, 1)
    route_marker = '    @router.post("/v1/workspaces/{workspace_id}/connectors/telegram/poll")\n'
    if route_marker not in content:
        raise RuntimeError("Telegram poll route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def update_state_identity() -> None:
    path = "services/api/src/finance_agent/storage/state_identity.py"
    content = read(path)
    marker = '''    commitments = _rows(
        store,
        """
        SELECT commitment_id, label, amount_minor, currency, due_on, recurrence,
               recurrence_count, status, source, evidence_ids_json, updated_at
        FROM cash_commitments
        WHERE workspace_id = ?
        ORDER BY due_on, commitment_id
        """,
        (workspace_id,),
    )
'''
    addition = marker + '''    settlements = _rows(
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
    if "settlements = _rows(" not in content:
        if marker not in content:
            raise RuntimeError("commitment identity marker missing")
        content = content.replace(marker, addition, 1)
    payload_marker = '        "cashCommitments": commitments,\n'
    if '"invoiceSettlements": settlements' not in content:
        if payload_marker not in content:
            raise RuntimeError("commitments identity payload missing")
        content = content.replace(
            payload_marker,
            payload_marker + '        "invoiceSettlements": settlements,\n',
            1,
        )
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/finance/test_receivables_matching.py", TESTS)
    write("docs/RECEIVABLES.md", '''# Receivables and settlement confirmation\n\nIssuing an invoice creates a planned cash commitment for its exact gross amount and due date. Folio can scan posted bank credits for deterministic settlement candidates. A candidate requires exact NZD gross amount and a date between invoice issue and 30 days after due date. Invoice-number and buyer-name references increase the score but never settle the invoice automatically.\n\nThe owner must confirm the exact candidate and provide a reason. Confirmation creates an append-only settlement event, binds one invoice to one bank transaction, retains the transaction evidence and completes the matching cash commitment. Rejection records no settlement and leaves expected cash unchanged.\n\nAn issued invoice, a proposed match and a confirmed settlement are separate states. Folio does not infer delivery, acceptance, payment or reconciliation from invoice generation or amount similarity.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 27: evidence-backed receivables settlement\n\n- Issued invoices create explicit planned cash commitments for exact gross amounts.\n- Posted credits can become deterministic candidates through exact amount and bounded date rules.\n- Invoice-number and buyer-name references increase candidate score but never auto-settle.\n- Owner confirmation creates a one-to-one append-only settlement event with evidence.\n- Confirmed settlement completes the matching expected-cash commitment.\n- Invoice issue, match proposal, settlement and reconciliation remain distinct proof states.\n'''
    if "## Stack 27: evidence-backed receivables settlement" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_services()
    update_protocol_routes()
    update_state_identity()
    tests_docs()
    print("receivables matching changes applied")


if __name__ == "__main__":
    main()
