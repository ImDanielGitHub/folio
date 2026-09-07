from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def insert_method_before(path: str, class_name: str, before_name: str, method: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    before = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


MIGRATION = '''    Migration(
        version={version},
        name="audit_trail_exports",
        sql="""
        CREATE TABLE audit_trail_export_revisions (
            export_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            start_at TEXT,
            end_at TEXT,
            filters_json TEXT NOT NULL,
            event_count INTEGER NOT NULL CHECK (event_count >= 0),
            content BLOB NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            created_at TEXT NOT NULL,
            PRIMARY KEY (export_id, revision)
        );

        CREATE INDEX audit_export_workspace_time
            ON audit_trail_export_revisions(workspace_id, created_at DESC, revision DESC);
        """,
    ),
'''

MODULE = '''"""Unified, content-minimised audit timeline and JSONL export."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

from finance_agent.storage import SQLiteStore, canonical_json

ALLOWED_KINDS = frozenset(
    {
        "finance_event",
        "job_run",
        "source_ingest",
        "model_run",
        "egress",
        "invoice_settlement",
        "budget_policy",
        "reserve_policy",
        "cash_commitment",
        "invoice_lifecycle",
        "notification",
        "backup_restore",
    }
)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _parse_json(value: object) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hash(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    workspace_id: str
    kind: str
    action: str
    status: str
    occurred_at: str
    actor: str | None
    correlation_id: str | None
    subject_type: str | None
    subject_id: str | None
    evidence_ids: tuple[str, ...]
    metadata: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "auditVersion": "folio.audit-event@1",
            "eventId": self.event_id,
            "workspaceId": self.workspace_id,
            "kind": self.kind,
            "action": self.action,
            "status": self.status,
            "occurredAt": self.occurred_at,
            "actor": self.actor,
            "correlationId": self.correlation_id,
            "subjectType": self.subject_type,
            "subjectId": self.subject_id,
            "evidenceIds": list(self.evidence_ids),
            "metadata": dict(self.metadata),
            "rawContentIncluded": False,
        }


class UnifiedAuditTrailService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def _table_exists(self, name: str) -> bool:
        row = self.store.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        )
        return row is not None

    def _finance_events(self, workspace_id: str) -> Iterable[AuditEvent]:
        rows = self.store.fetch_all(
            """
            SELECT event_id, event_type, actor, occurred_at, source_turn_id,
                   scope_json, evidence_ids_json, correlation_id,
                   undone_by_event_id, redone_by_event_id
            FROM finance_events WHERE workspace_id = ?
            ORDER BY occurred_at, event_id
            """,
            (workspace_id,),
        )
        for row in rows:
            scope = _parse_json(row["scope_json"])
            subject_ids = scope.get("transactionIds") or scope.get("ruleIds") or []
            yield AuditEvent(
                event_id=str(row["event_id"]),
                workspace_id=workspace_id,
                kind="finance_event",
                action=str(row["event_type"]),
                status=(
                    "undone"
                    if row["undone_by_event_id"]
                    else "redone"
                    if row["redone_by_event_id"]
                    else "committed"
                ),
                occurred_at=str(row["occurred_at"]),
                actor=str(row["actor"]),
                correlation_id=str(row["correlation_id"]),
                subject_type="finance_scope",
                subject_id=(str(subject_ids[0]) if isinstance(subject_ids, list) and subject_ids else None),
                evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
                metadata={
                    "sourceTurnIdHash": _hash(row["source_turn_id"]) if row["source_turn_id"] else None,
                    "scopeItemCount": len(subject_ids) if isinstance(subject_ids, list) else 0,
                    "undoAvailable": not bool(row["undone_by_event_id"]),
                },
            )

    def _job_runs(self, workspace_id: str) -> Iterable[AuditEvent]:
        for row in self.store.fetch_all(
            """
            SELECT run_id, status, attempt_count, started_at, completed_at,
                   receipt_id, correlation_id, input_hash, error_json
            FROM job_runs WHERE workspace_id = ?
            ORDER BY COALESCE(started_at, completed_at), run_id
            """,
            (workspace_id,),
        ):
            error = _parse_json(row["error_json"])
            yield AuditEvent(
                event_id=str(row["run_id"]),
                workspace_id=workspace_id,
                kind="job_run",
                action="daily_close",
                status=str(row["status"]),
                occurred_at=str(row["completed_at"] or row["started_at"]),
                actor="system",
                correlation_id=str(row["correlation_id"]),
                subject_type="job_run",
                subject_id=str(row["run_id"]),
                evidence_ids=(),
                metadata={
                    "attemptCount": int(row["attempt_count"]),
                    "receiptId": str(row["receipt_id"]) if row["receipt_id"] else None,
                    "inputHash": str(row["input_hash"]),
                    "errorType": error.get("type") or error.get("code"),
                    "errorContentIncluded": False,
                },
            )

    def _sources(self, workspace_id: str) -> Iterable[AuditEvent]:
        for row in self.store.fetch_all(
            """
            SELECT source_item_id, source_type, digest, mapping_version,
                   received_at, status, row_count
            FROM source_items WHERE workspace_id = ?
            ORDER BY received_at, source_item_id
            """,
            (workspace_id,),
        ):
            yield AuditEvent(
                event_id=_stable_id("audit", "source", str(row["source_item_id"])),
                workspace_id=workspace_id,
                kind="source_ingest",
                action=str(row["source_type"]),
                status=str(row["status"]),
                occurred_at=str(row["received_at"]),
                actor="system",
                correlation_id=None,
                subject_type="source_item",
                subject_id=str(row["source_item_id"]),
                evidence_ids=(),
                metadata={
                    "digest": str(row["digest"]),
                    "mappingVersion": str(row["mapping_version"]),
                    "rowCount": int(row["row_count"]),
                    "sourceLabelIncluded": False,
                    "sourceRowsIncluded": False,
                },
            )

    def _model_runs(self, workspace_id: str) -> Iterable[AuditEvent]:
        for row in self.store.fetch_all(
            "SELECT model_run_id, receipt_json, created_at FROM model_runs WHERE workspace_id = ? ORDER BY created_at, model_run_id",
            (workspace_id,),
        ):
            receipt = _parse_json(row["receipt_json"])
            yield AuditEvent(
                event_id=str(row["model_run_id"]),
                workspace_id=workspace_id,
                kind="model_run",
                action=str(receipt.get("capability") or "model_request"),
                status=str(receipt.get("status") or "unknown"),
                occurred_at=str(receipt.get("occurredAt") or row["created_at"]),
                actor="model",
                correlation_id=str(receipt.get("runId")) if receipt.get("runId") else None,
                subject_type="model",
                subject_id=str(receipt.get("model")) if receipt.get("model") else None,
                evidence_ids=(),
                metadata={
                    "mode": receipt.get("mode"),
                    "provider": receipt.get("provider"),
                    "inputCharacters": receipt.get("inputCharacters", 0),
                    "outputCharacters": receipt.get("outputCharacters", 0),
                    "latencyMs": receipt.get("latencyMs", 0),
                    "promptIncluded": False,
                    "outputIncluded": False,
                },
            )

    def _egress(self, workspace_id: str) -> Iterable[AuditEvent]:
        for row in self.store.fetch_all(
            "SELECT receipt_id, receipt_json, created_at FROM egress_receipts WHERE workspace_id = ? ORDER BY created_at, receipt_id",
            (workspace_id,),
        ):
            receipt = _parse_json(row["receipt_json"])
            yield AuditEvent(
                event_id=str(row["receipt_id"]),
                workspace_id=workspace_id,
                kind="egress",
                action=str(receipt.get("purpose") or "typed_projection"),
                status="committed",
                occurred_at=str(receipt.get("occurredAt") or row["created_at"]),
                actor="system",
                correlation_id=str(receipt.get("runId")) if receipt.get("runId") else None,
                subject_type="egress_projection",
                subject_id=str(row["receipt_id"]),
                evidence_ids=(),
                metadata={
                    "mode": receipt.get("mode"),
                    "provider": receipt.get("provider"),
                    "model": receipt.get("model"),
                    "fieldClasses": receipt.get("fieldClasses", []),
                    "itemCount": receipt.get("itemCount", 0),
                    "characterCount": receipt.get("characterCount", 0),
                    "payloadIncluded": False,
                },
            )

    def _optional_events(self, workspace_id: str) -> Iterable[AuditEvent]:
        if self._table_exists("invoice_settlement_events"):
            for row in self.store.fetch_all(
                "SELECT * FROM invoice_settlement_events WHERE workspace_id = ? ORDER BY occurred_at, event_id",
                (workspace_id,),
            ):
                yield AuditEvent(
                    event_id=str(row["event_id"]),
                    workspace_id=workspace_id,
                    kind="invoice_settlement",
                    action="owner_confirmed_payment",
                    status="confirmed",
                    occurred_at=str(row["occurred_at"]),
                    actor="owner",
                    correlation_id=None,
                    subject_type="invoice",
                    subject_id=str(row["invoice_id"]),
                    evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
                    metadata={
                        "transactionId": str(row["transaction_id"]),
                        "candidateId": str(row["candidate_id"]),
                        "reasonIncluded": False,
                    },
                )
        if self._table_exists("category_budget_policy_revisions"):
            for row in self.store.fetch_all(
                "SELECT * FROM category_budget_policy_revisions WHERE workspace_id = ? ORDER BY created_at, policy_id, revision",
                (workspace_id,),
            ):
                yield AuditEvent(
                    event_id=_stable_id("audit", str(row["policy_id"]), str(row["revision"])),
                    workspace_id=workspace_id,
                    kind="budget_policy",
                    action="category_budget_revision",
                    status=str(row["status"]),
                    occurred_at=str(row["created_at"]),
                    actor=str(row["source"]),
                    correlation_id=None,
                    subject_type="budget_policy",
                    subject_id=str(row["policy_id"]),
                    evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
                    metadata={
                        "revision": int(row["revision"]),
                        "category": str(row["category"]),
                        "periodStart": str(row["period_start"]),
                        "periodEnd": str(row["period_end"]),
                        "limitMinor": int(row["limit_minor"]),
                    },
                )
        if self._table_exists("reserve_policy_revisions"):
            for row in self.store.fetch_all(
                "SELECT * FROM reserve_policy_revisions WHERE workspace_id = ? ORDER BY created_at, revision",
                (workspace_id,),
            ):
                yield AuditEvent(
                    event_id=_stable_id("audit", workspace_id, "reserve", str(row["revision"])),
                    workspace_id=workspace_id,
                    kind="reserve_policy",
                    action="protected_reserve_revision",
                    status="committed",
                    occurred_at=str(row["created_at"]),
                    actor=str(row["source"]),
                    correlation_id=None,
                    subject_type="workspace",
                    subject_id=workspace_id,
                    evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
                    metadata={
                        "revision": int(row["revision"]),
                        "protectedReserveMinor": int(row["protected_reserve_minor"]),
                        "rationaleIncluded": False,
                    },
                )
        if self._table_exists("cash_commitments"):
            for row in self.store.fetch_all(
                "SELECT * FROM cash_commitments WHERE workspace_id = ? ORDER BY created_at, commitment_id",
                (workspace_id,),
            ):
                yield AuditEvent(
                    event_id=_stable_id("audit", str(row["commitment_id"]), str(row["updated_at"])),
                    workspace_id=workspace_id,
                    kind="cash_commitment",
                    action="cash_commitment_state",
                    status=str(row["status"]),
                    occurred_at=str(row["updated_at"]),
                    actor=str(row["source"]),
                    correlation_id=None,
                    subject_type="cash_commitment",
                    subject_id=str(row["commitment_id"]),
                    evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
                    metadata={
                        "amountMinor": int(row["amount_minor"]),
                        "currency": str(row["currency"]),
                        "dueOn": str(row["due_on"]),
                        "labelIncluded": False,
                    },
                )
        if self._table_exists("sales_invoice_revisions"):
            for row in self.store.fetch_all(
                """
                SELECT r.invoice_id, r.revision, r.status, r.content_hash,
                       r.created_at, i.invoice_number
                FROM sales_invoice_revisions r
                JOIN sales_invoices i ON i.invoice_id = r.invoice_id
                WHERE i.workspace_id = ?
                ORDER BY r.created_at, r.invoice_id, r.revision
                """,
                (workspace_id,),
            ):
                yield AuditEvent(
                    event_id=_stable_id("audit", str(row["invoice_id"]), str(row["revision"])),
                    workspace_id=workspace_id,
                    kind="invoice_lifecycle",
                    action="invoice_revision",
                    status=str(row["status"]),
                    occurred_at=str(row["created_at"]),
                    actor="owner",
                    correlation_id=None,
                    subject_type="invoice",
                    subject_id=str(row["invoice_id"]),
                    evidence_ids=(),
                    metadata={
                        "revision": int(row["revision"]),
                        "invoiceNumberHash": _hash(row["invoice_number"]),
                        "contentHash": str(row["content_hash"]),
                        "invoicePayloadIncluded": False,
                    },
                )

    def events(
        self,
        *,
        workspace_id: str,
        kinds: tuple[str, ...] = (),
        statuses: tuple[str, ...] = (),
        start_at: str | None = None,
        end_at: str | None = None,
        query: str | None = None,
        limit: int = 500,
    ) -> tuple[AuditEvent, ...]:
        if not 1 <= limit <= 5000:
            raise ValueError("audit limit must be between 1 and 5000")
        if any(kind not in ALLOWED_KINDS for kind in kinds):
            raise ValueError("unsupported audit event kind")
        start = datetime.fromisoformat(start_at) if start_at else None
        end = datetime.fromisoformat(end_at) if end_at else None
        if start and end and start > end:
            raise ValueError("audit start must be on or before end")
        values = [
            *self._finance_events(workspace_id),
            *self._job_runs(workspace_id),
            *self._sources(workspace_id),
            *self._model_runs(workspace_id),
            *self._egress(workspace_id),
            *self._optional_events(workspace_id),
        ]
        query_value = query.casefold().strip() if query else None
        filtered: list[AuditEvent] = []
        for event in values:
            occurred = datetime.fromisoformat(event.occurred_at)
            if kinds and event.kind not in kinds:
                continue
            if statuses and event.status not in statuses:
                continue
            if start and occurred < start:
                continue
            if end and occurred > end:
                continue
            if query_value:
                haystack = " ".join(
                    value
                    for value in (
                        event.event_id,
                        event.kind,
                        event.action,
                        event.status,
                        event.correlation_id,
                        event.subject_type,
                        event.subject_id,
                    )
                    if value
                ).casefold()
                if query_value not in haystack:
                    continue
            filtered.append(event)
        return tuple(
            sorted(filtered, key=lambda value: (value.occurred_at, value.event_id), reverse=True)[:limit]
        )

    def export_jsonl(
        self,
        *,
        workspace_id: str,
        kinds: tuple[str, ...] = (),
        statuses: tuple[str, ...] = (),
        start_at: str | None = None,
        end_at: str | None = None,
        query: str | None = None,
    ) -> tuple[str, bytes, str, int]:
        events = self.events(
            workspace_id=workspace_id,
            kinds=kinds,
            statuses=statuses,
            start_at=start_at,
            end_at=end_at,
            query=query,
            limit=5000,
        )
        filters = {
            "kinds": list(kinds),
            "statuses": list(statuses),
            "startAt": start_at,
            "endAt": end_at,
            "query": query,
        }
        manifest = {
            "auditExportVersion": "folio.audit-export@1",
            "workspaceId": workspace_id,
            "eventCount": len(events),
            "filters": filters,
            "rawOwnerContentIncluded": False,
            "rawSourceContentIncluded": False,
            "modelPromptOrOutputIncluded": False,
        }
        lines = [canonical_json({"type": "manifest", **manifest})]
        lines.extend(
            canonical_json({"type": "event", **event.as_dict()})
            for event in reversed(events)
        )
        content = ("\n".join(lines) + "\n").encode("utf-8")
        content_hash = hashlib.sha256(content).hexdigest()
        export_id = _stable_id(
            "auditexport", workspace_id, canonical_json(filters)
        )
        created_at = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS revision FROM audit_trail_export_revisions WHERE export_id = ?",
                (export_id,),
            ).fetchone()
            revision = int(row["revision"]) + 1
            connection.execute(
                """
                INSERT INTO audit_trail_export_revisions(
                    export_id, revision, workspace_id, start_at, end_at,
                    filters_json, event_count, content, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    export_id,
                    revision,
                    workspace_id,
                    start_at,
                    end_at,
                    canonical_json(filters),
                    len(events),
                    content,
                    content_hash,
                    created_at,
                ),
            )
        return export_id, content, content_hash, len(events)
'''

SERVICE_METHODS = '''    async def audit_trail_events(
        self,
        *,
        workspace_id: str,
        kinds: tuple[str, ...],
        statuses: tuple[str, ...],
        start_at: str | None,
        end_at: str | None,
        query: str | None,
        limit: int,
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return tuple(
            event.as_dict()
            for event in UnifiedAuditTrailService(self.store).events(
                workspace_id=workspace_id,
                kinds=kinds,
                statuses=statuses,
                start_at=start_at,
                end_at=end_at,
                query=query,
                limit=limit,
            )
        )

    async def audit_trail_export_payload(
        self,
        *,
        workspace_id: str,
        kinds: tuple[str, ...],
        statuses: tuple[str, ...],
        start_at: str | None,
        end_at: str | None,
        query: str | None,
    ) -> ArtifactPayload:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            export_id, content, content_hash, _count = UnifiedAuditTrailService(
                self.store
            ).export_jsonl(
                workspace_id=workspace_id,
                kinds=kinds,
                statuses=statuses,
                start_at=start_at,
                end_at=end_at,
                query=query,
            )
        return ArtifactPayload(
            content=content,
            media_type="application/x-ndjson; charset=utf-8",
            filename=f"folio-audit-{export_id}.jsonl",
            content_hash=content_hash,
        )
'''

ROUTES = '''    @router.get("/v1/workspaces/{workspace_id}/audit-trail")
    async def audit_trail_events(
        workspace_id: PathIdentifier,
        services: Services,
        kind: Annotated[list[str] | None, Query()] = None,
        status: Annotated[list[str] | None, Query()] = None,
        start_at: Annotated[datetime | None, Query(alias="startAt")] = None,
        end_at: Annotated[datetime | None, Query(alias="endAt")] = None,
        query: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=5000)] = 500,
    ) -> dict[str, object]:
        try:
            events = await services.audit_trail_events(
                workspace_id=workspace_id,
                kinds=tuple(kind or ()),
                statuses=tuple(status or ()),
                start_at=start_at.isoformat() if start_at else None,
                end_at=end_at.isoformat() if end_at else None,
                query=query,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"workspaceId": workspace_id, "events": list(events)}

    @router.get("/v1/workspaces/{workspace_id}/audit-trail.jsonl")
    async def audit_trail_export(
        workspace_id: PathIdentifier,
        services: Services,
        kind: Annotated[list[str] | None, Query()] = None,
        status: Annotated[list[str] | None, Query()] = None,
        start_at: Annotated[datetime | None, Query(alias="startAt")] = None,
        end_at: Annotated[datetime | None, Query(alias="endAt")] = None,
        query: Annotated[str | None, Query(max_length=200)] = None,
    ) -> Response:
        try:
            value = await services.audit_trail_export_payload(
                workspace_id=workspace_id,
                kinds=tuple(kind or ()),
                statuses=tuple(status or ()),
                start_at=start_at.isoformat() if start_at else None,
                end_at=end_at.isoformat() if end_at else None,
                query=query,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return Response(
            content=value.content,
            media_type=value.media_type,
            headers={
                "Content-Disposition": content_disposition(
                    value.filename, disposition="attachment"
                ),
                "ETag": f'"{value.content_hash}"',
                "Cache-Control": "no-store",
            },
        )

'''

TESTS = '''from __future__ import annotations

import json
from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.finance.budgets import BudgetReservePolicyService
from finance_agent.finance.receivables import ReceivablesService
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore
from finance_agent.audit_trail import UnifiedAuditTrailService

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def setup(tmp_path: Path) -> tuple[SQLiteStore, FinanceEngine]:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    DailyCloseService(engine).run()
    BudgetReservePolicyService(store).set_budget(
        workspace_id="ws_koru_studio",
        category="software_subscriptions",
        period_start="2026-07-01",
        period_end="2026-07-31",
        limit_minor=25000,
        evidence_ids=("evd_koru_bank_csv",),
    )
    return store, engine


def test_timeline_unifies_core_events_without_raw_content(tmp_path: Path) -> None:
    store, _engine = setup(tmp_path)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO conversation_turns(turn_id, workspace_id, thread_id, role, content, occurred_at, status, evidence_ids_json, model_mode) VALUES ('turn_audit_private', 'ws_koru_studio', 'thr_koru_studio_main', 'owner', 'AUDIT PRIVATE OWNER TEXT 987654', '2026-08-26T00:00:00+00:00', 'complete', '[]', 'local')"
        )
    events = UnifiedAuditTrailService(store).events(
        workspace_id="ws_koru_studio",
        limit=1000,
    )
    kinds = {event.kind for event in events}
    assert {"job_run", "source_ingest", "budget_policy"}.issubset(kinds)
    encoded = json.dumps([event.as_dict() for event in events])
    assert "AUDIT PRIVATE OWNER TEXT 987654" not in encoded
    assert all(event.as_dict()["rawContentIncluded"] is False for event in events)
    budget = next(event for event in events if event.kind == "budget_policy")
    assert budget.evidence_ids == ("evd_koru_bank_csv",)


def test_filters_are_bounded_and_search_only_identifiers_and_types(tmp_path: Path) -> None:
    store, _engine = setup(tmp_path)
    service = UnifiedAuditTrailService(store)
    jobs = service.events(
        workspace_id="ws_koru_studio",
        kinds=("job_run",),
        statuses=("completed",),
        query="daily_close",
        limit=10,
    )
    assert jobs
    assert all(event.kind == "job_run" for event in jobs)
    assert service.events(
        workspace_id="ws_koru_studio",
        query="private source description that must not be indexed",
        limit=10,
    ) == ()


def test_jsonl_export_is_append_only_hashable_and_content_minimised(tmp_path: Path) -> None:
    store, _engine = setup(tmp_path)
    service = UnifiedAuditTrailService(store)
    export_id, content, content_hash, count = service.export_jsonl(
        workspace_id="ws_koru_studio",
        kinds=("source_ingest", "job_run", "budget_policy"),
    )
    assert export_id.startswith("auditexport_")
    assert len(content_hash) == 64
    lines = content.decode().splitlines()
    manifest = json.loads(lines[0])
    assert manifest["type"] == "manifest"
    assert manifest["eventCount"] == count
    assert manifest["rawOwnerContentIncluded"] is False
    assert manifest["rawSourceContentIncluded"] is False
    assert all(json.loads(line)["type"] == "event" for line in lines[1:])
    service.export_jsonl(
        workspace_id="ws_koru_studio",
        kinds=("source_ingest", "job_run", "budget_policy"),
    )
    rows = store.fetch_all(
        "SELECT revision FROM audit_trail_export_revisions WHERE export_id = ? ORDER BY revision",
        (export_id,),
    )
    assert [int(row["revision"]) for row in rows] == [1, 2]


def test_unsupported_kind_and_reversed_time_fail_closed(tmp_path: Path) -> None:
    store, _engine = setup(tmp_path)
    service = UnifiedAuditTrailService(store)
    try:
        service.events(workspace_id="ws_koru_studio", kinds=("raw_prompt",))
    except ValueError:
        pass
    else:
        raise AssertionError("unsupported audit kind was accepted")
    try:
        service.events(
            workspace_id="ws_koru_studio",
            start_at="2026-08-27T00:00:00+00:00",
            end_at="2026-08-26T00:00:00+00:00",
        )
    except ValueError:
        pass
    else:
        raise AssertionError("reversed audit range was accepted")
'''


def add_migration_module() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    content = read(path)
    versions = [int(value) for value in re.findall(r"version=(\d+)", content)]
    version = max(versions) + 1
    closing = content.rfind("\n)")
    if closing < 0:
        raise RuntimeError("MIGRATIONS tuple close not found")
    prefix = content[:closing].rstrip()
    if not prefix.endswith(","):
        prefix += ","
    write(path, prefix + "\n" + MIGRATION.format(version=version) + content[closing:])
    write("services/api/src/finance_agent/audit_trail.py", MODULE)


def update_service_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.support_bundle import RedactedSupportBundleService\n"
    import_line = "from finance_agent.audit_trail import UnifiedAuditTrailService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("support bundle import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "generate_support_bundle", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def generate_support_bundle(\n"
    addition = '''    async def audit_trail_events(\n        self, *, workspace_id: str, kinds: tuple[str, ...],\n        statuses: tuple[str, ...], start_at: str | None, end_at: str | None,\n        query: str | None, limit: int\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def audit_trail_export_payload(\n        self, *, workspace_id: str, kinds: tuple[str, ...],\n        statuses: tuple[str, ...], start_at: str | None, end_at: str | None,\n        query: str | None\n    ) -> ArtifactPayload: ...\n\n'''
    if marker not in content:
        raise RuntimeError("support bundle protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    marker = '    @router.post("/v1/workspaces/{workspace_id}/support-bundle")\n'
    if marker not in content:
        raise RuntimeError("support bundle route marker missing")
    content = content.replace(marker, ROUTES + marker, 1)
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/test_unified_audit_trail.py", TESTS)
    write("docs/AUDIT_TRAIL.md", '''# Unified audit trail\n\nFolio projects existing append-only records into one typed timeline. It includes finance events, Daily Close runs, source-ingest receipts, model metadata, typed egress receipts, invoice settlements, budgets, reserve policies, cash commitments and invoice lifecycle revisions. Optional categories appear only when their tables exist.\n\nAudit events expose IDs, action, status, time, actor, correlation, subject, evidence and bounded metadata. They do not include owner messages, source labels or rows, finance-event reasons, model prompts/output, typed egress payloads, invoice payloads or reserve rationale. Search covers identifiers and event classifications only.\n\nJSONL exports begin with a manifest, are content-hashed and persist as append-only revisions. Export generation proves only that the bytes exist locally; it does not prove an accountant, auditor or support recipient received or reviewed them.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 31: unified content-minimised audit trail\n\n- Existing append-only receipts project into one typed timeline.\n- Filters cover kind, status, date, identifier and bounded result count.\n- Evidence, correlation and content hashes remain visible.\n- Owner prose, source rows, prompts, model output and egress payloads remain absent.\n- JSONL exports carry a manifest and append-only revisions.\n- Local export creation remains separate from recipient-visible receipt or review.\n'''
    if "## Stack 31: unified content-minimised audit trail" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_service_protocol_routes()
    tests_docs()
    print("unified audit trail changes applied")


if __name__ == "__main__":
    main()
