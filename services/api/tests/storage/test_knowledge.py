from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from finance_agent.storage.knowledge import (
    EntityType,
    FactBasis,
    FactKind,
    FactStatus,
    KnowledgeEntity,
    KnowledgeFact,
    KnowledgeSource,
    ObjectKind,
    QuestionAxis,
    SourceKind,
    SQLiteKnowledgeStore,
)
from finance_agent.storage.knowledge_compiler import bootstrap_committed_finance
from finance_agent.storage.store import SQLiteStore, canonical_json

NOW = datetime(2026, 7, 17, 9, 30, tzinfo=UTC)
WORKSPACE_ID = "ws_knowledge_test"
THREAD_ID = "thread_knowledge_test"


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "knowledge.sqlite3")
    store.migrate()
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workspaces(
                workspace_id, name, entity_type, currency, timezone,
                protected_reserve_minor, data_through, thread_id, model_mode,
                state_revision, created_at, updated_at
            ) VALUES (?, 'Koru Studio', 'nz_sole_trader', 'NZD', 'Pacific/Auckland',
                      250000, '2026-07-17', ?, 'local', 7, ?, ?)
            """,
            (WORKSPACE_ID, THREAD_ID, NOW.isoformat(), NOW.isoformat()),
        )
    return store


def _seed_committed_finance(store: SQLiteStore) -> None:
    store.record_turn(
        turn_id="turn_owner_policy",
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        role="owner",
        content="Mitre 10 under $500 is business equipment for the studio.",
        occurred_at=NOW.isoformat(),
    )
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO accounts(account_id, workspace_id, name, currency, created_at)
            VALUES ('acct_operating', ?, 'Operating account', 'NZD', ?)
            """,
            (WORKSPACE_ID, NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO source_items(
                source_item_id, workspace_id, source_type, label, digest,
                mapping_version, received_at, status, row_count
            ) VALUES (
                'src_bank_july', ?, 'csv', 'July operating statement', ?,
                'bank-csv-v1', ?, 'processed', 1
            )
            """,
            (WORKSPACE_ID, "a" * 64, NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO source_rows(
                source_row_id, source_item_id, row_number, account_id, occurred_on,
                description, amount_minor, currency, source_status,
                external_reference, mapping_version, row_hash, raw_json
            ) VALUES (
                'row_mitre10', 'src_bank_july', 1, 'acct_operating', '2026-07-16',
                'MITRE 10', -12999, 'NZD', 'posted', 'bank-ref-1',
                'bank-csv-v1', ?, '{}'
            )
            """,
            ("b" * 64,),
        )
        connection.execute(
            """
            INSERT INTO evidence_links(
                evidence_id, workspace_id, source_item_id, source_row_id, label, created_at
            ) VALUES (
                'evd_mitre10', ?, 'src_bank_july', 'row_mitre10',
                'Mitre 10 bank row', ?
            )
            """,
            (WORKSPACE_ID, NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO transactions(
                transaction_id, workspace_id, account_id, source_row_id, evidence_id,
                occurred_on, description, amount_minor, currency, source_status,
                status, classification, category, classification_source, created_at, updated_at
            ) VALUES (
                'txn_mitre10', ?, 'acct_operating', 'row_mitre10', 'evd_mitre10',
                '2026-07-16', 'MITRE 10', -12999, 'NZD', 'posted', 'posted',
                'business', 'Equipment', 'accepted_feedback', ?, ?
            )
            """,
            (WORKSPACE_ID, NOW.isoformat(), NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO claims(
                claim_id, workspace_id, claim_type, statement, source_turn_id,
                scope_json, effective_date, recorded_at, status, supersedes_claim_id
            ) VALUES (
                'claim_mitre10', ?, 'classification_instruction',
                'Mitre 10 under $500 is business equipment for the studio.',
                'turn_owner_policy', ?, '2026-07-17', ?, 'active', NULL
            )
            """,
            (
                WORKSPACE_ID,
                canonical_json({"merchantContains": "MITRE 10", "maximumAmountMinor": 50000}),
                NOW.isoformat(),
            ),
        )
        connection.execute(
            """
            INSERT INTO classification_rules(
                rule_id, workspace_id, merchant_contains, maximum_amount_minor, currency,
                target_classification, target_category, effective_from, priority, active,
                source_turn_id, source_claim_id, created_at, updated_at
            ) VALUES (
                'rule_mitre10', ?, 'MITRE 10', 50000, 'NZD', 'business', 'Equipment',
                '2026-07-17', 100, 1, 'turn_owner_policy', 'claim_mitre10', ?, ?
            )
            """,
            (WORKSPACE_ID, NOW.isoformat(), NOW.isoformat()),
        )
        for finding in (
            (
                "finding_receipt",
                "missing_document",
                "attention",
                "Mitre 10 receipt",
                "The receipt is missing.",
                12999,
                ["evd_mitre10"],
            ),
            (
                "finding_reserve",
                "reserve_risk",
                "critical",
                "July reserve gap",
                "Known outgoing commitments cross the protected reserve.",
                80000,
                ["evd_mitre10"],
            ),
        ):
            connection.execute(
                """
                INSERT INTO findings(
                    finding_id, revision, workspace_id, kind, severity, title, summary,
                    amount_minor, currency, status, evidence_ids_json, state_revision,
                    is_current, created_at, obsoleted_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, 'NZD', 'open', ?, 7, 1, ?, NULL)
                """,
                (
                    finding[0],
                    WORKSPACE_ID,
                    finding[1],
                    finding[2],
                    finding[3],
                    finding[4],
                    finding[5],
                    canonical_json(finding[6]),
                    NOW.isoformat(),
                ),
            )


def test_first_look_compiles_committed_finance_and_one_ranked_question(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _seed_committed_finance(store)
    ledger_before = {
        "transactions": [dict(row) for row in store.fetch_all("SELECT * FROM transactions")],
        "events": [dict(row) for row in store.fetch_all("SELECT * FROM finance_events")],
        "revision": int(
            store.fetch_one(
                "SELECT state_revision FROM workspaces WHERE workspace_id = ?", (WORKSPACE_ID,)
            )["state_revision"]
        ),
    }

    first = bootstrap_committed_finance(
        store,
        workspace_id=WORKSPACE_ID,
        as_of=date(2026, 7, 17),
        recorded_at=NOW,
    )
    axes = first.payload["axes"]
    assert isinstance(axes, dict)
    assert all(axes[axis] for axis in ("who", "what", "where", "when", "why"))
    question = first.payload["highestValueQuestion"]
    assert isinstance(question, dict)
    assert question["findingId"] == "finding_reserve"
    assert question["sourceIds"] == ["finding_reserve", "evd_mitre10"]
    assert "Which planned outgoing" in str(question["prompt"])

    assert store.fetch_one("SELECT COUNT(*) AS count FROM knowledge_owner_statements")["count"] == 1
    assert store.fetch_one("SELECT COUNT(*) AS count FROM knowledge_documents")["count"] == 1
    assert (
        store.fetch_one(
            "SELECT COUNT(*) AS count FROM knowledge_facts WHERE fact_kind = 'relationship'"
        )["count"]
        >= 2
    )
    summary_row = store.fetch_one(
        """
        SELECT source_finding_ids_json FROM knowledge_business_summary_revisions
        WHERE summary_id = ? AND revision = ?
        """,
        (first.summary_id, first.revision),
    )
    assert json.loads(str(summary_row["source_finding_ids_json"])) == ["finding_reserve"]

    repeated = bootstrap_committed_finance(
        store,
        workspace_id=WORKSPACE_ID,
        as_of=date(2026, 7, 17),
        recorded_at=NOW,
    )
    assert repeated.content_hash == first.content_hash
    assert repeated.revision == first.revision
    assert (
        store.fetch_one("SELECT COUNT(*) AS count FROM knowledge_business_summary_revisions")[
            "count"
        ]
        == 1
    )

    ledger_after = {
        "transactions": [dict(row) for row in store.fetch_all("SELECT * FROM transactions")],
        "events": [dict(row) for row in store.fetch_all("SELECT * FROM finance_events")],
        "revision": int(
            store.fetch_one(
                "SELECT state_revision FROM workspaces WHERE workspace_id = ?", (WORKSPACE_ID,)
            )["state_revision"]
        ),
    }
    assert ledger_after == ledger_before

    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE workspaces
            SET state_revision = 8, data_through = '2026-07-18', updated_at = ?
            WHERE workspace_id = ?
            """,
            (NOW.replace(day=18).isoformat(), WORKSPACE_ID),
        )
        connection.execute(
            """
            UPDATE source_items SET status = 'failed', row_count = 2
            WHERE source_item_id = 'src_bank_july'
            """
        )
    refreshed = bootstrap_committed_finance(
        store,
        workspace_id=WORKSPACE_ID,
        as_of=date(2026, 7, 17),
        recorded_at=NOW.replace(day=18),
    )
    assert refreshed.summary_id == first.summary_id
    assert refreshed.revision == first.revision + 1
    assert store.fetch_one("SELECT COUNT(*) AS count FROM knowledge_documents")["count"] == 2
    assert (
        store.fetch_one(
            """
        SELECT COUNT(*) AS count FROM knowledge_entities
        WHERE entity_type = 'organisation'
        """
        )["count"]
        == 1
    )
    assert [
        str(row["status"])
        for row in store.fetch_all(
            """
            SELECT status FROM knowledge_fact_status_events
            WHERE fact_id IN (
                SELECT fact_id FROM knowledge_facts
                WHERE predicate = 'finance.data_through'
            )
            ORDER BY occurred_at, sequence
            """
        )
    ].count("superseded") == 1


def test_candidates_contradictions_supersession_and_task_scoped_retrieval(tmp_path: Path) -> None:
    store = _store(tmp_path)
    knowledge = SQLiteKnowledgeStore(store)
    first_statement = knowledge.record_owner_turn(
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        turn_id="turn_hamilton",
        content="I repair commercial cameras from my Hamilton studio.",
        occurred_at=NOW,
        task_scope="business_profile",
    )
    assert (
        knowledge.record_owner_turn(
            workspace_id=WORKSPACE_ID,
            thread_id=THREAD_ID,
            turn_id="turn_hamilton",
            content="I repair commercial cameras from my Hamilton studio.",
            occurred_at=NOW,
            task_scope="business_profile",
        )
        == first_statement
    )
    source = KnowledgeSource(
        SourceKind.OWNER_TURN,
        first_statement,
        turn_id="turn_hamilton",
    )
    knowledge.record_entity(
        KnowledgeEntity(
            entity_id="entity_koru",
            workspace_id=WORKSPACE_ID,
            entity_type=EntityType.ORGANISATION,
            canonical_name="Koru Studio",
            source=source,
            recorded_at=NOW,
            task_scope="business_profile",
        )
    )
    hamilton = KnowledgeFact(
        fact_id="fact_location_hamilton",
        workspace_id=WORKSPACE_ID,
        question_axis=QuestionAxis.WHERE,
        fact_kind=FactKind.ATTRIBUTE,
        subject_entity_id="entity_koru",
        predicate="business.base_city",
        scope_key="business:base_city",
        object_kind=ObjectKind.TEXT,
        object_text="Hamilton",
        value="Hamilton",
        source=source,
        basis=FactBasis.EXPLICIT,
        confidence=1.0,
        recorded_at=NOW,
        task_scope="business_profile",
    )
    knowledge.record_fact(hamilton)
    knowledge.record_fact(
        KnowledgeFact(
            fact_id="fact_location_auckland_candidate",
            workspace_id=WORKSPACE_ID,
            question_axis=QuestionAxis.WHERE,
            fact_kind=FactKind.ATTRIBUTE,
            subject_entity_id="entity_koru",
            predicate="business.base_city",
            scope_key="business:base_city",
            object_kind=ObjectKind.TEXT,
            object_text="Auckland",
            value="Auckland",
            source=KnowledgeSource(SourceKind.MODEL_CANDIDATE, "model:local:test"),
            basis=FactBasis.INFERRED,
            confidence=0.61,
            recorded_at=NOW,
            task_scope="business_profile",
        ),
        initial_status=FactStatus.CANDIDATE,
    )

    summary = knowledge.current_business_summary(
        workspace_id=WORKSPACE_ID,
        as_of=date(2026, 7, 17),
        generated_at=NOW,
    )
    where_facts = summary.payload["axes"]["where"]
    assert [fact["objectText"] for fact in where_facts] == ["Hamilton"]
    assert summary.payload["highestValueQuestion"] is None
    assert len(summary.payload["openContradictions"]) == 1

    hidden = knowledge.retrieve(
        workspace_id=WORKSPACE_ID,
        query="Auckland",
        task_scope="business_profile",
        as_of=date(2026, 7, 17),
        retrieved_at=NOW,
    )
    assert all(hit.record_id != "fact_location_auckland_candidate" for hit in hidden.hits)
    reviewed = knowledge.retrieve(
        workspace_id=WORKSPACE_ID,
        query="Auckland",
        task_scope="business_profile",
        as_of=date(2026, 7, 17),
        include_candidates=True,
        retrieved_at=NOW,
    )
    assert [hit.record_id for hit in reviewed.hits] == ["fact_location_auckland_candidate"]
    assert reviewed.hits[0].status is FactStatus.CANDIDATE
    assert (
        store.fetch_one("SELECT COUNT(*) AS count FROM knowledge_context_retrieval_receipts")[
            "count"
        ]
        == 2
    )

    owner_context = knowledge.retrieve(
        workspace_id=WORKSPACE_ID,
        query="repair commercial cameras",
        task_scope="business_profile",
        as_of=date(2026, 7, 17),
        retrieved_at=NOW,
        run_id="run_a",
        thread_id=THREAD_ID,
    )
    assert any(hit.record_type == "owner_statement" for hit in owner_context.hits)
    assert all(hit.task_scope in {"business_profile", "general"} for hit in owner_context.hits)
    second_run = knowledge.retrieve(
        workspace_id=WORKSPACE_ID,
        query="repair commercial cameras",
        task_scope="business_profile",
        as_of=date(2026, 7, 17),
        retrieved_at=NOW,
        run_id="run_b",
        thread_id=THREAD_ID,
    )
    assert second_run.receipt_id != owner_context.receipt_id
    assert (
        store.fetch_one(
            """
        SELECT COUNT(*) AS count FROM knowledge_context_retrieval_receipts
        WHERE run_id IS NOT NULL
        """
        )["count"]
        == 2
    )

    general = knowledge.retrieve(
        workspace_id=WORKSPACE_ID,
        query="Hamilton",
        task_scope="general",
        as_of=date(2026, 7, 17),
        retrieved_at=NOW,
        run_id="run_general",
        thread_id=THREAD_ID,
    )
    assert any(hit.record_id == "fact_location_hamilton" for hit in general.hits)
    trimmed = knowledge.retrieve(
        workspace_id=WORKSPACE_ID,
        query="Hamilton repair commercial cameras",
        task_scope="general",
        as_of=date(2026, 7, 17),
        retrieved_at=NOW,
        run_id="run_trimmed",
        thread_id=THREAD_ID,
        max_characters=120,
    )
    packet_receipt = store.fetch_one(
        """
        SELECT selected_ids_json, dropped_ids_json, max_characters,
               packet_characters, packet_hash
        FROM knowledge_context_retrieval_receipts WHERE receipt_id = ?
        """,
        (trimmed.receipt_id,),
    )
    assert json.loads(str(packet_receipt["selected_ids_json"])) == [
        hit.record_id for hit in trimmed.hits
    ]
    assert json.loads(str(packet_receipt["dropped_ids_json"]))
    assert int(packet_receipt["packet_characters"]) <= 120
    assert int(packet_receipt["max_characters"]) == 120
    assert len(str(packet_receipt["packet_hash"])) == 64

    second_statement = knowledge.record_owner_turn(
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        turn_id="turn_tauranga",
        content="I have moved the studio base to Tauranga.",
        occurred_at=NOW.replace(hour=10),
        task_scope="business_profile",
    )
    knowledge.record_fact(
        KnowledgeFact(
            fact_id="fact_location_tauranga",
            workspace_id=WORKSPACE_ID,
            question_axis=QuestionAxis.WHERE,
            fact_kind=FactKind.ATTRIBUTE,
            subject_entity_id="entity_koru",
            predicate="business.base_city",
            scope_key="business:base_city",
            object_kind=ObjectKind.TEXT,
            object_text="Tauranga",
            value="Tauranga",
            source=KnowledgeSource(
                SourceKind.OWNER_TURN,
                second_statement,
                turn_id="turn_tauranga",
            ),
            basis=FactBasis.EXPLICIT,
            confidence=1.0,
            recorded_at=NOW.replace(hour=10),
            task_scope="business_profile",
            supersedes_fact_id="fact_location_hamilton",
        )
    )
    current = knowledge.current_business_summary(
        workspace_id=WORKSPACE_ID,
        as_of=date(2026, 7, 17),
        generated_at=NOW.replace(hour=10),
    )
    assert [fact["objectText"] for fact in current.payload["axes"]["where"]] == ["Tauranga"]
    assert [
        str(row["status"])
        for row in store.fetch_all(
            """
            SELECT status FROM knowledge_fact_status_events
            WHERE fact_id = 'fact_location_hamilton' ORDER BY sequence
            """
        )
    ] == ["active", "superseded"]

    with store.transaction() as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE knowledge_facts SET object_text = 'Changed' WHERE fact_id = ?",
            (hamilton.fact_id,),
        )


def test_knowledge_migration_and_fts_survive_demo_recreate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert (
        store.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_fts'"
        )
        is not None
    )

    store.recreate()

    assert [
        int(row["version"])
        for row in store.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    ] == [1, 2, 3, 4, 5]
    assert (
        store.fetch_one(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_fts'"
        )
        is not None
    )
