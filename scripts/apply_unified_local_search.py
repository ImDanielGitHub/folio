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
        name="workspace_local_search_receipts",
        sql="""
        CREATE TABLE workspace_search_receipts (
            receipt_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            query_hash TEXT NOT NULL CHECK (length(query_hash) = 64),
            result_ids_json TEXT NOT NULL,
            result_type_counts_json TEXT NOT NULL,
            max_results INTEGER NOT NULL CHECK (max_results BETWEEN 1 AND 100),
            result_count INTEGER NOT NULL CHECK (result_count BETWEEN 0 AND 100),
            created_at TEXT NOT NULL
        );

        CREATE INDEX workspace_search_receipts_time
            ON workspace_search_receipts(workspace_id, created_at DESC);
        """,
    ),
'''

MODULE = '''"""Deterministic local search across finance, knowledge, invoices and audit IDs."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from finance_agent.audit_trail import UnifiedAuditTrailService
from finance_agent.storage import SQLiteStore, canonical_json

QUERY_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_'’-]{0,63}")


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _snippet(value: object, query: str, limit: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    index = text.casefold().find(query.casefold())
    start = max(0, index - limit // 3) if index >= 0 else 0
    end = min(len(text), start + limit)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _tokens(query: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(match.group().casefold() for match in QUERY_TOKEN.finditer(query)))


def _score(query: str, identifier: str, title: str, body: str) -> int:
    needle = query.casefold()
    identifier_value = identifier.casefold()
    title_value = title.casefold()
    body_value = body.casefold()
    if identifier_value == needle:
        return 10000
    if identifier_value.startswith(needle):
        return 9500
    if title_value == needle:
        return 9250
    if title_value.startswith(needle):
        return 9000
    if needle in title_value:
        return 8000
    if needle in body_value:
        return 6500
    terms = _tokens(query)
    hits = sum(term in (title_value + " " + body_value) for term in terms)
    return min(6400, 3000 + hits * 600)


@dataclass(frozen=True, slots=True)
class SearchResult:
    result_type: str
    result_id: str
    title: str
    subtitle: str
    occurred_at: str | None
    amount_minor: int | None
    currency: str | None
    evidence_ids: tuple[str, ...]
    score_basis_points: int
    metadata: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "resultType": self.result_type,
            "resultId": self.result_id,
            "title": self.title,
            "subtitle": self.subtitle,
            "occurredAt": self.occurred_at,
            "amountMinor": self.amount_minor,
            "currency": self.currency,
            "evidenceIds": list(self.evidence_ids),
            "scoreBasisPoints": self.score_basis_points,
            "metadata": self.metadata,
        }


class WorkspaceSearchService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def _table_exists(self, name: str) -> bool:
        return self.store.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ) is not None

    def _transactions(self, workspace_id: str, query: str) -> list[SearchResult]:
        pattern = _like(query)
        rows = self.store.fetch_all(
            """
            SELECT transaction_id, occurred_on, description, amount_minor,
                   currency, classification, category, status, evidence_id
            FROM transactions
            WHERE workspace_id = ? AND (
                transaction_id LIKE ? ESCAPE '\\'
                OR description LIKE ? ESCAPE '\\'
                OR COALESCE(category, '') LIKE ? ESCAPE '\\'
                OR classification LIKE ? ESCAPE '\\'
            )
            ORDER BY occurred_on DESC, transaction_id
            LIMIT 200
            """,
            (workspace_id, pattern, pattern, pattern, pattern),
        )
        return [
            SearchResult(
                result_type="transaction",
                result_id=str(row["transaction_id"]),
                title=str(row["description"]),
                subtitle=" · ".join(
                    value
                    for value in (
                        str(row["occurred_on"]),
                        str(row["classification"]),
                        str(row["category"]) if row["category"] else None,
                        str(row["status"]),
                    )
                    if value
                ),
                occurred_at=str(row["occurred_on"]),
                amount_minor=int(row["amount_minor"]),
                currency=str(row["currency"]),
                evidence_ids=(str(row["evidence_id"]),),
                score_basis_points=_score(
                    query,
                    str(row["transaction_id"]),
                    str(row["description"]),
                    " ".join(
                        str(value or "")
                        for value in (row["classification"], row["category"], row["status"])
                    ),
                ),
                metadata={"classification": str(row["classification"]), "category": row["category"]},
            )
            for row in rows
        ]

    def _knowledge(self, workspace_id: str, query: str) -> list[SearchResult]:
        tokens = _tokens(query)
        if not tokens:
            return []
        fts_query = " AND ".join(f'"{token}"*' for token in tokens)
        rows = self.store.fetch_all(
            """
            SELECT workspace_id, record_type, record_id, title, body, tags
            FROM knowledge_fts
            WHERE workspace_id = ? AND knowledge_fts MATCH ?
            LIMIT 200
            """,
            (workspace_id, fts_query),
        )
        results: list[SearchResult] = []
        for row in rows:
            result_type = str(row["record_type"])
            result_id = str(row["record_id"])
            title = str(row["title"])
            body = str(row["body"])
            evidence_ids: tuple[str, ...] = ()
            occurred_at: str | None = None
            if result_type == "document":
                source = self.store.fetch_one(
                    "SELECT evidence_id, received_at FROM knowledge_documents WHERE document_id = ? AND workspace_id = ?",
                    (result_id, workspace_id),
                )
                if source:
                    evidence_ids = (str(source["evidence_id"]),) if source["evidence_id"] else ()
                    occurred_at = str(source["received_at"])
            elif result_type == "owner_statement":
                source = self.store.fetch_one(
                    "SELECT occurred_at FROM knowledge_owner_statements WHERE statement_id = ? AND workspace_id = ?",
                    (result_id, workspace_id),
                )
                occurred_at = str(source["occurred_at"]) if source else None
            elif result_type == "fact":
                source = self.store.fetch_one(
                    "SELECT evidence_id, recorded_at FROM knowledge_facts WHERE fact_id = ? AND workspace_id = ?",
                    (result_id, workspace_id),
                )
                if source:
                    evidence_ids = (str(source["evidence_id"]),) if source["evidence_id"] else ()
                    occurred_at = str(source["recorded_at"])
            results.append(
                SearchResult(
                    result_type=result_type,
                    result_id=result_id,
                    title=title,
                    subtitle=_snippet(body, query),
                    occurred_at=occurred_at,
                    amount_minor=None,
                    currency=None,
                    evidence_ids=evidence_ids,
                    score_basis_points=_score(query, result_id, title, body),
                    metadata={"tags": str(row["tags"]), "localKnowledge": True},
                )
            )
        return results

    def _invoices(self, workspace_id: str, query: str) -> list[SearchResult]:
        if not self._table_exists("sales_invoices"):
            return []
        pattern = _like(query)
        rows = self.store.fetch_all(
            """
            SELECT i.invoice_id, i.invoice_number, i.buyer_name, i.issue_date,
                   i.due_date, i.status, r.payload_json
            FROM sales_invoices i
            JOIN sales_invoice_revisions r ON r.invoice_id = i.invoice_id
            WHERE i.workspace_id = ? AND r.revision = (
                SELECT MAX(r2.revision) FROM sales_invoice_revisions r2
                WHERE r2.invoice_id = i.invoice_id
            ) AND (
                i.invoice_id LIKE ? ESCAPE '\\'
                OR i.invoice_number LIKE ? ESCAPE '\\'
                OR i.buyer_name LIKE ? ESCAPE '\\'
            )
            ORDER BY i.issue_date DESC, i.invoice_number
            LIMIT 100
            """,
            (workspace_id, pattern, pattern, pattern),
        )
        results: list[SearchResult] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            totals = payload.get("totals", {}) if isinstance(payload, dict) else {}
            title = f"Invoice {row['invoice_number']} · {row['buyer_name']}"
            results.append(
                SearchResult(
                    result_type="invoice",
                    result_id=str(row["invoice_id"]),
                    title=title,
                    subtitle=f"{row['status']} · issued {row['issue_date']} · due {row['due_date']}",
                    occurred_at=str(row["issue_date"]),
                    amount_minor=int(totals.get("grossMinor", 0)),
                    currency="NZD",
                    evidence_ids=(),
                    score_basis_points=_score(
                        query,
                        str(row["invoice_id"]),
                        title,
                        str(row["status"]),
                    ),
                    metadata={"status": str(row["status"]), "dueDate": str(row["due_date"])},
                )
            )
        return results

    def _audit(self, workspace_id: str, query: str) -> list[SearchResult]:
        events = UnifiedAuditTrailService(self.store).events(
            workspace_id=workspace_id,
            query=query,
            limit=100,
        )
        return [
            SearchResult(
                result_type="audit_event",
                result_id=event.event_id,
                title=f"{event.kind}: {event.action}",
                subtitle=f"{event.status} · {event.occurred_at}",
                occurred_at=event.occurred_at,
                amount_minor=None,
                currency=None,
                evidence_ids=event.evidence_ids,
                score_basis_points=_score(
                    query, event.event_id, event.action, event.kind + " " + event.status
                ),
                metadata={"kind": event.kind, "status": event.status, "subjectId": event.subject_id},
            )
            for event in events
        ]

    def search(
        self,
        *,
        workspace_id: str,
        query: str,
        result_types: tuple[str, ...] = (),
        max_results: int = 30,
    ) -> dict[str, object]:
        query_value = " ".join(query.split())
        if not 2 <= len(query_value) <= 200:
            raise ValueError("search query must contain between 2 and 200 characters")
        if not 1 <= max_results <= 100:
            raise ValueError("maxResults must be between 1 and 100")
        allowed_types = {
            "transaction", "document", "owner_statement", "entity", "fact",
            "invoice", "audit_event",
        }
        if any(value not in allowed_types for value in result_types):
            raise ValueError("unsupported search result type")
        values = [
            *self._transactions(workspace_id, query_value),
            *self._knowledge(workspace_id, query_value),
            *self._invoices(workspace_id, query_value),
            *self._audit(workspace_id, query_value),
        ]
        if result_types:
            values = [value for value in values if value.result_type in result_types]
        deduplicated: dict[tuple[str, str], SearchResult] = {}
        for value in values:
            key = (value.result_type, value.result_id)
            existing = deduplicated.get(key)
            if existing is None or value.score_basis_points > existing.score_basis_points:
                deduplicated[key] = value
        ordered = sorted(
            deduplicated.values(),
            key=lambda value: (
                -value.score_basis_points,
                value.result_type,
                value.result_id,
            ),
        )[:max_results]
        counts: dict[str, int] = {}
        for value in ordered:
            counts[value.result_type] = counts.get(value.result_type, 0) + 1
        query_hash = hashlib.sha256(query_value.encode()).hexdigest()
        receipt_id = _stable_id(
            "searchrcpt", workspace_id, query_hash, datetime.now(UTC).isoformat()
        )
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO workspace_search_receipts(
                    receipt_id, workspace_id, query_hash, result_ids_json,
                    result_type_counts_json, max_results, result_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    workspace_id,
                    query_hash,
                    canonical_json([f"{value.result_type}:{value.result_id}" for value in ordered]),
                    canonical_json(counts),
                    max_results,
                    len(ordered),
                    datetime.now(UTC).isoformat(),
                ),
            )
        return {
            "searchVersion": "folio.workspace-search@1",
            "workspaceId": workspace_id,
            "queryHash": query_hash,
            "queryStored": False,
            "receiptId": receipt_id,
            "resultCount": len(ordered),
            "resultTypeCounts": counts,
            "results": [value.as_dict() for value in ordered],
            "modelUsed": False,
            "externalCallsMade": False,
        }
'''

SERVICE_METHOD = '''    async def search_workspace(
        self,
        *,
        workspace_id: str,
        query: str,
        result_types: tuple[str, ...],
        max_results: int,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return WorkspaceSearchService(self.store).search(
            workspace_id=workspace_id,
            query=query,
            result_types=result_types,
            max_results=max_results,
        )
'''

ROUTE = '''    @router.get("/v1/workspaces/{workspace_id}/search")
    async def search_workspace(
        workspace_id: PathIdentifier,
        services: Services,
        query: Annotated[str, Query(alias="q", min_length=2, max_length=200)],
        result_type: Annotated[list[str] | None, Query(alias="type")] = None,
        max_results: Annotated[int, Query(alias="maxResults", ge=1, le=100)] = 30,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.search_workspace(
                    workspace_id=workspace_id,
                    query=query,
                    result_types=tuple(result_type or ()),
                    max_results=max_results,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.finance.invoices import SalesInvoiceService
from finance_agent.jobs import DailyCloseService
from finance_agent.search import WorkspaceSearchService
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def setup(tmp_path: Path) -> WorkspaceSearchService:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    DailyCloseService(engine).run()
    invoices = SalesInvoiceService(store)
    invoices.save_draft(
        workspace_id="ws_koru_studio",
        invoice_id=None,
        invoice_number="INV-SEARCH-001",
        seller_name="Koru Studio",
        seller_nzbn=None,
        buyer_name="Searchable Acme",
        buyer_nzbn=None,
        issue_date="2026-08-01",
        due_date="2026-08-15",
        notes=None,
        lines=({"description": "Search design", "quantityMillis": 1000, "unitPriceMinor": 10000, "taxTreatment": "standard"},),
    )
    return WorkspaceSearchService(store)


def test_transaction_search_returns_exact_money_and_evidence(tmp_path: Path) -> None:
    service = setup(tmp_path)
    result = service.search(
        workspace_id="ws_koru_studio",
        query="MITRE 10",
        result_types=("transaction",),
    )
    assert result["modelUsed"] is False
    assert result["externalCallsMade"] is False
    assert result["queryStored"] is False
    assert result["resultCount"] == 1
    item = result["results"][0]
    assert item["resultId"] == "txn_koru_006"
    assert item["amountMinor"] == -18475
    assert item["currency"] == "NZD"
    assert item["evidenceIds"] == ["evd_koru_mitre10_row"]


def test_invoice_and_audit_identifiers_are_searchable(tmp_path: Path) -> None:
    service = setup(tmp_path)
    invoice = service.search(
        workspace_id="ws_koru_studio",
        query="INV-SEARCH-001",
        result_types=("invoice",),
    )
    assert invoice["resultCount"] == 1
    assert invoice["results"][0]["title"].startswith("Invoice INV-SEARCH-001")
    audit = service.search(
        workspace_id="ws_koru_studio",
        query="daily_close",
        result_types=("audit_event",),
    )
    assert audit["resultCount"] >= 1
    assert all(item["resultType"] == "audit_event" for item in audit["results"])


def test_query_receipt_stores_hash_and_ids_not_raw_query(tmp_path: Path) -> None:
    service = setup(tmp_path)
    query = "PRIVATE SEARCH QUERY 665544"
    result = service.search(
        workspace_id="ws_koru_studio",
        query=query,
        max_results=20,
    )
    row = service.store.fetch_one(
        "SELECT * FROM workspace_search_receipts WHERE receipt_id = ?",
        (result["receiptId"],),
    )
    assert str(row["query_hash"]) == result["queryHash"]
    assert query not in str(tuple(row))
    assert int(row["result_count"]) == result["resultCount"]


def test_invalid_type_short_query_and_result_limit_fail_closed(tmp_path: Path) -> None:
    service = setup(tmp_path)
    for kwargs in (
        {"query": "x"},
        {"query": "valid", "result_types": ("raw_prompt",)},
        {"query": "valid", "max_results": 101},
    ):
        try:
            service.search(workspace_id="ws_koru_studio", **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid search accepted: {kwargs}")
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
    write("services/api/src/finance_agent/search.py", MODULE)


def update_service_protocol_route() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.audit_trail import UnifiedAuditTrailService\n"
    import_line = "from finance_agent.search import WorkspaceSearchService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("audit trail import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "audit_trail_events", SERVICE_METHOD)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def audit_trail_events(\n"
    addition = '''    async def search_workspace(\n        self, *, workspace_id: str, query: str,\n        result_types: tuple[str, ...], max_results: int\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("audit protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    marker = '    @router.get("/v1/workspaces/{workspace_id}/audit-trail")\n'
    if marker not in content:
        raise RuntimeError("audit route marker missing")
    content = content.replace(marker, ROUTE + marker, 1)
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/test_workspace_search.py", TESTS)
    write("docs/LOCAL_SEARCH.md", '''# Deterministic local search\n\nFolio searches posted transactions, indexed documents, owner statements, entities, structured facts, invoice identifiers/buyers and content-minimised audit identifiers on the local SQLite database. It does not call a model or external service. Results remain typed and retain exact amounts and evidence where those fields exist.\n\nSearch ranking is deterministic: exact and prefix identifier/title matches rank above title/body token matches. Result count and type filters are bounded. Knowledge snippets are limited to 240 characters. Search receipts store only the SHA-256 query hash, selected result IDs, type counts and limits; the raw query is not stored in the receipt.\n\nSearch finds evidence and records. It does not change finance truth, classify a transaction, settle an invoice or assert that a text match is semantically correct.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 33: deterministic unified local search\n\n- Transactions, local knowledge, invoices and audit identifiers share one bounded API.\n- Search is local and model-free.\n- Exact money and evidence stay attached to finance results.\n- Ranking is deterministic and type/result limits are explicit.\n- Receipts retain only query hashes, selected IDs and counts.\n- Search does not mutate or become an authority for finance meaning.\n'''
    if "## Stack 33: deterministic unified local search" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_service_protocol_route()
    tests_docs()
    print("unified local search changes applied")


if __name__ == "__main__":
    main()
