from __future__ import annotations

import ast
import json
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
        name="keyset_pagination_indexes",
        sql="""
        CREATE INDEX IF NOT EXISTS transactions_workspace_date_id_desc
            ON transactions(workspace_id, occurred_on DESC, transaction_id DESC);
        CREATE INDEX IF NOT EXISTS transactions_workspace_class_date
            ON transactions(
                workspace_id, classification, occurred_on DESC, transaction_id DESC
            );
        CREATE INDEX IF NOT EXISTS source_items_workspace_received_id_desc
            ON source_items(workspace_id, received_at DESC, source_item_id DESC);
        CREATE INDEX IF NOT EXISTS conversation_turns_workspace_thread_time_id
            ON conversation_turns(
                workspace_id, thread_id, occurred_at DESC, turn_id DESC
            );
        """,
    ),
'''

MODULE = '''"""Opaque validated keyset cursors and bounded transaction pages."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json

CURSOR_VERSION = "folio.transaction-cursor@1"
MAX_CURSOR_CHARACTERS = 512
MAX_LIMIT = 200


def _query_hash(
    *,
    classification: str,
    status: str,
    search: str | None,
    date_from: str | None,
    date_to: str | None,
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "classification": classification,
                "status": status,
                "search": search or "",
                "dateFrom": date_from,
                "dateTo": date_to,
            }
        ).encode()
    ).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class TransactionCursor:
    occurred_on: str
    transaction_id: str
    query_hash: str

    def encode(self) -> str:
        payload = canonical_json(
            {
                "version": CURSOR_VERSION,
                "occurredOn": self.occurred_on,
                "transactionId": self.transaction_id,
                "queryHash": self.query_hash,
            }
        ).encode()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @classmethod
    def decode(cls, value: str, *, expected_query_hash: str) -> TransactionCursor:
        if not value or len(value) > MAX_CURSOR_CHARACTERS:
            raise ValueError("transaction cursor is empty or too long")
        try:
            raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
            payload = json.loads(raw)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("transaction cursor is not valid base64url JSON") from exc
        expected = {"version", "occurredOn", "transactionId", "queryHash"}
        if not isinstance(payload, dict) or set(payload) != expected:
            raise ValueError("transaction cursor does not match the closed schema")
        if payload["version"] != CURSOR_VERSION:
            raise ValueError("unsupported transaction cursor version")
        try:
            occurred_on = date.fromisoformat(str(payload["occurredOn"])).isoformat()
        except ValueError as exc:
            raise ValueError("transaction cursor contains an invalid date") from exc
        transaction_id = str(payload["transactionId"])
        if not transaction_id.startswith("txn_") or len(transaction_id) > 113:
            raise ValueError("transaction cursor contains an invalid transaction id")
        query_hash = str(payload["queryHash"])
        if query_hash != expected_query_hash:
            raise ValueError("transaction cursor belongs to different filters")
        return cls(occurred_on, transaction_id, query_hash)


class TransactionPageService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def page(
        self,
        *,
        workspace_id: str,
        limit: int = 50,
        cursor: str | None = None,
        classification: str = "any",
        status: str = "any",
        search: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, object]:
        if not 1 <= limit <= MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
        if classification not in {"any", "business", "personal", "unresolved", "transfer"}:
            raise ValueError("unsupported classification filter")
        if status not in {"any", "posted", "pending", "duplicate", "ignored"}:
            raise ValueError("unsupported status filter")
        search_value = search.strip() if search else None
        if search_value and len(search_value) > 100:
            raise ValueError("transaction search must not exceed 100 characters")
        for label, raw in (("dateFrom", date_from), ("dateTo", date_to)):
            if raw:
                try:
                    date.fromisoformat(raw)
                except ValueError as exc:
                    raise ValueError(f"{label} must use YYYY-MM-DD") from exc
        if date_from and date_to and date_from > date_to:
            raise ValueError("dateFrom must be on or before dateTo")
        query_hash = _query_hash(
            classification=classification,
            status=status,
            search=search_value,
            date_from=date_from,
            date_to=date_to,
        )
        decoded = (
            TransactionCursor.decode(cursor, expected_query_hash=query_hash)
            if cursor else None
        )
        clauses = ["t.workspace_id = ?"]
        parameters: list[object] = [workspace_id]
        if classification != "any":
            clauses.append("t.classification = ?")
            parameters.append(classification)
        if status != "any":
            clauses.append("t.status = ?")
            parameters.append(status)
        if search_value:
            clauses.append("UPPER(t.description) LIKE UPPER(?) ESCAPE '\\'")
            escaped = search_value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            parameters.append(f"%{escaped}%")
        if date_from:
            clauses.append("t.occurred_on >= ?")
            parameters.append(date_from)
        if date_to:
            clauses.append("t.occurred_on <= ?")
            parameters.append(date_to)
        if decoded:
            clauses.append(
                "(t.occurred_on < ? OR (t.occurred_on = ? AND t.transaction_id < ?))"
            )
            parameters.extend(
                [decoded.occurred_on, decoded.occurred_on, decoded.transaction_id]
            )
        parameters.append(limit + 1)
        rows = self.store.fetch_all(
            f"""
            SELECT t.transaction_id, t.account_id, t.occurred_on, t.description,
                   t.amount_minor, t.currency, t.source_status, t.status,
                   t.classification, t.category, t.classification_source,
                   t.rule_id, t.duplicate_of_transaction_id, t.evidence_id,
                   r.source_item_id
            FROM transactions t
            JOIN source_rows r ON r.source_row_id = t.source_row_id
            WHERE {' AND '.join(clauses)}
            ORDER BY t.occurred_on DESC, t.transaction_id DESC
            LIMIT ?
            """,
            parameters,
        )
        has_more = len(rows) > limit
        visible = rows[:limit]
        items = [
            {
                "transactionId": str(row["transaction_id"]),
                "accountId": str(row["account_id"]),
                "sourceItemId": str(row["source_item_id"]),
                "occurredOn": str(row["occurred_on"]),
                "description": str(row["description"]),
                "amountMinor": int(row["amount_minor"]),
                "currency": str(row["currency"]),
                "sourceStatus": str(row["source_status"]),
                "status": str(row["status"]),
                "classification": str(row["classification"]),
                "category": str(row["category"]) if row["category"] else None,
                "classificationSource": str(row["classification_source"]),
                "ruleId": str(row["rule_id"]) if row["rule_id"] else None,
                "duplicateOfTransactionId": (
                    str(row["duplicate_of_transaction_id"])
                    if row["duplicate_of_transaction_id"] else None
                ),
                "evidenceIds": [str(row["evidence_id"])],
            }
            for row in visible
        ]
        next_cursor = None
        if has_more and visible:
            last = visible[-1]
            next_cursor = TransactionCursor(
                occurred_on=str(last["occurred_on"]),
                transaction_id=str(last["transaction_id"]),
                query_hash=query_hash,
            ).encode()
        return {
            "pageVersion": "folio.transaction-page@1",
            "workspaceId": workspace_id,
            "items": items,
            "limit": limit,
            "hasMore": has_more,
            "nextCursor": next_cursor,
            "filters": {
                "classification": classification,
                "status": status,
                "search": search_value,
                "dateFrom": date_from,
                "dateTo": date_to,
            },
            "ordering": ["occurredOn:desc", "transactionId:desc"],
            "offsetUsed": False,
        }
'''

SERVICE_METHOD = '''    async def transaction_page(
        self,
        *,
        workspace_id: str,
        limit: int,
        cursor: str | None,
        classification: str,
        status: str,
        search: str | None,
        date_from: str | None,
        date_to: str | None,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return TransactionPageService(self.store).page(
            workspace_id=workspace_id,
            limit=limit,
            cursor=cursor,
            classification=classification,
            status=status,
            search=search,
            date_from=date_from,
            date_to=date_to,
        )
'''

ROUTE = '''    @router.get("/v1/workspaces/{workspace_id}/transactions")
    async def transaction_page(
        workspace_id: PathIdentifier,
        services: Services,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
        classification: Annotated[str, Query(pattern=r"^(any|business|personal|unresolved|transfer)$")] = "any",
        status: Annotated[str, Query(pattern=r"^(any|posted|pending|duplicate|ignored)$")] = "any",
        search: Annotated[str | None, Query(max_length=100)] = None,
        date_from: Annotated[date | None, Query(alias="dateFrom")] = None,
        date_to: Annotated[date | None, Query(alias="dateTo")] = None,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.transaction_page(
                    workspace_id=workspace_id,
                    limit=limit,
                    cursor=cursor,
                    classification=classification,
                    status=status,
                    search=search,
                    date_from=date_from.isoformat() if date_from else None,
                    date_to=date_to.isoformat() if date_to else None,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

'''

CLIENT = '''import { requestJson } from "./transport";
import { OPERATIONS_WORKSPACE_ID } from "./operations";

export type TransactionPageItem = {
  transactionId: string;
  accountId: string;
  sourceItemId: string;
  occurredOn: string;
  description: string;
  amountMinor: number;
  currency: string;
  sourceStatus: string;
  status: string;
  classification: string;
  category: string | null;
  classificationSource: string;
  ruleId: string | null;
  duplicateOfTransactionId: string | null;
  evidenceIds: string[];
};

export type TransactionPage = {
  pageVersion: "folio.transaction-page@1";
  workspaceId: string;
  items: TransactionPageItem[];
  limit: number;
  hasMore: boolean;
  nextCursor: string | null;
  filters: Record<string, unknown>;
  ordering: string[];
  offsetUsed: false;
};

export type TransactionFilters = {
  classification: string;
  status: string;
  search: string;
};

export async function loadTransactionPage(
  filters: TransactionFilters,
  cursor: string | null,
): Promise<TransactionPage> {
  const parameters = new URLSearchParams({
    limit: "50",
    classification: filters.classification,
    status: filters.status,
  });
  if (filters.search.trim()) parameters.set("search", filters.search.trim());
  if (cursor) parameters.set("cursor", cursor);
  return requestJson<TransactionPage>(
    `/v1/workspaces/${encodeURIComponent(OPERATIONS_WORKSPACE_ID)}/transactions?${parameters}`,
    undefined,
    15_000,
  );
}
'''

COMPONENT = '''import { FormEvent, useCallback, useEffect, useState } from "react";
import { loadTransactionPage, type TransactionFilters, type TransactionPageItem } from "./pagination";
import "./pagination.css";

const money = (minor: number, currency: string) =>
  new Intl.NumberFormat("en-NZ", { style: "currency", currency }).format(minor / 100);

const initialFilters: TransactionFilters = { classification: "any", status: "any", search: "" };

export function PaginatedTransactions() {
  const [filters, setFilters] = useState(initialFilters);
  const [items, setItems] = useState<TransactionPageItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("Loading the first transaction page.");

  const load = useCallback(async (nextCursor: string | null, append: boolean) => {
    setBusy(true);
    try {
      const page = await loadTransactionPage(filters, nextCursor);
      setItems((current) => append ? [...current, ...page.items] : page.items);
      setCursor(page.nextCursor);
      setHasMore(page.hasMore);
      setMessage(`${append ? "Loaded" : "Showing"} ${page.items.length} transaction${page.items.length === 1 ? "" : "s"}. Offset pagination was not used.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Transaction page could not be loaded.");
    } finally {
      setBusy(false);
    }
  }, [filters]);

  useEffect(() => { void load(null, false); }, [load]);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setFilters({
      classification: String(data.get("classification")),
      status: String(data.get("status")),
      search: String(data.get("search") ?? ""),
    });
  };

  return <section className="paged-transactions" aria-labelledby="paged-transactions-title">
    <div className="paged-transactions-heading"><div><h2 id="paged-transactions-title">Transaction history</h2><p>Stable date and ID keysets, 50 rows at a time.</p></div></div>
    <form onSubmit={submit} className="paged-transaction-filters">
      <label><span>Classification</span><select name="classification" defaultValue={filters.classification}><option value="any">Any</option><option value="business">Business</option><option value="personal">Personal</option><option value="unresolved">Unresolved</option><option value="transfer">Transfer</option></select></label>
      <label><span>Status</span><select name="status" defaultValue={filters.status}><option value="any">Any</option><option value="posted">Posted</option><option value="pending">Pending</option><option value="duplicate">Duplicate</option><option value="ignored">Ignored</option></select></label>
      <label><span>Search description</span><input name="search" maxLength={100} defaultValue={filters.search} /></label>
      <button type="submit" disabled={busy}>Apply filters</button>
    </form>
    <p className="paged-transactions-status" role="status" aria-live="polite">{message}</p>
    <div className="operations-table-wrap"><table><thead><tr><th>Date</th><th>Description</th><th>Prepared as</th><th>Status</th><th>Amount</th></tr></thead><tbody>{items.map((item) => <tr key={item.transactionId}><td>{item.occurredOn}</td><td><strong>{item.description}</strong><small>{item.sourceItemId}</small></td><td>{item.category ?? item.classification}</td><td>{item.status}</td><td className={item.amountMinor < 0 ? "expense" : "income"}>{money(item.amountMinor, item.currency)}</td></tr>)}</tbody></table></div>
    {hasMore ? <button className="paged-load-more" type="button" disabled={busy || !cursor} onClick={() => void load(cursor, true)}>{busy ? "Loading…" : "Load 50 more"}</button> : <p className="paged-end">End of the matching transaction history.</p>}
  </section>;
}
'''

CSS = '''.paged-transactions{margin-top:24px}.paged-transactions-heading{display:flex;justify-content:space-between;gap:12px;align-items:end}.paged-transactions-heading h2{margin:0!important}.paged-transactions-heading p{margin:4px 0 0;color:var(--muted,#a9b2ab);font-size:12px}.paged-transaction-filters{display:grid;grid-template-columns:1fr 1fr 2fr auto;gap:9px;align-items:end;margin:12px 0}.paged-transaction-filters label{display:grid;gap:4px}.paged-transaction-filters span{font-size:12px;color:var(--muted,#a9b2ab)}.paged-transaction-filters input,.paged-transaction-filters select{background:rgba(0,0,0,.2);border:1px solid var(--line,#384039);border-radius:8px;color:inherit;padding:8px;font:inherit}.paged-transaction-filters button,.paged-load-more{border:1px solid var(--line,#384039);background:rgba(255,255,255,.08);color:inherit;border-radius:8px;padding:8px 11px;font:inherit}.paged-transactions-status,.paged-end{color:var(--muted,#a9b2ab);font-size:12px}.paged-transactions td small{display:block;color:var(--muted,#a9b2ab);margin-top:3px}.paged-transactions td.expense{color:#e39a87}.paged-transactions td.income{color:#7bd18b}.paged-load-more{margin-top:10px;width:100%}@media(max-width:760px){.paged-transaction-filters{grid-template-columns:1fr 1fr}.paged-transaction-filters label:nth-child(3),.paged-transaction-filters button{grid-column:1/-1}}'''

PYTHON_TEST = '''from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from finance_agent.finance import FinanceEngine
from finance_agent.storage import SQLiteStore, canonical_json
from finance_agent.api.pagination import TransactionCursor, TransactionPageService

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def add_rows(store: SQLiteStore, count: int = 240) -> None:
    digest = hashlib.sha256(b"pagination-source").hexdigest()
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO source_items(
                source_item_id, workspace_id, source_type, label, digest,
                mapping_version, received_at, status, row_count
            ) VALUES ('src_pagination_test', 'ws_koru_studio', 'csv',
                'Pagination test', ?, 'pagination_test@1',
                '2026-08-26T00:00:00+00:00', 'processed', ?)
            """,
            (digest, count),
        )
        for index in range(count):
            row_id = f"row_pagination_{index:04d}"
            transaction_id = f"txn_pagination_{index:04d}"
            evidence_id = f"evd_pagination_{index:04d}"
            occurred = f"2026-{(index % 12) + 1:02d}-{(index % 27) + 1:02d}"
            raw = canonical_json({"index": index})
            connection.execute(
                """
                INSERT INTO source_rows(
                    source_row_id, source_item_id, row_number, account_id,
                    occurred_on, description, amount_minor, currency, source_status,
                    external_reference, mapping_version, row_hash, raw_json
                ) VALUES (?, 'src_pagination_test', ?, 'acct_koru_business', ?, ?, ?,
                    'NZD', 'posted', ?, 'pagination_test@1', ?, ?)
                """,
                (
                    row_id, index + 1, occurred, f"Pagination merchant {index}",
                    -(index + 1), transaction_id,
                    hashlib.sha256(f"{digest}\\0{index + 1}\\0{raw}".encode()).hexdigest(), raw,
                ),
            )
            connection.execute(
                "INSERT INTO evidence_links(evidence_id, workspace_id, source_item_id, source_row_id, label, created_at) VALUES (?, 'ws_koru_studio', 'src_pagination_test', ?, ?, '2026-08-26T00:00:00+00:00')",
                (evidence_id, row_id, f"Pagination evidence {index}"),
            )
            connection.execute(
                """
                INSERT INTO transactions(
                    transaction_id, workspace_id, account_id, source_row_id, evidence_id,
                    occurred_on, description, amount_minor, currency, source_status, status,
                    classification, category, classification_source, rule_id,
                    duplicate_of_transaction_id, created_at, updated_at
                ) VALUES (?, 'ws_koru_studio', 'acct_koru_business', ?, ?, ?, ?, ?,
                    'NZD', 'posted', 'posted', 'business', 'test', 'deterministic',
                    NULL, NULL, '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00')
                """,
                (transaction_id, row_id, evidence_id, occurred, f"Pagination merchant {index}", -(index + 1)),
            )


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    add_rows(store)
    return store, TransactionPageService(store)


def test_keyset_pages_have_stable_non_overlapping_boundaries(tmp_path: Path) -> None:
    _store, service = setup(tmp_path)
    first = service.page(workspace_id="ws_koru_studio", limit=50)
    second = service.page(
        workspace_id="ws_koru_studio", limit=50, cursor=str(first["nextCursor"])
    )
    assert first["offsetUsed"] is False
    assert first["hasMore"] is True
    first_ids = {item["transactionId"] for item in first["items"]}
    second_ids = {item["transactionId"] for item in second["items"]}
    assert len(first_ids) == len(second_ids) == 50
    assert first_ids.isdisjoint(second_ids)
    assert first["items"][-1]["occurredOn"] >= second["items"][0]["occurredOn"]


def test_cursor_cannot_be_reused_with_other_filters(tmp_path: Path) -> None:
    _store, service = setup(tmp_path)
    first = service.page(
        workspace_id="ws_koru_studio", limit=10, classification="business"
    )
    with pytest.raises(ValueError, match="different filters"):
        service.page(
            workspace_id="ws_koru_studio",
            limit=10,
            classification="personal",
            cursor=str(first["nextCursor"]),
        )


def test_cursor_schema_and_limits_fail_closed() -> None:
    with pytest.raises(ValueError):
        TransactionCursor.decode("not-base64!", expected_query_hash="abc")
    with pytest.raises(ValueError, match="too long"):
        TransactionCursor.decode("a" * 513, expected_query_hash="abc")


def test_query_plan_uses_workspace_date_index_and_not_offset(tmp_path: Path) -> None:
    store, _service = setup(tmp_path)
    plan = store.fetch_all(
        "EXPLAIN QUERY PLAN SELECT transaction_id FROM transactions WHERE workspace_id = ? ORDER BY occurred_on DESC, transaction_id DESC LIMIT ?",
        ("ws_koru_studio", 51),
    )
    detail = " ".join(str(row["detail"]) for row in plan)
    assert "transactions_workspace_date_id_desc" in detail
    assert "OFFSET" not in detail.upper()
'''

NODE_TEST = '''import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const component = await readFile(new URL("../src/PaginatedTransactions.tsx", import.meta.url), "utf8");
const client = await readFile(new URL("../src/pagination.ts", import.meta.url), "utf8");

test("transaction history appends bounded pages instead of virtual offset state", () => {
  assert.match(component, /Load 50 more/);
  assert.match(component, /append \? \[\.\.\.current, \.\.\.page\.items\]/);
  assert.match(component, /role="status"/);
  assert.doesNotMatch(client, /offset/i);
});

test("pagination client carries filters and opaque cursor only", () => {
  assert.match(client, /URLSearchParams/);
  assert.match(client, /parameters\.set\("cursor", cursor\)/);
  assert.match(client, /limit: "50"/);
});
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
    write("services/api/src/finance_agent/api/pagination.py", MODULE)


def update_backend() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.api.working_understanding import WorkingUnderstandingRuntime\n"
    import_line = "from finance_agent.api.pagination import TransactionPageService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("working understanding import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "system_integrity_check", SERVICE_METHOD)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def system_integrity_check(self) -> Mapping[str, object]: ...\n"
    addition = '''    async def transaction_page(\n        self, *, workspace_id: str, limit: int, cursor: str | None,\n        classification: str, status: str, search: str | None,\n        date_from: str | None, date_to: str | None\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("integrity protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    marker = '    @router.get("/v1/system/trust-summary")\n'
    if marker not in content:
        raise RuntimeError("trust summary route marker missing")
    content = content.replace(marker, ROUTE + marker, 1)
    write(path, content)


def update_frontend_tests_docs() -> None:
    write("apps/desktop/src/pagination.ts", CLIENT)
    write("apps/desktop/src/PaginatedTransactions.tsx", COMPONENT)
    write("apps/desktop/src/pagination.css", CSS)
    path = "apps/desktop/src/OperationsWorkbench.tsx"
    content = read(path)
    if 'import { PaginatedTransactions } from "./PaginatedTransactions";' not in content:
        content = content.replace(
            'import "./operations.css";\n',
            'import "./operations.css";\nimport { PaginatedTransactions } from "./PaginatedTransactions";\n',
            1,
        )
    marker = '''                  <h2>Accounts and current sources</h2>
                  <div className="operations-columns">'''
    replacement = '''                  <PaginatedTransactions />
                  <h2>Accounts and current sources</h2>
                  <div className="operations-columns">'''
    if marker not in content:
        raise RuntimeError("operations overview insertion marker missing")
    content = content.replace(marker, replacement, 1)
    write(path, content)
    write("services/api/tests/api/test_transaction_pagination.py", PYTHON_TEST)
    write("apps/desktop/tests/pagination.test.mjs", NODE_TEST)
    package_path = ROOT / "package.json"
    package = json.loads(package_path.read_text())
    scripts = package.setdefault("scripts", {})
    scripts["test:pagination"] = "node --test apps/desktop/tests/pagination.test.mjs"
    verify = scripts.get("verify", "")
    if "pnpm test:pagination" not in verify:
        scripts["verify"] = verify + " && pnpm test:pagination"
    package_path.write_text(json.dumps(package, indent=2) + "\n")
    write("docs/PAGINATION.md", '''# Keyset transaction pagination\n\nThe transaction API orders rows by `occurred_on DESC, transaction_id DESC` and uses a bounded base64url JSON cursor containing that final key plus a hash of the active filters. Reusing a cursor with different classification, status, description or date filters fails. Limits are 1 to 200, search is capped at 100 characters and no SQL `OFFSET` is used.\n\nThe desktop requests 50 rows, retains the opaque next cursor and appends only after the owner chooses Load more. Filter changes restart from the first keyset page. Status text announces page results and the table remains a real keyboard and assistive-technology table.\n\nKeyset pagination bounds query work and prevents skipped or repeated rows caused by offset shifts. It does not freeze the dataset: new transactions may appear before the first page, so a fresh filter submission or refresh is required to see them.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 49: indexed keyset transaction pagination\n\n- Transaction pages use stable descending date and ID keys, never offset scans.\n- Opaque cursors carry a filter hash and fail when reused with changed filters.\n- Limits, cursor length, search length, dates and enums are bounded.\n- Index-plan and multi-page non-overlap tests cover large synthetic histories.\n- Desktop history loads 50 rows at a time with explicit accessible Load more.\n- New leading rows require refresh; pagination does not pretend to snapshot a changing dataset.\n'''
    if "## Stack 49: indexed keyset transaction pagination" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_backend()
    update_frontend_tests_docs()
    print("keyset pagination changes applied")


if __name__ == "__main__":
    main()
