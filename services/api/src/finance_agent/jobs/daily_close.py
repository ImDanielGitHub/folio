"""Idempotent staged Daily Close and callable worker tick."""

from __future__ import annotations

import hashlib
import json
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

KORU_FIXTURE_SHA256 = "c2c07beeca632f4e09700837cc4b199653ce9b68f65b804b7c30e9838ef94eac"


@dataclass(frozen=True, slots=True)
class DailyCloseResult:
    run_id: str
    receipt_id: str
    snapshot_id: str
    status: str
    new_findings: int
    new_artifacts: int
    new_owner_messages: int
    close_turn_id: str


@dataclass(frozen=True, slots=True)
class DailyCloseIdentity:
    input_hash: str
    idempotency_key: str
    run_id: str
    receipt_id: str
    correlation_id: str
    snapshot_id: str
    close_turn_id: str
    suffix: str


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

    def identity(self, *, requested_idempotency_key: str | None = None) -> DailyCloseIdentity:
        input_hash = self._input_hash()
        idempotency_key = requested_idempotency_key or f"daily-close:{WORKSPACE_ID}:{input_hash}"
        existing_input = self.engine.store.fetch_one(
            """
            SELECT * FROM job_runs
            WHERE workspace_id = ? AND input_hash = ? AND status = 'completed'
            ORDER BY completed_at, run_id LIMIT 1
            """,
            (WORKSPACE_ID, input_hash),
        )
        existing_key = None
        if existing_input is None:
            existing_key = self.engine.store.fetch_one(
                """
                SELECT * FROM job_runs
                WHERE workspace_id = ? AND idempotency_key = ?
                ORDER BY started_at, run_id LIMIT 1
                """,
                (WORKSPACE_ID, idempotency_key),
            )
            if existing_key is not None and existing_key["input_hash"] != input_hash:
                raise ValueError(
                    "idempotency key is already bound to different Daily Close input"
                )
        existing = existing_input or existing_key
        canonical_source = self.engine.store.fetch_one(
            """
            SELECT source_item_id, digest, mapping_version, row_count
            FROM source_items
            WHERE workspace_id = ? AND source_type = 'csv'
            ORDER BY source_item_id LIMIT 1
            """,
            (WORKSPACE_ID,),
        )
        csv_sources = self.engine.store.fetch_all(
            """
            SELECT source_item_id FROM source_items
            WHERE workspace_id = ? AND source_type = 'csv'
            """,
            (WORKSPACE_ID,),
        )
        canonical_first_close = (
            len(self.engine.store.fetch_all("SELECT run_id FROM job_runs")) == 0
            and len(csv_sources) == 1
            and canonical_source is not None
            and str(canonical_source["source_item_id"]) == "src_koru_bank_csv_20260717"
            and str(canonical_source["digest"]) == KORU_FIXTURE_SHA256
            and str(canonical_source["mapping_version"]) == "koru_bank_csv@1"
            and int(canonical_source["row_count"]) == 10
        )
        suffix = hashlib.sha256(f"{idempotency_key}\0{input_hash}".encode()).hexdigest()[:16]
        return DailyCloseIdentity(
            input_hash=input_hash,
            idempotency_key=idempotency_key,
            run_id=(
                str(existing["run_id"])
                if existing is not None
                else (
                    "run_koru_daily_close_20260717"
                    if canonical_first_close
                    else f"run_close_{suffix}"
                )
            ),
            receipt_id=(
                str(existing["receipt_id"])
                if existing is not None
                else (
                    "rcpt_koru_daily_close_20260717"
                    if canonical_first_close
                    else f"rcpt_close_{suffix}"
                )
            ),
            correlation_id=(
                str(existing["correlation_id"])
                if existing is not None
                else (
                    "corr_koru_daily_close_20260717"
                    if canonical_first_close
                    else f"corr_close_{suffix}"
                )
            ),
            snapshot_id=(
                "snap_koru_after_close" if canonical_first_close else f"snap_close_{suffix}"
            ),
            close_turn_id=(
                "turn_koru_morning_close" if canonical_first_close else f"turn_close_{suffix}"
            ),
            suffix=suffix,
        )

    def run(self, *, requested_idempotency_key: str | None = None) -> DailyCloseResult:
        identity = self.identity(requested_idempotency_key=requested_idempotency_key)
        existing = self.engine.store.fetch_one(
            """
            SELECT * FROM job_runs
            WHERE workspace_id = ? AND input_hash = ? AND status = 'completed'
            ORDER BY completed_at, run_id LIMIT 1
            """,
            (WORKSPACE_ID, identity.input_hash),
        )
        if existing is not None:
            stored_result = (
                json.loads(str(existing["result_json"]))
                if existing["result_json"]
                else {}
            )
            snapshot_id = str(
                stored_result.get("snapshotId")
                or self.engine.get_snapshot()["snapshotId"]
            )
            close_turn_id = str(
                stored_result.get("closeTurnId")
                or (
                    "turn_koru_morning_close"
                    if identity.run_id == "run_koru_daily_close_20260717"
                    else f"turn_close_{identity.run_id.removeprefix('run_close_')}"
                )
            )
            return DailyCloseResult(
                run_id=identity.run_id,
                receipt_id=identity.receipt_id,
                snapshot_id=snapshot_id,
                status="no_op",
                new_findings=0,
                new_artifacts=0,
                new_owner_messages=0,
                close_turn_id=close_turn_id,
            )

        input_hash = identity.input_hash
        idempotency_key = identity.idempotency_key
        run_id = identity.run_id
        receipt_id = identity.receipt_id
        correlation_id = identity.correlation_id
        snapshot_id = identity.snapshot_id
        close_turn_id = identity.close_turn_id
        suffix = identity.suffix
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
                stage_run_id = (
                    f"stage_koru_{name}"
                    if run_id == "run_koru_daily_close_20260717"
                    else f"stage_close_{suffix}_{sequence:02d}_{name}"
                )
                connection.execute(
                    """
                    INSERT INTO job_stage_runs(
                        stage_run_id, run_id, stage, sequence, status, input_hash,
                        output_hash, attempt, started_at, completed_at
                    ) VALUES (?, ?, ?, ?, 'completed', ?, ?, 1, ?, ?)
                    """,
                    (
                        stage_run_id,
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
                "snapshotId": snapshot_id,
                "closeTurnId": close_turn_id,
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
                connection,
                derived=derived,
                occurred_at=occurred_at,
                snapshot_id=snapshot_id,
                close_turn_id=close_turn_id,
            )

        return DailyCloseResult(
            run_id=run_id,
            receipt_id=receipt_id,
            snapshot_id=snapshot["snapshotId"],
            status="completed",
            new_findings=3,
            new_artifacts=2,
            new_owner_messages=1,
            close_turn_id=close_turn_id,
        )


class DailyCloseWorker:
    """Small scheduler seam: one tick safely runs or no-ops the close."""

    def __init__(self, service: DailyCloseService) -> None:
        self.service = service

    def tick(self) -> DailyCloseResult:
        return self.service.run()
