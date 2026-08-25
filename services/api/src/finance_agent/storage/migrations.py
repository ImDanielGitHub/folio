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
                CHECK (source_type IN (
                    'csv', 'telegram_fixture', 'owner_claim', 'akahu_fixture'
                )),
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
    Migration(
        version=3,
        name="agent_receipts",
        sql="""
        CREATE TABLE work_receipts (
            receipt_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            run_id TEXT NOT NULL,
            receipt_json TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            created_at TEXT NOT NULL
        );

        CREATE INDEX work_receipts_run ON work_receipts(run_id, created_at);
        """,
    ),
    Migration(
        version=4,
        name="dialogue_frame_revision",
        sql="""
        ALTER TABLE dialogue_frames
            ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1);
        """,
    ),
    Migration(
        version=5,
        name="append_only_working_knowledge",
        sql="""
        CREATE TABLE knowledge_owner_statements (
            statement_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            thread_id TEXT NOT NULL,
            turn_id TEXT NOT NULL,
            content TEXT NOT NULL CHECK (length(trim(content)) > 0),
            task_scope TEXT NOT NULL CHECK (length(trim(task_scope)) > 0),
            occurred_at TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            record_hash TEXT NOT NULL CHECK (length(record_hash) = 64),
            UNIQUE (workspace_id, turn_id)
        );

        CREATE TABLE knowledge_documents (
            document_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            document_kind TEXT NOT NULL CHECK (document_kind IN (
                'receipt', 'invoice', 'contract', 'bank_statement', 'tax_document',
                'correspondence', 'note', 'other'
            )),
            title TEXT NOT NULL CHECK (length(trim(title)) > 0),
            task_scope TEXT NOT NULL CHECK (length(trim(task_scope)) > 0),
            source_kind TEXT NOT NULL CHECK (source_kind IN (
                'owner_turn', 'document', 'finance_evidence', 'connector',
                'deterministic', 'model_candidate', 'import'
            )),
            source_ref TEXT NOT NULL CHECK (length(trim(source_ref)) > 0),
            source_turn_id TEXT REFERENCES conversation_turns(turn_id),
            evidence_id TEXT REFERENCES evidence_links(evidence_id),
            received_at TEXT NOT NULL,
            effective_from TEXT,
            effective_until TEXT,
            extracted_text TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            metadata_json TEXT NOT NULL,
            record_hash TEXT NOT NULL CHECK (length(record_hash) = 64),
            CHECK (
                effective_from IS NULL OR effective_until IS NULL
                OR effective_from <= effective_until
            )
        );

        CREATE TABLE knowledge_entities (
            entity_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            entity_type TEXT NOT NULL CHECK (entity_type IN (
                'person', 'organisation', 'place', 'project', 'customer', 'supplier',
                'account', 'asset', 'service', 'event', 'other'
            )),
            canonical_name TEXT NOT NULL CHECK (length(trim(canonical_name)) > 0),
            task_scope TEXT NOT NULL CHECK (length(trim(task_scope)) > 0),
            source_kind TEXT NOT NULL CHECK (source_kind IN (
                'owner_turn', 'document', 'finance_evidence', 'connector',
                'deterministic', 'model_candidate', 'import'
            )),
            source_ref TEXT NOT NULL CHECK (length(trim(source_ref)) > 0),
            source_turn_id TEXT REFERENCES conversation_turns(turn_id),
            source_document_id TEXT REFERENCES knowledge_documents(document_id),
            evidence_id TEXT REFERENCES evidence_links(evidence_id),
            recorded_at TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            record_hash TEXT NOT NULL CHECK (length(record_hash) = 64)
        );

        CREATE TABLE knowledge_facts (
            fact_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            question_axis TEXT NOT NULL
                CHECK (question_axis IN ('who', 'what', 'where', 'when', 'why')),
            fact_kind TEXT NOT NULL CHECK (fact_kind IN ('attribute', 'relationship')),
            subject_entity_id TEXT NOT NULL REFERENCES knowledge_entities(entity_id),
            predicate TEXT NOT NULL CHECK (length(trim(predicate)) > 0),
            scope_key TEXT NOT NULL CHECK (length(trim(scope_key)) > 0),
            object_kind TEXT NOT NULL CHECK (object_kind IN (
                'text', 'entity', 'document', 'date', 'datetime', 'boolean', 'number', 'json'
            )),
            object_text TEXT NOT NULL,
            object_entity_id TEXT REFERENCES knowledge_entities(entity_id),
            object_document_id TEXT REFERENCES knowledge_documents(document_id),
            value_json TEXT NOT NULL,
            value_hash TEXT NOT NULL CHECK (length(value_hash) = 64),
            task_scope TEXT NOT NULL CHECK (length(trim(task_scope)) > 0),
            source_kind TEXT NOT NULL CHECK (source_kind IN (
                'owner_turn', 'document', 'finance_evidence', 'connector',
                'deterministic', 'model_candidate', 'import'
            )),
            source_ref TEXT NOT NULL CHECK (length(trim(source_ref)) > 0),
            source_turn_id TEXT REFERENCES conversation_turns(turn_id),
            source_document_id TEXT REFERENCES knowledge_documents(document_id),
            evidence_id TEXT REFERENCES evidence_links(evidence_id),
            basis TEXT NOT NULL CHECK (basis IN (
                'explicit', 'document_extracted', 'deterministic', 'inferred', 'hypothetical'
            )),
            confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
            valid_from TEXT,
            valid_until TEXT,
            recorded_at TEXT NOT NULL,
            supersedes_fact_id TEXT REFERENCES knowledge_facts(fact_id),
            record_hash TEXT NOT NULL CHECK (length(record_hash) = 64),
            CHECK (
                valid_from IS NULL OR valid_until IS NULL OR valid_from <= valid_until
            ),
            CHECK (
                (object_kind = 'entity' AND object_entity_id IS NOT NULL
                    AND object_document_id IS NULL AND fact_kind = 'relationship')
                OR (object_kind = 'document' AND object_document_id IS NOT NULL
                    AND object_entity_id IS NULL)
                OR (object_kind NOT IN ('entity', 'document')
                    AND object_entity_id IS NULL AND object_document_id IS NULL
                    AND fact_kind = 'attribute')
            )
        );

        CREATE TABLE knowledge_fact_status_events (
            event_id TEXT PRIMARY KEY,
            fact_id TEXT NOT NULL REFERENCES knowledge_facts(fact_id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            sequence INTEGER NOT NULL CHECK (sequence >= 1),
            status TEXT NOT NULL CHECK (status IN (
                'candidate', 'active', 'superseded', 'retracted', 'rejected'
            )),
            reason TEXT NOT NULL,
            source_kind TEXT NOT NULL CHECK (source_kind IN (
                'owner_turn', 'document', 'finance_evidence', 'connector',
                'deterministic', 'model_candidate', 'import'
            )),
            source_ref TEXT NOT NULL CHECK (length(trim(source_ref)) > 0),
            source_turn_id TEXT REFERENCES conversation_turns(turn_id),
            occurred_at TEXT NOT NULL,
            record_hash TEXT NOT NULL CHECK (length(record_hash) = 64),
            UNIQUE (fact_id, sequence)
        );

        CREATE TABLE knowledge_contradictions (
            contradiction_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            fact_a_id TEXT NOT NULL REFERENCES knowledge_facts(fact_id),
            fact_b_id TEXT NOT NULL REFERENCES knowledge_facts(fact_id),
            scope_key TEXT NOT NULL,
            predicate TEXT NOT NULL,
            reason_code TEXT NOT NULL CHECK (reason_code IN (
                'same_scope_different_value', 'manual'
            )),
            detected_at TEXT NOT NULL,
            record_hash TEXT NOT NULL CHECK (length(record_hash) = 64),
            CHECK (fact_a_id < fact_b_id),
            UNIQUE (workspace_id, fact_a_id, fact_b_id, reason_code)
        );

        CREATE TABLE knowledge_contradiction_status_events (
            event_id TEXT PRIMARY KEY,
            contradiction_id TEXT NOT NULL
                REFERENCES knowledge_contradictions(contradiction_id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            sequence INTEGER NOT NULL CHECK (sequence >= 1),
            status TEXT NOT NULL CHECK (status IN ('open', 'resolved', 'dismissed')),
            reason TEXT NOT NULL,
            source_kind TEXT NOT NULL CHECK (source_kind IN (
                'owner_turn', 'document', 'finance_evidence', 'connector',
                'deterministic', 'model_candidate', 'import'
            )),
            source_ref TEXT NOT NULL CHECK (length(trim(source_ref)) > 0),
            source_turn_id TEXT REFERENCES conversation_turns(turn_id),
            occurred_at TEXT NOT NULL,
            record_hash TEXT NOT NULL CHECK (length(record_hash) = 64),
            UNIQUE (contradiction_id, sequence)
        );

        CREATE TABLE knowledge_business_summary_revisions (
            summary_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            as_of TEXT NOT NULL,
            limit_per_axis INTEGER NOT NULL CHECK (limit_per_axis >= 1),
            query_version TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            source_fact_ids_json TEXT NOT NULL,
            source_status_event_ids_json TEXT NOT NULL,
            source_finding_ids_json TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            generated_at TEXT NOT NULL,
            PRIMARY KEY (summary_id, revision),
            UNIQUE (workspace_id, as_of, query_version, content_hash)
        );

        CREATE TABLE knowledge_context_retrieval_receipts (
            receipt_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            run_id TEXT,
            thread_id TEXT,
            task_scope TEXT NOT NULL,
            query_hash TEXT NOT NULL CHECK (length(query_hash) = 64),
            as_of TEXT NOT NULL,
            include_candidates INTEGER NOT NULL CHECK (include_candidates IN (0, 1)),
            result_ids_json TEXT NOT NULL,
            selected_ids_json TEXT NOT NULL,
            dropped_ids_json TEXT NOT NULL,
            max_characters INTEGER NOT NULL CHECK (max_characters >= 1),
            packet_characters INTEGER NOT NULL CHECK (packet_characters >= 0),
            packet_hash TEXT NOT NULL CHECK (length(packet_hash) = 64),
            source_status_event_ids_json TEXT NOT NULL,
            query_version TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            retrieved_at TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE knowledge_fts USING fts5(
            workspace_id UNINDEXED,
            record_type UNINDEXED,
            record_id UNINDEXED,
            task_scope UNINDEXED,
            title,
            body,
            tags,
            tokenize = 'unicode61 remove_diacritics 2'
        );

        CREATE TRIGGER knowledge_owner_statements_fts_insert
        AFTER INSERT ON knowledge_owner_statements
        BEGIN
            INSERT INTO knowledge_fts(
                workspace_id, record_type, record_id, task_scope, title, body, tags
            ) VALUES (
                NEW.workspace_id, 'owner_statement', NEW.statement_id, NEW.task_scope,
                'Owner said', NEW.content, 'owner explicit ' || NEW.task_scope
            );
        END;

        CREATE TRIGGER knowledge_documents_fts_insert
        AFTER INSERT ON knowledge_documents
        BEGIN
            INSERT INTO knowledge_fts(
                workspace_id, record_type, record_id, task_scope, title, body, tags
            ) VALUES (
                NEW.workspace_id, 'document', NEW.document_id, NEW.task_scope,
                NEW.title, NEW.extracted_text, NEW.document_kind || ' ' || NEW.task_scope
            );
        END;

        CREATE TRIGGER knowledge_entities_fts_insert
        AFTER INSERT ON knowledge_entities
        BEGIN
            INSERT INTO knowledge_fts(
                workspace_id, record_type, record_id, task_scope, title, body, tags
            ) VALUES (
                NEW.workspace_id, 'entity', NEW.entity_id, NEW.task_scope,
                NEW.canonical_name, NEW.canonical_name,
                NEW.entity_type || ' ' || NEW.task_scope
            );
        END;

        CREATE TRIGGER knowledge_facts_fts_insert
        AFTER INSERT ON knowledge_facts
        BEGIN
            INSERT INTO knowledge_fts(
                workspace_id, record_type, record_id, task_scope, title, body, tags
            ) VALUES (
                NEW.workspace_id, 'fact', NEW.fact_id, NEW.task_scope,
                (SELECT canonical_name FROM knowledge_entities
                    WHERE entity_id = NEW.subject_entity_id) || ' ' || NEW.predicate,
                NEW.object_text,
                NEW.question_axis || ' ' || NEW.fact_kind || ' ' || NEW.predicate
                    || ' ' || NEW.task_scope
            );
        END;

        CREATE TRIGGER knowledge_fact_supersession_scope_guard
        BEFORE INSERT ON knowledge_facts
        WHEN NEW.supersedes_fact_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM knowledge_facts AS previous
              WHERE previous.fact_id = NEW.supersedes_fact_id
                AND previous.workspace_id = NEW.workspace_id
                AND previous.question_axis = NEW.question_axis
                AND previous.subject_entity_id = NEW.subject_entity_id
                AND previous.predicate = NEW.predicate
                AND previous.scope_key = NEW.scope_key
          )
        BEGIN
            SELECT RAISE(ABORT, 'knowledge supersession must keep the same fact scope');
        END;

        CREATE TRIGGER knowledge_fact_status_workspace_guard
        BEFORE INSERT ON knowledge_fact_status_events
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_facts
            WHERE fact_id = NEW.fact_id AND workspace_id = NEW.workspace_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'knowledge fact status workspace mismatch');
        END;

        CREATE TRIGGER knowledge_contradiction_workspace_guard
        BEFORE INSERT ON knowledge_contradictions
        WHEN NOT EXISTS (
            SELECT 1
            FROM knowledge_facts AS fact_a
            JOIN knowledge_facts AS fact_b ON fact_b.fact_id = NEW.fact_b_id
            WHERE fact_a.fact_id = NEW.fact_a_id
              AND fact_a.workspace_id = NEW.workspace_id
              AND fact_b.workspace_id = NEW.workspace_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'knowledge contradiction workspace mismatch');
        END;

        CREATE TRIGGER knowledge_contradiction_status_workspace_guard
        BEFORE INSERT ON knowledge_contradiction_status_events
        WHEN NOT EXISTS (
            SELECT 1 FROM knowledge_contradictions
            WHERE contradiction_id = NEW.contradiction_id
              AND workspace_id = NEW.workspace_id
        )
        BEGIN
            SELECT RAISE(ABORT, 'knowledge contradiction status workspace mismatch');
        END;

        CREATE INDEX knowledge_owner_statements_workspace_time
            ON knowledge_owner_statements(workspace_id, occurred_at, turn_id);
        CREATE INDEX knowledge_documents_workspace_scope
            ON knowledge_documents(workspace_id, task_scope, received_at);
        CREATE INDEX knowledge_entities_workspace_type
            ON knowledge_entities(workspace_id, entity_type, canonical_name);
        CREATE INDEX knowledge_facts_current_scope
            ON knowledge_facts(workspace_id, scope_key, predicate, valid_from, valid_until);
        CREATE INDEX knowledge_fact_status_order
            ON knowledge_fact_status_events(fact_id, sequence DESC);
        CREATE INDEX knowledge_contradictions_workspace
            ON knowledge_contradictions(workspace_id, detected_at);
        CREATE INDEX knowledge_contradiction_status_order
            ON knowledge_contradiction_status_events(contradiction_id, sequence DESC);
        CREATE INDEX knowledge_summaries_workspace_as_of
            ON knowledge_business_summary_revisions(workspace_id, as_of, revision DESC);
        CREATE INDEX knowledge_retrieval_receipts_workspace_time
            ON knowledge_context_retrieval_receipts(workspace_id, retrieved_at);
        CREATE INDEX knowledge_retrieval_receipts_run
            ON knowledge_context_retrieval_receipts(workspace_id, run_id, retrieved_at);

        CREATE TRIGGER knowledge_owner_statements_no_update
        BEFORE UPDATE ON knowledge_owner_statements
        BEGIN
            SELECT RAISE(ABORT, 'knowledge owner statements are append-only');
        END;

        CREATE TRIGGER knowledge_owner_statements_no_delete
        BEFORE DELETE ON knowledge_owner_statements
        BEGIN
            SELECT RAISE(ABORT, 'knowledge owner statements are append-only');
        END;

        CREATE TRIGGER knowledge_documents_no_update
        BEFORE UPDATE ON knowledge_documents
        BEGIN
            SELECT RAISE(ABORT, 'knowledge documents are append-only');
        END;

        CREATE TRIGGER knowledge_documents_no_delete
        BEFORE DELETE ON knowledge_documents
        BEGIN
            SELECT RAISE(ABORT, 'knowledge documents are append-only');
        END;

        CREATE TRIGGER knowledge_entities_no_update
        BEFORE UPDATE ON knowledge_entities
        BEGIN
            SELECT RAISE(ABORT, 'knowledge entities are append-only');
        END;

        CREATE TRIGGER knowledge_entities_no_delete
        BEFORE DELETE ON knowledge_entities
        BEGIN
            SELECT RAISE(ABORT, 'knowledge entities are append-only');
        END;

        CREATE TRIGGER knowledge_facts_no_update
        BEFORE UPDATE ON knowledge_facts
        BEGIN
            SELECT RAISE(ABORT, 'knowledge facts are append-only');
        END;

        CREATE TRIGGER knowledge_facts_no_delete
        BEFORE DELETE ON knowledge_facts
        BEGIN
            SELECT RAISE(ABORT, 'knowledge facts are append-only');
        END;

        CREATE TRIGGER knowledge_fact_status_no_update
        BEFORE UPDATE ON knowledge_fact_status_events
        BEGIN
            SELECT RAISE(ABORT, 'knowledge fact status events are append-only');
        END;

        CREATE TRIGGER knowledge_fact_status_no_delete
        BEFORE DELETE ON knowledge_fact_status_events
        BEGIN
            SELECT RAISE(ABORT, 'knowledge fact status events are append-only');
        END;

        CREATE TRIGGER knowledge_contradictions_no_update
        BEFORE UPDATE ON knowledge_contradictions
        BEGIN
            SELECT RAISE(ABORT, 'knowledge contradictions are append-only');
        END;

        CREATE TRIGGER knowledge_contradictions_no_delete
        BEFORE DELETE ON knowledge_contradictions
        BEGIN
            SELECT RAISE(ABORT, 'knowledge contradictions are append-only');
        END;

        CREATE TRIGGER knowledge_contradiction_status_no_update
        BEFORE UPDATE ON knowledge_contradiction_status_events
        BEGIN
            SELECT RAISE(ABORT, 'knowledge contradiction status events are append-only');
        END;

        CREATE TRIGGER knowledge_contradiction_status_no_delete
        BEFORE DELETE ON knowledge_contradiction_status_events
        BEGIN
            SELECT RAISE(ABORT, 'knowledge contradiction status events are append-only');
        END;

        CREATE TRIGGER knowledge_business_summaries_no_update
        BEFORE UPDATE ON knowledge_business_summary_revisions
        BEGIN
            SELECT RAISE(ABORT, 'knowledge business summaries are append-only');
        END;

        CREATE TRIGGER knowledge_business_summaries_no_delete
        BEFORE DELETE ON knowledge_business_summary_revisions
        BEGIN
            SELECT RAISE(ABORT, 'knowledge business summaries are append-only');
        END;

        CREATE TRIGGER knowledge_retrieval_receipts_no_update
        BEFORE UPDATE ON knowledge_context_retrieval_receipts
        BEGIN
            SELECT RAISE(ABORT, 'knowledge retrieval receipts are append-only');
        END;

        CREATE TRIGGER knowledge_retrieval_receipts_no_delete
        BEFORE DELETE ON knowledge_context_retrieval_receipts
        BEGIN
            SELECT RAISE(ABORT, 'knowledge retrieval receipts are append-only');
        END;
        """,
    ),
    Migration(
        version=6,
        name="akahu_fixture_source_type",
        sql="""
        PRAGMA foreign_keys = OFF;

        CREATE TABLE source_items_v6 (
            source_item_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            source_type TEXT NOT NULL
                CHECK (source_type IN (
                    'csv', 'telegram_fixture', 'owner_claim', 'akahu_fixture'
                )),
            label TEXT NOT NULL,
            digest TEXT NOT NULL CHECK (length(digest) = 64),
            mapping_version TEXT NOT NULL,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'processed', 'failed')),
            row_count INTEGER NOT NULL CHECK (row_count >= 0),
            UNIQUE (workspace_id, digest, mapping_version)
        );

        INSERT INTO source_items_v6 (
            source_item_id, workspace_id, source_type, label, digest,
            mapping_version, received_at, status, row_count
        )
        SELECT
            source_item_id, workspace_id, source_type, label, digest,
            mapping_version, received_at, status, row_count
        FROM source_items;

        DROP TABLE source_items;
        ALTER TABLE source_items_v6 RENAME TO source_items;

        CREATE INDEX IF NOT EXISTS source_items_pending
            ON source_items(workspace_id, status, received_at);

        PRAGMA foreign_keys = ON;
        """,
    ),
    Migration(
        version=7,
        name="plaid_fixture_source_type",
        sql="""
        PRAGMA foreign_keys = OFF;

        CREATE TABLE source_items_v7 (
            source_item_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            source_type TEXT NOT NULL
                CHECK (source_type IN (
                    'csv', 'telegram_fixture', 'owner_claim', 'akahu_fixture',
                    'plaid_fixture'
                )),
            label TEXT NOT NULL,
            digest TEXT NOT NULL CHECK (length(digest) = 64),
            mapping_version TEXT NOT NULL,
            received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'processed', 'failed')),
            row_count INTEGER NOT NULL CHECK (row_count >= 0),
            UNIQUE (workspace_id, digest, mapping_version)
        );

        INSERT INTO source_items_v7 (
            source_item_id, workspace_id, source_type, label, digest,
            mapping_version, received_at, status, row_count
        )
        SELECT
            source_item_id, workspace_id, source_type, label, digest,
            mapping_version, received_at, status, row_count
        FROM source_items;

        DROP TABLE source_items;
        ALTER TABLE source_items_v7 RENAME TO source_items;

        CREATE INDEX IF NOT EXISTS source_items_pending
            ON source_items(workspace_id, status, received_at);

        PRAGMA foreign_keys = ON;
        """,
    ),    Migration(
        version=8,
        name="provider_quarantine_and_turn_provenance",
        sql="""
        ALTER TABLE conversation_turns
            ADD COLUMN model_mode TEXT NOT NULL DEFAULT 'local'
            CHECK (model_mode IN ('local', 'hybrid', 'cloud'));

        CREATE TABLE provider_transaction_events (
            event_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            provider TEXT NOT NULL,
            provider_account_id TEXT NOT NULL,
            provider_transaction_id TEXT NOT NULL,
            source_item_id TEXT NOT NULL REFERENCES source_items(source_item_id),
            event_type TEXT NOT NULL CHECK (event_type IN (
                'added', 'modified', 'removed', 'quarantined'
            )),
            occurred_on TEXT,
            description TEXT NOT NULL,
            amount_minor INTEGER,
            currency TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            supersedes_event_id TEXT REFERENCES provider_transaction_events(event_id),
            recorded_at TEXT NOT NULL,
            UNIQUE (workspace_id, provider, event_id)
        );

        CREATE INDEX provider_events_reference
            ON provider_transaction_events(
                workspace_id, provider, provider_account_id, provider_transaction_id, recorded_at
            );
        """,
    ),

)
