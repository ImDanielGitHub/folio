from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, value: str) -> None:
    (ROOT / path).write_text(value, encoding="utf-8")


def replace_once(value: str, old: str, new: str, *, label: str) -> str:
    count = value.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return value.replace(old, new, 1)


def replace_regex(value: str, pattern: str, replacement: str, *, label: str) -> str:
    updated, count = re.subn(pattern, replacement, value, count=1, flags=re.S)
    if count != 1:
        raise RuntimeError(f"{label}: expected one regex match, found {count}")
    return updated


def patch_migrations() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    value = read(path)
    if 'name="foreign_currency_quarantine"' in value:
        return
    addition = r'''
    Migration(
        version=20,
        name="foreign_currency_quarantine",
        sql="""
        CREATE TABLE provider_import_quarantine (
            quarantine_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            provider TEXT NOT NULL CHECK (provider IN ('plaid')),
            source_item_id TEXT NOT NULL,
            source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
            mapping_version TEXT NOT NULL,
            provider_currency TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('quarantined', 'released', 'discarded')),
            created_at TEXT NOT NULL,
            UNIQUE (workspace_id, provider, source_digest, mapping_version)
        );

        CREATE INDEX provider_quarantine_workspace_status
            ON provider_import_quarantine(workspace_id, status, created_at);

        UPDATE transactions
        SET status = 'ignored',
            classification = 'unresolved',
            category = NULL,
            classification_source = 'unclassified',
            rule_id = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE source_row_id IN (
            SELECT source_rows.source_row_id
            FROM source_rows
            JOIN source_items USING (source_item_id)
            WHERE source_items.source_type = 'plaid_fixture'
        );
        """,
    ),
    Migration(
        version=21,
        name="conversation_turn_model_mode",
        sql="""
        ALTER TABLE conversation_turns
            ADD COLUMN model_mode TEXT NOT NULL DEFAULT 'local'
            CHECK (model_mode IN ('local', 'hybrid', 'cloud'));
        """,
    ),
'''
    stripped = value.rstrip()
    if not stripped.endswith(")"):
        raise RuntimeError("migrations.py does not end with the migration tuple")
    write(path, stripped[:-1] + addition + ")\n")


def patch_store() -> None:
    path = "services/api/src/finance_agent/storage/store.py"
    value = read(path)
    value = replace_once(
        value,
        """        status: str = \"complete\",\n        evidence_ids: Sequence[str] = (),\n    ) -> None:\n""",
        """        status: str = \"complete\",\n        evidence_ids: Sequence[str] = (),\n        model_mode: str = \"local\",\n    ) -> None:\n""",
        label="record_turn signature",
    )
    value = replace_once(
        value,
        """                expected = (\n                    workspace_id,\n                    thread_id,\n                    role,\n                    content,\n                )\n                actual = (\n                    existing[\"workspace_id\"],\n                    existing[\"thread_id\"],\n                    existing[\"role\"],\n                    existing[\"content\"],\n                )\n""",
        """                expected = (\n                    workspace_id,\n                    thread_id,\n                    role,\n                    content,\n                    model_mode,\n                )\n                actual = (\n                    existing[\"workspace_id\"],\n                    existing[\"thread_id\"],\n                    existing[\"role\"],\n                    existing[\"content\"],\n                    existing[\"model_mode\"],\n                )\n""",
        label="record_turn idempotency",
    )
    value = replace_once(
        value,
        """                    status, evidence_ids_json\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n""",
        """                    status, evidence_ids_json, model_mode\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n""",
        label="record_turn insert columns",
    )
    value = replace_once(
        value,
        """                    status,\n                    canonical_json(list(evidence_ids)),\n                ),\n""",
        """                    status,\n                    canonical_json(list(evidence_ids)),\n                    model_mode,\n                ),\n""",
        label="record_turn insert values",
    )
    value = replace_once(
        value,
        """            supersedes = claim.get(\"supersedesClaimId\")\n            if supersedes is not None:\n                connection.execute(\n                    \"UPDATE claims SET status = 'superseded' WHERE claim_id = ?\",\n                    (supersedes,),\n                )\n""",
        """            supersedes = claim.get(\"supersedesClaimId\")\n            if supersedes is not None:\n                previous = connection.execute(\n                    \"SELECT workspace_id FROM claims WHERE claim_id = ?\",\n                    (supersedes,),\n                ).fetchone()\n                if previous is None:\n                    raise ValueError(f\"unknown superseded claim: {supersedes}\")\n                if str(previous[\"workspace_id\"]) != str(claim[\"workspaceId\"]):\n                    raise ValueError(\"a claim cannot supersede a claim from another workspace\")\n                connection.execute(\n                    \"UPDATE claims SET status = 'superseded' WHERE claim_id = ?\",\n                    (supersedes,),\n                )\n""",
        label="claim workspace guard",
    )
    value = replace_once(
        value,
        """            if existing is not None and str(existing[\"frame_json\"]) == encoded:\n                return\n            connection.execute(\n""",
        """            if existing is not None:\n                ownership = connection.execute(\n                    \"SELECT workspace_id, thread_id FROM dialogue_frames WHERE frame_id = ?\",\n                    (frame[\"frameId\"],),\n                ).fetchone()\n                assert ownership is not None\n                if (\n                    str(ownership[\"workspace_id\"]) != str(frame[\"workspaceId\"])\n                    or str(ownership[\"thread_id\"]) != str(frame[\"threadId\"])\n                ):\n                    raise ValueError(\"a dialogue frame id cannot move between workspaces or threads\")\n                if str(existing[\"frame_json\"]) == encoded:\n                    return\n            connection.execute(\n""",
        label="dialogue frame ownership guard",
    )
    write(path, value)


def patch_conversations() -> None:
    path = "services/api/src/finance_agent/storage/conversations.py"
    value = read(path)
    value = replace_once(
        value,
        """            occurred_at=turn.occurred_at.isoformat(),\n        )\n""",
        """            occurred_at=turn.occurred_at.isoformat(),\n            model_mode=turn.mode,\n        )\n""",
        label="append turn model mode",
    )
    value = replace_once(
        value,
        """            SELECT turn_id, role, content, occurred_at\n""",
        """            SELECT turn_id, role, content, occurred_at, model_mode\n""",
        label="recent turns model mode column",
    )
    value = replace_regex(
        value,
        r"\n        mode_row = self\.store\.fetch_one\(.*?\n        mode = str\(mode_row\[\"model_mode\"\]\) if mode_row else \"local\"\n",
        "\n",
        label="remove retrospective workspace mode",
    )
    value = replace_once(
        value,
        """                mode=mode,\n""",
        """                mode=str(row[\"model_mode\"]),\n""",
        label="use turn model mode",
    )
    write(path, value)


def patch_finance_service() -> None:
    path = "services/api/src/finance_agent/finance/service.py"
    value = read(path)
    segment_start = value.index("    def ingest_plaid_fixture(")
    segment_end = value.index("    @staticmethod\n    def _transaction_from_row", segment_start)
    segment = value[segment_start:segment_end]
    segment = replace_once(
        segment,
        """                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NZD', ?, 'pending', 'unresolved', NULL,\n""",
        """                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NZD', ?, 'ignored', 'unresolved', NULL,\n""",
        label="quarantine Plaid transaction status",
    )
    segment = replace_once(
        segment,
        """        return parsed\n\n""",
        """            quarantine_payload = {\n                \"accountLabel\": parsed.account_label,\n                \"syncedAt\": parsed.synced_at,\n                \"providerCurrency\": parsed.currency,\n                \"transactionCount\": parsed.row_count,\n                \"externalReferences\": [\n                    transaction.external_reference for transaction in parsed.transactions\n                ],\n            }\n            connection.execute(\n                \"\"\"\n                INSERT INTO provider_import_quarantine(\n                    quarantine_id, workspace_id, provider, source_item_id, source_digest,\n                    mapping_version, provider_currency, payload_json, reason, status, created_at\n                ) VALUES (?, ?, 'plaid', ?, ?, ?, ?, ?, ?, 'quarantined', ?)\n                ON CONFLICT(workspace_id, provider, source_digest, mapping_version) DO NOTHING\n                \"\"\",\n                (\n                    stable_id(\"quarantine\", parsed.digest, version),\n                    WORKSPACE_ID,\n                    parsed.source_item_id,\n                    parsed.digest,\n                    version,\n                    parsed.currency,\n                    canonical_json(quarantine_payload),\n                    (\n                        \"Folio's deterministic ledger is NZD-only; provider amounts are \"\n                        \"retained as evidence but excluded from ledger totals until a reviewed \"\n                        \"foreign-exchange conversion is committed.\"\n                    ),\n                    parsed.synced_at,\n                ),\n            )\n        return parsed\n\n""",
        label="record Plaid quarantine",
    )
    write(path, value[:segment_start] + segment + value[segment_end:])


def patch_api_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    value = read(path)
    value = replace_once(
        value,
        """                \"status\": result.status,\n                \"accountLabel\": result.account_label,\n                \"syncedAt\": result.synced_at,\n                \"rowCount\": result.row_count,\n                \"sourceSha256\": result.digest,\n                \"providerCurrency\": result.currency,\n                \"liveSyncAttempted\": result.live_sync_attempted,\n""",
        """                \"status\": (\n                    \"quarantined\" if result.currency != \"NZD\" else result.status\n                ),\n                \"accountLabel\": result.account_label,\n                \"syncedAt\": result.synced_at,\n                \"rowCount\": result.row_count,\n                \"sourceSha256\": result.digest,\n                \"providerCurrency\": result.currency,\n                \"ledgerRowsCommitted\": (\n                    0 if result.currency != \"NZD\" else result.row_count\n                ),\n                \"liveSyncAttempted\": result.live_sync_attempted,\n""",
        label="fixture quarantine receipt",
    )
    value = replace_once(
        value,
        """                \"status\": imported.status,\n                \"sourceSha256\": imported.digest,\n                \"accountCount\": len(accounts),\n                \"transactionCount\": len(transactions),\n                \"rowCount\": imported.row_count,\n                \"settledOnly\": True,\n""",
        """                \"status\": (\n                    \"quarantined\" if imported.currency != \"NZD\" else imported.status\n                ),\n                \"sourceSha256\": imported.digest,\n                \"accountCount\": len(accounts),\n                \"transactionCount\": len(transactions),\n                \"rowCount\": imported.row_count,\n                \"providerCurrency\": imported.currency,\n                \"ledgerRowsCommitted\": (\n                    0 if imported.currency != \"NZD\" else imported.row_count\n                ),\n                \"settledOnly\": True,\n""",
        label="live quarantine receipt",
    )
    write(path, value)


def patch_daily_close() -> None:
    path = "services/api/src/finance_agent/jobs/daily_close.py"
    value = read(path)
    value = replace_once(
        value,
        "from dataclasses import dataclass\nfrom typing import Any\n",
        "from collections.abc import Callable\nfrom dataclasses import dataclass\nfrom datetime import UTC, datetime, timedelta\nfrom typing import Any\n",
        label="daily close imports",
    )
    value = replace_once(
        value,
        """    suffix: str\n\n\nclass DailyCloseService:\n    def __init__(self, engine: FinanceEngine, *, worker_id: str = \"worker_local_001\") -> None:\n        self.engine = engine\n        self.worker_id = worker_id\n\n""",
        """    suffix: str\n    canonical_fixture: bool\n\n\nclass DailyCloseService:\n    def __init__(\n        self,\n        engine: FinanceEngine,\n        *,\n        worker_id: str = \"worker_local_001\",\n        clock: Callable[[], datetime] | None = None,\n    ) -> None:\n        self.engine = engine\n        self.worker_id = worker_id\n        self.clock = clock or (lambda: datetime.now(UTC))\n\n""",
        label="daily close clock dependency",
    )
    new_hash = '''    def _input_hash(self) -> str:\n        workspace = self.engine.store.fetch_one(\n            \"\"\"\n            SELECT workspace_id, currency, timezone, protected_reserve_minor, data_through,\n                   state_revision\n            FROM workspaces WHERE workspace_id = ?\n            \"\"\",\n            (WORKSPACE_ID,),\n        )\n        if workspace is None:\n            raise ValueError(f\"unknown workspace: {WORKSPACE_ID}\")\n        sources = self.engine.store.fetch_all(\n            \"\"\"\n            SELECT source_item_id, source_type, digest, mapping_version, status, row_count\n            FROM source_items WHERE workspace_id = ?\n            ORDER BY source_type, source_item_id\n            \"\"\",\n            (WORKSPACE_ID,),\n        )\n        rules = self.engine.store.fetch_all(\n            \"\"\"\n            SELECT rule_id, merchant_contains, maximum_amount_minor, currency,\n                   target_classification, target_category, effective_from, priority, active\n            FROM classification_rules WHERE workspace_id = ? AND active = 1\n            ORDER BY priority DESC, rule_id\n            \"\"\",\n            (WORKSPACE_ID,),\n        )\n        claims = self.engine.store.fetch_all(\n            \"\"\"\n            SELECT claim_id, claim_type, statement, scope_json, effective_date, status\n            FROM claims WHERE workspace_id = ? AND status = 'active'\n            ORDER BY recorded_at, claim_id\n            \"\"\",\n            (WORKSPACE_ID,),\n        )\n        definition = self.engine.store.fetch_one(\n            \"\"\"\n            SELECT policy_version, enabled FROM job_definitions\n            WHERE workspace_id = ? AND job_type = 'daily_close'\n            \"\"\",\n            (WORKSPACE_ID,),\n        )\n        payload = {\n            \"policyVersion\": str(definition[\"policy_version\"]) if definition else POLICY_VERSION,\n            \"jobEnabled\": bool(definition[\"enabled\"]) if definition else False,\n            \"workspace\": dict(workspace),\n            \"sources\": [dict(row) for row in sources],\n            \"activeRules\": [dict(row) for row in rules],\n            \"activeClaims\": [dict(row) for row in claims],\n        }\n        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()\n\n'''
    value = replace_regex(
        value,
        r"    def _input_hash\(self\) -> str:\n.*?\n    def identity\(",
        new_hash + "    def identity(",
        label="Daily Close state manifest",
    )
    value = replace_once(
        value,
        """        csv_sources = self.engine.store.fetch_all(\n            \"\"\"\n            SELECT source_item_id FROM source_items\n            WHERE workspace_id = ? AND source_type = 'csv'\n            \"\"\",\n            (WORKSPACE_ID,),\n        )\n        canonical_first_close = (\n            len(self.engine.store.fetch_all(\"SELECT run_id FROM job_runs\")) == 0\n            and len(csv_sources) == 1\n""",
        """        all_sources = self.engine.store.fetch_all(\n            \"\"\"\n            SELECT source_item_id, source_type FROM source_items\n            WHERE workspace_id = ?\n            \"\"\",\n            (WORKSPACE_ID,),\n        )\n        canonical_first_close = (\n            len(self.engine.store.fetch_all(\"SELECT run_id FROM job_runs\")) == 0\n            and len(all_sources) == 1\n            and str(all_sources[0][\"source_type\"]) == \"csv\"\n""",
        label="canonical fixture source boundary",
    )
    value = replace_once(
        value,
        """            suffix=suffix,\n        )\n""",
        """            suffix=suffix,\n            canonical_fixture=canonical_first_close,\n        )\n""",
        label="identity canonical marker",
    )
    value = replace_once(
        value,
        """        occurred_at = \"2026-07-17T08:00:04+12:00\"\n\n        with self.engine.store.transaction() as connection:\n""",
        """        if identity.canonical_fixture:\n            started_at = datetime.fromisoformat(DATA_THROUGH)\n            completed_at = datetime.fromisoformat(\"2026-07-17T08:00:04+12:00\")\n        else:\n            started_at = self.clock()\n            completed_at = self.clock()\n            if completed_at < started_at:\n                completed_at = started_at\n        lease_expires_at = started_at + timedelta(minutes=5)\n        started_at_value = started_at.isoformat()\n        occurred_at = completed_at.isoformat()\n\n        with self.engine.store.transaction() as connection:\n            finding_rows_before = {\n                (str(row[\"finding_id\"]), int(row[\"revision\"]))\n                for row in connection.execute(\n                    \"SELECT finding_id, revision FROM findings WHERE workspace_id = ?\",\n                    (WORKSPACE_ID,),\n                )\n            }\n            artifact_rows_before = {\n                (str(row[\"artifact_id\"]), int(row[\"revision\"]))\n                for row in connection.execute(\n                    \"SELECT artifact_id, revision FROM artifacts WHERE workspace_id = ?\",\n                    (WORKSPACE_ID,),\n                )\n            }\n            owner_turns_before = int(\n                connection.execute(\n                    \"SELECT COUNT(*) FROM conversation_turns WHERE workspace_id = ?\",\n                    (WORKSPACE_ID,),\n                ).fetchone()[0]\n            )\n""",
        label="real run timestamps and baselines",
    )
    value = replace_once(
        value,
        """                ) VALUES (?, 'jobdef_koru_daily_close', ?, ?, ?, 'running', ?,\n                    '2026-07-17T08:05:00+12:00', 1, ?, ?)\n""",
        """                ) VALUES (?, 'jobdef_koru_daily_close', ?, ?, ?, 'running', ?,\n                    ?, 1, ?, ?)\n""",
        label="dynamic lease SQL",
    )
    value = replace_once(
        value,
        """                    self.worker_id,\n                    DATA_THROUGH,\n                    correlation_id,\n""",
        """                    self.worker_id,\n                    lease_expires_at.isoformat(),\n                    started_at_value,\n                    correlation_id,\n""",
        label="dynamic lease values",
    )
    value = replace_once(
        value,
        """                        DATA_THROUGH,\n                        occurred_at,\n""",
        """                        started_at_value,\n                        occurred_at,\n""",
        label="dynamic stage start",
    )
    value = replace_once(
        value,
        """            snapshot = self.engine.complete_daily_close_snapshot(\n                connection,\n                derived=derived,\n                occurred_at=occurred_at,\n                snapshot_id=snapshot_id,\n                close_turn_id=close_turn_id,\n            )\n\n        return DailyCloseResult(\n""",
        """            snapshot = self.engine.complete_daily_close_snapshot(\n                connection,\n                derived=derived,\n                occurred_at=occurred_at,\n                snapshot_id=snapshot_id,\n                close_turn_id=close_turn_id,\n            )\n            finding_rows_after = {\n                (str(row[\"finding_id\"]), int(row[\"revision\"]))\n                for row in connection.execute(\n                    \"SELECT finding_id, revision FROM findings WHERE workspace_id = ?\",\n                    (WORKSPACE_ID,),\n                )\n            }\n            artifact_rows_after = {\n                (str(row[\"artifact_id\"]), int(row[\"revision\"]))\n                for row in connection.execute(\n                    \"SELECT artifact_id, revision FROM artifacts WHERE workspace_id = ?\",\n                    (WORKSPACE_ID,),\n                )\n            }\n            owner_turns_after = int(\n                connection.execute(\n                    \"SELECT COUNT(*) FROM conversation_turns WHERE workspace_id = ?\",\n                    (WORKSPACE_ID,),\n                ).fetchone()[0]\n            )\n            new_findings = len(finding_rows_after - finding_rows_before)\n            new_artifacts = len(artifact_rows_after - artifact_rows_before)\n            new_owner_messages = max(0, owner_turns_after - owner_turns_before)\n\n        return DailyCloseResult(\n""",
        label="dynamic committed counts",
    )
    value = replace_once(
        value,
        """            new_findings=3,\n            new_artifacts=2,\n            new_owner_messages=1,\n""",
        """            new_findings=new_findings,\n            new_artifacts=new_artifacts,\n            new_owner_messages=new_owner_messages,\n""",
        label="return dynamic counts",
    )
    write(path, value)


def patch_plaid_tests() -> None:
    for path in sorted((ROOT / "services/api/tests").rglob("test_plaid*.py")):
        value = path.read_text(encoding="utf-8")
        value = value.replace('== "pending"', '== "ignored"')
        value = value.replace("== 'pending'", "== 'ignored'")
        path.write_text(value, encoding="utf-8")


def add_tests() -> None:
    path = ROOT / "services/api/tests/finance/test_production_invariants.py"
    path.write_text(
        '''from __future__ import annotations\n\nfrom datetime import UTC, datetime\nfrom pathlib import Path\n\nimport pytest\n\nfrom finance_agent.finance import FinanceEngine\nfrom finance_agent.jobs import DailyCloseService\nfrom finance_agent.storage import SQLiteConversationStore, SQLiteStore\n\nROOT = Path(__file__).resolve().parents[4]\nCSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"\n\n\ndef seeded_engine(tmp_path: Path) -> tuple[SQLiteStore, FinanceEngine]:\n    store = SQLiteStore(tmp_path / "folio.sqlite3")\n    engine = FinanceEngine(store)\n    engine.reset_demo(CSV)\n    return store, engine\n\n\ndef test_plaid_usd_rows_are_quarantined_outside_nzd_totals(tmp_path: Path) -> None:\n    store, engine = seeded_engine(tmp_path)\n    before = engine.get_snapshot()["totals"]\n    result = engine.ingest_plaid_fixture()\n    assert result.currency == "USD"\n    assert engine.get_snapshot()["totals"] == before\n    rows = store.fetch_all(\n        """\n        SELECT transactions.status, source_rows.raw_json\n        FROM transactions\n        JOIN source_rows USING (source_row_id)\n        JOIN source_items USING (source_item_id)\n        WHERE source_items.source_type = 'plaid_fixture'\n        """\n    )\n    assert rows\n    assert {str(row["status"]) for row in rows} == {"ignored"}\n    quarantine = store.fetch_one(\n        "SELECT provider_currency, status FROM provider_import_quarantine"\n    )\n    assert quarantine is not None\n    assert dict(quarantine) == {"provider_currency": "USD", "status": "quarantined"}\n\n\ndef test_daily_close_identity_changes_when_provider_evidence_arrives(tmp_path: Path) -> None:\n    _store, engine = seeded_engine(tmp_path)\n    service = DailyCloseService(engine)\n    before = service.identity().input_hash\n    engine.ingest_akahu_fixture()\n    assert service.identity().input_hash != before\n    assert service.identity().canonical_fixture is False\n\n\ndef test_daily_close_identity_changes_when_active_rule_changes(tmp_path: Path) -> None:\n    _store, engine = seeded_engine(tmp_path)\n    service = DailyCloseService(engine)\n    before = service.identity().input_hash\n    engine.create_classification_rule(\n        merchant_contains="MITRE 10",\n        maximum_amount_minor=50000,\n        target_classification="business",\n        target_category="client_fit_out_materials",\n        effective_from="2026-07-01",\n        source_turn_id="turn_rule_manifest",\n        owner_statement="MITRE 10 was client fit-out material.",\n    )\n    assert service.identity().input_hash != before\n\n\ndef test_non_fixture_daily_close_uses_injected_clock_and_actual_counts(tmp_path: Path) -> None:\n    store, engine = seeded_engine(tmp_path)\n    engine.ingest_akahu_fixture()\n    instants = iter([\n        datetime(2026, 8, 27, 1, 0, tzinfo=UTC),\n        datetime(2026, 8, 27, 1, 0, 4, tzinfo=UTC),\n    ])\n    service = DailyCloseService(engine, clock=lambda: next(instants))\n    result = service.run()\n    job = store.fetch_one(\n        "SELECT started_at, completed_at FROM job_runs WHERE run_id = ?",\n        (result.run_id,),\n    )\n    assert job is not None\n    assert str(job["started_at"]) == "2026-08-27T01:00:00+00:00"\n    assert str(job["completed_at"]) == "2026-08-27T01:00:04+00:00"\n    assert result.new_findings == len(store.fetch_all("SELECT * FROM findings"))\n    assert result.new_artifacts == len(store.fetch_all("SELECT * FROM artifacts"))\n    assert result.new_owner_messages == 1\n\n\ndef test_each_turn_preserves_the_model_mode_used_for_that_turn(tmp_path: Path) -> None:\n    store, _engine = seeded_engine(tmp_path)\n    store.record_turn(\n        turn_id="turn_mode_local", workspace_id="ws_koru_studio",\n        thread_id="thr_koru_studio_main", role="owner", content="Local question",\n        occurred_at="2026-08-27T01:00:00+00:00", model_mode="local",\n    )\n    store.record_turn(\n        turn_id="turn_mode_cloud", workspace_id="ws_koru_studio",\n        thread_id="thr_koru_studio_main", role="agent", content="Cloud answer",\n        occurred_at="2026-08-27T01:00:01+00:00", model_mode="cloud",\n    )\n    turns = SQLiteConversationStore(store).recent_turns("thr_koru_studio_main", 20)\n    selected = {turn.turn_id: turn.mode for turn in turns}\n    assert selected["turn_mode_local"] == "local"\n    assert selected["turn_mode_cloud"] == "cloud"\n\n\ndef test_claim_cannot_supersede_another_workspaces_claim(tmp_path: Path) -> None:\n    store, _engine = seeded_engine(tmp_path)\n    with store.transaction() as connection:\n        connection.execute(\n            """\n            INSERT INTO workspaces(\n                workspace_id, name, entity_type, currency, timezone, protected_reserve_minor,\n                data_through, thread_id, model_mode, created_at, updated_at\n            ) VALUES ('ws_other', 'Other', 'nz_sole_trader', 'NZD', 'Pacific/Auckland',\n                      0, '2026-08-27T00:00:00+00:00', 'thr_other', 'local',\n                      '2026-08-27T00:00:00+00:00', '2026-08-27T00:00:00+00:00')\n            """\n        )\n        connection.execute(\n            """\n            INSERT INTO claims(\n                claim_id, workspace_id, claim_type, statement, source_turn_id, scope_json,\n                effective_date, recorded_at, status, supersedes_claim_id\n            ) VALUES ('claim_other', 'ws_other', 'business_context', 'Other claim',\n                      'turn_other', '{}', '2026-08-27', '2026-08-27T00:00:00+00:00',\n                      'active', NULL)\n            """\n        )\n    with pytest.raises(ValueError, match="another workspace"):\n        store.record_claim({\n            "claimId": "claim_cross_workspace", "workspaceId": "ws_koru_studio",\n            "claimType": "business_context", "statement": "Unsafe supersession",\n            "sourceTurnId": "turn_cross_workspace", "scope": {},\n            "effectiveDate": "2026-08-27",\n            "recordedAt": "2026-08-27T01:00:00+00:00",\n            "supersedesClaimId": "claim_other",\n        })\n''',
        encoding="utf-8",
    )


def main() -> None:
    patch_migrations()
    patch_store()
    patch_conversations()
    patch_finance_service()
    patch_api_services()
    patch_daily_close()
    patch_plaid_tests()
    add_tests()


if __name__ == "__main__":
    main()
