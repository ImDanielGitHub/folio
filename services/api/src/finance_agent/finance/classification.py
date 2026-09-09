"""Deterministic classification, rule precedence, duplicates, and totals."""

from __future__ import annotations

from collections.abc import Iterable

from .domain import ClassificationDecision, ClassificationRule, FinanceTotals, Transaction


def normalise_merchant(value: str) -> str:
    return " ".join(value.upper().split())


def rule_matches(rule: ClassificationRule, transaction: Transaction) -> bool:
    """Match a rule using expense magnitude, never signed-value comparison."""

    return (
        transaction.status == "posted"
        and transaction.currency == rule.currency
        and transaction.occurred_on >= rule.effective_from
        and rule.merchant_contains.upper() in normalise_merchant(transaction.description)
        and transaction.amount_minor < 0
        and transaction.expense_magnitude_minor <= rule.maximum_amount_minor
    )


def choose_rule(
    transaction: Transaction,
    rules: Iterable[ClassificationRule],
) -> ClassificationRule | None:
    """Explicit rules win by priority and then stable rule ID."""

    ordered = sorted(rules, key=lambda rule: (-rule.priority, rule.rule_id))
    return next((rule for rule in ordered if rule_matches(rule, transaction)), None)


def deterministic_classification(transaction: Transaction) -> ClassificationDecision:
    description = normalise_merchant(transaction.description)

    if transaction.status in {"duplicate", "ignored"}:
        return ClassificationDecision("unresolved", None, "unclassified")
    if transaction.source_status == "pending":
        return ClassificationDecision("unresolved", None, "unclassified")
    if transaction.amount_minor > 0:
        return ClassificationDecision("business", "client_income", "deterministic")
    if "KORU STUDIO RENT" in description:
        return ClassificationDecision("business", "studio_rent", "deterministic")
    if any(merchant in description for merchant in ("ADOBE", "XERO", "FIGMA")):
        return ClassificationDecision("business", "software_subscriptions", "deterministic")
    if "OWNER DRAW" in description:
        return ClassificationDecision("personal", "owner_draw", "deterministic")
    if "HARBOUR CAFE" in description:
        return ClassificationDecision("personal", "personal_meals", "deterministic")
    return ClassificationDecision("unresolved", None, "deterministic")


def classification_for(
    transaction: Transaction,
    rules: Iterable[ClassificationRule],
) -> ClassificationDecision:
    explicit = choose_rule(transaction, rules)
    if explicit is not None:
        return ClassificationDecision(
            classification=explicit.target_classification,
            category=explicit.target_category,
            source="explicit_rule",
            rule_id=explicit.rule_id,
        )
    return deterministic_classification(transaction)


def pending_duplicate_pairs(transactions: Iterable[Transaction]) -> dict[str, str]:
    posted: dict[tuple[str, str, int, str], str] = {}
    pending: list[Transaction] = []
    for transaction in transactions:
        key = (
            transaction.occurred_on,
            normalise_merchant(transaction.description),
            transaction.amount_minor,
            transaction.currency,
        )
        if transaction.source_status == "posted":
            posted.setdefault(key, transaction.transaction_id)
        elif transaction.source_status == "pending":
            pending.append(transaction)

    result: dict[str, str] = {}
    for transaction in pending:
        key = (
            transaction.occurred_on,
            normalise_merchant(transaction.description),
            transaction.amount_minor,
            transaction.currency,
        )
        posted_id = posted.get(key)
        if posted_id is not None:
            result[transaction.transaction_id] = posted_id
    return result


def calculate_classified_totals(
    transactions: Iterable[Transaction],
    *,
    protected_reserve_minor: int,
    projected_low_point_minor: int,
) -> FinanceTotals:
    current_balance = 0
    business_income = 0
    business_expense = 0
    personal_expense = 0
    unresolved_expense = 0

    for transaction in transactions:
        if transaction.status != "posted":
            continue
        current_balance += transaction.amount_minor
        if transaction.amount_minor >= 0 and transaction.classification == "business":
            business_income += transaction.amount_minor
        elif transaction.amount_minor < 0:
            magnitude = abs(transaction.amount_minor)
            if transaction.classification == "business":
                business_expense += magnitude
            elif transaction.classification == "personal":
                personal_expense += magnitude
            elif transaction.classification == "unresolved":
                unresolved_expense += magnitude

    return FinanceTotals(
        current_balance_minor=current_balance,
        protected_reserve_minor=protected_reserve_minor,
        business_income_minor=business_income,
        business_expense_minor=business_expense,
        personal_expense_minor=personal_expense,
        unresolved_expense_minor=unresolved_expense,
        projected_low_point_minor=projected_low_point_minor,
        reserve_shortfall_minor=max(0, protected_reserve_minor - projected_low_point_minor),
    )
