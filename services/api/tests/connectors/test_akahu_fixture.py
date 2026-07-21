"""Offline Akahu fixture ingestion never reaches the network."""

from __future__ import annotations

from pathlib import Path

import pytest

from finance_agent.connectors.akahu_fixture import AkahuFixtureIngestor
from finance_agent.connectors.base import ConnectorError
from finance_agent.finance.service import FinanceEngine
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]


def test_akahu_fixture_ingestor_loads_demo_file() -> None:
    result = AkahuFixtureIngestor().ingest()
    assert result.row_count == 6
    assert result.live_sync_attempted is False
    assert "ANZ Everyday" in result.account_label
    assert result.source_item_id == "src_koru_akahu_anz_everyday"


def test_akahu_fixture_rejects_empty_transactions() -> None:
    with pytest.raises(ConnectorError, match="at least one transaction"):
        AkahuFixtureIngestor().ingest(
            {
                "account": {"name": "ANZ Everyday", "currency": "NZD"},
                "syncedAt": "2026-07-17T09:40:00+12:00",
                "transactions": [],
            }
        )


def test_engine_persists_and_deduplicates_akahu_fixture(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "akahu.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv")
    first = engine.ingest_akahu_fixture()
    second = engine.ingest_akahu_fixture()

    assert first.status == "ingested"
    assert first.row_count == 6
    assert second.status == "deduplicated"
    with store.connect() as connection:
        source = connection.execute(
            "SELECT source_type, row_count FROM source_items WHERE source_item_id = ?",
            (first.source_item_id,),
        ).fetchone()
        assert source["source_type"] == "akahu_fixture"
        assert source["row_count"] == 6
        count = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM transactions AS transaction_row
            JOIN source_rows AS source_row
              ON source_row.source_row_id = transaction_row.source_row_id
            WHERE source_row.source_item_id = ?
            """,
            (first.source_item_id,),
        ).fetchone()["count"]
        assert count == 6
