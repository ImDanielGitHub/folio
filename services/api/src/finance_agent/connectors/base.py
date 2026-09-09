"""Small connector seam for recorded sources and notification outboxes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class ConnectorError(RuntimeError):
    """Safe connector error that never includes secrets or response bodies."""


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
