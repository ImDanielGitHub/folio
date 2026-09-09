"""Deterministic FinancePlan execution over the public finance-core port."""

from __future__ import annotations

from dataclasses import dataclass

from finance_agent.agent.catalogue import ControllerState
from finance_agent.agent.plan import (
    CreateClassificationRuleAction,
    FinancePlan,
    PrepareOwnerPackAction,
    QuerySummaryAction,
    QueryTransactionsAction,
    RecordBusinessClaimAction,
    RunCashScenarioAction,
    ShowSurfaceAction,
    UndoEventAction,
)
from finance_agent.agent.ports import FinanceCorePort, FinanceServiceResult


class ExecutionFailedClosed(RuntimeError):
    """A deterministic service rejected an action; remaining actions were stopped."""


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    results: tuple[FinanceServiceResult, ...]
    state_trace: tuple[ControllerState, ...]
    event_ids: tuple[str, ...]


class FinancePlanExecutor:
    def __init__(self, finance_core: FinanceCorePort) -> None:
        self.finance_core = finance_core

    async def execute(self, plan: FinancePlan, *, source_turn_id: str) -> ExecutionReceipt:
        results: list[FinanceServiceResult] = []
        trace: list[ControllerState] = [ControllerState.EXECUTE_READS]

        for action in plan.actions:
            result: FinanceServiceResult | None = None
            if isinstance(action, QuerySummaryAction):
                result = await self.finance_core.query_summary(action)
            elif isinstance(action, QueryTransactionsAction):
                result = await self.finance_core.query_transactions(action)
            elif isinstance(action, RunCashScenarioAction):
                result = await self.finance_core.run_cash_scenario(action)
            if result is not None:
                self._accept(result)
                results.append(result)

        write_actions = tuple(
            action
            for action in plan.actions
            if isinstance(
                action,
                (RecordBusinessClaimAction, CreateClassificationRuleAction, UndoEventAction),
            )
        )
        if write_actions:
            trace.append(ControllerState.EXECUTE_REVERSIBLE_WRITE)
            write_results = await self.finance_core.execute_reversible_writes(
                write_actions, source_turn_id=source_turn_id
            )
            for result in write_results:
                self._accept(result)
                results.append(result)

        trace.append(ControllerState.RECOMPUTE)
        event_ids = tuple(result.event_id for result in results if result.event_id)
        recompute = await self.finance_core.recompute(event_ids)
        self._accept(recompute)
        results.append(recompute)

        for action in plan.actions:
            if isinstance(action, PrepareOwnerPackAction):
                result = await self.finance_core.prepare_owner_pack(action)
                self._accept(result)
                results.append(result)

        trace.append(ControllerState.SELECT_SURFACE)
        for action in plan.actions:
            if isinstance(action, ShowSurfaceAction):
                result = await self.finance_core.select_surface(action)
                self._accept(result)
                results.append(result)

        return ExecutionReceipt(
            results=tuple(results),
            state_trace=tuple(trace),
            event_ids=event_ids,
        )

    @staticmethod
    def _accept(result: FinanceServiceResult) -> None:
        if result.status not in {"completed", "no_op"}:
            raise ExecutionFailedClosed(
                f"{result.kind} failed closed with status {result.status}"
            )
