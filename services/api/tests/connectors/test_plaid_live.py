"""Read-only Plaid sandbox sync stays exact, bounded, and configuration-gated."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from finance_agent.api.routes import create_router
from finance_agent.api.services import PLAID_MAPPING_VERSION, LocalRouteServices
from finance_agent.connectors.base import ConnectorError
from finance_agent.connectors.plaid import PlaidConfig, PlaidReadOnlyAdapter


def _configured_adapter(
    requests: list[httpx.Request],
) -> tuple[PlaidReadOnlyAdapter, httpx.AsyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        data = json.loads(request.content.decode())
        assert data["client_id"] == "test-client-id"
        assert data["secret"] == "test-secret"
        path = request.url.path
        if path == "/sandbox/public_token/create":
            return httpx.Response(
                200,
                json={"public_token": "public-sandbox-test-token"},
            )
        if path == "/item/public_token/exchange":
            assert data["public_token"] == "public-sandbox-test-token"
            return httpx.Response(
                200,
                json={
                    "access_token": "access-sandbox-test-token",
                    "item_id": "item-sandbox-1",
                },
            )
        if path == "/accounts/get":
            assert data["access_token"] == "access-sandbox-test-token"
            return httpx.Response(
                200,
                json={
                    "accounts": [
                        {
                            "account_id": "acc_chase_checking",
                            "name": "Plaid Checking",
                            "mask": "0000",
                            "balances": {"iso_currency_code": "USD"},
                        }
                    ]
                },
            )
        if path == "/transactions/sync":
            assert data["access_token"] == "access-sandbox-test-token"
            cursor = data.get("cursor")
            if cursor is None:
                return httpx.Response(
                    200,
                    json={
                        "added": [
                            {
                                "transaction_id": "txn_coffee",
                                "account_id": "acc_chase_checking",
                                "date": "2026-07-18",
                                "name": "Coffee client meeting",
                                "amount": 12.34,
                                "iso_currency_code": "USD",
                                "pending": False,
                            }
                        ],
                        "modified": [],
                        "removed": [],
                        "next_cursor": "cursor-2",
                        "has_more": True,
                    },
                )
            assert cursor == "cursor-2"
            return httpx.Response(
                200,
                json={
                    "added": [
                        {
                            "transaction_id": "txn_fee",
                            "account_id": "acc_chase_checking",
                            "date": "2026-07-19",
                            "name": "Account fee adjustment",
                            "amount": 0.29,
                            "iso_currency_code": "USD",
                            "pending": False,
                        }
                    ],
                    "modified": [],
                    "removed": [],
                    "next_cursor": "cursor-done",
                    "has_more": False,
                },
            )
        if path == "/link/token/create":
            return httpx.Response(
                200,
                json={"link_token": "link-sandbox-test-token", "expiration": "2099-01-01"},
            )
        raise AssertionError(f"unexpected Plaid path: {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = PlaidReadOnlyAdapter(
        PlaidConfig(
            enabled=True,
            client_id="test-client-id",
            secret="test-secret",
            environment="sandbox",
        ),
        client=client,
    )
    return adapter, client


@pytest.mark.asyncio
async def test_live_sync_paginates_and_quarantines_foreign_currency_minor_units(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []
    adapter, client = _configured_adapter(requests)
    services = LocalRouteServices(
        tmp_path / "plaid-live.sqlite3",
        auto_seed=False,
        plaid_adapter=adapter,
    )
    try:
        await services.reset_demo("ws_koru_studio")

        capabilities = await services.connection_capabilities()
        assert capabilities["externalCallsMade"] is False
        assert capabilities["providers"]["plaid"]["status"] == "configured"  # type: ignore[index]
        assert requests == []

        result = await services.sync_plaid()
        assert result["status"] == "quarantined_currency_mismatch"
        assert result["accountCount"] == 1
        assert result["transactionCount"] == 2
        assert result["rowCount"] == 0
        assert result["providerEventCount"] == 2
        assert result["providerCurrency"] == "USD"
        assert result["ledgerCommitted"] is False
        assert result["quarantineReason"] == "workspace_currency_mismatch"
        assert result["liveSyncAttempted"] is True
        assert result["externalCallsMade"] is True
        assert len(requests) == 5

        rows = services.store.fetch_all(
            """
            SELECT amount_minor, currency, event_type
            FROM provider_transaction_events
            WHERE provider = 'plaid'
            ORDER BY provider_transaction_id
            """
        )
        # Provider cents are preserved exactly but never relabelled as NZD ledger cents.
        assert [int(row["amount_minor"]) for row in rows] == [-1234, -29]
        assert [str(row["currency"]) for row in rows] == ["USD", "USD"]
        assert [str(row["event_type"]) for row in rows] == ["quarantined", "quarantined"]
        assert services.store.fetch_all(
            "SELECT source_row_id FROM source_rows WHERE mapping_version = ?",
            (PLAID_MAPPING_VERSION,),
        ) == []

        repeated = await services.sync_plaid()
        assert repeated["status"] == "no_new_transactions"
        assert repeated["rowCount"] == 0
        event_rows = services.store.fetch_all(
            "SELECT event_id FROM provider_transaction_events"
        )
        assert len(event_rows) == 2
    finally:
        await services.aclose()
        await client.aclose()


@pytest.mark.asyncio
async def test_unconfigured_sync_fails_closed_before_network(tmp_path: Path) -> None:
    call_count = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = PlaidReadOnlyAdapter(PlaidConfig(enabled=False), client=client)
    services = LocalRouteServices(
        tmp_path / "plaid-disabled.sqlite3",
        auto_seed=False,
        plaid_adapter=adapter,
    )
    app = FastAPI()
    app.include_router(create_router())
    app.state.finance_route_services = services
    try:
        capabilities = await services.connection_capabilities()
        assert capabilities["providers"]["plaid"]["status"] == "unconfigured"  # type: ignore[index]
        assert call_count == 0

        with pytest.raises(ConnectorError, match="disabled or unconfigured"):
            await adapter.create_link_token()
        assert call_count == 0

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as route_client:
            response = await route_client.post("/v1/connectors/plaid/sync", json={})
        assert response.status_code == 409
        assert response.json()["detail"] == {
            "code": "connector_unconfigured",
            "message": "Plaid is disabled or unconfigured",
            "retryable": False,
            "provider": "plaid",
        }
        assert call_count == 0
    finally:
        await services.aclose()
        await client.aclose()


def test_config_repr_never_contains_secrets() -> None:
    config = PlaidConfig(
        enabled=True,
        client_id="private-client-id",
        secret="private-secret",
        access_token="private-access-token",
    )
    rendered = repr(config)
    assert "private-client-id" not in rendered
    assert "private-secret" not in rendered
    assert "private-access-token" not in rendered


@pytest.mark.asyncio
async def test_link_token_route_when_configured(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    adapter, client = _configured_adapter(requests)
    services = LocalRouteServices(
        tmp_path / "plaid-link.sqlite3",
        auto_seed=False,
        plaid_adapter=adapter,
    )
    try:
        result = await services.create_plaid_link_token()
        assert result["linkToken"] == "link-sandbox-test-token"
        assert result["environment"] == "sandbox"
        assert any(request.url.path == "/link/token/create" for request in requests)
    finally:
        await services.aclose()
        await client.aclose()
