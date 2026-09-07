from __future__ import annotations

import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def insert_method_before(path: str, class_name: str, before_name: str, method: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    before = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


MIGRATION = '''    Migration(
        version={version},
        name="telegram_live_inbound",
        sql="""
        CREATE TABLE telegram_poll_state (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id),
            next_offset INTEGER NOT NULL DEFAULT 0 CHECK (next_offset >= 0),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE telegram_live_updates (
            update_id INTEGER PRIMARY KEY CHECK (update_id >= 0),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            chat_id_hash TEXT NOT NULL CHECK (length(chat_id_hash) = 64),
            message_id INTEGER,
            payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
            evidence_id TEXT NOT NULL REFERENCES evidence_links(evidence_id),
            document_id TEXT REFERENCES knowledge_documents(document_id),
            received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('processed', 'rejected'))
        );

        CREATE TABLE telegram_live_attachments (
            attachment_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            update_id INTEGER NOT NULL REFERENCES telegram_live_updates(update_id),
            telegram_file_id_hash TEXT NOT NULL CHECK (length(telegram_file_id_hash) = 64),
            filename TEXT NOT NULL,
            media_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
            content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
            source_bytes BLOB NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (workspace_id, update_id, content_sha256)
        );

        CREATE INDEX telegram_updates_workspace_time
            ON telegram_live_updates(workspace_id, received_at, update_id);
        """,
    ),
'''

TELEGRAM_LIVE = '''"""Authenticated, allowlisted, inbound-only Telegram capture."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePath
from typing import Any
from urllib.parse import urlparse

import httpx

from finance_agent.connectors.base import ConnectorError
from finance_agent.connectors.provider_http import (
    ProviderRequestError,
    RetryPolicy,
    request_json_with_retry,
)
from finance_agent.storage import SQLiteStore, canonical_json

TELEGRAM_HOST = "api.telegram.org"
MAX_ATTACHMENT_BYTES = 10_000_000
MAX_MESSAGE_CHARACTERS = 8_000
ALLOWED_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
        "text/markdown",
        "image/jpeg",
        "image/png",
    }
)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _hash(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _clean_filename(value: str) -> str:
    name = PurePath(value.replace("\\", "/")).name.strip()
    name = re.sub(r"[\\x00-\\x1f\\x7f]", "", name)
    return (name or "telegram-attachment")[:255]


@dataclass(frozen=True, slots=True)
class TelegramLiveConfig:
    enabled: bool = False
    bot_token: str | None = field(default=None, repr=False)
    allowed_chat_id: int | None = field(default=None, repr=False)
    base_url: str = "https://api.telegram.org"
    timeout_seconds: float = 35.0
    max_attachment_bytes: int = MAX_ATTACHMENT_BYTES

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or parsed.hostname != TELEGRAM_HOST:
            raise ValueError("Telegram credentials may only use https://api.telegram.org")
        if self.enabled and (not self.bot_token or self.allowed_chat_id is None):
            raise ValueError("enabled Telegram requires bot token and allowed chat ID")
        if not 1 <= self.max_attachment_bytes <= MAX_ATTACHMENT_BYTES:
            raise ValueError("Telegram attachment limit must be between 1 byte and 10 MB")

    @classmethod
    def from_env(cls) -> TelegramLiveConfig:
        chat = os.getenv("TELEGRAM_ALLOWED_CHAT_ID")
        return cls(
            enabled=os.getenv("TELEGRAM_LIVE_ENABLED", "false").lower() == "true",
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            allowed_chat_id=int(chat) if chat else None,
        )


@dataclass(frozen=True, slots=True)
class TelegramAttachment:
    file_id: str
    filename: str
    media_type: str
    declared_size: int | None


class TelegramLiveAdapter:
    def __init__(
        self,
        config: TelegramLiveConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self.config = config or TelegramLiveConfig.from_env()
        self.retry_policy = retry_policy or RetryPolicy.from_env("TELEGRAM")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
            headers={"Accept": "application/json"},
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def capability(self) -> dict[str, object]:
        return {
            "provider": "telegram",
            "configured": bool(
                self.config.enabled
                and self.config.bot_token
                and self.config.allowed_chat_id is not None
            ),
            "mode": "inbound_only",
            "sendSupported": False,
            "attachmentLimitBytes": self.config.max_attachment_bytes,
            "allowedMediaTypes": sorted(ALLOWED_MEDIA_TYPES),
            "retryPolicy": self.retry_policy.as_dict(),
        }

    def _api_url(self, method: str) -> str:
        if not self.config.bot_token:
            raise ConnectorError("Telegram is disabled or unconfigured")
        if not re.fullmatch(r"[A-Za-z]+", method):
            raise ValueError("invalid Telegram API method")
        return f"{self.config.base_url.rstrip('/')}/bot{self.config.bot_token}/{method}"

    async def _api(
        self, method: str, body: Mapping[str, object]
    ) -> Mapping[str, object]:
        if not (
            self.config.enabled
            and self.config.bot_token
            and self.config.allowed_chat_id is not None
        ):
            raise ConnectorError("Telegram is disabled or unconfigured")
        try:
            result = await request_json_with_retry(
                self._client,
                method="POST",
                url=self._api_url(method),
                provider="telegram",
                operation=method,
                policy=self.retry_policy,
                idempotent=True,
                json_body=body,
            )
        except ProviderRequestError as exc:
            raise ConnectorError(exc.safe_detail()) from exc
        if result.payload.get("ok") is not True:
            raise ConnectorError(f"Telegram {method} returned an unsuccessful response")
        return result.payload

    async def get_updates(self, *, offset: int, limit: int = 50) -> tuple[Mapping[str, object], ...]:
        payload = await self._api(
            "getUpdates",
            {
                "offset": offset,
                "limit": min(max(limit, 1), 100),
                "timeout": 0,
                "allowed_updates": ["message"],
            },
        )
        result = payload.get("result")
        if not isinstance(result, list):
            raise ConnectorError("Telegram getUpdates response has no result list")
        return tuple(value for value in result if isinstance(value, Mapping))

    async def file_path(self, file_id: str) -> str:
        payload = await self._api("getFile", {"file_id": file_id})
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise ConnectorError("Telegram getFile response is incomplete")
        path = result.get("file_path")
        if not isinstance(path, str) or not path.strip() or ".." in path or path.startswith("/"):
            raise ConnectorError("Telegram returned an invalid attachment path")
        return path

    async def download(self, file_id: str, *, declared_size: int | None) -> bytes:
        if declared_size is not None and declared_size > self.config.max_attachment_bytes:
            raise ConnectorError("Telegram attachment exceeds the local byte limit")
        if not self.config.bot_token:
            raise ConnectorError("Telegram is disabled or unconfigured")
        file_path = await self.file_path(file_id)
        url = f"{self.config.base_url.rstrip('/')}/file/bot{self.config.bot_token}/{file_path}"
        total = 0
        chunks: list[bytes] = []
        try:
            async with self._client.stream("GET", url) as response:
                if response.is_error:
                    raise ConnectorError("Telegram attachment download failed")
                length = response.headers.get("content-length")
                if length and int(length) > self.config.max_attachment_bytes:
                    raise ConnectorError("Telegram attachment exceeds the local byte limit")
                async for chunk in response.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > self.config.max_attachment_bytes:
                        raise ConnectorError("Telegram attachment exceeds the local byte limit")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ConnectorError("Telegram attachment download failed") from exc
        if not chunks:
            raise ConnectorError("Telegram attachment is empty")
        return b"".join(chunks)


def _message(update: Mapping[str, object]) -> Mapping[str, object] | None:
    value = update.get("message")
    return value if isinstance(value, Mapping) else None


def _chat_id(message: Mapping[str, object]) -> int | None:
    chat = message.get("chat")
    value = chat.get("id") if isinstance(chat, Mapping) else None
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _attachment(message: Mapping[str, object]) -> TelegramAttachment | None:
    document = message.get("document")
    if isinstance(document, Mapping):
        file_id = document.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            return None
        media_type = str(document.get("mime_type") or "application/octet-stream")
        filename = _clean_filename(str(document.get("file_name") or "telegram-document"))
        size = document.get("file_size")
        return TelegramAttachment(
            file_id=file_id,
            filename=filename,
            media_type=media_type,
            declared_size=int(size) if isinstance(size, int) and not isinstance(size, bool) else None,
        )
    photos = message.get("photo")
    if isinstance(photos, list):
        candidates = [value for value in photos if isinstance(value, Mapping)]
        if candidates:
            selected = max(
                candidates,
                key=lambda value: int(value.get("file_size") or 0),
            )
            file_id = selected.get("file_id")
            if isinstance(file_id, str) and file_id:
                size = selected.get("file_size")
                return TelegramAttachment(
                    file_id=file_id,
                    filename="telegram-photo.jpg",
                    media_type="image/jpeg",
                    declared_size=int(size) if isinstance(size, int) and not isinstance(size, bool) else None,
                )
    return None


class TelegramInboundService:
    def __init__(self, store: SQLiteStore, adapter: TelegramLiveAdapter) -> None:
        self.store = store
        self.adapter = adapter

    def _offset(self, workspace_id: str) -> int:
        row = self.store.fetch_one(
            "SELECT next_offset FROM telegram_poll_state WHERE workspace_id = ?",
            (workspace_id,),
        )
        return int(row["next_offset"]) if row else 0

    async def poll_once(
        self,
        *,
        workspace_id: str,
        limit: int = 50,
    ) -> dict[str, object]:
        offset = self._offset(workspace_id)
        updates = await self.adapter.get_updates(offset=offset, limit=limit)
        processed = 0
        deduplicated = 0
        rejected = 0
        attachment_count = 0
        highest = offset - 1
        for update in sorted(updates, key=lambda value: int(value.get("update_id", -1))):
            raw_update_id = update.get("update_id")
            if not isinstance(raw_update_id, int) or isinstance(raw_update_id, bool) or raw_update_id < 0:
                raise ConnectorError("Telegram update has an invalid update_id")
            update_id = int(raw_update_id)
            highest = max(highest, update_id)
            if self.store.fetch_one(
                "SELECT 1 FROM telegram_live_updates WHERE update_id = ?",
                (update_id,),
            ) is not None:
                deduplicated += 1
                continue
            message = _message(update)
            chat_id = _chat_id(message) if message else None
            if message is None or chat_id != self.adapter.config.allowed_chat_id:
                rejected += 1
                continue
            text = str(message.get("text") or message.get("caption") or "").strip()
            text = text[:MAX_MESSAGE_CHARACTERS]
            attachment = _attachment(message)
            attachment_bytes: bytes | None = None
            if attachment is not None:
                if attachment.media_type not in ALLOWED_MEDIA_TYPES:
                    raise ConnectorError("Telegram attachment media type is not allowed")
                attachment_bytes = await self.adapter.download(
                    attachment.file_id,
                    declared_size=attachment.declared_size,
                )
            if not text and attachment is None:
                rejected += 1
                continue
            received_at = datetime.now(UTC).isoformat()
            payload_json = canonical_json(update)
            payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
            evidence_id = _stable_id("evd", workspace_id, "telegram", str(update_id))
            document_id = _stable_id("doc", workspace_id, "telegram", str(update_id))
            title = attachment.filename if attachment else f"Telegram message {update_id}"
            extracted_text = text or f"Telegram attachment: {title}"
            metadata = {
                "provider": "telegram",
                "updateId": update_id,
                "messageId": message.get("message_id"),
                "chatAllowlisted": True,
                "attachmentMediaType": attachment.media_type if attachment else None,
                "attachmentDownloaded": attachment_bytes is not None,
                "ocrUsed": False,
            }
            record_value = {
                "documentId": document_id,
                "workspaceId": workspace_id,
                "documentKind": "correspondence",
                "title": title,
                "taskScope": "telegram inbound context",
                "sourceKind": "connector",
                "sourceRef": f"telegram:update:{update_id}",
                "evidenceId": evidence_id,
                "receivedAt": received_at,
                "extractedText": extracted_text,
                "contentHash": payload_hash,
                "metadata": metadata,
            }
            record_hash = hashlib.sha256(canonical_json(record_value).encode()).hexdigest()
            with self.store.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO evidence_links(
                        evidence_id, workspace_id, source_item_id, source_row_id,
                        label, created_at
                    ) VALUES (?, ?, NULL, NULL, ?, ?)
                    """,
                    (evidence_id, workspace_id, title, received_at),
                )
                connection.execute(
                    """
                    INSERT INTO knowledge_documents(
                        document_id, workspace_id, document_kind, title, task_scope,
                        source_kind, source_ref, source_turn_id, evidence_id, received_at,
                        effective_from, effective_until, extracted_text, content_hash,
                        metadata_json, record_hash
                    ) VALUES (?, ?, 'correspondence', ?, 'telegram inbound context',
                        'connector', ?, NULL, ?, ?, NULL, NULL, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        workspace_id,
                        title,
                        f"telegram:update:{update_id}",
                        evidence_id,
                        received_at,
                        extracted_text,
                        payload_hash,
                        canonical_json(metadata),
                        record_hash,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO telegram_live_updates(
                        update_id, workspace_id, chat_id_hash, message_id,
                        payload_hash, evidence_id, document_id, received_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'processed')
                    """,
                    (
                        update_id,
                        workspace_id,
                        _hash(chat_id),
                        message.get("message_id"),
                        payload_hash,
                        evidence_id,
                        document_id,
                        received_at,
                    ),
                )
                if attachment and attachment_bytes:
                    content_hash = hashlib.sha256(attachment_bytes).hexdigest()
                    connection.execute(
                        """
                        INSERT INTO telegram_live_attachments(
                            attachment_id, workspace_id, update_id,
                            telegram_file_id_hash, filename, media_type, size_bytes,
                            content_sha256, source_bytes, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            _stable_id("att", workspace_id, str(update_id), content_hash),
                            workspace_id,
                            update_id,
                            _hash(attachment.file_id),
                            attachment.filename,
                            attachment.media_type,
                            len(attachment_bytes),
                            content_hash,
                            attachment_bytes,
                            received_at,
                        ),
                    )
                    attachment_count += 1
            processed += 1
        next_offset = max(offset, highest + 1)
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO telegram_poll_state(workspace_id, next_offset, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    next_offset = excluded.next_offset,
                    updated_at = excluded.updated_at
                """,
                (workspace_id, next_offset, datetime.now(UTC).isoformat()),
            )
        return {
            "workspaceId": workspace_id,
            "received": len(updates),
            "processed": processed,
            "deduplicated": deduplicated,
            "rejected": rejected,
            "attachments": attachment_count,
            "nextOffset": next_offset,
            "externalCallsMade": True,
            "sendAttempted": False,
            "ocrUsed": False,
        }
'''

SERVICE_METHOD = '''    async def poll_telegram_live(
        self, *, workspace_id: str, limit: int
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            result = await TelegramInboundService(
                self.store, self.telegram_live
            ).poll_once(workspace_id=workspace_id, limit=limit)
            self.working_understanding.ensure_current(workspace_id=workspace_id)
        return result
'''

ROUTE = '''    @router.post("/v1/workspaces/{workspace_id}/connectors/telegram/poll")
    async def poll_telegram_live(
        workspace_id: PathIdentifier,
        services: Services,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.poll_telegram_live(
                    workspace_id=workspace_id,
                    limit=limit,
                )
            )
        except ConnectorError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

'''

CLI = '''from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from finance_agent.api.services import LocalRouteServices
from finance_agent.workspace import active_database_path, active_workspace_id


async def run(limit: int) -> int:
    services = LocalRouteServices(active_database_path(), auto_seed=True)
    try:
        result = await services.poll_telegram_live(
            workspace_id=active_workspace_id(),
            limit=limit,
        )
        print(result)
        return 0
    finally:
        await services.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Explicitly poll the configured inbound-only Telegram bot once"
    )
    parser.add_argument("--limit", type=int, default=50)
    arguments = parser.parse_args()
    if not 1 <= arguments.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    return asyncio.run(run(arguments.limit))


if __name__ == "__main__":
    raise SystemExit(main())
'''

TESTS = '''from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from finance_agent.connectors.provider_http import RetryPolicy
from finance_agent.connectors.telegram_live import (
    TelegramInboundService,
    TelegramLiveAdapter,
    TelegramLiveConfig,
)
from finance_agent.finance import FinanceEngine
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def update(update_id: int, chat_id: int, *, text: str = "Owner context", document: dict | None = None) -> dict:
    message = {
        "message_id": update_id + 100,
        "date": 1787700000,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": chat_id, "is_bot": False, "first_name": "Owner"},
        "text": text,
    }
    if document:
        message.pop("text", None)
        message["caption"] = text
        message["document"] = document
    return {"update_id": update_id, "message": message}


@pytest.mark.asyncio
async def test_allowlisted_updates_commit_once_and_advance_offset(tmp_path: Path) -> None:
    calls: list[str] = []
    payload = {
        "ok": True,
        "result": [
            update(10, 700001, text="Acme will pay next Friday."),
            update(11, 999999, text="unauthorised private content"),
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=payload)

    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TelegramLiveAdapter(
        TelegramLiveConfig(
            enabled=True,
            bot_token="123:secret-token",
            allowed_chat_id=700001,
        ),
        client=client,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    service = TelegramInboundService(store, adapter)
    first = await service.poll_once(workspace_id="ws_koru_studio")
    assert first["processed"] == 1
    assert first["rejected"] == 1
    assert first["nextOffset"] == 12
    assert first["sendAttempted"] is False
    second = await service.poll_once(workspace_id="ws_koru_studio")
    assert second["deduplicated"] == 1
    rows = store.fetch_all("SELECT * FROM telegram_live_updates")
    assert len(rows) == 1
    documents = store.fetch_all("SELECT extracted_text FROM knowledge_documents WHERE source_kind = 'connector'")
    assert len(documents) == 1
    assert "Acme will pay" in str(documents[0]["extracted_text"])
    assert "unauthorised private content" not in " ".join(str(row) for row in documents)
    assert all("secret-token" not in path for path in calls)
    await client.aclose()


@pytest.mark.asyncio
async def test_bounded_document_attachment_is_downloaded_without_ocr(tmp_path: Path) -> None:
    document = {
        "file_id": "file-secret-id",
        "file_unique_id": "unique",
        "file_name": "invoice.pdf",
        "mime_type": "application/pdf",
        "file_size": 12,
    }
    updates = {"ok": True, "result": [update(20, 700001, text="Client invoice", document=document)]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/getUpdates"):
            return httpx.Response(200, json=updates)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "documents/invoice.pdf"}})
        if "/file/bot" in request.url.path:
            return httpx.Response(200, content=b"%PDF-fixture")
        raise AssertionError(request.url)

    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TelegramLiveAdapter(
        TelegramLiveConfig(enabled=True, bot_token="123:token", allowed_chat_id=700001),
        client=client,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    result = await TelegramInboundService(store, adapter).poll_once(
        workspace_id="ws_koru_studio"
    )
    assert result["attachments"] == 1
    assert result["ocrUsed"] is False
    attachment = store.fetch_one("SELECT * FROM telegram_live_attachments")
    assert bytes(attachment["source_bytes"]) == b"%PDF-fixture"
    assert str(attachment["telegram_file_id_hash"]) != "file-secret-id"
    await client.aclose()


@pytest.mark.asyncio
async def test_oversized_or_disallowed_attachment_fails_closed_before_commit(tmp_path: Path) -> None:
    document = {
        "file_id": "file-id",
        "file_unique_id": "unique",
        "file_name": "malware.exe",
        "mime_type": "application/x-msdownload",
        "file_size": 100,
    }
    updates = {"ok": True, "result": [update(30, 700001, text="unsafe", document=document)]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=updates)

    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TelegramLiveAdapter(
        TelegramLiveConfig(enabled=True, bot_token="123:token", allowed_chat_id=700001),
        client=client,
        retry_policy=RetryPolicy(max_attempts=1),
    )
    with pytest.raises(Exception, match="media type"):
        await TelegramInboundService(store, adapter).poll_once(
            workspace_id="ws_koru_studio"
        )
    assert store.fetch_all("SELECT * FROM telegram_live_updates") == []
    await client.aclose()
'''


def add_migration_and_module() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    content = read(path)
    versions = [int(value) for value in re.findall(r"version=(\d+)", content)]
    version = max(versions) + 1
    closing = content.rfind("\n)")
    if closing < 0:
        raise RuntimeError("MIGRATIONS tuple close not found")
    prefix = content[:closing].rstrip()
    if not prefix.endswith(","):
        prefix += ","
    write(path, prefix + "\n" + MIGRATION.format(version=version) + content[closing:])
    write("services/api/src/finance_agent/connectors/telegram_live.py", TELEGRAM_LIVE)
    write("scripts/telegram_live_poll.py", CLI)


def update_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.connectors import TelegramConfig, TelegramFixtureIngestor\n"
    imports = (
        "from finance_agent.connectors.telegram_live import (\n"
        "    TelegramInboundService,\n"
        "    TelegramLiveAdapter,\n"
        "    TelegramLiveConfig,\n"
        ")\n"
    )
    if imports not in content:
        if marker not in content:
            raise RuntimeError("Telegram fixture import marker missing")
        content = content.replace(marker, marker + imports, 1)
    init_marker = '''        self.telegram = TelegramFixtureIngestor(
            TelegramConfig(allowed_chat_id=700001)
        )
'''
    if "self.telegram_live =" not in content:
        if init_marker not in content:
            raise RuntimeError("Telegram service init marker missing")
        content = content.replace(
            init_marker,
            init_marker
            + "        self.telegram_live = TelegramLiveAdapter(\n"
            + "            TelegramLiveConfig.from_env()\n"
            + "        )\n",
            1,
        )
    close_marker = "        await self.local_model.aclose()\n"
    if "await self.telegram_live.aclose()" not in content:
        if close_marker not in content:
            raise RuntimeError("service close marker missing")
        content = content.replace(
            close_marker,
            "        await self.telegram_live.aclose()\n" + close_marker,
            1,
        )
    capability_marker = '''                "telegram": {
                    "status": "fixture_only",
'''
    if capability_marker in content:
        replacement = '''                "telegram": {
                    "status": (
                        "configured" if self.telegram_live.capability()["configured"]
                        else "fixture_only"
                    ),
                    "mode": "inbound_only",
                    "sendSupported": False,
                    "fixtureAvailable": True,
                    "live": self.telegram_live.capability(),
'''
        content = content.replace(capability_marker, replacement, 1)
    write(path, content)
    insert_method_before(path, "LocalRouteServices", "egress_consent_status", SERVICE_METHOD)


def update_protocol_route_scripts() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def egress_consent_status(\n"
    addition = '''    async def poll_telegram_live(\n        self, *, workspace_id: str, limit: int\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("egress consent protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    marker = '    @router.get("/v1/workspaces/{workspace_id}/privacy/egress-consent")\n'
    if marker not in content:
        raise RuntimeError("egress consent route marker missing")
    content = content.replace(marker, ROUTE + marker, 1)
    write(path, content)

    path = "package.json"
    value = json.loads(read(path))
    scripts = value["scripts"]
    scripts["test:telegram-live"] = "uv run --project services/api python scripts/telegram_live_poll.py"
    write(path, json.dumps(value, indent=2) + "\n")


def add_tests_docs_env() -> None:
    write("services/api/tests/connectors/test_telegram_live_inbound.py", TESTS)
    path = ".env.example"
    content = read(path)
    addition = '''
TELEGRAM_RETRY_MAX_ATTEMPTS=4
TELEGRAM_RETRY_BASE_SECONDS=0.25
TELEGRAM_RETRY_MAX_SECONDS=30
TELEGRAM_RETRY_JITTER_RATIO=0.2
'''
    if "TELEGRAM_RETRY_MAX_ATTEMPTS" not in content:
        write(path, content.rstrip() + "\n" + addition)
    write("docs/TELEGRAM_INBOUND.md", '''# Telegram inbound-only connector\n\nFolio can explicitly poll one configured Telegram bot for messages from one allowlisted private chat. The bot token and raw chat ID are process-injected and never persisted. Update IDs are deduplicated, and the next offset advances only after the poll has processed or deliberately rejected the returned batch. Unauthorised message content is not stored.\n\nAccepted text and captions become source-linked correspondence in the local knowledge index. Supported attachments are downloaded within a 10 MB limit, hashed and retained locally. File IDs are hashed before persistence. Image attachments are stored as evidence but no OCR is performed or claimed. Executables and unsupported media fail closed.\n\nThe connector is inbound-only. It cannot send, reply, acknowledge or notify through Telegram. Running `pnpm test:telegram-live` is an explicit external action and requires configured credentials. CI uses only mock Telegram responses and does not prove a live bot round trip.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 26: authenticated inbound-only Telegram capture\n\n- One bot and one private chat are process-configured and allowlisted.\n- Update IDs are deduplicated and offsets advance only after batch handling.\n- Unauthorised message content is never stored.\n- Text and bounded attachments enter the source-linked local knowledge layer.\n- Attachment file IDs are hashed; unsupported media and oversized files fail closed.\n- Sending, replying, OCR and live-bot proof remain unclaimed.\n'''
    if "## Stack 26: authenticated inbound-only Telegram capture" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_and_module()
    update_services()
    update_protocol_route_scripts()
    add_tests_docs_env()
    print("Telegram inbound changes applied")


if __name__ == "__main__":
    main()
