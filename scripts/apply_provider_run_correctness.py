from __future__ import annotations

import ast
import hashlib
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


def replace_once(path: str, old: str, new: str) -> None:
    content = read(path)
    count = content.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:120]!r}")
    write(path, content.replace(old, new, 1))


def insert_once(path: str, marker: str, addition: str, *, before: bool = False) -> None:
    content = read(path)
    count = content.count(marker)
    if count != 1:
        raise RuntimeError(f"{path}: expected one marker, found {count}: {marker[:120]!r}")
    replacement = addition + marker if before else marker + addition
    write(path, content.replace(marker, replacement, 1))


def replace_function(path: str, name: str, replacement: str, *, class_name: str | None = None) -> None:
    content = read(path)
    tree = ast.parse(content)
    candidate: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    if class_name is None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                candidate = node
                break
    else:
        owner = next(
            (
                node
                for node in tree.body
                if isinstance(node, ast.ClassDef) and node.name == class_name
            ),
            None,
        )
        if owner is not None:
            candidate = next(
                (
                    node
                    for node in owner.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == name
                ),
                None,
            )
    if candidate is None or candidate.end_lineno is None:
        raise RuntimeError(f"{path}: could not find {class_name or '<module>'}.{name}")
    lines = content.splitlines(keepends=True)
    start = candidate.lineno - 1
    end = candidate.end_lineno
    if start > 0:
        while start > 0 and lines[start - 1].lstrip().startswith("@"):
            start -= 1
    replacement = replacement.rstrip() + "\n"
    write(path, "".join(lines[:start]) + replacement + "".join(lines[end:]))


PLAID_PAGE = '''

@dataclass(frozen=True, slots=True)
class PlaidSyncPage:
    """One lossless /transactions/sync page.

    Iteration preserves the former three-tuple contract for downstream code that
    has not yet adopted the explicit added/modified/removed attributes.
    """

    added: tuple[Mapping[str, object], ...]
    modified: tuple[Mapping[str, object], ...]
    removed: tuple[Mapping[str, object], ...]
    next_cursor: str | None
    has_more: bool

    def __iter__(self):
        yield self.added
        yield self.next_cursor
        yield self.has_more
'''

PLAID_SYNC_METHOD = '''    async def sync_transactions(
        self,
        *,
        access_token: str,
        cursor: str | None = None,
        count: int = 100,
    ) -> PlaidSyncPage:
        body: dict[str, object] = {"access_token": access_token, "count": count}
        if cursor:
            body["cursor"] = cursor
        response = await self._post("/transactions/sync", body)

        def records(field: str) -> tuple[Mapping[str, object], ...]:
            value = response.get(field)
            if not isinstance(value, list):
                raise ConnectorError(
                    f"Plaid transactions sync response omitted {field} records"
                )
            return tuple(item for item in value if isinstance(item, Mapping))

        next_cursor = response.get("next_cursor")
        has_more = bool(response.get("has_more"))
        parsed_cursor = (
            str(next_cursor)
            if isinstance(next_cursor, str) and next_cursor.strip()
            else None
        )
        if has_more and parsed_cursor is None:
            raise ConnectorError(
                "Plaid transactions sync reported more pages without a cursor"
            )
        return PlaidSyncPage(
            added=records("added"),
            modified=records("modified"),
            removed=records("removed"),
            next_cursor=parsed_cursor,
            has_more=has_more,
        )
'''

PROVIDER_EVENTS = '''"""Lossless provider change streams kept outside the NZD ledger."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json

PROVIDER_SYNC_MAPPING_VERSION = "provider_change_stream@1"


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


@dataclass(frozen=True, slots=True)
class ProviderSyncResult:
    source_item_id: str | None
    status: str
    added: int
    modified: int
    removed: int
    current: int
    digest: str | None


def _required_text(value: Mapping[str, object], key: str) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        raise ValueError(f"provider mutation requires {key}")
    return candidate.strip()


def record_provider_sync(
    store: SQLiteStore,
    *,
    workspace_id: str,
    provider: str,
    account_labels: Mapping[str, str],
    mutations: Sequence[Mapping[str, object]],
    received_at: str,
) -> ProviderSyncResult:
    if not mutations:
        current = len(current_provider_transactions(store, workspace_id, provider))
        return ProviderSyncResult(None, "no_changes", 0, 0, 0, current, None)

    canonical_mutations = [dict(value) for value in mutations]
    digest = hashlib.sha256(canonical_json(canonical_mutations).encode()).hexdigest()
    source_item_id = _stable_id("src", workspace_id, provider, digest)
    counts = {"added": 0, "modified": 0, "removed": 0}

    with store.transaction() as connection:
        existing = connection.execute(
            """
            SELECT source_item_id FROM source_items
            WHERE workspace_id = ? AND digest = ? AND mapping_version = ?
            """,
            (workspace_id, digest, PROVIDER_SYNC_MAPPING_VERSION),
        ).fetchone()
        if existing is not None:
            current = len(current_provider_transactions(store, workspace_id, provider))
            return ProviderSyncResult(
                str(existing["source_item_id"]),
                "deduplicated",
                0,
                0,
                0,
                current,
                digest,
            )

        connection.execute(
            """
            INSERT INTO source_items(
                source_item_id, workspace_id, source_type, label, digest,
                mapping_version, received_at, status, row_count
            ) VALUES (?, ?, 'plaid_fixture', ?, ?, ?, ?, 'processed', ?)
            """,
            (
                source_item_id,
                workspace_id,
                f"{provider.title()} transaction change stream",
                digest,
                PROVIDER_SYNC_MAPPING_VERSION,
                received_at,
                len(canonical_mutations),
            ),
        )
        evidence_id = _stable_id("evd", source_item_id, "change_stream")
        connection.execute(
            """
            INSERT INTO evidence_links(
                evidence_id, workspace_id, source_item_id, source_row_id, label, created_at
            ) VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (
                evidence_id,
                workspace_id,
                source_item_id,
                f"{provider.title()} transaction change stream",
                received_at,
            ),
        )

        for index, mutation in enumerate(canonical_mutations):
            event_type = _required_text(mutation, "eventType")
            if event_type not in counts:
                raise ValueError(f"unsupported provider event type: {event_type}")
            provider_transaction_id = _required_text(
                mutation, "providerTransactionId"
            )
            provider_account_id_value = mutation.get("providerAccountId")
            provider_account_id = (
                provider_account_id_value.strip()
                if isinstance(provider_account_id_value, str)
                and provider_account_id_value.strip()
                else None
            )
            previous = connection.execute(
                """
                SELECT * FROM provider_transaction_events
                WHERE workspace_id = ? AND provider = ?
                  AND provider_transaction_id = ?
                ORDER BY recorded_at DESC, event_id DESC LIMIT 1
                """,
                (workspace_id, provider, provider_transaction_id),
            ).fetchone()
            if provider_account_id is None and previous is not None:
                provider_account_id = str(previous["provider_account_id"])
            if provider_account_id is None:
                raise ValueError(
                    "removed provider transaction has no account and no prior state"
                )
            previous_event_id = str(previous["event_id"]) if previous else None
            event_id = _stable_id(
                "prevt",
                workspace_id,
                provider,
                digest,
                str(index),
                event_type,
                provider_transaction_id,
            )
            description = mutation.get("description")
            if not isinstance(description, str) or not description.strip():
                description = (
                    str(previous["description"])
                    if previous is not None
                    else f"{provider.title()} transaction"
                )
            amount = mutation.get("amountMinor")
            amount_minor = (
                int(amount)
                if isinstance(amount, int) and not isinstance(amount, bool)
                else (
                    int(previous["amount_minor"])
                    if previous is not None and previous["amount_minor"] is not None
                    else None
                )
            )
            currency = mutation.get("currency")
            canonical_currency = (
                currency.strip().upper()
                if isinstance(currency, str) and currency.strip()
                else (
                    str(previous["currency"])
                    if previous is not None
                    else "USD"
                )
            )
            occurred_on = mutation.get("occurredOn")
            canonical_date = (
                occurred_on.strip()
                if isinstance(occurred_on, str) and occurred_on.strip()
                else (
                    str(previous["occurred_on"])
                    if previous is not None and previous["occurred_on"]
                    else None
                )
            )
            payload = {
                **mutation,
                "accountLabel": account_labels.get(provider_account_id),
                "ledgerCommitted": False,
                "quarantineReason": "workspace_currency_mismatch",
            }
            connection.execute(
                """
                INSERT INTO provider_transaction_events(
                    event_id, workspace_id, provider, provider_account_id,
                    provider_transaction_id, source_item_id, event_type,
                    occurred_on, description, amount_minor, currency,
                    payload_json, supersedes_event_id, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    workspace_id,
                    provider,
                    provider_account_id,
                    provider_transaction_id,
                    source_item_id,
                    event_type,
                    canonical_date,
                    description.strip(),
                    amount_minor,
                    canonical_currency,
                    canonical_json(payload),
                    previous_event_id,
                    received_at,
                ),
            )
            counts[event_type] += 1

    current = len(current_provider_transactions(store, workspace_id, provider))
    return ProviderSyncResult(
        source_item_id,
        "recorded",
        counts["added"],
        counts["modified"],
        counts["removed"],
        current,
        digest,
    )


def current_provider_transactions(
    store: SQLiteStore,
    workspace_id: str,
    provider: str,
) -> list[dict[str, Any]]:
    rows = store.fetch_all(
        """
        WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY provider_account_id, provider_transaction_id
                ORDER BY recorded_at DESC, event_id DESC
            ) AS position
            FROM provider_transaction_events
            WHERE workspace_id = ? AND provider = ?
        )
        SELECT * FROM ranked
        WHERE position = 1 AND event_type != 'removed'
        ORDER BY occurred_on, provider_transaction_id
        """,
        (workspace_id, provider),
    )
    return [dict(row) for row in rows]
'''

MATERIAL_STATE = '''"""Canonical material-state manifest used for Daily Close identity."""

from __future__ import annotations

from typing import Any

from finance_agent.storage import SQLiteStore


def _rows(store: SQLiteStore, sql: str, workspace_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in store.fetch_all(sql, (workspace_id,))]


def material_state_manifest(
    store: SQLiteStore,
    *,
    workspace_id: str,
    fallback_policy_version: str,
) -> dict[str, object]:
    workspace = store.fetch_one(
        """
        SELECT protected_reserve_minor, currency, timezone, data_through,
               state_revision
        FROM workspaces WHERE workspace_id = ?
        """,
        (workspace_id,),
    )
    definition = store.fetch_one(
        """
        SELECT policy_version, enabled FROM job_definitions
        WHERE workspace_id = ? AND job_type = 'daily_close'
        """,
        (workspace_id,),
    )
    return {
        "policyVersion": (
            str(definition["policy_version"])
            if definition is not None
            else fallback_policy_version
        ),
        "jobEnabled": bool(definition["enabled"]) if definition is not None else True,
        "workspace": dict(workspace) if workspace is not None else None,
        "sources": _rows(
            store,
            """
            SELECT source_item_id, source_type, digest, mapping_version, status, row_count
            FROM source_items WHERE workspace_id = ? ORDER BY source_item_id
            """,
            workspace_id,
        ),
        "rules": _rows(
            store,
            """
            SELECT rule_id, merchant_contains, maximum_amount_minor, currency,
                   target_classification, target_category, effective_from,
                   priority, active, updated_at
            FROM classification_rules
            WHERE workspace_id = ? ORDER BY rule_id
            """,
            workspace_id,
        ),
        "claims": _rows(
            store,
            """
            SELECT claim_id, claim_type, statement, scope_json, effective_date,
                   status, supersedes_claim_id, recorded_at
            FROM claims WHERE workspace_id = ? ORDER BY claim_id
            """,
            workspace_id,
        ),
        "financeEvents": _rows(
            store,
            """
            SELECT event_id, event_type, occurred_at, scope_json,
                   undone_by_event_id, redone_by_event_id
            FROM finance_events WHERE workspace_id = ? ORDER BY event_id
            """,
            workspace_id,
        ),
        "providerEvents": _rows(
            store,
            """
            SELECT event_id, provider, provider_account_id,
                   provider_transaction_id, event_type, amount_minor,
                   currency, supersedes_event_id, recorded_at
            FROM provider_transaction_events
            WHERE workspace_id = ? ORDER BY event_id
            """,
            workspace_id,
        ),
    }
'''

SYNC_PLAID = '''    async def sync_plaid(
        self,
        *,
        public_token: str | None = None,
    ) -> Mapping[str, object]:
        """Persist Plaid's full change stream without relabelling USD as NZD."""

        access_token = await self.plaid.resolve_access_token(public_token)
        account_items = await self.plaid.list_accounts(access_token=access_token)
        accounts = normalise_plaid_accounts(account_items)
        if not accounts:
            raise ConnectorError("Plaid returned no accounts")
        account_by_provider_id = {account.provider_id: account for account in accounts}

        added_items: list[Mapping[str, object]] = []
        modified_items: list[Mapping[str, object]] = []
        removed_items: list[Mapping[str, object]] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        for _ in range(PLAID_MAX_PAGES):
            page = await self.plaid.sync_transactions(
                access_token=access_token,
                cursor=cursor,
            )
            added_items.extend(page.added)
            modified_items.extend(page.modified)
            removed_items.extend(page.removed)
            item_count = len(added_items) + len(modified_items) + len(removed_items)
            if item_count > PLAID_MAX_ITEMS:
                raise ConnectorError("Plaid transaction sync exceeded the local item limit")
            if not page.has_more:
                break
            if page.next_cursor is None:
                raise ConnectorError("Plaid pagination omitted the next cursor")
            if page.next_cursor in seen_cursors:
                raise ConnectorError("Plaid transaction pagination repeated a cursor")
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor
        else:
            raise ConnectorError("Plaid transaction sync exceeded the page limit")

        added = normalise_plaid_transactions(tuple(added_items), accounts)
        modified = normalise_plaid_transactions(tuple(modified_items), accounts)
        mutations: list[dict[str, object]] = []
        for event_type, transactions in (("added", added), ("modified", modified)):
            for transaction in transactions:
                mutations.append(
                    {
                        "eventType": event_type,
                        "providerAccountId": transaction.account_id,
                        "providerTransactionId": transaction.provider_id,
                        "occurredOn": transaction.occurred_on,
                        "description": transaction.description,
                        "amountMinor": transaction.amount_minor,
                        "currency": transaction.currency,
                        "externalReference": transaction.external_reference,
                    }
                )
        for item in removed_items:
            transaction_id = item.get("transaction_id") or item.get("id")
            if not isinstance(transaction_id, str) or not transaction_id.strip():
                raise ConnectorError("Plaid removed transaction omitted transaction_id")
            raw_account_id = item.get("account_id")
            account = (
                account_by_provider_id.get(raw_account_id)
                if isinstance(raw_account_id, str)
                else None
            )
            mutations.append(
                {
                    "eventType": "removed",
                    "providerAccountId": account.account_id if account else None,
                    "providerTransactionId": transaction_id.strip(),
                    "currency": account.currency if account else "USD",
                }
            )

        synced_at = _now().isoformat()
        async with self._lock:
            result = record_provider_sync(
                self.store,
                workspace_id=WORKSPACE_ID,
                provider="plaid",
                account_labels={account.account_id: account.label for account in accounts},
                mutations=mutations,
                received_at=synced_at,
            )
            self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)
        return {
            "sourceItemId": result.source_item_id,
            "status": result.status,
            "sourceSha256": result.digest,
            "accountCount": len(accounts),
            "addedCount": result.added,
            "modifiedCount": result.modified,
            "removedCount": result.removed,
            "currentProviderTransactionCount": result.current,
            "rowCount": 0,
            "providerEventCount": result.added + result.modified + result.removed,
            "providerCurrency": "USD",
            "ledgerCommitted": False,
            "quarantineReason": "workspace_currency_mismatch",
            "settledOnly": True,
            "liveSyncAttempted": True,
            "externalCallsMade": True,
        }
'''

DAILY_INPUT_HASH = '''    def _input_hash(self) -> str:
        payload = material_state_manifest(
            self.engine.store,
            workspace_id=WORKSPACE_ID,
            fallback_policy_version=POLICY_VERSION,
        )
        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()
'''

HEALTH_ROUTE = '''    @router.get("/health")
    async def health(request: Request, services: Services) -> dict[str, object]:
        result = dict(await services.health())
        result["runtime"] = {
            "developmentRoutes": bool(
                getattr(request.app.state, "development_routes", False)
            ),
            "sessionAuthRequired": bool(
                getattr(request.app.state, "session_auth_required", False)
            ),
        }
        return result
'''

TESTS = '''from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from finance_agent.api.app import create_app
from finance_agent.connectors.plaid import PlaidConfig, PlaidReadOnlyAdapter
from finance_agent.finance import FinanceEngine
from finance_agent.finance.provider_events import (
    current_provider_transactions,
    record_provider_sync,
)
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


@pytest.mark.asyncio
async def test_plaid_sync_page_preserves_added_modified_and_removed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transactions/sync"
        return httpx.Response(
            200,
            json={
                "added": [{"transaction_id": "txn_added"}],
                "modified": [{"transaction_id": "txn_modified"}],
                "removed": [{"transaction_id": "txn_removed"}],
                "next_cursor": "cursor_2",
                "has_more": True,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = PlaidReadOnlyAdapter(
        PlaidConfig(enabled=True, client_id="client", secret="secret"),
        client=client,
    )
    page = await adapter.sync_transactions(access_token="access")
    assert [item["transaction_id"] for item in page.added] == ["txn_added"]
    assert [item["transaction_id"] for item in page.modified] == ["txn_modified"]
    assert [item["transaction_id"] for item in page.removed] == ["txn_removed"]
    assert page.next_cursor == "cursor_2"
    assert page.has_more is True
    legacy_added, legacy_cursor, legacy_more = page
    assert legacy_added == page.added
    assert legacy_cursor == page.next_cursor
    assert legacy_more is True
    await client.aclose()


def test_provider_change_stream_supersedes_and_removes_current_state(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    first = record_provider_sync(
        store,
        workspace_id="ws_koru_studio",
        provider="plaid",
        account_labels={"acct_provider": "Plaid checking"},
        received_at="2026-08-26T08:00:00+00:00",
        mutations=[
            {
                "eventType": "added",
                "providerAccountId": "acct_provider",
                "providerTransactionId": "provider_txn_1",
                "occurredOn": "2026-08-25",
                "description": "Original",
                "amountMinor": -1000,
                "currency": "USD",
            }
        ],
    )
    assert first.added == 1
    second = record_provider_sync(
        store,
        workspace_id="ws_koru_studio",
        provider="plaid",
        account_labels={"acct_provider": "Plaid checking"},
        received_at="2026-08-26T08:01:00+00:00",
        mutations=[
            {
                "eventType": "modified",
                "providerAccountId": "acct_provider",
                "providerTransactionId": "provider_txn_1",
                "occurredOn": "2026-08-25",
                "description": "Corrected",
                "amountMinor": -1250,
                "currency": "USD",
            }
        ],
    )
    assert second.modified == 1
    current = current_provider_transactions(store, "ws_koru_studio", "plaid")
    assert len(current) == 1
    assert current[0]["description"] == "Corrected"
    assert current[0]["amount_minor"] == -1250
    assert current[0]["supersedes_event_id"] is not None

    removed = record_provider_sync(
        store,
        workspace_id="ws_koru_studio",
        provider="plaid",
        account_labels={"acct_provider": "Plaid checking"},
        received_at="2026-08-26T08:02:00+00:00",
        mutations=[
            {
                "eventType": "removed",
                "providerTransactionId": "provider_txn_1",
            }
        ],
    )
    assert removed.removed == 1
    assert current_provider_transactions(store, "ws_koru_studio", "plaid") == []
    assert len(store.fetch_all("SELECT * FROM transactions")) == 10


def test_daily_close_identity_tracks_claims_events_and_workspace_policy(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    service = DailyCloseService(engine)
    initial = service.identity().input_hash
    store.record_turn(
        turn_id="turn_material_claim",
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        role="owner",
        content="Keep a higher reserve.",
        occurred_at="2026-08-26T09:00:00+00:00",
    )
    store.record_claim(
        {
            "claimId": "claim_material_reserve",
            "workspaceId": "ws_koru_studio",
            "claimType": "reserve_policy",
            "statement": "Keep a higher reserve.",
            "sourceTurnId": "turn_material_claim",
            "scope": {},
            "effectiveDate": "2026-08-26",
            "recordedAt": "2026-08-26T09:00:00+00:00",
        }
    )
    after_claim = service.identity().input_hash
    assert after_claim != initial
    with store.transaction() as connection:
        connection.execute(
            """
            UPDATE workspaces SET protected_reserve_minor = ?, state_revision = state_revision + 1
            WHERE workspace_id = ?
            """,
            (250000, "ws_koru_studio"),
        )
    assert service.identity().input_hash != after_claim


@pytest.mark.asyncio
async def test_production_mode_hides_demo_fixture_diagnostics_and_docs(tmp_path: Path) -> None:
    app = create_app(
        database_path=tmp_path / "folio.sqlite3",
        development_routes=False,
        session_token=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.post("/v1/demo/reset", json={})).status_code == 404
        assert (
            await client.post("/v1/ingest/plaid-fixture", json={})
        ).status_code == 404
        assert (
            await client.get(
                "/v1/diagnostics/working-understanding",
                params={"workspaceId": "ws_koru_studio"},
            )
        ).status_code == 404
        health = await client.get("/health")
    assert health.status_code == 200
    assert health.json()["runtime"] == {
        "developmentRoutes": False,
        "sessionAuthRequired": False,
    }


@pytest.mark.asyncio
async def test_turn_submission_reports_its_actual_synchronous_semantics(tmp_path: Path) -> None:
    app = create_app(
        database_path=tmp_path / "folio.sqlite3",
        development_routes=True,
        session_token=None,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=30
    ) as client:
        response = await client.post(
            "/v1/threads/thr_koru_studio_main/turns",
            json={
                "workspaceId": "ws_koru_studio",
                "turnId": "turn_sync_semantics",
                "content": "Show me the current finance summary.",
                "mode": "local",
            },
        )
    assert response.status_code == 200
    assert response.json()["status"] in {"completed", "question"}
'''


def update_plaid_adapter() -> None:
    path = "services/api/src/finance_agent/connectors/plaid.py"
    insert_once(path, "\n\nclass PlaidReadOnlyAdapter:", PLAID_PAGE, before=True)
    replace_function(
        path,
        "sync_transactions",
        PLAID_SYNC_METHOD,
        class_name="PlaidReadOnlyAdapter",
    )


def add_provider_events() -> None:
    write("services/api/src/finance_agent/finance/provider_events.py", PROVIDER_EVENTS)
    path = "services/api/src/finance_agent/api/services.py"
    marker = "from finance_agent.finance import FinanceEngine, FinanceStateError, FinanceTotals\n"
    insert_once(
        path,
        marker,
        "from finance_agent.finance.provider_events import record_provider_sync\n",
    )
    replace_function(path, "sync_plaid", SYNC_PLAID, class_name="LocalRouteServices")


def expand_daily_close_identity() -> None:
    write("services/api/src/finance_agent/finance/material_state.py", MATERIAL_STATE)
    path = "services/api/src/finance_agent/jobs/daily_close.py"
    marker = "from finance_agent.storage import canonical_json\n"
    insert_once(
        path,
        marker,
        "from finance_agent.finance.material_state import material_state_manifest\n",
    )
    replace_function(path, "_input_hash", DAILY_INPUT_HASH, class_name="DailyCloseService")


def add_development_route_gate() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    addition = '''\n\ndef require_development_routes(request: Request) -> None:\n    if not bool(getattr(request.app.state, "development_routes", False)):\n        raise HTTPException(status_code=404, detail="Not found")\n'''
    if "def require_development_routes" not in read(path):
        write(path, read(path).rstrip() + addition + "\n")

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    if "    Request," not in content:
        content = content.replace("    Query,\n", "    Query,\n    Request,\n", 1)
    content = content.replace(
        "from finance_agent.api.routes.dependencies import RouteServices, get_route_services",
        "from finance_agent.api.routes.dependencies import (\n    RouteServices,\n    get_route_services,\n    require_development_routes,\n)",
        1,
    )
    decorator_paths = (
        "/v1/demo/reset",
        "/v1/ingest/telegram-fixture",
        "/v1/ingest/akahu-fixture",
        "/v1/ingest/plaid-fixture",
    )
    for route in decorator_paths:
        old = f'@router.post("{route}")'
        new = (
            f'@router.post("{route}", '
            "dependencies=[Depends(require_development_routes)])"
        )
        if old not in content:
            raise RuntimeError(f"missing development route decorator: {old}")
        content = content.replace(old, new, 1)
    old = '@router.get("/v1/diagnostics/working-understanding")'
    new = (
        '@router.get("/v1/diagnostics/working-understanding", '
        'dependencies=[Depends(require_development_routes)])'
    )
    if old not in content:
        raise RuntimeError("missing diagnostics route decorator")
    content = content.replace(old, new, 1)
    write(path, content)
    replace_function(path, "health", HEALTH_ROUTE)
    replace_once(
        path,
        '@router.post("/v1/threads/{thread_id}/turns", status_code=202)',
        '@router.post("/v1/threads/{thread_id}/turns")',
    )

    path = "services/api/src/finance_agent/api/app.py"
    content = read(path)
    tree = ast.parse(content)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "create_app"
    )
    lines = content.splitlines(keepends=True)
    header_end = function.body[0].lineno - 1
    header = "".join(lines[function.lineno - 1 : header_end])
    if "development_routes" not in header:
        closing = header.rfind(") -> FastAPI:")
        if closing < 0:
            raise RuntimeError("could not extend create_app signature")
        header = (
            header[:closing]
            + "    development_routes: bool | None = None,\n"
            + header[closing:]
        )
        content = "".join(lines[: function.lineno - 1]) + header + "".join(lines[header_end:])
    if "resolved_development_routes" not in content:
        content = content.replace(
            "    services = LocalRouteServices(",
            "    resolved_development_routes = (\n"
            "        development_routes\n"
            "        if development_routes is not None\n"
            "        else os.getenv(\"FOLIO_ENV\", \"development\").lower() != \"production\"\n"
            "    )\n"
            "    services = LocalRouteServices(",
            1,
        )
    content = content.replace('docs_url="/docs",', 'docs_url="/docs" if resolved_development_routes else None,', 1)
    if "value.state.development_routes" not in content:
        content = content.replace(
            "    value.state.finance_route_services = services\n",
            "    value.state.finance_route_services = services\n"
            "    value.state.development_routes = resolved_development_routes\n"
            "    value.state.session_auth_required = bool(\n"
            "        session_token if session_token is not None else os.getenv(\"FOLIO_SESSION_TOKEN\")\n"
            "    )\n",
            1,
        )
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/integration/test_provider_run_correctness.py", TESTS)
    path = ".env.example"
    content = read(path)
    if "FOLIO_ENV=" not in content:
        content = content.replace(
            "# The API must remain loopback-only in P0.\n",
            "# development exposes sealed demo, fixture, diagnostics and API docs routes.\n"
            "# production returns 404 for those routes.\n"
            "FOLIO_ENV=development\n\n"
            "# The API must remain loopback-only in P0.\n",
            1,
        )
        write(path, content)
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 2: provider and run correctness\n\n- Plaid `/transactions/sync` now retains added, modified and removed records.\n- Provider events form a superseding timeline and removed records leave the current projection.\n- Foreign-currency provider state remains outside the NZD ledger.\n- Daily Close identity includes claims, finance events, provider events, job policy and workspace revision.\n- Production mode hides demo reset, fixture ingestion, diagnostics and API docs.\n- The turn endpoint now reports its actual synchronous HTTP semantics.\n'''
    if "## Stack 2: provider and run correctness" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    update_plaid_adapter()
    add_provider_events()
    expand_daily_close_identity()
    add_development_route_gate()
    add_tests_and_docs()
    print("provider and run correctness transformations applied")


if __name__ == "__main__":
    main()
