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
        name="duplicate_review_workflow",
        sql="""
        CREATE TABLE duplicate_review_candidates (
            candidate_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            transaction_a_id TEXT NOT NULL REFERENCES transactions(transaction_id),
            transaction_b_id TEXT NOT NULL REFERENCES transactions(transaction_id),
            score_basis_points INTEGER NOT NULL CHECK (
                score_basis_points BETWEEN 0 AND 10000
            ),
            factors_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('proposed', 'confirmed', 'rejected')),
            created_at TEXT NOT NULL,
            decided_at TEXT,
            CHECK (transaction_a_id < transaction_b_id),
            UNIQUE (workspace_id, transaction_a_id, transaction_b_id)
        );

        CREATE TABLE duplicate_review_events (
            event_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            candidate_id TEXT NOT NULL REFERENCES duplicate_review_candidates(candidate_id),
            event_type TEXT NOT NULL CHECK (
                event_type IN ('confirmed', 'rejected', 'undone')
            ),
            actor TEXT NOT NULL CHECK (actor = 'owner'),
            keeper_transaction_id TEXT REFERENCES transactions(transaction_id),
            duplicate_transaction_id TEXT REFERENCES transactions(transaction_id),
            reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            reverses_event_id TEXT REFERENCES duplicate_review_events(event_id)
        );

        CREATE INDEX duplicate_candidates_status
            ON duplicate_review_candidates(workspace_id, status, created_at DESC);
        CREATE INDEX duplicate_events_duplicate
            ON duplicate_review_events(workspace_id, duplicate_transaction_id, occurred_at);
        CREATE INDEX duplicate_events_reversal
            ON duplicate_review_events(reverses_event_id, event_type);
        """,
    ),
'''

MODULE = '''"""Deterministic duplicate candidates with owner-selected keeper and exact Undo."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, date, datetime
from itertools import combinations
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json

TOKEN_RE = re.compile(r"[A-Z0-9]+")
STOP = frozenset({"THE", "A", "AN", "NZ", "LIMITED", "LTD", "PAYMENT", "PURCHASE"})
MAX_DATE_DISTANCE_DAYS = 3
MIN_SCORE = 8000


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:24]}"


def _tokens(value: str) -> frozenset[str]:
    return frozenset(
        token for token in TOKEN_RE.findall(value.upper()) if token not in STOP
    )


def _similarity(left: str, right: str) -> int:
    a = _tokens(left)
    b = _tokens(right)
    if not a or not b:
        return 0
    return int(len(a & b) * 10000 / len(a | b))


def _row_value(row: Any) -> dict[str, object]:
    return {
        "transactionId": str(row["transaction_id"]),
        "sourceItemId": str(row["source_item_id"]),
        "accountId": str(row["account_id"]),
        "occurredOn": str(row["occurred_on"]),
        "description": str(row["description"]),
        "amountMinor": int(row["amount_minor"]),
        "currency": str(row["currency"]),
        "status": str(row["status"]),
        "sourceStatus": str(row["source_status"]),
        "evidenceIds": [str(row["evidence_id"])],
    }


class DuplicateReviewService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def _transactions(self, workspace_id: str) -> list[Any]:
        return self.store.fetch_all(
            """
            SELECT t.*, r.source_item_id
            FROM transactions t
            JOIN source_rows r ON r.source_row_id = t.source_row_id
            WHERE t.workspace_id = ?
              AND t.status IN ('posted', 'pending')
              AND t.classification != 'transfer'
            ORDER BY t.occurred_on, t.transaction_id
            """,
            (workspace_id,),
        )

    def scan(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        values: list[dict[str, object]] = []
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            for left, right in combinations(self._transactions(workspace_id), 2):
                if int(left["amount_minor"]) != int(right["amount_minor"]):
                    continue
                if str(left["currency"]) != str(right["currency"]):
                    continue
                distance = abs(
                    (
                        date.fromisoformat(str(left["occurred_on"]))
                        - date.fromisoformat(str(right["occurred_on"]))
                    ).days
                )
                if distance > MAX_DATE_DISTANCE_DAYS:
                    continue
                similarity = _similarity(
                    str(left["description"]), str(right["description"])
                )
                if similarity < 5000:
                    continue
                date_score = (2000, 1500, 1000, 500)[distance]
                description_score = 2500 if similarity == 10000 else int(similarity / 4)
                cross_source = str(left["source_item_id"]) != str(right["source_item_id"])
                score = min(10000, 5000 + date_score + description_score + (500 if cross_source else 0))
                if score < MIN_SCORE:
                    continue
                transaction_ids = sorted(
                    (str(left["transaction_id"]), str(right["transaction_id"]))
                )
                candidate_id = _stable_id(
                    "duplicatecandidate", workspace_id, *transaction_ids
                )
                factors = {
                    "exactAmount": True,
                    "sameCurrency": True,
                    "dateDistanceDays": distance,
                    "descriptionSimilarityBasisPoints": similarity,
                    "crossSource": cross_source,
                }
                connection.execute(
                    """
                    INSERT INTO duplicate_review_candidates(
                        candidate_id, workspace_id, transaction_a_id,
                        transaction_b_id, score_basis_points, factors_json,
                        status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)
                    ON CONFLICT(workspace_id, transaction_a_id, transaction_b_id)
                    DO UPDATE SET
                        score_basis_points = excluded.score_basis_points,
                        factors_json = excluded.factors_json
                    """,
                    (
                        candidate_id,
                        workspace_id,
                        transaction_ids[0],
                        transaction_ids[1],
                        score,
                        canonical_json(factors),
                        now,
                    ),
                )
                values.append(
                    {
                        "candidateId": candidate_id,
                        "transactionA": _row_value(
                            left
                            if str(left["transaction_id"]) == transaction_ids[0]
                            else right
                        ),
                        "transactionB": _row_value(
                            right
                            if str(right["transaction_id"]) == transaction_ids[1]
                            else left
                        ),
                        "scoreBasisPoints": score,
                        "factors": factors,
                        "status": "proposed",
                        "committed": False,
                    }
                )
        return tuple(
            sorted(
                values,
                key=lambda value: (-int(value["scoreBasisPoints"]), str(value["candidateId"])),
            )
        )

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        rows = self.store.fetch_all(
            """
            SELECT c.*, a.occurred_on AS a_date, a.description AS a_description,
                   a.amount_minor AS a_amount, a.currency AS a_currency,
                   a.evidence_id AS a_evidence, a.status AS a_status,
                   b.occurred_on AS b_date, b.description AS b_description,
                   b.amount_minor AS b_amount, b.currency AS b_currency,
                   b.evidence_id AS b_evidence, b.status AS b_status
            FROM duplicate_review_candidates c
            JOIN transactions a ON a.transaction_id = c.transaction_a_id
            JOIN transactions b ON b.transaction_id = c.transaction_b_id
            WHERE c.workspace_id = ?
            ORDER BY c.created_at DESC, c.candidate_id
            """,
            (workspace_id,),
        )
        return tuple(
            {
                "candidateId": str(row["candidate_id"]),
                "transactionA": {
                    "transactionId": str(row["transaction_a_id"]),
                    "occurredOn": str(row["a_date"]),
                    "description": str(row["a_description"]),
                    "amountMinor": int(row["a_amount"]),
                    "currency": str(row["a_currency"]),
                    "status": str(row["a_status"]),
                    "evidenceIds": [str(row["a_evidence"])],
                },
                "transactionB": {
                    "transactionId": str(row["transaction_b_id"]),
                    "occurredOn": str(row["b_date"]),
                    "description": str(row["b_description"]),
                    "amountMinor": int(row["b_amount"]),
                    "currency": str(row["b_currency"]),
                    "status": str(row["b_status"]),
                    "evidenceIds": [str(row["b_evidence"])],
                },
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
        keeper_transaction_id: str,
        reason: str,
    ) -> dict[str, object]:
        reason_value = reason.strip()
        if not reason_value:
            raise ValueError("duplicate confirmation reason is required")
        now = datetime.now(UTC).isoformat()
        try:
            with self.store.transaction() as connection:
                candidate = connection.execute(
                    "SELECT * FROM duplicate_review_candidates WHERE workspace_id = ? AND candidate_id = ?",
                    (workspace_id, candidate_id),
                ).fetchone()
                if candidate is None:
                    raise KeyError(candidate_id)
                pair = {
                    str(candidate["transaction_a_id"]),
                    str(candidate["transaction_b_id"]),
                }
                if keeper_transaction_id not in pair:
                    raise ValueError("keeperTransactionId must belong to this candidate")
                duplicate_id = next(value for value in pair if value != keeper_transaction_id)
                existing = connection.execute(
                    """
                    SELECT event_id FROM duplicate_review_events
                    WHERE candidate_id = ? AND event_type = 'confirmed'
                      AND event_id NOT IN (
                        SELECT reverses_event_id FROM duplicate_review_events
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
                conflict = connection.execute(
                    """
                    SELECT event_id FROM duplicate_review_events active
                    WHERE active.workspace_id = ? AND active.event_type = 'confirmed'
                      AND active.event_id NOT IN (
                        SELECT reverses_event_id FROM duplicate_review_events
                        WHERE event_type = 'undone' AND reverses_event_id IS NOT NULL
                      )
                      AND active.duplicate_transaction_id = ?
                    LIMIT 1
                    """,
                    (workspace_id, duplicate_id),
                ).fetchone()
                if conflict is not None:
                    raise ValueError("duplicate transaction already has an active duplicate decision")
                keeper = connection.execute(
                    "SELECT * FROM transactions WHERE workspace_id = ? AND transaction_id = ?",
                    (workspace_id, keeper_transaction_id),
                ).fetchone()
                duplicate = connection.execute(
                    "SELECT * FROM transactions WHERE workspace_id = ? AND transaction_id = ?",
                    (workspace_id, duplicate_id),
                ).fetchone()
                if keeper is None or duplicate is None:
                    raise KeyError(candidate_id)
                if str(keeper["status"]) == "duplicate":
                    raise ValueError("keeper transaction is already marked duplicate")
                before = {
                    "status": str(duplicate["status"]),
                    "duplicateOfTransactionId": (
                        str(duplicate["duplicate_of_transaction_id"])
                        if duplicate["duplicate_of_transaction_id"] else None
                    ),
                }
                after = {
                    "status": "duplicate",
                    "duplicateOfTransactionId": keeper_transaction_id,
                }
                connection.execute(
                    """
                    UPDATE transactions
                    SET status = 'duplicate', duplicate_of_transaction_id = ?,
                        updated_at = ? WHERE transaction_id = ?
                    """,
                    (keeper_transaction_id, now, duplicate_id),
                )
                evidence_ids = [
                    str(keeper["evidence_id"]), str(duplicate["evidence_id"])
                ]
                event_id = _stable_id(
                    "duplicateevent", workspace_id, candidate_id, "confirmed", now
                )
                connection.execute(
                    """
                    INSERT INTO duplicate_review_events(
                        event_id, workspace_id, candidate_id, event_type, actor,
                        keeper_transaction_id, duplicate_transaction_id, reason,
                        before_json, after_json, evidence_ids_json, occurred_at,
                        reverses_event_id
                    ) VALUES (?, ?, ?, 'confirmed', 'owner', ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        event_id,
                        workspace_id,
                        candidate_id,
                        keeper_transaction_id,
                        duplicate_id,
                        reason_value[:500],
                        canonical_json(before),
                        canonical_json(after),
                        canonical_json(evidence_ids),
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE duplicate_review_candidates SET status = 'confirmed', decided_at = ? WHERE candidate_id = ?",
                    (now, candidate_id),
                )
        except sqlite3.IntegrityError as exc:
            if "hard locked" in str(exc):
                raise PermissionError("duplicate row is inside a hard-locked period") from exc
            raise
        return {
            "candidateId": candidate_id,
            "eventId": event_id,
            "status": "confirmed",
            "keeperTransactionId": keeper_transaction_id,
            "duplicateTransactionId": duplicate_id,
            "evidenceIds": evidence_ids,
            "occurredAt": now,
        }

    def reject(
        self, *, workspace_id: str, candidate_id: str, reason: str
    ) -> dict[str, object]:
        reason_value = reason.strip()
        if not reason_value:
            raise ValueError("duplicate rejection reason is required")
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            candidate = connection.execute(
                "SELECT * FROM duplicate_review_candidates WHERE workspace_id = ? AND candidate_id = ?",
                (workspace_id, candidate_id),
            ).fetchone()
            if candidate is None:
                raise KeyError(candidate_id)
            existing = connection.execute(
                "SELECT event_id FROM duplicate_review_events WHERE candidate_id = ? AND event_type = 'rejected'",
                (candidate_id,),
            ).fetchone()
            if existing:
                return {
                    "candidateId": candidate_id,
                    "eventId": str(existing["event_id"]),
                    "status": "rejected",
                    "idempotentReplay": True,
                }
            if str(candidate["status"]) == "confirmed":
                raise ValueError("confirmed duplicate must be undone before rejection")
            event_id = _stable_id("duplicateevent", workspace_id, candidate_id, "rejected")
            connection.execute(
                """
                INSERT INTO duplicate_review_events(
                    event_id, workspace_id, candidate_id, event_type, actor,
                    reason, before_json, after_json, evidence_ids_json,
                    occurred_at, reverses_event_id
                ) VALUES (?, ?, ?, 'rejected', 'owner', ?, '{}', '{}', '[]', ?, NULL)
                """,
                (event_id, workspace_id, candidate_id, reason_value[:500], now),
            )
            connection.execute(
                "UPDATE duplicate_review_candidates SET status = 'rejected', decided_at = ? WHERE candidate_id = ?",
                (now, candidate_id),
            )
        return {"candidateId": candidate_id, "eventId": event_id, "status": "rejected"}

    def undo(
        self, *, workspace_id: str, event_id: str, reason: str
    ) -> dict[str, object]:
        reason_value = reason.strip()
        if not reason_value:
            raise ValueError("duplicate undo reason is required")
        now = datetime.now(UTC).isoformat()
        try:
            with self.store.transaction() as connection:
                event = connection.execute(
                    "SELECT * FROM duplicate_review_events WHERE workspace_id = ? AND event_id = ? AND event_type = 'confirmed'",
                    (workspace_id, event_id),
                ).fetchone()
                if event is None:
                    raise KeyError(event_id)
                existing = connection.execute(
                    "SELECT event_id FROM duplicate_review_events WHERE event_type = 'undone' AND reverses_event_id = ?",
                    (event_id,),
                ).fetchone()
                if existing:
                    return {
                        "eventId": str(existing["event_id"]),
                        "reversesEventId": event_id,
                        "status": "undone",
                        "idempotentReplay": True,
                    }
                before = json.loads(str(event["before_json"]))
                connection.execute(
                    """
                    UPDATE transactions SET status = ?, duplicate_of_transaction_id = ?,
                        updated_at = ? WHERE transaction_id = ?
                    """,
                    (
                        before["status"],
                        before["duplicateOfTransactionId"],
                        now,
                        event["duplicate_transaction_id"],
                    ),
                )
                undo_id = _stable_id("duplicateevent", workspace_id, event_id, "undone", now)
                connection.execute(
                    """
                    INSERT INTO duplicate_review_events(
                        event_id, workspace_id, candidate_id, event_type, actor,
                        keeper_transaction_id, duplicate_transaction_id, reason,
                        before_json, after_json, evidence_ids_json, occurred_at,
                        reverses_event_id
                    ) VALUES (?, ?, ?, 'undone', 'owner', ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        undo_id,
                        workspace_id,
                        event["candidate_id"],
                        event["keeper_transaction_id"],
                        event["duplicate_transaction_id"],
                        reason_value[:500],
                        event["after_json"],
                        event["before_json"],
                        event["evidence_ids_json"],
                        now,
                        event_id,
                    ),
                )
                connection.execute(
                    "UPDATE duplicate_review_candidates SET status = 'proposed', decided_at = ? WHERE candidate_id = ?",
                    (now, event["candidate_id"]),
                )
        except sqlite3.IntegrityError as exc:
            if "hard locked" in str(exc):
                raise PermissionError("duplicate row is inside a hard-locked period") from exc
            raise
        return {
            "eventId": undo_id,
            "reversesEventId": event_id,
            "candidateId": str(event["candidate_id"]),
            "status": "undone",
            "occurredAt": now,
        }
'''

SERVICE_METHODS = '''    async def scan_duplicate_candidates(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return DuplicateReviewService(self.store).scan(workspace_id)

    async def list_duplicate_candidates(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return DuplicateReviewService(self.store).list(workspace_id)

    async def confirm_duplicate_candidate(
        self,
        *,
        workspace_id: str,
        candidate_id: str,
        keeper_transaction_id: str,
        reason: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = DuplicateReviewService(self.store).confirm(
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                keeper_transaction_id=keeper_transaction_id,
                reason=reason,
            )
            result = self.daily_close.run()
            self._register_daily_close_events(result)
        return {**value, "dailyCloseRunId": result.run_id}

    async def reject_duplicate_candidate(
        self, *, workspace_id: str, candidate_id: str, reason: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return DuplicateReviewService(self.store).reject(
                workspace_id=workspace_id,
                candidate_id=candidate_id,
                reason=reason,
            )

    async def undo_duplicate_confirmation(
        self, *, workspace_id: str, event_id: str, reason: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = DuplicateReviewService(self.store).undo(
                workspace_id=workspace_id,
                event_id=event_id,
                reason=reason,
            )
            result = self.daily_close.run()
            self._register_daily_close_events(result)
        return {**value, "dailyCloseRunId": result.run_id}
'''

ROUTE_MODELS = '''

class DuplicateConfirmRequest(RequestModel):
    keeper_transaction_id: str = Field(
        alias="keeperTransactionId", pattern=IDENTIFIER_PATTERN
    )
    reason: str = Field(min_length=1, max_length=500)


class DuplicateDecisionRequest(RequestModel):
    reason: str = Field(min_length=1, max_length=500)
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/duplicates/scan")
    async def scan_duplicate_candidates(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        values = await services.scan_duplicate_candidates(workspace_id=workspace_id)
        return {"workspaceId": workspace_id, "candidates": list(values)}

    @router.get("/v1/workspaces/{workspace_id}/duplicates/candidates")
    async def list_duplicate_candidates(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        values = await services.list_duplicate_candidates(workspace_id=workspace_id)
        return {"workspaceId": workspace_id, "candidates": list(values)}

    @router.post(
        "/v1/workspaces/{workspace_id}/duplicates/candidates/{candidate_id}/confirm"
    )
    async def confirm_duplicate_candidate(
        workspace_id: PathIdentifier,
        candidate_id: PathIdentifier,
        body: DuplicateConfirmRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.confirm_duplicate_candidate(
                    workspace_id=workspace_id,
                    candidate_id=candidate_id,
                    keeper_transaction_id=body.keeper_transaction_id,
                    reason=body.reason,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="duplicate candidate not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post(
        "/v1/workspaces/{workspace_id}/duplicates/candidates/{candidate_id}/reject"
    )
    async def reject_duplicate_candidate(
        workspace_id: PathIdentifier,
        candidate_id: PathIdentifier,
        body: DuplicateDecisionRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.reject_duplicate_candidate(
                    workspace_id=workspace_id,
                    candidate_id=candidate_id,
                    reason=body.reason,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="duplicate candidate not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post(
        "/v1/workspaces/{workspace_id}/duplicates/events/{event_id}/undo"
    )
    async def undo_duplicate_confirmation(
        workspace_id: PathIdentifier,
        event_id: PathIdentifier,
        body: DuplicateDecisionRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.undo_duplicate_confirmation(
                    workspace_id=workspace_id,
                    event_id=event_id,
                    reason=body.reason,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="duplicate confirmation not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

import hashlib
from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.finance.duplicates import DuplicateReviewService
from finance_agent.storage import SQLiteStore, canonical_json

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def add_source(store: SQLiteStore, source_id: str, digest_seed: str) -> None:
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO source_items(
                source_item_id, workspace_id, source_type, label, digest,
                mapping_version, received_at, status, row_count
            ) VALUES (?, 'ws_koru_studio', 'csv', ?, ?, 'duplicate_test@1',
                '2026-08-26T00:00:00+00:00', 'processed', 1)
            """,
            (
                source_id,
                source_id,
                hashlib.sha256(digest_seed.encode()).hexdigest(),
            ),
        )


def add_transaction(
    store: SQLiteStore,
    *,
    source_id: str,
    transaction_id: str,
    occurred_on: str,
    description: str,
    amount_minor: int,
) -> None:
    suffix = transaction_id.removeprefix("txn_")
    row_id = f"row_{suffix}"
    evidence_id = f"evd_{suffix}"
    raw = canonical_json({"date": occurred_on, "description": description})
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO source_rows(
                source_row_id, source_item_id, row_number, account_id,
                occurred_on, description, amount_minor, currency, source_status,
                external_reference, mapping_version, row_hash, raw_json
            ) VALUES (?, ?, 1, 'acct_koru_business', ?, ?, ?, 'NZD', 'posted',
                ?, 'duplicate_test@1', ?, ?)
            """,
            (
                row_id,
                source_id,
                occurred_on,
                description,
                amount_minor,
                transaction_id,
                hashlib.sha256(raw.encode()).hexdigest(),
                raw,
            ),
        )
        connection.execute(
            """
            INSERT INTO evidence_links(
                evidence_id, workspace_id, source_item_id, source_row_id,
                label, created_at
            ) VALUES (?, 'ws_koru_studio', ?, ?, ?, '2026-08-26T00:00:00+00:00')
            """,
            (evidence_id, source_id, row_id, description),
        )
        connection.execute(
            """
            INSERT INTO transactions(
                transaction_id, workspace_id, account_id, source_row_id, evidence_id,
                occurred_on, description, amount_minor, currency, source_status, status,
                classification, category, classification_source, rule_id,
                duplicate_of_transaction_id, created_at, updated_at
            ) VALUES (?, 'ws_koru_studio', 'acct_koru_business', ?, ?, ?, ?, ?,
                'NZD', 'posted', 'posted', 'business', 'software_subscriptions',
                'deterministic', NULL, NULL, '2026-08-26T00:00:00+00:00',
                '2026-08-26T00:00:00+00:00')
            """,
            (transaction_id, row_id, evidence_id, occurred_on, description, amount_minor),
        )


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    add_source(store, "src_duplicate_a", "a")
    add_source(store, "src_duplicate_b", "b")
    add_transaction(
        store,
        source_id="src_duplicate_a",
        transaction_id="txn_duplicate_a",
        occurred_on="2026-08-20",
        description="ADOBE CREATIVE CLOUD AUCKLAND",
        amount_minor=-8999,
    )
    add_transaction(
        store,
        source_id="src_duplicate_b",
        transaction_id="txn_duplicate_b",
        occurred_on="2026-08-21",
        description="Adobe Creative Cloud Auckland NZ",
        amount_minor=-8999,
    )
    return store, DuplicateReviewService(store)


def test_scan_reports_cross_source_similarity_without_mutation(tmp_path: Path) -> None:
    store, service = setup(tmp_path)
    candidate = next(
        value for value in service.scan("ws_koru_studio")
        if {value["transactionA"]["transactionId"], value["transactionB"]["transactionId"]}
        == {"txn_duplicate_a", "txn_duplicate_b"}
    )
    assert candidate["committed"] is False
    assert candidate["scoreBasisPoints"] >= 8000
    assert candidate["factors"]["crossSource"] is True
    assert candidate["factors"]["exactAmount"] is True
    assert candidate["factors"]["descriptionSimilarityBasisPoints"] >= 5000
    statuses = store.fetch_all(
        "SELECT status FROM transactions WHERE transaction_id LIKE 'txn_duplicate_%'"
    )
    assert all(str(row["status"]) == "posted" for row in statuses)


def test_owner_selects_keeper_and_undo_restores_duplicate_state(tmp_path: Path) -> None:
    store, service = setup(tmp_path)
    candidate = next(
        value for value in service.scan("ws_koru_studio")
        if value["transactionA"]["transactionId"] == "txn_duplicate_a"
    )
    confirmed = service.confirm(
        workspace_id="ws_koru_studio",
        candidate_id=str(candidate["candidateId"]),
        keeper_transaction_id="txn_duplicate_a",
        reason="Owner confirmed source B repeats source A.",
    )
    duplicate = store.fetch_one(
        "SELECT status, duplicate_of_transaction_id FROM transactions WHERE transaction_id = 'txn_duplicate_b'"
    )
    assert (str(duplicate["status"]), str(duplicate["duplicate_of_transaction_id"])) == (
        "duplicate", "txn_duplicate_a"
    )
    assert confirmed["evidenceIds"] == ["evd_duplicate_a", "evd_duplicate_b"]
    replay = service.confirm(
        workspace_id="ws_koru_studio",
        candidate_id=str(candidate["candidateId"]),
        keeper_transaction_id="txn_duplicate_a",
        reason="Repeated confirmation.",
    )
    assert replay["idempotentReplay"] is True
    undone = service.undo(
        workspace_id="ws_koru_studio",
        event_id=str(confirmed["eventId"]),
        reason="Owner reversed the duplicate decision.",
    )
    assert undone["status"] == "undone"
    restored = store.fetch_one(
        "SELECT status, duplicate_of_transaction_id FROM transactions WHERE transaction_id = 'txn_duplicate_b'"
    )
    assert (str(restored["status"]), restored["duplicate_of_transaction_id"]) == (
        "posted", None
    )


def test_mismatched_amount_or_distant_date_does_not_propose(tmp_path: Path) -> None:
    store, service = setup(tmp_path)
    add_source(store, "src_duplicate_c", "c")
    add_transaction(
        store,
        source_id="src_duplicate_c",
        transaction_id="txn_duplicate_c",
        occurred_on="2026-08-30",
        description="Adobe Creative Cloud Auckland",
        amount_minor=-9000,
    )
    pairs = {
        frozenset((value["transactionA"]["transactionId"], value["transactionB"]["transactionId"]))
        for value in service.scan("ws_koru_studio")
    }
    assert frozenset(("txn_duplicate_a", "txn_duplicate_c")) not in pairs


def test_rejection_is_idempotent_and_does_not_change_transactions(tmp_path: Path) -> None:
    store, service = setup(tmp_path)
    candidate = service.scan("ws_koru_studio")[0]
    value = service.reject(
        workspace_id="ws_koru_studio",
        candidate_id=str(candidate["candidateId"]),
        reason="These are separate subscriptions.",
    )
    assert value["status"] == "rejected"
    replay = service.reject(
        workspace_id="ws_koru_studio",
        candidate_id=str(candidate["candidateId"]),
        reason="Repeated rejection.",
    )
    assert replay["idempotentReplay"] is True
    statuses = store.fetch_all(
        "SELECT status FROM transactions WHERE transaction_id LIKE 'txn_duplicate_%'"
    )
    assert all(str(row["status"]) == "posted" for row in statuses)
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
    write("services/api/src/finance_agent/finance/duplicates.py", MODULE)


def update_service_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.foreign_currency import ForeignCurrencyService\n"
    import_line = "from finance_agent.finance.duplicates import DuplicateReviewService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("foreign currency import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "list_foreign_currency_items", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def list_foreign_currency_items(\n"
    addition = '''    async def scan_duplicate_candidates(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def list_duplicate_candidates(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def confirm_duplicate_candidate(\n        self, *, workspace_id: str, candidate_id: str,\n        keeper_transaction_id: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n    async def reject_duplicate_candidate(\n        self, *, workspace_id: str, candidate_id: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n    async def undo_duplicate_confirmation(\n        self, *, workspace_id: str, event_id: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("foreign currency protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass FxRateRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("FxRateRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.get("/v1/workspaces/{workspace_id}/foreign-currency/items")\n'
    if route_marker not in content:
        raise RuntimeError("foreign-currency route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def update_audit_state_identity() -> None:
    path = "services/api/src/finance_agent/audit_trail.py"
    content = read(path)
    kind_marker = '        "foreign_currency",\n'
    if '"duplicate_review"' not in content:
        if kind_marker not in content:
            raise RuntimeError("foreign currency audit kind marker missing")
        content = content.replace(kind_marker, kind_marker + '        "duplicate_review",\n', 1)
    optional_marker = '        if self._table_exists("fx_conversion_events"):\n'
    block = '''        if self._table_exists("duplicate_review_events"):
            for row in self.store.fetch_all(
                "SELECT * FROM duplicate_review_events WHERE workspace_id = ? ORDER BY occurred_at, event_id",
                (workspace_id,),
            ):
                yield AuditEvent(
                    event_id=str(row["event_id"]),
                    workspace_id=workspace_id,
                    kind="duplicate_review",
                    action=f"duplicate_{row['event_type']}",
                    status=str(row["event_type"]),
                    occurred_at=str(row["occurred_at"]),
                    actor="owner",
                    correlation_id=None,
                    subject_type="duplicate_candidate",
                    subject_id=str(row["candidate_id"]),
                    evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
                    metadata={
                        "keeperTransactionId": str(row["keeper_transaction_id"]) if row["keeper_transaction_id"] else None,
                        "duplicateTransactionId": str(row["duplicate_transaction_id"]) if row["duplicate_transaction_id"] else None,
                        "reversesEventId": str(row["reverses_event_id"]) if row["reverses_event_id"] else None,
                        "reasonIncluded": False,
                    },
                )
'''
    if "duplicate_confirmed" not in content:
        if optional_marker not in content:
            raise RuntimeError("FX optional audit marker missing")
        content = content.replace(optional_marker, block + optional_marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/storage/state_identity.py"
    content = read(path)
    marker = '''    fx_events = _rows(
        store,
        """
        SELECT event_id, item_id, event_type, rate_id, rate_revision,
               target_account_id, original_amount_minor, original_currency,
               converted_amount_minor, exact_numerator, exact_denominator,
               rounding_mode, evidence_ids_json, occurred_at
        FROM fx_conversion_events
        WHERE workspace_id = ? ORDER BY occurred_at, event_id
        """,
        (workspace_id,),
    )
'''
    addition = marker + '''    duplicate_events = _rows(
        store,
        """
        SELECT event_id, candidate_id, event_type, keeper_transaction_id,
               duplicate_transaction_id, before_json, after_json,
               evidence_ids_json, occurred_at, reverses_event_id
        FROM duplicate_review_events
        WHERE workspace_id = ? ORDER BY occurred_at, event_id
        """,
        (workspace_id,),
    )
'''
    if "duplicate_events = _rows(" not in content:
        if marker not in content:
            raise RuntimeError("FX identity marker missing")
        content = content.replace(marker, addition, 1)
    payload_marker = '        "fxEvents": fx_events,\n'
    if '"duplicateEvents": duplicate_events' not in content:
        if payload_marker not in content:
            raise RuntimeError("FX identity payload missing")
        content = content.replace(
            payload_marker,
            payload_marker + '        "duplicateEvents": duplicate_events,\n',
            1,
        )
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/finance/test_duplicate_review_workflow.py", TESTS)
    write("docs/DUPLICATE_REVIEW.md", '''# Duplicate review workflow\n\nFolio compares posted or pending non-transfer rows with exact amount and currency, dates no more than three days apart and at least 50% token overlap in their descriptions. The visible score combines exact amount, date proximity, description similarity and whether records came from different source items. Scanning only creates a candidate.\n\nThe owner chooses which transaction remains authoritative. Confirmation stores both evidence links and the other row's exact prior status/duplicate link, then marks only that row `duplicate`. One row cannot receive two active duplicate decisions. Rejecting a candidate does not change either transaction. Undo appends a reversing event and restores the prior state. Hard-locked periods block confirmation and Undo.\n\nA high score is not proof of duplication. Provider IDs, source context and owner judgement remain necessary, and confirmation is not deletion of either immutable source row.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 43: deterministic duplicate review workflow\n\n- Candidates require exact amount/currency, bounded dates and visible text similarity.\n- Cross-source origin and each scoring factor are exposed.\n- Scanning never alters transaction state.\n- The owner selects the keeper and both evidence links remain attached.\n- Reject is non-mutating; Undo restores exact prior duplicate state.\n- A score is not proof, source rows remain immutable and hard locks remain authoritative.\n'''
    if "## Stack 43: deterministic duplicate review workflow" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_service_protocol_routes()
    update_audit_state_identity()
    tests_docs()
    print("duplicate review workflow changes applied")


if __name__ == "__main__":
    main()
