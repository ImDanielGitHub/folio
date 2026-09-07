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
        name="internal_transfer_matching",
        sql="""
        CREATE TABLE transfer_match_candidates (
            candidate_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            debit_transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
            credit_transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
            score_basis_points INTEGER NOT NULL CHECK (
                score_basis_points BETWEEN 0 AND 10000
            ),
            factors_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),
            created_at TEXT NOT NULL,
            decided_at TEXT,
            CHECK (debit_transaction_id != credit_transaction_id),
            UNIQUE (workspace_id, debit_transaction_id, credit_transaction_id)
        );

        CREATE TABLE transfer_match_events (
            event_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            candidate_id TEXT NOT NULL REFERENCES transfer_match_candidates(candidate_id),
            event_type TEXT NOT NULL CHECK (event_type IN ('confirmed', 'undone')),
            actor TEXT NOT NULL CHECK (actor = 'owner'),
            reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
            debit_transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
            credit_transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            reverses_event_id TEXT REFERENCES transfer_match_events(event_id)
        );

        CREATE UNIQUE INDEX transfer_one_active_debit
            ON transfer_match_events(debit_transaction_id)
            WHERE event_type = 'confirmed' AND event_id NOT IN (
                SELECT reverses_event_id FROM transfer_match_events
                WHERE event_type = 'undone' AND reverses_event_id IS NOT NULL
            );

        CREATE INDEX transfer_candidates_status
            ON transfer_match_candidates(workspace_id, status, created_at);
        """,
    ),
'''

MODULE = '''"""Deterministic internal-transfer candidates with explicit owner confirmation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json

MAX_DATE_DISTANCE_DAYS = 3
MIN_SCORE = 8000


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:24]}"


def _normalise(value: str) -> str:
    return " ".join("".join(character if character.isalnum() else " " for character in value.upper()).split())


def _state(row: Any) -> dict[str, object]:
    return {
        "classification": str(row["classification"]),
        "category": str(row["category"]) if row["category"] else None,
        "classificationSource": str(row["classification_source"]),
        "ruleId": str(row["rule_id"]) if row["rule_id"] else None,
    }


class InternalTransferService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def scan(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        rows = self.store.fetch_all(
            """
            SELECT t.transaction_id, t.account_id, a.name AS account_name,
                   t.occurred_on, t.description, t.amount_minor, t.currency,
                   t.classification, t.category, t.classification_source,
                   t.rule_id, t.evidence_id
            FROM transactions t
            JOIN accounts a ON a.account_id = t.account_id
            WHERE t.workspace_id = ? AND t.status = 'posted'
              AND t.source_status = 'posted' AND t.amount_minor != 0
              AND t.classification != 'transfer'
              AND t.transaction_id NOT IN (
                SELECT debit_transaction_id FROM transfer_match_events
                WHERE event_type = 'confirmed' AND event_id NOT IN (
                    SELECT reverses_event_id FROM transfer_match_events
                    WHERE event_type = 'undone' AND reverses_event_id IS NOT NULL
                )
                UNION
                SELECT credit_transaction_id FROM transfer_match_events
                WHERE event_type = 'confirmed' AND event_id NOT IN (
                    SELECT reverses_event_id FROM transfer_match_events
                    WHERE event_type = 'undone' AND reverses_event_id IS NOT NULL
                )
              )
            ORDER BY t.occurred_on, t.transaction_id
            """,
            (workspace_id,),
        )
        debits = [row for row in rows if int(row["amount_minor"]) < 0]
        credits = [row for row in rows if int(row["amount_minor"]) > 0]
        values: list[dict[str, object]] = []
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            for debit in debits:
                for credit in credits:
                    if str(debit["account_id"]) == str(credit["account_id"]):
                        continue
                    if str(debit["currency"]) != str(credit["currency"]):
                        continue
                    if abs(int(debit["amount_minor"])) != int(credit["amount_minor"]):
                        continue
                    debit_date = date.fromisoformat(str(debit["occurred_on"]))
                    credit_date = date.fromisoformat(str(credit["occurred_on"]))
                    distance = abs((credit_date - debit_date).days)
                    if distance > MAX_DATE_DISTANCE_DAYS:
                        continue
                    debit_description = _normalise(str(debit["description"]))
                    credit_description = _normalise(str(credit["description"]))
                    account_reference = (
                        _normalise(str(debit["account_name"])) in credit_description
                        or _normalise(str(credit["account_name"])) in debit_description
                    )
                    transfer_words = any(
                        word in debit_description or word in credit_description
                        for word in ("TRANSFER", "XFER", "INTERNAL", "ONLINE BANKING")
                    )
                    score = 8000
                    if distance == 0:
                        score += 1000
                    elif distance == 1:
                        score += 600
                    elif distance == 2:
                        score += 300
                    if account_reference:
                        score += 500
                    if transfer_words:
                        score += 500
                    score = min(score, 10000)
                    if score < MIN_SCORE:
                        continue
                    candidate_id = _stable_id(
                        "transfercandidate",
                        workspace_id,
                        str(debit["transaction_id"]),
                        str(credit["transaction_id"]),
                    )
                    factors = {
                        "exactOppositeAmount": True,
                        "differentAccounts": True,
                        "dateDistanceDays": distance,
                        "sameCurrency": True,
                        "accountNameReference": account_reference,
                        "transferWordReference": transfer_words,
                    }
                    connection.execute(
                        """
                        INSERT INTO transfer_match_candidates(
                            candidate_id, workspace_id, debit_transaction_id,
                            credit_transaction_id, score_basis_points,
                            factors_json, status, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)
                        ON CONFLICT(workspace_id, debit_transaction_id, credit_transaction_id)
                        DO UPDATE SET
                            score_basis_points = excluded.score_basis_points,
                            factors_json = excluded.factors_json
                        """,
                        (
                            candidate_id,
                            workspace_id,
                            debit["transaction_id"],
                            credit["transaction_id"],
                            score,
                            canonical_json(factors),
                            now,
                        ),
                    )
                    values.append(
                        {
                            "candidateId": candidate_id,
                            "debit": {
                                "transactionId": str(debit["transaction_id"]),
                                "accountId": str(debit["account_id"]),
                                "accountName": str(debit["account_name"]),
                                "occurredOn": str(debit["occurred_on"]),
                                "description": str(debit["description"]),
                                "amountMinor": int(debit["amount_minor"]),
                                "currency": str(debit["currency"]),
                                "evidenceIds": [str(debit["evidence_id"])],
                            },
                            "credit": {
                                "transactionId": str(credit["transaction_id"]),
                                "accountId": str(credit["account_id"]),
                                "accountName": str(credit["account_name"]),
                                "occurredOn": str(credit["occurred_on"]),
                                "description": str(credit["description"]),
                                "amountMinor": int(credit["amount_minor"]),
                                "currency": str(credit["currency"]),
                                "evidenceIds": [str(credit["evidence_id"])],
                            },
                            "scoreBasisPoints": score,
                            "factors": factors,
                            "status": "proposed",
                            "committed": False,
                        }
                    )
        return tuple(
            sorted(
                values,
                key=lambda value: (
                    -int(value["scoreBasisPoints"]),
                    str(value["candidateId"]),
                ),
            )
        )

    def list_candidates(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        rows = self.store.fetch_all(
            """
            SELECT c.*, d.occurred_on AS debit_date,
                   d.description AS debit_description, d.amount_minor AS debit_amount,
                   d.currency, d.account_id AS debit_account, d.evidence_id AS debit_evidence,
                   cr.occurred_on AS credit_date,
                   cr.description AS credit_description, cr.amount_minor AS credit_amount,
                   cr.account_id AS credit_account, cr.evidence_id AS credit_evidence
            FROM transfer_match_candidates c
            JOIN transactions d ON d.transaction_id = c.debit_transaction_id
            JOIN transactions cr ON cr.transaction_id = c.credit_transaction_id
            WHERE c.workspace_id = ?
            ORDER BY c.created_at DESC, c.candidate_id
            """,
            (workspace_id,),
        )
        return tuple(
            {
                "candidateId": str(row["candidate_id"]),
                "debitTransactionId": str(row["debit_transaction_id"]),
                "creditTransactionId": str(row["credit_transaction_id"]),
                "debitAccountId": str(row["debit_account"]),
                "creditAccountId": str(row["credit_account"]),
                "debitOccurredOn": str(row["debit_date"]),
                "creditOccurredOn": str(row["credit_date"]),
                "debitDescription": str(row["debit_description"]),
                "creditDescription": str(row["credit_description"]),
                "amountMinor": int(row["credit_amount"]),
                "currency": str(row["currency"]),
                "scoreBasisPoints": int(row["score_basis_points"]),
                "factors": json.loads(str(row["factors_json"])),
                "status": str(row["status"]),
                "evidenceIds": [str(row["debit_evidence"]), str(row["credit_evidence"])],
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
            raise ValueError("transfer confirmation reason is required")
        now = datetime.now(UTC).isoformat()
        try:
            with self.store.transaction() as connection:
                candidate = connection.execute(
                    """
                    SELECT c.*, d.evidence_id AS debit_evidence,
                           cr.evidence_id AS credit_evidence,
                           d.classification AS debit_classification,
                           d.category AS debit_category,
                           d.classification_source AS debit_source,
                           d.rule_id AS debit_rule_id,
                           cr.classification AS credit_classification,
                           cr.category AS credit_category,
                           cr.classification_source AS credit_source,
                           cr.rule_id AS credit_rule_id
                    FROM transfer_match_candidates c
                    JOIN transactions d ON d.transaction_id = c.debit_transaction_id
                    JOIN transactions cr ON cr.transaction_id = c.credit_transaction_id
                    WHERE c.workspace_id = ? AND c.candidate_id = ?
                    """,
                    (workspace_id, candidate_id),
                ).fetchone()
                if candidate is None:
                    raise KeyError(candidate_id)
                existing = connection.execute(
                    """
                    SELECT event_id FROM transfer_match_events
                    WHERE candidate_id = ? AND event_type = 'confirmed'
                      AND event_id NOT IN (
                        SELECT reverses_event_id FROM transfer_match_events
                        WHERE event_type = 'undone' AND reverses_event_id IS NOT NULL
                      )
                    """,
                    (candidate_id,),
                ).fetchone()
                if existing is not None:
                    return {
                        "candidateId": candidate_id,
                        "eventId": str(existing["event_id"]),
                        "status": "confirmed",
                        "idempotentReplay": True,
                    }
                if str(candidate["status"]) not in {"proposed", "confirmed"}:
                    raise ValueError("only a proposed transfer candidate can be confirmed")
                debit_before = {
                    "classification": str(candidate["debit_classification"]),
                    "category": str(candidate["debit_category"]) if candidate["debit_category"] else None,
                    "classificationSource": str(candidate["debit_source"]),
                    "ruleId": str(candidate["debit_rule_id"]) if candidate["debit_rule_id"] else None,
                }
                credit_before = {
                    "classification": str(candidate["credit_classification"]),
                    "category": str(candidate["credit_category"]) if candidate["credit_category"] else None,
                    "classificationSource": str(candidate["credit_source"]),
                    "ruleId": str(candidate["credit_rule_id"]) if candidate["credit_rule_id"] else None,
                }
                after = {
                    "classification": "transfer",
                    "category": "internal_transfer",
                    "classificationSource": "accepted_feedback",
                    "ruleId": None,
                }
                connection.execute(
                    """
                    UPDATE transactions
                    SET classification = 'transfer', category = 'internal_transfer',
                        classification_source = 'accepted_feedback', rule_id = NULL,
                        updated_at = ?
                    WHERE transaction_id IN (?, ?)
                    """,
                    (
                        now,
                        candidate["debit_transaction_id"],
                        candidate["credit_transaction_id"],
                    ),
                )
                event_id = _stable_id(
                    "transferevent", workspace_id, candidate_id, "confirmed", now
                )
                evidence_ids = [
                    str(candidate["debit_evidence"]),
                    str(candidate["credit_evidence"]),
                ]
                connection.execute(
                    """
                    INSERT INTO transfer_match_events(
                        event_id, workspace_id, candidate_id, event_type, actor,
                        reason, debit_transaction_id, credit_transaction_id,
                        before_json, after_json, evidence_ids_json, occurred_at,
                        reverses_event_id
                    ) VALUES (?, ?, ?, 'confirmed', 'owner', ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        event_id,
                        workspace_id,
                        candidate_id,
                        reason_value[:500],
                        candidate["debit_transaction_id"],
                        candidate["credit_transaction_id"],
                        canonical_json({"debit": debit_before, "credit": credit_before}),
                        canonical_json({"debit": after, "credit": after}),
                        canonical_json(evidence_ids),
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE transfer_match_candidates SET status = 'confirmed', decided_at = ? WHERE candidate_id = ?",
                    (now, candidate_id),
                )
        except sqlite3.IntegrityError as exc:
            if "hard locked" in str(exc):
                raise PermissionError("one or both transfer rows are in a hard-locked period") from exc
            raise
        return {
            "candidateId": candidate_id,
            "eventId": event_id,
            "status": "confirmed",
            "debitTransactionId": str(candidate["debit_transaction_id"]),
            "creditTransactionId": str(candidate["credit_transaction_id"]),
            "evidenceIds": evidence_ids,
            "occurredAt": now,
        }

    def undo(
        self,
        *,
        workspace_id: str,
        event_id: str,
        reason: str,
    ) -> dict[str, object]:
        reason_value = reason.strip()
        if not reason_value:
            raise ValueError("transfer undo reason is required")
        now = datetime.now(UTC).isoformat()
        try:
            with self.store.transaction() as connection:
                event = connection.execute(
                    """
                    SELECT * FROM transfer_match_events
                    WHERE workspace_id = ? AND event_id = ? AND event_type = 'confirmed'
                    """,
                    (workspace_id, event_id),
                ).fetchone()
                if event is None:
                    raise KeyError(event_id)
                undone = connection.execute(
                    "SELECT event_id FROM transfer_match_events WHERE reverses_event_id = ? AND event_type = 'undone'",
                    (event_id,),
                ).fetchone()
                if undone is not None:
                    return {
                        "eventId": str(undone["event_id"]),
                        "reversesEventId": event_id,
                        "status": "undone",
                        "idempotentReplay": True,
                    }
                before = json.loads(str(event["before_json"]))
                for side, transaction_id in (
                    ("debit", str(event["debit_transaction_id"])),
                    ("credit", str(event["credit_transaction_id"])),
                ):
                    state = before[side]
                    connection.execute(
                        """
                        UPDATE transactions
                        SET classification = ?, category = ?, classification_source = ?,
                            rule_id = ?, updated_at = ?
                        WHERE transaction_id = ?
                        """,
                        (
                            state["classification"],
                            state["category"],
                            state["classificationSource"],
                            state["ruleId"],
                            now,
                            transaction_id,
                        ),
                    )
                undo_id = _stable_id(
                    "transferevent", workspace_id, event_id, "undone", now
                )
                connection.execute(
                    """
                    INSERT INTO transfer_match_events(
                        event_id, workspace_id, candidate_id, event_type, actor,
                        reason, debit_transaction_id, credit_transaction_id,
                        before_json, after_json, evidence_ids_json, occurred_at,
                        reverses_event_id
                    ) VALUES (?, ?, ?, 'undone', 'owner', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        undo_id,
                        workspace_id,
                        event["candidate_id"],
                        reason_value[:500],
                        event["debit_transaction_id"],
                        event["credit_transaction_id"],
                        event["after_json"],
                        event["before_json"],
                        event["evidence_ids_json"],
                        now,
                        event_id,
                    ),
                )
                connection.execute(
                    "UPDATE transfer_match_candidates SET status = 'proposed', decided_at = ? WHERE candidate_id = ?",
                    (now, event["candidate_id"]),
                )
        except sqlite3.IntegrityError as exc:
            if "hard locked" in str(exc):
                raise PermissionError("one or both transfer rows are in a hard-locked period") from exc
            raise
        return {
            "eventId": undo_id,
            "reversesEventId": event_id,
            "candidateId": str(event["candidate_id"]),
            "status": "undone",
            "occurredAt": now,
        }
'''

SERVICE_METHODS = '''    async def scan_internal_transfers(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return InternalTransferService(self.store).scan(workspace_id)

    async def list_internal_transfer_candidates(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return InternalTransferService(self.store).list_candidates(workspace_id)

    async def confirm_internal_transfer(
        self, *, workspace_id: str, candidate_id: str, reason: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = InternalTransferService(self.store).confirm(
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                reason=reason,
            )
            result = self.daily_close.run()
            self._register_daily_close_events(result)
        return {**value, "dailyCloseRunId": result.run_id}

    async def undo_internal_transfer(
        self, *, workspace_id: str, event_id: str, reason: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = InternalTransferService(self.store).undo(
                workspace_id=workspace_id,
                event_id=event_id,
                reason=reason,
            )
            result = self.daily_close.run()
            self._register_daily_close_events(result)
        return {**value, "dailyCloseRunId": result.run_id}
'''

ROUTE_MODEL = '''

class TransferDecisionRequest(RequestModel):
    reason: str = Field(min_length=1, max_length=500)
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/transfers/scan")
    async def scan_internal_transfers(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        candidates = await services.scan_internal_transfers(workspace_id=workspace_id)
        return {"workspaceId": workspace_id, "candidates": list(candidates)}

    @router.get("/v1/workspaces/{workspace_id}/transfers/candidates")
    async def list_internal_transfer_candidates(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        candidates = await services.list_internal_transfer_candidates(
            workspace_id=workspace_id
        )
        return {"workspaceId": workspace_id, "candidates": list(candidates)}

    @router.post(
        "/v1/workspaces/{workspace_id}/transfers/candidates/{candidate_id}/confirm"
    )
    async def confirm_internal_transfer(
        workspace_id: PathIdentifier,
        candidate_id: PathIdentifier,
        body: TransferDecisionRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.confirm_internal_transfer(
                    workspace_id=workspace_id,
                    candidate_id=candidate_id,
                    reason=body.reason,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="transfer candidate not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/v1/workspaces/{workspace_id}/transfers/events/{event_id}/undo")
    async def undo_internal_transfer(
        workspace_id: PathIdentifier,
        event_id: PathIdentifier,
        body: TransferDecisionRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.undo_internal_transfer(
                    workspace_id=workspace_id,
                    event_id=event_id,
                    reason=body.reason,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="transfer event not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

import hashlib
from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.finance.transfers import InternalTransferService
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore, canonical_json

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def add_transaction(
    store: SQLiteStore,
    *,
    transaction_id: str,
    account_id: str,
    occurred_on: str,
    description: str,
    amount_minor: int,
) -> None:
    source_row_id = f"row_{transaction_id.removeprefix('txn_')}"
    evidence_id = f"evd_{transaction_id.removeprefix('txn_')}"
    raw = canonical_json({
        "occurredOn": occurred_on,
        "description": description,
        "amountMinor": amount_minor,
    })
    row_hash = hashlib.sha256(raw.encode()).hexdigest()
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO source_rows(
                source_row_id, source_item_id, row_number, account_id,
                occurred_on, description, amount_minor, currency, source_status,
                external_reference, mapping_version, row_hash, raw_json
            ) VALUES (?, 'src_koru_bank_csv_20260717', ?, ?, ?, ?, ?, 'NZD',
                'posted', ?, 'transfer_test@1', ?, ?)
            """,
            (
                source_row_id,
                1000 + abs(amount_minor),
                account_id,
                occurred_on,
                description,
                amount_minor,
                transaction_id,
                row_hash,
                raw,
            ),
        )
        connection.execute(
            """
            INSERT INTO evidence_links(
                evidence_id, workspace_id, source_item_id, source_row_id,
                label, created_at
            ) VALUES (?, 'ws_koru_studio', 'src_koru_bank_csv_20260717', ?, ?,
                '2026-08-26T00:00:00+00:00')
            """,
            (evidence_id, source_row_id, description),
        )
        connection.execute(
            """
            INSERT INTO transactions(
                transaction_id, workspace_id, account_id, source_row_id, evidence_id,
                occurred_on, description, amount_minor, currency, source_status, status,
                classification, category, classification_source, rule_id,
                duplicate_of_transaction_id, created_at, updated_at
            ) VALUES (?, 'ws_koru_studio', ?, ?, ?, ?, ?, ?, 'NZD', 'posted',
                'posted', ?, ?, 'deterministic', NULL, NULL,
                '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00')
            """,
            (
                transaction_id,
                account_id,
                source_row_id,
                evidence_id,
                occurred_on,
                description,
                amount_minor,
                "business" if amount_minor > 0 else "unresolved",
                "client_income" if amount_minor > 0 else None,
            ),
        )


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    DailyCloseService(engine).run()
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO accounts(account_id, workspace_id, name, currency, created_at)
            VALUES ('acct_koru_savings', 'ws_koru_studio', 'Koru Savings', 'NZD',
                '2026-08-26T00:00:00+00:00')
            """
        )
    add_transaction(
        store,
        transaction_id="txn_transfer_out",
        account_id="acct_koru_business",
        occurred_on="2026-08-20",
        description="Online banking transfer to Koru Savings",
        amount_minor=-50000,
    )
    add_transaction(
        store,
        transaction_id="txn_transfer_in",
        account_id="acct_koru_savings",
        occurred_on="2026-08-20",
        description="Internal transfer from business account",
        amount_minor=50000,
    )
    return store, engine, InternalTransferService(store)


def test_scan_proposes_exact_opposite_cross_account_pair_without_mutation(tmp_path: Path) -> None:
    store, _engine, service = setup(tmp_path)
    values = service.scan("ws_koru_studio")
    candidate = next(
        value for value in values
        if value["debit"]["transactionId"] == "txn_transfer_out"
        and value["credit"]["transactionId"] == "txn_transfer_in"
    )
    assert candidate["committed"] is False
    assert candidate["scoreBasisPoints"] == 10000
    assert candidate["factors"]["exactOppositeAmount"] is True
    rows = store.fetch_all(
        "SELECT transaction_id, classification FROM transactions WHERE transaction_id LIKE 'txn_transfer_%' ORDER BY transaction_id"
    )
    assert {str(row["classification"]) for row in rows} != {"transfer"}


def test_confirmation_marks_both_sides_transfer_and_undo_restores_exact_prior_state(tmp_path: Path) -> None:
    store, _engine, service = setup(tmp_path)
    candidate = next(
        value for value in service.scan("ws_koru_studio")
        if value["debit"]["transactionId"] == "txn_transfer_out"
    )
    confirmed = service.confirm(
        workspace_id="ws_koru_studio",
        candidate_id=str(candidate["candidateId"]),
        reason="Owner confirmed this was a movement between Koru accounts.",
    )
    assert confirmed["status"] == "confirmed"
    rows = store.fetch_all(
        "SELECT classification, category FROM transactions WHERE transaction_id IN ('txn_transfer_out', 'txn_transfer_in')"
    )
    assert all(str(row["classification"]) == "transfer" for row in rows)
    assert all(str(row["category"]) == "internal_transfer" for row in rows)
    replay = service.confirm(
        workspace_id="ws_koru_studio",
        candidate_id=str(candidate["candidateId"]),
        reason="Repeated confirmation.",
    )
    assert replay["idempotentReplay"] is True
    undone = service.undo(
        workspace_id="ws_koru_studio",
        event_id=str(confirmed["eventId"]),
        reason="Owner reversed the transfer match.",
    )
    assert undone["status"] == "undone"
    debit = store.fetch_one(
        "SELECT classification, category FROM transactions WHERE transaction_id = 'txn_transfer_out'"
    )
    credit = store.fetch_one(
        "SELECT classification, category FROM transactions WHERE transaction_id = 'txn_transfer_in'"
    )
    assert (str(debit["classification"]), debit["category"]) == ("unresolved", None)
    assert (str(credit["classification"]), str(credit["category"])) == (
        "business", "client_income"
    )
    assert len(store.fetch_all("SELECT * FROM transfer_match_events")) == 2


def test_same_account_amount_mismatch_and_date_distance_do_not_pair(tmp_path: Path) -> None:
    store, _engine, service = setup(tmp_path)
    add_transaction(
        store,
        transaction_id="txn_same_account_credit",
        account_id="acct_koru_business",
        occurred_on="2026-08-20",
        description="Same account credit",
        amount_minor=50000,
    )
    add_transaction(
        store,
        transaction_id="txn_mismatch_credit",
        account_id="acct_koru_savings",
        occurred_on="2026-08-20",
        description="Wrong amount",
        amount_minor=49999,
    )
    add_transaction(
        store,
        transaction_id="txn_far_credit",
        account_id="acct_koru_savings",
        occurred_on="2026-08-30",
        description="Far transfer",
        amount_minor=50000,
    )
    values = service.scan("ws_koru_studio")
    pairs = {
        (value["debit"]["transactionId"], value["credit"]["transactionId"])
        for value in values
    }
    assert ("txn_transfer_out", "txn_same_account_credit") not in pairs
    assert ("txn_transfer_out", "txn_mismatch_credit") not in pairs
    assert ("txn_transfer_out", "txn_far_credit") not in pairs
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
    write("services/api/src/finance_agent/finance/transfers.py", MODULE)


def update_service_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.import_preview import CSVImportPreviewService\n"
    import_line = "from finance_agent.finance.transfers import InternalTransferService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("CSV preview import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "preview_csv_import", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def preview_csv_import(\n"
    addition = '''    async def scan_internal_transfers(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def list_internal_transfer_candidates(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def confirm_internal_transfer(\n        self, *, workspace_id: str, candidate_id: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n    async def undo_internal_transfer(\n        self, *, workspace_id: str, event_id: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("CSV preview protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass ClassificationRulePreviewRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("ClassificationRulePreviewRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODEL + model_marker, 1)
    route_marker = '    @router.post("/v1/ingest/csv/preview")\n'
    if route_marker not in content:
        raise RuntimeError("CSV preview route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def update_audit_state_identity() -> None:
    path = "services/api/src/finance_agent/audit_trail.py"
    content = read(path)
    kind_marker = '        "accounting_period",\n'
    if '"internal_transfer"' not in content:
        if kind_marker not in content:
            raise RuntimeError("accounting period audit kind marker missing")
        content = content.replace(kind_marker, kind_marker + '        "internal_transfer",\n', 1)
    optional_marker = '        if self._table_exists("accounting_period_revisions"):\n'
    block = '''        if self._table_exists("transfer_match_events"):
            for row in self.store.fetch_all(
                "SELECT * FROM transfer_match_events WHERE workspace_id = ? ORDER BY occurred_at, event_id",
                (workspace_id,),
            ):
                yield AuditEvent(
                    event_id=str(row["event_id"]),
                    workspace_id=workspace_id,
                    kind="internal_transfer",
                    action=f"transfer_{row['event_type']}",
                    status=str(row["event_type"]),
                    occurred_at=str(row["occurred_at"]),
                    actor="owner",
                    correlation_id=None,
                    subject_type="transfer_candidate",
                    subject_id=str(row["candidate_id"]),
                    evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
                    metadata={
                        "debitTransactionId": str(row["debit_transaction_id"]),
                        "creditTransactionId": str(row["credit_transaction_id"]),
                        "reversesEventId": str(row["reverses_event_id"]) if row["reverses_event_id"] else None,
                        "reasonIncluded": False,
                    },
                )
'''
    if "transfer_confirmed" not in content:
        if optional_marker not in content:
            raise RuntimeError("accounting period optional audit marker missing")
        content = content.replace(optional_marker, block + optional_marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/storage/state_identity.py"
    content = read(path)
    marker = '''    accounting_periods = _rows(
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
    addition = marker + '''    transfer_events = _rows(
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
    if "transfer_events = _rows(" not in content:
        if marker not in content:
            raise RuntimeError("accounting period identity marker missing")
        content = content.replace(marker, addition, 1)
    payload_marker = '        "accountingPeriods": accounting_periods,\n'
    if '"transferEvents": transfer_events' not in content:
        if payload_marker not in content:
            raise RuntimeError("accounting period identity payload missing")
        content = content.replace(
            payload_marker,
            payload_marker + '        "transferEvents": transfer_events,\n',
            1,
        )
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/finance/test_internal_transfer_matching.py", TESTS)
    write("docs/INTERNAL_TRANSFERS.md", '''# Internal transfer matching\n\nFolio scans posted movements across different accounts in the same workspace. A candidate requires exact opposite NZD amounts and dates no more than three days apart. Same-day timing, account-name references and transfer words increase a deterministic score. Same-account, amount-mismatched, foreign-currency or date-distant rows are excluded. Scanning does not change either transaction.\n\nOwner confirmation records both linked evidence items, saves each side's exact prior classification state and marks the debit and credit as `transfer/internal_transfer`. One transaction cannot belong to two active confirmed matches. Undo appends a reversing event and restores both prior states exactly. Hard-locked accounting periods block confirmation and Undo at SQLite level.\n\nA candidate is not proof that money moved internally. It is a bounded comparison that still requires owner judgement. Confirmation is not external bank reconciliation.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 39: deterministic internal-transfer matching\n\n- Candidates require exact opposite amounts, different accounts and bounded dates.\n- Timing, account references and transfer words affect a visible deterministic score.\n- Scanning never changes classification.\n- Owner confirmation links both evidence items and records exact prior states.\n- Undo appends a reversal and restores both sides exactly.\n- Hard locks remain authoritative and candidates are not external reconciliation proof.\n'''
    if "## Stack 39: deterministic internal-transfer matching" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_service_protocol_routes()
    update_audit_state_identity()
    tests_docs()
    print("internal transfer matching changes applied")


if __name__ == "__main__":
    main()
