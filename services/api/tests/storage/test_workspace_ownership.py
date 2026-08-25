from __future__ import annotations

from pathlib import Path

import pytest
from finance_agent.finance import FinanceEngine
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def seeded(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    return store


def test_claim_cannot_supersede_a_claim_from_another_workspace(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id, name, entity_type, currency, timezone, "
            "protected_reserve_minor, data_through, thread_id, model_mode, created_at, updated_at) "
            "VALUES ('ws_other_business', 'Other', 'nz_sole_trader', 'NZD', 'Pacific/Auckland', "
            "0, '2026-08-26', 'thr_other_main', 'local', '2026-08-26', '2026-08-26')"
        )
        connection.execute(
            "INSERT INTO conversation_turns(turn_id, workspace_id, thread_id, role, content, "
            "occurred_at, status, evidence_ids_json, model_mode) VALUES "
            "('turn_other_claim', 'ws_other_business', 'thr_other_main', 'owner', 'Other', "
            "'2026-08-26', 'complete', '[]', 'local')"
        )
        connection.execute(
            "INSERT INTO claims(claim_id, workspace_id, claim_type, statement, source_turn_id, "
            "scope_json, effective_date, recorded_at, status, supersedes_claim_id) VALUES "
            "('claim_other', 'ws_other_business', 'business_context', 'Other claim', "
            "'turn_other_claim', '{}', '2026-08-26', '2026-08-26', 'active', NULL)"
        )
    with pytest.raises(ValueError, match="same workspace"):
        store.record_claim(
            {
                "claimId": "claim_koru_bad_supersession",
                "workspaceId": "ws_koru_studio",
                "claimType": "business_context",
                "statement": "Koru claim",
                "sourceTurnId": "turn_koru_morning_close",
                "scope": {},
                "effectiveDate": "2026-08-26",
                "recordedAt": "2026-08-26",
                "supersedesClaimId": "claim_other",
            }
        )
