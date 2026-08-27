from __future__ import annotations

import json
import re
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
    if 'name="database_backup_receipts"' in value:
        return
    addition = r'''
    Migration(
        version=22,
        name="database_backup_receipts",
        sql="""
        CREATE TABLE backup_receipts (
            backup_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            relative_path TEXT NOT NULL UNIQUE,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
            page_count INTEGER NOT NULL CHECK (page_count > 0),
            schema_version INTEGER NOT NULL CHECK (schema_version >= 0),
            status TEXT NOT NULL CHECK (status IN ('available', 'pruned', 'failed')),
            created_at TEXT NOT NULL,
            verified_at TEXT NOT NULL
        );

        CREATE INDEX backup_receipts_workspace_created
            ON backup_receipts(workspace_id, created_at DESC, backup_id DESC);
        """,
    ),
'''
    stripped = value.rstrip()
    if not stripped.endswith(")"):
        raise RuntimeError("migrations.py does not end with the migration tuple")
    write(path, stripped[:-1] + addition + ")\n")


def create_backup_module() -> None:
    write(
        "services/api/src/finance_agent/storage/backup.py",
        '''"""Verified local SQLite backups with bounded retention and durable receipts."""\n\nfrom __future__ import annotations\n\nimport hashlib\nimport os\nimport sqlite3\nfrom collections.abc import Callable\nfrom dataclasses import dataclass\nfrom datetime import UTC, datetime\nfrom pathlib import Path\nfrom urllib.parse import quote\n\nfrom finance_agent.storage.store import SQLiteStore\n\n\nclass BackupError(RuntimeError):\n    """A backup could not be created or verified safely."""\n\n\n@dataclass(frozen=True, slots=True)\nclass DatabaseVerification:\n    page_count: int\n    schema_version: int\n\n\n@dataclass(frozen=True, slots=True)\nclass BackupReceipt:\n    backup_id: str\n    workspace_id: str\n    relative_path: str\n    content_hash: str\n    size_bytes: int\n    page_count: int\n    schema_version: int\n    status: str\n    created_at: str\n    verified_at: str\n\n    def as_contract(self) -> dict[str, object]:\n        return {\n            "backupId": self.backup_id,\n            "workspaceId": self.workspace_id,\n            "relativePath": self.relative_path,\n            "contentHash": self.content_hash,\n            "sizeBytes": self.size_bytes,\n            "pageCount": self.page_count,\n            "schemaVersion": self.schema_version,\n            "status": self.status,\n            "createdAt": self.created_at,\n            "verifiedAt": self.verified_at,\n        }\n\n\ndef verify_sqlite_database(path: str | Path) -> DatabaseVerification:\n    candidate = Path(path).expanduser().resolve()\n    if not candidate.is_file() or candidate.stat().st_size <= 0:\n        raise BackupError("backup file is missing or empty")\n    uri = f"file:{quote(str(candidate))}?mode=ro&immutable=1"\n    try:\n        connection = sqlite3.connect(uri, uri=True, timeout=5.0)\n        try:\n            quick_check = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]\n            if quick_check != ["ok"]:\n                raise BackupError("SQLite quick_check did not return ok")\n            foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))\n            if foreign_keys:\n                raise BackupError("SQLite foreign-key verification failed")\n            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])\n            schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])\n        finally:\n            connection.close()\n    except sqlite3.Error as exc:\n        raise BackupError("SQLite backup verification failed") from exc\n    if page_count <= 0:\n        raise BackupError("SQLite backup has no pages")\n    return DatabaseVerification(page_count=page_count, schema_version=schema_version)\n\n\nclass DatabaseBackupService:\n    def __init__(\n        self,\n        store: SQLiteStore,\n        backup_root: str | Path,\n        *,\n        retention: int = 10,\n        clock: Callable[[], datetime] | None = None,\n    ) -> None:\n        if retention < 1:\n            raise ValueError("backup retention must be positive")\n        if store.database_path == ":memory:":\n            raise ValueError("in-memory databases cannot create persistent backups")\n        self.store = store\n        self.backup_root = Path(backup_root).expanduser().resolve()\n        self.retention = retention\n        self.clock = clock or (lambda: datetime.now(UTC))\n        self.backup_root.mkdir(parents=True, exist_ok=True)\n        try:\n            self.backup_root.chmod(0o700)\n        except OSError:\n            pass\n\n    @staticmethod\n    def _receipt(row: sqlite3.Row) -> BackupReceipt:\n        return BackupReceipt(\n            backup_id=str(row["backup_id"]),\n            workspace_id=str(row["workspace_id"]),\n            relative_path=str(row["relative_path"]),\n            content_hash=str(row["content_hash"]),\n            size_bytes=int(row["size_bytes"]),\n            page_count=int(row["page_count"]),\n            schema_version=int(row["schema_version"]),\n            status=str(row["status"]),\n            created_at=str(row["created_at"]),\n            verified_at=str(row["verified_at"]),\n        )\n\n    def create(self, workspace_id: str) -> BackupReceipt:\n        workspace = self.store.fetch_one(\n            "SELECT workspace_id FROM workspaces WHERE workspace_id = ?",\n            (workspace_id,),\n        )\n        if workspace is None:\n            raise KeyError(workspace_id)\n        now = self.clock().astimezone(UTC)\n        timestamp = now.strftime("%Y%m%dT%H%M%S%fZ")\n        seed = f"{workspace_id}\\0{timestamp}"\n        backup_id = f"backup_{hashlib.sha256(seed.encode()).hexdigest()[:24]}"\n        filename = f"folio-{workspace_id}-{timestamp}-{backup_id[-8:]}.sqlite3"\n        final_path = self.backup_root / filename\n        temporary_path = self.backup_root / f".{filename}.tmp"\n\n        source = sqlite3.connect(self.store.database_path, timeout=30.0)\n        target = sqlite3.connect(temporary_path, timeout=30.0)\n        try:\n            source.execute("PRAGMA wal_checkpoint(PASSIVE)")\n            source.backup(target)\n            target.commit()\n        except sqlite3.Error as exc:\n            raise BackupError("SQLite online backup failed") from exc\n        finally:\n            target.close()\n            source.close()\n\n        try:\n            verification = verify_sqlite_database(temporary_path)\n            content = temporary_path.read_bytes()\n            digest = hashlib.sha256(content).hexdigest()\n            size_bytes = len(content)\n            try:\n                temporary_path.chmod(0o600)\n            except OSError:\n                pass\n            os.replace(temporary_path, final_path)\n            try:\n                final_path.chmod(0o600)\n            except OSError:\n                pass\n        except Exception:\n            temporary_path.unlink(missing_ok=True)\n            raise\n\n        relative_path = final_path.relative_to(self.backup_root).as_posix()\n        occurred_at = now.isoformat()\n        with self.store.transaction() as connection:\n            connection.execute(\n                """\n                INSERT INTO backup_receipts(\n                    backup_id, workspace_id, relative_path, content_hash, size_bytes,\n                    page_count, schema_version, status, created_at, verified_at\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'available', ?, ?)\n                """,\n                (\n                    backup_id, workspace_id, relative_path, digest, size_bytes,\n                    verification.page_count, verification.schema_version,\n                    occurred_at, occurred_at,\n                ),\n            )\n        self._prune(workspace_id)\n        row = self.store.fetch_one(\n            "SELECT * FROM backup_receipts WHERE backup_id = ?",\n            (backup_id,),\n        )\n        assert row is not None\n        return self._receipt(row)\n\n    def list(self, workspace_id: str) -> tuple[BackupReceipt, ...]:\n        return tuple(\n            self._receipt(row)\n            for row in self.store.fetch_all(\n                """\n                SELECT * FROM backup_receipts\n                WHERE workspace_id = ?\n                ORDER BY created_at DESC, backup_id DESC\n                """,\n                (workspace_id,),\n            )\n        )\n\n    def verify(self, backup_id: str) -> BackupReceipt:\n        row = self.store.fetch_one(\n            "SELECT * FROM backup_receipts WHERE backup_id = ?",\n            (backup_id,),\n        )\n        if row is None:\n            raise KeyError(backup_id)\n        receipt = self._receipt(row)\n        if receipt.status != "available":\n            raise BackupError("backup is no longer available")\n        candidate = (self.backup_root / receipt.relative_path).resolve()\n        if candidate.parent != self.backup_root:\n            raise BackupError("backup receipt points outside the backup root")\n        verification = verify_sqlite_database(candidate)\n        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()\n        if digest != receipt.content_hash:\n            raise BackupError("backup content hash does not match its receipt")\n        if verification.page_count != receipt.page_count:\n            raise BackupError("backup page count does not match its receipt")\n        return receipt\n\n    def _prune(self, workspace_id: str) -> None:\n        available = self.store.fetch_all(\n            """\n            SELECT * FROM backup_receipts\n            WHERE workspace_id = ? AND status = 'available'\n            ORDER BY created_at DESC, backup_id DESC\n            """,\n            (workspace_id,),\n        )\n        for row in available[self.retention :]:\n            receipt = self._receipt(row)\n            candidate = (self.backup_root / receipt.relative_path).resolve()\n            if candidate.parent == self.backup_root:\n                candidate.unlink(missing_ok=True)\n            with self.store.transaction() as connection:\n                connection.execute(\n                    "UPDATE backup_receipts SET status = 'pruned' WHERE backup_id = ?",\n                    (receipt.backup_id,),\n                )\n\n\n__all__ = [\n    "BackupError", "BackupReceipt", "DatabaseBackupService",\n    "DatabaseVerification", "verify_sqlite_database",\n]\n''',
    )


def patch_store_permissions() -> None:
    path = "services/api/src/finance_agent/storage/store.py"
    value = read(path)
    value = replace_once(
        value,
        """        self.database_path = str(database_path)\n        if self.database_path != \":memory:\":\n            Path(self.database_path).expanduser().resolve().parent.mkdir(\n                parents=True, exist_ok=True\n            )\n""",
        """        self.database_path = str(database_path)\n        if self.database_path != \":memory:\":\n            resolved = Path(self.database_path).expanduser().resolve()\n            self.database_path = str(resolved)\n            resolved.parent.mkdir(parents=True, exist_ok=True)\n            try:\n                resolved.parent.chmod(0o700)\n            except OSError:\n                pass\n""",
        label="resolved private database path",
    )
    value = replace_once(
        value,
        """        connection.execute(\"PRAGMA foreign_keys = ON\")\n        connection.execute(\"PRAGMA busy_timeout = 30000\")\n        return connection\n""",
        """        connection.execute(\"PRAGMA foreign_keys = ON\")\n        connection.execute(\"PRAGMA busy_timeout = 30000\")\n        connection.execute(\"PRAGMA synchronous = FULL\")\n        connection.execute(\"PRAGMA secure_delete = ON\")\n        connection.execute(\"PRAGMA trusted_schema = OFF\")\n        if self.database_path != \":memory:\":\n            try:\n                Path(self.database_path).chmod(0o600)\n            except OSError:\n                pass\n        return connection\n""",
        label="SQLite durability pragmas",
    )
    write(path, value)


def patch_app_database_path() -> None:
    path = "services/api/src/finance_agent/api/app.py"
    value = read(path)
    value = replace_once(value, "from pathlib import Path\n", "import os\nfrom pathlib import Path\n", label="app os import")
    value = replace_once(
        value,
        """    services = LocalRouteServices(database_path or DEFAULT_DATABASE, auto_seed=auto_seed)\n""",
        """    configured_database = os.getenv(\"FINANCE_DATABASE_PATH\")\n    selected_database = (\n        database_path\n        if database_path is not None\n        else Path(configured_database).expanduser() if configured_database else DEFAULT_DATABASE\n    )\n    services = LocalRouteServices(selected_database, auto_seed=auto_seed)\n""",
        label="configured database path",
    )
    write(path, value)


def patch_route_protocol() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    value = read(path)
    value = replace_once(
        value,
        """    async def working_understanding_diagnostics(\n""",
        """    async def create_backup(self, workspace_id: str) -> Mapping[str, object]: ...\n\n    async def list_backups(self, workspace_id: str) -> Mapping[str, object]: ...\n\n    async def working_understanding_diagnostics(\n""",
        label="backup route protocol",
    )
    write(path, value)


def patch_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/router.py"
    value = read(path)
    value = replace_once(
        value,
        """class DailyCloseRequest(RequestModel):\n""",
        """class BackupRequest(RequestModel):\n    workspace_id: str = Field(alias=\"workspaceId\")\n\n\nclass DailyCloseRequest(RequestModel):\n""",
        label="backup request model",
    )
    anchor = '''    @router.get("/v1/diagnostics/working-understanding")\n'''
    addition = '''    @router.post("/v1/backups", status_code=201)\n    async def create_backup(\n        body: BackupRequest,\n        services: Services,\n    ) -> dict[str, object]:\n        return dict(await services.create_backup(body.workspace_id))\n\n    @router.get("/v1/backups")\n    async def list_backups(\n        services: Services,\n        workspace_id: Annotated[str, Query(alias="workspaceId")],\n    ) -> dict[str, object]:\n        return dict(await services.list_backups(workspace_id))\n\n'''
    if anchor not in value:
        raise RuntimeError("working-understanding route anchor is missing")
    value = value.replace(anchor, addition + anchor, 1)
    write(path, value)


def patch_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    value = read(path)
    value = replace_once(
        value,
        "from finance_agent.storage import SQLiteConversationStore, SQLiteStore, canonical_json\n",
        "from finance_agent.storage import SQLiteConversationStore, SQLiteStore, canonical_json\nfrom finance_agent.storage.backup import DatabaseBackupService\n",
        label="backup service import",
    )
    value = replace_once(
        value,
        """        self.store = SQLiteStore(database_path)\n        self.engine = FinanceEngine(self.store)\n""",
        """        self.store = SQLiteStore(database_path)\n        backup_root = Path(self.store.database_path).resolve().parent / \"backups\"\n        self.backups = DatabaseBackupService(self.store, backup_root)\n        self.engine = FinanceEngine(self.store)\n""",
        label="backup service composition",
    )
    anchor = """    async def working_understanding_diagnostics(\n"""
    methods = '''    async def create_backup(self, workspace_id: str) -> Mapping[str, object]:\n        async with self._lock:\n            receipt = await asyncio.to_thread(self.backups.create, workspace_id)\n        return receipt.as_contract()\n\n    async def list_backups(self, workspace_id: str) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        receipts = await asyncio.to_thread(self.backups.list, workspace_id)\n        return {\n            "workspaceId": workspace_id,\n            "backups": [receipt.as_contract() for receipt in receipts],\n        }\n\n'''
    if anchor not in value:
        raise RuntimeError("service diagnostics anchor is missing")
    value = value.replace(anchor, methods + anchor, 1)
    write(path, value)


def create_cli() -> None:
    write(
        "scripts/database_control.py",
        '''from __future__ import annotations\n\nimport argparse\nimport json\nimport os\nfrom pathlib import Path\n\nfrom finance_agent.storage import SQLiteStore\nfrom finance_agent.storage.backup import DatabaseBackupService, verify_sqlite_database\n\nROOT = Path(__file__).resolve().parents[1]\nDEFAULT_DATABASE = ROOT / "var" / "finance-agent.sqlite3"\nWORKSPACE_ID = "ws_koru_studio"\n\n\ndef database_path() -> Path:\n    configured = os.getenv("FINANCE_DATABASE_PATH")\n    return Path(configured).expanduser() if configured else DEFAULT_DATABASE\n\n\ndef main() -> int:\n    parser = argparse.ArgumentParser(description="Create or verify a local Folio SQLite backup")\n    subparsers = parser.add_subparsers(dest="command", required=True)\n    backup = subparsers.add_parser("backup")\n    backup.add_argument("--workspace", default=WORKSPACE_ID)\n    verify = subparsers.add_parser("verify")\n    verify.add_argument("path", type=Path)\n    arguments = parser.parse_args()\n\n    if arguments.command == "verify":\n        result = verify_sqlite_database(arguments.path)\n        print(json.dumps({\n            "status": "verified",\n            "path": str(arguments.path.resolve()),\n            "pageCount": result.page_count,\n            "schemaVersion": result.schema_version,\n        }, indent=2))\n        return 0\n\n    store = SQLiteStore(database_path())\n    store.migrate()\n    service = DatabaseBackupService(store, Path(store.database_path).parent / "backups")\n    receipt = service.create(arguments.workspace)\n    print(json.dumps(receipt.as_contract(), indent=2))\n    return 0\n\n\nif __name__ == "__main__":\n    raise SystemExit(main())\n''',
    )


def patch_package_scripts() -> None:
    path = "package.json"
    value = json.loads(read(path))
    value["scripts"]["backup"] = "uv run --project services/api python scripts/database_control.py backup"
    value["scripts"]["backup:verify"] = "uv run --project services/api python scripts/database_control.py verify"
    write(path, json.dumps(value, indent=2) + "\n")


def add_tests() -> None:
    write(
        "services/api/tests/storage/test_backups.py",
        '''from __future__ import annotations\n\nimport os\nimport sqlite3\nfrom datetime import UTC, datetime, timedelta\nfrom pathlib import Path\n\nimport pytest\nfrom fastapi.testclient import TestClient\n\nfrom finance_agent.api.app import create_app\nfrom finance_agent.finance import FinanceEngine\nfrom finance_agent.storage import SQLiteStore\nfrom finance_agent.storage.backup import BackupError, DatabaseBackupService, verify_sqlite_database\n\nROOT = Path(__file__).resolve().parents[4]\nCSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"\n\n\ndef seeded(tmp_path: Path) -> tuple[SQLiteStore, FinanceEngine]:\n    store = SQLiteStore(tmp_path / "live" / "folio.sqlite3")\n    engine = FinanceEngine(store)\n    engine.reset_demo(CSV)\n    return store, engine\n\n\ndef test_backup_is_verified_and_remains_a_consistent_snapshot(tmp_path: Path) -> None:\n    store, engine = seeded(tmp_path)\n    service = DatabaseBackupService(\n        store, tmp_path / "backups",\n        clock=lambda: datetime(2026, 8, 27, 2, 0, tzinfo=UTC),\n    )\n    receipt = service.create("ws_koru_studio")\n    backup_path = tmp_path / "backups" / receipt.relative_path\n\n    verification = verify_sqlite_database(backup_path)\n    assert verification.page_count == receipt.page_count\n    assert service.verify(receipt.backup_id) == receipt\n    with sqlite3.connect(backup_path) as connection:\n        original_count = int(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0])\n    engine.ingest_akahu_fixture()\n    with sqlite3.connect(backup_path) as connection:\n        assert int(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]) == original_count\n    assert len(store.fetch_all("SELECT * FROM transactions")) > original_count\n    if os.name == "posix":\n        assert backup_path.stat().st_mode & 0o777 == 0o600\n\n\ndef test_backup_retention_prunes_old_files_and_preserves_receipts(tmp_path: Path) -> None:\n    store, _engine = seeded(tmp_path)\n    instant = datetime(2026, 8, 27, 3, 0, tzinfo=UTC)\n    ticks = iter([instant, instant + timedelta(seconds=1), instant + timedelta(seconds=2)])\n    service = DatabaseBackupService(store, tmp_path / "backups", retention=2, clock=lambda: next(ticks))\n\n    first = service.create("ws_koru_studio")\n    second = service.create("ws_koru_studio")\n    third = service.create("ws_koru_studio")\n\n    receipts = {receipt.backup_id: receipt for receipt in service.list("ws_koru_studio")}\n    assert receipts[first.backup_id].status == "pruned"\n    assert receipts[second.backup_id].status == "available"\n    assert receipts[third.backup_id].status == "available"\n    assert not (tmp_path / "backups" / first.relative_path).exists()\n    with pytest.raises(BackupError, match="no longer available"):\n        service.verify(first.backup_id)\n\n\ndef test_configured_database_path_and_backup_routes(tmp_path: Path, monkeypatch) -> None:\n    database = tmp_path / "configured" / "folio.sqlite3"\n    monkeypatch.setenv("FINANCE_DATABASE_PATH", str(database))\n    monkeypatch.delenv("FOLIO_SESSION_TOKEN", raising=False)\n    app = create_app(auto_seed=True)\n    with TestClient(app) as client:\n        created = client.post("/v1/backups", json={"workspaceId": "ws_koru_studio"})\n        listed = client.get("/v1/backups", params={"workspaceId": "ws_koru_studio"})\n    assert created.status_code == 201\n    assert listed.status_code == 200\n    assert listed.json()["backups"][0]["backupId"] == created.json()["backupId"]\n    assert database.resolve() == Path(app.state.finance_route_services.store.database_path)\n\ndef test_tampered_backup_fails_hash_verification(tmp_path: Path) -> None:\n    store, _engine = seeded(tmp_path)\n    service = DatabaseBackupService(store, tmp_path / "backups")\n    receipt = service.create("ws_koru_studio")\n    path = tmp_path / "backups" / receipt.relative_path\n    with path.open("ab") as handle:\n        handle.write(b"tamper")\n    with pytest.raises(BackupError, match="content hash"):\n        service.verify(receipt.backup_id)\n''',
    )


def main() -> None:
    patch_migrations()
    create_backup_module()
    patch_store_permissions()
    patch_app_database_path()
    patch_route_protocol()
    patch_routes()
    patch_services()
    create_cli()
    patch_package_scripts()
    add_tests()


if __name__ == "__main__":
    main()
