from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from finance_agent.api.app import create_app
from finance_agent.api.http_security import OriginGuardMiddleware
from finance_agent.finance import FinanceEngine
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def _engine(tmp_path: Path) -> FinanceEngine:
    value = FinanceEngine(SQLiteStore(tmp_path / "folio.sqlite3"))
    value.reset_demo(CSV)
    return value


def test_database_path_expands_home_before_connect(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    store = SQLiteStore("~/folio/data.sqlite3")
    expected = (home / "folio" / "data.sqlite3").resolve()
    assert Path(store.database_path) == expected
    store.migrate()
    assert expected.exists()


@pytest.mark.asyncio
async def test_create_app_consumes_finance_database_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configured = tmp_path / "configured" / "folio.sqlite3"
    monkeypatch.setenv("FINANCE_DATABASE_PATH", str(configured))
    value = create_app(auto_seed=False)
    try:
        assert Path(value.state.finance_route_services.store.database_path) == configured.resolve()
    finally:
        await value.state.finance_route_services.aclose()


def test_daily_close_identity_includes_claim_policy_and_close_date(tmp_path: Path) -> None:
    value = _engine(tmp_path)
    def first_clock() -> datetime:
        return datetime(2026, 8, 26, 9, 0, tzinfo=UTC)

    def next_clock() -> datetime:
        return datetime(2026, 8, 27, 9, 0, tzinfo=UTC)

    service = DailyCloseService(value, clock=first_clock)
    initial = service.identity().input_hash

    value.store.record_turn(
        turn_id="turn_state_vector_claim",
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        role="owner",
        content="Keep a larger reserve for September.",
        occurred_at="2026-08-26T09:01:00+00:00",
        model_mode="local",
    )
    value.store.record_claim(
        {
            "claimId": "claim_state_vector_reserve",
            "workspaceId": "ws_koru_studio",
            "claimType": "reserve_policy",
            "statement": "Keep a larger reserve for September.",
            "sourceTurnId": "turn_state_vector_claim",
            "scope": {"workspaceId": "ws_koru_studio"},
            "effectiveDate": "2026-09-01",
            "recordedAt": "2026-08-26T09:01:00+00:00",
        }
    )
    after_claim = service.identity().input_hash
    assert after_claim != initial

    with value.store.transaction() as connection:
        connection.execute(
            "UPDATE job_definitions SET policy_version = 'daily_close_policy@2' "
            "WHERE workspace_id = 'ws_koru_studio'"
        )
    after_policy = service.identity().input_hash
    assert after_policy != after_claim
    assert DailyCloseService(value, clock=next_clock).identity().input_hash != after_policy


@pytest.mark.asyncio
async def test_origin_guard_rejects_untrusted_mutation_origin() -> None:
    app = FastAPI()

    @app.post("/mutate")
    async def mutate() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(
        OriginGuardMiddleware,
        allowed_origins={"app://folio", "http://127.0.0.1:4173"},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        rejected = await client.post(
            "/mutate",
            headers={"Origin": "https://evil.example"},
        )
        accepted = await client.post(
            "/mutate",
            headers={"Origin": "app://folio"},
        )
        test_client = await client.post(
            "/mutate",
            headers={"Origin": "http://test"},
        )
        cli = await client.post("/mutate")
    assert rejected.status_code == 403
    assert accepted.json() == {"ok": True}
    assert test_client.json() == {"ok": True}
    assert cli.json() == {"ok": True}


@pytest.mark.asyncio
async def test_production_app_disables_docs_demo_reset_and_diagnostics(
    tmp_path: Path,
) -> None:
    value = create_app(
        database_path=tmp_path / "production.sqlite3",
        auto_seed=False,
        runtime_mode="production",
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=value),
            base_url="http://test",
        ) as client:
            health = await client.get("/health")
            docs = await client.get("/docs")
            openapi = await client.get("/openapi.json")
            reset = await client.post(
                "/v1/demo/reset",
                headers={"Origin": "http://test"},
                json={"workspaceId": "ws_koru_studio"},
            )
            diagnostics = await client.get(
                "/v1/diagnostics/working-understanding",
                params={"workspaceId": "ws_koru_studio"},
            )
        assert health.status_code == 200
        assert docs.status_code == 404
        assert openapi.status_code == 404
        assert reset.status_code == 404
        assert diagnostics.status_code == 404
    finally:
        await value.state.finance_route_services.aclose()
