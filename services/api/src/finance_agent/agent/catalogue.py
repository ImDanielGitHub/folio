"""State- and intent-relevant action catalogues for small-model planning."""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from finance_agent.agent.plan import FinancePlan


class ControllerState(StrEnum):
    LOAD_CONTEXT = "LOAD_CONTEXT"
    COMPILE_PLAN = "COMPILE_PLAN"
    VALIDATE_PLAN = "VALIDATE_PLAN"
    EXECUTE_READS = "EXECUTE_READS"
    ASK_ONE_QUESTION = "ASK_ONE_QUESTION"
    EXECUTE_REVERSIBLE_WRITE = "EXECUTE_REVERSIBLE_WRITE"
    RECOMPUTE = "RECOMPUTE"
    SELECT_SURFACE = "SELECT_SURFACE"
    EXPLAIN = "EXPLAIN"
    COMMIT_RECEIPT = "COMMIT_RECEIPT"


class IntentClass(StrEnum):
    READ_SUMMARY = "read_summary"
    READ_TRANSACTIONS = "read_transactions"
    SCENARIO = "scenario"
    CORRECTION = "correction"
    UNDO = "undo"
    OWNER_PACK = "owner_pack"
    STOP_SYNTHESIS = "stop_synthesis"
    UNKNOWN = "unknown"


ACTION_CATALOGUE: Final[dict[IntentClass, frozenset[str]]] = {
    IntentClass.READ_SUMMARY: frozenset({"query_summary", "show_surface"}),
    IntentClass.READ_TRANSACTIONS: frozenset({"query_transactions", "show_surface"}),
    IntentClass.SCENARIO: frozenset({"run_cash_scenario", "show_surface"}),
    IntentClass.CORRECTION: frozenset(
        {"record_business_claim", "create_classification_rule", "show_surface"}
    ),
    IntentClass.UNDO: frozenset({"undo_event", "show_surface"}),
    IntentClass.OWNER_PACK: frozenset({"prepare_owner_pack", "show_surface"}),
    IntentClass.STOP_SYNTHESIS: frozenset({"show_surface"}),
    IntentClass.UNKNOWN: frozenset({"query_summary", "show_surface"}),
}

WRITE_KINDS: Final[frozenset[str]] = frozenset(
    {"record_business_claim", "create_classification_rule", "undo_event"}
)


class PlanValidationError(ValueError):
    """A model plan violated product-owned execution policy."""


def validate_plan_for_intent(
    plan: FinancePlan,
    *,
    intent_class: IntentClass,
    thread_id: str,
    run_id: str,
) -> FinancePlan:
    """Validate execution authority after structural Pydantic validation."""

    if plan.thread_id != thread_id or plan.run_id != run_id:
        raise PlanValidationError("plan threadId/runId does not match the active run")
    allowed = ACTION_CATALOGUE[intent_class]
    unknown = [kind for kind in plan.action_kinds if kind not in allowed]
    if unknown:
        raise PlanValidationError(
            f"action catalogue violation for {intent_class.value}: {', '.join(unknown)}"
        )
    write_count = sum(kind in WRITE_KINDS for kind in plan.action_kinds)
    if write_count > 2:
        raise PlanValidationError("a plan may contain at most two reversible write actions")
    if "undo_event" in plan.action_kinds and len(plan.action_kinds) > 2:
        raise PlanValidationError("Undo may only be paired with a receipt surface")
    return plan


def flat_plan_schema(allowed_kinds: frozenset[str]) -> dict[str, object]:
    """Return the intentionally flat schema sent to weak/local models.

    The canonical Pydantic contract remains the final authority. Optional action
    fields keep this generation schema flat and avoid nested oneOf/$ref trees that
    smaller models frequently mishandle.
    """

    action_properties: dict[str, object] = {
        "actionId": {"type": "string", "maxLength": 96},
        "kind": {"type": "string", "enum": sorted(allowed_kinds)},
        "window": {
            "type": "string",
            "enum": ["current", "last_30_days", "forecast_30_days"],
        },
        "merchantContains": {"type": ["string", "null"], "maxLength": 100},
        "classification": {
            "type": "string",
            "enum": ["any", "business", "personal", "unresolved", "transfer"],
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        "scenarioId": {"type": "string", "maxLength": 96},
        "plannedAmountMinor": {"type": "integer"},
        "currency": {"type": "string", "enum": ["NZD"]},
        "plannedDate": {"type": "string", "format": "date"},
        "claimType": {
            "type": "string",
            "enum": [
                "business_context",
                "classification_instruction",
                "planned_expense",
                "reserve_policy",
            ],
        },
        "statement": {"type": "string", "maxLength": 1000},
        "effectiveDate": {"type": "string", "format": "date"},
        "maximumAmountMinor": {"type": "integer", "minimum": 0},
        "targetClassification": {"type": "string", "enum": ["business"]},
        "targetCategory": {"type": "string", "maxLength": 80},
        "effectiveFrom": {"type": "string", "format": "date"},
        "targetEventId": {"type": "string", "maxLength": 96},
        "format": {"type": "string", "enum": ["html", "pdf", "html_and_pdf"]},
        "surfaceType": {
            "type": "string",
            "enum": [
                "living_brief",
                "transaction_detail",
                "cash_scenario",
                "records_table",
                "owner_pack",
                "work_receipt",
            ],
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["planVersion", "planId", "threadId", "runId", "intent", "actions"],
        "properties": {
            "planVersion": {"type": "string", "enum": ["FinancePlan@1"]},
            "planId": {"type": "string", "maxLength": 96},
            "threadId": {"type": "string", "maxLength": 96},
            "runId": {"type": "string", "maxLength": 96},
            "intent": {"type": "string", "maxLength": 240},
            "actions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["actionId", "kind"],
                    "properties": action_properties,
                },
            },
        },
    }
