"""Runtime bridge for Folio's durable, model-independent business understanding."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, date, datetime

from finance_agent.agent.catalogue import IntentClass
from finance_agent.agent.fallback import classify_intent
from finance_agent.storage.knowledge import (
    BusinessSummary,
    EntityType,
    FactBasis,
    FactKind,
    KnowledgeEntity,
    KnowledgeFact,
    KnowledgeSource,
    ObjectKind,
    QuestionAxis,
    SourceKind,
    SQLiteKnowledgeStore,
)
from finance_agent.storage.knowledge_compiler import CommittedFinanceKnowledgeCompiler
from finance_agent.storage.store import SQLiteStore, canonical_json

_INTENT_SCOPES: dict[IntentClass, str] = {
    IntentClass.READ_SUMMARY: "general",
    IntentClass.READ_TRANSACTIONS: "reconciliation",
    IntentClass.SCENARIO: "cash_flow",
    IntentClass.CORRECTION: "categorisation",
    IntentClass.UNDO: "categorisation",
    IntentClass.OWNER_PACK: "documents",
    IntentClass.STOP_SYNTHESIS: "general",
    IntentClass.UNKNOWN: "general",
}

_PERSON_NAME = r"[A-Za-z][A-Za-z'’\-]+(?:\s+[A-Za-z][A-Za-z'’\-]+){1,3}"
_ROLE_PATTERNS = (
    re.compile(
        rf"\b(?P<name>{_PERSON_NAME})\s+is\s+(?:now\s+)?(?:our|my)\s+"
        r"(?P<role>accountant|bookkeeper|supplier|customer|client)\b",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(?:our|my)\s+(?P<role>accountant|bookkeeper|supplier|customer|client)\s+"
        rf"is\s+(?P<name>{_PERSON_NAME})(?=[.;,\n]|$)",
        re.IGNORECASE,
    ),
)
_BUSINESS_LOCATION_PATTERNS = (
    re.compile(
        r"\b[A-Za-z][A-Za-z0-9&'’\- ]{1,80}\s+is\s+(?:now\s+)?based\s+in\s+"
        r"(?P<location>[A-Za-z][A-Za-z'’\- ]{1,60})(?=[.;,\n]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bwe(?:'re| are)\s+(?:now\s+)?based\s+in\s+"
        r"(?P<location>[A-Za-z][A-Za-z'’\- ]{1,60})(?=[.;,\n]|$)",
        re.IGNORECASE,
    ),
)
_PURPOSE_PATTERN = re.compile(
    r"\b(?:the\s+)?(?P<merchant>[A-Za-z0-9&'’\- ]{2,60}?)\s+"
    r"(?:purchase|expense|transaction)\s+(?:was|is)\s+(?:materials\s+)?"
    r"(?:for|used\s+for)\s+(?P<purpose>[^.;\n]{3,240})(?=[.;\n]|$)",
    re.IGNORECASE,
)
_FUNDS_LOCATION_PATTERN = re.compile(
    r"\b(?:we\s+)?keep\s+(?P<funds>GST|tax(?:\s+money)?|reserve(?:\s+funds)?)\s+"
    r"in\s+(?:the\s+)?(?P<location>[^.;\n]{3,100})(?=[.;\n]|$)",
    re.IGNORECASE,
)
_CORRECTION_PATTERN = re.compile(
    r"\b(correction|actually|instead|no longer|now|previous|correct that)\b",
    re.IGNORECASE,
)
_CROSS_BUSINESS_QUERY_PATTERN = re.compile(
    r"\b(who|what do (?:you|we) know|whole business|across the business|and why)\b",
    re.IGNORECASE,
)


def _hash(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _stable_id(prefix: str, value: object) -> str:
    return f"{prefix}_{_hash(value)[:24]}"


def _clean_phrase(value: str) -> str:
    return " ".join(value.strip(" \t\r\n,.;:").split())


class WorkingUnderstandingRuntime:
    """Compile, retrieve and inspect derived context without owning finance truth."""

    def __init__(self, store: SQLiteStore) -> None:
        self.store = store
        self.knowledge = SQLiteKnowledgeStore(store)
        self.compiler = CommittedFinanceKnowledgeCompiler(store, self.knowledge)

    def _workspace_clock(self, workspace_id: str) -> tuple[date, str]:
        row = self.store.fetch_one(
            "SELECT data_through, thread_id FROM workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        if row is None:
            raise KeyError(f"unknown workspace: {workspace_id}")
        return date.fromisoformat(str(row["data_through"])[:10]), str(row["thread_id"])

    def ensure_current(
        self,
        *,
        workspace_id: str,
        recorded_at: datetime | None = None,
    ) -> BusinessSummary:
        as_of, _ = self._workspace_clock(workspace_id)
        return self.compiler.bootstrap_committed_finance(
            workspace_id=workspace_id,
            as_of=as_of,
            recorded_at=recorded_at or datetime.now(UTC),
        )

    def _business_entity_id(self, workspace_id: str) -> str:
        row = self.store.fetch_one(
            """
            SELECT subject_entity_id FROM knowledge_facts
            WHERE workspace_id = ? AND predicate = 'business.name'
            ORDER BY recorded_at, fact_id LIMIT 1
            """,
            (workspace_id,),
        )
        if row is None:
            raise RuntimeError("working understanding has no business identity")
        return str(row["subject_entity_id"])

    def _entity(
        self,
        *,
        workspace_id: str,
        entity_type: EntityType,
        name: str,
        source: KnowledgeSource,
        occurred_at: datetime,
    ) -> str:
        existing = self.store.fetch_one(
            """
            SELECT entity_id FROM knowledge_entities
            WHERE workspace_id = ? AND entity_type = ? AND LOWER(canonical_name) = LOWER(?)
            ORDER BY recorded_at, entity_id LIMIT 1
            """,
            (workspace_id, entity_type.value, name),
        )
        if existing is not None:
            return str(existing["entity_id"])
        entity_id = _stable_id(
            "kentity",
            {"workspaceId": workspace_id, "entityType": entity_type.value, "name": name.casefold()},
        )
        self.knowledge.record_entity(
            KnowledgeEntity(
                entity_id=entity_id,
                workspace_id=workspace_id,
                entity_type=entity_type,
                canonical_name=name,
                source=source,
                recorded_at=occurred_at,
                task_scope="business_profile",
            )
        )
        return entity_id

    def _record_explicit_fact(
        self,
        *,
        workspace_id: str,
        subject_entity_id: str,
        axis: QuestionAxis,
        predicate: str,
        scope_key: str,
        object_text: str,
        value: object,
        source: KnowledgeSource,
        occurred_at: datetime,
        task_scope: str,
        correction: bool,
        object_entity_id: str | None = None,
    ) -> None:
        current = self.store.fetch_one(
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
            SELECT fact.fact_id, fact.object_text, fact.value_json, fact.object_entity_id
            FROM knowledge_facts AS fact
            JOIN current_status AS status ON status.fact_id = fact.fact_id
            WHERE fact.workspace_id = ? AND fact.subject_entity_id = ?
              AND fact.predicate = ? AND fact.scope_key = ? AND status.status = 'active'
            ORDER BY fact.recorded_at DESC, fact.fact_id DESC LIMIT 1
            """,
            (workspace_id, subject_entity_id, predicate, scope_key),
        )
        if current is not None and (
            str(current["object_text"]) == object_text
            and str(current["value_json"]) == canonical_json(value)
            and current["object_entity_id"] == object_entity_id
        ):
            return
        supersedes = str(current["fact_id"]) if current is not None and correction else None
        fact_id = _stable_id(
            "kfact",
            {
                "workspaceId": workspace_id,
                "predicate": predicate,
                "scopeKey": scope_key,
                "value": value,
                "sourceRef": source.ref,
            },
        )
        self.knowledge.record_fact(
            KnowledgeFact(
                fact_id=fact_id,
                workspace_id=workspace_id,
                question_axis=axis,
                fact_kind=(FactKind.RELATIONSHIP if object_entity_id else FactKind.ATTRIBUTE),
                subject_entity_id=subject_entity_id,
                predicate=predicate,
                scope_key=scope_key,
                object_kind=(ObjectKind.ENTITY if object_entity_id else ObjectKind.TEXT),
                object_text=object_text,
                object_entity_id=object_entity_id,
                value=value,
                source=source,
                basis=FactBasis.EXPLICIT,
                confidence=1.0,
                recorded_at=occurred_at,
                task_scope=task_scope,
                valid_from=occurred_at.date(),
                supersedes_fact_id=supersedes,
            ),
            reason="Recorded from the owner's explicit explanation.",
        )

    def ingest_owner_turn(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        turn_id: str,
        content: str,
        occurred_at: datetime,
    ) -> None:
        """Retain the full statement, then conservatively structure explicit facts."""

        self.ensure_current(workspace_id=workspace_id, recorded_at=occurred_at)
        statement_id = self.knowledge.record_owner_turn(
            workspace_id=workspace_id,
            thread_id=thread_id,
            turn_id=turn_id,
            content=content,
            occurred_at=occurred_at,
            task_scope="general",
        )
        source = KnowledgeSource(SourceKind.OWNER_TURN, statement_id, turn_id=turn_id)
        business_entity_id = self._business_entity_id(workspace_id)
        correction = _CORRECTION_PATTERN.search(content) is not None

        for pattern in _ROLE_PATTERNS:
            for match in pattern.finditer(content):
                name = _clean_phrase(match.group("name"))
                role = match.group("role").casefold()
                entity_type = (
                    EntityType.SUPPLIER
                    if role == "supplier"
                    else EntityType.CUSTOMER
                    if role in {"customer", "client"}
                    else EntityType.PERSON
                )
                entity_id = self._entity(
                    workspace_id=workspace_id,
                    entity_type=entity_type,
                    name=name,
                    source=source,
                    occurred_at=occurred_at,
                )
                canonical_role = "customer" if role == "client" else role
                self._record_explicit_fact(
                    workspace_id=workspace_id,
                    subject_entity_id=business_entity_id,
                    axis=QuestionAxis.WHO,
                    predicate=f"business.{canonical_role}",
                    scope_key=f"business:{canonical_role}",
                    object_text=name,
                    object_entity_id=entity_id,
                    value={"entityId": entity_id, "role": canonical_role},
                    source=source,
                    occurred_at=occurred_at,
                    task_scope="business_profile",
                    correction=correction,
                )

        for pattern in _BUSINESS_LOCATION_PATTERNS:
            for match in pattern.finditer(content):
                location = _clean_phrase(match.group("location"))
                self._record_explicit_fact(
                    workspace_id=workspace_id,
                    subject_entity_id=business_entity_id,
                    axis=QuestionAxis.WHERE,
                    predicate="business.base_city",
                    scope_key="business:base_city",
                    object_text=location,
                    value=location,
                    source=source,
                    occurred_at=occurred_at,
                    task_scope="business_profile",
                    correction=correction,
                )

        for match in _PURPOSE_PATTERN.finditer(content):
            merchant = _clean_phrase(match.group("merchant"))
            purpose = _clean_phrase(match.group("purpose"))
            self._record_explicit_fact(
                workspace_id=workspace_id,
                subject_entity_id=business_entity_id,
                axis=QuestionAxis.WHY,
                predicate="expense.business_purpose",
                scope_key=f"merchant:{merchant.casefold()}",
                object_text=f"{merchant}: {purpose}",
                value={"merchant": merchant, "purpose": purpose},
                source=source,
                occurred_at=occurred_at,
                task_scope="categorisation",
                correction=correction,
            )

        for match in _FUNDS_LOCATION_PATTERN.finditer(content):
            funds = _clean_phrase(match.group("funds")).casefold()
            location = _clean_phrase(match.group("location"))
            self._record_explicit_fact(
                workspace_id=workspace_id,
                subject_entity_id=business_entity_id,
                axis=QuestionAxis.WHERE,
                predicate="funds.kept_in",
                scope_key=f"funds:{funds}",
                object_text=f"{funds.upper() if funds == 'gst' else funds}: {location}",
                value={"funds": funds, "location": location},
                source=source,
                occurred_at=occurred_at,
                task_scope="cash_flow",
                correction=correction,
            )

    def record_committed_owner_turn(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        turn_id: str,
    ) -> BusinessSummary:
        row = self.store.fetch_one(
            """
            SELECT content, occurred_at FROM conversation_turns
            WHERE workspace_id = ? AND thread_id = ? AND turn_id = ? AND role = 'owner'
            """,
            (workspace_id, thread_id, turn_id),
        )
        if row is None:
            raise KeyError(f"committed owner turn not found: {turn_id}")
        occurred_at = datetime.fromisoformat(str(row["occurred_at"]).replace("Z", "+00:00"))
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=UTC)
        self.ingest_owner_turn(
            workspace_id=workspace_id,
            thread_id=thread_id,
            turn_id=turn_id,
            content=str(row["content"]),
            occurred_at=occurred_at,
        )
        return self.ensure_current(workspace_id=workspace_id, recorded_at=occurred_at)

    @staticmethod
    def _summary_entries(summary: BusinessSummary) -> list[dict[str, object]]:
        axes = summary.payload.get("axes", {})
        if not isinstance(axes, Mapping):
            return []
        values: list[dict[str, object]] = []
        for axis in ("who", "what", "where", "when", "why"):
            entries = axes.get(axis, [])
            if not isinstance(entries, list):
                continue
            for entry in entries[:2]:
                if not isinstance(entry, Mapping):
                    continue
                source = entry.get("source", {})
                values.append(
                    {
                        "recordType": "fact",
                        "recordId": entry.get("factId"),
                        "axis": axis,
                        "title": entry.get("predicate"),
                        "excerpt": str(entry.get("objectText", ""))[:320],
                        "basis": entry.get("basis"),
                        "source": dict(source) if isinstance(source, Mapping) else {},
                    }
                )
        return values

    def context_for(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        run_id: str,
        query: str,
        max_characters: int,
    ) -> Mapping[str, object]:
        if max_characters < 512:
            raise ValueError("working-understanding budget must be at least 512 characters")
        as_of, expected_thread_id = self._workspace_clock(workspace_id)
        if thread_id != expected_thread_id:
            raise KeyError(f"unknown workspace thread: {thread_id}")
        summary = self.ensure_current(workspace_id=workspace_id)
        task_scope = (
            "general"
            if _CROSS_BUSINESS_QUERY_PATTERN.search(query)
            else _INTENT_SCOPES[classify_intent(query)]
        )
        retrieval = self.knowledge.retrieve(
            workspace_id=workspace_id,
            query=query,
            task_scope=task_scope,
            as_of=as_of,
            limit=10,
            include_candidates=False,
            run_id=run_id,
            thread_id=thread_id,
            max_characters=max_characters,
        )
        retrieved_entries: list[dict[str, object]] = [
            {
                "recordType": hit.record_type,
                "recordId": hit.record_id,
                "title": hit.title,
                "excerpt": hit.excerpt[:320],
                "status": hit.status.value if hit.status else None,
                "statusEventId": hit.status_event_id,
                "source": {
                    "kind": hit.source.kind.value,
                    "ref": hit.source.ref,
                    "turnId": hit.source.turn_id,
                    "documentId": hit.source.document_id,
                    "evidenceId": hit.source.evidence_id,
                },
            }
            for hit in retrieval.hits
        ]
        entries = retrieved_entries or self._summary_entries(summary)
        selected = list(entries)
        dropped_ids: list[str] = []
        owner_statement_count = self.store.fetch_one(
            "SELECT COUNT(*) AS count FROM knowledge_owner_statements WHERE workspace_id = ?",
            (workspace_id,),
        )
        packet: dict[str, object] = {
            "summaryRevision": summary.revision,
            "summaryContentHash": summary.content_hash,
            "asOf": as_of.isoformat(),
            "taskScope": task_scope,
            "retrievalReceiptId": retrieval.receipt_id,
            "entries": selected,
            "totalByAxis": summary.payload.get("totalByAxis") or {},
            "ownerStatementCount": int(owner_statement_count["count"])
            if owner_statement_count is not None
            else 0,
            "highestValueQuestion": summary.payload.get("highestValueQuestion"),
            "openContradictionCount": len(
                summary.payload.get("openContradictions", [])  # type: ignore[arg-type]
            ),
            "droppedRecordIds": dropped_ids,
        }
        encoded = canonical_json(packet)
        while len(encoded) > max_characters and selected:
            removed = selected.pop()
            dropped_ids.insert(0, str(removed.get("recordId", "unknown")))
            encoded = canonical_json(packet)
        if len(encoded) > max_characters:
            packet["highestValueQuestion"] = None
            encoded = canonical_json(packet)
        if len(encoded) > max_characters:
            raise ValueError("working-understanding metadata exceeds its bounded budget")
        packet["packetCharacters"] = len(encoded)
        packet["packetHash"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        return packet

    def diagnostics(
        self,
        *,
        workspace_id: str,
        run_id: str | None = None,
    ) -> Mapping[str, object]:
        summary = self.ensure_current(workspace_id=workspace_id)
        counts: dict[str, int] = {}
        for table in (
            "knowledge_owner_statements",
            "knowledge_documents",
            "knowledge_entities",
            "knowledge_facts",
            "knowledge_contradictions",
        ):
            row = self.store.fetch_one(
                f"SELECT COUNT(*) AS count FROM {table} WHERE workspace_id = ?",
                (workspace_id,),
            )
            counts[table] = int(row["count"]) if row is not None else 0
        retrievals = self.store.fetch_all(
            """
            SELECT receipt_id, run_id, thread_id, task_scope, as_of, result_ids_json,
                   selected_ids_json, dropped_ids_json, max_characters,
                   packet_characters, packet_hash, query_version, content_hash, retrieved_at
            FROM knowledge_context_retrieval_receipts
            WHERE workspace_id = ? AND (? IS NULL OR run_id = ?)
            ORDER BY retrieved_at DESC, receipt_id DESC LIMIT 20
            """,
            (workspace_id, run_id, run_id),
        )
        facts = self.store.fetch_all(
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
            SELECT fact.fact_id, fact.question_axis, fact.predicate, fact.scope_key,
                   fact.object_text, fact.value_json, fact.basis, fact.confidence,
                   fact.source_kind, fact.source_ref, fact.source_turn_id,
                   fact.source_document_id, fact.evidence_id, fact.valid_from,
                   fact.valid_until, fact.supersedes_fact_id, fact.recorded_at,
                   status.status, status.event_id AS status_event_id,
                   replacement.fact_id AS superseded_by_fact_id
            FROM knowledge_facts AS fact
            JOIN current_status AS status ON status.fact_id = fact.fact_id
            LEFT JOIN knowledge_facts AS replacement
              ON replacement.supersedes_fact_id = fact.fact_id
            WHERE fact.workspace_id = ?
            ORDER BY fact.recorded_at, fact.fact_id
            """,
            (workspace_id,),
        )
        return {
            "diagnosticVersion": "WorkingUnderstandingDiagnostics@1",
            "workspaceId": workspace_id,
            "summary": {
                "summaryId": summary.summary_id,
                "revision": summary.revision,
                "asOf": summary.as_of.isoformat(),
                "contentHash": summary.content_hash,
                "totalByAxis": summary.payload.get("totalByAxis"),
                "highestValueQuestion": summary.payload.get("highestValueQuestion"),
                "openContradictions": summary.payload.get("openContradictions"),
                "axes": summary.payload.get("axes"),
            },
            "counts": counts,
            "facts": [
                {
                    "factId": str(row["fact_id"]),
                    "axis": str(row["question_axis"]),
                    "predicate": str(row["predicate"]),
                    "scopeKey": str(row["scope_key"]),
                    "objectText": str(row["object_text"]),
                    "value": json.loads(str(row["value_json"])),
                    "basis": str(row["basis"]),
                    "confidence": float(row["confidence"]),
                    "status": str(row["status"]),
                    "statusEventId": str(row["status_event_id"]),
                    "source": {
                        "kind": str(row["source_kind"]),
                        "ref": str(row["source_ref"]),
                        "turnId": row["source_turn_id"],
                        "documentId": row["source_document_id"],
                        "evidenceId": row["evidence_id"],
                    },
                    "validFrom": row["valid_from"],
                    "validUntil": row["valid_until"],
                    "recordedAt": str(row["recorded_at"]),
                    "supersedesFactId": row["supersedes_fact_id"],
                    "supersededByFactId": row["superseded_by_fact_id"],
                }
                for row in facts
            ],
            "retrievalReceipts": [
                {
                    "receiptId": str(row["receipt_id"]),
                    "runId": row["run_id"],
                    "threadId": row["thread_id"],
                    "taskScope": str(row["task_scope"]),
                    "asOf": str(row["as_of"]),
                    "resultIds": json.loads(str(row["result_ids_json"])),
                    "selectedIds": json.loads(str(row["selected_ids_json"])),
                    "droppedIds": json.loads(str(row["dropped_ids_json"])),
                    "maxCharacters": int(row["max_characters"]),
                    "packetCharacters": int(row["packet_characters"]),
                    "packetHash": str(row["packet_hash"]),
                    "queryVersion": str(row["query_version"]),
                    "contentHash": str(row["content_hash"]),
                    "retrievedAt": str(row["retrieved_at"]),
                }
                for row in retrievals
            ],
            "privacy": {
                "rawTurnsIncluded": False,
                "rawDocumentsIncluded": False,
                "promptsIncluded": False,
                "credentialsIncluded": False,
            },
            "diagnosticHash": _hash(
                {
                    "summary": summary.content_hash,
                    "counts": counts,
                    "retrievalReceiptIds": [str(row["receipt_id"]) for row in retrievals],
                }
            ),
        }


__all__ = ["WorkingUnderstandingRuntime"]
