"""Deterministic exact-money finance package (Task 1)."""

from .classification import (
    calculate_classified_totals,
    classification_for,
    pending_duplicate_pairs,
    rule_matches,
)
from .domain import (
    CashForecast,
    ClassificationDecision,
    ClassificationRule,
    FinanceTotals,
    ForecastEvent,
    ForecastPoint,
    Money,
    Transaction,
)
from .forecast import koru_30_day_forecast, project_cash
from .ingest import CSVImporter, CSVIngestError, ImportResult
from .service import EventResult, FinanceEngine, FinanceStateError

__all__ = [
    "CSVImporter",
    "CSVIngestError",
    "CashForecast",
    "ClassificationDecision",
    "ClassificationRule",
    "FinanceTotals",
    "FinanceEngine",
    "FinanceStateError",
    "ForecastEvent",
    "ForecastPoint",
    "ImportResult",
    "Money",
    "EventResult",
    "Transaction",
    "calculate_classified_totals",
    "classification_for",
    "koru_30_day_forecast",
    "pending_duplicate_pairs",
    "project_cash",
    "rule_matches",
]
