"""Exact-money domain types used by deterministic finance code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class Money:
    amount_minor: int
    currency: str = "NZD"

    def __post_init__(self) -> None:
        if isinstance(self.amount_minor, bool) or not isinstance(self.amount_minor, int):
            raise TypeError("money must use integer minor units")
        if self.currency != "NZD":
            raise ValueError("the P0 finance core supports NZD only")

    def __add__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._same_currency(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def magnitude(self) -> int:
        return abs(self.amount_minor)

    def as_contract(self) -> dict[str, Any]:
        return {"amountMinor": self.amount_minor, "currency": self.currency}

    def _same_currency(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError("cannot combine money in different currencies")


@dataclass(frozen=True, slots=True)
class Transaction:
    transaction_id: str
    occurred_on: str
    description: str
    amount_minor: int
    currency: str
    source_status: str
    status: str
    classification: str
    category: str | None
    classification_source: str
    rule_id: str | None
    evidence_id: str
    duplicate_of_transaction_id: str | None = None

    @property
    def expense_magnitude_minor(self) -> int:
        return abs(min(self.amount_minor, 0))


@dataclass(frozen=True, slots=True)
class ClassificationRule:
    rule_id: str
    merchant_contains: str
    maximum_amount_minor: int
    currency: str
    target_classification: str
    target_category: str | None
    effective_from: str
    priority: int = 100

    def __post_init__(self) -> None:
        if not self.merchant_contains.strip():
            raise ValueError("merchant_contains must not be blank")
        if self.maximum_amount_minor < 0:
            raise ValueError("maximum amount must be non-negative")
        if self.currency != "NZD":
            raise ValueError("the P0 rule engine supports NZD only")
        if self.target_classification not in {"business", "personal", "unresolved"}:
            raise ValueError("unsupported rule classification")


@dataclass(frozen=True, slots=True)
class ClassificationDecision:
    classification: str
    category: str | None
    source: str
    rule_id: str | None = None


@dataclass(frozen=True, slots=True)
class FinanceTotals:
    current_balance_minor: int
    protected_reserve_minor: int
    business_income_minor: int
    business_expense_minor: int
    personal_expense_minor: int
    unresolved_expense_minor: int
    projected_low_point_minor: int
    reserve_shortfall_minor: int

    def as_contract(self, *, as_of: str) -> dict[str, Any]:
        return {
            "asOf": as_of,
            "currency": "NZD",
            "currentBalanceMinor": self.current_balance_minor,
            "protectedReserveMinor": self.protected_reserve_minor,
            "businessIncomeMinor": self.business_income_minor,
            "businessExpenseMinor": self.business_expense_minor,
            "personalExpenseMinor": self.personal_expense_minor,
            "unresolvedExpenseMinor": self.unresolved_expense_minor,
            "projectedLowPointMinor": self.projected_low_point_minor,
            "reserveShortfallMinor": self.reserve_shortfall_minor,
        }


@dataclass(frozen=True, slots=True)
class ForecastEvent:
    date: str
    label: str
    amount_minor: int


@dataclass(frozen=True, slots=True)
class ForecastPoint:
    date: str
    label: str
    amount_minor: int
    balance_minor: int
    reserve_minor: int

    @property
    def status(self) -> str:
        return "below_reserve" if self.balance_minor < self.reserve_minor else "above_reserve"

    def as_contract(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "balanceMinor": self.balance_minor,
            "reserveMinor": self.reserve_minor,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class CashForecast:
    points: tuple[ForecastPoint, ...]
    assumptions: tuple[str, ...]
    low_point_minor: int
    reserve_shortfall_minor: int
    alternative_low_point_minor: int
