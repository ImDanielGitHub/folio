"""Narrow public boundaries between the controller and deterministic services."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

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


@dataclass(frozen=True, slots=True)
class FinanceContext:
    workspace_id: str
    thread_id: str
    current_surface_type: str
    projection: Mapping[str, object]
    unresolved_merchant: str | None = None
    unresolved_date: str | None = None
    latest_undoable_event_id: str | None = None
    scenario_id: str | None = None
    scenario_amount_minor: int | None = None
    scenario_date: str | None = None


@dataclass(frozen=True, slots=True)
class FinanceServiceResult:
    action_id: str
    kind: str
    status: str
    data: Mapping[str, object] = field(default_factory=dict)
    evidence_ids: tuple[str, ...] = ()
    event_id: str | None = None


class FinanceCorePort(Protocol):
    """Task 1 adapter target.

    The write boundary receives only validated scopes. It resolves affected
    transaction IDs and commits all write actions atomically.
    """

    async def load_context(self, workspace_id: str, thread_id: str) -> FinanceContext: ...

    async def query_summary(self, action: QuerySummaryAction) -> FinanceServiceResult: ...

    async def query_transactions(
        self, action: QueryTransactionsAction
    ) -> FinanceServiceResult: ...

    async def run_cash_scenario(
        self, action: RunCashScenarioAction
    ) -> FinanceServiceResult: ...

    async def execute_reversible_writes(
        self,
        actions: Sequence[WriteAction],
        *,
        source_turn_id: str,
    ) -> tuple[FinanceServiceResult, ...]: ...

    async def recompute(self, event_ids: Sequence[str]) -> FinanceServiceResult: ...

    async def prepare_owner_pack(
        self, action: PrepareOwnerPackAction
    ) -> FinanceServiceResult: ...

    async def select_surface(self, action: ShowSurfaceAction) -> FinanceServiceResult: ...


class ClaimsPort(Protocol):
    async def record_claim(self, action: RecordBusinessClaimAction, source_turn_id: str) -> str: ...


class RulesPort(Protocol):
    async def create_rule(
        self, action: CreateClassificationRuleAction, source_turn_id: str
    ) -> str: ...


class UndoPort(Protocol):
    async def undo(self, action: UndoEventAction, source_turn_id: str) -> str: ...
