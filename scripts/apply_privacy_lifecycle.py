from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_method(path: str, class_name: str, name: str, replacement: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    candidate = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    if candidate.end_lineno is None:
        raise RuntimeError(f"{path}: method {class_name}.{name} has no end line")
    lines = content.splitlines(keepends=True)
    start = candidate.lineno - 1
    while start > 0 and lines[start - 1].lstrip().startswith("@"):
        start -= 1
    write(path, "".join(lines[:start]) + replacement.rstrip() + "\n\n" + "".join(lines[candidate.end_lineno:]))


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
        name="privacy_retention",
        sql="""
        CREATE TABLE privacy_settings (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id),
            model_receipt_days INTEGER NOT NULL DEFAULT 90
                CHECK (model_receipt_days BETWEEN 1 AND 3650),
            request_audit_days INTEGER NOT NULL DEFAULT 180
                CHECK (request_audit_days BETWEEN 1 AND 3650),
            scheduler_receipt_days INTEGER NOT NULL DEFAULT 365
                CHECK (scheduler_receipt_days BETWEEN 1 AND 3650),
            updated_at TEXT NOT NULL
        );

        CREATE TABLE privacy_action_receipts (
            receipt_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            action TEXT NOT NULL CHECK (action IN ('retention_purge')),
            occurred_at TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64)
        );

        CREATE INDEX privacy_action_receipts_workspace_time
            ON privacy_action_receipts(workspace_id, occurred_at, receipt_id);
        """,
    ),
'''

PRIVACY = '''"""Local privacy inventory, bounded retention, and owner-controlled destruction."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json
from finance_agent.storage.backups import WorkspaceBackupManager
from finance_agent.storage.encrypted_exports import encrypt_backup

PURGE_TABLES = {
    "model_runs": ("created_at", "model_receipt_days"),
    "egress_receipts": ("created_at", "model_receipt_days"),
    "request_audit_events": ("started_at", "request_audit_days"),
    "scheduler_receipts": ("tick_at", "scheduler_receipt_days"),
}
INVENTORY_TABLES = (
    "accounts",
    "source_items",
    "source_rows",
    "evidence_links",
    "transactions",
    "classification_rules",
    "finance_events",
    "conversation_turns",
    "claims",
    "knowledge_owner_statements",
    "knowledge_documents",
    "knowledge_entities",
    "knowledge_facts",
    "model_runs",
    "egress_receipts",
    "request_audit_events",
    "scheduler_receipts",
    "workspace_backups",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def destroyed_marker_path(database_path: str | Path) -> Path:
    path = Path(database_path).expanduser().resolve()
    return path.with_suffix(path.suffix + ".destroyed.json")


@dataclass(frozen=True, slots=True)
class RetentionSettings:
    workspace_id: str
    model_receipt_days: int
    request_audit_days: int
    scheduler_receipt_days: int

    def as_dict(self) -> dict[str, object]:
        return {
            "workspaceId": self.workspace_id,
            "modelReceiptDays": self.model_receipt_days,
            "requestAuditDays": self.request_audit_days,
            "schedulerReceiptDays": self.scheduler_receipt_days,
        }


class PrivacyManager:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def ensure_settings(self, workspace_id: str) -> RetentionSettings:
        now = _now().isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO privacy_settings(workspace_id, updated_at)
                VALUES (?, ?) ON CONFLICT(workspace_id) DO NOTHING
                """,
                (workspace_id, now),
            )
        return self.settings(workspace_id)

    def settings(self, workspace_id: str) -> RetentionSettings:
        row = self.store.fetch_one(
            "SELECT * FROM privacy_settings WHERE workspace_id = ?", (workspace_id,)
        )
        if row is None:
            return self.ensure_settings(workspace_id)
        return RetentionSettings(
            workspace_id=str(row["workspace_id"]),
            model_receipt_days=int(row["model_receipt_days"]),
            request_audit_days=int(row["request_audit_days"]),
            scheduler_receipt_days=int(row["scheduler_receipt_days"]),
        )

    def update_settings(
        self,
        workspace_id: str,
        *,
        model_receipt_days: int,
        request_audit_days: int,
        scheduler_receipt_days: int,
    ) -> RetentionSettings:
        for name, value in {
            "modelReceiptDays": model_receipt_days,
            "requestAuditDays": request_audit_days,
            "schedulerReceiptDays": scheduler_receipt_days,
        }.items():
            if not 1 <= value <= 3650:
                raise ValueError(f"{name} must be between 1 and 3650")
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO privacy_settings(
                    workspace_id, model_receipt_days, request_audit_days,
                    scheduler_receipt_days, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    model_receipt_days = excluded.model_receipt_days,
                    request_audit_days = excluded.request_audit_days,
                    scheduler_receipt_days = excluded.scheduler_receipt_days,
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_id,
                    model_receipt_days,
                    request_audit_days,
                    scheduler_receipt_days,
                    _now().isoformat(),
                ),
            )
        return self.settings(workspace_id)

    def inventory(self, workspace_id: str) -> dict[str, object]:
        counts: dict[str, int] = {}
        for table in INVENTORY_TABLES:
            row = self.store.fetch_one(
                f"SELECT COUNT(*) AS count FROM {table} WHERE workspace_id = ?",
                (workspace_id,),
            )
            counts[table] = int(row["count"]) if row is not None else 0
        database_path = Path(self.store.database_path)
        return {
            "workspaceId": workspace_id,
            "records": counts,
            "databaseBytes": database_path.stat().st_size if database_path.exists() else 0,
            "financeRetentionAutomatic": False,
            "purgeableClasses": sorted(PURGE_TABLES),
        }

    def purge_preview(
        self, workspace_id: str, *, now: datetime | None = None
    ) -> dict[str, object]:
        settings = self.settings(workspace_id)
        current = (now or _now()).astimezone(UTC)
        values = settings.as_dict()
        counts: dict[str, int] = {}
        cutoffs: dict[str, str] = {}
        for table, (date_column, setting_name) in PURGE_TABLES.items():
            days = int(values[
                {
                    "model_receipt_days": "modelReceiptDays",
                    "request_audit_days": "requestAuditDays",
                    "scheduler_receipt_days": "schedulerReceiptDays",
                }[setting_name]
            ])
            cutoff = (current - timedelta(days=days)).isoformat()
            row = self.store.fetch_one(
                f"SELECT COUNT(*) AS count FROM {table} WHERE workspace_id = ? AND {date_column} < ?",
                (workspace_id, cutoff),
            )
            counts[table] = int(row["count"]) if row is not None else 0
            cutoffs[table] = cutoff
        return {
            "workspaceId": workspace_id,
            "counts": counts,
            "cutoffs": cutoffs,
            "financeRecordsIncluded": False,
        }

    def purge(self, workspace_id: str, *, now: datetime | None = None) -> dict[str, object]:
        preview = self.purge_preview(workspace_id, now=now)
        occurred_at = (now or _now()).astimezone(UTC).isoformat()
        with self.store.transaction() as connection:
            connection.execute("PRAGMA secure_delete = ON")
            for table, (date_column, _) in PURGE_TABLES.items():
                connection.execute(
                    f"DELETE FROM {table} WHERE workspace_id = ? AND {date_column} < ?",
                    (workspace_id, preview["cutoffs"][table]),
                )
            receipt_id = _stable_id(
                "privacyrcpt", workspace_id, occurred_at, canonical_json(preview)
            )
            detail = {**preview, "vacuumed": True}
            encoded = canonical_json(detail)
            connection.execute(
                """
                INSERT INTO privacy_action_receipts(
                    receipt_id, workspace_id, action, occurred_at,
                    detail_json, content_hash
                ) VALUES (?, ?, 'retention_purge', ?, ?, ?)
                """,
                (
                    receipt_id,
                    workspace_id,
                    occurred_at,
                    encoded,
                    hashlib.sha256(encoded.encode()).hexdigest(),
                ),
            )
        with self.store.connect() as connection:
            connection.execute("VACUUM")
        return {
            "receiptId": receipt_id,
            "occurredAt": occurred_at,
            **preview,
            "vacuumed": True,
        }

    def destroy_workspace(
        self,
        workspace_id: str,
        *,
        confirmation: str,
        passphrase: str,
    ) -> dict[str, object]:
        expected = f"DELETE {workspace_id}"
        if confirmation != expected:
            raise ValueError(f"confirmation must exactly match {expected}")
        backup = WorkspaceBackupManager(self.store).create(workspace_id)
        encrypted = encrypt_backup(
            backup.content,
            passphrase=passphrase,
            backup_id=backup.backup_id,
            workspace_id=workspace_id,
        )
        database_path = Path(self.store.database_path).expanduser().resolve()
        export_dir = database_path.parent / "privacy-exports"
        export_dir.mkdir(parents=True, exist_ok=True)
        export_path = export_dir / f"{backup.backup_id}.folioenc"
        temporary = export_path.with_suffix(".tmp")
        temporary.write_bytes(encrypted.content)
        os.chmod(temporary, 0o600)
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, export_path)
        os.chmod(export_path, 0o600)
        destroyed_at = _now().isoformat()
        receipt = {
            "format": "folio.workspace-destruction@1",
            "workspaceId": workspace_id,
            "destroyedAt": destroyed_at,
            "backupId": backup.backup_id,
            "encryptedExport": export_path.name,
            "encryptedExportSha256": encrypted.sha256,
            "databaseSha256BeforeDestruction": backup.database_sha256,
        }
        marker = destroyed_marker_path(database_path)
        marker.write_text(canonical_json(receipt), encoding="utf-8")
        os.chmod(marker, 0o600)
        self.store.recreate()
        return receipt
'''

SERVICE_METHODS = '''    async def privacy_inventory(self, *, workspace_id: str) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return PrivacyManager(self.store).inventory(workspace_id)

    async def privacy_settings(self, *, workspace_id: str) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return PrivacyManager(self.store).settings(workspace_id).as_dict()

    async def update_privacy_settings(
        self,
        *,
        workspace_id: str,
        model_receipt_days: int,
        request_audit_days: int,
        scheduler_receipt_days: int,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            settings = PrivacyManager(self.store).update_settings(
                workspace_id,
                model_receipt_days=model_receipt_days,
                request_audit_days=request_audit_days,
                scheduler_receipt_days=scheduler_receipt_days,
            )
        return settings.as_dict()

    async def privacy_purge_preview(self, *, workspace_id: str) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return PrivacyManager(self.store).purge_preview(workspace_id)

    async def purge_expired_private_history(
        self, *, workspace_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return PrivacyManager(self.store).purge(workspace_id)

    async def destroy_workspace(
        self,
        *,
        workspace_id: str,
        confirmation: str,
        passphrase: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            receipt = PrivacyManager(self.store).destroy_workspace(
                workspace_id,
                confirmation=confirmation,
                passphrase=passphrase,
            )
            self._workspace_destroyed = True
            self.event_buffer.clear()
        return receipt
'''

INIT_REPLACEMENT = '''    def __init__(
        self,
        database_path: str | Path,
        *,
        auto_seed: bool = True,
        akahu_adapter: AkahuReadOnlyAdapter | None = None,
        plaid_adapter: PlaidReadOnlyAdapter | None = None,
    ) -> None:
        self.store = SQLiteStore(database_path)
        self.engine = FinanceEngine(self.store)
        self.engine.initialise()
        self._destroyed_marker = destroyed_marker_path(self.store.database_path)
        self._workspace_destroyed = self._destroyed_marker.exists()
        self.working_understanding = WorkingUnderstandingRuntime(self.store)
        self.finance_core = FinanceCoreAdapter(self.engine)
        self.daily_close = DailyCloseService(self.engine)
        self.scheduler = LocalScheduler(
            self.store, self.daily_close, workspace_id=WORKSPACE_ID
        )
        self.event_buffer = RunEventBuffer(retention=500)
        self.telegram = TelegramFixtureIngestor(
            TelegramConfig(allowed_chat_id=700001)
        )
        self.akahu = akahu_adapter or AkahuReadOnlyAdapter()
        self.plaid = plaid_adapter or PlaidReadOnlyAdapter()
        self.local_model = LMStudioAdapter(LMStudioConfig.from_env())
        self.cloud_model = OpenAIResponsesAdapter(OpenAIConfig.from_env())
        self.model_router = ModelModeRouter(self.local_model, self.cloud_model)
        self.receipts = SQLiteReceiptSink(self.store)
        self.current_mode = ModelMode.LOCAL
        self._lock = asyncio.Lock()
        self._scheduler_tick_lock = asyncio.Lock()
        self._active_turn_tasks: dict[str, asyncio.Task[Any]] = {}
        self._compose_controller()
        if auto_seed and not self._workspace_destroyed:
            self._ensure_seeded()
'''

HEALTH_REPLACEMENT = '''    async def health(self) -> Mapping[str, object]:
        database_ready = self.store.fetch_one("SELECT 1 AS ready") is not None
        workspace_state = "destroyed" if self._workspace_destroyed else "ready"
        return {
            "status": "ready" if database_ready else "degraded",
            "service": "standalone-finance-agent-api",
            "loopback": True,
            "database": "ready" if database_ready else "unavailable",
            "workspace": workspace_state,
            "workspaceId": None if self._workspace_destroyed else WORKSPACE_ID,
            "modelDiscoveryPath": "/v1/models/capabilities",
            "externalCalls": "disabled_by_default",
        }
'''

ROUTE_MODELS = '''

class PrivacySettingsRequest(RequestModel):
    model_receipt_days: int = Field(alias="modelReceiptDays", ge=1, le=3650)
    request_audit_days: int = Field(alias="requestAuditDays", ge=1, le=3650)
    scheduler_receipt_days: int = Field(alias="schedulerReceiptDays", ge=1, le=3650)


class DestroyWorkspaceRequest(RequestModel):
    confirmation: str = Field(min_length=1, max_length=200)
    passphrase: str = Field(min_length=12, max_length=1024)
'''

ROUTES = '''    @router.get("/v1/workspaces/{workspace_id}/privacy/inventory")
    async def privacy_inventory(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        return dict(await services.privacy_inventory(workspace_id=workspace_id))

    @router.get("/v1/workspaces/{workspace_id}/privacy/settings")
    async def privacy_settings(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        return dict(await services.privacy_settings(workspace_id=workspace_id))

    @router.post("/v1/workspaces/{workspace_id}/privacy/settings")
    async def update_privacy_settings(
        workspace_id: PathIdentifier,
        body: PrivacySettingsRequest,
        services: Services,
    ) -> dict[str, object]:
        return dict(
            await services.update_privacy_settings(
                workspace_id=workspace_id,
                model_receipt_days=body.model_receipt_days,
                request_audit_days=body.request_audit_days,
                scheduler_receipt_days=body.scheduler_receipt_days,
            )
        )

    @router.get("/v1/workspaces/{workspace_id}/privacy/purge-preview")
    async def privacy_purge_preview(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        return dict(await services.privacy_purge_preview(workspace_id=workspace_id))

    @router.post("/v1/workspaces/{workspace_id}/privacy/purge")
    async def purge_expired_private_history(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        return dict(
            await services.purge_expired_private_history(workspace_id=workspace_id)
        )

    @router.post("/v1/workspaces/{workspace_id}/destroy")
    async def destroy_workspace(
        workspace_id: PathIdentifier,
        body: DestroyWorkspaceRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.destroy_workspace(
                    workspace_id=workspace_id,
                    confirmation=body.confirmation,
                    passphrase=body.passphrase,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from finance_agent.api.services import LocalRouteServices
from finance_agent.storage import SQLiteStore
from finance_agent.storage.encrypted_exports import decrypt_backup
from finance_agent.storage.privacy import PrivacyManager, destroyed_marker_path


@pytest.mark.asyncio
async def test_retention_preview_and_purge_never_delete_finance_records(tmp_path: Path) -> None:
    database = tmp_path / "folio.sqlite3"
    services = LocalRouteServices(database, auto_seed=True)
    old = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    with services.store.transaction() as connection:
        connection.execute(
            "INSERT INTO model_runs(model_run_id, workspace_id, receipt_json, created_at) VALUES (?, ?, '{}', ?)",
            ("model_old", "ws_koru_studio", old),
        )
        connection.execute(
            "INSERT INTO request_audit_events(request_id, workspace_id, method, path, client_origin, started_at, status_code, completed_at) VALUES (?, ?, 'POST', '/v1/test', 'automation', ?, 200, ?)",
            ("req_old_privacy", "ws_koru_studio", old, old),
        )
    manager = PrivacyManager(services.store)
    before_transactions = len(services.store.fetch_all("SELECT * FROM transactions"))
    preview = manager.purge_preview("ws_koru_studio")
    assert preview["counts"]["model_runs"] == 1
    assert preview["counts"]["request_audit_events"] == 1
    assert preview["financeRecordsIncluded"] is False
    result = manager.purge("ws_koru_studio")
    assert result["counts"]["model_runs"] == 1
    assert services.store.fetch_one("SELECT 1 FROM model_runs WHERE model_run_id = 'model_old'") is None
    assert services.store.fetch_one("SELECT 1 FROM request_audit_events WHERE request_id = 'req_old_privacy'") is None
    assert len(services.store.fetch_all("SELECT * FROM transactions")) == before_transactions
    await services.aclose()


@pytest.mark.asyncio
async def test_workspace_destruction_requires_exact_confirmation_and_exports_encrypted_backup(tmp_path: Path) -> None:
    database = tmp_path / "folio.sqlite3"
    services = LocalRouteServices(database, auto_seed=True)
    with pytest.raises(ValueError, match="exactly"):
        await services.destroy_workspace(
            workspace_id="ws_koru_studio",
            confirmation="delete",
            passphrase="long enough owner passphrase",
        )
    receipt = await services.destroy_workspace(
        workspace_id="ws_koru_studio",
        confirmation="DELETE ws_koru_studio",
        passphrase="long enough owner passphrase",
    )
    marker = destroyed_marker_path(database)
    assert marker.exists()
    export_path = database.parent / "privacy-exports" / str(receipt["encryptedExport"])
    assert export_path.exists()
    archive, header = decrypt_backup(
        export_path.read_bytes(),
        passphrase="long enough owner passphrase",
        expected_workspace_id="ws_koru_studio",
    )
    assert archive.startswith(b"PK")
    assert header["backupId"] == receipt["backupId"]
    assert SQLiteStore(database).fetch_all("SELECT * FROM workspaces") == []
    health = await services.health()
    assert health["workspace"] == "destroyed"
    await services.aclose()

    reopened = LocalRouteServices(database, auto_seed=True)
    assert reopened.store.fetch_all("SELECT * FROM workspaces") == []
    assert (await reopened.health())["workspace"] == "destroyed"
    await reopened.aclose()
'''


def add_migration() -> None:
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


def add_privacy_module() -> None:
    write("services/api/src/finance_agent/storage/privacy.py", PRIVACY)
    path = "services/api/src/finance_agent/storage/store.py"
    content = read(path)
    if 'connection.execute("PRAGMA secure_delete = ON")' not in content:
        content = content.replace(
            '        connection.execute("PRAGMA foreign_keys = ON")\n',
            '        connection.execute("PRAGMA foreign_keys = ON")\n        connection.execute("PRAGMA secure_delete = ON")\n',
            1,
        )
        write(path, content)


def update_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.storage.encrypted_exports import decrypt_backup, encrypt_backup\n"
    import_line = "from finance_agent.storage.privacy import PrivacyManager, destroyed_marker_path\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("encrypted export import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    replace_method(path, "LocalRouteServices", "__init__", INIT_REPLACEMENT)
    replace_method(path, "LocalRouteServices", "health", HEALTH_REPLACEMENT)
    insert_method_before(path, "LocalRouteServices", "encrypted_workspace_backup_payload", SERVICE_METHODS)
    content = read(path)
    old = '''    async def reset_demo(self, workspace_id: str) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        async with self._lock:\n            imported = self.engine.reset_demo(DEMO_CSV)\n'''
    new = '''    async def reset_demo(self, workspace_id: str) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        async with self._lock:\n            self._destroyed_marker.unlink(missing_ok=True)\n            self._workspace_destroyed = False\n            imported = self.engine.reset_demo(DEMO_CSV)\n'''
    if old not in content:
        raise RuntimeError("reset_demo prefix changed")
    content = content.replace(old, new, 1)
    snapshot_guard = '''    def workspace_snapshot_sync(self, workspace_id: str) -> dict[str, Any]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n'''
    guarded = '''    def workspace_snapshot_sync(self, workspace_id: str) -> dict[str, Any]:\n        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:\n            raise KeyError(workspace_id)\n'''
    if snapshot_guard not in content:
        raise RuntimeError("workspace snapshot guard changed")
    content = content.replace(snapshot_guard, guarded, 1)
    write(path, content)


def update_protocol_and_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def encrypted_workspace_backup_payload(\n"
    addition = '''    async def privacy_inventory(\n        self, *, workspace_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def privacy_settings(\n        self, *, workspace_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def update_privacy_settings(\n        self, *, workspace_id: str, model_receipt_days: int,\n        request_audit_days: int, scheduler_receipt_days: int\n    ) -> Mapping[str, object]: ...\n\n    async def privacy_purge_preview(\n        self, *, workspace_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def purge_expired_private_history(\n        self, *, workspace_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def destroy_workspace(\n        self, *, workspace_id: str, confirmation: str, passphrase: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("encrypted backup protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass EncryptedExportRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("EncryptedExportRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.post("/v1/backups/{backup_id}/encrypted-export")\n'
    if route_marker not in content:
        raise RuntimeError("encrypted export route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/storage/test_privacy_lifecycle.py", TESTS)
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 9: privacy retention and owner-controlled destruction\n\n- Folio exposes a local data inventory without returning record contents.\n- Retention settings apply only to model, egress, request-audit, and scheduler receipts.\n- Finance records are explicitly excluded from automatic retention deletion.\n- Purges use SQLite secure-delete and VACUUM, and produce a content-hashed receipt.\n- Workspace destruction requires an exact typed confirmation and a passphrase.\n- A passphrase-encrypted backup and external destruction receipt are written before the workspace database is wiped.\n- A destruction marker prevents automatic demo reseeding after restart.\n'''
    if "## Stack 9: privacy retention and owner-controlled destruction" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration()
    add_privacy_module()
    update_services()
    update_protocol_and_routes()
    add_tests_and_docs()
    print("privacy lifecycle changes applied")


if __name__ == "__main__":
    main()
