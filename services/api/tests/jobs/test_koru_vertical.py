# ruff: noqa: E501
from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.jobs import DailyCloseService, DailyCloseWorker
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"
TELEGRAM = ROOT / "fixtures" / "demo" / "telegram-update.json"


def test_koru_vertical_is_exact_idempotent_and_reversible(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "koru.sqlite3")
    engine = FinanceEngine(store)
    imported = engine.reset_demo(CSV, telegram_update_path=TELEGRAM)

    assert imported.source_sha256 == hashlib.sha256(CSV.read_bytes()).hexdigest()
    assert imported.row_count == 10
    duplicate_import = engine.ingest_csv(
        CSV,
        source_item_id="src_koru_bank_csv_20260717",
        mapping_version="koru_bank_csv@1",
    )
    assert duplicate_import.duplicate_import is True

    worker = DailyCloseWorker(DailyCloseService(engine))
    first = worker.tick()
    second = worker.tick()
    assert (first.status, first.new_findings, first.new_artifacts) == ("completed", 3, 2)
    assert (
        second.status,
        second.new_findings,
        second.new_artifacts,
        second.new_owner_messages,
    ) == (
        "no_op",
        0,
        0,
        0,
    )
    assert len(store.fetch_all("SELECT * FROM job_runs")) == 1
    assert len(store.fetch_all("SELECT * FROM job_stage_runs")) == 10

    snapshot = engine.get_snapshot()
    assert snapshot["totals"] == {
        "asOf": "2026-07-17T08:00:00+12:00",
        "currency": "NZD",
        "currentBalanceMinor": 504576,
        "protectedReserveMinor": 200000,
        "businessIncomeMinor": 725000,
        "businessExpenseMinor": 139499,
        "personalExpenseMinor": 62450,
        "unresolvedExpenseMinor": 18475,
        "projectedLowPointMinor": 190077,
        "reserveShortfallMinor": 9923,
    }
    assert {finding["findingId"] for finding in snapshot["findings"]} == {
        "finding_koru_missing_receipt",
        "finding_koru_duplicate_pending",
        "finding_koru_reserve_risk",
    }
    scenario = engine.get_cash_scenario_surface()
    assert [point["balanceMinor"] for point in scenario["blocks"][0]["points"]] == [
        504576,
        629576,
        509576,
        500577,
        493077,
        193077,
        190077,
    ]

    correction = engine.create_classification_rule(
        merchant_contains="MITRE 10",
        maximum_amount_minor=50000,
        target_classification="business",
        target_category="client_fit_out_materials",
        effective_from="2026-07-01",
        source_turn_id="turn_koru_owner_mitre10",
        owner_statement=(
            "Mitre 10 was materials for a client fit-out. Apply this only below NZD 500."
        ),
    )
    assert correction.event["scopeJson"]["transactionIds"] == ["txn_koru_006"]
    assert correction.snapshot["totals"]["businessExpenseMinor"] == 157974
    assert correction.snapshot["totals"]["unresolvedExpenseMinor"] == 0
    assert correction.snapshot["totals"]["projectedLowPointMinor"] == 190077

    html_type, html_bytes, _ = engine.get_artifact("artifact_koru_owner_pack_html")
    pdf_type, pdf_bytes, _ = engine.get_artifact("artifact_koru_owner_pack_pdf")
    assert html_type.startswith("text/html")
    assert b"Preparatory working material" in html_bytes
    assert b"NZD 1,579.74" in html_bytes
    assert pdf_type == "application/pdf"
    assert pdf_bytes.startswith(b"%PDF-")
    dto_hashes = {
        row["dto_hash"]
        for row in store.fetch_all("SELECT dto_hash FROM artifacts WHERE is_current = 1")
    }
    assert len(dto_hashes) == 1

    undo = engine.undo_event("evt_koru_rule_mitre10")
    assert undo["snapshotId"] == "snap_koru_after_undo"
    assert engine.get_snapshot()["totals"]["unresolvedExpenseMinor"] == 18475
    redo = engine.redo_event("evt_koru_rule_mitre10_undo")
    assert redo.event["scopeJson"]["transactionIds"] == ["txn_koru_006"]
    assert engine.get_snapshot()["totals"]["businessExpenseMinor"] == 157974
    assert len(store.fetch_all("SELECT * FROM finance_events")) == 3
    assert len(store.fetch_all("SELECT * FROM artifacts WHERE is_current = 1")) == 2
    assert len(store.fetch_all("SELECT * FROM outbox_messages WHERE status = 'queued'")) == 1

    with store.transaction() as connection:
        try:
            connection.execute(
                "UPDATE source_rows SET description = 'changed' WHERE source_row_id = 'row_koru_001'"
            )
        except sqlite3.IntegrityError as error:
            assert "immutable" in str(error)
        else:
            raise AssertionError("immutable source row accepted an update")
