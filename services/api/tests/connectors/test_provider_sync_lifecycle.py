"""Provider synchronisation records corrections without rewriting source history."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from finance_agent.api.routes.router import connector_http_status
from finance_agent.api.services import LocalRouteServices
from finance_agent.connectors.akahu import AkahuReadOnlyAdapter
from finance_agent.connectors.base import ConnectorError, ConnectorErrorCode
from finance_agent.connectors.plaid import (
    PlaidConfig,
    PlaidReadOnlyAdapter,
    PlaidSyncPage,
    normalise_accounts,
    normalise_transactions,
)


def _account_payload() -> dict[str, object]:
    return {
        "account_id": "acc_provider_checking",
        "name": "Provider Checking",
        "balances": {"iso_currency_code": "USD"},
    }


def _transaction_payload(
    *,
    amount: float = 12.34,
    pending: object = False,
) -> dict[str, object]:
    return {
        "transaction_id": "txn_provider_coffee",
        "account_id": "acc_provider_checking",
        "date": "2026-08-25",
        "name": "Coffee client meeting",
        "amount": amount,
        "iso_currency_code": "USD",
        "pending": pending,
    }


def _adapter(handler: httpx.MockTransport) -> tuple[PlaidReadOnlyAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=handler)
    adapter = PlaidReadOnlyAdapter(
        PlaidConfig(
            enabled=True,
            client_id="test-client-id",
            secret="test-secret",
            access_token="access-test",
            environment="sandbox",
        ),
        client=client,
    )
    return adapter, client


def test_pending_must_be_a_real_boolean() -> None:
    accounts = normalise_accounts((_account_payload(),))

    with pytest.raises(ConnectorError) as error:
        normalise_transactions(
            (_transaction_payload(pending="false"),),
            accounts,
        )

    assert error.value.code is ConnectorErrorCode.INVALID_RESPONSE
    assert "pending" in str(error.value)


def test_akahu_page_rejects_non_object_items() -> None:
    with pytest.raises(ConnectorError) as error:
        AkahuReadOnlyAdapter._page(
            {"items": [{"_id": "valid"}, "silently-dropped-before"]}
        )

    assert error.value.code is ConnectorErrorCode.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_plaid_adapter_exposes_all_lifecycle_groups() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/transactions/sync"
        return httpx.Response(
            200,
            json={
                "added": [_transaction_payload()],
                "modified": [_transaction_payload(amount=15.00)],
                "removed": [{"transaction_id": "txn_removed"}],
                "next_cursor": "cursor-next",
                "has_more": False,
            },
        )

    adapter, client = _adapter(httpx.MockTransport(handler))
    try:
        page = await adapter.sync_transactions(access_token="access-test")
    finally:
        await client.aclose()

    assert isinstance(page, PlaidSyncPage)
    assert len(page.added) == 1
    assert len(page.modified) == 1
    assert [item.provider_id for item in page.removed] == ["txn_removed"]
    assert page.next_cursor == "cursor-next"
    assert page.has_more is False


@pytest.mark.asyncio
async def test_plaid_adapter_rejects_malformed_page_entries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/accounts/get":
            return httpx.Response(200, json={"accounts": [_account_payload(), "bad"]})
        raise AssertionError(request.url.path)

    adapter, client = _adapter(httpx.MockTransport(handler))
    try:
        with pytest.raises(ConnectorError) as error:
            await adapter.list_accounts(access_token="access-test")
    finally:
        await client.aclose()

    assert error.value.code is ConnectorErrorCode.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_repeated_plaid_cursor_fails_before_page_limit(tmp_path: Path) -> None:
    transaction_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal transaction_calls
        data = json.loads(request.content.decode())
        if request.url.path == "/accounts/get":
            return httpx.Response(200, json={"accounts": [_account_payload()]})
        if request.url.path == "/transactions/sync":
            transaction_calls += 1
            assert data["access_token"] == "access-test"
            return httpx.Response(
                200,
                json={
                    "added": [],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "cursor-repeat",
                    "has_more": True,
                },
            )
        raise AssertionError(request.url.path)

    adapter, client = _adapter(httpx.MockTransport(handler))
    services = LocalRouteServices(
        tmp_path / "repeated-cursor.sqlite3",
        auto_seed=False,
        plaid_adapter=adapter,
    )
    try:
        await services.reset_demo("ws_koru_studio")
        with pytest.raises(ConnectorError) as error:
            await services.sync_plaid()
    finally:
        await services.aclose()
        await client.aclose()

    assert error.value.code is ConnectorErrorCode.REPEATED_CURSOR
    assert transaction_calls == 2


@pytest.mark.asyncio
async def test_plaid_modification_and_removal_append_a_supersession_chain(
    tmp_path: Path,
) -> None:
    sync_number = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sync_number
        if request.url.path == "/accounts/get":
            return httpx.Response(200, json={"accounts": [_account_payload()]})
        if request.url.path == "/transactions/sync":
            sync_number += 1
            if sync_number == 1:
                lifecycle = {
                    "added": [_transaction_payload()],
                    "modified": [],
                    "removed": [],
                }
            elif sync_number == 2:
                lifecycle = {
                    "added": [],
                    "modified": [_transaction_payload(amount=15.00)],
                    "removed": [],
                }
            else:
                lifecycle = {
                    "added": [],
                    "modified": [],
                    "removed": [{"transaction_id": "txn_provider_coffee"}],
                }
            return httpx.Response(
                200,
                json={
                    **lifecycle,
                    "next_cursor": f"cursor-{sync_number}",
                    "has_more": False,
                },
            )
        raise AssertionError(request.url.path)

    adapter, client = _adapter(httpx.MockTransport(handler))
    services = LocalRouteServices(
        tmp_path / "provider-lifecycle.sqlite3",
        auto_seed=False,
        plaid_adapter=adapter,
    )
    try:
        await services.reset_demo("ws_koru_studio")
        first = await services.sync_plaid()
        second = await services.sync_plaid()
        third = await services.sync_plaid()
    finally:
        await services.aclose()
        await client.aclose()

    assert first["status"] == "quarantined_currency_mismatch"
    assert second["status"] == "provider_events_recorded"
    assert third["status"] == "provider_events_recorded"
    rows = services.store.fetch_all(
        """
        SELECT event_id, event_type, amount_minor, supersedes_event_id
        FROM provider_transaction_events
        WHERE provider = 'plaid'
        ORDER BY recorded_at, event_id
        """
    )
    assert [str(row["event_type"]) for row in rows] == [
        "quarantined",
        "modified",
        "removed",
    ]
    assert [row["amount_minor"] for row in rows] == [-1234, -1500, -1500]
    assert rows[0]["supersedes_event_id"] is None
    assert rows[1]["supersedes_event_id"] == rows[0]["event_id"]
    assert rows[2]["supersedes_event_id"] == rows[1]["event_id"]
    assert services.store.fetch_all(
        "SELECT source_row_id FROM source_rows WHERE mapping_version = 'plaid_live@2'"
    ) == []


def test_connector_status_mapping_uses_error_code_not_message_text() -> None:
    error = ConnectorError(
        "wording can change without changing HTTP semantics",
        code=ConnectorErrorCode.UNCONFIGURED,
    )

    assert connector_http_status(error) == 409
