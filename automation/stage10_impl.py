from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, value: str) -> None:
    destination = ROOT / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(value, encoding="utf-8")


def replace_once(value: str, old: str, new: str, *, label: str) -> str:
    count = value.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return value.replace(old, new, 1)


def patch_migrations() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    value = read(path)
    if 'name="telegram_live_ingestion"' in value:
        return
    addition = r'''
    Migration(
        version=26,
        name="telegram_live_ingestion",
        sql="""
        CREATE TABLE telegram_connections (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id),
            allowed_chat_id INTEGER NOT NULL,
            last_update_id INTEGER NOT NULL DEFAULT 0 CHECK (last_update_id >= 0),
            status TEXT NOT NULL CHECK (status IN ('configured', 'disabled', 'error')),
            last_polled_at TEXT,
            last_error_code TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE telegram_live_updates (
            update_id INTEGER PRIMARY KEY CHECK (update_id >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            text_content TEXT NOT NULL,
            payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
            source_item_id TEXT,
            evidence_id TEXT,
            document_id TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('accepted', 'duplicate', 'rejected_chat', 'rejected_payload')
            ),
            received_at TEXT NOT NULL
        );

        CREATE TABLE telegram_live_attachments (
            attachment_id TEXT PRIMARY KEY,
            update_id INTEGER NOT NULL REFERENCES telegram_live_updates(update_id),
            file_id_hash TEXT NOT NULL CHECK (length(file_id_hash) = 64),
            file_unique_id TEXT,
            media_type TEXT,
            size_bytes INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
            content_hash TEXT CHECK (content_hash IS NULL OR length(content_hash) = 64),
            relative_path TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('downloaded', 'metadata_only', 'quarantined')
            ),
            quarantine_reason TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX telegram_updates_workspace_received
            ON telegram_live_updates(workspace_id, received_at DESC, update_id DESC);
        CREATE INDEX telegram_attachments_update
            ON telegram_live_attachments(update_id, attachment_id);
        """,
    ),
'''
    stripped = value.rstrip()
    if not stripped.endswith(")"):
        raise RuntimeError("migrations.py does not end with the migration tuple")
    write(path, stripped[:-1] + addition + ")\n")


def create_telegram_module() -> None:
    write(
        "services/api/src/finance_agent/connectors/telegram_live.py",
        '''"""Config-gated Telegram polling and webhook ingestion with local evidence storage."""\n\nfrom __future__ import annotations\n\nimport hashlib\nimport hmac\nimport json\nimport os\nfrom collections.abc import Mapping, Sequence\nfrom dataclasses import dataclass, field\nfrom datetime import UTC, datetime\nfrom pathlib import Path, PurePosixPath\nfrom typing import Any\nfrom urllib.parse import urlparse\n\nimport httpx\n\nfrom finance_agent.connectors.base import ConnectorError\nfrom finance_agent.storage import SQLiteStore, canonical_json\n\nTELEGRAM_API_ORIGIN = "https://api.telegram.org"\nMAX_UPDATE_BYTES = 1_000_000\nMAX_ATTACHMENT_BYTES = 5_000_000\nMAX_UPDATES_PER_POLL = 100\nALLOWED_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "application/pdf"})\n\n\n@dataclass(frozen=True, slots=True)\nclass TelegramLiveConfig:\n    enabled: bool = False\n    bot_token: str | None = field(default=None, repr=False)\n    allowed_chat_id: int | None = None\n    webhook_secret: str | None = field(default=None, repr=False)\n    api_origin: str = TELEGRAM_API_ORIGIN\n    timeout_seconds: float = 30.0\n    attachment_root: Path = Path("var/telegram-attachments")\n\n    def __post_init__(self) -> None:\n        parsed = urlparse(self.api_origin)\n        if parsed.scheme != "https" or parsed.hostname != "api.telegram.org" or parsed.path not in {"", "/"}:\n            raise ValueError("Telegram credentials may only be sent to https://api.telegram.org")\n        if self.enabled and not (self.bot_token and self.allowed_chat_id is not None):\n            raise ValueError("enabled Telegram requires a bot token and allowed chat id")\n        if self.allowed_chat_id is not None and isinstance(self.allowed_chat_id, bool):\n            raise ValueError("Telegram allowed chat id must be an integer")\n\n    @classmethod\n    def from_env(cls, *, database_path: str | Path | None = None) -> TelegramLiveConfig:\n        raw_chat = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()\n        try:\n            chat_id = int(raw_chat) if raw_chat else None\n        except ValueError as exc:\n            raise ValueError("TELEGRAM_ALLOWED_CHAT_ID must be an integer") from exc\n        root = (\n            Path(database_path).expanduser().resolve().parent / "telegram-attachments"\n            if database_path and str(database_path) != ":memory:"\n            else Path("var/telegram-attachments").resolve()\n        )\n        return cls(\n            enabled=os.getenv("TELEGRAM_LIVE_ENABLED", "false").lower() == "true",\n            bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,\n            allowed_chat_id=chat_id,\n            webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET") or None,\n            attachment_root=root,\n        )\n\n\n@dataclass(frozen=True, slots=True)\nclass TelegramPollResult:\n    status: str\n    update_count: int\n    accepted_count: int\n    rejected_count: int\n    duplicate_count: int\n    last_update_id: int\n    external_calls_made: bool\n\n    def as_contract(self) -> dict[str, object]:\n        return {\n            "status": self.status,\n            "updateCount": self.update_count,\n            "acceptedCount": self.accepted_count,\n            "rejectedCount": self.rejected_count,\n            "duplicateCount": self.duplicate_count,\n            "lastUpdateId": self.last_update_id,\n            "externalCallsMade": self.external_calls_made,\n            "outboundMessagesSent": 0,\n        }\n\n\ndef _stable_id(prefix: str, *parts: str) -> str:\n    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]\n    return f"{prefix}_{digest}"\n\n\ndef _required_int(value: object, label: str) -> int:\n    if isinstance(value, bool) or not isinstance(value, int):\n        raise ConnectorError(f"Telegram {label} must be an integer")\n    return value\n\n\ndef _bounded_text(value: object, maximum: int = 4096) -> str:\n    if value is None:\n        return ""\n    if not isinstance(value, str):\n        raise ConnectorError("Telegram text content must be a string")\n    if len(value) > maximum:\n        raise ConnectorError("Telegram text content exceeds the local limit")\n    return value.strip()\n\n\ndef _payload_bytes(update: Mapping[str, object]) -> bytes:\n    try:\n        encoded = canonical_json(update).encode("utf-8")\n    except (TypeError, ValueError) as exc:\n        raise ConnectorError("Telegram update is not valid JSON data") from exc\n    if len(encoded) > MAX_UPDATE_BYTES:\n        raise ConnectorError("Telegram update exceeds the local payload limit")\n    return encoded\n\n\nclass TelegramBotApiAdapter:\n    def __init__(\n        self,\n        config: TelegramLiveConfig,\n        *,\n        client: httpx.AsyncClient | None = None,\n    ) -> None:\n        self.config = config\n        self._owns_client = client is None\n        self._client = client or httpx.AsyncClient(\n            timeout=httpx.Timeout(config.timeout_seconds, connect=10.0),\n            headers={"Accept": "application/json"},\n        )\n\n    async def aclose(self) -> None:\n        if self._owns_client:\n            await self._client.aclose()\n\n    def _enabled_token(self) -> str:\n        if not (self.config.enabled and self.config.bot_token):\n            raise ConnectorError("Telegram is disabled or unconfigured")\n        return self.config.bot_token\n\n    async def _post(self, method: str, payload: Mapping[str, object]) -> object:\n        token = self._enabled_token()\n        try:\n            response = await self._client.post(\n                f"{self.config.api_origin}/bot{token}/{method}", json=dict(payload)\n            )\n            response.raise_for_status()\n            body = response.json()\n        except (httpx.HTTPError, ValueError) as exc:\n            raise ConnectorError("Telegram Bot API request failed") from exc\n        if not isinstance(body, Mapping) or body.get("ok") is not True:\n            raise ConnectorError("Telegram Bot API returned an invalid response")\n        return body.get("result")\n\n    async def get_updates(self, *, offset: int) -> tuple[Mapping[str, object], ...]:\n        result = await self._post(\n            "getUpdates",\n            {\n                "offset": offset,\n                "limit": MAX_UPDATES_PER_POLL,\n                "timeout": 0,\n                "allowed_updates": ["message"],\n            },\n        )\n        if not isinstance(result, list):\n            raise ConnectorError("Telegram updates response did not contain a list")\n        return tuple(item for item in result if isinstance(item, Mapping))\n\n    async def get_file(self, file_id: str) -> Mapping[str, object]:\n        result = await self._post("getFile", {"file_id": file_id})\n        if not isinstance(result, Mapping):\n            raise ConnectorError("Telegram file response was incomplete")\n        return result\n\n    async def download_file(self, file_path: str) -> bytes:\n        token = self._enabled_token()\n        candidate = PurePosixPath(file_path)\n        if candidate.is_absolute() or ".." in candidate.parts or not candidate.parts:\n            raise ConnectorError("Telegram file path is invalid")\n        url = f"{self.config.api_origin}/file/bot{token}/{candidate.as_posix()}"\n        total = 0\n        chunks: list[bytes] = []\n        try:\n            async with self._client.stream("GET", url) as response:\n                response.raise_for_status()\n                declared = int(response.headers.get("content-length", "0") or "0")\n                if declared > MAX_ATTACHMENT_BYTES:\n                    raise ConnectorError("Telegram attachment exceeds the local size limit")\n                async for chunk in response.aiter_bytes():\n                    total += len(chunk)\n                    if total > MAX_ATTACHMENT_BYTES:\n                        raise ConnectorError("Telegram attachment exceeds the local size limit")\n                    chunks.append(chunk)\n        except ConnectorError:\n            raise\n        except (httpx.HTTPError, ValueError) as exc:\n            raise ConnectorError("Telegram attachment download failed") from exc\n        if total == 0:\n            raise ConnectorError("Telegram attachment is empty")\n        return b"".join(chunks)\n\n\nclass TelegramLiveIngestor:\n    def __init__(\n        self,\n        store: SQLiteStore,\n        config: TelegramLiveConfig,\n        adapter: TelegramBotApiAdapter,\n    ) -> None:\n        self.store = store\n        self.config = config\n        self.adapter = adapter\n        self.attachment_root = config.attachment_root.expanduser().resolve()\n        self.attachment_root.mkdir(parents=True, exist_ok=True)\n        try:\n            self.attachment_root.chmod(0o700)\n        except OSError:\n            pass\n\n    def ensure_connection(self, workspace_id: str) -> None:\n        if self.config.allowed_chat_id is None:\n            return\n        instant = datetime.now(UTC).isoformat()\n        with self.store.transaction() as connection:\n            connection.execute(\n                """\n                INSERT INTO telegram_connections(\n                    workspace_id, allowed_chat_id, last_update_id, status, updated_at\n                ) VALUES (?, ?, 0, ?, ?)\n                ON CONFLICT(workspace_id) DO UPDATE SET\n                    allowed_chat_id = excluded.allowed_chat_id,\n                    status = excluded.status,\n                    updated_at = excluded.updated_at\n                """,\n                (\n                    workspace_id, self.config.allowed_chat_id,\n                    "configured" if self.config.enabled else "disabled", instant,\n                ),\n            )\n\n    def capability(self) -> dict[str, object]:\n        return {\n            "provider": "telegram",\n            "configured": bool(\n                self.config.enabled and self.config.bot_token\n                and self.config.allowed_chat_id is not None\n            ),\n            "mode": "read_only_ingestion",\n            "pollingAvailable": True,\n            "webhookAvailable": bool(self.config.webhook_secret),\n            "outboundMessages": False,\n            "maxAttachmentBytes": MAX_ATTACHMENT_BYTES,\n        }\n\n    def verify_webhook_secret(self, supplied: str | None) -> None:\n        expected = self.config.webhook_secret\n        if not expected:\n            raise ConnectorError("Telegram webhook is disabled or unconfigured")\n        if supplied is None or not hmac.compare_digest(supplied, expected):\n            raise ConnectorError("Telegram webhook authentication failed")\n\n    @staticmethod\n    def _attachment_candidate(message: Mapping[str, object]) -> dict[str, object] | None:\n        photo = message.get("photo")\n        if isinstance(photo, list):\n            choices = [item for item in photo if isinstance(item, Mapping)]\n            if choices:\n                selected = max(choices, key=lambda item: int(item.get("file_size", 0) or 0))\n                return {\n                    "file_id": selected.get("file_id"),\n                    "file_unique_id": selected.get("file_unique_id"),\n                    "file_size": selected.get("file_size"),\n                    "media_type": "image/jpeg",\n                }\n        document = message.get("document")\n        if isinstance(document, Mapping):\n            return {\n                "file_id": document.get("file_id"),\n                "file_unique_id": document.get("file_unique_id"),\n                "file_size": document.get("file_size"),\n                "media_type": document.get("mime_type"),\n            }\n        return None\n\n    async def ingest_update(\n        self, workspace_id: str, update: Mapping[str, object], *, received_at: str | None = None\n    ) -> str:\n        raw = _payload_bytes(update)\n        update_id = _required_int(update.get("update_id"), "update_id")\n        existing = self.store.fetch_one(\n            "SELECT status FROM telegram_live_updates WHERE update_id = ?", (update_id,)\n        )\n        if existing is not None:\n            return "duplicate"\n        message = update.get("message")\n        if not isinstance(message, Mapping):\n            self._record_rejection(workspace_id, update_id, raw, "rejected_payload", received_at)\n            return "rejected_payload"\n        chat = message.get("chat")\n        if not isinstance(chat, Mapping):\n            self._record_rejection(workspace_id, update_id, raw, "rejected_payload", received_at)\n            return "rejected_payload"\n        chat_id = _required_int(chat.get("id"), "chat id")\n        if chat_id != self.config.allowed_chat_id:\n            self._record_rejection(\n                workspace_id, update_id, raw, "rejected_chat", received_at, chat_id=chat_id\n            )\n            return "rejected_chat"\n        sender = message.get("from")\n        if isinstance(sender, Mapping) and sender.get("is_bot") is True:\n            self._record_rejection(\n                workspace_id, update_id, raw, "rejected_payload", received_at, chat_id=chat_id\n            )\n            return "rejected_payload"\n        message_id = _required_int(message.get("message_id"), "message id")\n        occurred_unix = _required_int(message.get("date"), "message date")\n        occurred_at = datetime.fromtimestamp(occurred_unix, tz=UTC).isoformat()\n        text = _bounded_text(message.get("text") or message.get("caption"))\n        if not text and self._attachment_candidate(message) is None:\n            self._record_rejection(\n                workspace_id, update_id, raw, "rejected_payload", received_at, chat_id=chat_id,\n                message_id=message_id, occurred_at=occurred_at,\n            )\n            return "rejected_payload"\n\n        attachment = self._attachment_candidate(message)\n        attachment_record: dict[str, object] | None = None\n        if attachment is not None:\n            attachment_record = await self._prepare_attachment(update_id, attachment)\n        received = received_at or datetime.now(UTC).isoformat()\n        digest = hashlib.sha256(raw).hexdigest()\n        source_item_id = _stable_id("src", "telegram_live", str(update_id), digest)\n        evidence_id = _stable_id("evd", "telegram_live", str(update_id), digest)\n        document_id = _stable_id("doc", "telegram_live", str(update_id), digest)\n        metadata = {\n            "provider": "telegram",\n            "updateId": update_id,\n            "chatIdHash": hashlib.sha256(str(chat_id).encode()).hexdigest(),\n            "messageId": message_id,\n            "attachment": attachment_record,\n        }\n        record_hash = hashlib.sha256(\n            canonical_json({\n                "workspaceId": workspace_id, "documentId": document_id,\n                "content": text, "metadata": metadata,\n            }).encode()\n        ).hexdigest()\n        with self.store.transaction() as connection:\n            connection.execute(\n                """\n                INSERT INTO source_items(\n                    source_item_id, workspace_id, source_type, label, digest,\n                    mapping_version, received_at, status, row_count\n                ) VALUES (?, ?, 'telegram_fixture', ?, ?, 'telegram_live@1', ?, 'processed', 1)\n                """,\n                (\n                    source_item_id, workspace_id,\n                    f"Telegram live message {message_id}", digest, received,\n                ),\n            )\n            connection.execute(\n                """\n                INSERT INTO evidence_links(\n                    evidence_id, workspace_id, source_item_id, source_row_id, label, created_at\n                ) VALUES (?, ?, ?, NULL, ?, ?)\n                """,\n                (evidence_id, workspace_id, source_item_id, "Telegram owner message", received),\n            )\n            connection.execute(\n                """\n                INSERT INTO knowledge_documents(\n                    document_id, workspace_id, document_kind, title, task_scope,\n                    source_kind, source_ref, source_turn_id, evidence_id, received_at,\n                    effective_from, effective_until, extracted_text, content_hash,\n                    metadata_json, record_hash\n                ) VALUES (?, ?, 'correspondence', ?, 'business_context', 'connector', ?,\n                    NULL, ?, ?, ?, NULL, ?, ?, ?, ?)\n                """,\n                (\n                    document_id, workspace_id, f"Telegram message {message_id}",\n                    f"telegram:update:{update_id}", evidence_id, received, occurred_at, text,\n                    hashlib.sha256(text.encode()).hexdigest(), canonical_json(metadata), record_hash,\n                ),\n            )\n            connection.execute(\n                """\n                INSERT INTO telegram_live_updates(\n                    update_id, workspace_id, chat_id, message_id, occurred_at, text_content,\n                    payload_hash, source_item_id, evidence_id, document_id, status, received_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'accepted', ?)\n                """,\n                (\n                    update_id, workspace_id, chat_id, message_id, occurred_at, text, digest,\n                    source_item_id, evidence_id, document_id, received,\n                ),\n            )\n            if attachment_record is not None:\n                connection.execute(\n                    """\n                    INSERT INTO telegram_live_attachments(\n                        attachment_id, update_id, file_id_hash, file_unique_id, media_type,\n                        size_bytes, content_hash, relative_path, status, quarantine_reason,\n                        created_at\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    """,\n                    (\n                        attachment_record["attachmentId"], update_id,\n                        attachment_record["fileIdHash"], attachment_record.get("fileUniqueId"),\n                        attachment_record.get("mediaType"), attachment_record.get("sizeBytes"),\n                        attachment_record.get("contentHash"), attachment_record.get("relativePath"),\n                        attachment_record["status"], attachment_record.get("quarantineReason"),\n                        received,\n                    ),\n                )\n        return "accepted"\n\n    def _record_rejection(\n        self, workspace_id: str, update_id: int, raw: bytes, status: str,\n        received_at: str | None, *, chat_id: int = 0, message_id: int = 0,\n        occurred_at: str | None = None\n    ) -> None:\n        received = received_at or datetime.now(UTC).isoformat()\n        with self.store.transaction() as connection:\n            connection.execute(\n                """\n                INSERT INTO telegram_live_updates(\n                    update_id, workspace_id, chat_id, message_id, occurred_at, text_content,\n                    payload_hash, source_item_id, evidence_id, document_id, status, received_at\n                ) VALUES (?, ?, ?, ?, ?, '', ?, NULL, NULL, NULL, ?, ?)\n                """,\n                (\n                    update_id, workspace_id, chat_id, message_id,\n                    occurred_at or received, hashlib.sha256(raw).hexdigest(), status, received,\n                ),\n            )\n\n    async def _prepare_attachment(\n        self, update_id: int, attachment: Mapping[str, object]\n    ) -> dict[str, object]:\n        file_id = attachment.get("file_id")\n        if not isinstance(file_id, str) or not file_id:\n            return self._quarantined_attachment(update_id, "missing_file_id", attachment)\n        media_type = attachment.get("media_type")\n        if not isinstance(media_type, str) or media_type not in ALLOWED_MEDIA_TYPES:\n            return self._quarantined_attachment(update_id, "unsupported_media_type", attachment)\n        declared_size = attachment.get("file_size")\n        if isinstance(declared_size, bool) or (declared_size is not None and not isinstance(declared_size, int)):\n            return self._quarantined_attachment(update_id, "invalid_size", attachment)\n        if isinstance(declared_size, int) and declared_size > MAX_ATTACHMENT_BYTES:\n            return self._quarantined_attachment(update_id, "declared_size_limit", attachment)\n        try:\n            file_info = await self.adapter.get_file(file_id)\n            file_path = file_info.get("file_path")\n            if not isinstance(file_path, str):\n                raise ConnectorError("Telegram file response omitted file_path")\n            content = await self.adapter.download_file(file_path)\n        except ConnectorError:\n            return self._quarantined_attachment(update_id, "download_failed", attachment)\n        digest = hashlib.sha256(content).hexdigest()\n        extension = {\n            "image/jpeg": ".jpg", "image/png": ".png", "application/pdf": ".pdf"\n        }[media_type]\n        relative_path = f"telegram-{update_id}-{digest[:20]}{extension}"\n        final_path = self.attachment_root / relative_path\n        temporary_path = self.attachment_root / f".{relative_path}.tmp"\n        temporary_path.write_bytes(content)\n        os.replace(temporary_path, final_path)\n        try:\n            final_path.chmod(0o600)\n        except OSError:\n            pass\n        return {\n            "attachmentId": _stable_id("tgatt", str(update_id), digest),\n            "fileIdHash": hashlib.sha256(file_id.encode()).hexdigest(),\n            "fileUniqueId": attachment.get("file_unique_id"),\n            "mediaType": media_type,\n            "sizeBytes": len(content),\n            "contentHash": digest,\n            "relativePath": relative_path,\n            "status": "downloaded",\n            "quarantineReason": None,\n        }\n\n    @staticmethod\n    def _quarantined_attachment(\n        update_id: int, reason: str, attachment: Mapping[str, object]\n    ) -> dict[str, object]:\n        file_id = str(attachment.get("file_id") or "missing")\n        return {\n            "attachmentId": _stable_id("tgatt", str(update_id), file_id, reason),\n            "fileIdHash": hashlib.sha256(file_id.encode()).hexdigest(),\n            "fileUniqueId": attachment.get("file_unique_id"),\n            "mediaType": attachment.get("media_type"),\n            "sizeBytes": attachment.get("file_size") if isinstance(attachment.get("file_size"), int) else None,\n            "contentHash": None,\n            "relativePath": None,\n            "status": "quarantined",\n            "quarantineReason": reason,\n        }\n\n    async def poll(self, workspace_id: str) -> TelegramPollResult:\n        if not self.config.enabled:\n            raise ConnectorError("Telegram is disabled or unconfigured")\n        self.ensure_connection(workspace_id)\n        row = self.store.fetch_one(\n            "SELECT last_update_id FROM telegram_connections WHERE workspace_id = ?",\n            (workspace_id,),\n        )\n        last_update_id = int(row["last_update_id"]) if row else 0\n        updates = await self.adapter.get_updates(offset=last_update_id + 1)\n        if len(updates) > MAX_UPDATES_PER_POLL:\n            raise ConnectorError("Telegram poll exceeded the local update limit")\n        accepted = rejected = duplicates = 0\n        maximum = last_update_id\n        for update in sorted(updates, key=lambda item: int(item.get("update_id", 0) or 0)):\n            update_id = _required_int(update.get("update_id"), "update_id")\n            maximum = max(maximum, update_id)\n            status = await self.ingest_update(workspace_id, update)\n            if status == "accepted":\n                accepted += 1\n            elif status == "duplicate":\n                duplicates += 1\n            else:\n                rejected += 1\n        instant = datetime.now(UTC).isoformat()\n        with self.store.transaction() as connection:\n            connection.execute(\n                """\n                UPDATE telegram_connections\n                SET last_update_id = ?, status = 'configured', last_polled_at = ?,\n                    last_error_code = NULL, updated_at = ?\n                WHERE workspace_id = ?\n                """,\n                (maximum, instant, instant, workspace_id),\n            )\n        return TelegramPollResult(\n            status="completed", update_count=len(updates), accepted_count=accepted,\n            rejected_count=rejected, duplicate_count=duplicates,\n            last_update_id=maximum, external_calls_made=True,\n        )\n''',
    )


def patch_session_auth() -> None:
    path = "services/api/src/finance_agent/api/session_auth.py"
    value = read(path)
    value = replace_once(
        value,
        """        protected_prefix: str = \"/v1\",\n    ) -> None:\n""",
        """        protected_prefix: str = \"/v1\",\n        exempt_paths: tuple[str, ...] = (\"/v1/connectors/telegram/webhook\",),\n    ) -> None:\n""",
        label="session auth webhook exemption signature",
    )
    value = replace_once(
        value,
        """        self.protected_prefix = protected_prefix\n""",
        """        self.protected_prefix = protected_prefix\n        self.exempt_paths = frozenset(exempt_paths)\n""",
        label="session auth webhook exemptions",
    )
    value = replace_once(
        value,
        """            or str(scope.get(\"path\", \"\")) == \"/health\"\n            or not str(scope.get(\"path\", \"\")).startswith(self.protected_prefix)\n""",
        """            or str(scope.get(\"path\", \"\")) == \"/health\"\n            or str(scope.get(\"path\", \"\")) in self.exempt_paths\n            or not str(scope.get(\"path\", \"\")).startswith(self.protected_prefix)\n""",
        label="session webhook path exemption",
    )
    write(path, value)


def patch_route_protocol() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    value = read(path)
    anchor = """    async def data_inventory(self, workspace_id: str) -> Mapping[str, object]: ...\n"""
    methods = """    async def poll_telegram_live(self, workspace_id: str) -> Mapping[str, object]: ...\n\n    async def ingest_telegram_webhook(\n        self, *, update: Mapping[str, object], secret_token: str | None\n    ) -> Mapping[str, object]: ...\n\n"""
    if anchor not in value:
        raise RuntimeError("data inventory protocol anchor is missing")
    write(path, value.replace(anchor, methods + anchor, 1))


def patch_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/router.py"
    value = read(path)
    route_anchor = '''    @router.get("/v1/data/inventory")\n'''
    routes = '''    @router.post("/v1/connectors/telegram/poll")\n    async def poll_telegram_live(\n        body: SchedulerTickRequest,\n        services: Services,\n    ) -> dict[str, object]:\n        try:\n            return dict(await services.poll_telegram_live(body.workspace_id))\n        except ConnectorError as exc:\n            status = 409 if "disabled or unconfigured" in str(exc) else 502\n            raise HTTPException(status_code=status, detail=str(exc)) from exc\n\n    @router.post("/v1/connectors/telegram/webhook", status_code=202)\n    async def ingest_telegram_webhook(\n        body: dict[str, Any],\n        services: Services,\n        secret_token: Annotated[\n            str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")\n        ] = None,\n    ) -> dict[str, object]:\n        try:\n            return dict(\n                await services.ingest_telegram_webhook(\n                    update=body, secret_token=secret_token\n                )\n            )\n        except ConnectorError as exc:\n            status = 401 if "authentication failed" in str(exc) else 409\n            raise HTTPException(status_code=status, detail=str(exc)) from exc\n\n'''
    if route_anchor not in value:
        raise RuntimeError("data inventory route anchor is missing")
    write(path, value.replace(route_anchor, routes + route_anchor, 1))


def patch_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    value = read(path)
    value = replace_once(
        value,
        "from finance_agent.connectors import TelegramConfig, TelegramFixtureIngestor\n",
        "from finance_agent.connectors import TelegramConfig, TelegramFixtureIngestor\nfrom finance_agent.connectors.telegram_live import (\n    TelegramBotApiAdapter, TelegramLiveConfig, TelegramLiveIngestor,\n)\n",
        label="live Telegram imports",
    )
    value = replace_once(
        value,
        """        self.telegram = TelegramFixtureIngestor(\n            TelegramConfig(allowed_chat_id=700001)\n        )\n""",
        """        self.telegram = TelegramFixtureIngestor(\n            TelegramConfig(allowed_chat_id=700001)\n        )\n        self.telegram_live_config = TelegramLiveConfig.from_env(\n            database_path=self.store.database_path\n        )\n        self.telegram_live_adapter = TelegramBotApiAdapter(self.telegram_live_config)\n        self.telegram_live = TelegramLiveIngestor(\n            self.store, self.telegram_live_config, self.telegram_live_adapter\n        )\n""",
        label="live Telegram composition",
    )
    value = replace_once(
        value,
        """        self.data_control.ensure_default(WORKSPACE_ID)\n""",
        """        self.data_control.ensure_default(WORKSPACE_ID)\n        self.telegram_live.ensure_connection(WORKSPACE_ID)\n""",
        label="live Telegram connection",
    )
    anchor = """    async def data_inventory(self, workspace_id: str) -> Mapping[str, object]:\n"""
    methods = '''    async def poll_telegram_live(self, workspace_id: str) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        result = await self.telegram_live.poll(workspace_id)\n        self.working_understanding.ensure_current(workspace_id=workspace_id)\n        return result.as_contract()\n\n    async def ingest_telegram_webhook(\n        self, *, update: Mapping[str, object], secret_token: str | None\n    ) -> Mapping[str, object]:\n        self.telegram_live.verify_webhook_secret(secret_token)\n        status = await self.telegram_live.ingest_update(WORKSPACE_ID, update)\n        if status == "accepted":\n            self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)\n        update_id = update.get("update_id")\n        return {\n            "status": status,\n            "updateId": update_id if isinstance(update_id, int) else None,\n            "outboundMessagesSent": 0,\n        }\n\n'''
    if anchor not in value:
        raise RuntimeError("data inventory method anchor is missing")
    value = value.replace(anchor, methods + anchor, 1)

    capability_anchor = '''                "demo": {\n'''
    telegram_capability = '''                "telegram": {\n                    "status": (\n                        "configured"\n                        if self.telegram_live.capability()["configured"]\n                        else "unconfigured"\n                    ),\n                    "mode": "read_only_ingestion",\n                    "markets": ["global"],\n                    "pollingAvailable": True,\n                    "webhookAvailable": self.telegram_live.capability()["webhookAvailable"],\n                    "outboundMessages": False,\n                },\n'''
    if capability_anchor not in value:
        raise RuntimeError("connection capability provider anchor is missing")
    value = value.replace(capability_anchor, telegram_capability + capability_anchor, 1)

    close_anchor = """        await self.plaid.aclose()\n"""
    if close_anchor not in value:
        raise RuntimeError("connector close anchor is missing")
    value = value.replace(
        close_anchor,
        close_anchor + "        await self.telegram_live_adapter.aclose()\n",
        1,
    )
    write(path, value)


def create_polling_cli() -> None:
    write(
        "scripts/telegram_control.py",
        '''from __future__ import annotations\n\nimport argparse\nimport asyncio\nimport json\nimport os\nimport signal\n\nfrom finance_agent.api.services import LocalRouteServices, WORKSPACE_ID\n\n\nasync def main() -> int:\n    parser = argparse.ArgumentParser(description="Operate Folio's read-only Telegram ingestion")\n    subparsers = parser.add_subparsers(dest="command", required=True)\n    subparsers.add_parser("poll-once")\n    serve = subparsers.add_parser("serve")\n    serve.add_argument("--interval", type=float, default=20.0)\n    arguments = parser.parse_args()\n    database = os.getenv("FINANCE_DATABASE_PATH", "var/finance-agent.sqlite3")\n    services = LocalRouteServices(database, auto_seed=False)\n    try:\n        if arguments.command == "poll-once":\n            print(json.dumps(await services.poll_telegram_live(WORKSPACE_ID), indent=2))\n            return 0\n        if arguments.interval < 5 or arguments.interval > 300:\n            parser.error("serve interval must be between 5 and 300 seconds")\n        stop = asyncio.Event()\n        loop = asyncio.get_running_loop()\n        for signal_name in (signal.SIGINT, signal.SIGTERM):\n            try:\n                loop.add_signal_handler(signal_name, stop.set)\n            except NotImplementedError:\n                pass\n        while not stop.is_set():\n            try:\n                result = await services.poll_telegram_live(WORKSPACE_ID)\n                if int(result.get("updateCount", 0)):\n                    print(json.dumps(result, separators=(",", ":")), flush=True)\n            except Exception:\n                print(json.dumps({\n                    "status": "failed", "code": "telegram_poll_failed",\n                    "retryable": True,\n                }), flush=True)\n            try:\n                await asyncio.wait_for(stop.wait(), timeout=arguments.interval)\n            except TimeoutError:\n                pass\n        return 0\n    finally:\n        await services.aclose()\n\n\nif __name__ == "__main__":\n    raise SystemExit(asyncio.run(main()))\n''',
    )


def patch_launcher() -> None:
    path = "run"
    value = read(path)
    value = replace_once(
        value,
        """SCHEDULER_PID_FILE=\"${PID_DIR}/scheduler.pid\"\n""",
        """SCHEDULER_PID_FILE=\"${PID_DIR}/scheduler.pid\"\nTELEGRAM_LOG=\"${LOG_DIR}/telegram.log\"\nTELEGRAM_PID_FILE=\"${PID_DIR}/telegram.pid\"\n""",
        label="Telegram launcher paths",
    )
    value = replace_once(
        value,
        """  stop_pidfile \"$SCHEDULER_PID_FILE\" \"scheduler\"\n""",
        """  stop_pidfile \"$SCHEDULER_PID_FILE\" \"scheduler\"\n  stop_pidfile \"$TELEGRAM_PID_FILE\" \"Telegram ingestion\"\n""",
        label="stop Telegram process",
    )
    anchor = """wait_for() {\n"""
    function = '''start_telegram_ingestion() {\n  if [[ "${TELEGRAM_LIVE_ENABLED:-false}" != "true" ]]; then\n    return 0\n  fi\n  if [[ -f "$TELEGRAM_PID_FILE" ]]; then\n    local telegram_pid\n    telegram_pid="$(cat "$TELEGRAM_PID_FILE" 2>/dev/null || true)"\n    if [[ -n "$telegram_pid" ]] && kill -0 "$telegram_pid" 2>/dev/null; then\n      log "Telegram ingestion already running (pid ${telegram_pid})"\n      return 0\n    fi\n    rm -f "$TELEGRAM_PID_FILE"\n  fi\n  : >"$TELEGRAM_LOG"\n  log "Starting read-only Telegram ingestion…"\n  nohup uv run --project services/api python scripts/telegram_control.py serve \\\n    >>"$TELEGRAM_LOG" 2>&1 &\n  echo $! >"$TELEGRAM_PID_FILE"\n}\n\n'''
    if anchor not in value:
        raise RuntimeError("launcher wait_for anchor is missing")
    value = value.replace(anchor, function + anchor, 1)
    value = replace_once(
        value,
        """start_scheduler\n\n""",
        """start_scheduler\nstart_telegram_ingestion\n\n""",
        label="start Telegram worker",
    )
    write(path, value)


def patch_env_and_scripts() -> None:
    env_path = ".env.example"
    value = read(env_path)
    if "TELEGRAM_WEBHOOK_SECRET=" not in value:
        value = value.replace(
            "TELEGRAM_ALLOWED_CHAT_ID=\n",
            "TELEGRAM_ALLOWED_CHAT_ID=\n# Required only for the explicit webhook route. Use Telegram's secret_token value.\nTELEGRAM_WEBHOOK_SECRET=\n",
        )
    write(env_path, value)
    package_path = "package.json"
    package = json.loads(read(package_path))
    package["scripts"]["telegram:poll-once"] = "uv run --project services/api python scripts/telegram_control.py poll-once"
    package["scripts"]["telegram:serve"] = "uv run --project services/api python scripts/telegram_control.py serve"
    write(package_path, json.dumps(package, indent=2) + "\n")


def add_docs() -> None:
    write(
        "docs/TELEGRAM_LIVE.md",
        '''# Telegram live ingestion\n\nTelegram is an optional, read-only source of owner context.\n\n- Polling is disabled unless `TELEGRAM_LIVE_ENABLED=true`, a bot token is injected and one chat ID is allowlisted.\n- The optional webhook route is exempt from the Folio desktop session only because it requires Telegram's exact `X-Telegram-Bot-Api-Secret-Token`.\n- Updates from other chats, bot senders, unsupported payloads and replayed update IDs are rejected before their message content becomes business knowledge.\n- JPEG, PNG and PDF attachments are bounded to 5 MB, downloaded only from Telegram's pinned HTTPS origin and stored with private local permissions. Unsupported or failed attachments are quarantined.\n- Folio stores token hashes or provider identifiers only where needed. It never stores or returns the bot token.\n- No messages, payments or other outbound Telegram actions are implemented.\n\nA public webhook still requires an owner-managed HTTPS reverse proxy or tunnel. Folio itself remains loopback-only.\n''',
    )


def add_tests() -> None:
    write(
        "services/api/tests/connectors/test_telegram_live.py",
        '''from __future__ import annotations\n\nimport hashlib\nimport os\nfrom pathlib import Path\n\nimport httpx\nimport pytest\n\nfrom finance_agent.connectors.base import ConnectorError\nfrom finance_agent.connectors.telegram_live import (\n    MAX_ATTACHMENT_BYTES, TelegramBotApiAdapter, TelegramLiveConfig,\n    TelegramLiveIngestor,\n)\nfrom finance_agent.finance import FinanceEngine\nfrom finance_agent.storage import SQLiteStore\n\nROOT = Path(__file__).resolve().parents[4]\nCSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"\n\n\ndef update(update_id: int = 100, chat_id: int = 700001, *, with_photo: bool = False):\n    message = {\n        "message_id": 50, "date": 1784214000,\n        "from": {"id": chat_id, "is_bot": False, "first_name": "Owner"},\n        "chat": {"id": chat_id, "type": "private"},\n        "caption" if with_photo else "text": "Parking for the client fit-out.",\n    }\n    if with_photo:\n        message["photo"] = [{\n            "file_id": "file_photo_1", "file_unique_id": "unique_photo_1",\n            "width": 100, "height": 100, "file_size": 4,\n        }]\n    return {"update_id": update_id, "message": message}\n\n\ndef compose(tmp_path: Path, handler):\n    store = SQLiteStore(tmp_path / "telegram.sqlite3")\n    engine = FinanceEngine(store)\n    engine.reset_demo(CSV)\n    config = TelegramLiveConfig(\n        enabled=True, bot_token="token-secret", allowed_chat_id=700001,\n        webhook_secret="webhook-secret", attachment_root=tmp_path / "attachments",\n    )\n    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))\n    adapter = TelegramBotApiAdapter(config, client=client)\n    ingestor = TelegramLiveIngestor(store, config, adapter)\n    ingestor.ensure_connection("ws_koru_studio")\n    return store, client, adapter, ingestor\n\n\ndef test_configuration_rejects_non_telegram_hosts(tmp_path: Path) -> None:\n    with pytest.raises(ValueError, match="only be sent"):\n        TelegramLiveConfig(\n            enabled=True, bot_token="token", allowed_chat_id=1,\n            api_origin="https://telegram.example.com", attachment_root=tmp_path,\n        )\n\n\n@pytest.mark.asyncio\nasync def test_poll_uses_cursor_accepts_allowlisted_chat_and_is_replay_safe(tmp_path: Path) -> None:\n    requests: list[httpx.Request] = []\n\n    def handler(request: httpx.Request) -> httpx.Response:\n        requests.append(request)\n        if request.url.path.endswith("/getUpdates"):\n            return httpx.Response(200, json={"ok": True, "result": [update()]})\n        raise AssertionError(request.url)\n\n    store, client, _adapter, ingestor = compose(tmp_path, handler)\n    first = await ingestor.poll("ws_koru_studio")\n    second = await ingestor.poll("ws_koru_studio")\n    assert first.accepted_count == 1\n    assert second.duplicate_count == 1\n    first_payload = __import__("json").loads(requests[0].content)\n    second_payload = __import__("json").loads(requests[1].content)\n    assert first_payload["offset"] == 1\n    assert second_payload["offset"] == 101\n    assert len(store.fetch_all("SELECT * FROM telegram_live_updates")) == 1\n    assert len(store.fetch_all("SELECT * FROM knowledge_documents WHERE source_kind = 'connector'")) == 1\n    await client.aclose()\n\n\n@pytest.mark.asyncio\nasync def test_wrong_chat_is_rejected_without_persisting_message_content(tmp_path: Path) -> None:\n    def handler(request: httpx.Request) -> httpx.Response:\n        return httpx.Response(200, json={"ok": True, "result": [update(chat_id=999)]})\n\n    store, client, _adapter, ingestor = compose(tmp_path, handler)\n    result = await ingestor.poll("ws_koru_studio")\n    assert result.rejected_count == 1\n    row = store.fetch_one("SELECT text_content, status FROM telegram_live_updates")\n    assert row is not None\n    assert str(row["status"]) == "rejected_chat"\n    assert str(row["text_content"]) == ""\n    assert not store.fetch_all("SELECT * FROM knowledge_documents WHERE source_kind = 'connector'")\n    await client.aclose()\n\n\n@pytest.mark.asyncio\nasync def test_attachment_download_is_pinned_bounded_hashed_and_private(tmp_path: Path) -> None:\n    content = b"jpeg"\n\n    def handler(request: httpx.Request) -> httpx.Response:\n        if request.url.path.endswith("/getFile"):\n            return httpx.Response(200, json={\n                "ok": True, "result": {"file_path": "photos/file_photo_1.jpg"},\n            })\n        if "/file/bottoken-secret/photos/file_photo_1.jpg" in request.url.path:\n            return httpx.Response(200, content=content, headers={"Content-Length": str(len(content))})\n        raise AssertionError(request.url)\n\n    store, client, _adapter, ingestor = compose(tmp_path, handler)\n    status = await ingestor.ingest_update("ws_koru_studio", update(with_photo=True))\n    assert status == "accepted"\n    row = store.fetch_one("SELECT * FROM telegram_live_attachments")\n    assert row is not None\n    assert str(row["status"]) == "downloaded"\n    assert str(row["content_hash"]) == hashlib.sha256(content).hexdigest()\n    path = tmp_path / "attachments" / str(row["relative_path"])\n    assert path.read_bytes() == content\n    if os.name == "posix":\n        assert path.stat().st_mode & 0o777 == 0o600\n    await client.aclose()\n\n\n@pytest.mark.asyncio\nasync def test_declared_oversize_attachment_is_quarantined_without_download(tmp_path: Path) -> None:\n    calls = 0\n\n    def handler(request: httpx.Request) -> httpx.Response:\n        nonlocal calls\n        calls += 1\n        raise AssertionError("oversize attachment should not make a provider call")\n\n    store, client, _adapter, ingestor = compose(tmp_path, handler)\n    payload = update(with_photo=True)\n    payload["message"]["photo"][0]["file_size"] = MAX_ATTACHMENT_BYTES + 1\n    assert await ingestor.ingest_update("ws_koru_studio", payload) == "accepted"\n    row = store.fetch_one("SELECT status, quarantine_reason FROM telegram_live_attachments")\n    assert row is not None\n    assert dict(row) == {"status": "quarantined", "quarantine_reason": "declared_size_limit"}\n    assert calls == 0\n    await client.aclose()\n\n\ndef test_webhook_secret_is_exact_and_not_returned(tmp_path: Path) -> None:\n    _store, client, _adapter, ingestor = compose(\n        tmp_path, lambda request: httpx.Response(500)\n    )\n    ingestor.verify_webhook_secret("webhook-secret")\n    with pytest.raises(ConnectorError, match="authentication failed"):\n        ingestor.verify_webhook_secret("wrong")\n    assert "webhook-secret" not in str(ingestor.capability())\n    __import__("asyncio").run(client.aclose())\n''',
    )
    write(
        "services/api/tests/api/test_telegram_webhook_security.py",
        '''from __future__ import annotations\n\nfrom pathlib import Path\n\nfrom fastapi.testclient import TestClient\n\nfrom finance_agent.api.app import create_app\n\n\ndef test_webhook_is_exempt_from_desktop_session_but_requires_telegram_secret(\n    tmp_path: Path, monkeypatch\n) -> None:\n    monkeypatch.setenv("FOLIO_SESSION_TOKEN", "desktop-session")\n    monkeypatch.setenv("TELEGRAM_LIVE_ENABLED", "true")\n    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot-token")\n    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_ID", "700001")\n    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "telegram-secret")\n    app = create_app(database_path=tmp_path / "webhook.sqlite3", auto_seed=True)\n    update = {\n        "update_id": 800,\n        "message": {\n            "message_id": 80, "date": 1784214000,\n            "from": {"id": 700001, "is_bot": False, "first_name": "Owner"},\n            "chat": {"id": 700001, "type": "private"},\n            "text": "Client context from Telegram",\n        },\n    }\n    with TestClient(app) as client:\n        missing = client.post("/v1/connectors/telegram/webhook", json=update)\n        accepted = client.post(\n            "/v1/connectors/telegram/webhook", json=update,\n            headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},\n        )\n        ordinary = client.get("/v1/models/capabilities")\n    assert missing.status_code == 401\n    assert accepted.status_code == 202\n    assert ordinary.status_code == 401\n''',
    )


def main() -> None:
    patch_migrations()
    create_telegram_module()
    patch_session_auth()
    patch_route_protocol()
    patch_routes()
    patch_services()
    create_polling_cli()
    patch_launcher()
    patch_env_and_scripts()
    add_docs()
    add_tests()


if __name__ == "__main__":
    main()
