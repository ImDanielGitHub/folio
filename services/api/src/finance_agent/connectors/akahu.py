"""Configuration-gated, read-only Akahu provider seam.

This adapter intentionally stops at typed pages. Task 1 owns normalisation,
exact-money conversion, source commits, cursors and persistence. No Akahu call is
part of the P0 golden path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from finance_agent.connectors.base import ConnectorError


@dataclass(frozen=True, slots=True)
class AkahuConfig:
    enabled: bool = False
    app_token: str | None = field(default=None, repr=False)
    user_token: str | None = field(default=None, repr=False)
    base_url: str = "https://api.akahu.io/v1"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or parsed.hostname != "api.akahu.io":
            raise ValueError("Akahu credentials may only be sent to https://api.akahu.io")
        if self.enabled and not (self.app_token and self.user_token):
            raise ValueError("enabled Akahu requires both app and user tokens")

    @classmethod
    def from_env(cls) -> AkahuConfig:
        return cls(
            enabled=os.getenv("FINANCE_AKAHU_ENABLED", "false").lower() == "true",
            app_token=os.getenv("AKAHU_APP_TOKEN") or None,
            user_token=os.getenv("AKAHU_USER_TOKEN") or None,
        )


@dataclass(frozen=True, slots=True)
class ProviderPage:
    items: tuple[Mapping[str, object], ...]
    next_cursor: str | None


class AkahuReadOnlyAdapter:
    def __init__(
        self,
        config: AkahuConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or AkahuConfig.from_env()
        headers = {"Accept": "application/json"}
        if self.config.user_token and self.config.app_token:
            headers.update(
                {
                    "Authorization": f"Bearer {self.config.user_token}",
                    "X-Akahu-Id": self.config.app_token,
                }
            )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
            headers=headers,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def capability(self) -> dict[str, object]:
        return {
            "provider": "akahu",
            "configured": bool(
                self.config.enabled and self.config.app_token and self.config.user_token
            ),
            "mode": "read_only",
            "goldenPathDependency": False,
        }

    async def _get(
        self, path: str, *, params: Mapping[str, str] | None = None
    ) -> Mapping[str, object]:
        if not (self.config.enabled and self.config.app_token and self.config.user_token):
            raise ConnectorError("Akahu is disabled or unconfigured")
        try:
            response = await self._client.get(
                f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}", params=params
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError("Akahu read request failed") from exc
        if not isinstance(payload, Mapping) or payload.get("success") is False:
            raise ConnectorError("Akahu returned an invalid read response")
        return payload

    @staticmethod
    def _page(payload: Mapping[str, object]) -> ProviderPage:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ConnectorError("Akahu response did not contain an item list")
        items = tuple(item for item in raw_items if isinstance(item, Mapping))
        raw_cursor = payload.get("cursor")
        next_cursor = raw_cursor.get("next") if isinstance(raw_cursor, Mapping) else None
        return ProviderPage(items=items, next_cursor=str(next_cursor) if next_cursor else None)

    async def list_accounts(self) -> ProviderPage:
        return self._page(await self._get("accounts"))

    async def list_transactions(
        self,
        *,
        start: str,
        end: str,
        cursor: str | None = None,
        pending: bool = False,
    ) -> ProviderPage:
        params = {"start": start, "end": end}
        if cursor:
            params["cursor"] = cursor
        path = "transactions/pending" if pending else "transactions"
        return self._page(await self._get(path, params=params))
