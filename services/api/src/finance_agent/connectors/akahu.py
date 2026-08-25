"""Configuration-gated, read-only Akahu provider seam.

Credentials are process-injected, never persisted, and can only be sent to the
pinned Akahu API host. Provider payloads are normalised into exact NZD cents
before the finance importer is allowed to commit them.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import httpx

from finance_agent.connectors.base import ConnectorError, ConnectorErrorCode


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


@dataclass(frozen=True, slots=True)
class AkahuAccount:
    """Minimal provider account identity used by the local importer."""

    provider_id: str
    account_id: str
    label: str
    currency: str


@dataclass(frozen=True, slots=True)
class AkahuTransaction:
    """Settled Akahu transaction reduced to Folio's deterministic schema."""

    provider_id: str
    account_id: str
    occurred_on: str
    description: str
    amount_minor: int
    currency: str
    external_reference: str


def _stable_provider_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(f"akahu\0{value}".encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _required_text(item: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ConnectorError(f"Akahu item is missing required {keys[0]}")


def _currency(item: Mapping[str, object]) -> str:
    direct = item.get("currency")
    if isinstance(direct, str) and direct.strip():
        return direct.strip().upper()
    balance = item.get("balance")
    if isinstance(balance, Mapping):
        nested = balance.get("currency")
        if isinstance(nested, str) and nested.strip():
            return nested.strip().upper()
    raise ConnectorError("Akahu item is missing its currency")


def normalise_accounts(items: tuple[Mapping[str, object], ...]) -> tuple[AkahuAccount, ...]:
    """Validate and deterministically map provider accounts without retaining tokens."""

    accounts: list[AkahuAccount] = []
    seen: set[str] = set()
    for item in items:
        provider_id = _required_text(item, "_id", "id")
        if provider_id in seen:
            raise ConnectorError("Akahu returned a duplicate account id")
        seen.add(provider_id)
        currency = _currency(item)
        if currency != "NZD":
            raise ConnectorError("Folio currently supports NZD Akahu accounts only")
        try:
            label = _required_text(item, "name", "formatted_account")
        except ConnectorError:
            label = "Akahu account"
        accounts.append(
            AkahuAccount(
                provider_id=provider_id,
                account_id=_stable_provider_id("acct", provider_id),
                label=label[:240],
                currency=currency,
            )
        )
    return tuple(sorted(accounts, key=lambda value: value.provider_id))


def _transaction_account_id(item: Mapping[str, object]) -> str:
    value = item.get("_account")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        return _required_text(value, "_id", "id")
    value = item.get("account")
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, Mapping):
        return _required_text(value, "_id", "id")
    raise ConnectorError("Akahu transaction is missing its account id")


def _description(item: Mapping[str, object]) -> str:
    for key in ("description", "particulars", "type"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    merchant = item.get("merchant")
    if isinstance(merchant, Mapping):
        try:
            return _required_text(merchant, "name")[:500]
        except ConnectorError:
            pass
    raise ConnectorError("Akahu transaction is missing its description")


def _occurred_on(item: Mapping[str, object]) -> str:
    raw = _required_text(item, "date")
    candidate = raw[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError as exc:
        raise ConnectorError("Akahu transaction has an invalid date") from exc


def _amount_minor(item: Mapping[str, object]) -> int:
    raw = item.get("amount")
    if raw is None or isinstance(raw, bool):
        raise ConnectorError("Akahu transaction is missing its amount")
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ConnectorError("Akahu transaction has an invalid amount") from exc
    if not amount.is_finite():
        raise ConnectorError("Akahu transaction has a non-finite amount")
    minor = amount * 100
    if minor != minor.to_integral_value():
        raise ConnectorError("Akahu transaction amount has fractional cents")
    return int(minor)


def normalise_transactions(
    items: tuple[Mapping[str, object], ...],
    accounts: tuple[AkahuAccount, ...],
) -> tuple[AkahuTransaction, ...]:
    """Map settled provider rows to exact, stable canonical transaction fields."""

    account_by_provider_id = {account.provider_id: account for account in accounts}
    transactions: list[AkahuTransaction] = []
    seen: set[str] = set()
    for item in items:
        provider_id = _required_text(item, "_id", "id")
        provider_account_id = _transaction_account_id(item)
        account = account_by_provider_id.get(provider_account_id)
        if account is None:
            raise ConnectorError("Akahu transaction references an unknown account")
        external_reference = f"akahu:{provider_account_id}:{provider_id}"
        if external_reference in seen:
            raise ConnectorError("Akahu returned a duplicate transaction id")
        seen.add(external_reference)
        currency = item.get("currency")
        if currency is None:
            canonical_currency = account.currency
        elif isinstance(currency, str):
            canonical_currency = currency.strip().upper()
        else:
            raise ConnectorError("Akahu transaction has an invalid currency")
        if canonical_currency != "NZD":
            raise ConnectorError("Folio currently supports NZD Akahu transactions only")
        transactions.append(
            AkahuTransaction(
                provider_id=provider_id,
                account_id=account.account_id,
                occurred_on=_occurred_on(item),
                description=_description(item),
                amount_minor=_amount_minor(item),
                currency=canonical_currency,
                external_reference=external_reference,
            )
        )
    return tuple(
        sorted(
            transactions,
            key=lambda value: (value.occurred_on, value.external_reference),
        )
    )


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
        self._headers = headers
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
            raise ConnectorError(
                "Akahu is disabled or unconfigured",
                code=ConnectorErrorCode.UNCONFIGURED,
            )
        try:
            response = await self._client.get(
                f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}",
                params=params,
                headers=self._headers,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise ConnectorError(
                "Akahu read request failed",
                code=ConnectorErrorCode.UPSTREAM_FAILURE,
                retryable=status == 429 or status >= 500,
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError(
                "Akahu read request failed",
                code=ConnectorErrorCode.UPSTREAM_FAILURE,
                retryable=True,
            ) from exc
        if not isinstance(payload, Mapping) or payload.get("success") is False:
            raise ConnectorError(
                "Akahu returned an invalid read response",
                code=ConnectorErrorCode.INVALID_RESPONSE,
            )
        return payload

    @staticmethod
    def _page(payload: Mapping[str, object]) -> ProviderPage:
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ConnectorError(
                "Akahu response did not contain an item list",
                code=ConnectorErrorCode.INVALID_RESPONSE,
            )
        if any(not isinstance(item, Mapping) for item in raw_items):
            raise ConnectorError(
                "Akahu response contained a malformed item",
                code=ConnectorErrorCode.INVALID_RESPONSE,
            )
        items = tuple(raw_items)
        raw_cursor = payload.get("cursor")
        next_cursor = raw_cursor.get("next") if isinstance(raw_cursor, Mapping) else None
        return ProviderPage(items=items, next_cursor=str(next_cursor) if next_cursor else None)

    async def list_accounts(self, *, cursor: str | None = None) -> ProviderPage:
        params = {"cursor": cursor} if cursor else None
        return self._page(await self._get("accounts", params=params))

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
