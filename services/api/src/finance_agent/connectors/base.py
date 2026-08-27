"""Small connector seam for recorded sources and notification outboxes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class ConnectorError(RuntimeError):
    """Safe, typed connector failure without secrets or provider response bodies."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "connector_failure",
        retryable: bool = False,
        status_code: int = 502,
        provider: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.provider = provider

    def as_detail(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
            "provider": self.provider,
        }


def connector_unconfigured(provider: str) -> ConnectorError:
    return ConnectorError(
        f"{provider} is disabled or unconfigured",
        code="connector_unconfigured",
        status_code=409,
        provider=provider.lower(),
    )


def provider_http_error(provider: str, status_code: int) -> ConnectorError:
    normalised = provider.lower()
    if status_code in {401, 403}:
        return ConnectorError(
            f"{provider} authentication failed",
            code="provider_auth_failed",
            status_code=502,
            provider=normalised,
        )
    if status_code == 429:
        return ConnectorError(
            f"{provider} rate limited the request",
            code="provider_rate_limited",
            retryable=True,
            status_code=503,
            provider=normalised,
        )
    if status_code >= 500:
        return ConnectorError(
            f"{provider} is temporarily unavailable",
            code="provider_unavailable",
            retryable=True,
            status_code=503,
            provider=normalised,
        )
    return ConnectorError(
        f"{provider} rejected the request",
        code="provider_request_rejected",
        status_code=502,
        provider=normalised,
    )


@dataclass(frozen=True, slots=True)
class InboundSource:
    source_item_id: str
    connector: str
    external_id: str
    received_at: datetime
    text: str
    attachment_references: tuple[Mapping[str, object], ...]
    metadata: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    outbox_id: str
    connector: str
    destination_id: str
    kind: str
    text: str
    correlation_id: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    outbox_id: str
    status: str
    provider_message_id: str | None
    attempted_at: datetime


class InboundSourceConnector(Protocol):
    async def poll(self, *, offset: int | None = None) -> tuple[InboundSource, ...]: ...


class OutboundNotifier(Protocol):
    async def send(self, message: OutboxMessage) -> DeliveryReceipt: ...
