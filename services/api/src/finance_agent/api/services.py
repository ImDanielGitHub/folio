"""Concrete local composition adapters for the frozen finance API boundary."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from finance_agent.agent.controller import (
    FinanceAgentController,
    ReceiptSink,
    TurnRequest,
    WorkReceipt,
)
from finance_agent.agent.dialogue import (
    DialogueFrame,
)
from finance_agent.agent.events import RunEvent, RunEventBuffer
from finance_agent.agent.harness import ModelHarness
from finance_agent.agent.plan import (
    CreateClassificationRuleAction,
    PrepareOwnerPackAction,
    QuerySummaryAction,
    QueryTransactionsAction,
    RecordBusinessClaimAction,
    RunCashScenarioAction,
    ShowSurfaceAction,
    UndoEventAction,
    WriteAction,
)
from finance_agent.agent.ports import FinanceContext, FinanceServiceResult
from finance_agent.api.routes import ArtifactPayload
from finance_agent.api.working_understanding import WorkingUnderstandingRuntime
from finance_agent.connectors import TelegramConfig, TelegramFixtureIngestor
from finance_agent.connectors.akahu import (
    AkahuReadOnlyAdapter,
    AkahuTransaction,
    normalise_accounts,
    normalise_transactions,
)
from finance_agent.connectors.base import ConnectorError
from finance_agent.connectors.plaid import (
    PlaidReadOnlyAdapter,
)
from finance_agent.connectors.plaid import (
    normalise_accounts as normalise_plaid_accounts,
)
from finance_agent.connectors.plaid import (
    normalise_transactions as normalise_plaid_transactions,
)
from finance_agent.connectors.plaid_events import record_plaid_event_batch
from finance_agent.finance import FinanceEngine, FinanceStateError, FinanceTotals
from finance_agent.finance.service import THREAD_ID, WORKSPACE_ID
from finance_agent.finance.surfaces import (
    living_brief_surface,
)
from finance_agent.jobs import DailyCloseResult, DailyCloseService
from finance_agent.jobs.daily_close import STAGES
from finance_agent.models.base import AdapterStatus, ModelMode
from finance_agent.models.lm_studio import LMStudioAdapter, LMStudioConfig
from finance_agent.models.openai import OpenAIConfig, OpenAIResponsesAdapter
from finance_agent.models.router import ModelModeRouter
from finance_agent.storage import SQLiteConversationStore, SQLiteStore, canonical_json

ROOT = Path(__file__).resolve().parents[5]
DEMO_CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"
DEMO_TELEGRAM = ROOT / "fixtures" / "demo" / "telegram-update.json"
DEMO_TELEGRAM_ATTACHMENT = (
    ROOT / "fixtures" / "demo" / "telegram-attachment-reference.json"
)
AKAHU_MAPPING_VERSION = "akahu_live@1"
AKAHU_MAX_PAGES = 100
AKAHU_MAX_ITEMS = 20_000
AKAHU_MAX_WINDOW_DAYS = 366
PLAID_MAPPING_VERSION = "plaid_live@2"
PLAID_MAX_PAGES = 100
PLAID_MAX_ITEMS = 20_000
NEW_ZEALAND_TIME = ZoneInfo("Pacific/Auckland")


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _now() -> datetime:
    return datetime.now(UTC)


def _finance_totals(snapshot: Mapping[str, Any]) -> FinanceTotals:
    totals = snapshot["totals"]
    return FinanceTotals(
        current_balance_minor=int(totals["currentBalanceMinor"]),
        protected_reserve_minor=int(totals["protectedReserveMinor"]),
        business_income_minor=int(totals["businessIncomeMinor"]),
        business_expense_minor=int(totals["businessExpenseMinor"]),
        personal_expense_minor=int(totals["personalExpenseMinor"]),
        unresolved_expense_minor=int(totals["unresolvedExpenseMinor"]),
        projected_low_point_minor=int(totals["projectedLowPointMinor"]),
        reserve_shortfall_minor=int(totals["reserveShortfallMinor"]),
    )


def _akahu_window(start: str | None, end: str | None) -> tuple[str, str]:
    try:
        end_date = date.fromisoformat(end) if end else _now().date()
        start_date = date.fromisoformat(start) if start else end_date - timedelta(days=90)
    except ValueError as exc:
        raise ValueError("Akahu sync dates must use YYYY-MM-DD") from exc
    if start_date > end_date:
        raise ValueError("Akahu sync start must be on or before end")
    if (end_date - start_date).days > AKAHU_MAX_WINDOW_DAYS:
        raise ValueError(
            f"Akahu sync window cannot exceed {AKAHU_MAX_WINDOW_DAYS} days"
        )
    return start_date.isoformat(), end_date.isoformat()


def _akahu_query_window(start: str, end: str) -> tuple[str, str]:
    """Translate inclusive owner dates to Akahu's exclusive/inclusive bounds."""

    start_date = date.fromisoformat(start)
    end_date = date.fromisoformat(end)
    start_boundary = datetime.combine(
        start_date,
        time.min,
        tzinfo=NEW_ZEALAND_TIME,
    ) - timedelta(milliseconds=1)
    end_boundary = datetime.combine(
        end_date,
        time(23, 59, 59, 999000),
        tzinfo=NEW_ZEALAND_TIME,
    )
    return (
        start_boundary.isoformat(timespec="milliseconds"),
        end_boundary.isoformat(timespec="milliseconds"),
    )


def _akahu_csv(transactions: Sequence[AkahuTransaction]) -> bytes:
    output = io.StringIO(newline="")
    fieldnames = (
        "source_row_id",
        "account_id",
        "occurred_on",
        "description",
        "amount_minor",
        "currency",
        "status",
        "external_reference",
    )
    writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    for transaction in transactions:
        writer.writerow(
            {
                "source_row_id": _stable_id("row", transaction.external_reference),
                "account_id": transaction.account_id,
                "occurred_on": transaction.occurred_on,
                "description": transaction.description,
                "amount_minor": str(transaction.amount_minor),
                "currency": transaction.currency,
                "status": "posted",
                "external_reference": transaction.external_reference,
            }
        )
    return output.getvalue().encode()


class SQLiteReceiptSink(ReceiptSink):
    """Persist controller work receipts before a run can report completion."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    async def commit(self, receipt: WorkReceipt) -> None:
        occurred_at = _now().isoformat()
        payload = {
            "receiptId": receipt.receipt_id,
            "runId": receipt.run_id,
            "contentHash": receipt.content_hash,
            "evidenceIds": list(receipt.evidence_ids),
            "status": receipt.status,
        }
        with self.store.transaction() as connection:
            existing = connection.execute(
                "SELECT receipt_json FROM work_receipts WHERE receipt_id = ?",
                (receipt.receipt_id,),
            ).fetchone()
            encoded = canonical_json(payload)
            if existing is not None:
                if str(existing["receipt_json"]) != encoded:
                    raise ValueError("receipt id is already bound to different content")
                return
            connection.execute(
                """
                INSERT INTO work_receipts(
                    receipt_id, workspace_id, run_id, receipt_json,
                    content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    WORKSPACE_ID,
                    receipt.run_id,
                    encoded,
                    receipt.content_hash,
                    occurred_at,
                ),
            )


class FinanceCoreAdapter:
    """Map the bounded agent port onto Task 1's public deterministic engine."""

    def __init__(self, engine: FinanceEngine) -> None:
        self.engine = engine

    async def load_context(self, workspace_id: str, thread_id: str) -> FinanceContext:
        if workspace_id != WORKSPACE_ID or thread_id != THREAD_ID:
            raise KeyError("unknown local finance workspace or thread")
        snapshot = self.engine.get_snapshot()
        surface = snapshot["currentSurface"]
        findings = snapshot["findings"]
        evidence_ids = sorted(
            {
                evidence_id
                for finding in findings
                for evidence_id in finding.get("evidenceIds", [])
            }
        )
        cash_surface = self.engine.get_cash_scenario_surface()
        cash_block = next(
            block for block in cash_surface["blocks"] if block["type"] == "cash_series"
        )
        unresolved = self.engine.store.fetch_one(
            """
            SELECT description, occurred_on FROM transactions
            WHERE workspace_id = ? AND classification = 'unresolved'
              AND status = 'posted'
            ORDER BY occurred_on, transaction_id LIMIT 1
            """,
            (WORKSPACE_ID,),
        )
        latest_event = self.engine.store.fetch_one(
            """
            SELECT event_id FROM finance_events
            WHERE workspace_id = ? AND undone_by_event_id IS NULL
              AND redone_by_event_id IS NULL
            ORDER BY occurred_at DESC, event_id DESC LIMIT 1
            """,
            (WORKSPACE_ID,),
        )
        projection = {
            "dataThrough": snapshot["freshness"]["dataThrough"],
            "aggregate_amounts": dict(snapshot["totals"]),
            "finding_labels": [finding["title"] for finding in findings],
            "forecast_assumptions": list(cash_block["assumptions"]),
            "owner_claims": [
                {
                    "statement": str(row["statement"]),
                    "sourceTurnId": str(row["source_turn_id"]),
                }
                for row in self.engine.store.fetch_all(
                    """
                    SELECT statement, source_turn_id FROM claims
                    WHERE workspace_id = ? AND status = 'active'
                    ORDER BY recorded_at, claim_id
                    """,
                    (WORKSPACE_ID,),
                )
            ],
            "evidence_labels": evidence_ids,
        }
        return FinanceContext(
            workspace_id=workspace_id,
            thread_id=thread_id,
            current_surface_type=str(surface["surfaceType"]),
            projection=projection,
            unresolved_merchant=(
                "MITRE 10"
                if unresolved and "MITRE 10" in unresolved["description"].upper()
                else None
            ),
            unresolved_date=str(unresolved["occurred_on"]) if unresolved else None,
            latest_undoable_event_id=str(latest_event["event_id"]) if latest_event else None,
            scenario_id="scenario_koru_laptop",
            scenario_amount_minor=300000,
            scenario_date="2026-08-07",
        )

    async def query_summary(self, action: QuerySummaryAction) -> FinanceServiceResult:
        snapshot = self.engine.get_snapshot()
        evidence_ids = tuple(
            dict.fromkeys(
                evidence_id
                for finding in snapshot["findings"]
                for evidence_id in finding["evidenceIds"]
            )
        )
        return FinanceServiceResult(
            action_id=action.action_id,
            kind=action.kind,
            status="completed",
            data={"window": action.window, "totals": snapshot["totals"]},
            evidence_ids=evidence_ids,
        )

    def _transaction_rows(
        self,
        *,
        merchant_contains: str | None,
        classification: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        clauses = ["workspace_id = ?"]
        parameters: list[object] = [WORKSPACE_ID]
        if merchant_contains:
            clauses.append("UPPER(description) LIKE UPPER(?)")
            parameters.append(f"%{merchant_contains}%")
        if classification != "any":
            clauses.append("classification = ?")
            parameters.append(classification)
        parameters.append(limit)
        rows = self.engine.store.fetch_all(
            f"""
            SELECT transaction_id, occurred_on, description, amount_minor, currency,
                   status, classification, category, evidence_id
            FROM transactions WHERE {' AND '.join(clauses)}
            ORDER BY occurred_on, transaction_id LIMIT ?
            """,
            parameters,
        )
        return [
            {
                "transactionId": row["transaction_id"],
                "occurredOn": row["occurred_on"],
                "description": row["description"],
                "amountMinor": row["amount_minor"],
                "currency": row["currency"],
                "status": row["status"],
                "classification": row["classification"],
                "category": row["category"],
                "evidenceIds": [row["evidence_id"]],
            }
            for row in rows
        ]

    async def query_transactions(
        self, action: QueryTransactionsAction
    ) -> FinanceServiceResult:
        rows = self._transaction_rows(
            merchant_contains=action.merchant_contains,
            classification=action.classification,
            limit=action.limit,
        )
        return FinanceServiceResult(
            action_id=action.action_id,
            kind=action.kind,
            status="completed",
            data={"rows": rows, "count": len(rows)},
            evidence_ids=tuple(
                dict.fromkeys(
                    evidence_id for row in rows for evidence_id in row["evidenceIds"]
                )
            ),
        )

    async def run_cash_scenario(
        self, action: RunCashScenarioAction
    ) -> FinanceServiceResult:
        if (
            action.scenario_id != "scenario_koru_laptop"
            or action.currency != "NZD"
            or action.planned_amount_minor != 300000
            or action.planned_date != date(2026, 8, 7)
        ):
            return FinanceServiceResult(
                action_id=action.action_id,
                kind=action.kind,
                status="failed_closed",
                data={"reason": "scenario parameters are outside the frozen demo"},
            )
        surface = self.engine.get_cash_scenario_surface()
        return FinanceServiceResult(
            action_id=action.action_id,
            kind=action.kind,
            status="completed",
            data={"surface": surface},
            evidence_ids=("evd_koru_bank_csv", "evd_koru_forecast_30d"),
        )

    async def execute_reversible_writes(
        self,
        actions: Sequence[WriteAction],
        *,
        source_turn_id: str,
    ) -> tuple[FinanceServiceResult, ...]:
        claim = next(
            (item for item in actions if isinstance(item, RecordBusinessClaimAction)),
            None,
        )
        rule = next(
            (item for item in actions if isinstance(item, CreateClassificationRuleAction)),
            None,
        )
        results: list[FinanceServiceResult] = []
        rule_result: Any | None = None
        if rule is not None:
            existing = self.engine.store.fetch_one(
                """
                SELECT event_id FROM finance_events
                WHERE event_id = 'evt_koru_rule_mitre10'
                """
            )
            if existing is None:
                rule_result = self.engine.create_classification_rule(
                    merchant_contains=rule.merchant_contains,
                    maximum_amount_minor=rule.maximum_amount_minor,
                    target_classification=rule.target_classification,
                    target_category=rule.target_category,
                    effective_from=rule.effective_from.isoformat(),
                    source_turn_id=source_turn_id,
                    owner_statement=(
                        claim.statement
                        if claim is not None
                        else (
                            f"Treat {rule.merchant_contains} as {rule.target_category} "
                            f"below NZD {rule.maximum_amount_minor / 100:.2f}."
                        )
                    ),
                    claim_id=(
                        _stable_id("claim", source_turn_id, claim.action_id)
                        if claim is not None
                        else _stable_id("claim", source_turn_id, rule.action_id)
                    ),
                )
            if claim is not None:
                results.append(
                    FinanceServiceResult(
                        action_id=claim.action_id,
                        kind=claim.kind,
                        status="completed" if rule_result else "no_op",
                        data={"claimType": claim.claim_type},
                        evidence_ids=("evd_koru_owner_claim_mitre10",),
                    )
                )
            results.append(
                FinanceServiceResult(
                    action_id=rule.action_id,
                    kind=rule.kind,
                    status="completed" if rule_result else "no_op",
                    data={
                        "event": rule_result.event if rule_result else None,
                        "snapshotId": (
                            rule_result.snapshot["snapshotId"]
                            if rule_result
                            else self.engine.get_snapshot()["snapshotId"]
                        ),
                    },
                    evidence_ids=(
                        "evd_koru_mitre10_row",
                        "evd_koru_owner_claim_mitre10",
                    ),
                    event_id=(rule_result.event["eventId"] if rule_result else None),
                )
            )

        for action in actions:
            if action is claim or action is rule:
                continue
            if isinstance(action, RecordBusinessClaimAction):
                claim_id = _stable_id("claim", source_turn_id, action.action_id)
                self.engine.store.record_claim(
                    {
                        "claimId": claim_id,
                        "workspaceId": WORKSPACE_ID,
                        "claimType": action.claim_type,
                        "statement": action.statement,
                        "sourceTurnId": source_turn_id,
                        "scope": {},
                        "effectiveDate": action.effective_date.isoformat(),
                        "recordedAt": _now().isoformat(),
                    }
                )
                results.append(
                    FinanceServiceResult(
                        action_id=action.action_id,
                        kind=action.kind,
                        status="completed",
                        data={"claimId": claim_id},
                    )
                )
            elif isinstance(action, UndoEventAction):
                response = self.engine.undo_event(
                    action.target_event_id,
                    request_id=_stable_id("req", source_turn_id, action.action_id),
                )
                undo_event = response["undoEvent"]
                results.append(
                    FinanceServiceResult(
                        action_id=action.action_id,
                        kind=action.kind,
                        status="completed",
                        data=response,
                        evidence_ids=tuple(undo_event["evidenceIds"]),
                        event_id=undo_event["eventId"],
                    )
                )
        return tuple(results)

    async def recompute(self, event_ids: Sequence[str]) -> FinanceServiceResult:
        snapshot = self.engine.get_snapshot()
        return FinanceServiceResult(
            action_id="action_recompute_current",
            kind="recompute",
            status="no_op",
            data={"snapshotId": snapshot["snapshotId"], "eventIds": list(event_ids)},
        )

    async def prepare_owner_pack(
        self, action: PrepareOwnerPackAction
    ) -> FinanceServiceResult:
        snapshot = self.engine.get_snapshot()
        artifacts = snapshot["artifacts"]
        return FinanceServiceResult(
            action_id=action.action_id,
            kind=action.kind,
            status="completed",
            data={"format": action.format, "artifacts": artifacts},
            evidence_ids=tuple(
                dict.fromkeys(
                    evidence_id
                    for artifact in artifacts
                    for evidence_id in artifact["evidenceIds"]
                )
            ),
        )

    def _records_surface(self) -> dict[str, Any]:
        rows = self._transaction_rows(
            merchant_contains=None, classification="any", limit=100
        )
        snapshot = self.engine.get_snapshot()
        return {
            "specVersion": "FinanceSurfaceSpec@1",
            "surfaceId": "surface_koru_records",
            "surfaceType": "records_table",
            "title": "Prepared records",
            "subtitle": "Posted transactions · 1–17 July",
            "freshness": snapshot["freshness"],
            "blocks": [
                {
                    "blockId": "block_records_summary",
                    "type": "narrative",
                    "text": (
                        "The source-linked rows stay deterministic. The pending duplicate "
                        "is held out, and owner corrections remain visible in Activity."
                    ),
                    "tone": "neutral",
                },
                {
                    "blockId": "block_records_rows",
                    "type": "transaction_rows",
                    "rows": rows,
                    "totalMinor": snapshot["totals"]["currentBalanceMinor"],
                    "currency": "NZD",
                },
            ],
            "actions": [
                {
                    "actionId": "act_records_sources",
                    "type": "open_drawer",
                    "label": "Review source coverage",
                    "drawer": "sources",
                }
            ],
        }

    def _transaction_detail_surface(self) -> dict[str, Any]:
        rows = self._transaction_rows(
            merchant_contains="MITRE 10", classification="any", limit=1
        )
        snapshot = self.engine.get_snapshot()
        classification = rows[0]["classification"] if rows else "unresolved"
        return {
            "specVersion": "FinanceSurfaceSpec@1",
            "surfaceId": "surface_koru_mitre_detail",
            "surfaceType": "transaction_detail",
            "title": "Mitre 10 Hamilton",
            "subtitle": f"15 July · {classification} · source-linked",
            "freshness": snapshot["freshness"],
            "blocks": [
                {
                    "blockId": "block_mitre_context",
                    "type": "narrative",
                    "text": (
                        "This purchase keeps its source evidence and owner-supplied "
                        "classification context together."
                    ),
                    "tone": "neutral" if classification == "business" else "caution",
                },
                {
                    "blockId": "block_mitre_rows",
                    "type": "transaction_rows",
                    "rows": rows,
                    "totalMinor": sum(int(row["amountMinor"]) for row in rows),
                    "currency": "NZD",
                },
                {
                    "blockId": "block_mitre_sources",
                    "type": "source_list",
                    "sources": [
                        {
                            "sourceItemId": "src_koru_bank_csv_20260717",
                            "label": "Folio demo bank export — row 7",
                            "sourceType": "csv",
                            "receivedAt": "2026-07-17T07:59:00+12:00",
                            "status": "processed",
                        }
                    ],
                },
            ],
            "actions": [
                {
                    "actionId": "act_mitre_sources",
                    "type": "focus_source",
                    "label": "Inspect source row",
                    "sourceItemId": "src_koru_bank_csv_20260717",
                }
            ],
        }

    def _surface_for(self, surface_type: str) -> dict[str, Any]:
        snapshot = self.engine.get_snapshot()
        current = snapshot["currentSurface"]
        if current["surfaceType"] == surface_type:
            return dict(current)
        if surface_type == "living_brief":
            return living_brief_surface(
                totals=_finance_totals(snapshot),
                findings=snapshot["findings"],
                data_through=snapshot["freshness"]["dataThrough"],
            )
        if surface_type == "cash_scenario":
            return self.engine.get_cash_scenario_surface()
        if surface_type == "owner_pack":
            return self.engine.get_owner_pack_surface()
        if surface_type == "records_table":
            return self._records_surface()
        if surface_type == "transaction_detail":
            return self._transaction_detail_surface()
        if surface_type == "work_receipt":
            return dict(current)
        raise ValueError(f"surface outside the closed registry: {surface_type}")

    async def select_surface(self, action: ShowSurfaceAction) -> FinanceServiceResult:
        surface = self._surface_for(action.surface_type)
        current = self.engine.get_snapshot()
        if current["currentSurface"] != surface:
            occurred_at = _now().isoformat()
            snapshot_id = _stable_id("snap", action.action_id, occurred_at)
            with self.engine.store.transaction() as connection:
                self.engine.set_surface(connection, surface)
                self.engine.store_snapshot(
                    connection,
                    snapshot_id=snapshot_id,
                    occurred_at=occurred_at,
                )
        return FinanceServiceResult(
            action_id=action.action_id,
            kind=action.kind,
            status="completed",
            data={"surface": surface},
            evidence_ids=tuple(
                dict.fromkeys(
                    evidence_id
                    for block in surface["blocks"]
                    for evidence_id in block.get("evidenceIds", [])
                )
            ),
        )


class LocalRouteServices:
    """Local-only route implementation with deterministic fixture boundaries."""

    def __init__(
        self,
        database_path: str | Path,
        *,
        auto_seed: bool = True,
        akahu_adapter: AkahuReadOnlyAdapter | None = None,
        plaid_adapter: PlaidReadOnlyAdapter | None = None,
    ) -> None:
        self.store = SQLiteStore(database_path)
        self.engine = FinanceEngine(self.store)
        self.engine.initialise()
        self.working_understanding = WorkingUnderstandingRuntime(self.store)
        self.finance_core = FinanceCoreAdapter(self.engine)
        self.daily_close = DailyCloseService(self.engine)
        self.event_buffer = RunEventBuffer(retention=500)
        self.telegram = TelegramFixtureIngestor(
            TelegramConfig(allowed_chat_id=700001)
        )
        self.akahu = akahu_adapter or AkahuReadOnlyAdapter()
        self.plaid = plaid_adapter or PlaidReadOnlyAdapter()
        self.local_model = LMStudioAdapter(LMStudioConfig.from_env())
        self.cloud_model = OpenAIResponsesAdapter(OpenAIConfig.from_env())
        self.model_router = ModelModeRouter(self.local_model, self.cloud_model)
        self.receipts = SQLiteReceiptSink(self.store)
        self.current_mode = ModelMode.LOCAL
        self._lock = asyncio.Lock()
        self._compose_controller()
        if auto_seed:
            self._ensure_seeded()

    def _compose_controller(self) -> None:
        self.conversations = SQLiteConversationStore(self.store)
        self.controller = FinanceAgentController(
            finance_core=self.finance_core,
            conversations=self.conversations,
            harness=ModelHarness(self.model_router),
            receipt_sink=self.receipts,
            working_understanding=self.working_understanding,
        )

    def _initialise_frame(self) -> None:
        if self.store.current_dialogue_frame(WORKSPACE_ID, THREAD_ID) is not None:
            return
        self.conversations.save_frame(
            DialogueFrame(
                frame_id="frame_koru_current",
                workspace_id=WORKSPACE_ID,
                thread_id=THREAD_ID,
                updated_at=_now(),
                current_intent="morning_close_review",
                active_scenario_id="scenario_koru_laptop",
            )
        )

    def _ensure_seeded(self) -> None:
        try:
            self.engine.get_snapshot()
        except FinanceStateError:
            self.engine.reset_demo(DEMO_CSV)
            self._initialise_frame()
            identity = self.daily_close.identity()
            self.event_buffer.register_run(
                identity.run_id,
                resync_path=f"/v1/workspaces/{WORKSPACE_ID}/snapshot",
            )
            result = self.daily_close.run()
            self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)
            self._register_daily_close_events(result)
        else:
            self._initialise_frame()
            self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)
            mode = self.store.fetch_one(
                "SELECT model_mode FROM workspaces WHERE workspace_id = ?", (WORKSPACE_ID,)
            )
            if mode is not None:
                self.current_mode = ModelMode(str(mode["model_mode"]))

    def _register_daily_close_events(self, result: DailyCloseResult) -> None:
        run_id = result.run_id
        self.event_buffer.register_run(
            run_id, resync_path=f"/v1/workspaces/{WORKSPACE_ID}/snapshot"
        )
        if self.event_buffer.read(run_id):
            return
        job = self.store.fetch_one(
            "SELECT * FROM job_runs WHERE run_id = ?", (run_id,)
        )
        if job is None or not job["result_json"]:
            raise RuntimeError(f"Daily Close run has no committed result: {run_id}")
        stage_rows = self.store.fetch_all(
            "SELECT * FROM job_stage_runs WHERE run_id = ? ORDER BY sequence",
            (run_id,),
        )
        if len(stage_rows) != len(STAGES):
            raise RuntimeError(f"Daily Close run has incomplete stage history: {run_id}")
        stored_snapshot = self.store.fetch_one(
            "SELECT snapshot_json FROM workspace_snapshots WHERE snapshot_id = ?",
            (result.snapshot_id,),
        )
        snapshot = (
            json.loads(str(stored_snapshot["snapshot_json"]))
            if stored_snapshot is not None
            else self.workspace_snapshot_sync(WORKSPACE_ID)
        )
        close_turn = self.store.fetch_one(
            "SELECT * FROM conversation_turns WHERE turn_id = ?",
            (result.close_turn_id,),
        )
        if close_turn is None:
            raise RuntimeError(f"Daily Close run has no linked close message: {run_id}")
        sequence = 0

        def append(
            event_type: str,
            payload: Mapping[str, object],
            *,
            occurred_at: datetime,
        ) -> None:
            nonlocal sequence
            sequence += 1
            self.event_buffer.append(
                RunEvent.model_validate(
                    {
                        "eventId": _stable_id(
                            "streamevt", run_id, str(sequence), event_type
                        ),
                        "threadId": THREAD_ID,
                        "runId": run_id,
                        "sequence": sequence,
                        "occurredAt": occurred_at,
                        "type": event_type,
                        "payload": dict(payload),
                    }
                )
            )

        append(
            "run.started",
            {"mode": "local", "reason": "daily_close", "resumeFromSequence": None},
            occurred_at=datetime.fromisoformat(str(job["started_at"])),
        )
        for stage_row in stage_rows:
            started_at = datetime.fromisoformat(str(stage_row["started_at"]))
            completed_at = datetime.fromisoformat(
                str(stage_row["completed_at"] or stage_row["started_at"])
            )
            duration_ms = max(0, int((completed_at - started_at).total_seconds() * 1000))
            stage = str(stage_row["stage"])
            append("stage.started", {"stage": stage}, occurred_at=started_at)
            append(
                "stage.completed",
                {
                    "stage": stage,
                    "status": str(stage_row["status"]),
                    "durationMs": duration_ms,
                },
                occurred_at=completed_at,
            )
        close_occurred_at = datetime.fromisoformat(str(close_turn["occurred_at"]))
        append(
            "message.completed",
            {
                "turnId": str(close_turn["turn_id"]),
                "content": str(close_turn["content"])[:8000],
                "evidenceIds": json.loads(str(close_turn["evidence_ids_json"])),
            },
            occurred_at=close_occurred_at,
        )
        completed_at = datetime.fromisoformat(str(job["completed_at"]))
        append("state.snapshot", {"snapshot": snapshot}, occurred_at=completed_at)
        committed_receipt = str(job["result_json"])
        append(
            "receipt.committed",
            {
                "receiptType": "daily_close",
                "receiptId": str(job["receipt_id"]),
                "contentHash": hashlib.sha256(committed_receipt.encode()).hexdigest(),
                "evidenceIds": ["evd_koru_bank_csv"],
            },
            occurred_at=completed_at,
        )
        append(
            "run.completed",
            {
                "status": str(job["status"]),
                "durationMs": max(
                    0,
                    int(
                        (
                            completed_at - datetime.fromisoformat(str(job["started_at"]))
                        ).total_seconds()
                        * 1000
                    ),
                ),
                "snapshotId": result.snapshot_id,
                "receiptId": str(job["receipt_id"]),
            },
            occurred_at=completed_at,
        )

    def _persist_turns(
        self,
        *,
        turn_id: str,
        result: Any,
    ) -> None:
        occurred_at = _now().isoformat()
        agent_turn_id = _stable_id(
            "turn", result.run_id, "question" if result.question else "agent"
        )
        evidence_ids = list(result.work_receipt.evidence_ids)
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE conversation_turns SET evidence_ids_json = ?
                WHERE turn_id = ? AND workspace_id = ? AND thread_id = ?
                """,
                (
                    canonical_json(evidence_ids),
                    turn_id,
                    WORKSPACE_ID,
                    THREAD_ID,
                ),
            )
            connection.execute(
                """
                UPDATE conversation_turns SET evidence_ids_json = ?
                WHERE turn_id = ? AND workspace_id = ? AND thread_id = ?
                """,
                (
                    canonical_json(evidence_ids),
                    agent_turn_id,
                    WORKSPACE_ID,
                    THREAD_ID,
                ),
            )
            connection.execute(
                "UPDATE workspaces SET model_mode = ?, updated_at = ? WHERE workspace_id = ?",
                (self.current_mode.value, occurred_at, WORKSPACE_ID),
            )
            for receipt in result.model_receipts:
                contract = receipt.as_contract()
                connection.execute(
                    """
                    INSERT INTO model_runs(model_run_id, workspace_id, receipt_json, created_at)
                    VALUES (?, ?, ?, ?) ON CONFLICT(model_run_id) DO NOTHING
                    """,
                    (
                        receipt.receipt_id,
                        WORKSPACE_ID,
                        canonical_json(contract),
                        occurred_at,
                    ),
                )
            for receipt in result.egress_receipts:
                contract = receipt.as_contract()
                connection.execute(
                    """
                    INSERT INTO egress_receipts(
                        receipt_id, workspace_id, receipt_json, created_at
                    ) VALUES (?, ?, ?, ?) ON CONFLICT(receipt_id) DO NOTHING
                    """,
                    (
                        receipt.receipt_id,
                        WORKSPACE_ID,
                        canonical_json(contract),
                        occurred_at,
                    ),
                )

    def _register_turn_events(self, result: Any) -> None:
        run_id = result.run_id
        snapshot = self.workspace_snapshot_sync(WORKSPACE_ID)
        occurred_at = _now()
        evidence_ids = list(result.work_receipt.evidence_ids)
        agent_turn_id = _stable_id(
            "turn", run_id, "question" if result.question else "agent"
        )
        sequence = 0

        def append(event_type: str, payload: Mapping[str, object]) -> None:
            nonlocal sequence
            sequence += 1
            self.event_buffer.append(
                RunEvent.model_validate(
                    {
                        "eventId": _stable_id(
                            "streamevt", run_id, str(sequence), event_type
                        ),
                        "threadId": THREAD_ID,
                        "runId": run_id,
                        "sequence": sequence,
                        "occurredAt": occurred_at,
                        "type": event_type,
                        "payload": dict(payload),
                    }
                )
            )

        append(
            "run.started",
            {
                "mode": self.current_mode.value,
                "reason": "owner_turn",
                "resumeFromSequence": None,
            },
        )
        tool_names = {
            "query_summary",
            "query_transactions",
            "run_cash_scenario",
            "record_business_claim",
            "create_classification_rule",
            "undo_event",
            "prepare_owner_pack",
            "show_surface",
        }
        execution_results = result.execution.results if result.execution else ()
        for service_result in execution_results:
            if service_result.kind not in tool_names:
                continue
            append(
                "tool.started",
                {
                    "toolCallId": service_result.action_id,
                    "toolName": service_result.kind,
                },
            )
            append(
                "tool.completed",
                {
                    "toolCallId": service_result.action_id,
                    "toolName": service_result.kind,
                    "status": (
                        "failed_closed"
                        if service_result.status == "failed_closed"
                        else "completed"
                    ),
                    "durationMs": 0,
                    "evidenceIds": list(service_result.evidence_ids),
                },
            )
        append(
            "message.completed",
            {
                "turnId": agent_turn_id,
                "content": result.narrative,
                "evidenceIds": evidence_ids,
            },
        )
        real_surface = next(
            (
                service_result.data.get("surface")
                for service_result in execution_results
                if service_result.kind == "show_surface"
                and service_result.status in {"completed", "no_op"}
                and isinstance(service_result.data.get("surface"), Mapping)
            ),
            None,
        )
        if (
            isinstance(real_surface, Mapping)
            and real_surface.get("surfaceId")
            == snapshot["currentSurface"]["surfaceId"]
        ):
            append("surface.replace", {"surface": dict(real_surface)})
        append("state.snapshot", {"snapshot": snapshot})
        for receipt in result.model_receipts:
            contract = receipt.as_contract()
            append(
                "receipt.committed",
                {
                    "receiptType": "model",
                    "receiptId": receipt.receipt_id,
                    "contentHash": hashlib.sha256(
                        canonical_json(contract).encode()
                    ).hexdigest(),
                    "evidenceIds": [],
                },
            )
        for receipt in result.egress_receipts:
            contract = receipt.as_contract()
            append(
                "receipt.committed",
                {
                    "receiptType": "egress",
                    "receiptId": receipt.receipt_id,
                    "contentHash": hashlib.sha256(
                        canonical_json(contract).encode()
                    ).hexdigest(),
                    "evidenceIds": [],
                },
            )
        append(
            "receipt.committed",
            {
                "receiptType": "finance_event",
                "receiptId": result.work_receipt.receipt_id,
                "contentHash": result.work_receipt.content_hash,
                "evidenceIds": evidence_ids,
            },
        )
        append(
            "run.completed",
            {
                "status": "completed",
                "durationMs": 0,
                "snapshotId": snapshot["snapshotId"],
                "receiptId": result.work_receipt.receipt_id,
            },
        )

    async def health(self) -> Mapping[str, object]:
        database_ready = self.store.fetch_one("SELECT 1 AS ready") is not None
        return {
            "status": "ready" if database_ready else "degraded",
            "service": "standalone-finance-agent-api",
            "loopback": True,
            "database": "ready" if database_ready else "unavailable",
            "workspaceId": WORKSPACE_ID,
            "modelDiscoveryPath": "/v1/models/capabilities",
            "externalCalls": "disabled_by_default",
        }

    async def reset_demo(self, workspace_id: str) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID:
            raise KeyError(workspace_id)
        async with self._lock:
            imported = self.engine.reset_demo(DEMO_CSV)
            self.event_buffer.clear()
            self.telegram = TelegramFixtureIngestor(
                TelegramConfig(allowed_chat_id=700001)
            )
            self.current_mode = ModelMode.LOCAL
            self._compose_controller()
            self._initialise_frame()
            self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)
            return {
                "workspaceId": workspace_id,
                "status": "reset",
                "sourceItemId": "src_koru_bank_csv_20260717",
                "sourceSha256": imported.source_sha256,
                "rowCount": imported.row_count,
                "nextAction": "run_daily_close",
            }

    async def ingest_csv(
        self,
        *,
        workspace_id: str,
        filename: str,
        content: bytes,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID:
            raise KeyError(workspace_id)
        async with self._lock:
            with tempfile.NamedTemporaryFile(suffix=".csv") as value:
                value.write(content)
                value.flush()
                imported = self.engine.ingest_csv(
                    value.name,
                    label="Imported bank CSV",
                    mapping_version="koru_bank_csv@1",
                )
            self.working_understanding.ensure_current(workspace_id=workspace_id)
            return {
                "sourceItemId": imported.source_item_id,
                "status": "deduplicated" if imported.duplicate_import else "ingested",
                "sourceSha256": imported.source_sha256,
                "rowCount": imported.row_count,
            }

    async def ingest_akahu_fixture(
        self,
        *,
        payload: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        async with self._lock:
            result = self.engine.ingest_akahu_fixture(payload)
            self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)
            return {
                "sourceItemId": result.source_item_id,
                "status": result.status,
                "accountLabel": result.account_label,
                "syncedAt": result.synced_at,
                "rowCount": result.row_count,
                "sourceSha256": result.digest,
                "liveSyncAttempted": result.live_sync_attempted,
            }

    async def sync_akahu(
        self,
        *,
        start: str | None,
        end: str | None,
    ) -> Mapping[str, object]:
        """Fetch settled Akahu pages and commit only new exact-money rows."""

        start_date, end_date = _akahu_window(start, end)
        account_items: list[Mapping[str, object]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(AKAHU_MAX_PAGES):
            page = await self.akahu.list_accounts(cursor=cursor)
            account_items.extend(page.items)
            if len(account_items) > AKAHU_MAX_ITEMS:
                raise ConnectorError("Akahu account sync exceeded the local item limit")
            if page.next_cursor is None:
                break
            if page.next_cursor in seen_cursors:
                raise ConnectorError("Akahu account pagination repeated a cursor")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        else:
            raise ConnectorError("Akahu account sync exceeded the page limit")
        accounts = normalise_accounts(tuple(account_items))
        if not accounts:
            raise ConnectorError("Akahu returned no accounts")

        query_start, query_end = _akahu_query_window(start_date, end_date)
        transaction_items: list[Mapping[str, object]] = []
        cursor = None
        seen_cursors.clear()
        for _ in range(AKAHU_MAX_PAGES):
            page = await self.akahu.list_transactions(
                start=query_start,
                end=query_end,
                cursor=cursor,
                pending=False,
            )
            transaction_items.extend(page.items)
            if len(transaction_items) > AKAHU_MAX_ITEMS:
                raise ConnectorError("Akahu transaction sync exceeded the local item limit")
            if page.next_cursor is None:
                break
            if page.next_cursor in seen_cursors:
                raise ConnectorError("Akahu transaction pagination repeated a cursor")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        else:
            raise ConnectorError("Akahu transaction sync exceeded the page limit")
        transactions = normalise_transactions(tuple(transaction_items), accounts)

        synced_at = _now().isoformat()
        async with self._lock:
            with self.store.transaction() as connection:
                for account in accounts:
                    existing = connection.execute(
                        "SELECT workspace_id FROM accounts WHERE account_id = ?",
                        (account.account_id,),
                    ).fetchone()
                    if existing is not None and str(existing["workspace_id"]) != WORKSPACE_ID:
                        raise ConnectorError(
                            "Akahu account identity conflicts with another workspace"
                        )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO accounts(
                            account_id, workspace_id, name, currency, created_at
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            account.account_id,
                            WORKSPACE_ID,
                            f"Akahu · {account.label}",
                            account.currency,
                            synced_at,
                        ),
                    )
                existing_references = {
                    str(row["external_reference"])
                    for row in connection.execute(
                        """
                        SELECT external_reference FROM source_rows
                        WHERE mapping_version = ?
                        """,
                        (AKAHU_MAPPING_VERSION,),
                    )
                }
            new_transactions = tuple(
                transaction
                for transaction in transactions
                if transaction.external_reference not in existing_references
            )
            if not new_transactions:
                self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)
                return {
                    "sourceItemId": None,
                    "status": "no_new_transactions",
                    "sourceSha256": None,
                    "accountCount": len(accounts),
                    "transactionCount": len(transactions),
                    "rowCount": 0,
                    "window": {"start": start_date, "end": end_date},
                    "settledOnly": True,
                    "liveSyncAttempted": True,
                    "externalCallsMade": True,
                }

            content = _akahu_csv(new_transactions)
            with tempfile.NamedTemporaryFile(suffix=".csv") as value:
                value.write(content)
                value.flush()
                imported = self.engine.ingest_csv(
                    value.name,
                    label=(
                        "Akahu live settled transactions "
                        f"({start_date} to {end_date})"
                    ),
                    mapping_version=AKAHU_MAPPING_VERSION,
                    received_at=synced_at,
                )
            self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)
            return {
                "sourceItemId": imported.source_item_id,
                "status": "deduplicated" if imported.duplicate_import else "ingested",
                "sourceSha256": imported.source_sha256,
                "accountCount": len(accounts),
                "transactionCount": len(transactions),
                "rowCount": imported.row_count,
                "window": {"start": start_date, "end": end_date},
                "settledOnly": True,
                "liveSyncAttempted": True,
                "externalCallsMade": True,
            }

    async def ingest_plaid_fixture(
        self,
        *,
        payload: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        async with self._lock:
            result = self.engine.ingest_plaid_fixture(payload)
            self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)
            return {
                "sourceItemId": result.source_item_id,
                "status": result.status,
                "accountLabel": result.account_label,
                "syncedAt": result.synced_at,
                "rowCount": result.row_count,
                "sourceSha256": result.digest,
                "providerCurrency": result.currency,
                "liveSyncAttempted": result.live_sync_attempted,
            }

    async def create_plaid_link_token(self) -> Mapping[str, object]:
        token = await self.plaid.create_link_token()
        return {
            "linkToken": token,
            "environment": self.plaid.config.environment,
            "liveSyncAttempted": True,
            "externalCallsMade": True,
        }

    async def sync_plaid(
        self,
        *,
        public_token: str | None = None,
    ) -> Mapping[str, object]:
        """Record complete Plaid sync semantics without relabelling USD as NZD."""

        access_token = await self.plaid.resolve_access_token(public_token)
        account_items = await self.plaid.list_accounts(access_token=access_token)
        accounts = normalise_plaid_accounts(account_items)
        if not accounts:
            raise ConnectorError(
                "Plaid returned no accounts",
                code="provider_invalid_response",
                provider="plaid",
            )

        added_items: list[Mapping[str, object]] = []
        modified_items: list[Mapping[str, object]] = []
        removed_items: list[Mapping[str, object]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(PLAID_MAX_PAGES):
            page = await self.plaid.sync_transactions(
                access_token=access_token,
                cursor=cursor,
            )
            added_items.extend(page.added)
            modified_items.extend(page.modified)
            removed_items.extend(page.removed)
            total_items = len(added_items) + len(modified_items) + len(removed_items)
            if total_items > PLAID_MAX_ITEMS:
                raise ConnectorError(
                    "Plaid transaction sync exceeded the local item limit",
                    code="provider_item_limit",
                    provider="plaid",
                )
            if not page.has_more:
                break
            if page.next_cursor is None:
                raise ConnectorError(
                    "Plaid omitted a continuation cursor",
                    code="provider_invalid_response",
                    provider="plaid",
                )
            if page.next_cursor in seen_cursors:
                raise ConnectorError(
                    "Plaid transaction sync repeated a cursor",
                    code="provider_cursor_repeated",
                    provider="plaid",
                )
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        else:
            raise ConnectorError(
                "Plaid transaction sync exceeded the page limit",
                code="provider_page_limit",
                provider="plaid",
            )

        added = normalise_plaid_transactions(tuple(added_items), accounts)
        modified = normalise_plaid_transactions(tuple(modified_items), accounts)
        synced_at = _now().isoformat()
        async with self._lock:
            batch = record_plaid_event_batch(
                self.store,
                workspace_id=WORKSPACE_ID,
                accounts=accounts,
                added=added,
                modified=modified,
                removed=tuple(removed_items),
                synced_at=synced_at,
                mapping_version=PLAID_MAPPING_VERSION,
            )
            self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)

        if batch.event_count == 0:
            status = "no_new_transactions"
        else:
            status = "quarantined_currency_mismatch"
        primary = accounts[0]
        return {
            "sourceItemId": batch.source_item_id if batch.event_count else None,
            "status": status,
            "sourceSha256": batch.digest if batch.event_count else None,
            "accountCount": len(accounts),
            "transactionCount": len(added) + len(modified),
            "addedCount": batch.added_count,
            "modifiedCount": batch.modified_count,
            "removedCount": batch.removed_count,
            "rowCount": 0,
            "providerEventCount": batch.event_count,
            "providerCurrency": primary.currency,
            "ledgerCommitted": False,
            "quarantineReason": "workspace_currency_mismatch",
            "settledOnly": True,
            "liveSyncAttempted": True,
            "externalCallsMade": True,
        }

    async def ingest_telegram_fixture(
        self,
        *,
        update: Mapping[str, object],
        attachment_reference: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        async with self._lock:
            result = self.telegram.ingest(update, attachment_reference)
            if result.source is not None:
                with tempfile.NamedTemporaryFile(suffix=".json", mode="w+") as value:
                    json.dump(update, value, sort_keys=True, separators=(",", ":"))
                    value.flush()
                    self.engine.record_telegram_fixture_source(value.name)
                with self.store.transaction() as connection:
                    connection.execute(
                        """
                        UPDATE source_items SET status = 'processed'
                        WHERE source_item_id = ? AND workspace_id = ?
                        """,
                        (result.source.source_item_id, WORKSPACE_ID),
                    )
                self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)
            return {
                "sourceItemId": (
                    result.source.source_item_id
                    if result.source is not None
                    else "src_koru_telegram_910001"
                ),
                "status": result.status,
                "updateId": result.update_id,
                "liveSendAttempted": False,
            }

    async def enqueue_daily_close(
        self, *, workspace_id: str, idempotency_key: str | None
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID:
            raise KeyError(workspace_id)
        async with self._lock:
            identity = self.daily_close.identity(
                requested_idempotency_key=idempotency_key
            )
            self.event_buffer.register_run(
                identity.run_id,
                resync_path=f"/v1/workspaces/{WORKSPACE_ID}/snapshot",
            )
            result = self.daily_close.run(
                requested_idempotency_key=idempotency_key
            )
            self.working_understanding.ensure_current(workspace_id=workspace_id)
            self._register_daily_close_events(result)
            return {
                "runId": result.run_id,
                "receiptId": result.receipt_id,
                "snapshotId": result.snapshot_id,
                "status": result.status,
                "idempotencyKey": idempotency_key,
                "newFindings": result.new_findings,
                "newArtifacts": result.new_artifacts,
                "newOwnerMessages": result.new_owner_messages,
            }

    async def read_events(
        self, *, run_id: str, after_sequence: int
    ) -> tuple[RunEvent, ...]:
        return self.event_buffer.read(run_id, after_sequence=after_sequence)

    async def submit_turn(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        turn_id: str,
        content: str,
        mode: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or thread_id != THREAD_ID:
            raise KeyError("unknown workspace or thread")
        async with self._lock:
            self.current_mode = ModelMode(mode)
            run_id = _stable_id("run", thread_id, turn_id, content)
            existing = self.store.fetch_one(
                "SELECT content FROM conversation_turns WHERE turn_id = ? AND role = 'owner'",
                (turn_id,),
            )
            if existing is not None:
                if str(existing["content"]) != content:
                    raise ValueError("turnId is already bound to different content")
                receipt = self.store.fetch_one(
                    """
                    SELECT receipt_id FROM work_receipts
                    WHERE run_id = ? ORDER BY created_at DESC LIMIT 1
                    """,
                    (run_id,),
                )
                if receipt is not None:
                    self.working_understanding.ensure_current(workspace_id=workspace_id)
                    snapshot = self.workspace_snapshot_sync(workspace_id)
                    return {
                        "runId": run_id,
                        "status": "completed",
                        "question": (
                            snapshot["thread"]["activeQuestion"]["prompt"]
                            if snapshot["thread"]["activeQuestion"]
                            else None
                        ),
                        "planSource": "idempotent_replay",
                        "receiptId": str(receipt["receipt_id"]),
                        "snapshotId": snapshot["snapshotId"],
                    }
            self.event_buffer.register_run(
                run_id, resync_path=f"/v1/workspaces/{WORKSPACE_ID}/snapshot"
            )
            result = await self.controller.run_turn(
                TurnRequest(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    run_id=run_id,
                    turn_id=turn_id,
                    content=content,
                    mode=self.current_mode,
                )
            )
            self._persist_turns(turn_id=turn_id, result=result)
            self.working_understanding.record_committed_owner_turn(
                workspace_id=workspace_id,
                thread_id=thread_id,
                turn_id=turn_id,
            )
            self._register_turn_events(result)
            snapshot = self.workspace_snapshot_sync(workspace_id)
            return {
                "runId": run_id,
                "status": "completed" if result.question is None else "question",
                "question": result.question,
                "planSource": result.plan_source,
                "receiptId": result.work_receipt.receipt_id,
                "snapshotId": snapshot["snapshotId"],
            }

    def workspace_snapshot_sync(self, workspace_id: str) -> dict[str, Any]:
        if workspace_id != WORKSPACE_ID:
            raise KeyError(workspace_id)
        committed = self.engine.get_snapshot()
        with self.store.connect() as connection:
            snapshot = self.engine.build_snapshot(
                connection,
                snapshot_id=committed["snapshotId"],
            )
        frame = self.conversations.get_frame(THREAD_ID)
        snapshot["thread"]["activeQuestion"] = (
            {
                "questionId": frame.active_question.question_id,
                "prompt": frame.active_question.prompt,
                "askedAt": frame.active_question.asked_at.isoformat(),
            }
            if frame.active_question
            else None
        )
        snapshot["modelMode"] = self.current_mode.value
        return snapshot

    async def workspace_snapshot(self, workspace_id: str) -> Mapping[str, object]:
        return self.workspace_snapshot_sync(workspace_id)

    async def undo_event(
        self,
        *,
        event_id: str,
        request_id: str,
        actor: str,
        reason: str,
    ) -> Mapping[str, object]:
        if actor != "owner":
            raise ValueError("only the owner can request Undo")
        async with self._lock:
            result = self.engine.undo_event(
                event_id,
                request_id=request_id,
                reason=reason,
            )
            self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)
            return result

    async def artifact(self, artifact_id: str) -> ArtifactPayload:
        media_type, content, content_hash = self.engine.get_artifact(artifact_id)
        suffix = "pdf" if media_type == "application/pdf" else "html"
        return ArtifactPayload(
            content=content,
            media_type=media_type,
            filename=f"koru-studio-owner-pack.{suffix}",
            content_hash=content_hash,
        )

    async def model_capabilities(self) -> Mapping[str, object]:
        capabilities = await self.model_router.capabilities()
        modes = capabilities.get("modes")
        cloud = modes.get("cloud") if isinstance(modes, Mapping) else None
        cloud_status = cloud.get("status") if isinstance(cloud, Mapping) else None
        return {
            **capabilities,
            "selectedMode": self.current_mode.value,
            "privacy": {
                "local": "Finance computation and language stay on this Mac.",
                "hybrid": (
                    "Finance computation stays local; only policy-approved typed "
                    "projections may leave the device when cloud is configured."
                ),
                "cloud": (
                    "Raw source files remain local by default. Cloud language is "
                    "unavailable until explicitly configured outside this prototype."
                ),
            },
            "cloudCredentialState": (
                "configured"
                if cloud_status == AdapterStatus.READY.value
                else "absent"
            ),
            "externalCallsMade": False,
        }

    async def connection_capabilities(self) -> Mapping[str, object]:
        akahu = self.akahu.capability()
        akahu_configured = bool(akahu["configured"])
        plaid = self.plaid.capability()
        plaid_configured = bool(plaid["configured"])
        plaid_markets_value = plaid.get("markets")
        plaid_markets = (
            [str(market) for market in plaid_markets_value]
            if isinstance(plaid_markets_value, list)
            else ["US"]
        )
        return {
            "providers": {
                "demo": {
                    "status": "ready",
                    "mode": "sealed_fixture",
                    "markets": ["NZ"],
                },
                "csv": {
                    "status": "ready",
                    "mode": "local_file",
                    "markets": ["NZ"],
                },
                "akahu": {
                    "status": "configured" if akahu_configured else "unconfigured",
                    "mode": "read_only",
                    "markets": ["NZ"],
                    "fixtureAvailable": True,
                    "liveSyncPath": "/v1/connectors/akahu/sync",
                    "credentialSource": (
                        "process_environment" if akahu_configured else "absent"
                    ),
                    "detail": (
                        "Read-only live sync is configured for this process."
                        if akahu_configured
                        else (
                            "The sealed Akahu-shaped demo is available. Live reads require "
                            "process-injected Akahu app and user tokens."
                        )
                    ),
                },
                "plaid": {
                    "status": "configured" if plaid_configured else "unconfigured",
                    "mode": "read_only",
                    "markets": plaid_markets,
                    "supportsNewZealand": False,
                    "fixtureAvailable": True,
                    "environment": plaid.get("environment", "sandbox"),
                    "linkTokenPath": "/v1/connectors/plaid/link-token",
                    "liveSyncPath": "/v1/connectors/plaid/sync",
                    "credentialSource": (
                        "process_environment" if plaid_configured else "absent"
                    ),
                    "detail": (
                        "Read-only Plaid sandbox Link is configured for this process."
                        if plaid_configured
                        else (
                            "The sealed Plaid-shaped demo is available. Live sandbox Link "
                            "requires process-injected PLAID_CLIENT_ID and PLAID_SECRET. "
                            "Plaid does not support New Zealand banks."
                        )
                    ),
                },
            },
            "externalCallsMade": False,
        }

    async def working_understanding_diagnostics(
        self,
        *,
        workspace_id: str,
        run_id: str | None = None,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID:
            raise KeyError(workspace_id)
        return self.working_understanding.diagnostics(
            workspace_id=workspace_id,
            run_id=run_id,
        )

    async def aclose(self) -> None:
        await self.akahu.aclose()
        await self.plaid.aclose()
        await self.local_model.aclose()
        await self.cloud_model.aclose()


__all__ = [
    "DEMO_CSV",
    "DEMO_TELEGRAM",
    "DEMO_TELEGRAM_ATTACHMENT",
    "FinanceCoreAdapter",
    "LocalRouteServices",
]
