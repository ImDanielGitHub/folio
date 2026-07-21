"""Closed FinanceSurfaceSpec producers backed only by finance-core values."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .domain import CashForecast, FinanceTotals


def freshness(data_through: str) -> dict[str, str]:
    return {
        "dataThrough": data_through,
        "status": "current",
        "timezone": "Pacific/Auckland",
    }


def living_brief_surface(
    *,
    totals: FinanceTotals,
    findings: Sequence[dict[str, Any]],
    data_through: str,
) -> dict[str, Any]:
    finding_blocks = []
    block_ids = {
        "finding_koru_missing_receipt": "block_koru_missing_receipt",
        "finding_koru_duplicate_pending": "block_koru_duplicate_pending",
        "finding_koru_reserve_risk": "block_koru_reserve_risk",
    }
    for finding in findings:
        if finding["status"] != "open":
            continue
        finding_blocks.append(
            {
                "blockId": block_ids.get(
                    finding["findingId"], f"block_{finding['findingId'].split('_', 1)[-1]}"
                ),
                "type": "finding",
                "findingId": finding["findingId"],
                "severity": finding["severity"],
                "title": finding["title"],
                "summary": finding["summary"],
                "amountMinor": finding["amountMinor"],
                "currency": finding["currency"],
                "status": finding["status"],
                "evidenceIds": finding["evidenceIds"],
            }
        )
    return {
        "specVersion": "FinanceSurfaceSpec@1",
        "surfaceId": "surface_koru_living_brief",
        "surfaceType": "living_brief",
        "title": "Morning close",
        "subtitle": "Three items need your attention",
        "freshness": freshness(data_through),
        "blocks": [
            {
                "blockId": "block_koru_close_summary",
                "type": "narrative",
                "text": (
                    "The close finished from 10 bank rows. One pending Figma row was held out "
                    "as a likely duplicate. The 30-day plan dips below the protected reserve "
                    "after the planned laptop purchase."
                ),
                "tone": "caution",
            },
            {
                "blockId": "block_koru_current_balance",
                "type": "metric",
                "label": "Cleared balance",
                "valueMinor": totals.current_balance_minor,
                "currency": "NZD",
                "evidenceIds": ["evd_koru_bank_csv"],
            },
            {
                "blockId": "block_koru_projected_low",
                "type": "metric",
                "label": "Projected 30-day low",
                "valueMinor": totals.projected_low_point_minor,
                "currency": "NZD",
                "evidenceIds": ["evd_koru_forecast_30d"],
            },
            *finding_blocks,
        ],
        "actions": [
            {
                "actionId": "act_koru_show_cash",
                "type": "run_scenario",
                "label": "Explore cash risk",
                "scenarioId": "scenario_koru_laptop",
            },
            {
                "actionId": "act_koru_open_sources",
                "type": "open_drawer",
                "label": "Open sources",
                "drawer": "sources",
            },
            {
                "actionId": "act_koru_open_activity",
                "type": "open_drawer",
                "label": "Open activity",
                "drawer": "activity",
            },
        ],
    }


def cash_scenario_surface(*, forecast: CashForecast, data_through: str) -> dict[str, Any]:
    return {
        "specVersion": "FinanceSurfaceSpec@1",
        "surfaceId": "surface_koru_cash_scenario",
        "surfaceType": "cash_scenario",
        "title": "30-day cash scenario",
        "subtitle": "Planned laptop purchase on 7 August",
        "freshness": freshness(data_through),
        "blocks": [
            {
                "blockId": "block_koru_cash_series",
                "type": "cash_series",
                "currency": "NZD",
                "points": [point.as_contract() for point in forecast.points],
                "assumptions": list(forecast.assumptions),
                "evidenceIds": ["evd_koru_bank_csv", "evd_koru_forecast_30d"],
            },
            {
                "blockId": "block_koru_scenario_compare",
                "type": "scenario_compare",
                "baseline": {
                    "label": "With laptop",
                    "lowPointMinor": forecast.low_point_minor,
                    "reserveShortfallMinor": forecast.reserve_shortfall_minor,
                    "currency": "NZD",
                },
                "alternative": {
                    "label": "Defer laptop",
                    "lowPointMinor": forecast.alternative_low_point_minor,
                    "reserveShortfallMinor": 0,
                    "currency": "NZD",
                },
                "assumptions": ["Only the planned laptop timing changes."],
                "evidenceIds": ["evd_koru_forecast_30d"],
            },
        ],
        "actions": [
            {
                "actionId": "act_koru_open_sources_cash",
                "type": "open_drawer",
                "label": "View assumptions",
                "drawer": "sources",
            }
        ],
    }


def work_receipt_surface(
    *,
    event_id: str,
    title: str,
    subtitle: str,
    changes: Sequence[dict[str, Any]],
    evidence_ids: Sequence[str],
    data_through: str,
    inverse_label: str,
) -> dict[str, Any]:
    suffix = event_id.removeprefix("evt_")
    return {
        "specVersion": "FinanceSurfaceSpec@1",
        "surfaceId": "surface_koru_work_receipt",
        "surfaceType": "work_receipt",
        "title": title,
        "subtitle": subtitle,
        "freshness": freshness(data_through),
        "blocks": [
            {
                "blockId": f"block_{suffix}_change",
                "type": "change_diff",
                "eventId": event_id,
                "changes": list(changes),
                "evidenceIds": list(evidence_ids),
                "undoAvailable": True,
            }
        ],
        "actions": [
            {
                "actionId": f"act_{suffix}_inverse",
                "type": "undo_event",
                "label": inverse_label,
                "eventId": event_id,
            },
            {
                "actionId": f"act_{suffix}_activity",
                "type": "open_drawer",
                "label": "Open activity",
                "drawer": "activity",
            },
        ],
    }


def owner_pack_surface(*, artifacts: Sequence[dict[str, Any]], data_through: str) -> dict[str, Any]:
    blocks = [
        {
            "blockId": f"block_{artifact['artifactId'].removeprefix('artifact_')}",
            "type": "artifact_preview",
            "artifactId": artifact["artifactId"],
            "kind": "html" if artifact["kind"] == "owner_pack_html" else "pdf",
            "title": artifact["title"],
            "generatedAt": artifact["generatedAt"],
            "contentHash": artifact["contentHash"],
            "downloadAvailable": True,
            "evidenceIds": artifact["evidenceIds"],
        }
        for artifact in artifacts
    ]
    actions = [
        {
            "actionId": f"act_{artifact['artifactId'].removeprefix('artifact_')}_download",
            "type": "download_artifact",
            "label": f"Download {'HTML' if artifact['kind'] == 'owner_pack_html' else 'PDF'}",
            "artifactId": artifact["artifactId"],
            "format": "html" if artifact["kind"] == "owner_pack_html" else "pdf",
        }
        for artifact in artifacts
    ]
    return {
        "specVersion": "FinanceSurfaceSpec@1",
        "surfaceId": "surface_koru_owner_pack",
        "surfaceType": "owner_pack",
        "title": "Folio demo owner pack",
        "subtitle": "Source-linked preparatory working material",
        "freshness": freshness(data_through),
        "blocks": blocks,
        "actions": actions,
    }
