"""Deterministic, read-bounded routing when a weak model cannot compile a plan."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from finance_agent.agent.catalogue import IntentClass
from finance_agent.agent.plan import FinancePlan
from finance_agent.agent.ports import FinanceContext

_STOP_RE = re.compile(
    r"\b(stop|pause|leave it there|that(?:'s| is) enough|done for now|synthesi[sz]e)\b",
    re.IGNORECASE,
)
_UNDO_RE = re.compile(r"\b(undo|revert|put (?:it|that) back|roll back)\b", re.IGNORECASE)
_SCENARIO_RE = re.compile(
    r"\b(scenario|what if|forecast|cash|reserve|laptop|afford|defer)\b", re.IGNORECASE
)
_CORRECTION_RE = re.compile(
    r"\b(business|client|fit[- ]?out|classif|categor|merchant rule|was for)\b",
    re.IGNORECASE,
)
_PACK_RE = re.compile(r"\b(owner pack|working papers|report|pdf|export)\b", re.IGNORECASE)
_TRANSACTIONS_RE = re.compile(
    r"\b(transaction|charge|merchant|purchase|spent|expense row)\b", re.IGNORECASE
)
_SUMMARY_RE = re.compile(r"\b(summary|balance|income|expenses?|morning close)\b", re.IGNORECASE)
_LIMIT_RE = re.compile(
    r"(?:under|below|less than|up to|maximum(?: of)?)\s*(?:NZD|\$)?\s*([0-9,]+(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class FallbackDecision:
    intent_class: IntentClass
    plan: FinancePlan | None
    question: str | None = None


def classify_intent(content: str) -> IntentClass:
    if _STOP_RE.search(content):
        return IntentClass.STOP_SYNTHESIS
    if _UNDO_RE.search(content):
        return IntentClass.UNDO
    if _PACK_RE.search(content):
        return IntentClass.OWNER_PACK
    if _CORRECTION_RE.search(content):
        return IntentClass.CORRECTION
    if _SCENARIO_RE.search(content):
        return IntentClass.SCENARIO
    if _TRANSACTIONS_RE.search(content):
        return IntentClass.READ_TRANSACTIONS
    if _SUMMARY_RE.search(content):
        return IntentClass.READ_SUMMARY
    return IntentClass.UNKNOWN


def _stable_suffix(run_id: str, content: str) -> str:
    return hashlib.sha256(f"{run_id}\0{content}".encode()).hexdigest()[:12]


def _maximum_minor(content: str) -> int | None:
    match = _LIMIT_RE.search(content)
    if not match:
        return None
    try:
        return int(Decimal(match.group(1).replace(",", "")) * 100)
    except (InvalidOperation, ValueError):
        return None


def _effective_date(context: FinanceContext) -> date:
    if context.unresolved_date:
        return date.fromisoformat(context.unresolved_date)
    data_through = context.projection.get("dataThrough")
    if isinstance(data_through, str) and len(data_through) >= 10:
        return date.fromisoformat(data_through[:10])
    return date.today()


def _merchant(content: str, context: FinanceContext) -> str | None:
    if re.search(r"\bmitre\s*10\b", content, re.IGNORECASE):
        return "MITRE 10"
    return context.unresolved_merchant


def compile_fallback_plan(
    *,
    content: str,
    context: FinanceContext,
    thread_id: str,
    run_id: str,
) -> FallbackDecision:
    """Compile only high-confidence demo/read intents; otherwise ask one question."""

    intent_class = classify_intent(content)
    suffix = _stable_suffix(run_id, content)
    common = {
        "planVersion": "FinancePlan@1",
        "planId": f"plan_fallback_{suffix}",
        "threadId": thread_id,
        "runId": run_id,
    }
    actions: list[dict[str, object]]
    intent: str

    if intent_class is IntentClass.CORRECTION:
        merchant = _merchant(content, context)
        maximum_minor = _maximum_minor(content)
        if merchant is None:
            return FallbackDecision(
                intent_class,
                None,
                "Which merchant or transaction should this correction apply to?",
            )
        if maximum_minor is None:
            return FallbackDecision(
                intent_class,
                None,
                f"What maximum amount should the {merchant} rule apply below?",
            )
        effective = _effective_date(context).isoformat()
        category = (
            "client_fit_out_materials"
            if re.search(r"fit[- ]?out|materials?", content, re.IGNORECASE)
            else "business_expense"
        )
        intent = "Record the owner's scoped business correction and show its receipt."
        actions = [
            {
                "actionId": f"action_claim_{suffix}",
                "kind": "record_business_claim",
                "claimType": "classification_instruction",
                "statement": content[:1000],
                "effectiveDate": effective,
            },
            {
                "actionId": f"action_rule_{suffix}",
                "kind": "create_classification_rule",
                "merchantContains": merchant,
                "maximumAmountMinor": maximum_minor,
                "currency": "NZD",
                "targetClassification": "business",
                "targetCategory": category,
                "effectiveFrom": effective,
            },
            {
                "actionId": f"action_surface_{suffix}",
                "kind": "show_surface",
                "surfaceType": "work_receipt",
            },
        ]
    elif intent_class is IntentClass.UNDO:
        if context.latest_undoable_event_id is None:
            return FallbackDecision(
                intent_class,
                None,
                "Which recorded change would you like me to undo?",
            )
        intent = "Undo the latest identified reversible finance event."
        actions = [
            {
                "actionId": f"action_undo_{suffix}",
                "kind": "undo_event",
                "targetEventId": context.latest_undoable_event_id,
            },
            {
                "actionId": f"action_surface_{suffix}",
                "kind": "show_surface",
                "surfaceType": "work_receipt",
            },
        ]
    elif intent_class is IntentClass.SCENARIO:
        if not (context.scenario_id and context.scenario_amount_minor and context.scenario_date):
            return FallbackDecision(
                intent_class,
                None,
                "What planned amount and date should I use for the cash scenario?",
            )
        intent = "Run the current typed cash scenario and show its deterministic surface."
        actions = [
            {
                "actionId": f"action_scenario_{suffix}",
                "kind": "run_cash_scenario",
                "scenarioId": context.scenario_id,
                "plannedAmountMinor": context.scenario_amount_minor,
                "currency": "NZD",
                "plannedDate": context.scenario_date,
            },
            {
                "actionId": f"action_surface_{suffix}",
                "kind": "show_surface",
                "surfaceType": "cash_scenario",
            },
        ]
    elif intent_class is IntentClass.OWNER_PACK:
        intent = "Prepare the deterministic owner pack and show it."
        actions = [
            {
                "actionId": f"action_pack_{suffix}",
                "kind": "prepare_owner_pack",
                "format": "html_and_pdf",
            },
            {
                "actionId": f"action_surface_{suffix}",
                "kind": "show_surface",
                "surfaceType": "owner_pack",
            },
        ]
    elif intent_class is IntentClass.READ_TRANSACTIONS:
        intent = "Query a bounded transaction projection."
        actions = [
            {
                "actionId": f"action_query_{suffix}",
                "kind": "query_transactions",
                "merchantContains": _merchant(content, context),
                "classification": "any",
                "limit": 25,
            },
            {
                "actionId": f"action_surface_{suffix}",
                "kind": "show_surface",
                "surfaceType": "records_table",
            },
        ]
    elif intent_class is IntentClass.STOP_SYNTHESIS:
        intent = "Stop the inquiry and preserve the current confirmed state."
        actions = [
            {
                "actionId": f"action_surface_{suffix}",
                "kind": "show_surface",
                "surfaceType": context.current_surface_type,
            }
        ]
    else:
        resolved_class = (
            IntentClass.READ_SUMMARY if intent_class is IntentClass.UNKNOWN else intent_class
        )
        intent = "Read the current deterministic summary without mutation."
        actions = [
            {
                "actionId": f"action_summary_{suffix}",
                "kind": "query_summary",
                "window": "current",
            },
            {
                "actionId": f"action_surface_{suffix}",
                "kind": "show_surface",
                "surfaceType": "living_brief",
            },
        ]
        intent_class = resolved_class

    plan = FinancePlan.model_validate({**common, "intent": intent, "actions": actions})
    return FallbackDecision(intent_class, plan)
