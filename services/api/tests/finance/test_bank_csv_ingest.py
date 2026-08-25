from __future__ import annotations

from pathlib import Path

import pytest
from finance_agent.finance import CSVIngestError, FinanceEngine
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
DEMO = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"
BANK_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "bank"


def _seeded_engine(tmp_path: Path) -> tuple[SQLiteStore, FinanceEngine]:
    store = SQLiteStore(tmp_path / "bank-import.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(DEMO)
    return store, engine


def _imported_rows(store: SQLiteStore, source_item_id: str) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in store.fetch_all(
            """
            SELECT source_row_id, account_id, occurred_on, description, amount_minor,
                   currency, source_status, external_reference, mapping_version
            FROM source_rows
            WHERE source_item_id = ?
            ORDER BY row_number
            """,
            (source_item_id,),
        )
    ]


def test_anz_debit_credit_statement_maps_to_existing_account(tmp_path: Path) -> None:
    store, engine = _seeded_engine(tmp_path)

    result = engine.ingest_csv(BANK_FIXTURES / "anz-debit-credit.csv")
    rows = _imported_rows(store, result.source_item_id)

    assert result.row_count == 2
    assert result.mapping_version == "bank_csv@1+nz_bank_debit_credit@1"
    assert [row["account_id"] for row in rows] == [
        "acct_koru_business",
        "acct_koru_business",
    ]
    assert [row["occurred_on"] for row in rows] == ["2026-07-17", "2026-07-18"]
    assert [row["amount_minor"] for row in rows] == [-8640, 125000]
    assert [row["currency"] for row in rows] == ["NZD", "NZD"]
    assert [row["external_reference"] for row in rows] == [
        "ANZ-20260717-001",
        "ANZ-20260718-002",
    ]
    assert all(str(row["source_row_id"]).startswith("row_") for row in rows)


def test_asb_signed_amount_statement_composes_description(tmp_path: Path) -> None:
    store, engine = _seeded_engine(tmp_path)

    result = engine.ingest_csv(BANK_FIXTURES / "asb-signed-amount.csv")
    rows = _imported_rows(store, result.source_item_id)

    assert result.mapping_version == "bank_csv@1+nz_bank_signed_amount@1"
    assert [row["description"] for row in rows] == [
        "ADOBE — Monthly plan",
        "TUI CLIENT — Invoice 1042",
    ]
    assert [row["amount_minor"] for row in rows] == [-2499, 45000]
    assert [row["external_reference"] for row in rows] == [
        "ASB-20260717-001",
        "ASB-20260718-002",
    ]


def test_malformed_statement_rolls_back_every_row(tmp_path: Path) -> None:
    store, engine = _seeded_engine(tmp_path)
    before = {
        table: store.fetch_one(f"SELECT COUNT(*) AS count FROM {table}")["count"]
        for table in ("source_items", "source_rows", "transactions")
    }

    with pytest.raises(CSVIngestError, match="row 2: only NZD is supported"):
        engine.ingest_csv(BANK_FIXTURES / "malformed-debit-credit.csv")

    after = {
        table: store.fetch_one(f"SELECT COUNT(*) AS count FROM {table}")["count"]
        for table in ("source_items", "source_rows", "transactions")
    }
    assert after == before


def test_repeat_practical_statement_import_is_deduplicated(tmp_path: Path) -> None:
    store, engine = _seeded_engine(tmp_path)
    fixture = BANK_FIXTURES / "asb-signed-amount.csv"

    first = engine.ingest_csv(fixture)
    second = engine.ingest_csv(fixture)

    assert first.duplicate_import is False
    assert second.duplicate_import is True
    assert second.source_item_id == first.source_item_id
    assert second.transaction_ids == first.transaction_ids
    assert len(_imported_rows(store, first.source_item_id)) == 2
