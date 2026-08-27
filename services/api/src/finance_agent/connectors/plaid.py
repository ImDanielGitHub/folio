"""Configuration-gated, read-only Plaid sandbox/Link seam.

Credentials are process-injected, never persisted, and can only be sent to the
pinned Plaid API hosts (sandbox by default). Provider payloads are normalised
into exact USD cents before the finance importer is allowed to commit them.

Link flow (current Plaid pattern):
1. ``/link/token/create`` → temporary ``link_token`` for Plaid Link
2. Link returns a ``public_token`` (or sandbox creates one without Link)
3. ``/item/public_token/exchange`` → ephemeral ``access_token`` for this sync
4. ``/transactions/sync`` → settled transaction pages

Access tokens are not written to SQLite, evidence or receipts.
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

from finance_agent.connectors.base import (
    ConnectorError,
    connector_unconfigured,
    provider_http_error,
)

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "development": "https://development.plaid.com",
    "production": "https://production.plaid.com",
}
# First Platypus Bank — standard Plaid sandbox institution for Transactions.
SANDBOX_INSTITUTION_ID = "ins_109508"


@dataclass(frozen=True, slots=True)
class PlaidConfig:
    enabled: bool = False
    client_id: str | None = field(default=None, repr=False)
    secret: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)
    environment: str = "sandbox"
    products: tuple[str, ...] = ("transactions",)
    country_codes: tuple[str, ...] = ("US",)
    client_name: str = "Folio"
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        environment = self.environment.strip().lower()
        if environment not in PLAID_HOSTS:
            raise ValueError("Plaid environment must be sandbox, development or production")
        object.__setattr__(self, "environment", environment)
        parsed = urlparse(PLAID_HOSTS[environment])
        if parsed.scheme != "https" or parsed.hostname not in {
            "sandbox.plaid.com",
            "development.plaid.com",
            "production.plaid.com",
        }:
            raise ValueError("Plaid credentials may only be sent to pinned Plaid API hosts")
        if self.enabled and not (self.client_id and self.secret):
            raise ValueError("enabled Plaid requires client id and secret")

    @property
    def base_url(self) -> str:
        return PLAID_HOSTS[self.environment]

    @classmethod
    def from_env(cls) -> PlaidConfig:
        products_raw = os.getenv("PLAID_PRODUCTS", "transactions")
        countries_raw = os.getenv("PLAID_COUNTRY_CODES", "US")
        products = tuple(
            part.strip() for part in products_raw.split(",") if part.strip()
        ) or ("transactions",)
        countries = tuple(
            part.strip().upper() for part in countries_raw.split(",") if part.strip()
        ) or ("US",)
        return cls(
            enabled=os.getenv("FINANCE_PLAID_ENABLED", "false").lower() == "true",
            client_id=os.getenv("PLAID_CLIENT_ID") or None,
            secret=os.getenv("PLAID_SECRET") or None,
            access_token=os.getenv("PLAID_ACCESS_TOKEN") or None,
            environment=os.getenv("PLAID_ENV", "sandbox"),
            products=products,
            country_codes=countries,
            client_name=os.getenv("PLAID_CLIENT_NAME", "Folio") or "Folio",
        )


@dataclass(frozen=True, slots=True)
class PlaidAccount:
    provider_id: str
    account_id: str
    label: str
    currency: str
    mask: str | None = None


@dataclass(frozen=True, slots=True)
class PlaidTransaction:
    provider_id: str
    account_id: str
    occurred_on: str
    description: str
    amount_minor: int
    currency: str
    external_reference: str
    pending: bool = False


@dataclass(frozen=True, slots=True)
class PlaidSyncPage:
    added: tuple[Mapping[str, object], ...]
    modified: tuple[Mapping[str, object], ...]
    removed: tuple[Mapping[str, object], ...]
    next_cursor: str | None
    has_more: bool


def _stable_provider_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(f"plaid\0{value}".encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _required_text(item: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ConnectorError(f"Plaid item is missing required {keys[0]}")


def normalise_accounts(items: tuple[Mapping[str, object], ...]) -> tuple[PlaidAccount, ...]:
    """Validate and deterministically map provider accounts without retaining tokens."""

    accounts: list[PlaidAccount] = []
    seen: set[str] = set()
    for item in items:
        provider_id = _required_text(item, "account_id", "id")
        if provider_id in seen:
            raise ConnectorError("Plaid returned a duplicate account id")
        seen.add(provider_id)
        currency = "USD"
        balances = item.get("balances")
        if isinstance(balances, Mapping):
            iso = balances.get("iso_currency_code") or balances.get("unofficial_currency_code")
            if isinstance(iso, str) and iso.strip():
                currency = iso.strip().upper()
        if currency != "USD":
            raise ConnectorError("Folio currently supports USD Plaid accounts only")
        name = item.get("name") or item.get("official_name") or "Plaid account"
        if not isinstance(name, str) or not name.strip():
            name = "Plaid account"
        mask = item.get("mask")
        accounts.append(
            PlaidAccount(
                provider_id=provider_id,
                account_id=_stable_provider_id("acct", provider_id),
                label=name.strip()[:240],
                currency=currency,
                mask=mask.strip() if isinstance(mask, str) and mask.strip() else None,
            )
        )
    return tuple(sorted(accounts, key=lambda value: value.provider_id))


def _description(item: Mapping[str, object]) -> str:
    for key in ("name", "merchant_name", "original_description"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:500]
    raise ConnectorError("Plaid transaction is missing its description")


def _occurred_on(item: Mapping[str, object]) -> str:
    raw = _required_text(item, "date", "authorized_date")
    candidate = raw[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError as exc:
        raise ConnectorError("Plaid transaction has an invalid date") from exc


def _amount_minor(item: Mapping[str, object]) -> int:
    """Map Plaid amounts into Folio signed minor units.

    Plaid depository convention: positive amounts leave the account. Folio uses
    negative minor units for outflows, so the sign is inverted.
    """

    raw = item.get("amount")
    if raw is None or isinstance(raw, bool):
        raise ConnectorError("Plaid transaction is missing its amount")
    try:
        amount = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ConnectorError("Plaid transaction has an invalid amount") from exc
    if not amount.is_finite():
        raise ConnectorError("Plaid transaction has a non-finite amount")
    minor = (-amount) * 100
    if minor != minor.to_integral_value():
        raise ConnectorError("Plaid transaction amount has fractional cents")
    return int(minor)


def normalise_transactions(
    items: tuple[Mapping[str, object], ...],
    accounts: tuple[PlaidAccount, ...],
) -> tuple[PlaidTransaction, ...]:
    """Map settled provider rows to exact, stable canonical transaction fields."""

    account_by_provider_id = {account.provider_id: account for account in accounts}
    transactions: list[PlaidTransaction] = []
    seen: set[str] = set()
    for item in items:
        provider_id = _required_text(item, "transaction_id", "id")
        provider_account_id = _required_text(item, "account_id")
        account = account_by_provider_id.get(provider_account_id)
        if account is None:
            raise ConnectorError("Plaid transaction references an unknown account")
        pending_value = item.get("pending")
        if not isinstance(pending_value, bool):
            raise ConnectorError(
                "Plaid transaction pending must be a boolean",
                code="provider_invalid_response",
                provider="plaid",
            )
        if pending_value:
            continue
        external_reference = f"plaid:{provider_account_id}:{provider_id}"
        if external_reference in seen:
            raise ConnectorError("Plaid returned a duplicate transaction id")
        seen.add(external_reference)
        currency = item.get("iso_currency_code") or item.get("unofficial_currency_code")
        if currency is None:
            canonical_currency = account.currency
        elif isinstance(currency, str):
            canonical_currency = currency.strip().upper()
        else:
            raise ConnectorError("Plaid transaction has an invalid currency")
        if canonical_currency != "USD":
            raise ConnectorError("Folio currently supports USD Plaid transactions only")
        transactions.append(
            PlaidTransaction(
                provider_id=provider_id,
                account_id=account.account_id,
                occurred_on=_occurred_on(item),
                description=_description(item),
                amount_minor=_amount_minor(item),
                currency=canonical_currency,
                external_reference=external_reference,
                pending=pending_value,
            )
        )
    return tuple(
        sorted(
            transactions,
            key=lambda value: (value.occurred_on, value.external_reference),
        )
    )


class PlaidReadOnlyAdapter:
    def __init__(
        self,
        config: PlaidConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or PlaidConfig.from_env()
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def capability(self) -> dict[str, object]:
        configured = bool(
            self.config.enabled and self.config.client_id and self.config.secret
        )
        return {
            "provider": "plaid",
            "configured": configured,
            "mode": "read_only",
            "environment": self.config.environment,
            "hasAccessToken": bool(self.config.access_token),
            "supportsNewZealand": False,
            "markets": list(self.config.country_codes),
            "goldenPathDependency": False,
            "linkTokenPath": "/v1/connectors/plaid/link-token",
            "liveSyncPath": "/v1/connectors/plaid/sync",
        }

    def _auth_body(self) -> dict[str, str]:
        if not (self.config.enabled and self.config.client_id and self.config.secret):
            raise connector_unconfigured("Plaid")
        return {
            "client_id": self.config.client_id,
            "secret": self.config.secret,
        }

    async def _post(
        self, path: str, body: Mapping[str, object]
    ) -> Mapping[str, object]:
        payload = {**self._auth_body(), **body}
        try:
            response = await self._client.post(
                f"{self.config.base_url.rstrip('/')}/{path.lstrip('/')}",
                json=payload,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise provider_http_error("Plaid", exc.response.status_code) from exc
        except httpx.RequestError as exc:
            raise ConnectorError(
                "Plaid is temporarily unavailable",
                code="provider_unavailable",
                retryable=True,
                status_code=503,
                provider="plaid",
            ) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise ConnectorError(
                "Plaid returned invalid JSON",
                code="provider_invalid_response",
                provider="plaid",
            ) from exc
        if not isinstance(data, Mapping):
            raise ConnectorError(
                "Plaid returned an invalid read response",
                code="provider_invalid_response",
                provider="plaid",
            )
        return data

    async def create_link_token(self, *, client_user_id: str = "folio_local_owner") -> str:
        """Create a short-lived Link token for the current Plaid Link pattern."""

        response = await self._post(
            "/link/token/create",
            {
                "user": {"client_user_id": client_user_id},
                "client_name": self.config.client_name,
                "products": list(self.config.products),
                "country_codes": list(self.config.country_codes),
                "language": "en",
            },
        )
        token = response.get("link_token")
        if not isinstance(token, str) or not token.strip():
            raise ConnectorError("Plaid link token response was incomplete")
        return token.strip()

    async def exchange_public_token(self, public_token: str) -> tuple[str, str]:
        response = await self._post(
            "/item/public_token/exchange",
            {"public_token": public_token},
        )
        access_token = response.get("access_token")
        item_id = response.get("item_id")
        if not isinstance(access_token, str) or not access_token.strip():
            raise ConnectorError("Plaid token exchange did not return an access token")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ConnectorError("Plaid token exchange did not return an item id")
        return access_token.strip(), item_id.strip()

    async def create_sandbox_public_token(self) -> str:
        """Bypass Link in sandbox using /sandbox/public_token/create."""

        if self.config.environment != "sandbox":
            raise ConnectorError("Sandbox public tokens are only available in sandbox")
        response = await self._post(
            "/sandbox/public_token/create",
            {
                "institution_id": SANDBOX_INSTITUTION_ID,
                "initial_products": list(self.config.products),
            },
        )
        token = response.get("public_token")
        if not isinstance(token, str) or not token.strip():
            raise ConnectorError("Plaid sandbox public token response was incomplete")
        return token.strip()

    async def resolve_access_token(self, public_token: str | None = None) -> str:
        """Resolve an ephemeral access token without persisting it."""

        if public_token and public_token.strip():
            access_token, _item_id = await self.exchange_public_token(public_token.strip())
            return access_token
        if self.config.access_token:
            return self.config.access_token
        if self.config.environment == "sandbox":
            sandbox_token = await self.create_sandbox_public_token()
            access_token, _item_id = await self.exchange_public_token(sandbox_token)
            return access_token
        raise ConnectorError(
            "Plaid sync requires PLAID_ACCESS_TOKEN or a Link public_token"
        )

    @staticmethod
    def _mapping_items(
        payload: Mapping[str, object],
        field: str,
    ) -> tuple[Mapping[str, object], ...]:
        raw = payload.get(field)
        if not isinstance(raw, list):
            raise ConnectorError(
                f"Plaid response did not contain a {field} list",
                code="provider_invalid_response",
                provider="plaid",
            )
        if any(not isinstance(item, Mapping) for item in raw):
            raise ConnectorError(
                f"Plaid {field} contained a non-object item",
                code="provider_invalid_response",
                provider="plaid",
            )
        return tuple(item for item in raw if isinstance(item, Mapping))

    async def list_accounts(self, *, access_token: str) -> tuple[Mapping[str, object], ...]:
        response = await self._post("/accounts/get", {"access_token": access_token})
        return self._mapping_items(response, "accounts")

    async def sync_transactions(
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
        next_cursor_value = response.get("next_cursor")
        if next_cursor_value is not None and not isinstance(next_cursor_value, str):
            raise ConnectorError(
                "Plaid next_cursor must be text or null",
                code="provider_invalid_response",
                provider="plaid",
            )
        has_more = response.get("has_more")
        if not isinstance(has_more, bool):
            raise ConnectorError(
                "Plaid has_more must be a boolean",
                code="provider_invalid_response",
                provider="plaid",
            )
        next_cursor = next_cursor_value.strip() if isinstance(next_cursor_value, str) else None
        if has_more and not next_cursor:
            raise ConnectorError(
                "Plaid omitted next_cursor while has_more was true",
                code="provider_invalid_response",
                provider="plaid",
            )
        return PlaidSyncPage(
            added=self._mapping_items(response, "added"),
            modified=self._mapping_items(response, "modified"),
            removed=self._mapping_items(response, "removed"),
            next_cursor=next_cursor or None,
            has_more=has_more,
        )
