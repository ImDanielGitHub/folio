"""Closed FinancePlan contract and product-owned validation rules."""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ID_PATTERN = r"^[a-z][a-z0-9]{1,15}_[a-z0-9][a-z0-9_]{2,95}$"


class ContractModel(BaseModel):
    """Base class for contract-shaped objects that fail on unknown fields."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class QuerySummaryAction(ContractModel):
    action_id: str = Field(alias="actionId", pattern=ID_PATTERN)
    kind: Literal["query_summary"]
    window: Literal["current", "last_30_days", "forecast_30_days"]


class QueryTransactionsAction(ContractModel):
    action_id: str = Field(alias="actionId", pattern=ID_PATTERN)
    kind: Literal["query_transactions"]
    merchant_contains: str | None = Field(alias="merchantContains", max_length=100)
    classification: Literal["any", "business", "personal", "unresolved", "transfer"]
    limit: int = Field(ge=1, le=100)


class RunCashScenarioAction(ContractModel):
    action_id: str = Field(alias="actionId", pattern=ID_PATTERN)
    kind: Literal["run_cash_scenario"]
    scenario_id: str = Field(alias="scenarioId")
    planned_amount_minor: int = Field(alias="plannedAmountMinor")
    currency: Literal["NZD"]
    planned_date: date = Field(alias="plannedDate")


class RecordBusinessClaimAction(ContractModel):
    action_id: str = Field(alias="actionId", pattern=ID_PATTERN)
    kind: Literal["record_business_claim"]
    claim_type: Literal[
        "business_context", "classification_instruction", "planned_expense", "reserve_policy"
    ] = Field(alias="claimType")
    statement: str = Field(min_length=1, max_length=1000)
    effective_date: date = Field(alias="effectiveDate")


class CreateClassificationRuleAction(ContractModel):
    """Rule scope only; affected transaction IDs are intentionally impossible here."""

    action_id: str = Field(alias="actionId", pattern=ID_PATTERN)
    kind: Literal["create_classification_rule"]
    merchant_contains: str = Field(alias="merchantContains", min_length=1, max_length=100)
    maximum_amount_minor: int = Field(alias="maximumAmountMinor", ge=0)
    currency: Literal["NZD"]
    target_classification: Literal["business"] = Field(alias="targetClassification")
    target_category: str = Field(alias="targetCategory", min_length=1, max_length=80)
    effective_from: date = Field(alias="effectiveFrom")


class UndoEventAction(ContractModel):
    action_id: str = Field(alias="actionId", pattern=ID_PATTERN)
    kind: Literal["undo_event"]
    target_event_id: str = Field(alias="targetEventId")


class PrepareOwnerPackAction(ContractModel):
    action_id: str = Field(alias="actionId", pattern=ID_PATTERN)
    kind: Literal["prepare_owner_pack"]
    format: Literal["html", "pdf", "html_and_pdf"]


class ShowSurfaceAction(ContractModel):
    action_id: str = Field(alias="actionId", pattern=ID_PATTERN)
    kind: Literal["show_surface"]
    surface_type: Literal[
        "living_brief",
        "transaction_detail",
        "cash_scenario",
        "records_table",
        "owner_pack",
        "work_receipt",
    ] = Field(alias="surfaceType")


type PlanAction = Annotated[
    QuerySummaryAction
    | QueryTransactionsAction
    | RunCashScenarioAction
    | RecordBusinessClaimAction
    | CreateClassificationRuleAction
    | UndoEventAction
    | PrepareOwnerPackAction
    | ShowSurfaceAction,
    Field(discriminator="kind"),
]

type ReadAction = QuerySummaryAction | QueryTransactionsAction | RunCashScenarioAction
type WriteAction = (
    RecordBusinessClaimAction | CreateClassificationRuleAction | UndoEventAction
)


class FinancePlan(ContractModel):
    plan_version: Literal["FinancePlan@1"] = Field(alias="planVersion")
    plan_id: str = Field(alias="planId", pattern=ID_PATTERN)
    thread_id: str = Field(alias="threadId")
    run_id: str = Field(alias="runId")
    intent: str = Field(min_length=1, max_length=240)
    actions: list[PlanAction] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def action_ids_are_unique(self) -> FinancePlan:
        action_ids = [action.action_id for action in self.actions]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("FinancePlan actionId values must be unique")
        return self

    @property
    def action_kinds(self) -> tuple[str, ...]:
        return tuple(action.kind for action in self.actions)

    def as_contract(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True)
