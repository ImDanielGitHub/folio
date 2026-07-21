"""Deterministically project committed finance records into working knowledge."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from typing import cast

from .knowledge import (
    BusinessSummary,
    DocumentKind,
    EntityType,
    FactBasis,
    FactKind,
    FactStatus,
    KnowledgeDocument,
    KnowledgeEntity,
    KnowledgeFact,
    KnowledgeSource,
    ObjectKind,
    QuestionAxis,
    SourceKind,
    SQLiteKnowledgeStore,
)
from .store import SQLiteStore, canonical_json


def _stable_id(prefix: str, value: object) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


class CommittedFinanceKnowledgeCompiler:
    """Build a first-look business model from committed, deterministic tables."""

    def __init__(self, store: SQLiteStore, knowledge: SQLiteKnowledgeStore | None = None) -> None:
        self.store = store
        self.knowledge = knowledge or SQLiteKnowledgeStore(store)

    def _current_active_fact(
        self,
        *,
        workspace_id: str,
        subject_entity_id: str,
        predicate: str,
        scope_key: str,
    ) -> dict[str, object] | None:
        row = self.store.fetch_one(
            """
            WITH current_status AS (
                SELECT status_event.fact_id, status_event.status
                FROM knowledge_fact_status_events AS status_event
                WHERE NOT EXISTS (
                    SELECT 1 FROM knowledge_fact_status_events AS newer
                    WHERE newer.fact_id = status_event.fact_id
                      AND newer.sequence > status_event.sequence
                )
            )
            SELECT fact.fact_id, fact.value_json, fact.object_text, fact.object_kind,
                   fact.fact_kind, fact.question_axis, fact.basis, fact.task_scope
            FROM knowledge_facts AS fact
            JOIN current_status AS status ON status.fact_id = fact.fact_id
            WHERE fact.workspace_id = ? AND fact.subject_entity_id = ?
              AND fact.predicate = ? AND fact.scope_key = ? AND status.status = 'active'
            ORDER BY fact.recorded_at DESC, fact.fact_id DESC
            LIMIT 1
            """,
            (workspace_id, subject_entity_id, predicate, scope_key),
        )
        return None if row is None else dict(row)

    def _ensure_fact(
        self,
        *,
        workspace_id: str,
        subject_entity_id: str,
        question_axis: QuestionAxis,
        predicate: str,
        scope_key: str,
        object_kind: ObjectKind,
        object_text: str,
        value: object,
        source: KnowledgeSource,
        recorded_at: datetime,
        task_scope: str,
        fact_kind: FactKind = FactKind.ATTRIBUTE,
        object_entity_id: str | None = None,
        object_document_id: str | None = None,
        basis: FactBasis = FactBasis.DETERMINISTIC,
        confidence: float = 1.0,
        valid_from: date | None = None,
    ) -> str:
        current = self._current_active_fact(
            workspace_id=workspace_id,
            subject_entity_id=subject_entity_id,
            predicate=predicate,
            scope_key=scope_key,
        )
        expected = (
            canonical_json(value),
            object_text,
            object_kind.value,
            fact_kind.value,
            question_axis.value,
            basis.value,
            task_scope,
        )
        if current is not None:
            actual = (
                str(current["value_json"]),
                str(current["object_text"]),
                str(current["object_kind"]),
                str(current["fact_kind"]),
                str(current["question_axis"]),
                str(current["basis"]),
                str(current["task_scope"]),
            )
            if actual == expected:
                return str(current["fact_id"])
        supersedes = None if current is None else str(current["fact_id"])
        fact_id = _stable_id(
            "kfact",
            {
                "workspaceId": workspace_id,
                "subjectEntityId": subject_entity_id,
                "predicate": predicate,
                "scopeKey": scope_key,
                "value": value,
                "sourceRef": source.ref,
                "supersedesFactId": supersedes,
            },
        )
        self.knowledge.record_fact(
            KnowledgeFact(
                fact_id=fact_id,
                workspace_id=workspace_id,
                question_axis=question_axis,
                fact_kind=fact_kind,
                subject_entity_id=subject_entity_id,
                predicate=predicate,
                scope_key=scope_key,
                object_kind=object_kind,
                object_text=object_text,
                object_entity_id=object_entity_id,
                object_document_id=object_document_id,
                value=value,
                source=source,
                basis=basis,
                confidence=confidence,
                valid_from=valid_from,
                recorded_at=recorded_at,
                task_scope=task_scope,
                supersedes_fact_id=supersedes,
            )
        )
        return fact_id

    def _owner_source(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        turn_id: str | None,
        fallback_content: str,
        fallback_at: datetime,
        task_scope: str,
        deterministic_ref: str,
    ) -> KnowledgeSource:
        if turn_id is None:
            return KnowledgeSource(SourceKind.DETERMINISTIC, deterministic_ref)
        turn = self.store.fetch_one(
            """
            SELECT role, content, occurred_at FROM conversation_turns
            WHERE workspace_id = ? AND turn_id = ?
            """,
            (workspace_id, turn_id),
        )
        if turn is not None and str(turn["role"]) != "owner":
            return KnowledgeSource(SourceKind.DETERMINISTIC, deterministic_ref)
        content = fallback_content if turn is None else str(turn["content"])
        occurred_at = fallback_at if turn is None else _parse_timestamp(str(turn["occurred_at"]))
        statement_id = self.knowledge.record_owner_turn(
            workspace_id=workspace_id,
            thread_id=thread_id,
            turn_id=turn_id,
            content=content,
            occurred_at=occurred_at,
            # One committed owner turn can support several finance lanes.  Store it
            # once in the general tier; task-specific retrieval always includes it.
            task_scope="general",
        )
        return KnowledgeSource(
            SourceKind.OWNER_TURN,
            statement_id,
            turn_id=turn_id,
        )

    def bootstrap_committed_finance(
        self,
        *,
        workspace_id: str,
        as_of: date,
        recorded_at: datetime,
        limit_per_axis: int = 8,
    ) -> BusinessSummary:
        """Materialise a Koru-general first look, then return its current summary."""

        workspace = self.store.fetch_one(
            "SELECT * FROM workspaces WHERE workspace_id = ?", (workspace_id,)
        )
        if workspace is None:
            raise KeyError(f"unknown workspace: {workspace_id}")
        thread_id = str(workspace["thread_id"])
        workspace_source = KnowledgeSource(
            SourceKind.DETERMINISTIC,
            f"workspace:{workspace_id}",
        )
        workspace_created_at = _parse_timestamp(str(workspace["created_at"]))
        business_entity_id = _stable_id("kentity", {"workspaceId": workspace_id})
        self.knowledge.record_entity(
            KnowledgeEntity(
                entity_id=business_entity_id,
                workspace_id=workspace_id,
                entity_type=EntityType.ORGANISATION,
                canonical_name="Business workspace",
                source=workspace_source,
                recorded_at=workspace_created_at,
                task_scope="business_profile",
                metadata={"financeWorkspaceId": workspace_id},
            )
        )
        self._ensure_fact(
            workspace_id=workspace_id,
            subject_entity_id=business_entity_id,
            question_axis=QuestionAxis.WHO,
            predicate="business.name",
            scope_key=f"workspace:{workspace_id}:identity",
            object_kind=ObjectKind.TEXT,
            object_text=str(workspace["name"]),
            value=str(workspace["name"]),
            source=workspace_source,
            recorded_at=workspace_created_at,
            task_scope="business_profile",
        )
        self._ensure_fact(
            workspace_id=workspace_id,
            subject_entity_id=business_entity_id,
            question_axis=QuestionAxis.WHAT,
            predicate="business.entity_type",
            scope_key=f"workspace:{workspace_id}:entity_type",
            object_kind=ObjectKind.TEXT,
            object_text=str(workspace["entity_type"]),
            value=str(workspace["entity_type"]),
            source=workspace_source,
            recorded_at=recorded_at,
            task_scope="business_profile",
        )
        self._ensure_fact(
            workspace_id=workspace_id,
            subject_entity_id=business_entity_id,
            question_axis=QuestionAxis.WHEN,
            predicate="finance.data_through",
            scope_key=f"workspace:{workspace_id}:data_through",
            object_kind=ObjectKind.DATE,
            object_text=str(workspace["data_through"]),
            value=str(workspace["data_through"]),
            source=workspace_source,
            recorded_at=recorded_at,
            task_scope="reconciliation",
        )
        reserve_minor = int(workspace["protected_reserve_minor"])
        self._ensure_fact(
            workspace_id=workspace_id,
            subject_entity_id=business_entity_id,
            question_axis=QuestionAxis.WHY,
            predicate="policy.protected_reserve",
            scope_key=f"workspace:{workspace_id}:protected_reserve",
            object_kind=ObjectKind.NUMBER,
            object_text=f"Keep at least NZD {reserve_minor / 100:,.2f} protected.",
            value={"amountMinor": reserve_minor, "currency": str(workspace["currency"])},
            source=workspace_source,
            recorded_at=recorded_at,
            task_scope="cash_flow",
        )

        place_entity_id = _stable_id(
            "kentity", {"workspaceId": workspace_id, "timezone": workspace["timezone"]}
        )
        self.knowledge.record_entity(
            KnowledgeEntity(
                entity_id=place_entity_id,
                workspace_id=workspace_id,
                entity_type=EntityType.PLACE,
                canonical_name=str(workspace["timezone"]),
                source=workspace_source,
                recorded_at=workspace_created_at,
                task_scope="business_profile",
            )
        )
        self._ensure_fact(
            workspace_id=workspace_id,
            subject_entity_id=business_entity_id,
            question_axis=QuestionAxis.WHERE,
            predicate="business.operates_in",
            scope_key=f"workspace:{workspace_id}:operating_timezone",
            object_kind=ObjectKind.ENTITY,
            object_text=str(workspace["timezone"]),
            value={"entityId": place_entity_id},
            object_entity_id=place_entity_id,
            fact_kind=FactKind.RELATIONSHIP,
            source=workspace_source,
            recorded_at=recorded_at,
            task_scope="business_profile",
        )

        accounts = self.store.fetch_all(
            """
            SELECT account_id, name, currency, created_at FROM accounts
            WHERE workspace_id = ? ORDER BY account_id
            """,
            (workspace_id,),
        )
        for account in accounts:
            account_id = str(account["account_id"])
            account_source = KnowledgeSource(SourceKind.DETERMINISTIC, f"account:{account_id}")
            account_entity_id = _stable_id(
                "kentity",
                {
                    "workspaceId": workspace_id,
                    "accountId": account_id,
                    "name": str(account["name"]),
                    "currency": str(account["currency"]),
                },
            )
            self.knowledge.record_entity(
                KnowledgeEntity(
                    entity_id=account_entity_id,
                    workspace_id=workspace_id,
                    entity_type=EntityType.ACCOUNT,
                    canonical_name=str(account["name"]),
                    source=account_source,
                    recorded_at=_parse_timestamp(str(account["created_at"])),
                    task_scope="reconciliation",
                    metadata={"financeAccountId": account_id, "currency": str(account["currency"])},
                )
            )
            self._ensure_fact(
                workspace_id=workspace_id,
                subject_entity_id=business_entity_id,
                question_axis=QuestionAxis.WHAT,
                predicate="business.has_account",
                scope_key=f"account:{account_id}",
                object_kind=ObjectKind.ENTITY,
                object_text=str(account["name"]),
                value={"entityId": account_entity_id, "financeAccountId": account_id},
                object_entity_id=account_entity_id,
                fact_kind=FactKind.RELATIONSHIP,
                source=account_source,
                recorded_at=recorded_at,
                task_scope="reconciliation",
            )

        transaction_snapshot = self.store.fetch_one(
            """
            SELECT COUNT(*) AS transaction_count,
                   COALESCE(SUM(CASE WHEN status = 'posted' THEN amount_minor ELSE 0 END), 0)
                       AS posted_net_minor,
                   COALESCE(SUM(CASE WHEN classification = 'unresolved' THEN 1 ELSE 0 END), 0)
                       AS unresolved_count
            FROM transactions WHERE workspace_id = ?
            """,
            (workspace_id,),
        )
        if transaction_snapshot is None:
            raise RuntimeError("transaction aggregate did not return a row")
        transaction_value = {
            "transactionCount": int(transaction_snapshot["transaction_count"]),
            "postedNetMinor": int(transaction_snapshot["posted_net_minor"]),
            "unresolvedCount": int(transaction_snapshot["unresolved_count"]),
            "currency": str(workspace["currency"]),
            "dataThrough": str(workspace["data_through"]),
        }
        snapshot_ref = _stable_id("finance_snapshot", transaction_value)
        self._ensure_fact(
            workspace_id=workspace_id,
            subject_entity_id=business_entity_id,
            question_axis=QuestionAxis.WHAT,
            predicate="finance.transaction_snapshot",
            scope_key=f"workspace:{workspace_id}:transaction_snapshot",
            object_kind=ObjectKind.JSON,
            object_text=(
                f"{transaction_value['transactionCount']} committed transactions; "
                f"{transaction_value['unresolvedCount']} unresolved."
            ),
            value=transaction_value,
            source=KnowledgeSource(SourceKind.DETERMINISTIC, snapshot_ref),
            recorded_at=recorded_at,
            task_scope="reconciliation",
        )

        source_items = self.store.fetch_all(
            """
            SELECT source_item_id, source_type, label, digest, received_at, status, row_count
            FROM source_items WHERE workspace_id = ? ORDER BY received_at, source_item_id
            """,
            (workspace_id,),
        )
        document_kinds = {
            "csv": DocumentKind.BANK_STATEMENT,
            "akahu_fixture": DocumentKind.BANK_STATEMENT,
            "telegram_fixture": DocumentKind.CORRESPONDENCE,
            "owner_claim": DocumentKind.NOTE,
        }
        for item in source_items:
            source_item_id = str(item["source_item_id"])
            document_id = _stable_id(
                "kdoc",
                {
                    "workspaceId": workspace_id,
                    "sourceItemId": source_item_id,
                    "digest": str(item["digest"]),
                    "status": str(item["status"]),
                    "rowCount": int(item["row_count"]),
                },
            )
            source_kind = (
                SourceKind.CONNECTOR
                if str(item["source_type"]) in {"telegram_fixture", "akahu_fixture"}
                else SourceKind.IMPORT
            )
            source = KnowledgeSource(source_kind, f"source_item:{source_item_id}")
            self.knowledge.record_document(
                KnowledgeDocument(
                    document_id=document_id,
                    workspace_id=workspace_id,
                    document_kind=document_kinds[str(item["source_type"])],
                    title=str(item["label"]),
                    extracted_text=(
                        f"{item['label']}. {int(item['row_count'])} source rows; "
                        f"processing status {item['status']}."
                    ),
                    content_hash=str(item["digest"]),
                    source=source,
                    received_at=_parse_timestamp(str(item["received_at"])),
                    task_scope="documents",
                    metadata={
                        "financeSourceItemId": source_item_id,
                        "sourceType": str(item["source_type"]),
                        "status": str(item["status"]),
                    },
                )
            )
            self._ensure_fact(
                workspace_id=workspace_id,
                subject_entity_id=business_entity_id,
                question_axis=QuestionAxis.WHAT,
                predicate="business.source_document",
                scope_key=f"source_item:{source_item_id}",
                object_kind=ObjectKind.DOCUMENT,
                object_text=str(item["label"]),
                object_document_id=document_id,
                value={"documentId": document_id, "sourceItemId": source_item_id},
                source=source,
                recorded_at=recorded_at,
                task_scope="documents",
            )

        claims = self.store.fetch_all(
            """
            SELECT claim_id, claim_type, statement, source_turn_id, scope_json,
                   effective_date, recorded_at
            FROM claims WHERE workspace_id = ? AND status = 'active'
            ORDER BY recorded_at, claim_id
            """,
            (workspace_id,),
        )
        active_claim_scopes: set[str] = set()
        claim_axes = {
            "reserve_policy": QuestionAxis.WHY,
            "planned_expense": QuestionAxis.WHEN,
            "classification_instruction": QuestionAxis.WHAT,
            "business_context": QuestionAxis.WHAT,
        }
        for claim in claims:
            claim_id = str(claim["claim_id"])
            scope_key = f"claim:{claim_id}"
            active_claim_scopes.add(scope_key)
            claim_recorded_at = _parse_timestamp(str(claim["recorded_at"]))
            source = self._owner_source(
                workspace_id=workspace_id,
                thread_id=thread_id,
                turn_id=cast(str | None, claim["source_turn_id"]),
                fallback_content=str(claim["statement"]),
                fallback_at=claim_recorded_at,
                task_scope="business_profile",
                deterministic_ref=f"claim:{claim_id}",
            )
            self._ensure_fact(
                workspace_id=workspace_id,
                subject_entity_id=business_entity_id,
                question_axis=claim_axes[str(claim["claim_type"])],
                predicate=f"claim.{claim['claim_type']}",
                scope_key=scope_key,
                object_kind=ObjectKind.JSON,
                object_text=str(claim["statement"]),
                value={
                    "claimId": claim_id,
                    "statement": str(claim["statement"]),
                    "scope": json.loads(str(claim["scope_json"])),
                },
                source=source,
                recorded_at=claim_recorded_at,
                task_scope="business_profile",
                basis=FactBasis.EXPLICIT,
                valid_from=date.fromisoformat(str(claim["effective_date"])),
            )

        rules = self.store.fetch_all(
            """
            SELECT rule_id, merchant_contains, maximum_amount_minor, currency,
                   target_classification, target_category, effective_from,
                   source_turn_id, source_claim_id, created_at
            FROM classification_rules
            WHERE workspace_id = ? AND active = 1
            ORDER BY priority, created_at, rule_id
            """,
            (workspace_id,),
        )
        active_rule_scopes: set[str] = set()
        for rule in rules:
            rule_id = str(rule["rule_id"])
            scope_key = f"classification_rule:{rule_id}"
            active_rule_scopes.add(scope_key)
            rule_recorded_at = _parse_timestamp(str(rule["created_at"]))
            statement = (
                f"Classify {rule['merchant_contains']} up to {rule['maximum_amount_minor']} "
                f"{rule['currency']} minor units as {rule['target_classification']}."
            )
            source = self._owner_source(
                workspace_id=workspace_id,
                thread_id=thread_id,
                turn_id=cast(str | None, rule["source_turn_id"]),
                fallback_content=statement,
                fallback_at=rule_recorded_at,
                task_scope="categorisation",
                deterministic_ref=f"classification_rule:{rule_id}",
            )
            self._ensure_fact(
                workspace_id=workspace_id,
                subject_entity_id=business_entity_id,
                question_axis=QuestionAxis.WHAT,
                predicate="classification.rule",
                scope_key=scope_key,
                object_kind=ObjectKind.JSON,
                object_text=statement,
                value={
                    "ruleId": rule_id,
                    "merchantContains": str(rule["merchant_contains"]),
                    "maximumAmountMinor": int(rule["maximum_amount_minor"]),
                    "currency": str(rule["currency"]),
                    "targetClassification": str(rule["target_classification"]),
                    "targetCategory": cast(str | None, rule["target_category"]),
                    "sourceClaimId": cast(str | None, rule["source_claim_id"]),
                },
                source=source,
                recorded_at=rule_recorded_at,
                task_scope="categorisation",
                basis=(
                    FactBasis.EXPLICIT
                    if source.kind is SourceKind.OWNER_TURN
                    else FactBasis.DETERMINISTIC
                ),
                valid_from=date.fromisoformat(str(rule["effective_from"])),
            )

        self._retract_stale_scoped_facts(
            workspace_id=workspace_id,
            scope_prefix="claim:",
            active_scopes=active_claim_scopes,
            source=workspace_source,
            occurred_at=recorded_at,
        )
        self._retract_stale_scoped_facts(
            workspace_id=workspace_id,
            scope_prefix="classification_rule:",
            active_scopes=active_rule_scopes,
            source=workspace_source,
            occurred_at=recorded_at,
        )
        return self.knowledge.current_business_summary(
            workspace_id=workspace_id,
            as_of=as_of,
            limit_per_axis=limit_per_axis,
            generated_at=recorded_at,
        )

    def _retract_stale_scoped_facts(
        self,
        *,
        workspace_id: str,
        scope_prefix: str,
        active_scopes: set[str],
        source: KnowledgeSource,
        occurred_at: datetime,
    ) -> None:
        rows = self.store.fetch_all(
            """
            WITH current_status AS (
                SELECT status_event.fact_id, status_event.status
                FROM knowledge_fact_status_events AS status_event
                WHERE NOT EXISTS (
                    SELECT 1 FROM knowledge_fact_status_events AS newer
                    WHERE newer.fact_id = status_event.fact_id
                      AND newer.sequence > status_event.sequence
                )
            )
            SELECT fact.fact_id, fact.scope_key
            FROM knowledge_facts AS fact
            JOIN current_status AS status ON status.fact_id = fact.fact_id
            WHERE fact.workspace_id = ? AND fact.scope_key LIKE ?
              AND status.status = 'active'
            """,
            (workspace_id, f"{scope_prefix}%"),
        )
        for row in rows:
            if str(row["scope_key"]) in active_scopes:
                continue
            self.knowledge.transition_fact(
                fact_id=str(row["fact_id"]),
                target_status=FactStatus.RETRACTED,
                source=source,
                reason="The committed finance source is no longer active.",
                occurred_at=occurred_at,
            )


def bootstrap_committed_finance(
    store: SQLiteStore,
    *,
    workspace_id: str,
    as_of: date,
    recorded_at: datetime,
    limit_per_axis: int = 8,
) -> BusinessSummary:
    """Functional entry point for the deterministic first-look compiler."""

    return CommittedFinanceKnowledgeCompiler(store).bootstrap_committed_finance(
        workspace_id=workspace_id,
        as_of=as_of,
        recorded_at=recorded_at,
        limit_per_axis=limit_per_axis,
    )


__all__ = ["CommittedFinanceKnowledgeCompiler", "bootstrap_committed_finance"]
