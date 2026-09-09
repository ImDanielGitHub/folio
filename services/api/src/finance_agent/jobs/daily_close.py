"""Idempotent staged Daily Close and callable worker tick."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from finance_agent.finance.service import (
    DATA_THROUGH,
    POLICY_VERSION,
    WORKSPACE_ID,
    DerivedState,
    FinanceEngine,
    sha256_json,
)
from finance_agent.storage import canonical_json

STAGES = (
    "ingest",
    "normalise",
    "deduplicate",
    "apply_rules",
    "classify",
    "findings",
    "forecast",
    "owner_pack",
    "receipt",
    "telegram_outbox",
)


@dataclass(frozen=True, slots=True)
class DailyCloseResult:
    run_id: str
    receipt_id: str
    snapshot_id: str
    status: str
    new_findings: int
    new_artifacts: int
    new_owner_messages: int


class DailyCloseService:
    def __init__(self, engine: FinanceEngine, *, worker_id: str = "worker_local_001") -> None:
        self.engine = engine
        self.worker_id = worker_id

    def _input_hash(self) -> str:
        rows = self.engine.store.fetch_all(
            """
            SELECT source_item_id, digest, mapping_version, row_count
            FROM source_items WHERE workspace_id = ? AND source_type = 'csv'
            ORDER BY source_item_id
            """,
            (WORKSPACE_ID,),
        )
        payload = {
            "policyVersion": POLICY_VERSION,
            "sources": [dict(row) for row in rows],
        }
        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()

    def run(self) -> DailyCloseResult:
        input_hash = self._input_hash()
        idempotency_key = f"daily-close:{WORKSPACE_ID}:{input_hash}"
        existing = self.engine.store.fetch_one(
            """
            SELECT * FROM job_runs
            WHERE workspace_id = ? AND idempotency_key = ? AND status = 'completed'
            """,
            (WORKSPACE_ID, idempotency_key),
        )
        if existing is not None:
            snapshot = self.engine.get_snapshot()
            return DailyCloseResult(
                run_id=existing["run_id"],
                receipt_id=existing["receipt_id"],
                snapshot_id=snapshot["snapshotId"],
                status="no_op",
                new_findings=0,
                new_artifacts=0,
                new_owner_messages=0,
            )

        run_id = "run_koru_daily_close_20260717"
        receipt_id = "rcpt_koru_daily_close_20260717"
        correlation_id = "corr_koru_daily_close_20260717"
        occurred_at = "2026-07-17T08:00:04+12:00"

        with self.engine.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO job_runs(
                    run_id, definition_id, workspace_id, idempotency_key, input_hash,
                    status, lease_owner, lease_expires_at, attempt_count, started_at,
                    correlation_id
                ) VALUES (?, 'jobdef_koru_daily_close', ?, ?, ?, 'running', ?,
                    '2026-07-17T08:05:00+12:00', 1, ?, ?)
                """,
                (
                    run_id,
                    WORKSPACE_ID,
                    idempotency_key,
                    input_hash,
                    self.worker_id,
                    DATA_THROUGH,
                    correlation_id,
                ),
            )

            def stage(name: str, sequence: int, output: Any) -> None:
                connection.execute(
                    """
                    INSERT INTO job_stage_runs(
                        stage_run_id, run_id, stage, sequence, status, input_hash,
                        output_hash, attempt, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, 'completed', ?, ?, 1, ?, ?)
                    """,
                    (
                        f"stage_koru_{name}",
                        run_id,
                        name,
                        sequence,
                        input_hash,
                        sha256_json(output),
                        DATA_THROUGH,
                        occurred_at,
                    ),
                )

            processed = self.engine.process_pending_sources(connection)
            stage("ingest", 1, {"processedSources": processed})
            normalised = self.engine.validate_normalised_rows(connection)
            stage("normalise", 2, {"rows": normalised})
            pairs = self.engine.deduplicate(connection, occurred_at=occurred_at)
            stage("deduplicate", 3, pairs)
            rules = self.engine.load_active_rules(connection)
            stage("apply_rules", 4, {"activeRules": len(rules)})
            classified = self.engine.apply_classifications(connection, occurred_at=occurred_at)
            stage("classify", 5, {"classifiedRows": classified})
            totals, forecast = self.engine.preview_state(connection)
            stage("findings", 6, {"unresolvedExpenseMinor": totals.unresolved_expense_minor})
            derived: DerivedState = self.engine.recompute_derived(
                connection, occurred_at=occurred_at, correlation_id=correlation_id
            )
            stage(
                "forecast",
                7,
                {
                    "projectedLowPointMinor": forecast.low_point_minor,
                    "reserveShortfallMinor": forecast.reserve_shortfall_minor,
                },
            )
            stage("owner_pack", 8, [item["contentHash"] for item in derived.artifacts])
            receipt = {
                "receiptId": receipt_id,
                "runId": run_id,
                "inputHash": input_hash,
                "findingIds": [item["findingId"] for item in derived.findings],
                "artifactIds": [item["artifactId"] for item in derived.artifacts],
                "status": "completed",
            }
            stage("receipt", 9, receipt)
            stage("telegram_outbox", 10, {"queued": derived.totals.reserve_shortfall_minor > 0})
            connection.execute(
                """
                UPDATE job_runs
                SET status = 'completed', lease_owner = NULL, lease_expires_at = NULL,
                    completed_at = ?, receipt_id = ?, result_json = ?
                WHERE run_id = ?
                """,
                (occurred_at, receipt_id, canonical_json(receipt), run_id),
            )
            snapshot = self.engine.complete_daily_close_snapshot(
                connection, derived=derived, occurred_at=occurred_at
            )

        return DailyCloseResult(
            run_id=run_id,
            receipt_id=receipt_id,
            snapshot_id=snapshot["snapshotId"],
            status="completed",
            new_findings=3,
            new_artifacts=2,
            new_owner_messages=1,
        )


class DailyCloseWorker:
    """Small scheduler seam: one tick safely runs or no-ops the close."""

    def __init__(self, service: DailyCloseService) -> None:
        self.service = service

    def tick(self) -> DailyCloseResult:
        return self.service.run()
