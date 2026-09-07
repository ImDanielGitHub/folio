"""Starting Folio must never turn snapshot damage into an automatic data reset."""

from __future__ import annotations

from pathlib import Path

import pytest
from finance_agent.api.app import create_app
from finance_agent.finance import FinanceStateError
from finance_agent.finance.service import FinanceEngine
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["development", "demo", "production"])
async def test_existing_workspace_without_snapshot_is_preserved(tmp_path: Path, mode: str) -> None:
    database = tmp_path / "interrupted.sqlite3"
    store = SQLiteStore(database)
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    with store.transaction() as connection:
        connection.execute("UPDATE workspaces SET name = 'Owner records: preserve me'")
    store.record_turn(
        turn_id="turn_owner_existing",
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        role="owner",
        content="Irreplaceable owner context.",
        occurred_at="2026-09-07T00:00:00+00:00",
    )
    source_before = [dict(row) for row in store.fetch_all("SELECT * FROM source_rows")]
    created = None
    try:
        with pytest.raises(FinanceStateError, match="recovery"):
            created = create_app(database_path=database, runtime_mode=mode)
        assert (
            store.fetch_one("SELECT name FROM workspaces")["name"] == "Owner records: preserve me"
        )
        assert (
            store.fetch_one("SELECT content FROM conversation_turns")["content"]
            == "Irreplaceable owner context."
        )
        assert [dict(row) for row in store.fetch_all("SELECT * FROM source_rows")] == source_before
        assert store.fetch_one("SELECT COUNT(*) AS n FROM job_runs")["n"] == 0
    finally:
        if created is not None:
            await created.state.finance_route_services.aclose()


@pytest.mark.asyncio
async def test_current_snapshot_loss_does_not_destroy_previous_financial_history(
    tmp_path: Path,
) -> None:
    database = tmp_path / "damaged-pointer.sqlite3"
    original = create_app(database_path=database)
    store = original.state.finance_route_services.store
    with store.transaction() as connection:
        connection.execute("UPDATE workspace_snapshots SET is_current = 0")
        connection.execute("UPDATE workspaces SET name = 'Owner workspace'")
    before = [dict(row) for row in store.fetch_all("SELECT * FROM workspace_snapshots")]
    await original.state.finance_route_services.aclose()
    created = None
    try:
        with pytest.raises(FinanceStateError, match="recovery"):
            created = create_app(database_path=database)
        assert store.fetch_one("SELECT name FROM workspaces")["name"] == "Owner workspace"
        assert [dict(row) for row in store.fetch_all("SELECT * FROM workspace_snapshots")] == before
    finally:
        if created is not None:
            await created.state.finance_route_services.aclose()


@pytest.mark.asyncio
async def test_fresh_and_healthy_workspaces_still_start(tmp_path: Path) -> None:
    database = tmp_path / "healthy.sqlite3"
    first = create_app(database_path=database)
    snapshot = first.state.finance_route_services.engine.get_snapshot()["snapshotId"]
    await first.state.finance_route_services.aclose()
    second = create_app(database_path=database)
    try:
        assert second.state.finance_route_services.engine.get_snapshot()["snapshotId"] == snapshot
    finally:
        await second.state.finance_route_services.aclose()
