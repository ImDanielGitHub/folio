"""Transparent, deterministic 30-day cash projection."""

from __future__ import annotations

from collections.abc import Iterable

from .domain import CashForecast, ForecastEvent, ForecastPoint

KORU_FORECAST_EVENTS: tuple[ForecastEvent, ...] = (
    ForecastEvent("2026-07-28", "Expected client payment", 125_000),
    ForecastEvent("2026-07-31", "Studio rent", -120_000),
    ForecastEvent("2026-08-03", "Adobe", -8_999),
    ForecastEvent("2026-08-04", "Xero", -7_500),
    ForecastEvent("2026-08-07", "Planned laptop", -300_000),
    ForecastEvent("2026-08-10", "Figma", -3_000),
)

KORU_FORECAST_ASSUMPTIONS: tuple[str, ...] = (
    "The NZD 1,250 client payment arrives on 28 July.",
    "The NZD 3,000 laptop purchase is paid on 7 August.",
    "Rent and software recur on their listed dates; no unlisted income is assumed.",
)


def project_cash(
    *,
    current_balance_minor: int,
    protected_reserve_minor: int,
    start_date: str,
    events: Iterable[ForecastEvent],
    assumptions: Iterable[str],
    alternative_excluded_label: str = "Planned laptop",
) -> CashForecast:
    ordered_events = sorted(events, key=lambda event: (event.date, event.label))
    balance = current_balance_minor
    points = [
        ForecastPoint(
            date=start_date,
            label="Starting cleared balance",
            amount_minor=current_balance_minor,
            balance_minor=current_balance_minor,
            reserve_minor=protected_reserve_minor,
        )
    ]
    for event in ordered_events:
        balance += event.amount_minor
        points.append(
            ForecastPoint(
                date=event.date,
                label=event.label,
                amount_minor=event.amount_minor,
                balance_minor=balance,
                reserve_minor=protected_reserve_minor,
            )
        )

    alternative_balance = current_balance_minor
    alternative_balances = [alternative_balance]
    for event in ordered_events:
        if event.label == alternative_excluded_label:
            continue
        alternative_balance += event.amount_minor
        alternative_balances.append(alternative_balance)

    low_point = min(point.balance_minor for point in points)
    return CashForecast(
        points=tuple(points),
        assumptions=tuple(assumptions),
        low_point_minor=low_point,
        reserve_shortfall_minor=max(0, protected_reserve_minor - low_point),
        alternative_low_point_minor=min(alternative_balances),
    )


def koru_30_day_forecast(
    current_balance_minor: int,
    protected_reserve_minor: int,
) -> CashForecast:
    return project_cash(
        current_balance_minor=current_balance_minor,
        protected_reserve_minor=protected_reserve_minor,
        start_date="2026-07-17",
        events=KORU_FORECAST_EVENTS,
        assumptions=KORU_FORECAST_ASSUMPTIONS,
    )
