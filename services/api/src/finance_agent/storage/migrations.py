# ruff: noqa: E501
"""Versioned SQLite migrations for the deterministic finance store."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="finance_core",
        sql="""
        CREATE TABLE workspaces (
            workspace_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            entity_type TEXT NOT NULL CHECK (entity_type = 'nz_sole_trader'),
            currency TEXT NOT NULL CHECK (currency = 'NZD'),
            timezone TEXT NOT NULL CHECK (timezone = 'Pacific/Auckland'),
            protected_reserve_minor INTEGER NOT NULL CHECK (protected_reserve_minor >= 0),
            data_through TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            model_mode TEXT NOT NULL DEFAULT 'local'
                CHECK (model_mode IN ('local', 'hybrid', 'cloud')),
            current_surface_json TEXT,
            current_snapshot_id TEXT,
            state_revision INTEGER NOT NULL DEFAULT 0 CHECK (state_revision >= 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE accounts (
            account_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            name TEXT NOT NULL,
            currency TEXT NOT NULL CHECK (currency = 'NZD'),
            created_at TEXT NOT NULL
        );

        CREATE TABLE source_items (
            source_item_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            source_type TEXT NOT NULL
                CHECK (source_type IN ('csv', 'telegram_fixture', 'owner_claim')),
            label TEXT NOT NULL,
            digest TEXT NOT NULL CHECK (length(digest) = 64),
            mapping_version TEXT NOT NULL,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'processed', 'failed')),
            row_count INTEGER NOT NULL CHECK (row_count >= 0),
            UNIQUE (workspace_id, digest, mapping_version)
        );

        CREATE TABLE source_rows (
            source_row_id TEXT PRIMARY KEY,
            source_item_id TEXT NOT NULL REFERENCES source_items(source_item_id),
            row_number INTEGER NOT NULL CHECK (row_number >= 1),
            account_id TEXT NOT NULL REFERENCES accounts(account_id),
            occurred_on TEXT NOT NULL,
            description TEXT NOT NULL,
            amount_minor INTEGER NOT NULL,
            currency TEXT NOT NULL CHECK (currency = 'NZD'),
            source_status TEXT NOT NULL CHECK (source_status IN ('posted', 'pending')),
            external_reference TEXT NOT NULL,
            mapping_version TEXT NOT NULL,
            row_hash TEXT NOT NULL CHECK (length(row_hash) = 64),
            raw_json TEXT NOT NULL,
            UNIQUE (source_item_id, row_number),
            UNIQUE (source_item_id, external_reference)
        );

        CREATE TABLE evidence_links (
            evidence_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            source_item_id TEXT REFERENCES source_items(source_item_id),
            source_row_id TEXT REFERENCES source_rows(source_row_id),
            label TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE transactions (
            transaction_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            account_id TEXT NOT NULL REFERENCES accounts(account_id),
            source_row_id TEXT NOT NULL UNIQUE REFERENCES source_rows(source_row_id),
            evidence_id TEXT NOT NULL REFERENCES evidence_links(evidence_id),
            occurred_on TEXT NOT NULL,
            description TEXT NOT NULL,
            amount_minor INTEGER NOT NULL,
            currency TEXT NOT NULL CHECK (currency = 'NZD'),
            source_status TEXT NOT NULL CHECK (source_status IN ('posted', 'pending')),
            status TEXT NOT NULL CHECK (status IN ('posted', 'pending', 'duplicate', 'ignored')),
            classification TEXT NOT NULL
                CHECK (classification IN ('business', 'personal', 'unresolved', 'transfer')),
            category TEXT,
            classification_source TEXT NOT NULL
                CHECK (classification_source IN ('unclassified', 'deterministic', 'explicit_rule', 'accepted_feedback')),
            rule_id TEXT,
            duplicate_of_transaction_id TEXT REFERENCES transactions(transaction_id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE classification_rules (
            rule_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            merchant_contains TEXT NOT NULL,
            maximum_amount_minor INTEGER NOT NULL CHECK (maximum_amount_minor >= 0),
            currency TEXT NOT NULL CHECK (currency = 'NZD'),
            target_classification TEXT NOT NULL
                CHECK (target_classification IN ('business', 'personal', 'unresolved')),
            target_category TEXT,
            effective_from TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 100,
            active INTEGER NOT NULL CHECK (active IN (0, 1)),
            source_turn_id TEXT,
            source_claim_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE finance_events (
            event_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            event_type TEXT NOT NULL CHECK (event_type IN (
                'classification_rule.created',
                'classification_rule.reapplied',
                'event.undone',
                'claim.recorded'
            )),
            actor TEXT NOT NULL CHECK (actor IN ('owner', 'agent', 'system')),
            occurred_at TEXT NOT NULL,
            source_turn_id TEXT,
            reason TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL,
            inverse_event_json TEXT NOT NULL,
            correlation_id TEXT NOT NULL,
            undone_by_event_id TEXT REFERENCES finance_events(event_id),
            redone_by_event_id TEXT REFERENCES finance_events(event_id)
        );

        CREATE TABLE event_effects (
            effect_id TEXT PRIMARY KEY,
            event_id TEXT NOT NULL REFERENCES finance_events(event_id),
            effect_order INTEGER NOT NULL CHECK (effect_order >= 1),
            target_type TEXT NOT NULL
                CHECK (target_type IN ('transaction', 'classification_rule', 'claim', 'forecast')),
            target_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            before_json TEXT NOT NULL,
            after_json TEXT NOT NULL,
            UNIQUE (event_id, effect_order)
        );

        CREATE TABLE job_definitions (
            definition_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            job_type TEXT NOT NULL CHECK (job_type = 'daily_close'),
            enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
            policy_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (workspace_id, job_type)
        );

        CREATE TABLE job_runs (
            run_id TEXT PRIMARY KEY,
            definition_id TEXT NOT NULL REFERENCES job_definitions(definition_id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            idempotency_key TEXT NOT NULL,
            input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
            status TEXT NOT NULL
                CHECK (status IN ('queued', 'running', 'completed', 'no_op', 'failed')),
            lease_owner TEXT,
            lease_expires_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            started_at TEXT,
            completed_at TEXT,
            receipt_id TEXT,
            result_json TEXT,
            error_json TEXT,
            correlation_id TEXT NOT NULL,
            UNIQUE (workspace_id, idempotency_key)
        );

        CREATE TABLE job_stage_runs (
            stage_run_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES job_runs(run_id),
            stage TEXT NOT NULL CHECK (stage IN (
                'ingest', 'normalise', 'deduplicate', 'apply_rules', 'classify',
                'findings', 'forecast', 'owner_pack', 'receipt', 'telegram_outbox'
            )),
            sequence INTEGER NOT NULL CHECK (sequence >= 1),
            status TEXT NOT NULL CHECK (status IN ('running', 'completed', 'no_op', 'failed')),
            input_hash TEXT NOT NULL CHECK (length(input_hash) = 64),
            output_hash TEXT,
            attempt INTEGER NOT NULL DEFAULT 1 CHECK (attempt >= 1),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            error_json TEXT,
            UNIQUE (run_id, stage)
        );

        CREATE TABLE conversation_turns (
            turn_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            thread_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('owner', 'agent')),
            content TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('complete', 'streaming', 'failed')),
            evidence_ids_json TEXT NOT NULL
        );

        CREATE TABLE dialogue_frames (
            frame_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            thread_id TEXT NOT NULL,
            frame_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_current INTEGER NOT NULL CHECK (is_current IN (0, 1))
        );

        CREATE TABLE claims (
            claim_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            claim_type TEXT NOT NULL CHECK (claim_type IN (
                'business_context', 'classification_instruction', 'planned_expense', 'reserve_policy'
            )),
            statement TEXT NOT NULL,
            source_turn_id TEXT NOT NULL,
            scope_json TEXT NOT NULL,
            effective_date TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'superseded', 'retracted')),
            supersedes_claim_id TEXT REFERENCES claims(claim_id)
        );

        CREATE TABLE findings (
            finding_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            kind TEXT NOT NULL CHECK (kind IN ('missing_document', 'duplicate', 'reserve_risk')),
            severity TEXT NOT NULL CHECK (severity IN ('info', 'attention', 'critical')),
            title TEXT NOT NULL,
            summary TEXT NOT NULL,
            amount_minor INTEGER,
            currency TEXT CHECK (currency IS NULL OR currency = 'NZD'),
            status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'dismissed')),
            evidence_ids_json TEXT NOT NULL,
            state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
            is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
            created_at TEXT NOT NULL,
            obsoleted_at TEXT,
            PRIMARY KEY (finding_id, revision)
        );

        CREATE TABLE forecast_revisions (
            forecast_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            current_balance_minor INTEGER NOT NULL,
            protected_reserve_minor INTEGER NOT NULL CHECK (protected_reserve_minor >= 0),
            projected_low_point_minor INTEGER NOT NULL,
            reserve_shortfall_minor INTEGER NOT NULL CHECK (reserve_shortfall_minor >= 0),
            alternative_low_point_minor INTEGER NOT NULL,
            assumptions_json TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL,
            state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
            is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
            created_at TEXT NOT NULL,
            obsoleted_at TEXT,
            PRIMARY KEY (forecast_id, revision)
        );

        CREATE TABLE forecast_points (
            forecast_id TEXT NOT NULL,
            revision INTEGER NOT NULL,
            point_index INTEGER NOT NULL CHECK (point_index >= 0),
            date TEXT NOT NULL,
            label TEXT NOT NULL,
            amount_minor INTEGER NOT NULL,
            balance_minor INTEGER NOT NULL,
            reserve_minor INTEGER NOT NULL CHECK (reserve_minor >= 0),
            status TEXT NOT NULL CHECK (status IN ('above_reserve', 'below_reserve')),
            PRIMARY KEY (forecast_id, revision, point_index),
            FOREIGN KEY (forecast_id, revision)
                REFERENCES forecast_revisions(forecast_id, revision)
        );

        CREATE TABLE artifacts (
            artifact_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            kind TEXT NOT NULL CHECK (kind IN ('owner_pack_html', 'owner_pack_pdf')),
            title TEXT NOT NULL,
            media_type TEXT NOT NULL,
            content BLOB NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            dto_json TEXT NOT NULL,
            dto_hash TEXT NOT NULL CHECK (length(dto_hash) = 64),
            evidence_ids_json TEXT NOT NULL,
            state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
            generated_at TEXT NOT NULL,
            is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
            obsoleted_at TEXT,
            PRIMARY KEY (artifact_id, revision)
        );

        CREATE TABLE outbox_messages (
            outbox_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            kind TEXT NOT NULL CHECK (kind = 'reserve_risk_brief'),
            payload_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('queued', 'attempted', 'delivered', 'failed', 'obsolete')),
            idempotency_key TEXT NOT NULL UNIQUE,
            correlation_id TEXT NOT NULL,
            evidence_ids_json TEXT NOT NULL,
            state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
            created_at TEXT NOT NULL,
            attempted_at TEXT,
            delivered_at TEXT,
            failure_json TEXT,
            obsoleted_at TEXT
        );

        CREATE TABLE workspace_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
            snapshot_json TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            created_at TEXT NOT NULL,
            is_current INTEGER NOT NULL CHECK (is_current IN (0, 1))
        );

        CREATE TABLE model_runs (
            model_run_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            receipt_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE egress_receipts (
            receipt_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            receipt_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """,
    ),
    Migration(
        version=2,
        name="immutability_and_indexes",
        sql="""
        CREATE TRIGGER source_rows_no_update
        BEFORE UPDATE ON source_rows
        BEGIN
            SELECT RAISE(ABORT, 'source_rows are immutable');
        END;

        CREATE TRIGGER source_rows_no_delete
        BEFORE DELETE ON source_rows
        BEGIN
            SELECT RAISE(ABORT, 'source_rows are immutable');
        END;

        CREATE TRIGGER finance_events_no_update
        BEFORE UPDATE OF event_type, actor, occurred_at, source_turn_id, reason,
            before_json, after_json, scope_json, evidence_ids_json,
            inverse_event_json, correlation_id
        ON finance_events
        BEGIN
            SELECT RAISE(ABORT, 'finance event payloads are append-only');
        END;

        CREATE TRIGGER finance_events_no_delete
        BEFORE DELETE ON finance_events
        BEGIN
            SELECT RAISE(ABORT, 'finance events are append-only');
        END;

        CREATE UNIQUE INDEX findings_one_current
            ON findings(finding_id) WHERE is_current = 1;
        CREATE UNIQUE INDEX forecast_one_current
            ON forecast_revisions(forecast_id) WHERE is_current = 1;
        CREATE UNIQUE INDEX artifacts_one_current
            ON artifacts(artifact_id) WHERE is_current = 1;
        CREATE UNIQUE INDEX snapshots_one_current
            ON workspace_snapshots(workspace_id) WHERE is_current = 1;
        CREATE UNIQUE INDEX dialogue_frames_one_current
            ON dialogue_frames(workspace_id, thread_id) WHERE is_current = 1;

        CREATE INDEX transactions_workspace_status
            ON transactions(workspace_id, status, classification);
        CREATE INDEX transactions_match_rule
            ON transactions(workspace_id, currency, occurred_on, amount_minor);
        CREATE INDEX source_items_pending
            ON source_items(workspace_id, status, received_at);
        CREATE INDEX job_runs_claim
            ON job_runs(workspace_id, status, lease_expires_at);
        CREATE INDEX job_stages_ordered
            ON job_stage_runs(run_id, sequence);
        CREATE INDEX events_workspace_time
            ON finance_events(workspace_id, occurred_at, event_id);
        CREATE INDEX outbox_pending
            ON outbox_messages(workspace_id, status, created_at);
        """,
    ),
)
