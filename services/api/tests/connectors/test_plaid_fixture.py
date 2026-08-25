"""Offline Plaid fixture ingestion never reaches the network."""

from __future__ import annotations

from pathlib import Path

import pytest
from finance_agent.connectors.base import ConnectorError
from finance_agent.connectors.plaid_fixture import PlaidFixtureIngestor
from finance_agent.finance.service import FinanceEngine
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]


def test_plaid_fixture_ingestor_loads_demo_file() -> None:
    result = PlaidFixtureIngestor().ingest()
    assert result.row_count == 6
    assert result.live_sync_attempted is False
    assert result.currency == "USD"
    assert "Chase" in result.account_label
    assert result.source_item_id == "src_koru_plaid_chase_checking"


def test_plaid_fixture_rejects_empty_transactions() -> None:
    with pytest.raises(ConnectorError, match="at least one transaction"):
        PlaidFixtureIngestor().ingest(
            {
                "account": {"name": "Chase Business Checking", "currency": "USD"},
                "syncedAt": "2026-07-17T09:40:00-04:00",
                "transactions": [],
            }
        )


def test_engine_quarantines_and_deduplicates_foreign_currency_plaid_fixture(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "plaid.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv")
    before = len(store.fetch_all("SELECT transaction_id FROM transactions"))

    first = engine.ingest_plaid_fixture()
    second = engine.ingest_plaid_fixture()

    assert first.status == "quarantined_currency_mismatch"
    assert first.row_count == 6
    assert second.status == "deduplicated"
    assert len(store.fetch_all("SELECT transaction_id FROM transactions")) == before
    with store.connect() as connection:
        source = connection.execute(
            "SELECT source_type, status, row_count FROM source_items WHERE source_item_id = ?",
            (first.source_item_id,),
        ).fetchone()
        assert source["source_type"] == "plaid_fixture"
        assert source["status"] == "processed"
        assert source["row_count"] == 6
        events = connection.execute(
            """
            SELECT event_type, currency, amount_minor
            FROM provider_transaction_events
            WHERE source_item_id = ?
            ORDER BY provider_transaction_id
            """,
            (first.source_item_id,),
        ).fetchall()
        assert len(events) == 6
        assert {row["event_type"] for row in events} == {"quarantined"}
        assert {row["currency"] for row in events} == {"USD"}
