"""Read-only cash activity analysis with exact sums and explicit coverage limits."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json

MAX_ANALYSIS_ROWS = 20_000
MATERIAL_FIELDS = (
    "transaction_id",
    "occurred_on",
    "amount_minor",
    "currency",
    "status",
    "source_status",
    "classification",
    "category",
    "evidence_id",
)
LIMITATIONS = (
    "Imported record completeness is unknown; absent records do not establish zero activity.",
    "Cash movements are not accrual profit, tax liability "
    "or an independently verified bank balance.",
    "Observed months may be incomplete; differences are not complete-period growth rates.",
    "Categories reflect the current recorded classification, not independent accounting review.",
)


def _range(start: date, end: date) -> None:
    if start > end or (end - start).days >= 366:
        raise ValueError("Analysis requires an inclusive range of one to 366 days")


def _totals() -> dict[str, int]:
    return dict.fromkeys(
        (
            "cashInflowMinor",
            "cashOutflowMinor",
            "netCashFlowMinor",
            "businessInflowMinor",
            "businessOutflowMinor",
            "personalOutflowMinor",
            "unresolvedOutflowMinor",
            "transferInflowMinor",
            "transferOutflowMinor",
        ),
        0,
    )


def analyse_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    workspace_id: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Summarise only the supplied scope. Never silently truncate an oversized input."""
    _range(start, end)
    if len(rows) > MAX_ANALYSIS_ROWS:
        raise ValueError("Analysis row limit exceeded; choose a shorter period")
    totals = _totals()
    months: dict[str, dict[str, Any]] = {}
    categories: dict[str, dict[str, Any]] = {}
    monthly_categories: dict[str, dict[str, int]] = {}
    excluded = {"duplicate": 0, "ignored": 0, "pending": 0, "outsidePeriod": 0}
    material: list[dict[str, Any]] = []
    evidence: set[str] = set()
    seen: set[str] = set()
    observed: list[str] = []
    posted_count = 0
    for row in rows:
        transaction_id = row["transaction_id"]
        amount = row["amount_minor"]
        if not isinstance(transaction_id, str) or not transaction_id or transaction_id in seen:
            raise ValueError("Analysis requires unique non-empty transaction identifiers")
        seen.add(transaction_id)
        if isinstance(amount, bool) or not isinstance(amount, int) or row["currency"] != "NZD":
            raise ValueError("Analysis accepts exact NZD integer minor units only")
        occurred_on = date.fromisoformat(row["occurred_on"])
        if not start <= occurred_on <= end:
            excluded["outsidePeriod"] += 1
            continue
        classification = row["classification"]
        status = row["status"]
        if classification not in {"business", "personal", "unresolved", "transfer"}:
            raise ValueError("Unsupported transaction classification")
        if status not in {"posted", "pending", "duplicate", "ignored"}:
            raise ValueError("Unsupported transaction status")
        if row["source_status"] not in {"posted", "pending"}:
            raise ValueError("Unsupported source status")
        evidence_id = row["evidence_id"]
        if not isinstance(evidence_id, str) or not evidence_id:
            raise ValueError("Every analysed row requires linked evidence")
        material.append({key: row[key] for key in MATERIAL_FIELDS})
        observed.append(occurred_on.isoformat())
        if status in {"duplicate", "ignored"}:
            excluded[status] += 1
            continue
        if status == "pending" or row["source_status"] == "pending":
            excluded["pending"] += 1
            continue
        posted_count += 1
        evidence.add(evidence_id)
        month_key = occurred_on.strftime("%Y-%m")
        month = months.setdefault(month_key, {"month": month_key, **_totals(), "rowCount": 0})
        month["rowCount"] += 1
        changes: dict[str, int] = {}
        if classification == "transfer":
            changes["transferInflowMinor" if amount >= 0 else "transferOutflowMinor"] = abs(amount)
        else:
            changes["cashInflowMinor" if amount >= 0 else "cashOutflowMinor"] = abs(amount)
            changes["netCashFlowMinor"] = amount
            if amount >= 0 and classification == "business":
                changes["businessInflowMinor"] = amount
            elif amount < 0:
                changes[f"{classification}OutflowMinor"] = -amount
                if classification == "business":
                    category = row["category"] or "uncategorised"
                    if not isinstance(category, str) or len(category) > 240:
                        raise ValueError("Invalid category label")
                    bucket = categories.setdefault(
                        category,
                        {
                            "category": category,
                            "outflowMinor": 0,
                            "evidenceIds": [],
                        },
                    )
                    month_categories = monthly_categories.setdefault(month_key, {})
                    month_categories[category] = month_categories.get(category, 0) - amount
                    bucket["outflowMinor"] += -amount
                    bucket["evidenceIds"].append(evidence_id)
        for key, amount_change in changes.items():
            totals[key] += amount_change
            month[key] += amount_change
    ordered_months = [months[key] for key in sorted(months)]
    comparison = None
    if len(ordered_months) >= 2:
        previous, latest = ordered_months[-2:]
        before = monthly_categories.get(previous["month"], {})
        after = monthly_categories.get(latest["month"], {})
        category_changes: list[dict[str, Any]] = [
            {
                "category": category,
                "beforeMinor": before.get(category, 0),
                "afterMinor": after.get(category, 0),
                "changeMinor": after.get(category, 0) - before.get(category, 0),
            }
            for category in sorted(before.keys() | after.keys())
        ]
        comparison = {
            "fromMonth": previous["month"],
            "toMonth": latest["month"],
            "businessOutflowDeltaMinor": latest["businessOutflowMinor"]
            - previous["businessOutflowMinor"],
            "coverageComparable": False,
            "categoryChanges": sorted(
                category_changes, key=lambda item: (-abs(item["changeMinor"]), item["category"])
            ),
        }
    category_rows = sorted(
        categories.values(), key=lambda item: (-item["outflowMinor"], item["category"])
    )
    for item in category_rows:
        item["evidenceIds"] = sorted(set(item["evidenceIds"]))
    input_value = {
        "version": "cash.analysis@1",
        "workspaceId": workspace_id,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "rows": sorted(material, key=lambda item: item["transaction_id"]),
    }
    return {
        "analysisVersion": "cash.analysis@1",
        "workspaceId": workspace_id,
        "period": {"start": start.isoformat(), "end": end.isoformat()},
        "currency": "NZD",
        "basis": "recorded_cash_activity_not_profit_or_bank_balance",
        "inputHash": hashlib.sha256(canonical_json(input_value).encode()).hexdigest(),
        "coverage": {
            "status": "observed_records"
            if posted_count
            else "no_posted_records"
            if material
            else "no_records",
            "completeness": "unknown",
            "rowCount": len(material),
            "postedCount": posted_count,
            "firstObservedOn": min(observed) if observed else None,
            "lastObservedOn": max(observed) if observed else None,
            "excludedCounts": excluded,
        },
        "totals": totals,
        "months": ordered_months,
        "categories": category_rows,
        "comparison": comparison,
        "evidenceIds": sorted(evidence),
        "limitations": list(LIMITATIONS),
    }


def load_cash_analysis(
    store: SQLiteStore,
    *,
    workspace_id: str,
    start: date,
    end: date,
) -> dict[str, Any]:
    """Read one SQLite snapshot, scoped before aggregation, with a hard row cap."""
    _range(start, end)
    with store.transaction(immediate=False) as connection:
        if (
            connection.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            is None
        ):
            raise KeyError("unknown workspace")
        rows = connection.execute(
            """SELECT transaction_id, occurred_on, amount_minor, currency, status,
                      source_status, classification, category, evidence_id
               FROM transactions WHERE workspace_id = ? AND occurred_on >= ? AND occurred_on <= ?
               ORDER BY occurred_on, transaction_id LIMIT ?""",
            (workspace_id, start.isoformat(), end.isoformat(), MAX_ANALYSIS_ROWS + 1),
        ).fetchall()
        return analyse_rows(
            [dict(row) for row in rows], workspace_id=workspace_id, start=start, end=end
        )


def analysis_projection(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Small aggregate projection: no raw transactions or merchant history."""
    totals = analysis["totals"]
    period = analysis["period"]
    coverage = analysis["coverage"]
    labels = [
        f"Recorded activity period: {period['start']} to {period['end']}.",
        *LIMITATIONS,
    ]
    amounts: dict[str, object] = {"currency": "NZD"}
    if coverage["postedCount"]:
        for key in (
            "businessInflowMinor",
            "businessOutflowMinor",
            "personalOutflowMinor",
            "unresolvedOutflowMinor",
            "netCashFlowMinor",
        ):
            amounts["recordedPeriod" + key[0].upper() + key[1:]] = totals[key]
        for month in analysis["months"][-3:]:
            amounts[f"Observed {month['month']} business outflow Minor"] = month[
                "businessOutflowMinor"
            ]
        for item in analysis["categories"][:3]:
            label = str(item["category"]).replace("_", " ")[:80]
            amounts[f"Recorded period category {label} outflow Minor"] = item["outflowMinor"]
        comparison = analysis["comparison"]
        if comparison:
            scope = f"Observed {comparison['fromMonth']} to {comparison['toMonth']}"
            amounts[f"{scope} business outflow change Minor"] = comparison[
                "businessOutflowDeltaMinor"
            ]
            for change in comparison["categoryChanges"][:2]:
                category = str(change["category"]).replace("_", " ")[:80]
                amounts[f"{scope} category {category} outflow change Minor"] = change["changeMinor"]
        labels.append(f"Latest observed record date: {coverage['lastObservedOn']}.")
    else:
        labels.append(
            "No posted records were available for this period; "
            "no financial conclusion is supported."
        )
    return {"aggregate_amounts": amounts, "finding_labels": labels}
