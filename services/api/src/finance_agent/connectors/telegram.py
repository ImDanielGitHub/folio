"""Recorded Telegram ingestion and optional, configuration-gated Bot API I/O."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlparse

import httpx

from finance_agent.connectors.base import (
    ConnectorError,
    DeliveryReceipt,
    InboundSource,
    OutboxMessage,
)

_SECRET_PATTERNS = (
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~-]{12,}\b", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
)


def redact_secrets(text: str) -> str:
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


class UpdateDedupe(Protocol):
    def seen(self, update_id: int) -> bool: ...

    def record(self, update_id: int) -> None: ...


@dataclass(slots=True)
class InMemoryUpdateDedupe:
    update_ids: set[int] = field(default_factory=set)

    def seen(self, update_id: int) -> bool:
        return update_id in self.update_ids

    def record(self, update_id: int) -> None:
        self.update_ids.add(update_id)


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    live_enabled: bool = False
    bot_token: str | None = field(default=None, repr=False)
    allowed_chat_id: int | None = None
    base_url: str = "https://api.telegram.org"
    max_text_characters: int = 2000
    max_photo_bytes: int = 10_000_000
    timeout_seconds: float = 35.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or parsed.hostname != "api.telegram.org":
            raise ValueError("Telegram credentials may only be sent to https://api.telegram.org")
        if self.live_enabled and not (self.bot_token and self.allowed_chat_id is not None):
            raise ValueError("live Telegram requires a token and one allowlisted chat")

    @classmethod
    def from_env(cls) -> TelegramConfig:
        allowed = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
        return cls(
            live_enabled=os.getenv("TELEGRAM_LIVE_ENABLED", "false").lower() == "true",
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            allowed_chat_id=int(allowed) if allowed else None,
        )


@dataclass(frozen=True, slots=True)
class TelegramIngestResult:
    status: str
    update_id: int
    source: InboundSource | None


class TelegramFixtureIngestor:
    def __init__(
        self,
        config: TelegramConfig | None = None,
        *,
        dedupe: UpdateDedupe | None = None,
    ) -> None:
        self.config = config or TelegramConfig()
        self.dedupe = dedupe or InMemoryUpdateDedupe()

    def ingest(
        self,
        update: Mapping[str, object],
        attachment_reference: Mapping[str, object] | None = None,
    ) -> TelegramIngestResult:
        update_id = update.get("update_id")
        if not isinstance(update_id, int) or update_id < 0:
            raise ConnectorError("Telegram update_id must be a non-negative integer")
        if self.dedupe.seen(update_id):
            return TelegramIngestResult("deduplicated", update_id, None)
        message = update.get("message")
        if not isinstance(message, Mapping):
            raise ConnectorError("Telegram fixture must contain one message")
        chat = message.get("chat")
        if not isinstance(chat, Mapping) or not isinstance(chat.get("id"), int):
            raise ConnectorError("Telegram message must contain a numeric chat id")
        chat_id = int(chat["id"])
        if self.config.allowed_chat_id is not None and chat_id != self.config.allowed_chat_id:
            raise ConnectorError("Telegram chat is not allowlisted")
        message_id = message.get("message_id")
        if not isinstance(message_id, int):
            raise ConnectorError("Telegram message_id must be an integer")
        raw_text = message.get("caption", message.get("text", ""))
        if not isinstance(raw_text, str):
            raise ConnectorError("Telegram text/caption must be a string")
        if len(raw_text) > self.config.max_text_characters:
            raise ConnectorError("Telegram text/caption exceeds the bounded payload limit")
        text = redact_secrets(raw_text)
        attachments: list[Mapping[str, object]] = []
        photos = message.get("photo", [])
        if photos:
            if not isinstance(photos, list) or len(photos) > 10:
                raise ConnectorError("Telegram photo payload is invalid or too large")
            photo = photos[-1]
            if not isinstance(photo, Mapping):
                raise ConnectorError("Telegram photo reference must be an object")
            size = photo.get("file_size", 0)
            if not isinstance(size, int) or size < 0 or size > self.config.max_photo_bytes:
                raise ConnectorError("Telegram photo exceeds the bounded payload limit")
            file_id = photo.get("file_id")
            if not isinstance(file_id, str) or not file_id:
                raise ConnectorError("Telegram photo is missing a file reference")
            if attachment_reference is not None:
                expected_file_id = attachment_reference.get("telegramFileId")
                if expected_file_id != file_id:
                    raise ConnectorError("Telegram attachment reference does not match the update")
                attachments.append(dict(attachment_reference))
            else:
                attachments.append(
                    {
                        "telegramFileId": file_id,
                        "fileUniqueId": photo.get("file_unique_id"),
                        "fileSize": size,
                        "mediaType": "image/jpeg",
                    }
                )
        timestamp = message.get("date")
        received_at = (
            datetime.fromtimestamp(timestamp, tz=UTC)
            if isinstance(timestamp, int)
            else datetime.now(UTC)
        )
        fixture_source_id = (
            attachment_reference.get("sourceItemId")
            if attachment_reference is not None
            else None
        )
        source_id = (
            str(fixture_source_id)
            if fixture_source_id
            else f"src_telegram_{update_id}"
        )
        source = InboundSource(
            source_item_id=source_id,
            connector="telegram",
            external_id=f"{update_id}:{message_id}",
            received_at=received_at,
            text=text,
            attachment_references=tuple(attachments),
            metadata={
                "updateId": update_id,
                "messageId": message_id,
                "chatId": chat_id,
                "untrustedContent": True,
            },
        )
        self.dedupe.record(update_id)
        return TelegramIngestResult("ingested", update_id, source)


class TelegramBotAdapter:
    """Optional real Bot API poll/send adapter; inert unless live_enabled is true."""

    def __init__(
        self,
        config: TelegramConfig | None = None,
        *,
        ingestor: TelegramFixtureIngestor | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or TelegramConfig.from_env()
        self.ingestor = ingestor or TelegramFixtureIngestor(self.config)
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0)
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _method_url(self, method: str) -> str:
        if not self.config.live_enabled or not self.config.bot_token:
            raise ConnectorError("live Telegram is disabled")
        return f"{self.config.base_url}/bot{self.config.bot_token}/{method}"

    async def poll(self, *, offset: int | None = None) -> tuple[InboundSource, ...]:
        params: dict[str, object] = {
            "timeout": 25,
            "limit": 25,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            params["offset"] = offset
        try:
            response = await self._client.post(self._method_url("getUpdates"), json=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError("Telegram polling failed") from exc
        values = payload.get("result") if isinstance(payload, Mapping) else None
        if not isinstance(values, list):
            raise ConnectorError("Telegram polling returned an invalid payload")
        sources: list[InboundSource] = []
        for update in values:
            if not isinstance(update, Mapping):
                continue
            result = self.ingestor.ingest(update)
            if result.source is not None:
                sources.append(result.source)
        return tuple(sources)

    async def send(self, message: OutboxMessage) -> DeliveryReceipt:
        if str(self.config.allowed_chat_id) != message.destination_id:
            raise ConnectorError("Telegram destination is not allowlisted")
        if not 1 <= len(message.text) <= 1000:
            raise ConnectorError("Telegram outbox text must be between 1 and 1000 characters")
        payload = {
            "chat_id": self.config.allowed_chat_id,
            "text": redact_secrets(message.text),
            "disable_web_page_preview": True,
        }
        attempted_at = datetime.now(UTC)
        try:
            response = await self._client.post(self._method_url("sendMessage"), json=payload)
            response.raise_for_status()
            body = response.json()
            result = body.get("result") if isinstance(body, Mapping) else None
            provider_id = result.get("message_id") if isinstance(result, Mapping) else None
        except (httpx.HTTPError, ValueError) as exc:
            raise ConnectorError("Telegram send failed") from exc
        return DeliveryReceipt(
            outbox_id=message.outbox_id,
            status="delivered",
            provider_message_id=str(provider_id) if provider_id is not None else None,
            attempted_at=attempted_at,
        )


def reserve_risk_outbox(
    *,
    outbox_id: str,
    destination_id: str,
    correlation_id: str,
    shortfall_label: str,
) -> OutboxMessage:
    return OutboxMessage(
        outbox_id=outbox_id,
        connector="telegram",
        destination_id=destination_id,
        kind="reserve_risk_brief",
        text=(
            f"Koru Studio cash note: the 30-day plan moves {shortfall_label} below the "
            "protected reserve. Open the local workspace to review assumptions."
        ),
        correlation_id=correlation_id,
        metadata={"containsFullLedger": False, "containsRawSource": False},
    )
