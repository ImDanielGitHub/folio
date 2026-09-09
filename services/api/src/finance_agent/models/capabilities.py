"""Fixture-measured task tiers for local models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from finance_agent.agent.parser import FinancePlanParser, PlanParseError
from finance_agent.models.base import CapabilityCard


class CapabilityTierProbe:
    """Grade plan outputs by behaviour, never by model name or parameter count."""

    REQUIRED_TASKS = ("single_read", "multi_read", "reversible_rule")

    def __init__(self, parser: FinancePlanParser | None = None) -> None:
        self.parser = parser or FinancePlanParser()

    def grade(self, outputs: Mapping[str, str], card: CapabilityCard) -> CapabilityCard:
        passed: set[str] = set()
        for task in self.REQUIRED_TASKS:
            output = outputs.get(task)
            if output is None:
                continue
            try:
                plan = self.parser.parse(output)
            except PlanParseError:
                continue
            kinds = set(plan.action_kinds)
            passed_single = task == "single_read" and bool(
                kinds & {"query_summary", "query_transactions"}
            )
            passed_multi = (
                task == "multi_read"
                and len(plan.actions) >= 2
                and not kinds
                & {"record_business_claim", "create_classification_rule", "undo_event"}
            )
            passed_rule = task == "reversible_rule" and {
                "record_business_claim",
                "create_classification_rule",
            }.issubset(kinds)
            if passed_single or passed_multi or passed_rule:
                passed.add(task)
        tier = 0
        if "single_read" in passed:
            tier = 1
        if {"single_read", "multi_read"}.issubset(passed):
            tier = 2
        if set(self.REQUIRED_TASKS).issubset(passed):
            tier = 3
        return replace(card, tier=tier, tier_measured=True)
