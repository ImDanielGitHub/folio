from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def engine(tmp_path: Path) -> FinanceEngine:
    value = FinanceEngine(SQLiteStore(tmp_path / "folio.sqlite3"))
    value.reset_demo(CSV)
    return value


def test_plaid_usd_is_quarantined_outside_the_nzd_ledger(tmp_path: Path) -> None:
    value = engine(tmp_path)
    before = len(value.store.fetch_all("SELECT * FROM transactions"))
    result = value.ingest_plaid_fixture()
    after = len(value.store.fetch_all("SELECT * FROM transactions"))
    events = value.store.fetch_all("SELECT * FROM provider_transaction_events")
    assert result.status == "quarantined_currency_mismatch"
    assert after == before
    assert len(events) == result.row_count
    assert {str(row["currency"]) for row in events} == {"USD"}


def test_daily_close_identity_changes_for_non_csv_sources_and_rules(tmp_path: Path) -> None:
    value = engine(tmp_path)
    service = DailyCloseService(value)
    initial = service.identity().input_hash
    value.ingest_akahu_fixture()
    after_provider = service.identity().input_hash
    assert after_provider != initial
    DailyCloseService(value).run()
    value.create_classification_rule(
        merchant_contains="MITRE 10",
        maximum_amount_minor=50000,
        target_classification="business",
        target_category="materials",
        effective_from="2026-07-01",
        source_turn_id="turn_audit_rule",
        owner_statement="MITRE 10 was business materials.",
    )
    assert service.identity().input_hash != after_provider


def test_daily_close_uses_injected_clock_and_computed_counts(tmp_path: Path) -> None:
    value = engine(tmp_path)
    fixed = datetime(2026, 8, 26, 9, 30, tzinfo=UTC)
    result = DailyCloseService(value, clock=lambda: fixed).run()
    row = value.store.fetch_one("SELECT * FROM job_runs WHERE run_id = ?", (result.run_id,))
    assert row is not None
    assert str(row["started_at"]) == fixed.isoformat()
    assert result.new_findings == len(value.get_snapshot()["findings"])
    assert result.new_artifacts == len(value.get_snapshot()["artifacts"])


def test_each_conversation_turn_preserves_its_model_mode(tmp_path: Path) -> None:
    value = engine(tmp_path)
    value.store.migrate()
    # The demo frame is composed by the normal service; direct turn persistence is enough here.
    value.store.record_turn(
        turn_id="turn_audit_local",
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        role="owner",
        content="Local turn",
        occurred_at="2026-08-26T09:00:00+00:00",
        model_mode="local",
    )
    value.store.record_turn(
        turn_id="turn_audit_cloud",
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        role="agent",
        content="Cloud turn",
        occurred_at="2026-08-26T09:01:00+00:00",
        model_mode="cloud",
    )
    rows = value.store.fetch_all(
        "SELECT turn_id, model_mode FROM conversation_turns "
        "WHERE turn_id LIKE 'turn_audit_%' ORDER BY occurred_at"
    )
    assert [(str(row["turn_id"]), str(row["model_mode"])) for row in rows] == [
        ("turn_audit_local", "local"),
        ("turn_audit_cloud", "cloud"),
    ]
