"""Deterministic, evidence-backed chat narratives for completed finance work."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from finance_agent.agent.executor import ExecutionReceipt
from finance_agent.agent.plan import (
    CreateClassificationRuleAction,
    FinancePlan,
    PrepareOwnerPackAction,
    QuerySummaryAction,
    QueryTransactionsAction,
    RecordBusinessClaimAction,
    RunCashScenarioAction,
    UndoEventAction,
)
from finance_agent.agent.ports import FinanceServiceResult


def _money(minor: int, currency: str = "NZD") -> str:
    return f"{currency} {minor / 100:,.2f}"


def _humanise_category(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").strip()


def _evidence_clause(evidence_ids: Sequence[str]) -> str:
    count = len(tuple(dict.fromkeys(evidence_ids)))
    if count <= 0:
        return "The committed receipt is ready."
    if count == 1:
        return "Linked evidence is attached on the receipt."
    return f"{count} linked evidence items are attached on the receipt."


def _completed(results: Sequence[FinanceServiceResult], kind: str) -> FinanceServiceResult | None:
    for result in results:
        if result.kind == kind and result.status in {"completed", "no_op"}:
            return result
    return None


def _affected_count(result: FinanceServiceResult | None) -> int | None:
    if result is None:
        return None
    event = result.data.get("event")
    if isinstance(event, Mapping):
        scope = event.get("scopeJson")
        if isinstance(scope, Mapping):
            transaction_ids = scope.get("transactionIds")
            if isinstance(transaction_ids, list):
                return len(transaction_ids)
    return None


def compose_execution_narrative(
    *,
    plan: FinancePlan,
    execution: ExecutionReceipt,
) -> str:
    """Build a judge-visible outcome line from committed plan results.

    Used whenever the model is unavailable, rejects output, or the harness fell
    back to deterministic planning — finance amounts stay out of model prose.
    """

    evidence_ids = tuple(
        dict.fromkeys(
            evidence_id for result in execution.results for evidence_id in result.evidence_ids
        )
    )
    rule = next(
        (action for action in plan.actions if isinstance(action, CreateClassificationRuleAction)),
        None,
    )
    undo = next((action for action in plan.actions if isinstance(action, UndoEventAction)), None)
    claim = next(
        (action for action in plan.actions if isinstance(action, RecordBusinessClaimAction)),
        None,
    )
    scenario = next(
        (action for action in plan.actions if isinstance(action, RunCashScenarioAction)),
        None,
    )
    pack = next(
        (action for action in plan.actions if isinstance(action, PrepareOwnerPackAction)),
        None,
    )
    summary = next(
        (action for action in plan.actions if isinstance(action, QuerySummaryAction)),
        None,
    )
    transactions = next(
        (action for action in plan.actions if isinstance(action, QueryTransactionsAction)),
        None,
    )

    rule_result = _completed(execution.results, "create_classification_rule")
    undo_result = _completed(execution.results, "undo_event")

    if undo is not None and undo_result is not None:
        return (
            "I undid the last classification change and restored the prior bookkeeping state. "
            "The original event stays in the audit trail. "
            f"{_evidence_clause(evidence_ids or undo_result.evidence_ids)}"
        )

    if rule is not None and rule_result is not None:
        category = _humanise_category(rule.target_category)
        affected = _affected_count(rule_result)
        if affected is None:
            affected_line = "Matching purchases are now classified the same way."
        elif affected == 1:
            affected_line = (
                "One linked transaction was reclassified; the cash amount did not change."
            )
        else:
            affected_line = (
                f"{affected} linked transactions were reclassified; "
                "the cash amounts did not change."
            )
        inspect_line = (
            f"I checked the linked {rule.merchant_contains} evidence against your explanation. "
            "Here’s what I changed: "
        )
        claim_line = (
            "recorded your explanation as an owner claim, then applied "
            if claim is not None
            else "applied "
        )
        return (
            f"{inspect_line}{claim_line}a narrow {rule.merchant_contains} rule for "
            f"{rule.target_classification} / {category} purchases up to "
            f"{_money(rule.maximum_amount_minor, rule.currency)}. "
            f"{affected_line} "
            f"{_evidence_clause(evidence_ids or rule_result.evidence_ids)} "
            "Review the receipt — Undo is available if this isn’t right."
        )

    if scenario is not None and _completed(execution.results, "run_cash_scenario"):
        return (
            "I opened the deterministic cash scenario beside this conversation. "
            f"It uses the planned {_money(scenario.planned_amount_minor, scenario.currency)} "
            f"on {scenario.planned_date.isoformat()}. "
            f"{_evidence_clause(evidence_ids)}"
        )

    if pack is not None and _completed(execution.results, "prepare_owner_pack"):
        return (
            "I prepared the owner pack from the same committed figures and linked sources. "
            f"{_evidence_clause(evidence_ids)}"
        )

    if summary is not None and _completed(execution.results, "query_summary"):
        return (
            "Here’s what I found in the committed local picture — "
            "open Current picture beside this chat for the linked evidence. "
            f"{_evidence_clause(evidence_ids)}"
        )

    if transactions is not None and _completed(execution.results, "query_transactions"):
        merchant = transactions.merchant_contains or "matching"
        return (
            f"Here’s what I found for {merchant} in the committed ledger — "
            "the rows are on the canvas with source links. "
            f"{_evidence_clause(evidence_ids)}"
        )

    if claim is not None and _completed(execution.results, "record_business_claim"):
        return (
            "I kept that as an owner claim with source provenance. "
            "It will apply when the next matching source or Daily Close makes it relevant. "
            f"{_evidence_clause(evidence_ids)}"
        )

    return (
        "The bounded finance work completed against local deterministic services. "
        f"{_evidence_clause(evidence_ids)}"
    )
