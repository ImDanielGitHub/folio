from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch_migration() -> None:
    path = ROOT / "services/api/src/finance_agent/storage/migrations.py"
    content = path.read_text()
    old = '''        CREATE UNIQUE INDEX transfer_one_active_debit
            ON transfer_match_events(debit_transaction_id)
            WHERE event_type = 'confirmed' AND event_id NOT IN (
                SELECT reverses_event_id FROM transfer_match_events
                WHERE event_type = 'undone' AND reverses_event_id IS NOT NULL
            );

        CREATE INDEX transfer_candidates_status
'''
    new = '''        CREATE INDEX transfer_events_debit
            ON transfer_match_events(workspace_id, debit_transaction_id, occurred_at);
        CREATE INDEX transfer_events_credit
            ON transfer_match_events(workspace_id, credit_transaction_id, occurred_at);
        CREATE INDEX transfer_events_reversal
            ON transfer_match_events(reverses_event_id, event_type);

        CREATE INDEX transfer_candidates_status
'''
    if old not in content:
        raise RuntimeError("invalid transfer partial index block not found")
    path.write_text(content.replace(old, new, 1))


def patch_service() -> None:
    path = ROOT / "services/api/src/finance_agent/finance/transfers.py"
    content = path.read_text()
    marker = '''                if str(candidate["status"]) not in {"proposed", "confirmed"}:
                    raise ValueError("only a proposed transfer candidate can be confirmed")
                debit_before = {
'''
    replacement = '''                if str(candidate["status"]) not in {"proposed", "confirmed"}:
                    raise ValueError("only a proposed transfer candidate can be confirmed")
                conflict = connection.execute(
                    """
                    SELECT event_id FROM transfer_match_events active
                    WHERE active.workspace_id = ? AND active.event_type = 'confirmed'
                      AND active.event_id NOT IN (
                        SELECT reverses_event_id FROM transfer_match_events
                        WHERE event_type = 'undone' AND reverses_event_id IS NOT NULL
                      )
                      AND (
                        active.debit_transaction_id IN (?, ?)
                        OR active.credit_transaction_id IN (?, ?)
                      )
                    LIMIT 1
                    """,
                    (
                        workspace_id,
                        candidate["debit_transaction_id"],
                        candidate["credit_transaction_id"],
                        candidate["debit_transaction_id"],
                        candidate["credit_transaction_id"],
                    ),
                ).fetchone()
                if conflict is not None:
                    raise ValueError(
                        "one or both transactions already belong to an active transfer match"
                    )
                debit_before = {
'''
    if marker not in content:
        raise RuntimeError("transfer confirmation marker not found")
    path.write_text(content.replace(marker, replacement, 1))


def patch_test() -> None:
    path = ROOT / "services/api/tests/finance/test_internal_transfer_matching.py"
    content = path.read_text()
    addition = '''

def test_transaction_cannot_join_two_active_confirmed_pairs(tmp_path: Path) -> None:
    store, _engine, service = setup(tmp_path)
    add_transaction(
        store,
        transaction_id="txn_second_credit",
        account_id="acct_koru_savings",
        occurred_on="2026-08-21",
        description="Second possible transfer credit",
        amount_minor=50000,
    )
    candidates = service.scan("ws_koru_studio")
    first = next(
        value for value in candidates
        if value["debit"]["transactionId"] == "txn_transfer_out"
        and value["credit"]["transactionId"] == "txn_transfer_in"
    )
    second = next(
        value for value in candidates
        if value["debit"]["transactionId"] == "txn_transfer_out"
        and value["credit"]["transactionId"] == "txn_second_credit"
    )
    service.confirm(
        workspace_id="ws_koru_studio",
        candidate_id=str(first["candidateId"]),
        reason="Owner confirmed the first pair.",
    )
    try:
        service.confirm(
            workspace_id="ws_koru_studio",
            candidate_id=str(second["candidateId"]),
            reason="Attempt a conflicting pair.",
        )
    except ValueError as exc:
        assert "active transfer match" in str(exc)
    else:
        raise AssertionError("one transaction joined two active transfer pairs")
'''
    if "test_transaction_cannot_join_two_active_confirmed_pairs" not in content:
        path.write_text(content.rstrip() + addition + "\n")


if __name__ == "__main__":
    patch_migration()
    patch_service()
    patch_test()
    print("internal transfer uniqueness correction applied")
