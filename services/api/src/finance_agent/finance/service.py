# ruff: noqa: E501
"""Transactional deterministic finance engine and public integration boundary."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from finance_agent.artifacts import PREPARATORY_LANGUAGE, OwnerPackDTO, render_owner_pack
from finance_agent.storage import SQLiteStore, canonical_json

from .classification import (
    calculate_classified_totals,
    classification_for,
    pending_duplicate_pairs,
    rule_matches,
)
from .domain import CashForecast, ClassificationRule, FinanceTotals, Transaction
from .forecast import koru_30_day_forecast
from .ingest import CSVImporter, ImportResult, stable_id
from .surfaces import (
    cash_scenario_surface,
    living_brief_surface,
    owner_pack_surface,
    work_receipt_surface,
)

WORKSPACE_ID = "ws_koru_studio"
THREAD_ID = "thr_koru_studio_main"
ACCOUNT_ID = "acct_koru_business"
DATA_THROUGH = "2026-07-17T08:00:00+12:00"
DEMO_CREATED_AT = "2026-07-17T07:58:00+12:00"
CSV_RECEIVED_AT = "2026-07-17T07:59:00+12:00"
ARTIFACT_GENERATED_AT = "2026-07-17T08:00:03+12:00"
FORECAST_ID = "forecast_koru_30d"
POLICY_VERSION = "daily_close_policy@1"


class FinanceStateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DerivedState:
    state_revision: int
    totals: FinanceTotals
    forecast: CashForecast
    findings: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class EventResult:
    event: dict[str, Any]
    snapshot: dict[str, Any]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _json(value: str) -> Any:
    return json.loads(value)


class FinanceEngine:
    """Finance-owned API used by the job and agent/API lanes."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.importer = CSVImporter(store)

    def initialise(self) -> None:
        self.store.migrate()

    def reset_demo(
        self,
        csv_path: str | Path,
        *,
        telegram_update_path: str | Path | None = None,
    ) -> ImportResult:
        self.store.recreate()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO workspaces(
                    workspace_id, name, entity_type, currency, timezone,
                    protected_reserve_minor, data_through, thread_id, model_mode,
                    created_at, updated_at
                ) VALUES (?, 'Koru Studio', 'nz_sole_trader', 'NZD',
                    'Pacific/Auckland', 200000, ?, ?, 'local', ?, ?)
                """,
                (WORKSPACE_ID, DATA_THROUGH, THREAD_ID, DEMO_CREATED_AT, DEMO_CREATED_AT),
            )
            connection.execute(
                """
                INSERT INTO accounts(account_id, workspace_id, name, currency, created_at)
                VALUES (?, ?, 'Koru Studio business account', 'NZD', ?)
                """,
                (ACCOUNT_ID, WORKSPACE_ID, DEMO_CREATED_AT),
            )
            connection.execute(
                """
                INSERT INTO job_definitions(
                    definition_id, workspace_id, job_type, enabled, policy_version, created_at
                ) VALUES ('jobdef_koru_daily_close', ?, 'daily_close', 1, ?, ?)
                """,
                (WORKSPACE_ID, POLICY_VERSION, DEMO_CREATED_AT),
            )

        result = self.importer.ingest(
            csv_path,
            workspace_id=WORKSPACE_ID,
            source_item_id="src_koru_bank_csv_20260717",
            label="Koru Studio bank export — July 2026",
            mapping_version="koru_bank_csv@1",
            received_at=CSV_RECEIVED_AT,
        )
        if telegram_update_path is not None:
            self.record_telegram_fixture_source(telegram_update_path)
        return result

    def record_telegram_fixture_source(self, update_path: str | Path) -> None:
        raw_bytes = Path(update_path).read_bytes()
        digest = hashlib.sha256(raw_bytes).hexdigest()
        received_at = "2026-07-17T09:00:00+12:00"
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM source_items WHERE source_item_id = 'src_koru_telegram_910001'"
            ).fetchone()
            if existing is not None:
                return
            connection.execute(
                """
                INSERT INTO source_items(
                    source_item_id, workspace_id, source_type, label, digest,
                    mapping_version, received_at, status, row_count
                ) VALUES ('src_koru_telegram_910001', ?, 'telegram_fixture',
                    'Telegram parking receipt fixture', ?, 'telegram_update@1', ?, 'pending', 1)
                """,
                (WORKSPACE_ID, digest, received_at),
            )
            connection.execute(
                """
                INSERT INTO evidence_links(
                    evidence_id, workspace_id, source_item_id, source_row_id, label, created_at
                ) VALUES ('evd_koru_telegram_parking', ?, 'src_koru_telegram_910001', NULL,
                    'Telegram parking receipt fixture', ?)
                """,
                (WORKSPACE_ID, received_at),
            )

    def ingest_csv(
        self,
        path: str | Path,
        *,
        source_item_id: str | None = None,
        label: str | None = None,
        mapping_version: str = "bank_csv@1",
        received_at: str | None = None,
    ) -> ImportResult:
        return self.importer.ingest(
            path,
            workspace_id=WORKSPACE_ID,
            source_item_id=source_item_id,
            label=label,
            mapping_version=mapping_version,
            received_at=received_at,
        )

    @staticmethod
    def _transaction_from_row(row: sqlite3.Row) -> Transaction:
        return Transaction(
            transaction_id=row["transaction_id"],
            occurred_on=row["occurred_on"],
            description=row["description"],
            amount_minor=row["amount_minor"],
            currency=row["currency"],
            source_status=row["source_status"],
            status=row["status"],
            classification=row["classification"],
            category=row["category"],
            classification_source=row["classification_source"],
            rule_id=row["rule_id"],
            evidence_id=row["evidence_id"],
            duplicate_of_transaction_id=row["duplicate_of_transaction_id"],
        )

    def load_transactions(self, connection: sqlite3.Connection) -> list[Transaction]:
        rows = connection.execute(
            """
            SELECT * FROM transactions
            WHERE workspace_id = ?
            ORDER BY occurred_on, transaction_id
            """,
            (WORKSPACE_ID,),
        ).fetchall()
        return [self._transaction_from_row(row) for row in rows]

    @staticmethod
    def _rule_from_row(row: sqlite3.Row) -> ClassificationRule:
        return ClassificationRule(
            rule_id=row["rule_id"],
            merchant_contains=row["merchant_contains"],
            maximum_amount_minor=row["maximum_amount_minor"],
            currency=row["currency"],
            target_classification=row["target_classification"],
            target_category=row["target_category"],
            effective_from=row["effective_from"],
            priority=row["priority"],
        )

    def load_active_rules(self, connection: sqlite3.Connection) -> list[ClassificationRule]:
        rows = connection.execute(
            """
            SELECT * FROM classification_rules
            WHERE workspace_id = ? AND active = 1
            ORDER BY priority DESC, rule_id
            """,
            (WORKSPACE_ID,),
        ).fetchall()
        return [self._rule_from_row(row) for row in rows]

    def process_pending_sources(self, connection: sqlite3.Connection) -> int:
        cursor = connection.execute(
            """
            UPDATE source_items
            SET status = 'processed'
            WHERE workspace_id = ? AND source_type = 'csv' AND status = 'pending'
            """,
            (WORKSPACE_ID,),
        )
        return cursor.rowcount

    def validate_normalised_rows(self, connection: sqlite3.Connection) -> int:
        invalid = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM transactions
            WHERE workspace_id = ?
              AND (currency != 'NZD' OR typeof(amount_minor) != 'integer')
            """,
            (WORKSPACE_ID,),
        ).fetchone()["count"]
        if invalid:
            raise FinanceStateError("normalised transactions violated exact-money invariants")
        return connection.execute(
            "SELECT COUNT(*) AS count FROM transactions WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()["count"]

    def deduplicate(self, connection: sqlite3.Connection, *, occurred_at: str) -> dict[str, str]:
        connection.execute(
            """
            UPDATE transactions
            SET status = 'pending', duplicate_of_transaction_id = NULL, updated_at = ?
            WHERE workspace_id = ? AND source_status = 'pending'
            """,
            (occurred_at, WORKSPACE_ID),
        )
        pairs = pending_duplicate_pairs(self.load_transactions(connection))
        for pending_id, posted_id in pairs.items():
            connection.execute(
                """
                UPDATE transactions
                SET status = 'duplicate', duplicate_of_transaction_id = ?, updated_at = ?
                WHERE transaction_id = ?
                """,
                (posted_id, occurred_at, pending_id),
            )
        return pairs

    def apply_classifications(self, connection: sqlite3.Connection, *, occurred_at: str) -> int:
        rules = self.load_active_rules(connection)
        changed = 0
        for transaction in self.load_transactions(connection):
            decision = classification_for(transaction, rules)
            if (
                transaction.classification,
                transaction.category,
                transaction.classification_source,
                transaction.rule_id,
            ) == (decision.classification, decision.category, decision.source, decision.rule_id):
                continue
            connection.execute(
                """
                UPDATE transactions
                SET classification = ?, category = ?, classification_source = ?,
                    rule_id = ?, updated_at = ?
                WHERE transaction_id = ?
                """,
                (
                    decision.classification,
                    decision.category,
                    decision.source,
                    decision.rule_id,
                    occurred_at,
                    transaction.transaction_id,
                ),
            )
            changed += 1
        return changed

    def preview_state(self, connection: sqlite3.Connection) -> tuple[FinanceTotals, CashForecast]:
        transactions = self.load_transactions(connection)
        current_balance = sum(
            transaction.amount_minor
            for transaction in transactions
            if transaction.status == "posted"
        )
        reserve = connection.execute(
            "SELECT protected_reserve_minor FROM workspaces WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()["protected_reserve_minor"]
        forecast = koru_30_day_forecast(current_balance, reserve)
        totals = calculate_classified_totals(
            transactions,
            protected_reserve_minor=reserve,
            projected_low_point_minor=forecast.low_point_minor,
        )
        return totals, forecast

    def begin_state_revision(self, connection: sqlite3.Connection, *, occurred_at: str) -> int:
        connection.execute(
            """
            UPDATE workspaces
            SET state_revision = state_revision + 1, updated_at = ?
            WHERE workspace_id = ?
            """,
            (occurred_at, WORKSPACE_ID),
        )
        return connection.execute(
            "SELECT state_revision FROM workspaces WHERE workspace_id = ?",
            (WORKSPACE_ID,),
        ).fetchone()["state_revision"]

    def _ensure_forecast_evidence(
        self, connection: sqlite3.Connection, *, occurred_at: str
    ) -> None:
        existing = connection.execute(
            "SELECT 1 FROM evidence_links WHERE evidence_id = 'evd_koru_forecast_30d'"
        ).fetchone()
        if existing is not None:
            return
        connection.execute(
            """
            INSERT INTO evidence_links(
                evidence_id, workspace_id, source_item_id, source_row_id, label, created_at
            ) VALUES ('evd_koru_forecast_30d', ?, 'src_koru_bank_csv_20260717', NULL,
                'Deterministic 30-day cash roll-forward and assumptions', ?)
            """,
            (WORKSPACE_ID, occurred_at),
        )

    def store_findings(
        self,
        connection: sqlite3.Connection,
        *,
        state_revision: int,
        totals: FinanceTotals,
        occurred_at: str,
    ) -> tuple[dict[str, Any], ...]:
        duplicate = connection.execute(
            """
            SELECT transaction_id, amount_minor, evidence_id, duplicate_of_transaction_id
            FROM transactions
            WHERE workspace_id = ? AND status = 'duplicate'
            ORDER BY transaction_id LIMIT 1
            """,
            (WORKSPACE_ID,),
        ).fetchone()
        posted_evidence = "evd_koru_figma_posted"
        if duplicate is not None and duplicate["duplicate_of_transaction_id"]:
            posted = connection.execute(
                "SELECT evidence_id FROM transactions WHERE transaction_id = ?",
                (duplicate["duplicate_of_transaction_id"],),
            ).fetchone()
            if posted is not None:
                posted_evidence = posted["evidence_id"]

        finding_values: tuple[dict[str, Any], ...] = (
            {
                "findingId": "finding_koru_missing_receipt",
                "kind": "missing_document",
                "severity": "attention",
                "title": "Mitre 10 needs context"
                if totals.unresolved_expense_minor
                else "Mitre 10 context resolved",
                "summary": (
                    "NZD 184.75 is unresolved and has no receipt attached."
                    if totals.unresolved_expense_minor
                    else "The owner-provided client fit-out rule resolved this item."
                ),
                "amountMinor": totals.unresolved_expense_minor,
                "currency": "NZD",
                "status": "open" if totals.unresolved_expense_minor else "resolved",
                "evidenceIds": ["evd_koru_mitre10_row"],
            },
            {
                "findingId": "finding_koru_duplicate_pending",
                "kind": "duplicate",
                "severity": "info",
                "title": "Pending Figma row held out",
                "summary": "The NZD 30.00 pending row matches the posted charge.",
                "amountMinor": 0 if duplicate is None else abs(duplicate["amount_minor"]),
                "currency": "NZD",
                "status": "resolved" if duplicate is None else "open",
                "evidenceIds": [
                    posted_evidence,
                    "evd_koru_figma_pending" if duplicate is None else duplicate["evidence_id"],
                ],
            },
            {
                "findingId": "finding_koru_reserve_risk",
                "kind": "reserve_risk",
                "severity": "critical",
                "title": "Protected reserve is at risk",
                "summary": "The 30-day low is NZD 99.23 below reserve.",
                "amountMinor": totals.reserve_shortfall_minor,
                "currency": "NZD",
                "status": "open" if totals.reserve_shortfall_minor else "resolved",
                "evidenceIds": ["evd_koru_forecast_30d"],
            },
        )
        connection.execute(
            """
            UPDATE findings
            SET is_current = 0, obsoleted_at = ?
            WHERE workspace_id = ? AND is_current = 1
            """,
            (occurred_at, WORKSPACE_ID),
        )
        for finding in finding_values:
            revision = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM findings WHERE finding_id = ?",
                (finding["findingId"],),
            ).fetchone()["revision"]
            connection.execute(
                """
                INSERT INTO findings(
                    finding_id, revision, workspace_id, kind, severity, title, summary,
                    amount_minor, currency, status, evidence_ids_json, state_revision,
                    is_current, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    finding["findingId"],
                    revision,
                    WORKSPACE_ID,
                    finding["kind"],
                    finding["severity"],
                    finding["title"],
                    finding["summary"],
                    finding["amountMinor"],
                    finding["currency"],
                    finding["status"],
                    canonical_json(finding["evidenceIds"]),
                    state_revision,
                    occurred_at,
                ),
            )
        return finding_values

    def store_forecast(
        self,
        connection: sqlite3.Connection,
        *,
        state_revision: int,
        totals: FinanceTotals,
        forecast: CashForecast,
        occurred_at: str,
    ) -> int:
        self._ensure_forecast_evidence(connection, occurred_at=occurred_at)
        connection.execute(
            """
            UPDATE forecast_revisions
            SET is_current = 0, obsoleted_at = ?
            WHERE workspace_id = ? AND is_current = 1
            """,
            (occurred_at, WORKSPACE_ID),
        )
        revision = connection.execute(
            """
            SELECT COALESCE(MAX(revision), 0) + 1 AS revision
            FROM forecast_revisions WHERE forecast_id = ?
            """,
            (FORECAST_ID,),
        ).fetchone()["revision"]
        connection.execute(
            """
            INSERT INTO forecast_revisions(
                forecast_id, revision, workspace_id, current_balance_minor,
                protected_reserve_minor, projected_low_point_minor,
                reserve_shortfall_minor, alternative_low_point_minor,
                assumptions_json, evidence_ids_json, state_revision, is_current, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                FORECAST_ID,
                revision,
                WORKSPACE_ID,
                totals.current_balance_minor,
                totals.protected_reserve_minor,
                forecast.low_point_minor,
                forecast.reserve_shortfall_minor,
                forecast.alternative_low_point_minor,
                canonical_json(list(forecast.assumptions)),
                canonical_json(["evd_koru_bank_csv", "evd_koru_forecast_30d"]),
                state_revision,
                occurred_at,
            ),
        )
        for index, point in enumerate(forecast.points):
            connection.execute(
                """
                INSERT INTO forecast_points(
                    forecast_id, revision, point_index, date, label, amount_minor,
                    balance_minor, reserve_minor, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    FORECAST_ID,
                    revision,
                    index,
                    point.date,
                    point.label,
                    point.amount_minor,
                    point.balance_minor,
                    point.reserve_minor,
                    point.status,
                ),
            )
        return revision

    def _source_manifest(self, connection: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "sourceItemId": row["source_item_id"],
                "sourceType": row["source_type"],
                "label": row["label"],
                "digest": row["digest"],
                "mappingVersion": row["mapping_version"],
                "receivedAt": row["received_at"],
                "status": row["status"],
                "rowCount": row["row_count"],
            }
            for row in connection.execute(
                """
                SELECT * FROM source_items
                WHERE workspace_id = ? ORDER BY received_at, source_item_id
                """,
                (WORKSPACE_ID,),
            )
        )

    def _evidence_index(self, connection: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "evidenceId": row["evidence_id"],
                "sourceItemId": row["source_item_id"],
                "sourceRowId": row["source_row_id"],
                "label": row["label"],
            }
            for row in connection.execute(
                """
                SELECT * FROM evidence_links
                WHERE workspace_id = ? ORDER BY evidence_id
                """,
                (WORKSPACE_ID,),
            )
        )

    def build_owner_pack_dto(
        self,
        connection: sqlite3.Connection,
        *,
        totals: FinanceTotals,
        forecast: CashForecast,
        generated_at: str,
    ) -> OwnerPackDTO:
        active_rule = connection.execute(
            """
            SELECT merchant_contains, maximum_amount_minor
            FROM classification_rules
            WHERE workspace_id = ? AND active = 1
            ORDER BY priority DESC, rule_id LIMIT 1
            """,
            (WORKSPACE_ID,),
        ).fetchone()
        if active_rule is None:
            explanation = (
                "Mitre 10 remains unresolved because the source has no attached receipt "
                "or owner-confirmed business purpose."
            )
        else:
            explanation = (
                "The owner identified Mitre 10 as client fit-out materials. The explicit "
                f"rule applies only to {active_rule['merchant_contains']} expenses of "
                f"NZD {active_rule['maximum_amount_minor'] // 100:,}."
                f"{active_rule['maximum_amount_minor'] % 100:02d} or less."
            )
        unresolved = tuple(
            {
                "transactionId": row["transaction_id"],
                "title": row["description"],
                "amountMinor": abs(row["amount_minor"]),
                "evidenceIds": [row["evidence_id"]],
            }
            for row in connection.execute(
                """
                SELECT transaction_id, description, amount_minor, evidence_id
                FROM transactions
                WHERE workspace_id = ? AND status = 'posted'
                  AND classification = 'unresolved' AND amount_minor < 0
                ORDER BY occurred_on, transaction_id
                """,
                (WORKSPACE_ID,),
            )
        )
        forecast_payload = {
            "lowPointMinor": forecast.low_point_minor,
            "reserveShortfallMinor": forecast.reserve_shortfall_minor,
            "alternativeLowPointMinor": forecast.alternative_low_point_minor,
            "assumptions": list(forecast.assumptions),
            "points": [
                {
                    "date": point.date,
                    "label": point.label,
                    "amountMinor": point.amount_minor,
                    "balanceMinor": point.balance_minor,
                    "reserveMinor": point.reserve_minor,
                    "status": point.status,
                }
                for point in forecast.points
            ],
        }
        return OwnerPackDTO(
            pack_version="owner.pack@1",
            workspace_id=WORKSPACE_ID,
            workspace_name="Koru Studio",
            generated_at=generated_at,
            data_through=DATA_THROUGH,
            currency="NZD",
            preparatory_language=PREPARATORY_LANGUAGE,
            owner_explanation=explanation,
            totals=totals.as_contract(as_of=DATA_THROUGH),
            unresolved_items=unresolved,
            forecast=forecast_payload,
            source_manifest=self._source_manifest(connection),
            evidence_index=self._evidence_index(connection),
        )

    def store_owner_pack(
        self,
        connection: sqlite3.Connection,
        *,
        state_revision: int,
        totals: FinanceTotals,
        forecast: CashForecast,
        generated_at: str,
    ) -> tuple[dict[str, Any], ...]:
        dto = self.build_owner_pack_dto(
            connection, totals=totals, forecast=forecast, generated_at=generated_at
        )
        html_bytes, pdf_bytes = render_owner_pack(dto)
        dto_json = dto.canonical_json()
        dto_hash = dto.content_hash()
        evidence_ids = [entry["evidenceId"] for entry in dto.evidence_index]
        connection.execute(
            """
            UPDATE artifacts SET is_current = 0, obsoleted_at = ?
            WHERE workspace_id = ? AND is_current = 1
            """,
            (generated_at, WORKSPACE_ID),
        )
        specs = (
            (
                "artifact_koru_owner_pack_html",
                "owner_pack_html",
                "text/html; charset=utf-8",
                html_bytes,
            ),
            ("artifact_koru_owner_pack_pdf", "owner_pack_pdf", "application/pdf", pdf_bytes),
        )
        artifacts: list[dict[str, Any]] = []
        for artifact_id, kind, media_type, content in specs:
            revision = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision FROM artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()["revision"]
            content_hash = hashlib.sha256(content).hexdigest()
            connection.execute(
                """
                INSERT INTO artifacts(
                    artifact_id, revision, workspace_id, kind, title, media_type,
                    content, content_hash, dto_json, dto_hash, evidence_ids_json,
                    state_revision, generated_at, is_current
                ) VALUES (?, ?, ?, ?, 'Koru Studio owner pack', ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    artifact_id,
                    revision,
                    WORKSPACE_ID,
                    kind,
                    media_type,
                    content,
                    content_hash,
                    dto_json,
                    dto_hash,
                    canonical_json(evidence_ids),
                    state_revision,
                    generated_at,
                ),
            )
            artifacts.append(
                {
                    "artifactId": artifact_id,
                    "kind": kind,
                    "title": "Koru Studio owner pack",
                    "contentHash": content_hash,
                    "generatedAt": generated_at,
                    "evidenceIds": evidence_ids,
                }
            )
        return tuple(artifacts)

    def store_outbox(
        self,
        connection: sqlite3.Connection,
        *,
        state_revision: int,
        totals: FinanceTotals,
        correlation_id: str,
        occurred_at: str,
    ) -> int:
        connection.execute(
            """
            UPDATE outbox_messages
            SET status = 'obsolete', obsoleted_at = ?
            WHERE workspace_id = ? AND status IN ('queued', 'attempted')
            """,
            (occurred_at, WORKSPACE_ID),
        )
        if totals.reserve_shortfall_minor <= 0:
            return 0
        outbox_id = f"out_koru_reserve_risk_{state_revision:03d}"
        payload = {
            "kind": "reserve_risk_brief",
            "message": "The planned laptop takes cash below the protected reserve.",
            "reserveShortfallMinor": totals.reserve_shortfall_minor,
            "currency": "NZD",
            "localReference": "finding_koru_reserve_risk",
        }
        connection.execute(
            """
            INSERT INTO outbox_messages(
                outbox_id, workspace_id, kind, payload_json, status, idempotency_key,
                correlation_id, evidence_ids_json, state_revision, created_at
            ) VALUES (?, ?, 'reserve_risk_brief', ?, 'queued', ?, ?, ?, ?, ?)
            """,
            (
                outbox_id,
                WORKSPACE_ID,
                canonical_json(payload),
                f"reserve-risk:{WORKSPACE_ID}:{state_revision}",
                correlation_id,
                canonical_json(["evd_koru_forecast_30d"]),
                state_revision,
                occurred_at,
            ),
        )
        return 1

    def recompute_derived(
        self,
        connection: sqlite3.Connection,
        *,
        occurred_at: str,
        correlation_id: str,
    ) -> DerivedState:
        totals, forecast = self.preview_state(connection)
        revision = self.begin_state_revision(connection, occurred_at=occurred_at)
        self.store_forecast(
            connection,
            state_revision=revision,
            totals=totals,
            forecast=forecast,
            occurred_at=occurred_at,
        )
        findings = self.store_findings(
            connection,
            state_revision=revision,
            totals=totals,
            occurred_at=occurred_at,
        )
        artifacts = self.store_owner_pack(
            connection,
            state_revision=revision,
            totals=totals,
            forecast=forecast,
            generated_at=occurred_at,
        )
        self.store_outbox(
            connection,
            state_revision=revision,
            totals=totals,
            correlation_id=correlation_id,
            occurred_at=occurred_at,
        )
        return DerivedState(revision, totals, forecast, findings, artifacts)

    def set_surface(self, connection: sqlite3.Connection, surface: Mapping[str, Any]) -> None:
        connection.execute(
            "UPDATE workspaces SET current_surface_json = ? WHERE workspace_id = ?",
            (canonical_json(surface), WORKSPACE_ID),
        )

    def _current_artifacts(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        return [
            {
                "artifactId": row["artifact_id"],
                "kind": row["kind"],
                "title": row["title"],
                "contentHash": row["content_hash"],
                "generatedAt": row["generated_at"],
                "evidenceIds": _json(row["evidence_ids_json"]),
            }
            for row in connection.execute(
                """
                SELECT * FROM artifacts WHERE workspace_id = ? AND is_current = 1
                ORDER BY kind
                """,
                (WORKSPACE_ID,),
            )
        ]

    def build_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot_id: str,
        totals: FinanceTotals | None = None,
    ) -> dict[str, Any]:
        workspace = connection.execute(
            "SELECT * FROM workspaces WHERE workspace_id = ?", (WORKSPACE_ID,)
        ).fetchone()
        if workspace is None or workspace["current_surface_json"] is None:
            raise FinanceStateError("workspace has no current surface")
        if totals is None:
            totals, _ = self.preview_state(connection)
        turns = [
            {
                "turnId": row["turn_id"],
                "role": row["role"],
                "content": row["content"],
                "occurredAt": row["occurred_at"],
                "status": row["status"],
                "evidenceIds": _json(row["evidence_ids_json"]),
            }
            for row in connection.execute(
                """
                SELECT * FROM conversation_turns
                WHERE workspace_id = ? AND thread_id = ? ORDER BY occurred_at, turn_id
                """,
                (WORKSPACE_ID, THREAD_ID),
            )
        ]
        findings = [
            {
                "findingId": row["finding_id"],
                "kind": row["kind"],
                "severity": row["severity"],
                "title": row["title"],
                "summary": row["summary"],
                "amountMinor": row["amount_minor"],
                "currency": row["currency"],
                "status": row["status"],
                "evidenceIds": _json(row["evidence_ids_json"]),
            }
            for row in connection.execute(
                """
                SELECT * FROM findings WHERE workspace_id = ? AND is_current = 1
                ORDER BY finding_id
                """,
                (WORKSPACE_ID,),
            )
        ]
        activity: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT * FROM job_runs WHERE workspace_id = ? ORDER BY started_at, run_id",
            (WORKSPACE_ID,),
        ):
            activity.append(
                {
                    "activityId": (
                        "activity_koru_daily_close"
                        if row["run_id"] == "run_koru_daily_close_20260717"
                        else f"activity_{row['run_id'].removeprefix('run_')}"
                    ),
                    "kind": "job_run",
                    "summary": "Daily Close completed from 10 source rows",
                    "status": "completed"
                    if row["status"] in {"completed", "no_op"}
                    else row["status"],
                    "occurredAt": row["completed_at"] or row["started_at"] or DATA_THROUGH,
                    "undoable": False,
                    "correlationId": row["correlation_id"],
                    "evidenceIds": ["evd_koru_bank_csv"],
                }
            )
        for row in connection.execute(
            "SELECT * FROM finance_events WHERE workspace_id = ? ORDER BY occurred_at, event_id",
            (WORKSPACE_ID,),
        ):
            activity.append(
                {
                    "activityId": f"activity_{row['event_id'].removeprefix('evt_')}",
                    "kind": "undo" if row["event_type"] == "event.undone" else "finance_event",
                    "summary": row["reason"][:240],
                    "status": "undone" if row["undone_by_event_id"] else "completed",
                    "occurredAt": row["occurred_at"],
                    "undoable": row["redone_by_event_id"] is None,
                    "correlationId": row["correlation_id"],
                    "evidenceIds": _json(row["evidence_ids_json"]),
                }
            )
        sources = [
            {
                "sourceItemId": source["sourceItemId"],
                "sourceType": source["sourceType"],
                "label": source["label"],
                "digest": source["digest"],
                "receivedAt": source["receivedAt"],
                "status": source["status"],
                "rowCount": source["rowCount"],
            }
            for source in self._source_manifest(connection)
        ]
        snapshot = {
            "snapshotVersion": "api.snapshot@1",
            "snapshotId": snapshot_id,
            "workspace": {
                "workspaceId": WORKSPACE_ID,
                "name": workspace["name"],
                "entityType": workspace["entity_type"],
                "currency": workspace["currency"],
                "timezone": workspace["timezone"],
                "protectedReserveMinor": workspace["protected_reserve_minor"],
            },
            "thread": {"threadId": THREAD_ID, "turns": turns, "activeQuestion": None},
            "currentSurface": _json(workspace["current_surface_json"]),
            "findings": findings,
            "activity": activity,
            "sources": sources,
            "totals": totals.as_contract(as_of=DATA_THROUGH),
            "artifacts": self._current_artifacts(connection),
            "modelMode": workspace["model_mode"],
            "freshness": {
                "dataThrough": DATA_THROUGH,
                "status": "current",
                "timezone": "Pacific/Auckland",
            },
        }
        return snapshot

    def store_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot_id: str,
        totals: FinanceTotals | None = None,
        occurred_at: str,
    ) -> dict[str, Any]:
        snapshot = self.build_snapshot(connection, snapshot_id=snapshot_id, totals=totals)
        revision = connection.execute(
            "SELECT state_revision FROM workspaces WHERE workspace_id = ?", (WORKSPACE_ID,)
        ).fetchone()["state_revision"]
        connection.execute(
            "UPDATE workspace_snapshots SET is_current = 0 WHERE workspace_id = ? AND is_current = 1",
            (WORKSPACE_ID,),
        )
        connection.execute(
            """
            INSERT INTO workspace_snapshots(
                snapshot_id, workspace_id, state_revision, snapshot_json,
                content_hash, created_at, is_current
            ) VALUES (?, ?, ?, ?, ?, ?, 1)
            """,
            (
                snapshot_id,
                WORKSPACE_ID,
                revision,
                canonical_json(snapshot),
                sha256_json(snapshot),
                occurred_at,
            ),
        )
        connection.execute(
            "UPDATE workspaces SET current_snapshot_id = ? WHERE workspace_id = ?",
            (snapshot_id, WORKSPACE_ID),
        )
        return snapshot

    def complete_daily_close_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        derived: DerivedState,
        occurred_at: str,
    ) -> dict[str, Any]:
        surface = living_brief_surface(
            totals=derived.totals,
            findings=derived.findings,
            data_through=DATA_THROUGH,
        )
        self.set_surface(connection, surface)
        existing_turn = connection.execute(
            "SELECT 1 FROM conversation_turns WHERE turn_id = 'turn_koru_morning_close'"
        ).fetchone()
        if existing_turn is None:
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    turn_id, workspace_id, thread_id, role, content, occurred_at,
                    status, evidence_ids_json
                ) VALUES ('turn_koru_morning_close', ?, ?, 'agent', ?, ?, 'complete', ?)
                """,
                (
                    WORKSPACE_ID,
                    THREAD_ID,
                    "Morning close is complete. I held out one likely duplicate, found one expense "
                    "that needs context, and the planned laptop takes cash NZD 99.23 below your "
                    "protected reserve.",
                    occurred_at,
                    canonical_json(
                        [
                            "evd_koru_bank_csv",
                            "evd_koru_mitre10_row",
                            "evd_koru_figma_pending",
                            "evd_koru_forecast_30d",
                        ]
                    ),
                ),
            )
        return self.store_snapshot(
            connection,
            snapshot_id="snap_koru_after_close",
            totals=derived.totals,
            occurred_at=occurred_at,
        )

    def get_snapshot(self) -> dict[str, Any]:
        row = self.store.fetch_one(
            """
            SELECT snapshot_json FROM workspace_snapshots
            WHERE workspace_id = ? AND is_current = 1
            """,
            (WORKSPACE_ID,),
        )
        if row is None:
            raise FinanceStateError("no workspace snapshot has been committed")
        return _json(row["snapshot_json"])

    def get_cash_scenario_surface(self) -> dict[str, Any]:
        with self.store.connect() as connection:
            _, forecast = self.preview_state(connection)
            return cash_scenario_surface(forecast=forecast, data_through=DATA_THROUGH)

    def get_owner_pack_surface(self) -> dict[str, Any]:
        with self.store.connect() as connection:
            return owner_pack_surface(
                artifacts=self._current_artifacts(connection), data_through=DATA_THROUGH
            )

    def get_artifact(self, artifact_id: str) -> tuple[str, bytes, str]:
        row = self.store.fetch_one(
            """
            SELECT media_type, content, content_hash FROM artifacts
            WHERE artifact_id = ? AND is_current = 1
            """,
            (artifact_id,),
        )
        if row is None:
            raise KeyError(artifact_id)
        return row["media_type"], bytes(row["content"]), row["content_hash"]

    @staticmethod
    def _projection_item(
        target_type: str, target_id: str, values: Mapping[str, Any]
    ) -> dict[str, Any]:
        return {
            "targetType": target_type,
            "targetId": target_id,
            "fields": [{"name": name, "value": value} for name, value in values.items()],
        }

    def _append_event(self, connection: sqlite3.Connection, event: Mapping[str, Any]) -> None:
        connection.execute(
            """
            INSERT INTO finance_events(
                event_id, workspace_id, event_type, actor, occurred_at, source_turn_id,
                reason, before_json, after_json, scope_json, evidence_ids_json,
                inverse_event_json, correlation_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event["eventId"],
                event["workspaceId"],
                event["eventType"],
                event["actor"],
                event["occurredAt"],
                event["sourceTurnId"],
                event["reason"],
                canonical_json(event["beforeJson"]),
                canonical_json(event["afterJson"]),
                canonical_json(event["scopeJson"]),
                canonical_json(event["evidenceIds"]),
                canonical_json(event["inverseEventJson"]),
                event["correlationId"],
            ),
        )
        after_items = {
            (item["targetType"], item["targetId"]): item for item in event["afterJson"]["items"]
        }
        order = 0
        for before_item in event["beforeJson"]["items"]:
            key = (before_item["targetType"], before_item["targetId"])
            after_item = after_items.get(key)
            if after_item is None:
                continue
            after_fields = {field["name"]: field["value"] for field in after_item["fields"]}
            for field in before_item["fields"]:
                if field["name"] not in after_fields:
                    continue
                order += 1
                connection.execute(
                    """
                    INSERT INTO event_effects(
                        effect_id, event_id, effect_order, target_type, target_id,
                        field_name, before_json, after_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"eff_{event['eventId'].removeprefix('evt_')}_{order:03d}",
                        event["eventId"],
                        order,
                        before_item["targetType"],
                        before_item["targetId"],
                        field["name"],
                        canonical_json(field["value"]),
                        canonical_json(after_fields[field["name"]]),
                    ),
                )

    def _rule_projection(
        self,
        *,
        transactions: Sequence[Transaction],
        rule_id: str,
        rule_active: bool,
        totals: FinanceTotals,
    ) -> dict[str, Any]:
        items = [
            self._projection_item(
                "transaction",
                transaction.transaction_id,
                {
                    "classification": transaction.classification,
                    "category": transaction.category,
                    "ruleId": transaction.rule_id,
                },
            )
            for transaction in transactions
        ]
        items.extend(
            [
                self._projection_item("classification_rule", rule_id, {"active": rule_active}),
                self._projection_item(
                    "forecast",
                    FORECAST_ID,
                    {
                        "unresolvedExpenseMinor": totals.unresolved_expense_minor,
                        "projectedLowPointMinor": totals.projected_low_point_minor,
                        "reserveShortfallMinor": totals.reserve_shortfall_minor,
                    },
                ),
            ]
        )
        return {"items": items}

    def create_classification_rule(
        self,
        *,
        merchant_contains: str,
        maximum_amount_minor: int,
        target_classification: str,
        target_category: str,
        effective_from: str,
        source_turn_id: str,
        owner_statement: str,
        event_id: str = "evt_koru_rule_mitre10",
        rule_id: str = "rule_koru_mitre10_under_500",
        claim_id: str = "claim_koru_mitre10_client_fitout",
        occurred_at: str = "2026-07-17T08:04:01+12:00",
    ) -> EventResult:
        candidate_rule = ClassificationRule(
            rule_id=rule_id,
            merchant_contains=merchant_contains,
            maximum_amount_minor=maximum_amount_minor,
            currency="NZD",
            target_classification=target_classification,
            target_category=target_category,
            effective_from=effective_from,
        )
        correlation_id = "corr_koru_rule_mitre10"
        with self.store.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM finance_events WHERE event_id = ?", (event_id,)
            ).fetchone():
                raise FinanceStateError(f"event already exists: {event_id}")
            before_totals, _ = self.preview_state(connection)
            all_before = self.load_transactions(connection)
            affected_before = sorted(
                (
                    transaction
                    for transaction in all_before
                    if rule_matches(candidate_rule, transaction)
                ),
                key=lambda transaction: transaction.transaction_id,
            )
            if not affected_before:
                raise FinanceStateError("rule matched no posted transactions")

            claim_digest = hashlib.sha256(owner_statement.encode()).hexdigest()
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    turn_id, workspace_id, thread_id, role, content, occurred_at,
                    status, evidence_ids_json
                ) VALUES (?, ?, ?, 'owner', ?, ?, 'complete', ?)
                ON CONFLICT(turn_id) DO NOTHING
                """,
                (
                    source_turn_id,
                    WORKSPACE_ID,
                    THREAD_ID,
                    owner_statement,
                    occurred_at,
                    canonical_json(["evd_koru_owner_claim_mitre10"]),
                ),
            )
            connection.execute(
                """
                INSERT INTO source_items(
                    source_item_id, workspace_id, source_type, label, digest,
                    mapping_version, received_at, status, row_count
                ) VALUES ('src_koru_owner_claim_mitre10', ?, 'owner_claim',
                    'Owner classification instruction for Mitre 10', ?, 'owner_claim@1',
                    ?, 'processed', 1)
                """,
                (WORKSPACE_ID, claim_digest, occurred_at),
            )
            connection.execute(
                """
                INSERT INTO evidence_links(
                    evidence_id, workspace_id, source_item_id, source_row_id, label, created_at
                ) VALUES ('evd_koru_owner_claim_mitre10', ?,
                    'src_koru_owner_claim_mitre10', NULL,
                    'Owner said Mitre 10 was a client fit-out below NZD 500', ?)
                """,
                (WORKSPACE_ID, occurred_at),
            )
            scope = {
                "merchantContains": merchant_contains,
                "maximumAmountMinor": maximum_amount_minor,
                "currency": "NZD",
            }
            connection.execute(
                """
                INSERT INTO claims(
                    claim_id, workspace_id, claim_type, statement, source_turn_id,
                    scope_json, effective_date, recorded_at, status, supersedes_claim_id
                ) VALUES (?, ?, 'classification_instruction', ?, ?, ?, ?, ?, 'active', NULL)
                """,
                (
                    claim_id,
                    WORKSPACE_ID,
                    owner_statement,
                    source_turn_id,
                    canonical_json(scope),
                    effective_from,
                    occurred_at,
                ),
            )
            connection.execute(
                """
                INSERT INTO classification_rules(
                    rule_id, workspace_id, merchant_contains, maximum_amount_minor,
                    currency, target_classification, target_category, effective_from,
                    priority, active, source_turn_id, source_claim_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'NZD', ?, ?, ?, 100, 1, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    WORKSPACE_ID,
                    merchant_contains,
                    maximum_amount_minor,
                    target_classification,
                    target_category,
                    effective_from,
                    source_turn_id,
                    claim_id,
                    occurred_at,
                    occurred_at,
                ),
            )
            self.apply_classifications(connection, occurred_at=occurred_at)
            after_totals, _ = self.preview_state(connection)
            after_by_id = {
                transaction.transaction_id: transaction
                for transaction in self.load_transactions(connection)
            }
            affected_after = [after_by_id[item.transaction_id] for item in affected_before]
            before_projection = self._rule_projection(
                transactions=affected_before,
                rule_id=rule_id,
                rule_active=False,
                totals=before_totals,
            )
            after_projection = self._rule_projection(
                transactions=affected_after,
                rule_id=rule_id,
                rule_active=True,
                totals=after_totals,
            )
            event_scope = {
                "merchantContains": merchant_contains,
                "maximumAmountMinor": maximum_amount_minor,
                "currency": "NZD",
                "targetClassification": target_classification,
                "targetCategory": target_category,
                "transactionIds": [item.transaction_id for item in affected_before],
            }
            evidence_ids = [
                *[item.evidence_id for item in affected_before],
                "evd_koru_owner_claim_mitre10",
                "evd_koru_forecast_30d",
            ]
            event = {
                "eventVersion": "finance.event@1",
                "eventId": event_id,
                "workspaceId": WORKSPACE_ID,
                "eventType": "classification_rule.created",
                "actor": "agent",
                "occurredAt": occurred_at,
                "sourceTurnId": source_turn_id,
                "reason": (
                    "Owner said Mitre 10 was a client fit-out and limited the rule to "
                    "purchases below NZD 500."
                ),
                "beforeJson": before_projection,
                "afterJson": after_projection,
                "scopeJson": event_scope,
                "evidenceIds": list(dict.fromkeys(evidence_ids)),
                "inverseEventJson": {
                    "eventType": "event.undone",
                    "reason": "Restore the exact state before the classification rule was committed.",
                    "beforeJson": after_projection,
                    "afterJson": before_projection,
                    "scopeJson": event_scope,
                },
                "correlationId": correlation_id,
            }
            self._append_event(connection, event)
            derived = self.recompute_derived(
                connection, occurred_at=occurred_at, correlation_id=correlation_id
            )
            surface = work_receipt_surface(
                event_id=event_id,
                title="Classification updated",
                subtitle="One transaction changed; the event can be undone",
                changes=[
                    {
                        "field": "classification",
                        "label": "Mitre 10 classification",
                        "before": affected_before[0].classification,
                        "after": affected_after[0].classification,
                    },
                    {
                        "field": "category",
                        "label": "Category",
                        "before": affected_before[0].category,
                        "after": affected_after[0].category,
                    },
                    {
                        "field": "unresolvedExpenseMinor",
                        "label": "Unresolved expenses",
                        "before": before_totals.unresolved_expense_minor,
                        "after": after_totals.unresolved_expense_minor,
                    },
                ],
                evidence_ids=[item.evidence_id for item in affected_before]
                + ["evd_koru_owner_claim_mitre10"],
                data_through=occurred_at,
                inverse_label="Undo",
            )
            self.set_surface(connection, surface)
            snapshot = self.store_snapshot(
                connection,
                snapshot_id="snap_koru_after_rule",
                totals=derived.totals,
                occurred_at=occurred_at,
            )
        return EventResult(event=event, snapshot=snapshot)

    def _event_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "eventVersion": "finance.event@1",
            "eventId": row["event_id"],
            "workspaceId": row["workspace_id"],
            "eventType": row["event_type"],
            "actor": row["actor"],
            "occurredAt": row["occurred_at"],
            "sourceTurnId": row["source_turn_id"],
            "reason": row["reason"],
            "beforeJson": _json(row["before_json"]),
            "afterJson": _json(row["after_json"]),
            "scopeJson": _json(row["scope_json"]),
            "evidenceIds": _json(row["evidence_ids_json"]),
            "inverseEventJson": _json(row["inverse_event_json"]),
            "correlationId": row["correlation_id"],
        }

    def _apply_projection(
        self, connection: sqlite3.Connection, projection: Mapping[str, Any], *, occurred_at: str
    ) -> None:
        for item in projection["items"]:
            fields = {field["name"]: field["value"] for field in item["fields"]}
            if item["targetType"] == "classification_rule" and "active" in fields:
                connection.execute(
                    "UPDATE classification_rules SET active = ?, updated_at = ? WHERE rule_id = ?",
                    (1 if fields["active"] else 0, occurred_at, item["targetId"]),
                )
            elif item["targetType"] == "transaction":
                connection.execute(
                    """
                    UPDATE transactions
                    SET classification = ?, category = ?, rule_id = ?,
                        classification_source = ?, updated_at = ?
                    WHERE transaction_id = ?
                    """,
                    (
                        fields.get("classification"),
                        fields.get("category"),
                        fields.get("ruleId"),
                        "explicit_rule" if fields.get("ruleId") else "deterministic",
                        occurred_at,
                        item["targetId"],
                    ),
                )

    def _invert_event(
        self,
        *,
        target_event_id: str,
        new_event_id: str,
        actor: str,
        reason: str,
        occurred_at: str,
        snapshot_id: str,
    ) -> EventResult:
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM finance_events WHERE event_id = ?", (target_event_id,)
            ).fetchone()
            if row is None:
                raise KeyError(target_event_id)
            if row["event_type"] == "event.undone":
                if row["redone_by_event_id"] is not None:
                    raise FinanceStateError("undo event has already been reapplied")
            elif row["undone_by_event_id"] is not None:
                raise FinanceStateError("event has already been undone")
            target = self._event_from_row(row)
            inverse = target["inverseEventJson"]
            scope = dict(inverse["scopeJson"])
            self._apply_projection(connection, inverse["afterJson"], occurred_at=occurred_at)
            self.apply_classifications(connection, occurred_at=occurred_at)

            # Reapplication still resolves the affected set in finance code.
            if inverse["eventType"] == "classification_rule.reapplied":
                rule_row = connection.execute(
                    "SELECT * FROM classification_rules WHERE rule_id = ?",
                    ("rule_koru_mitre10_under_500",),
                ).fetchone()
                rule = self._rule_from_row(rule_row)
                scope["transactionIds"] = sorted(
                    transaction.transaction_id
                    for transaction in self.load_transactions(connection)
                    if rule_matches(rule, transaction)
                )
            reverse_type = (
                "classification_rule.reapplied"
                if inverse["eventType"] == "event.undone"
                else "event.undone"
            )
            event = {
                "eventVersion": "finance.event@1",
                "eventId": new_event_id,
                "workspaceId": WORKSPACE_ID,
                "eventType": inverse["eventType"],
                "actor": actor,
                "occurredAt": occurred_at,
                "sourceTurnId": target["sourceTurnId"],
                "reason": reason,
                "beforeJson": inverse["beforeJson"],
                "afterJson": inverse["afterJson"],
                "scopeJson": scope,
                "evidenceIds": target["evidenceIds"],
                "inverseEventJson": {
                    "eventType": reverse_type,
                    "reason": f"Invert {new_event_id} and restore its prior state.",
                    "beforeJson": inverse["afterJson"],
                    "afterJson": inverse["beforeJson"],
                    "scopeJson": scope,
                },
                "correlationId": target["correlationId"],
            }
            self._append_event(connection, event)
            if row["event_type"] == "event.undone":
                connection.execute(
                    "UPDATE finance_events SET redone_by_event_id = ? WHERE event_id = ?",
                    (new_event_id, target_event_id),
                )
            else:
                connection.execute(
                    "UPDATE finance_events SET undone_by_event_id = ? WHERE event_id = ?",
                    (new_event_id, target_event_id),
                )
            derived = self.recompute_derived(
                connection,
                occurred_at=occurred_at,
                correlation_id=target["correlationId"],
            )
            is_undo = event["eventType"] == "event.undone"
            before_unresolved = next(
                field["value"]
                for item in event["beforeJson"]["items"]
                if item["targetType"] == "forecast"
                for field in item["fields"]
                if field["name"] == "unresolvedExpenseMinor"
            )
            after_unresolved = next(
                field["value"]
                for item in event["afterJson"]["items"]
                if item["targetType"] == "forecast"
                for field in item["fields"]
                if field["name"] == "unresolvedExpenseMinor"
            )
            transaction_before = next(
                item for item in event["beforeJson"]["items"] if item["targetType"] == "transaction"
            )
            transaction_after = next(
                item for item in event["afterJson"]["items"] if item["targetType"] == "transaction"
            )
            before_fields = {
                field["name"]: field["value"] for field in transaction_before["fields"]
            }
            after_fields = {field["name"]: field["value"] for field in transaction_after["fields"]}
            surface = work_receipt_surface(
                event_id=new_event_id,
                title="Classification change undone" if is_undo else "Classification reapplied",
                subtitle="All dependent finance outputs were recomputed",
                changes=[
                    {
                        "field": "classification",
                        "label": "Mitre 10 classification",
                        "before": before_fields["classification"],
                        "after": after_fields["classification"],
                    },
                    {
                        "field": "category",
                        "label": "Category",
                        "before": before_fields["category"],
                        "after": after_fields["category"],
                    },
                    {
                        "field": "unresolvedExpenseMinor",
                        "label": "Unresolved expenses",
                        "before": before_unresolved,
                        "after": after_unresolved,
                    },
                ],
                evidence_ids=event["evidenceIds"],
                data_through=occurred_at,
                inverse_label="Redo" if is_undo else "Undo",
            )
            self.set_surface(connection, surface)
            snapshot = self.store_snapshot(
                connection,
                snapshot_id=snapshot_id,
                totals=derived.totals,
                occurred_at=occurred_at,
            )
        return EventResult(event=event, snapshot=snapshot)

    def undo_event(
        self,
        event_id: str,
        *,
        request_id: str = "req_koru_undo_mitre10",
        reason: str = "Owner requested the exact prior state.",
        occurred_at: str = "2026-07-17T08:05:00+12:00",
    ) -> dict[str, Any]:
        undo_id = (
            "evt_koru_rule_mitre10_undo"
            if event_id == "evt_koru_rule_mitre10"
            else stable_id("evt", event_id, request_id, "undo")
        )
        result = self._invert_event(
            target_event_id=event_id,
            new_event_id=undo_id,
            actor="owner",
            reason=reason,
            occurred_at=occurred_at,
            snapshot_id="snap_koru_after_undo",
        )
        return {
            "requestId": request_id,
            "originalEventId": event_id,
            "undoEvent": result.event,
            "snapshotId": result.snapshot["snapshotId"],
        }

    def redo_event(
        self,
        undo_event_id: str,
        *,
        occurred_at: str = "2026-07-17T08:06:00+12:00",
    ) -> EventResult:
        redo_id = (
            "evt_koru_rule_mitre10_redo"
            if undo_event_id == "evt_koru_rule_mitre10_undo"
            else stable_id("evt", undo_event_id, "redo")
        )
        return self._invert_event(
            target_event_id=undo_event_id,
            new_event_id=redo_id,
            actor="owner",
            reason="Owner reapplied the same deterministic classification event.",
            occurred_at=occurred_at,
            snapshot_id="snap_koru_after_redo",
        )
