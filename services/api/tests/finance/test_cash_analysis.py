"""Exact cash activity, honest coverage and production model-context wiring."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from finance_agent.api.app import create_app
from finance_agent.finance.analysis import analyse_rows, load_cash_analysis
from finance_agent.models.narrative_guard import NarrativeGuard
from finance_agent.storage import SQLiteStore


def row(
    number: int, amount: int, *, day: str = "2026-07-15", **changes: object
) -> dict[str, object]:
    return {
        "transaction_id": f"txn_sample_{number}",
        "occurred_on": day,
        "amount_minor": amount,
        "currency": "NZD",
        "status": "posted",
        "source_status": "posted",
        "classification": "business",
        "category": "software",
        "evidence_id": f"evd_sample_{number}",
        **changes,
    }


def analyse(rows: list[dict[str, object]]) -> dict[str, object]:
    return analyse_rows(
        rows, workspace_id="ws_sample_company", start=date(2026, 6, 1), end=date(2026, 7, 31)
    )


def test_cash_activity_does_not_claim_profit_or_bank_balance() -> None:
    result = analyse(
        [
            row(1, 10001),
            row(2, -2499),
            row(3, -121, classification="personal"),
            row(4, -33, classification="unresolved"),
        ]
    )
    assert result["totals"] == {
        "cashInflowMinor": 10001,
        "cashOutflowMinor": 2653,
        "netCashFlowMinor": 7348,
        "businessInflowMinor": 10001,
        "businessOutflowMinor": 2499,
        "personalOutflowMinor": 121,
        "unresolvedOutflowMinor": 33,
        "transferInflowMinor": 0,
        "transferOutflowMinor": 0,
    }
    assert result["basis"] == "recorded_cash_activity_not_profit_or_bank_balance"
    assert result["coverage"]["completeness"] == "unknown"


def test_pending_duplicates_ignored_and_transfers_are_not_operating_flows() -> None:
    result = analyse(
        [
            row(1, -100),
            row(2, -200, status="duplicate"),
            row(3, -300, status="ignored"),
            row(4, -400, status="pending"),
            row(5, -500, source_status="pending"),
            row(6, 600, classification="transfer"),
            row(7, -600, classification="transfer"),
        ]
    )
    assert result["totals"]["cashOutflowMinor"] == 100
    assert result["totals"]["transferInflowMinor"] == 600
    assert result["totals"]["transferOutflowMinor"] == 600
    assert result["coverage"]["excludedCounts"] == {
        "duplicate": 1,
        "ignored": 1,
        "pending": 2,
        "outsidePeriod": 0,
    }
    assert result["evidenceIds"] == ["evd_sample_1", "evd_sample_6", "evd_sample_7"]


def test_period_is_inclusive_without_including_future_rows() -> None:
    result = analyse(
        [
            row(1, -10, day="2026-06-01"),
            row(2, -20, day="2026-07-31"),
            row(3, -999, day="2026-08-01"),
        ]
    )
    assert result["totals"]["cashOutflowMinor"] == 30
    assert result["coverage"]["excludedCounts"]["outsidePeriod"] == 1
    assert [month["month"] for month in result["months"]] == ["2026-06", "2026-07"]


def test_missing_months_are_not_fabricated_as_zero_activity() -> None:
    result = analyse([row(1, -125)])
    assert len(result["months"]) == 1
    assert result["comparison"] is None
    assert result["coverage"]["firstObservedOn"] == "2026-07-15"
    assert result["coverage"]["lastObservedOn"] == "2026-07-15"


def test_month_comparison_is_observed_and_not_complete_period_growth() -> None:
    result = analyse([row(1, -101, day="2026-06-30"), row(2, -303)])
    assert result["comparison"] == {
        "fromMonth": "2026-06",
        "toMonth": "2026-07",
        "businessOutflowDeltaMinor": 202,
        "coverageComparable": False,
        "categoryChanges": [
            {"category": "software", "beforeMinor": 101, "afterMinor": 303, "changeMinor": 202}
        ],
    }
    assert "complete" in " ".join(result["limitations"]).lower()


def test_empty_activity_is_unknown_not_an_assertion_of_no_spending() -> None:
    result = analyse([])
    assert result["coverage"]["status"] == "no_records"
    assert result["coverage"]["firstObservedOn"] is None
    assert result["months"] == [] and result["categories"] == []
    assert result["comparison"] is None


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "USD"},
        {"amount_minor": True},
        {"amount_minor": 1.5},
        {"occurred_on": "not-a-date"},
    ],
)
def test_invalid_money_and_dates_fail_closed(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        analyse(
            [row(1, 100, **changes)]
            if "amount_minor" not in changes
            else [{**row(1, 100), **changes}]
        )


def test_input_hash_is_order_independent_and_changes_with_material_values() -> None:
    first, second = row(1, -3), row(2, -7)
    result = analyse([first, second])
    assert result["inputHash"] == analyse([second, first])["inputHash"]
    assert (
        result["inputHash"]
        != analyse([first, {**second, "classification": "personal"}])["inputHash"]
    )
    assert len(result["inputHash"]) == 64


def test_arithmetic_remains_exact_above_float_precision() -> None:
    value = 2**53 + 17
    result = analyse([row(1, value), row(2, -(value - 7))])
    assert result["totals"]["netCashFlowMinor"] == 7


def test_category_totals_are_partitioned_and_evidence_linked() -> None:
    result = analyse([row(1, -100), row(2, -20), row(3, -60, category="rent")])
    assert result["categories"][0] == {
        "category": "software",
        "outflowMinor": 120,
        "evidenceIds": ["evd_sample_1", "evd_sample_2"],
    }
    assert (
        sum(item["outflowMinor"] for item in result["categories"])
        == result["totals"]["businessOutflowMinor"]
    )


def test_range_and_duplicate_identifiers_fail_closed() -> None:
    with pytest.raises(ValueError):
        analyse([row(1, -1), row(1, -1)])
    with pytest.raises(ValueError):
        analyse_rows(
            [], workspace_id="ws_sample_company", start=date(2026, 8, 1), end=date(2026, 7, 1)
        )
    with pytest.raises(ValueError):
        analyse_rows(
            [], workspace_id="ws_sample_company", start=date(2020, 1, 1), end=date(2026, 7, 1)
        )


@pytest.mark.asyncio
async def test_actual_api_analysis_is_read_only_scoped_and_session_authenticated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "finance_agent.api.analysis_services.local_today", lambda _: date(2026, 9, 7)
    )
    app = create_app(database_path=tmp_path / "analysis.sqlite3", session_token="synthetic-session")
    services = app.state.finance_route_services
    before = {
        table: services.store.fetch_one(f"SELECT COUNT(*) AS count FROM {table}")["count"]
        for table in ("finance_events", "job_runs", "source_rows")
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = "/v1/workspaces/ws_koru_studio/analysis?start=2026-07-01&end=2026-07-31"
            assert (await client.get(path)).status_code == 401
            response = await client.get(path, headers={"X-Folio-Session": "synthetic-session"})
            assert response.status_code == 200
            result = response.json()
            assert result["totals"]["businessOutflowMinor"] == 139499
            assert (
                await client.get(
                    path.replace("ws_koru_studio", "ws_wrong_company"),
                    headers={"X-Folio-Session": "synthetic-session"},
                )
            ).status_code == 404
            assert (
                await client.get(
                    path.replace("2026-07-01", "2027-07-01"),
                    headers={"X-Folio-Session": "synthetic-session"},
                )
            ).status_code == 422
        after = {
            table: services.store.fetch_one(f"SELECT COUNT(*) AS count FROM {table}")["count"]
            for table in before
        }
        assert before == after
        context = await services.finance_core.load_context("ws_koru_studio", "thr_koru_studio_main")
        prompt = json.dumps(
            NarrativeGuard().compile_references(context.projection).as_prompt_value()
        )
        assert "Recorded period business outflow" in prompt
        assert "NZD 1,394.99" in prompt
        assert "completeness" in prompt.lower()
    finally:
        await services.aclose()


def test_storage_unknown_workspace_is_not_an_empty_result(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "empty.sqlite3")
    store.migrate()
    with pytest.raises(KeyError):
        load_cash_analysis(
            store, workspace_id="ws_absent_company", start=date(2026, 7, 1), end=date(2026, 7, 31)
        )


def test_comparison_identifies_recorded_category_drivers() -> None:
    result = analyse(
        [
            row(1, -100, day="2026-06-15", category="rent"),
            row(2, -250, day="2026-07-15", category="rent"),
            row(3, -80, day="2026-06-15", category="software"),
            row(4, -70, day="2026-07-15", category="software"),
        ]
    )
    assert result["comparison"]["categoryChanges"][0] == {
        "category": "rent",
        "beforeMinor": 100,
        "afterMinor": 250,
        "changeMinor": 150,
    }
    assert sum(item["changeMinor"] for item in result["comparison"]["categoryChanges"]) == 140
    from finance_agent.finance.analysis import analysis_projection

    references = NarrativeGuard().compile_references(analysis_projection(result))
    assert any(
        "rent" in item.label and item.formatted_value == "NZD 1.50"
        for item in references.amount_references
    )


def test_pending_only_activity_is_disclosed_explicitly() -> None:
    result = analyse([row(1, -10, status="pending")])
    assert result["coverage"]["status"] == "no_posted_records"


def test_oversized_input_is_rejected_not_silently_truncated() -> None:
    with pytest.raises(ValueError, match="limit"):
        analyse([row(index, 1) for index in range(20_001)])


@pytest.mark.asyncio
async def test_existing_empty_workspace_cannot_read_another_workspaces_data(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "scoped.sqlite3")
    services = app.state.finance_route_services
    try:
        existing = dict(
            services.store.fetch_one(
                "SELECT * FROM workspaces WHERE workspace_id = 'ws_koru_studio'"
            )
        )
        existing.update(workspace_id="ws_other_company", thread_id="thr_other_company")
        with services.store.transaction() as connection:
            columns = ", ".join(existing)
            placeholders = ", ".join("?" for _ in existing)
            connection.execute(
                f"INSERT INTO workspaces ({columns}) VALUES ({placeholders})",
                tuple(existing.values()),
            )
        result = load_cash_analysis(
            services.store,
            workspace_id="ws_other_company",
            start=date(2026, 7, 1),
            end=date(2026, 7, 31),
        )
        assert result["coverage"]["rowCount"] == 0
        assert result["evidenceIds"] == []
    finally:
        await services.aclose()


@pytest.mark.asyncio
async def test_analysis_reaches_model_through_real_turn_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finance_agent.models.base import (
        AdapterStatus,
        CapabilityCard,
        ModelMode,
        ModelPurpose,
        ModelResponse,
    )
    from finance_agent.models.router import ModelModeRouter

    class Model:
        provider = "lm_studio"
        requests = []

        async def capability(self):
            return CapabilityCard(
                self.provider,
                AdapterStatus.READY,
                "synthetic",
                0,
                False,
                True,
                False,
                8192,
                "test only",
            )

        async def complete(self, request):
            self.requests.append(request)
            return ModelResponse(
                "invalid plan"
                if request.purpose is ModelPurpose.COMPILE_PLAN
                else "Recorded period business outflow is NZD 1,394.99.",
                self.provider,
                "synthetic",
                1,
            )

        async def aclose(self):
            pass

    monkeypatch.setattr(
        "finance_agent.api.analysis_services.local_today", lambda _: date(2026, 9, 7)
    )
    app = create_app(database_path=tmp_path / "turn.sqlite3")
    services = app.state.finance_route_services
    local = Model()
    await services.local_model.aclose()
    services.local_model = local
    services.model_router = ModelModeRouter(local, services.cloud_model)
    services._compose_controller()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/v1/threads/thr_koru_studio_main/turns",
                json={
                    "workspaceId": "ws_koru_studio",
                    "turnId": "turn_analysis_integration",
                    "content": "Analyse recorded cash activity.",
                    "mode": ModelMode.LOCAL.value,
                },
            )
            assert response.status_code == 200
        explanation = next(
            request for request in local.requests if request.purpose is ModelPurpose.EXPLAIN
        )
        assert "Recorded period business outflow" in explanation.user
        assert "completeness" in explanation.user.lower()
        assert services.conversations.recent_turns("thr_koru_studio_main", 1)[0].content.startswith(
            "Recorded period business outflow is NZD 1,394.99."
        )
    finally:
        await services.aclose()
