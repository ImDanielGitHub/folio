from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "apply_audit_programme_v2.py"
spec = importlib.util.spec_from_file_location("audit_programme_v2", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load audit programme v2")
programme_v2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(programme_v2)

# Repair the RFC 5987 filename expression after embedding it in the generator string.
path = ROOT / "services/api/src/finance_agent/api/http_security.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    "    return f'{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{encoded}'\n",
    "    return f\"{disposition}; filename=\\\"{fallback}\\\"; filename*=UTF-8''{encoded}\"\n",
)
path.write_text(content, encoding="utf-8")

# Keep Daily Close idempotent across processing-status transitions while still
# hashing every material source and policy input.
path = ROOT / "services/api/src/finance_agent/jobs/daily_close.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    "            SELECT source_item_id, source_type, digest, mapping_version, status, row_count\n",
    "            SELECT source_item_id, source_type, digest, mapping_version, row_count\n",
)
path.write_text(content, encoding="utf-8")

# Align pre-existing Plaid fixture tests with foreign-currency quarantine.
path = ROOT / "services/api/tests/connectors/test_plaid_fixture.py"
content = path.read_text(encoding="utf-8")
old = '''def test_engine_persists_and_deduplicates_plaid_fixture(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "plaid.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv")
    first = engine.ingest_plaid_fixture()
    second = engine.ingest_plaid_fixture()

    assert first.status == "ingested"
    assert first.row_count == 6
    assert second.status == "deduplicated"
    with store.connect() as connection:
        source = connection.execute(
            "SELECT source_type, row_count FROM source_items WHERE source_item_id = ?",
            (first.source_item_id,),
        ).fetchone()
        assert source["source_type"] == "plaid_fixture"
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
'''
new = '''def test_engine_quarantines_and_deduplicates_foreign_currency_plaid_fixture(
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
'''
if old not in content:
    raise RuntimeError("Plaid fixture test block changed unexpectedly")
path.write_text(content.replace(old, new, 1), encoding="utf-8")

# Deduplicate quarantined Plaid rows by stable provider identity before creating
# a new immutable source receipt. A repeated provider page must not create a new
# source merely because the sync timestamp changed.
path = ROOT / "services/api/src/finance_agent/api/services.py"
content = path.read_text(encoding="utf-8")
old = '''        if any(account.currency != "NZD" for account in accounts):
            primary = accounts[0]
            payload = {
'''
new = '''        if any(account.currency != "NZD" for account in accounts):
            primary = accounts[0]
            existing_provider_references = {
                str(row["provider_transaction_id"])
                for row in self.store.fetch_all(
                    """
                    SELECT provider_transaction_id
                    FROM provider_transaction_events
                    WHERE workspace_id = ? AND provider = 'plaid'
                      AND provider_account_id = ?
                    """,
                    (WORKSPACE_ID, primary.account_id or PLAID_ACCOUNT_ID),
                )
            }
            new_transactions = tuple(
                transaction
                for transaction in transactions
                if transaction.external_reference not in existing_provider_references
            )
            if not new_transactions:
                self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)
                return {
                    "sourceItemId": None,
                    "status": "no_new_transactions",
                    "sourceSha256": None,
                    "accountCount": len(accounts),
                    "transactionCount": len(transactions),
                    "rowCount": 0,
                    "providerEventCount": 0,
                    "providerCurrency": primary.currency,
                    "ledgerCommitted": False,
                    "quarantineReason": "workspace_currency_mismatch",
                    "settledOnly": True,
                    "liveSyncAttempted": True,
                    "externalCallsMade": True,
                }
            payload = {
'''
if old not in content:
    raise RuntimeError("Plaid quarantine service block changed unexpectedly")
content = content.replace(old, new, 1)
foreign_start = content.index('        if any(account.currency != "NZD" for account in accounts):')
foreign_end = content.index("\n        async with self._lock:", foreign_start)
block = content[foreign_start:foreign_end]
block = block.replace(
    "                    for transaction in transactions\n",
    "                    for transaction in new_transactions\n",
    1,
)
content = content[:foreign_start] + block + content[foreign_end:]
path.write_text(content, encoding="utf-8")

path = ROOT / "services/api/tests/connectors/test_plaid_live.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    "async def test_live_sync_paginates_and_commits_exact_minor_units(tmp_path: Path) -> None:\n",
    "async def test_live_sync_paginates_and_quarantines_foreign_currency_minor_units(\n    tmp_path: Path,\n) -> None:\n",
    1,
)
old = '''        result = await services.sync_plaid()
        assert result["status"] == "ingested"
        assert result["accountCount"] == 1
        assert result["transactionCount"] == 2
        assert result["rowCount"] == 2
        assert result["liveSyncAttempted"] is True
        assert result["externalCallsMade"] is True
        assert len(requests) == 5

        rows = services.store.fetch_all(
            """
            SELECT amount_minor, source_status, external_reference
            FROM source_rows WHERE mapping_version = ?
            ORDER BY occurred_on, external_reference
            """,
            (PLAID_MAPPING_VERSION,),
        )
        # Plaid positive outflow → Folio negative minor units.
        assert [int(row["amount_minor"]) for row in rows] == [-1234, -29]
        assert [str(row["source_status"]) for row in rows] == ["posted", "posted"]

        repeated = await services.sync_plaid()
        assert repeated["status"] == "no_new_transactions"
        assert repeated["rowCount"] == 0
'''
new = '''        result = await services.sync_plaid()
        assert result["status"] == "quarantined_currency_mismatch"
        assert result["accountCount"] == 1
        assert result["transactionCount"] == 2
        assert result["rowCount"] == 0
        assert result["providerEventCount"] == 2
        assert result["providerCurrency"] == "USD"
        assert result["ledgerCommitted"] is False
        assert result["quarantineReason"] == "workspace_currency_mismatch"
        assert result["liveSyncAttempted"] is True
        assert result["externalCallsMade"] is True
        assert len(requests) == 5

        rows = services.store.fetch_all(
            """
            SELECT amount_minor, currency, event_type
            FROM provider_transaction_events
            WHERE provider = 'plaid'
            ORDER BY provider_transaction_id
            """
        )
        # Provider cents are preserved exactly but never relabelled as NZD ledger cents.
        assert [int(row["amount_minor"]) for row in rows] == [-1234, -29]
        assert [str(row["currency"]) for row in rows] == ["USD", "USD"]
        assert [str(row["event_type"]) for row in rows] == ["quarantined", "quarantined"]
        assert services.store.fetch_all(
            "SELECT source_row_id FROM source_rows WHERE mapping_version = ?",
            (PLAID_MAPPING_VERSION,),
        ) == []

        repeated = await services.sync_plaid()
        assert repeated["status"] == "no_new_transactions"
        assert repeated["rowCount"] == 0
        assert len(services.store.fetch_all("SELECT event_id FROM provider_transaction_events")) == 2
'''
if old not in content:
    raise RuntimeError("Plaid live test block changed unexpectedly")
path.write_text(content.replace(old, new, 1), encoding="utf-8")

# A newly committed Telegram source is material, so the next close completes;
# only an unchanged follow-up close is a no-op.
path = ROOT / "services/api/tests/integration/test_golden_api.py"
content = path.read_text(encoding="utf-8")
old = '''        second_close = await client.post(
            "/v1/jobs/daily-close",
            json={"workspaceId": "ws_koru_studio"},
        )
        assert second_close.json()["status"] == "no_op"
'''
new = '''        second_close = await client.post(
            "/v1/jobs/daily-close",
            json={"workspaceId": "ws_koru_studio"},
        )
        assert second_close.json()["status"] == "completed"
        unchanged_close = await client.post(
            "/v1/jobs/daily-close",
            json={"workspaceId": "ws_koru_studio"},
        )
        assert unchanged_close.json()["status"] == "no_op"
'''
if old not in content:
    raise RuntimeError("Golden close expectation changed unexpectedly")
path.write_text(content.replace(old, new, 1), encoding="utf-8")

path = ROOT / "services/api/tests/storage/test_knowledge.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    "    ] == [1, 2, 3, 4, 5, 6, 7]\n",
    "    ] == [1, 2, 3, 4, 5, 6, 7, 8]\n",
    1,
)
path.write_text(content, encoding="utf-8")

print("Audit programme v3 compatibility and regression fixes applied")
