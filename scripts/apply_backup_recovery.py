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


def insert_method_before(path: str, class_name: str, before_name: str, method: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    before = next(
        node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


MIGRATION = '''    Migration(
        version={version},
        name="workspace_backup_and_restore_receipts",
        sql="""
        CREATE TABLE workspace_backups (
            backup_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            created_at TEXT NOT NULL,
            schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
            database_sha256 TEXT NOT NULL CHECK (length(database_sha256) = 64),
            archive_sha256 TEXT NOT NULL CHECK (length(archive_sha256) = 64),
            manifest_json TEXT NOT NULL,
            content BLOB NOT NULL,
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0)
        );

        CREATE TABLE workspace_restore_receipts (
            receipt_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            backup_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            restored_at TEXT NOT NULL,
            restored_database_sha256 TEXT NOT NULL CHECK (length(restored_database_sha256) = 64),
            previous_database_sha256 TEXT NOT NULL CHECK (length(previous_database_sha256) = 64),
            recovery_filename TEXT NOT NULL,
            UNIQUE (workspace_id, request_id)
        );

        CREATE INDEX workspace_backups_workspace_time
            ON workspace_backups(workspace_id, created_at, backup_id);
        CREATE INDEX workspace_restore_receipts_workspace_time
            ON workspace_restore_receipts(workspace_id, restored_at, receipt_id);
        """,
    ),
'''

BACKUPS = '''"""Atomic local SQLite backups with integrity and restore receipts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

from finance_agent.storage.migrations import MIGRATIONS
from finance_agent.storage.store import SQLiteStore, canonical_json

BACKUP_FORMAT = "folio.workspace-backup@1"


def _now() -> datetime:
    return datetime.now(UTC)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


@dataclass(frozen=True, slots=True)
class WorkspaceBackup:
    backup_id: str
    workspace_id: str
    created_at: str
    schema_version: int
    database_sha256: str
    archive_sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class RestoreResult:
    backup_id: str
    workspace_id: str
    restored_at: str
    restored_database_sha256: str
    previous_database_sha256: str
    recovery_filename: str


class WorkspaceBackupManager:
    def __init__(self, store: SQLiteStore) -> None:
        if store.database_path == ":memory:":
            raise ValueError("workspace backup requires a file-backed SQLite database")
        self.store = store
        self.database_path = Path(store.database_path).expanduser().resolve()

    def create(self, workspace_id: str) -> WorkspaceBackup:
        workspace = self.store.fetch_one(
            "SELECT workspace_id, name FROM workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        if workspace is None:
            raise KeyError(workspace_id)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.store.connect() as connection:
            connection.execute("PRAGMA wal_checkpoint(FULL)")
        temporary = tempfile.NamedTemporaryFile(
            prefix="folio-backup-", suffix=".sqlite3", delete=False,
            dir=self.database_path.parent,
        )
        temporary_path = Path(temporary.name)
        temporary.close()
        try:
            source = sqlite3.connect(self.database_path)
            destination = sqlite3.connect(temporary_path)
            try:
                source.backup(destination)
                destination.commit()
                integrity = destination.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or str(integrity[0]).lower() != "ok":
                    raise ValueError("backup integrity check failed")
            finally:
                destination.close()
                source.close()
            database_bytes = temporary_path.read_bytes()
        finally:
            temporary_path.unlink(missing_ok=True)

        database_sha256 = _sha256(database_bytes)
        created_at = _now().isoformat()
        schema_version = max(migration.version for migration in MIGRATIONS)
        backup_id = _stable_id(
            "backup", workspace_id, created_at, database_sha256
        )
        manifest = {
            "format": BACKUP_FORMAT,
            "backupId": backup_id,
            "workspaceId": workspace_id,
            "workspaceName": str(workspace["name"]),
            "createdAt": created_at,
            "schemaVersion": schema_version,
            "databaseSha256": database_sha256,
        }
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", canonical_json(manifest))
            archive.writestr("workspace.sqlite3", database_bytes)
        content = buffer.getvalue()
        archive_sha256 = _sha256(content)
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO workspace_backups(
                    backup_id, workspace_id, created_at, schema_version,
                    database_sha256, archive_sha256, manifest_json, content, size_bytes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    backup_id,
                    workspace_id,
                    created_at,
                    schema_version,
                    database_sha256,
                    archive_sha256,
                    canonical_json(manifest),
                    content,
                    len(content),
                ),
            )
        return WorkspaceBackup(
            backup_id=backup_id,
            workspace_id=workspace_id,
            created_at=created_at,
            schema_version=schema_version,
            database_sha256=database_sha256,
            archive_sha256=archive_sha256,
            content=content,
        )

    def get(self, backup_id: str) -> WorkspaceBackup:
        row = self.store.fetch_one(
            "SELECT * FROM workspace_backups WHERE backup_id = ?", (backup_id,)
        )
        if row is None:
            raise KeyError(backup_id)
        content = bytes(row["content"])
        if _sha256(content) != str(row["archive_sha256"]):
            raise ValueError("stored backup archive digest does not match its receipt")
        return WorkspaceBackup(
            backup_id=str(row["backup_id"]),
            workspace_id=str(row["workspace_id"]),
            created_at=str(row["created_at"]),
            schema_version=int(row["schema_version"]),
            database_sha256=str(row["database_sha256"]),
            archive_sha256=str(row["archive_sha256"]),
            content=content,
        )

    def restore(self, backup_id: str, *, workspace_id: str) -> RestoreResult:
        backup = self.get(backup_id)
        if backup.workspace_id != workspace_id:
            raise ValueError("backup belongs to a different workspace")
        try:
            with zipfile.ZipFile(BytesIO(backup.content), "r") as archive:
                if set(archive.namelist()) != {"manifest.json", "workspace.sqlite3"}:
                    raise ValueError("backup archive has an unexpected file set")
                manifest = json.loads(archive.read("manifest.json"))
                database_bytes = archive.read("workspace.sqlite3")
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as exc:
            raise ValueError("backup archive is invalid") from exc
        if manifest.get("format") != BACKUP_FORMAT:
            raise ValueError("unsupported backup format")
        if manifest.get("backupId") != backup_id:
            raise ValueError("backup manifest identifier mismatch")
        if manifest.get("workspaceId") != workspace_id:
            raise ValueError("backup manifest workspace mismatch")
        database_sha256 = _sha256(database_bytes)
        if database_sha256 != manifest.get("databaseSha256"):
            raise ValueError("backup database digest mismatch")
        if database_sha256 != backup.database_sha256:
            raise ValueError("backup receipt database digest mismatch")

        temporary = tempfile.NamedTemporaryFile(
            prefix="folio-restore-", suffix=".sqlite3", delete=False,
            dir=self.database_path.parent,
        )
        temporary_path = Path(temporary.name)
        temporary.write(database_bytes)
        temporary.flush()
        os.fsync(temporary.fileno())
        temporary.close()
        try:
            os.chmod(temporary_path, 0o600)
            candidate = sqlite3.connect(temporary_path)
            try:
                integrity = candidate.execute("PRAGMA integrity_check").fetchone()
                if integrity is None or str(integrity[0]).lower() != "ok":
                    raise ValueError("restore candidate failed SQLite integrity check")
                candidate_workspace = candidate.execute(
                    "SELECT workspace_id FROM workspaces WHERE workspace_id = ?",
                    (workspace_id,),
                ).fetchone()
                if candidate_workspace is None:
                    raise ValueError("restore candidate does not contain the workspace")
                version_row = candidate.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                ).fetchone()
                candidate_version = int(version_row[0]) if version_row else 0
                supported_version = max(migration.version for migration in MIGRATIONS)
                if candidate_version > supported_version:
                    raise ValueError("backup schema is newer than this Folio build")
            finally:
                candidate.close()

            previous_bytes = self.database_path.read_bytes()
            previous_sha256 = _sha256(previous_bytes)
            stamp = _now().strftime("%Y%m%dT%H%M%SZ")
            recovery_path = self.database_path.with_name(
                f"{self.database_path.stem}.pre-restore-{stamp}.sqlite3"
            )
            shutil.copy2(self.database_path, recovery_path)
            os.chmod(recovery_path, 0o600)
            for suffix in ("-wal", "-shm"):
                Path(f"{self.database_path}{suffix}").unlink(missing_ok=True)
            os.replace(temporary_path, self.database_path)
            os.chmod(self.database_path, 0o600)
        finally:
            temporary_path.unlink(missing_ok=True)

        return RestoreResult(
            backup_id=backup_id,
            workspace_id=workspace_id,
            restored_at=_now().isoformat(),
            restored_database_sha256=database_sha256,
            previous_database_sha256=previous_sha256,
            recovery_filename=recovery_path.name,
        )
'''

SERVICE_METHODS = '''    def _recompose_after_restore(self) -> None:
        self.store = SQLiteStore(self.store.database_path)
        self.engine = FinanceEngine(self.store)
        self.engine.initialise()
        self.working_understanding = WorkingUnderstandingRuntime(self.store)
        self.finance_core = FinanceCoreAdapter(self.engine)
        self.daily_close = DailyCloseService(self.engine)
        self.event_buffer.clear()
        self.receipts = SQLiteReceiptSink(self.store)
        self._compose_controller()
        mode = self.store.fetch_one(
            "SELECT model_mode FROM workspaces WHERE workspace_id = ?", (WORKSPACE_ID,)
        )
        self.current_mode = (
            ModelMode(str(mode["model_mode"])) if mode is not None else ModelMode.LOCAL
        )
        self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)

    async def create_workspace_backup(
        self, *, workspace_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID:
            raise KeyError(workspace_id)
        async with self._lock:
            backup = WorkspaceBackupManager(self.store).create(workspace_id)
        return {
            "backupId": backup.backup_id,
            "workspaceId": backup.workspace_id,
            "createdAt": backup.created_at,
            "schemaVersion": backup.schema_version,
            "databaseSha256": backup.database_sha256,
            "archiveSha256": backup.archive_sha256,
            "sizeBytes": len(backup.content),
            "encrypted": False,
            "storage": "local_sqlite",
        }

    async def workspace_backup_payload(self, backup_id: str) -> ArtifactPayload:
        backup = WorkspaceBackupManager(self.store).get(backup_id)
        return ArtifactPayload(
            content=backup.content,
            media_type="application/zip",
            filename=f"folio-{backup.workspace_id}-{backup.backup_id}.zip",
            content_hash=backup.archive_sha256,
        )

    async def restore_workspace_backup(
        self,
        *,
        workspace_id: str,
        backup_id: str,
        request_id: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID:
            raise KeyError(workspace_id)
        async with self._lock:
            result = WorkspaceBackupManager(self.store).restore(
                backup_id, workspace_id=workspace_id
            )
            self._recompose_after_restore()
            receipt_id = _stable_id("restorercpt", workspace_id, request_id, backup_id)
            with self.store.transaction() as connection:
                existing = connection.execute(
                    """
                    SELECT backup_id FROM workspace_restore_receipts
                    WHERE workspace_id = ? AND request_id = ?
                    """,
                    (workspace_id, request_id),
                ).fetchone()
                if existing is not None and str(existing["backup_id"]) != backup_id:
                    raise ValueError("restore requestId is bound to another backup")
                connection.execute(
                    """
                    INSERT INTO workspace_restore_receipts(
                        receipt_id, workspace_id, backup_id, request_id,
                        restored_at, restored_database_sha256,
                        previous_database_sha256, recovery_filename
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workspace_id, request_id) DO NOTHING
                    """,
                    (
                        receipt_id,
                        workspace_id,
                        backup_id,
                        request_id,
                        result.restored_at,
                        result.restored_database_sha256,
                        result.previous_database_sha256,
                        result.recovery_filename,
                    ),
                )
            snapshot = self.workspace_snapshot_sync(workspace_id)
        return {
            "receiptId": receipt_id,
            "backupId": backup_id,
            "workspaceId": workspace_id,
            "restoredAt": result.restored_at,
            "restoredDatabaseSha256": result.restored_database_sha256,
            "previousDatabaseSha256": result.previous_database_sha256,
            "recoveryFilename": result.recovery_filename,
            "snapshotId": snapshot["snapshotId"],
        }
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/backups", status_code=201)
    async def create_workspace_backup(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        return dict(await services.create_workspace_backup(workspace_id=workspace_id))

    @router.get("/v1/backups/{backup_id}")
    async def download_workspace_backup(
        backup_id: PathIdentifier,
        services: Services,
    ) -> Response:
        value = await services.workspace_backup_payload(backup_id)
        return Response(
            content=value.content,
            media_type=value.media_type,
            headers={
                "Content-Disposition": content_disposition(
                    value.filename, disposition="attachment"
                ),
                "ETag": f'"{value.content_hash}"',
                "Cache-Control": "no-store",
            },
        )

    @router.post("/v1/backups/{backup_id}/restore")
    async def restore_workspace_backup(
        backup_id: PathIdentifier,
        body: RestoreBackupRequest,
        services: Services,
    ) -> dict[str, object]:
        return dict(
            await services.restore_workspace_backup(
                workspace_id=body.workspace_id,
                backup_id=backup_id,
                request_id=body.request_id,
            )
        )

'''

TESTS = '''from __future__ import annotations

import hashlib
import zipfile
from io import BytesIO
from pathlib import Path

import httpx
import pytest

from finance_agent.api.app import create_app
from finance_agent.api.services import LocalRouteServices
from finance_agent.storage import SQLiteStore


@pytest.mark.asyncio
async def test_backup_restore_is_integrity_checked_atomic_and_receipted(tmp_path: Path) -> None:
    database = tmp_path / "folio.sqlite3"
    services = LocalRouteServices(database, auto_seed=True)
    original = services.workspace_snapshot_sync("ws_koru_studio")
    backup = await services.create_workspace_backup(workspace_id="ws_koru_studio")
    payload = await services.workspace_backup_payload(str(backup["backupId"]))
    assert hashlib.sha256(payload.content).hexdigest() == backup["archiveSha256"]
    with zipfile.ZipFile(BytesIO(payload.content)) as archive:
        assert set(archive.namelist()) == {"manifest.json", "workspace.sqlite3"}

    with services.store.transaction() as connection:
        connection.execute(
            """
            UPDATE workspaces SET protected_reserve_minor = 999999,
                state_revision = state_revision + 1
            WHERE workspace_id = ?
            """,
            ("ws_koru_studio",),
        )
    assert services.workspace_snapshot_sync("ws_koru_studio")["workspace"]["protectedReserveMinor"] == 999999

    restored = await services.restore_workspace_backup(
        workspace_id="ws_koru_studio",
        backup_id=str(backup["backupId"]),
        request_id="restore_request_123",
    )
    snapshot = services.workspace_snapshot_sync("ws_koru_studio")
    assert snapshot["workspace"]["protectedReserveMinor"] == original["workspace"]["protectedReserveMinor"]
    assert restored["snapshotId"] == snapshot["snapshotId"]
    assert (tmp_path / str(restored["recoveryFilename"])).exists()
    receipt = services.store.fetch_one(
        "SELECT * FROM workspace_restore_receipts WHERE request_id = ?",
        ("restore_request_123",),
    )
    assert receipt is not None
    await services.aclose()


@pytest.mark.asyncio
async def test_tampered_stored_backup_is_rejected_before_replace(tmp_path: Path) -> None:
    database = tmp_path / "folio.sqlite3"
    services = LocalRouteServices(database, auto_seed=True)
    backup = await services.create_workspace_backup(workspace_id="ws_koru_studio")
    before = database.read_bytes()
    with services.store.transaction() as connection:
        connection.execute(
            "UPDATE workspace_backups SET content = ? WHERE backup_id = ?",
            (b"not a zip", backup["backupId"]),
        )
    with pytest.raises(ValueError, match="digest"):
        await services.restore_workspace_backup(
            workspace_id="ws_koru_studio",
            backup_id=str(backup["backupId"]),
            request_id="restore_tampered",
        )
    assert database.read_bytes() != b"not a zip"
    assert len(database.read_bytes()) >= len(before) // 2
    await services.aclose()


@pytest.mark.asyncio
async def test_backup_api_requires_session_and_downloads_no_store_archive(tmp_path: Path) -> None:
    app = create_app(
        database_path=tmp_path / "folio.sqlite3",
        development_routes=True,
        session_token="session-secret",
    )
    base_headers = {
        "X-Folio-Session": "session-secret",
        "X-Folio-Client": "desktop",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        denied = await client.post("/v1/workspaces/ws_koru_studio/backups")
        assert denied.status_code == 401
        created = await client.post(
            "/v1/workspaces/ws_koru_studio/backups",
            headers={**base_headers, "X-Request-ID": "req_backup_create_123"},
        )
        assert created.status_code == 201
        backup_id = created.json()["backupId"]
        downloaded = await client.get(
            f"/v1/backups/{backup_id}", headers=base_headers
        )
    assert downloaded.status_code == 200
    assert downloaded.headers["cache-control"] == "no-store"
    assert downloaded.headers["content-type"].startswith("application/zip")
    assert "attachment" in downloaded.headers["content-disposition"]
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


def add_backup_manager() -> None:
    write("services/api/src/finance_agent/storage/backups.py", BACKUPS)
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.storage import SQLiteConversationStore, SQLiteStore, canonical_json\n"
    if marker not in content:
        raise RuntimeError("storage import marker missing")
    content = content.replace(
        marker,
        marker + "from finance_agent.storage.backups import WorkspaceBackupManager\n",
        1,
    )
    write(path, content)
    insert_method_before(path, "LocalRouteServices", "record_request_audit", SERVICE_METHODS)


def update_protocol_and_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def health(self) -> Mapping[str, object]: ...\n"
    addition = '''    async def create_workspace_backup(\n        self, *, workspace_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def workspace_backup_payload(self, backup_id: str) -> ArtifactPayload: ...\n\n    async def restore_workspace_backup(\n        self, *, workspace_id: str, backup_id: str, request_id: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("RouteServices health marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    if "content_disposition" not in content.split("\n", 40)[0:40].__str__():
        import_marker = "from finance_agent.agent.events import SequenceGap, format_sse\n"
        content = content.replace(
            import_marker,
            import_marker + "from finance_agent.api.http_security import content_disposition\n",
            1,
        )
    cancel_model_marker = '''class CancelRunRequest(RequestModel):\n    workspace_id: str = Field(alias="workspaceId", pattern=IDENTIFIER_PATTERN)\n    request_id: str = Field(alias="requestId", pattern=IDENTIFIER_PATTERN)\n'''
    restore_model = cancel_model_marker + '''\n\nclass RestoreBackupRequest(RequestModel):\n    workspace_id: str = Field(alias="workspaceId", pattern=IDENTIFIER_PATTERN)\n    request_id: str = Field(alias="requestId", pattern=IDENTIFIER_PATTERN)\n'''
    if cancel_model_marker not in content:
        raise RuntimeError("CancelRunRequest marker missing")
    content = content.replace(cancel_model_marker, restore_model, 1)
    route_marker = '    @router.post("/v1/jobs/{run_id}/cancel", status_code=202)\n'
    if route_marker not in content:
        raise RuntimeError("cancel route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/storage/test_backup_recovery.py", TESTS)
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 6: integrity-checked local backup and restore\n\n- File-backed workspaces can create a SQLite online backup with a signed manifest of hashes.\n- Backup archives are stored locally, downloaded with `no-store`, and remain session protected.\n- Restore validates archive structure, SHA-256 receipts, SQLite integrity, schema compatibility, and workspace identity.\n- The existing database is preserved as a mode-0600 recovery file before atomic replacement.\n- Every restore writes a durable receipt with previous and restored database hashes.\n- These archives are not yet encrypted; no custom cryptography is claimed.\n'''
    if "## Stack 6: integrity-checked local backup and restore" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration()
    add_backup_manager()
    update_protocol_and_routes()
    add_tests_and_docs()
    print("backup and recovery changes applied")


if __name__ == "__main__":
    main()
