from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from finance_agent.api.services import LocalRouteServices
from finance_agent.connectors.akahu import AkahuReadOnlyAdapter
from finance_agent.connectors.base import ConnectorError
from finance_agent.connectors.plaid import (
    PlaidAccount,
    PlaidConfig,
    PlaidReadOnlyAdapter,
    normalise_transactions,
)

ACCOUNT = PlaidAccount(
    provider_id="acc_provider",
    account_id="acct_local",
    label="Checking",
    currency="USD",
)


def _transaction(
    transaction_id: str,
    *,
    amount: float = 12.34,
    pending: object = False,
    name: str = "Client coffee",
) -> dict[str, object]:
    return {
        "transaction_id": transaction_id,
        "account_id": ACCOUNT.provider_id,
        "date": "2026-08-25",
        "name": name,
        "amount": amount,
        "iso_currency_code": "USD",
        "pending": pending,
    }


def test_plaid_pending_requires_a_real_boolean() -> None:
    with pytest.raises(ConnectorError, match="pending must be a boolean") as raised:
        normalise_transactions((_transaction("txn_bad", pending="false"),), (ACCOUNT,))
    assert raised.value.code == "provider_invalid_response"
    assert raised.value.retryable is False


@pytest.mark.asyncio
async def test_plaid_sync_page_preserves_added_modified_and_removed() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "added": [_transaction("txn_added")],
                "modified": [_transaction("txn_modified", amount=10.0)],
                "removed": [{"transaction_id": "txn_removed"}],
                "next_cursor": "cursor-next",
                "has_more": True,
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = PlaidReadOnlyAdapter(
        PlaidConfig(enabled=True, client_id="id", secret="secret"),
        client=client,
    )
    try:
        page = await adapter.sync_transactions(access_token="access")
        assert len(page.added) == 1
        assert len(page.modified) == 1
        assert len(page.removed) == 1
        assert page.next_cursor == "cursor-next"
        assert page.has_more is True
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_plaid_sync_rejects_non_boolean_has_more() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "added": [],
                "modified": [],
                "removed": [],
                "next_cursor": None,
                "has_more": "false",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = PlaidReadOnlyAdapter(
        PlaidConfig(enabled=True, client_id="id", secret="secret"),
        client=client,
    )
    try:
        with pytest.raises(ConnectorError) as raised:
            await adapter.sync_transactions(access_token="access")
        assert raised.value.code == "provider_invalid_response"
    finally:
        await client.aclose()


def test_akahu_page_rejects_malformed_items_instead_of_silently_dropping() -> None:
    with pytest.raises(ConnectorError, match="non-object item") as raised:
        AkahuReadOnlyAdapter._page(  # noqa: SLF001 - explicit provider-boundary test
            {"items": [{"_id": "ok"}, "malformed"], "cursor": {"next": None}}
        )
    assert raised.value.code == "provider_invalid_response"


class SequencePlaidAdapter:
    def __init__(self) -> None:
        self.config = SimpleNamespace(environment="sandbox")
        self.round = 0
        self.page_calls = 0

    async def aclose(self) -> None:
        return None

    def capability(self) -> dict[str, object]:
        return {
            "provider": "plaid",
            "configured": True,
            "mode": "read_only",
            "environment": "sandbox",
            "markets": ["US"],
        }

    async def resolve_access_token(self, public_token: str | None = None) -> str:
        del public_token
        self.round += 1
        self.page_calls = 0
        return "access"

    async def list_accounts(self, *, access_token: str) -> tuple[dict[str, object], ...]:
        assert access_token == "access"
        return (
            {
                "account_id": ACCOUNT.provider_id,
                "name": ACCOUNT.label,
                "balances": {"iso_currency_code": "USD"},
            },
        )

    async def sync_transactions(
        self,
        *,
        access_token: str,
        cursor: str | None = None,
        count: int = 100,
    ) -> SimpleNamespace:
        del count
        assert access_token == "access"
        self.page_calls += 1
        if self.round == 1:
            assert cursor is None
            return SimpleNamespace(
                added=(
                    _transaction("txn_coffee", amount=12.34),
                    _transaction("txn_fee", amount=0.29, name="Account fee"),
                ),
                modified=(),
                removed=(),
                next_cursor="round-one-done",
                has_more=False,
            )
        assert self.round == 2
        assert cursor is None
        return SimpleNamespace(
            added=(),
            modified=(
                _transaction(
                    "txn_coffee",
                    amount=10.00,
                    name="Client coffee corrected",
                ),
            ),
            removed=({"transaction_id": "txn_fee"},),
            next_cursor="round-two-done",
            has_more=False,
        )


@pytest.mark.asyncio
async def test_live_plaid_sync_appends_modified_and_removed_tombstones(tmp_path: Path) -> None:
    adapter = SequencePlaidAdapter()
    services = LocalRouteServices(
        tmp_path / "plaid-events.sqlite3",
        auto_seed=False,
        plaid_adapter=adapter,  # type: ignore[arg-type]
    )
    try:
        await services.reset_demo("ws_koru_studio")
        first = await services.sync_plaid()
        second = await services.sync_plaid()

        assert first["providerEventCount"] == 2
        assert second["providerEventCount"] == 2
        rows = services.store.fetch_all(
            """
            SELECT provider_transaction_id, event_type, amount_minor,
                   supersedes_event_id, payload_json
            FROM provider_transaction_events
            WHERE provider = 'plaid'
            ORDER BY recorded_at, event_id
            """
        )
        assert sorted(str(row["event_type"]) for row in rows) == [
            "modified",
            "quarantined",
            "quarantined",
            "removed",
        ]
        latest = {str(row["event_type"]): row for row in rows}
        assert int(latest["modified"]["amount_minor"]) == -1000
        assert latest["modified"]["supersedes_event_id"] is not None
        assert latest["removed"]["supersedes_event_id"] is not None
        assert services.store.fetch_all(
            "SELECT source_row_id FROM source_rows WHERE mapping_version = 'plaid_live@2'"
        ) == []
    finally:
        await services.aclose()


class RepeatingCursorPlaidAdapter(SequencePlaidAdapter):
    async def sync_transactions(
        self,
        *,
        access_token: str,
        cursor: str | None = None,
        count: int = 100,
    ) -> SimpleNamespace:
        del count
        assert access_token == "access"
        return SimpleNamespace(
            added=(),
            modified=(),
            removed=(),
            next_cursor="same-cursor",
            has_more=True,
        )


@pytest.mark.asyncio
async def test_live_plaid_sync_rejects_repeated_cursor(tmp_path: Path) -> None:
    services = LocalRouteServices(
        tmp_path / "plaid-repeat.sqlite3",
        auto_seed=False,
        plaid_adapter=RepeatingCursorPlaidAdapter(),  # type: ignore[arg-type]
    )
    try:
        await services.reset_demo("ws_koru_studio")
        with pytest.raises(ConnectorError, match="repeated a cursor") as raised:
            await services.sync_plaid()
        assert raised.value.code == "provider_cursor_repeated"
        assert raised.value.retryable is False
    finally:
        await services.aclose()


@pytest.mark.asyncio
async def test_provider_rate_limit_is_typed_without_response_body_leakage() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="private upstream body")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = PlaidReadOnlyAdapter(
        PlaidConfig(enabled=True, client_id="id", secret="secret"),
        client=client,
    )
    try:
        with pytest.raises(ConnectorError) as raised:
            await adapter.create_link_token()
        assert raised.value.code == "provider_rate_limited"
        assert raised.value.retryable is True
        assert raised.value.status_code == 503
        assert "private upstream body" not in str(raised.value)
    finally:
        await client.aclose()
