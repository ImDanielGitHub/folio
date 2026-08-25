"""Read-only Akahu live sync stays exact, bounded, and configuration-gated."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from finance_agent.api.routes import create_router
from finance_agent.api.services import AKAHU_MAPPING_VERSION, LocalRouteServices
from finance_agent.connectors.akahu import AkahuConfig, AkahuReadOnlyAdapter
from finance_agent.connectors.base import ConnectorError


def _configured_adapter(
    requests: list[httpx.Request],
) -> tuple[AkahuReadOnlyAdapter, httpx.AsyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer test-user-token"
        assert request.headers["X-Akahu-Id"] == "test-app-token"
        cursor = request.url.params.get("cursor")
        if request.url.path == "/v1/accounts":
            if cursor is None:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "items": [
                            {
                                "_id": "acc_anz_everyday",
                                "name": "ANZ Everyday",
                                "balance": {"currency": "NZD"},
                            }
                        ],
                        "cursor": {"next": "accounts-page-2"},
                    },
                )
            assert cursor == "accounts-page-2"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "items": [
                        {
                            "_id": "acc_anz_saver",
                            "name": "ANZ Business Saver",
                            "balance": {"currency": "NZD"},
                        }
                    ],
                },
            )
        if request.url.path == "/v1/transactions":
            assert request.url.params["start"] == "2026-06-30T23:59:59.999+12:00"
            assert request.url.params["end"] == "2026-07-21T23:59:59.999+12:00"
            if cursor is None:
                return httpx.Response(
                    200,
                    json={
                        "success": True,
                        "items": [
                            {
                                "_id": "txn_coffee",
                                "_account": "acc_anz_everyday",
                                "date": "2026-07-18T09:15:00+12:00",
                                "description": "Coffee client meeting",
                                "amount": "12.34",
                            }
                        ],
                        "cursor": {"next": "transactions-page-2"},
                    },
                )
            assert cursor == "transactions-page-2"
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "items": [
                        {
                            "_id": "txn_fee",
                            "_account": {"_id": "acc_anz_saver"},
                            "date": "2026-07-19",
                            "description": "Account fee adjustment",
                            "amount": "-0.29",
                            "currency": "NZD",
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected Akahu path: {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = AkahuReadOnlyAdapter(
        AkahuConfig(
            enabled=True,
            app_token="test-app-token",
            user_token="test-user-token",
        ),
        client=client,
    )
    return adapter, client


@pytest.mark.asyncio
async def test_live_sync_paginates_and_commits_exact_minor_units(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    adapter, client = _configured_adapter(requests)
    services = LocalRouteServices(
        tmp_path / "akahu-live.sqlite3",
        auto_seed=False,
        akahu_adapter=adapter,
    )
    try:
        await services.reset_demo("ws_koru_studio")

        capabilities = await services.connection_capabilities()
        assert capabilities["externalCallsMade"] is False
        assert capabilities["providers"]["akahu"]["status"] == "configured"  # type: ignore[index]
        assert requests == []

        result = await services.sync_akahu(start="2026-07-01", end="2026-07-21")
        assert result == {
            "sourceItemId": result["sourceItemId"],
            "status": "ingested",
            "sourceSha256": result["sourceSha256"],
            "accountCount": 2,
            "transactionCount": 2,
            "rowCount": 2,
            "window": {"start": "2026-07-01", "end": "2026-07-21"},
            "settledOnly": True,
            "liveSyncAttempted": True,
            "externalCallsMade": True,
        }
        assert len(requests) == 4

        rows = services.store.fetch_all(
            """
            SELECT amount_minor, source_status, external_reference
            FROM source_rows WHERE mapping_version = ?
            ORDER BY occurred_on, external_reference
            """,
            (AKAHU_MAPPING_VERSION,),
        )
        assert [int(row["amount_minor"]) for row in rows] == [1234, -29]
        assert [str(row["source_status"]) for row in rows] == ["posted", "posted"]
        source = services.store.fetch_one(
            """
            SELECT source_type, label, mapping_version, row_count
            FROM source_items WHERE source_item_id = ?
            """,
            (result["sourceItemId"],),
        )
        assert source is not None
        assert str(source["source_type"]) == "csv"
        assert str(source["mapping_version"]) == AKAHU_MAPPING_VERSION
        assert str(source["label"]).startswith("Akahu live settled transactions")
        assert int(source["row_count"]) == 2

        repeated = await services.sync_akahu(
            start="2026-07-01", end="2026-07-21"
        )
        assert repeated["status"] == "no_new_transactions"
        assert repeated["rowCount"] == 0
        assert repeated["liveSyncAttempted"] is True
        assert len(requests) == 8
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
    adapter = AkahuReadOnlyAdapter(AkahuConfig(enabled=False), client=client)
    services = LocalRouteServices(
        tmp_path / "akahu-disabled.sqlite3",
        auto_seed=False,
        akahu_adapter=adapter,
    )
    app = FastAPI()
    app.include_router(create_router())
    app.state.finance_route_services = services
    try:
        capabilities = await services.connection_capabilities()
        assert capabilities["providers"]["akahu"]["status"] == "unconfigured"  # type: ignore[index]
        assert capabilities["externalCallsMade"] is False
        assert call_count == 0

        with pytest.raises(ConnectorError, match="disabled or unconfigured"):
            await adapter.list_accounts()
        assert call_count == 0

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as route_client:
            response = await route_client.post(
                "/v1/connectors/akahu/sync",
                json={"start": "2026-07-01", "end": "2026-07-21"},
            )
        assert response.status_code == 409
        assert response.json()["detail"] == "Akahu is disabled or unconfigured"
        assert call_count == 0
    finally:
        await services.aclose()
        await client.aclose()


def test_config_repr_never_contains_tokens() -> None:
    config = AkahuConfig(
        enabled=True,
        app_token="private-app-token",
        user_token="private-user-token",
    )
    assert "private-app-token" not in repr(config)
    assert "private-user-token" not in repr(config)


@pytest.mark.parametrize(
    ("start", "end", "detail"),
    [
        ("2026-08-01", "2026-07-01", "start must be on or before end"),
        ("2025-01-01", "2026-07-21", "cannot exceed 366 days"),
    ],
)
@pytest.mark.asyncio
async def test_sync_window_is_bounded_before_network(
    tmp_path: Path,
    start: str,
    end: str,
    detail: str,
) -> None:
    requests: list[httpx.Request] = []
    adapter, client = _configured_adapter(requests)
    services = LocalRouteServices(
        tmp_path / f"bounded-{start}.sqlite3",
        auto_seed=False,
        akahu_adapter=adapter,
    )
    try:
        with pytest.raises(ValueError, match=detail):
            await services.sync_akahu(start=start, end=end)
        assert requests == []
    finally:
        await services.aclose()
        await client.aclose()
