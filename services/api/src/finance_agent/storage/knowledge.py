"""Append-only, source-linked working knowledge for the local finance agent.

This module deliberately has no finance or ledger write methods.  It stores what the
owner said, structured business entities/documents, and typed candidate facts.  A fact
only becomes current business context through an append-only status event; financial
truth remains owned by the deterministic finance store.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import cast

from .store import SQLiteStore, canonical_json

SUMMARY_QUERY_VERSION = "working-knowledge-summary-v1"
RETRIEVAL_QUERY_VERSION = "working-knowledge-fts-v1"
_TASK_SCOPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_PREDICATE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,95}$")
_SEARCH_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


class SourceKind(StrEnum):
    OWNER_TURN = "owner_turn"
    DOCUMENT = "document"
    FINANCE_EVIDENCE = "finance_evidence"
    CONNECTOR = "connector"
    DETERMINISTIC = "deterministic"
    MODEL_CANDIDATE = "model_candidate"
    IMPORT = "import"


class EntityType(StrEnum):
    PERSON = "person"
    ORGANISATION = "organisation"
    PLACE = "place"
    PROJECT = "project"
    CUSTOMER = "customer"
    SUPPLIER = "supplier"
    ACCOUNT = "account"
    ASSET = "asset"
    SERVICE = "service"
    EVENT = "event"
    OTHER = "other"


class DocumentKind(StrEnum):
    RECEIPT = "receipt"
    INVOICE = "invoice"
    CONTRACT = "contract"
    BANK_STATEMENT = "bank_statement"
    TAX_DOCUMENT = "tax_document"
    CORRESPONDENCE = "correspondence"
    NOTE = "note"
    OTHER = "other"


class QuestionAxis(StrEnum):
    WHO = "who"
    WHAT = "what"
    WHERE = "where"
    WHEN = "when"
    WHY = "why"


class FactKind(StrEnum):
    ATTRIBUTE = "attribute"
    RELATIONSHIP = "relationship"


class ObjectKind(StrEnum):
    TEXT = "text"
    ENTITY = "entity"
    DOCUMENT = "document"
    DATE = "date"
    DATETIME = "datetime"
    BOOLEAN = "boolean"
    NUMBER = "number"
    JSON = "json"


class FactBasis(StrEnum):
    EXPLICIT = "explicit"
    DOCUMENT_EXTRACTED = "document_extracted"
    DETERMINISTIC = "deterministic"
    INFERRED = "inferred"
    HYPOTHETICAL = "hypothetical"


class FactStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"
    REJECTED = "rejected"


class ContradictionStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True)
class KnowledgeSource:
    kind: SourceKind
    ref: str
    turn_id: str | None = None
    document_id: str | None = None
    evidence_id: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeEntity:
    entity_id: str
    workspace_id: str
    entity_type: EntityType
    canonical_name: str
    source: KnowledgeSource
    recorded_at: datetime
    task_scope: str = "general"
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KnowledgeDocument:
    document_id: str
    workspace_id: str
    document_kind: DocumentKind
    title: str
    extracted_text: str
    content_hash: str
    source: KnowledgeSource
    received_at: datetime
    task_scope: str = "general"
    effective_from: date | None = None
    effective_until: date | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class KnowledgeFact:
    fact_id: str
    workspace_id: str
    question_axis: QuestionAxis
    fact_kind: FactKind
    subject_entity_id: str
    predicate: str
    scope_key: str
    object_kind: ObjectKind
    object_text: str
    value: object
    source: KnowledgeSource
    basis: FactBasis
    confidence: float
    recorded_at: datetime
    task_scope: str = "general"
    object_entity_id: str | None = None
    object_document_id: str | None = None
    valid_from: date | None = None
    valid_until: date | None = None
    supersedes_fact_id: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeHit:
    record_type: str
    record_id: str
    task_scope: str
    title: str
    excerpt: str
    score: float
    source: KnowledgeSource
    status: FactStatus | None = None
    status_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeRetrieval:
    receipt_id: str
    workspace_id: str
    task_scope: str
    as_of: date
    hits: tuple[KnowledgeHit, ...]


@dataclass(frozen=True, slots=True)
class BusinessSummary:
    summary_id: str
    revision: int
    workspace_id: str
    as_of: date
    content_hash: str
    payload: Mapping[str, object]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _payload_hash(value: object) -> str:
    return _sha256_text(canonical_json(value))


def _content_id(prefix: str, value: object) -> str:
    return f"{prefix}_{_payload_hash(value)[:24]}"


def _aware_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("knowledge timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _validate_name(value: str, field_name: str) -> str:
    normalised = value.strip()
    if not normalised:
        raise ValueError(f"{field_name} must not be blank")
    return normalised


def _validate_task_scope(value: str) -> str:
    if _TASK_SCOPE_PATTERN.fullmatch(value) is None:
        raise ValueError("task_scope must be a stable lowercase identifier")
    return value


def _validate_predicate(value: str) -> str:
    if _PREDICATE_PATTERN.fullmatch(value) is None:
        raise ValueError("predicate must be a stable lowercase identifier")
    return value


def _validate_interval(start: date | None, end: date | None) -> None:
    if start is not None and end is not None and start > end:
        raise ValueError("valid_from cannot be after valid_until")


def _source_payload(source: KnowledgeSource) -> dict[str, str | None]:
    return {
        "kind": source.kind.value,
        "ref": _validate_name(source.ref, "source.ref"),
        "turnId": source.turn_id,
        "documentId": source.document_id,
        "evidenceId": source.evidence_id,
    }


def _source_from_row(row: sqlite3.Row) -> KnowledgeSource:
    return KnowledgeSource(
        kind=SourceKind(str(row["source_kind"])),
        ref=str(row["source_ref"]),
        turn_id=cast(str | None, row["source_turn_id"]),
        document_id=cast(str | None, row["source_document_id"]),
        evidence_id=cast(str | None, row["evidence_id"]),
    )


def _fts_expression(query: str) -> str | None:
    tokens: list[str] = []
    seen: set[str] = set()
    for token in _SEARCH_TOKEN_PATTERN.findall(query.casefold()):
        if len(token) < 2 or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
        if len(tokens) == 12:
            break
    if not tokens:
        return None
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


class SQLiteKnowledgeStore:
    """Source-linked working memory with append-only state transitions."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _ensure_workspace(connection: sqlite3.Connection, workspace_id: str) -> None:
        if (
            connection.execute(
                "SELECT 1 FROM workspaces WHERE workspace_id = ?", (workspace_id,)
            ).fetchone()
            is None
        ):
            raise KeyError(f"unknown workspace: {workspace_id}")

    @staticmethod
    def _idempotent_row(
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        record_id: str,
        record_hash: str,
    ) -> bool:
        row = connection.execute(
            f'SELECT record_hash FROM "{table}" WHERE "{id_column}" = ?', (record_id,)
        ).fetchone()
        if row is None:
            return False
        if str(row["record_hash"]) != record_hash:
            raise ValueError(f"{id_column} is already bound to different content: {record_id}")
        return True

    @staticmethod
    def _validate_source(
        connection: sqlite3.Connection, workspace_id: str, source: KnowledgeSource
    ) -> None:
        _source_payload(source)
        if source.turn_id is not None:
            turn = connection.execute(
                "SELECT workspace_id FROM conversation_turns WHERE turn_id = ?",
                (source.turn_id,),
            ).fetchone()
            if turn is None or str(turn["workspace_id"]) != workspace_id:
                raise ValueError("source turn does not belong to the knowledge workspace")
        if source.document_id is not None:
            document = connection.execute(
                "SELECT workspace_id FROM knowledge_documents WHERE document_id = ?",
                (source.document_id,),
            ).fetchone()
            if document is None or str(document["workspace_id"]) != workspace_id:
                raise ValueError("source document does not belong to the knowledge workspace")
        if source.evidence_id is not None:
            evidence = connection.execute(
                "SELECT workspace_id FROM evidence_links WHERE evidence_id = ?",
                (source.evidence_id,),
            ).fetchone()
            if evidence is None or str(evidence["workspace_id"]) != workspace_id:
                raise ValueError("source evidence does not belong to the knowledge workspace")
        if source.kind is SourceKind.OWNER_TURN and source.turn_id is None:
            raise ValueError("owner_turn knowledge requires a source turn")
        if source.kind is SourceKind.OWNER_TURN:
            statement = connection.execute(
                """
                SELECT workspace_id, turn_id FROM knowledge_owner_statements
                WHERE statement_id = ?
                """,
                (source.ref,),
            ).fetchone()
            if (
                statement is None
                or str(statement["workspace_id"]) != workspace_id
                or str(statement["turn_id"]) != source.turn_id
            ):
                raise ValueError("owner_turn source must reference immutable owner_said evidence")
        if source.kind is SourceKind.DOCUMENT and source.document_id is None:
            raise ValueError("document knowledge requires a source document")
        if source.kind is SourceKind.FINANCE_EVIDENCE and source.evidence_id is None:
            raise ValueError("finance_evidence knowledge requires an evidence link")

    def record_owner_turn(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        turn_id: str,
        content: str,
        occurred_at: datetime,
        task_scope: str = "general",
    ) -> str:
        """Persist an immutable explicit owner statement and its transcript source."""

        content = _validate_name(content, "content")
        task_scope = _validate_task_scope(task_scope)
        occurred_at_text = _aware_timestamp(occurred_at)
        statement_id = _content_id("kstmt", {"workspaceId": workspace_id, "turnId": turn_id})
        content_hash = _sha256_text(content)
        payload = {
            "statementId": statement_id,
            "workspaceId": workspace_id,
            "threadId": thread_id,
            "turnId": turn_id,
            "content": content,
            "taskScope": task_scope,
            "occurredAt": occurred_at_text,
            "contentHash": content_hash,
        }
        record_hash = _payload_hash(payload)
        with self.store.transaction() as connection:
            self._ensure_workspace(connection, workspace_id)
            existing_turn = connection.execute(
                """
                SELECT workspace_id, thread_id, role, content
                FROM conversation_turns WHERE turn_id = ?
                """,
                (turn_id,),
            ).fetchone()
            if existing_turn is None:
                connection.execute(
                    """
                    INSERT INTO conversation_turns(
                        turn_id, workspace_id, thread_id, role, content, occurred_at,
                        status, evidence_ids_json
                    ) VALUES (?, ?, ?, 'owner', ?, ?, 'complete', '[]')
                    """,
                    (turn_id, workspace_id, thread_id, content, occurred_at_text),
                )
            elif (
                str(existing_turn["workspace_id"]),
                str(existing_turn["thread_id"]),
                str(existing_turn["role"]),
                str(existing_turn["content"]),
            ) != (workspace_id, thread_id, "owner", content):
                raise ValueError(f"turn_id is already bound to different content: {turn_id}")
            if self._idempotent_row(
                connection,
                table="knowledge_owner_statements",
                id_column="statement_id",
                record_id=statement_id,
                record_hash=record_hash,
            ):
                return statement_id
            connection.execute(
                """
                INSERT INTO knowledge_owner_statements(
                    statement_id, workspace_id, thread_id, turn_id, content, task_scope,
                    occurred_at, content_hash, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    statement_id,
                    workspace_id,
                    thread_id,
                    turn_id,
                    content,
                    task_scope,
                    occurred_at_text,
                    content_hash,
                    record_hash,
                ),
            )
        return statement_id

    def record_entity(self, entity: KnowledgeEntity) -> None:
        name = _validate_name(entity.canonical_name, "canonical_name")
        task_scope = _validate_task_scope(entity.task_scope)
        recorded_at = _aware_timestamp(entity.recorded_at)
        source_payload = _source_payload(entity.source)
        metadata_json = canonical_json(dict(entity.metadata))
        payload = {
            "entityId": entity.entity_id,
            "workspaceId": entity.workspace_id,
            "entityType": entity.entity_type.value,
            "canonicalName": name,
            "taskScope": task_scope,
            "source": source_payload,
            "recordedAt": recorded_at,
            "metadata": dict(entity.metadata),
        }
        record_hash = _payload_hash(payload)
        with self.store.transaction() as connection:
            self._ensure_workspace(connection, entity.workspace_id)
            self._validate_source(connection, entity.workspace_id, entity.source)
            if self._idempotent_row(
                connection,
                table="knowledge_entities",
                id_column="entity_id",
                record_id=entity.entity_id,
                record_hash=record_hash,
            ):
                return
            connection.execute(
                """
                INSERT INTO knowledge_entities(
                    entity_id, workspace_id, entity_type, canonical_name, task_scope,
                    source_kind, source_ref, source_turn_id, source_document_id,
                    evidence_id, recorded_at, metadata_json, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity.entity_id,
                    entity.workspace_id,
                    entity.entity_type.value,
                    name,
                    task_scope,
                    entity.source.kind.value,
                    entity.source.ref,
                    entity.source.turn_id,
                    entity.source.document_id,
                    entity.source.evidence_id,
                    recorded_at,
                    metadata_json,
                    record_hash,
                ),
            )

    def record_document(self, document: KnowledgeDocument) -> None:
        title = _validate_name(document.title, "title")
        task_scope = _validate_task_scope(document.task_scope)
        _validate_interval(document.effective_from, document.effective_until)
        if len(document.content_hash) != 64:
            raise ValueError("document content_hash must be a SHA-256 digest")
        received_at = _aware_timestamp(document.received_at)
        source_payload = _source_payload(document.source)
        payload = {
            "documentId": document.document_id,
            "workspaceId": document.workspace_id,
            "documentKind": document.document_kind.value,
            "title": title,
            "taskScope": task_scope,
            "source": source_payload,
            "receivedAt": received_at,
            "effectiveFrom": _date_text(document.effective_from),
            "effectiveUntil": _date_text(document.effective_until),
            "extractedText": document.extracted_text,
            "contentHash": document.content_hash,
            "metadata": dict(document.metadata),
        }
        record_hash = _payload_hash(payload)
        with self.store.transaction() as connection:
            self._ensure_workspace(connection, document.workspace_id)
            self._validate_source(connection, document.workspace_id, document.source)
            if self._idempotent_row(
                connection,
                table="knowledge_documents",
                id_column="document_id",
                record_id=document.document_id,
                record_hash=record_hash,
            ):
                return
            connection.execute(
                """
                INSERT INTO knowledge_documents(
                    document_id, workspace_id, document_kind, title, task_scope,
                    source_kind, source_ref, source_turn_id, evidence_id, received_at,
                    effective_from, effective_until, extracted_text, content_hash,
                    metadata_json, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    document.document_id,
                    document.workspace_id,
                    document.document_kind.value,
                    title,
                    task_scope,
                    document.source.kind.value,
                    document.source.ref,
                    document.source.turn_id,
                    document.source.evidence_id,
                    received_at,
                    _date_text(document.effective_from),
                    _date_text(document.effective_until),
                    document.extracted_text,
                    document.content_hash,
                    canonical_json(dict(document.metadata)),
                    record_hash,
                ),
            )

    @staticmethod
    def _fact_payload(fact: KnowledgeFact) -> tuple[dict[str, object], str, str]:
        _validate_predicate(fact.predicate)
        _validate_task_scope(fact.task_scope)
        _validate_name(fact.scope_key, "scope_key")
        _validate_name(fact.object_text, "object_text")
        _validate_interval(fact.valid_from, fact.valid_until)
        if not 0.0 <= fact.confidence <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        if fact.object_kind is ObjectKind.ENTITY:
            if fact.fact_kind is not FactKind.RELATIONSHIP or fact.object_entity_id is None:
                raise ValueError("entity objects require a relationship fact and object entity")
        elif fact.object_kind is ObjectKind.DOCUMENT:
            if fact.object_document_id is None:
                raise ValueError("document objects require an object document")
        elif (
            fact.fact_kind is not FactKind.ATTRIBUTE
            or fact.object_entity_id is not None
            or fact.object_document_id is not None
        ):
            raise ValueError("non-entity objects must be attribute facts")
        value_json = canonical_json(fact.value)
        value_hash = _sha256_text(value_json)
        recorded_at = _aware_timestamp(fact.recorded_at)
        payload: dict[str, object] = {
            "factId": fact.fact_id,
            "workspaceId": fact.workspace_id,
            "questionAxis": fact.question_axis.value,
            "factKind": fact.fact_kind.value,
            "subjectEntityId": fact.subject_entity_id,
            "predicate": fact.predicate,
            "scopeKey": fact.scope_key,
            "objectKind": fact.object_kind.value,
            "objectText": fact.object_text,
            "objectEntityId": fact.object_entity_id,
            "objectDocumentId": fact.object_document_id,
            "value": fact.value,
            "taskScope": fact.task_scope,
            "source": _source_payload(fact.source),
            "basis": fact.basis.value,
            "confidence": fact.confidence,
            "validFrom": _date_text(fact.valid_from),
            "validUntil": _date_text(fact.valid_until),
            "recordedAt": recorded_at,
            "supersedesFactId": fact.supersedes_fact_id,
        }
        return payload, value_json, value_hash

    @staticmethod
    def _current_fact_status(connection: sqlite3.Connection, fact_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                """
                SELECT event_id, fact_id, workspace_id, sequence, status, occurred_at
                FROM knowledge_fact_status_events AS status_event
                WHERE status_event.fact_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM knowledge_fact_status_events AS newer
                      WHERE newer.fact_id = status_event.fact_id
                        AND newer.sequence > status_event.sequence
                  )
                """,
                (fact_id,),
            ).fetchone(),
        )

    @staticmethod
    def _append_fact_status(
        connection: sqlite3.Connection,
        *,
        fact_id: str,
        workspace_id: str,
        status: FactStatus,
        reason: str,
        source: KnowledgeSource,
        occurred_at: datetime,
        event_discriminator: str,
    ) -> str:
        current = SQLiteKnowledgeStore._current_fact_status(connection, fact_id)
        sequence = 1 if current is None else int(current["sequence"]) + 1
        event_id = _content_id(
            "kfse",
            {
                "factId": fact_id,
                "sequence": sequence,
                "status": status.value,
                "discriminator": event_discriminator,
            },
        )
        occurred_at_text = _aware_timestamp(occurred_at)
        payload = {
            "eventId": event_id,
            "factId": fact_id,
            "workspaceId": workspace_id,
            "sequence": sequence,
            "status": status.value,
            "reason": reason,
            "source": _source_payload(source),
            "occurredAt": occurred_at_text,
        }
        connection.execute(
            """
            INSERT INTO knowledge_fact_status_events(
                event_id, fact_id, workspace_id, sequence, status, reason,
                source_kind, source_ref, source_turn_id, occurred_at, record_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                fact_id,
                workspace_id,
                sequence,
                status.value,
                reason,
                source.kind.value,
                source.ref,
                source.turn_id,
                occurred_at_text,
                _payload_hash(payload),
            ),
        )
        return event_id

    @staticmethod
    def _supersede_previous(
        connection: sqlite3.Connection,
        *,
        replacement_fact_id: str,
        previous_fact_id: str,
        workspace_id: str,
        source: KnowledgeSource,
        occurred_at: datetime,
    ) -> None:
        current = SQLiteKnowledgeStore._current_fact_status(connection, previous_fact_id)
        if current is None:
            raise ValueError("superseded fact has no status")
        current_status = FactStatus(str(current["status"]))
        if current_status is FactStatus.SUPERSEDED:
            return
        if current_status is not FactStatus.ACTIVE:
            raise ValueError("only an active fact can be superseded")
        SQLiteKnowledgeStore._append_fact_status(
            connection,
            fact_id=previous_fact_id,
            workspace_id=workspace_id,
            status=FactStatus.SUPERSEDED,
            reason=f"Superseded by {replacement_fact_id} in the same knowledge scope.",
            source=source,
            occurred_at=occurred_at,
            event_discriminator=replacement_fact_id,
        )

    @staticmethod
    def _record_contradictions(
        connection: sqlite3.Connection,
        *,
        fact_id: str,
        workspace_id: str,
        detected_at: datetime,
    ) -> None:
        fact = connection.execute(
            "SELECT * FROM knowledge_facts WHERE fact_id = ?", (fact_id,)
        ).fetchone()
        if fact is None:
            raise KeyError(f"unknown knowledge fact: {fact_id}")
        candidates = connection.execute(
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
            SELECT other.fact_id
            FROM knowledge_facts AS other
            JOIN current_status AS status ON status.fact_id = other.fact_id
            WHERE other.workspace_id = ?
              AND other.fact_id <> ?
              AND other.scope_key = ?
              AND other.predicate = ?
              AND other.value_hash <> ?
              AND status.status IN ('active', 'candidate')
              AND COALESCE(other.valid_from, '0000-01-01')
                    <= COALESCE(?, '9999-12-31')
              AND COALESCE(?, '0000-01-01')
                    <= COALESCE(other.valid_until, '9999-12-31')
            ORDER BY other.fact_id
            """,
            (
                workspace_id,
                fact_id,
                fact["scope_key"],
                fact["predicate"],
                fact["value_hash"],
                fact["valid_until"],
                fact["valid_from"],
            ),
        ).fetchall()
        detected_at_text = _aware_timestamp(detected_at)
        for candidate in candidates:
            fact_a_id, fact_b_id = sorted((fact_id, str(candidate["fact_id"])))
            contradiction_id = _content_id(
                "kcontra",
                {
                    "workspaceId": workspace_id,
                    "factAId": fact_a_id,
                    "factBId": fact_b_id,
                    "reasonCode": "same_scope_different_value",
                },
            )
            payload = {
                "contradictionId": contradiction_id,
                "workspaceId": workspace_id,
                "factAId": fact_a_id,
                "factBId": fact_b_id,
                "scopeKey": fact["scope_key"],
                "predicate": fact["predicate"],
                "reasonCode": "same_scope_different_value",
                "detectedAt": detected_at_text,
            }
            existing = connection.execute(
                "SELECT contradiction_id FROM knowledge_contradictions WHERE contradiction_id = ?",
                (contradiction_id,),
            ).fetchone()
            if existing is not None:
                continue
            connection.execute(
                """
                INSERT INTO knowledge_contradictions(
                    contradiction_id, workspace_id, fact_a_id, fact_b_id, scope_key,
                    predicate, reason_code, detected_at, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, 'same_scope_different_value', ?, ?)
                """,
                (
                    contradiction_id,
                    workspace_id,
                    fact_a_id,
                    fact_b_id,
                    fact["scope_key"],
                    fact["predicate"],
                    detected_at_text,
                    _payload_hash(payload),
                ),
            )
            event_payload = {
                "contradictionId": contradiction_id,
                "sequence": 1,
                "status": "open",
                "detectedAt": detected_at_text,
            }
            connection.execute(
                """
                INSERT INTO knowledge_contradiction_status_events(
                    event_id, contradiction_id, workspace_id, sequence, status, reason,
                    source_kind, source_ref, source_turn_id, occurred_at, record_hash
                ) VALUES (?, ?, ?, 1, 'open', ?, 'deterministic', ?, NULL, ?, ?)
                """,
                (
                    _content_id("kcse", event_payload),
                    contradiction_id,
                    workspace_id,
                    "Different values overlap for the same scoped business fact.",
                    "working-knowledge-contradiction-v1",
                    detected_at_text,
                    _payload_hash(event_payload),
                ),
            )

    def record_fact(
        self,
        fact: KnowledgeFact,
        *,
        initial_status: FactStatus = FactStatus.ACTIVE,
        reason: str = "Recorded as current business context.",
    ) -> None:
        payload, value_json, value_hash = self._fact_payload(fact)
        record_hash = _payload_hash(payload)
        if (
            fact.basis in {FactBasis.INFERRED, FactBasis.HYPOTHETICAL}
            and initial_status is not FactStatus.CANDIDATE
        ):
            raise ValueError("inferred and hypothetical facts must start as candidates")
        if (
            fact.source.kind is SourceKind.MODEL_CANDIDATE
            and initial_status is not FactStatus.CANDIDATE
        ):
            raise ValueError("model-derived facts must start as candidates")
        if initial_status not in {FactStatus.ACTIVE, FactStatus.CANDIDATE}:
            raise ValueError("new facts must start active or candidate")
        with self.store.transaction() as connection:
            self._ensure_workspace(connection, fact.workspace_id)
            self._validate_source(connection, fact.workspace_id, fact.source)
            subject = connection.execute(
                "SELECT workspace_id FROM knowledge_entities WHERE entity_id = ?",
                (fact.subject_entity_id,),
            ).fetchone()
            if subject is None or str(subject["workspace_id"]) != fact.workspace_id:
                raise ValueError("fact subject does not belong to the knowledge workspace")
            if fact.object_entity_id is not None:
                target = connection.execute(
                    "SELECT workspace_id FROM knowledge_entities WHERE entity_id = ?",
                    (fact.object_entity_id,),
                ).fetchone()
                if target is None or str(target["workspace_id"]) != fact.workspace_id:
                    raise ValueError("fact object entity does not belong to the workspace")
            if fact.object_document_id is not None:
                target_document = connection.execute(
                    "SELECT workspace_id FROM knowledge_documents WHERE document_id = ?",
                    (fact.object_document_id,),
                ).fetchone()
                if (
                    target_document is None
                    or str(target_document["workspace_id"]) != fact.workspace_id
                ):
                    raise ValueError("fact object document does not belong to the workspace")
            if fact.supersedes_fact_id is not None:
                previous = connection.execute(
                    """
                    SELECT workspace_id, question_axis, subject_entity_id, predicate, scope_key
                    FROM knowledge_facts WHERE fact_id = ?
                    """,
                    (fact.supersedes_fact_id,),
                ).fetchone()
                expected_scope = (
                    fact.workspace_id,
                    fact.question_axis.value,
                    fact.subject_entity_id,
                    fact.predicate,
                    fact.scope_key,
                )
                actual_scope = None if previous is None else tuple(previous)
                if actual_scope != expected_scope:
                    raise ValueError("supersession must retain the exact subject and fact scope")
            if self._idempotent_row(
                connection,
                table="knowledge_facts",
                id_column="fact_id",
                record_id=fact.fact_id,
                record_hash=record_hash,
            ):
                return
            connection.execute(
                """
                INSERT INTO knowledge_facts(
                    fact_id, workspace_id, question_axis, fact_kind, subject_entity_id,
                    predicate, scope_key, object_kind, object_text, object_entity_id,
                    object_document_id, value_json, value_hash, task_scope, source_kind,
                    source_ref, source_turn_id, source_document_id, evidence_id, basis,
                    confidence, valid_from, valid_until, recorded_at, supersedes_fact_id,
                    record_hash
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    fact.fact_id,
                    fact.workspace_id,
                    fact.question_axis.value,
                    fact.fact_kind.value,
                    fact.subject_entity_id,
                    fact.predicate,
                    fact.scope_key,
                    fact.object_kind.value,
                    fact.object_text,
                    fact.object_entity_id,
                    fact.object_document_id,
                    value_json,
                    value_hash,
                    fact.task_scope,
                    fact.source.kind.value,
                    fact.source.ref,
                    fact.source.turn_id,
                    fact.source.document_id,
                    fact.source.evidence_id,
                    fact.basis.value,
                    fact.confidence,
                    _date_text(fact.valid_from),
                    _date_text(fact.valid_until),
                    _aware_timestamp(fact.recorded_at),
                    fact.supersedes_fact_id,
                    record_hash,
                ),
            )
            self._append_fact_status(
                connection,
                fact_id=fact.fact_id,
                workspace_id=fact.workspace_id,
                status=initial_status,
                reason=reason,
                source=fact.source,
                occurred_at=fact.recorded_at,
                event_discriminator="initial",
            )
            if initial_status is FactStatus.ACTIVE and fact.supersedes_fact_id is not None:
                self._supersede_previous(
                    connection,
                    replacement_fact_id=fact.fact_id,
                    previous_fact_id=fact.supersedes_fact_id,
                    workspace_id=fact.workspace_id,
                    source=fact.source,
                    occurred_at=fact.recorded_at,
                )
            self._record_contradictions(
                connection,
                fact_id=fact.fact_id,
                workspace_id=fact.workspace_id,
                detected_at=fact.recorded_at,
            )

    def transition_fact(
        self,
        *,
        fact_id: str,
        target_status: FactStatus,
        source: KnowledgeSource,
        reason: str,
        occurred_at: datetime,
    ) -> str:
        """Append a review decision; fact payloads are never updated in place."""

        if target_status not in {FactStatus.ACTIVE, FactStatus.RETRACTED, FactStatus.REJECTED}:
            raise ValueError("public transitions may activate, retract, or reject a fact")
        with self.store.transaction() as connection:
            fact = connection.execute(
                "SELECT * FROM knowledge_facts WHERE fact_id = ?", (fact_id,)
            ).fetchone()
            if fact is None:
                raise KeyError(f"unknown knowledge fact: {fact_id}")
            workspace_id = str(fact["workspace_id"])
            self._validate_source(connection, workspace_id, source)
            current = self._current_fact_status(connection, fact_id)
            if current is None:
                raise ValueError("knowledge fact has no status")
            current_status = FactStatus(str(current["status"]))
            if current_status is target_status:
                return str(current["event_id"])
            allowed = {
                FactStatus.CANDIDATE: {
                    FactStatus.ACTIVE,
                    FactStatus.RETRACTED,
                    FactStatus.REJECTED,
                },
                FactStatus.ACTIVE: {FactStatus.RETRACTED},
            }
            if target_status not in allowed.get(current_status, set()):
                raise ValueError(f"invalid fact transition: {current_status} -> {target_status}")
            event_id = self._append_fact_status(
                connection,
                fact_id=fact_id,
                workspace_id=workspace_id,
                status=target_status,
                reason=reason,
                source=source,
                occurred_at=occurred_at,
                event_discriminator=reason,
            )
            if target_status is FactStatus.ACTIVE and fact["supersedes_fact_id"] is not None:
                self._supersede_previous(
                    connection,
                    replacement_fact_id=fact_id,
                    previous_fact_id=str(fact["supersedes_fact_id"]),
                    workspace_id=workspace_id,
                    source=source,
                    occurred_at=occurred_at,
                )
            if target_status is FactStatus.ACTIVE:
                self._record_contradictions(
                    connection,
                    fact_id=fact_id,
                    workspace_id=workspace_id,
                    detected_at=occurred_at,
                )
            return event_id

    @staticmethod
    def _open_contradictions(
        connection: sqlite3.Connection, workspace_id: str
    ) -> list[sqlite3.Row]:
        return list(
            connection.execute(
                """
                WITH current_status AS (
                    SELECT status_event.*
                    FROM knowledge_contradiction_status_events AS status_event
                    WHERE NOT EXISTS (
                        SELECT 1 FROM knowledge_contradiction_status_events AS newer
                        WHERE newer.contradiction_id = status_event.contradiction_id
                          AND newer.sequence > status_event.sequence
                    )
                )
                SELECT contradiction.contradiction_id, contradiction.fact_a_id,
                       contradiction.fact_b_id, contradiction.scope_key,
                       contradiction.predicate, contradiction.reason_code,
                       status.event_id AS status_event_id
                FROM knowledge_contradictions AS contradiction
                JOIN current_status AS status
                  ON status.contradiction_id = contradiction.contradiction_id
                WHERE contradiction.workspace_id = ? AND status.status = 'open'
                ORDER BY contradiction.detected_at, contradiction.contradiction_id
                """,
                (workspace_id,),
            ).fetchall()
        )

    def current_business_summary(
        self,
        *,
        workspace_id: str,
        as_of: date,
        limit_per_axis: int = 8,
        generated_at: datetime | None = None,
    ) -> BusinessSummary:
        """Build and persist a bounded deterministic summary from active facts only."""

        if limit_per_axis < 1:
            raise ValueError("limit_per_axis must be at least one")
        generated_at = generated_at or datetime.now(UTC)
        as_of_text = as_of.isoformat()
        with self.store.transaction() as connection:
            self._ensure_workspace(connection, workspace_id)
            rows = connection.execute(
                """
                WITH current_status AS (
                    SELECT status_event.*
                    FROM knowledge_fact_status_events AS status_event
                    WHERE NOT EXISTS (
                        SELECT 1 FROM knowledge_fact_status_events AS newer
                        WHERE newer.fact_id = status_event.fact_id
                          AND newer.sequence > status_event.sequence
                    )
                )
                SELECT fact.*, subject.canonical_name AS subject_name,
                       object_entity.canonical_name AS object_entity_name,
                       status.event_id AS status_event_id
                FROM knowledge_facts AS fact
                JOIN current_status AS status ON status.fact_id = fact.fact_id
                JOIN knowledge_entities AS subject
                  ON subject.entity_id = fact.subject_entity_id
                LEFT JOIN knowledge_entities AS object_entity
                  ON object_entity.entity_id = fact.object_entity_id
                WHERE fact.workspace_id = ?
                  AND status.status = 'active'
                  AND COALESCE(fact.valid_from, '0000-01-01') <= ?
                  AND COALESCE(fact.valid_until, '9999-12-31') >= ?
                ORDER BY fact.question_axis, fact.recorded_at DESC, fact.fact_id
                """,
                (workspace_id, as_of_text, as_of_text),
            ).fetchall()
            axes: dict[str, list[dict[str, object]]] = {axis.value: [] for axis in QuestionAxis}
            total_by_axis = {axis.value: 0 for axis in QuestionAxis}
            source_fact_ids: list[str] = []
            source_status_event_ids: list[str] = []
            for row in rows:
                axis = str(row["question_axis"])
                total_by_axis[axis] += 1
                if len(axes[axis]) >= limit_per_axis:
                    continue
                source = _source_from_row(row)
                item: dict[str, object] = {
                    "factId": str(row["fact_id"]),
                    "statusEventId": str(row["status_event_id"]),
                    "subject": {
                        "entityId": str(row["subject_entity_id"]),
                        "name": str(row["subject_name"]),
                    },
                    "predicate": str(row["predicate"]),
                    "factKind": str(row["fact_kind"]),
                    "objectKind": str(row["object_kind"]),
                    "objectText": str(row["object_text"]),
                    "objectEntityName": cast(str | None, row["object_entity_name"]),
                    "value": json.loads(str(row["value_json"])),
                    "basis": str(row["basis"]),
                    "confidence": float(row["confidence"]),
                    "validFrom": cast(str | None, row["valid_from"]),
                    "validUntil": cast(str | None, row["valid_until"]),
                    "source": _source_payload(source),
                }
                axes[axis].append(item)
                source_fact_ids.append(str(row["fact_id"]))
                source_status_event_ids.append(str(row["status_event_id"]))
            contradictions = self._open_contradictions(connection, workspace_id)
            highest_value_finding = connection.execute(
                """
                SELECT finding_id, revision, kind, severity, title, summary,
                       amount_minor, currency, evidence_ids_json
                FROM findings
                WHERE workspace_id = ? AND is_current = 1 AND status = 'open'
                ORDER BY
                    CASE severity
                        WHEN 'critical' THEN 3 WHEN 'attention' THEN 2 ELSE 1
                    END DESC,
                    ABS(COALESCE(amount_minor, 0)) DESC,
                    CASE kind
                        WHEN 'reserve_risk' THEN 3
                        WHEN 'missing_document' THEN 2
                        ELSE 1
                    END DESC,
                    created_at,
                    finding_id
                LIMIT 1
                """,
                (workspace_id,),
            ).fetchone()
            highest_value_question: dict[str, object] | None = None
            source_finding_ids: list[str] = []
            if highest_value_finding is not None:
                finding_id = str(highest_value_finding["finding_id"])
                finding_kind = str(highest_value_finding["kind"])
                title = str(highest_value_finding["title"])
                evidence_ids = cast(
                    list[str], json.loads(str(highest_value_finding["evidence_ids_json"]))
                )
                prompts = {
                    "reserve_risk": (
                        "Which planned outgoing should we adjust first to protect "
                        f"the reserve in {title}?"
                    ),
                    "missing_document": f"Could you add the missing document for {title}?",
                    "duplicate": f"Can you confirm whether {title} is a duplicate?",
                }
                source_finding_ids.append(finding_id)
                question_id = f"question_{finding_id}_r{int(highest_value_finding['revision'])}"
                highest_value_question = {
                    "questionId": question_id,
                    "prompt": prompts[finding_kind],
                    "reason": str(highest_value_finding["summary"]),
                    "findingId": finding_id,
                    "findingRevision": int(highest_value_finding["revision"]),
                    "severity": str(highest_value_finding["severity"]),
                    "amountMinor": cast(int | None, highest_value_finding["amount_minor"]),
                    "currency": cast(str | None, highest_value_finding["currency"]),
                    "evidenceIds": evidence_ids,
                    "sourceIds": [finding_id, *evidence_ids],
                }
            payload: dict[str, object] = {
                "workspaceId": workspace_id,
                "asOf": as_of_text,
                "queryVersion": SUMMARY_QUERY_VERSION,
                "axes": axes,
                "totalByAxis": total_by_axis,
                "truncatedByAxis": {
                    axis: max(0, total - limit_per_axis) for axis, total in total_by_axis.items()
                },
                "openContradictions": [
                    {
                        "contradictionId": str(row["contradiction_id"]),
                        "factAId": str(row["fact_a_id"]),
                        "factBId": str(row["fact_b_id"]),
                        "scopeKey": str(row["scope_key"]),
                        "predicate": str(row["predicate"]),
                        "reasonCode": str(row["reason_code"]),
                        "statusEventId": str(row["status_event_id"]),
                    }
                    for row in contradictions
                ],
                "highestValueQuestion": highest_value_question,
            }
            content_hash = _payload_hash(payload)
            summary_id = _content_id(
                "ksummary",
                {
                    "workspaceId": workspace_id,
                    "asOf": as_of_text,
                    "limitPerAxis": limit_per_axis,
                    "queryVersion": SUMMARY_QUERY_VERSION,
                },
            )
            existing = connection.execute(
                """
                SELECT revision FROM knowledge_business_summary_revisions
                WHERE workspace_id = ? AND as_of = ? AND query_version = ?
                  AND content_hash = ?
                """,
                (workspace_id, as_of_text, SUMMARY_QUERY_VERSION, content_hash),
            ).fetchone()
            if existing is not None:
                revision = int(existing["revision"])
            else:
                latest = connection.execute(
                    """
                    SELECT COALESCE(MAX(revision), 0) AS revision
                    FROM knowledge_business_summary_revisions WHERE summary_id = ?
                    """,
                    (summary_id,),
                ).fetchone()
                revision = int(latest["revision"]) + 1
                connection.execute(
                    """
                    INSERT INTO knowledge_business_summary_revisions(
                        summary_id, revision, workspace_id, as_of, limit_per_axis,
                        query_version, payload_json, source_fact_ids_json,
                        source_status_event_ids_json, source_finding_ids_json,
                        content_hash, generated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary_id,
                        revision,
                        workspace_id,
                        as_of_text,
                        limit_per_axis,
                        SUMMARY_QUERY_VERSION,
                        canonical_json(payload),
                        canonical_json(source_fact_ids),
                        canonical_json(source_status_event_ids),
                        canonical_json(source_finding_ids),
                        content_hash,
                        _aware_timestamp(generated_at),
                    ),
                )
            return BusinessSummary(
                summary_id=summary_id,
                revision=revision,
                workspace_id=workspace_id,
                as_of=as_of,
                content_hash=content_hash,
                payload=payload,
            )

    def retrieve(
        self,
        *,
        workspace_id: str,
        query: str,
        task_scope: str,
        as_of: date,
        limit: int = 8,
        include_candidates: bool = False,
        retrieved_at: datetime | None = None,
        run_id: str | None = None,
        thread_id: str | None = None,
        max_characters: int = 6000,
    ) -> KnowledgeRetrieval:
        """Retrieve task-scoped context and persist a source/status receipt."""

        task_scope = _validate_task_scope(task_scope)
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        if not 1 <= max_characters <= 100_000:
            raise ValueError("max_characters must be between 1 and 100000")
        retrieved_at = retrieved_at or datetime.now(UTC)
        expression = _fts_expression(query)
        as_of_text = as_of.isoformat()
        with self.store.transaction() as connection:
            self._ensure_workspace(connection, workspace_id)
            rows: list[sqlite3.Row] = []
            if expression is not None:
                rows = list(
                    connection.execute(
                        """
                        WITH current_status AS (
                            SELECT status_event.*
                            FROM knowledge_fact_status_events AS status_event
                            WHERE NOT EXISTS (
                                SELECT 1 FROM knowledge_fact_status_events AS newer
                                WHERE newer.fact_id = status_event.fact_id
                                  AND newer.sequence > status_event.sequence
                            )
                        ), ranked AS (
                            SELECT record_type, record_id, task_scope, title,
                                   snippet(knowledge_fts, 5, '[', ']', '...', 18) AS excerpt,
                                   bm25(knowledge_fts, 0.0, 0.0, 0.0, 0.0, 2.0, 1.0, 0.5)
                                       AS score
                            FROM knowledge_fts
                            WHERE knowledge_fts MATCH ?
                              AND workspace_id = ?
                              AND (
                                  ? = 'general' OR task_scope = ? OR task_scope = 'general'
                              )
                        )
                        SELECT ranked.*,
                               fact.source_kind AS fact_source_kind,
                               fact.source_ref AS fact_source_ref,
                               fact.source_turn_id AS fact_source_turn_id,
                               fact.source_document_id AS fact_source_document_id,
                               fact.evidence_id AS fact_evidence_id,
                               document.source_kind AS document_source_kind,
                               document.source_ref AS document_source_ref,
                               document.source_turn_id AS document_source_turn_id,
                               NULL AS document_source_document_id,
                               document.evidence_id AS document_evidence_id,
                               entity.source_kind AS entity_source_kind,
                               entity.source_ref AS entity_source_ref,
                               entity.source_turn_id AS entity_source_turn_id,
                               entity.source_document_id AS entity_source_document_id,
                               entity.evidence_id AS entity_evidence_id,
                               owner.turn_id AS owner_turn_id,
                               status.status AS fact_status,
                               status.event_id AS status_event_id
                        FROM ranked
                        LEFT JOIN knowledge_facts AS fact
                          ON ranked.record_type = 'fact' AND fact.fact_id = ranked.record_id
                        LEFT JOIN current_status AS status ON status.fact_id = fact.fact_id
                        LEFT JOIN knowledge_documents AS document
                          ON ranked.record_type = 'document'
                         AND document.document_id = ranked.record_id
                        LEFT JOIN knowledge_entities AS entity
                          ON ranked.record_type = 'entity' AND entity.entity_id = ranked.record_id
                        LEFT JOIN knowledge_owner_statements AS owner
                          ON ranked.record_type = 'owner_statement'
                         AND owner.statement_id = ranked.record_id
                        WHERE ranked.record_type <> 'fact'
                           OR (
                                (status.status = 'active'
                                  OR (? = 1 AND status.status = 'candidate'))
                                AND COALESCE(fact.valid_from, '0000-01-01') <= ?
                                AND COALESCE(fact.valid_until, '9999-12-31') >= ?
                           )
                        ORDER BY ranked.score, ranked.record_type, ranked.record_id
                        LIMIT ?
                        """,
                        (
                            expression,
                            workspace_id,
                            task_scope,
                            task_scope,
                            int(include_candidates),
                            as_of_text,
                            as_of_text,
                            limit,
                        ),
                    ).fetchall()
                )
            ranked_hits: list[KnowledgeHit] = []
            for row in rows:
                record_type = str(row["record_type"])
                if record_type == "fact":
                    source = KnowledgeSource(
                        kind=SourceKind(str(row["fact_source_kind"])),
                        ref=str(row["fact_source_ref"]),
                        turn_id=cast(str | None, row["fact_source_turn_id"]),
                        document_id=cast(str | None, row["fact_source_document_id"]),
                        evidence_id=cast(str | None, row["fact_evidence_id"]),
                    )
                    status = FactStatus(str(row["fact_status"]))
                    status_event_id = str(row["status_event_id"])
                elif record_type == "document":
                    source = KnowledgeSource(
                        kind=SourceKind(str(row["document_source_kind"])),
                        ref=str(row["document_source_ref"]),
                        turn_id=cast(str | None, row["document_source_turn_id"]),
                        document_id=cast(str | None, row["document_source_document_id"]),
                        evidence_id=cast(str | None, row["document_evidence_id"]),
                    )
                    status = None
                    status_event_id = None
                elif record_type == "entity":
                    source = KnowledgeSource(
                        kind=SourceKind(str(row["entity_source_kind"])),
                        ref=str(row["entity_source_ref"]),
                        turn_id=cast(str | None, row["entity_source_turn_id"]),
                        document_id=cast(str | None, row["entity_source_document_id"]),
                        evidence_id=cast(str | None, row["entity_evidence_id"]),
                    )
                    status = None
                    status_event_id = None
                else:
                    source = KnowledgeSource(
                        kind=SourceKind.OWNER_TURN,
                        ref=str(row["record_id"]),
                        turn_id=cast(str | None, row["owner_turn_id"]),
                    )
                    status = None
                    status_event_id = None
                ranked_hits.append(
                    KnowledgeHit(
                        record_type=record_type,
                        record_id=str(row["record_id"]),
                        task_scope=str(row["task_scope"]),
                        title=str(row["title"]),
                        excerpt=str(row["excerpt"]),
                        score=float(row["score"]),
                        source=source,
                        status=status,
                        status_event_id=status_event_id,
                    )
                )
            hits: list[KnowledgeHit] = []
            dropped_hits: list[KnowledgeHit] = []
            packet_parts: list[str] = []
            packet_characters = 0
            for hit in ranked_hits:
                packet_part = f"[{hit.record_type}:{hit.record_id}] {hit.title}\n{hit.excerpt}"
                separator_characters = 2 if packet_parts else 0
                proposed_characters = packet_characters + separator_characters + len(packet_part)
                if proposed_characters > max_characters:
                    dropped_hits.append(hit)
                    continue
                hits.append(hit)
                packet_parts.append(packet_part)
                packet_characters = proposed_characters
            packet_text = "\n\n".join(packet_parts)
            packet_hash = _sha256_text(packet_text)
            status_event_ids = [
                hit.status_event_id for hit in hits if hit.status_event_id is not None
            ]
            receipt_payload: dict[str, object] = {
                "workspaceId": workspace_id,
                "runId": run_id,
                "threadId": thread_id,
                "taskScope": task_scope,
                "queryHash": _sha256_text(query),
                "asOf": as_of_text,
                "includeCandidates": include_candidates,
                "maxCharacters": max_characters,
                "packetCharacters": packet_characters,
                "packetHash": packet_hash,
                "queryVersion": RETRIEVAL_QUERY_VERSION,
                "rankedResults": [
                    {
                        "recordType": hit.record_type,
                        "recordId": hit.record_id,
                        "statusEventId": hit.status_event_id,
                    }
                    for hit in ranked_hits
                ],
                "selectedIds": [hit.record_id for hit in hits],
                "droppedIds": [hit.record_id for hit in dropped_hits],
            }
            content_hash = _payload_hash(receipt_payload)
            receipt_id = _content_id("kret", receipt_payload)
            connection.execute(
                """
                INSERT OR IGNORE INTO knowledge_context_retrieval_receipts(
                    receipt_id, workspace_id, run_id, thread_id, task_scope, query_hash, as_of,
                    include_candidates, result_ids_json, selected_ids_json, dropped_ids_json,
                    max_characters, packet_characters, packet_hash,
                    source_status_event_ids_json, query_version, content_hash, retrieved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    workspace_id,
                    run_id,
                    thread_id,
                    task_scope,
                    _sha256_text(query),
                    as_of_text,
                    int(include_candidates),
                    canonical_json(
                        [
                            {"recordType": hit.record_type, "recordId": hit.record_id}
                            for hit in ranked_hits
                        ]
                    ),
                    canonical_json([hit.record_id for hit in hits]),
                    canonical_json([hit.record_id for hit in dropped_hits]),
                    max_characters,
                    packet_characters,
                    packet_hash,
                    canonical_json(status_event_ids),
                    RETRIEVAL_QUERY_VERSION,
                    content_hash,
                    _aware_timestamp(retrieved_at),
                ),
            )
            return KnowledgeRetrieval(
                receipt_id=receipt_id,
                workspace_id=workspace_id,
                task_scope=task_scope,
                as_of=as_of,
                hits=tuple(hits),
            )

    def resolve_contradiction(
        self,
        *,
        contradiction_id: str,
        target_status: ContradictionStatus,
        source: KnowledgeSource,
        reason: str,
        occurred_at: datetime,
    ) -> str:
        if target_status is ContradictionStatus.OPEN:
            raise ValueError("an existing contradiction can only be resolved or dismissed")
        with self.store.transaction() as connection:
            contradiction = connection.execute(
                "SELECT workspace_id FROM knowledge_contradictions WHERE contradiction_id = ?",
                (contradiction_id,),
            ).fetchone()
            if contradiction is None:
                raise KeyError(f"unknown knowledge contradiction: {contradiction_id}")
            workspace_id = str(contradiction["workspace_id"])
            self._validate_source(connection, workspace_id, source)
            current = connection.execute(
                """
                SELECT * FROM knowledge_contradiction_status_events AS status_event
                WHERE status_event.contradiction_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM knowledge_contradiction_status_events AS newer
                      WHERE newer.contradiction_id = status_event.contradiction_id
                        AND newer.sequence > status_event.sequence
                  )
                """,
                (contradiction_id,),
            ).fetchone()
            if current is None:
                raise ValueError("knowledge contradiction has no status")
            if str(current["status"]) == target_status.value:
                return str(current["event_id"])
            if str(current["status"]) != ContradictionStatus.OPEN.value:
                raise ValueError("resolved knowledge contradictions cannot transition again")
            sequence = int(current["sequence"]) + 1
            event_id = _content_id(
                "kcse",
                {
                    "contradictionId": contradiction_id,
                    "sequence": sequence,
                    "status": target_status.value,
                    "reason": reason,
                },
            )
            payload = {
                "eventId": event_id,
                "contradictionId": contradiction_id,
                "workspaceId": workspace_id,
                "sequence": sequence,
                "status": target_status.value,
                "reason": reason,
                "source": _source_payload(source),
                "occurredAt": _aware_timestamp(occurred_at),
            }
            connection.execute(
                """
                INSERT INTO knowledge_contradiction_status_events(
                    event_id, contradiction_id, workspace_id, sequence, status, reason,
                    source_kind, source_ref, source_turn_id, occurred_at, record_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    contradiction_id,
                    workspace_id,
                    sequence,
                    target_status.value,
                    reason,
                    source.kind.value,
                    source.ref,
                    source.turn_id,
                    _aware_timestamp(occurred_at),
                    _payload_hash(payload),
                ),
            )
            return event_id


__all__ = [
    "BusinessSummary",
    "ContradictionStatus",
    "DocumentKind",
    "EntityType",
    "FactBasis",
    "FactKind",
    "FactStatus",
    "KnowledgeDocument",
    "KnowledgeEntity",
    "KnowledgeFact",
    "KnowledgeHit",
    "KnowledgeRetrieval",
    "KnowledgeSource",
    "ObjectKind",
    "QuestionAxis",
    "SQLiteKnowledgeStore",
    "SourceKind",
]
