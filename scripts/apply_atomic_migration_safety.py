from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


MODULE = '''"""Preflight, backup, atomic migration and restoration for local SQLite stores."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

MIN_FREE_BYTES = 20_000_000
BACKUP_RETENTION = 5


@dataclass(frozen=True, slots=True)
class MigrationReceipt:
    receipt_version: str
    database_filename: str
    backup_filename: str | None
    from_version: int
    to_version: int
    status: str
    applied_versions: tuple[int, ...]
    error_type: str | None
    occurred_at: str

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _quick_check(connection: sqlite3.Connection) -> None:
    row = connection.execute("PRAGMA quick_check").fetchone()
    if row is None or str(row[0]).lower() != "ok":
        raise sqlite3.DatabaseError("SQLite quick_check failed")
    foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        raise sqlite3.IntegrityError("SQLite foreign_key_check failed")


def current_version(database_path: Path) -> int:
    if not database_path.exists() or database_path.stat().st_size == 0:
        return 0
    connection = sqlite3.connect(database_path)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        if exists is None:
            return 0
        row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
        ).fetchone()
        return int(row[0])
    finally:
        connection.close()


def preflight_backup(database_path: Path) -> Path | None:
    if not database_path.exists() or database_path.stat().st_size == 0:
        return None
    connection = sqlite3.connect(database_path, timeout=30.0)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _quick_check(connection)
        database_size = max(1, database_path.stat().st_size)
        free = shutil.disk_usage(database_path.parent).free
        required = max(MIN_FREE_BYTES, database_size * 3)
        if free < required:
            raise OSError(
                f"migration requires at least {required} free bytes; {free} are available"
            )
        backup_root = database_path.parent / ".migration-backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        os.chmod(backup_root, 0o700)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        version = current_version(database_path)
        backup_path = backup_root / (
            f"{database_path.name}.pre-v{version}.{stamp}.sqlite3"
        )
        destination = sqlite3.connect(backup_path)
        try:
            connection.backup(destination)
            destination.commit()
            _quick_check(destination)
        finally:
            destination.close()
        os.chmod(backup_path, 0o600)
        backups = sorted(
            backup_root.glob(f"{database_path.name}.pre-v*.sqlite3"),
            key=lambda value: value.stat().st_mtime_ns,
            reverse=True,
        )
        for stale in backups[BACKUP_RETENTION:]:
            stale.unlink(missing_ok=True)
        return backup_path
    finally:
        connection.close()


def restore_backup(database_path: Path, backup_path: Path | None) -> None:
    for suffix in ("-wal", "-shm"):
        Path(f"{database_path}{suffix}").unlink(missing_ok=True)
    if backup_path is None:
        database_path.unlink(missing_ok=True)
        return
    temporary = database_path.with_suffix(database_path.suffix + ".restore-tmp")
    shutil.copy2(backup_path, temporary)
    os.chmod(temporary, 0o600)
    connection = sqlite3.connect(temporary)
    try:
        _quick_check(connection)
    finally:
        connection.close()
    os.replace(temporary, database_path)
    os.chmod(database_path, 0o600)


def write_receipt(database_path: Path, receipt: MigrationReceipt) -> None:
    receipt_root = database_path.parent / ".migration-backups"
    receipt_root.mkdir(parents=True, exist_ok=True)
    os.chmod(receipt_root, 0o700)
    target = receipt_root / "last-migration-receipt.json"
    temporary = target.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(receipt.as_dict(), sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)


def verify_database(database_path: Path) -> None:
    connection = sqlite3.connect(database_path, timeout=30.0)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        _quick_check(connection)
    finally:
        connection.close()
'''

REPLACEMENT = '''    def migrate(self) -> None:
        from datetime import UTC, datetime
        from pathlib import Path

        from .migration_safety import (
            MigrationReceipt,
            current_version,
            preflight_backup,
            restore_backup,
            verify_database,
            write_receipt,
        )

        in_memory = self.database_path == ":memory:"
        database_path = Path(self.database_path).expanduser().resolve() if not in_memory else None
        from_version = (
            current_version(database_path) if database_path is not None else 0
        )
        backup_path = (
            preflight_backup(database_path) if database_path is not None else None
        )
        connection = self._open_connection()
        applied_versions: list[int] = []
        target_version = max((migration.version for migration in MIGRATIONS), default=0)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.commit()
            applied = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for migration in MIGRATIONS:
                if migration.version in applied:
                    continue
                escaped_name = migration.name.replace("'", "''")
                script = (
                    "BEGIN IMMEDIATE;\n"
                    + migration.sql
                    + "\nINSERT INTO schema_migrations(version, name) VALUES ("
                    + str(migration.version)
                    + ", '"
                    + escaped_name
                    + "');\nCOMMIT;"
                )
                try:
                    connection.executescript(script)
                except Exception:
                    try:
                        connection.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise
                applied_versions.append(migration.version)
            connection.close()
            connection = None
            if database_path is not None:
                verify_database(database_path)
                write_receipt(
                    database_path,
                    MigrationReceipt(
                        receipt_version="folio.migration-receipt@1",
                        database_filename=database_path.name,
                        backup_filename=backup_path.name if backup_path else None,
                        from_version=from_version,
                        to_version=target_version,
                        status="completed",
                        applied_versions=tuple(applied_versions),
                        error_type=None,
                        occurred_at=datetime.now(UTC).isoformat(),
                    ),
                )
        except Exception as exc:
            if connection is not None:
                connection.close()
            if database_path is not None:
                restore_backup(database_path, backup_path)
                write_receipt(
                    database_path,
                    MigrationReceipt(
                        receipt_version="folio.migration-receipt@1",
                        database_filename=database_path.name,
                        backup_filename=backup_path.name if backup_path else None,
                        from_version=from_version,
                        to_version=target_version,
                        status="restored_after_failure",
                        applied_versions=tuple(applied_versions),
                        error_type=type(exc).__name__,
                        occurred_at=datetime.now(UTC).isoformat(),
                    ),
                )
            raise
        finally:
            if connection is not None:
                connection.close()
'''

TESTS = '''from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from finance_agent.storage import SQLiteStore
from finance_agent.storage.migrations import Migration
import finance_agent.storage.store as store_module


def test_existing_database_gets_mode_600_preflight_backup_and_receipt(tmp_path: Path) -> None:
    database = tmp_path / "folio.sqlite3"
    store = SQLiteStore(database)
    store.migrate()
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id, name, entity_type, currency, timezone, protected_reserve_minor, data_through, thread_id, created_at, updated_at) VALUES ('ws_migration_test', 'Test', 'nz_sole_trader', 'NZD', 'Pacific/Auckland', 0, '2026-08-26T00:00:00+00:00', 'thr_migration_test', '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00')"
        )
    store.migrate()
    root = tmp_path / ".migration-backups"
    backups = list(root.glob("folio.sqlite3.pre-v*.sqlite3"))
    assert backups
    assert backups[-1].stat().st_mode & 0o777 == 0o600
    receipt = json.loads((root / "last-migration-receipt.json").read_text())
    assert receipt["status"] == "completed"
    assert receipt["database_filename"] == "folio.sqlite3"
    assert "error" not in receipt


def test_failing_migration_restores_exact_pre_run_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "folio.sqlite3"
    store = SQLiteStore(database)
    store.migrate()
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO workspaces(workspace_id, name, entity_type, currency, timezone, protected_reserve_minor, data_through, thread_id, created_at, updated_at) VALUES ('ws_restore_test', 'Restore', 'nz_sole_trader', 'NZD', 'Pacific/Auckland', 0, '2026-08-26T00:00:00+00:00', 'thr_restore_test', '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00')"
        )
    original = database.read_bytes()
    version = max(migration.version for migration in store_module.MIGRATIONS) + 1
    failing = Migration(
        version=version,
        name="intentional_failure",
        sql="CREATE TABLE should_not_survive(value TEXT); INVALID SQL;",
    )
    monkeypatch.setattr(store_module, "MIGRATIONS", (*store_module.MIGRATIONS, failing))
    with pytest.raises(Exception):
        store.migrate()
    assert database.read_bytes() == original
    assert store.fetch_one(
        "SELECT workspace_id FROM workspaces WHERE workspace_id = 'ws_restore_test'"
    ) is not None
    assert store.fetch_one(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'should_not_survive'"
    ) is None
    receipt = json.loads(
        (tmp_path / ".migration-backups" / "last-migration-receipt.json").read_text()
    )
    assert receipt["status"] == "restored_after_failure"
    assert receipt["error_type"]
    assert "INVALID SQL" not in json.dumps(receipt)


def test_each_migration_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "atomic.sqlite3"
    store = SQLiteStore(database)
    original = store_module.MIGRATIONS
    monkeypatch.setattr(
        store_module,
        "MIGRATIONS",
        (
            Migration(
                version=1,
                name="fails_after_create",
                sql="CREATE TABLE partial_table(value TEXT); BAD TOKEN;",
            ),
        ),
    )
    with pytest.raises(Exception):
        store.migrate()
    assert not database.exists()
    monkeypatch.setattr(store_module, "MIGRATIONS", original)


def test_backup_retention_is_bounded(tmp_path: Path) -> None:
    database = tmp_path / "folio.sqlite3"
    store = SQLiteStore(database)
    store.migrate()
    for _ in range(8):
        store.migrate()
    backups = list((tmp_path / ".migration-backups").glob("folio.sqlite3.pre-v*.sqlite3"))
    assert len(backups) <= 5
'''


def replace_migrate() -> None:
    path = "services/api/src/finance_agent/storage/store.py"
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "SQLiteStore")
    method = next(node for node in owner.body if isinstance(node, ast.FunctionDef) and node.name == "migrate")
    lines = content.splitlines(keepends=True)
    start = method.lineno - 1
    end = method.end_lineno
    write(path, "".join(lines[:start]) + REPLACEMENT.rstrip() + "\n\n" + "".join(lines[end:]))


def docs_tests() -> None:
    write("services/api/src/finance_agent/storage/migration_safety.py", MODULE)
    write("services/api/tests/storage/test_migration_safety.py", TESTS)
    write("docs/MIGRATION_SAFETY.md", '''# SQLite migration safety\n\nBefore migrating an existing Folio database, the store checkpoints WAL, runs SQLite `quick_check` and `foreign_key_check`, verifies at least the greater of 20 MB or three database sizes is free, and creates a SQLite API backup under `.migration-backups` with directory mode `0700` and file mode `0600`. The five newest backups are retained.\n\nEach migration, including its `schema_migrations` row, executes inside one `BEGIN IMMEDIATE` transaction. After all migrations, integrity checks run again. On any failure, Folio closes the connection, removes WAL/SHM state, restores the exact pre-run backup and writes a redacted receipt containing versions and exception type, not database content or the exception message. A failed first-time database is removed instead of leaving a partial schema.\n\nMigration completion proves local schema integrity, not application-level correctness. The normal full verification suite and backup/restore tests remain required before release.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 46: atomic migration preflight and restoration\n\n- Existing databases pass quick/foreign-key checks before schema change.\n- Free-space checks and mode-0600 SQLite backups precede migration.\n- Every migration and version receipt is one immediate transaction.\n- Any failure restores the exact pre-run file and removes partial first-run databases.\n- Redacted migration receipts record versions and error type without sensitive messages.\n- Backup retention is bounded and post-migration integrity is rechecked.\n'''
    if "## Stack 46: atomic migration preflight and restoration" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    replace_migrate()
    docs_tests()
    print("atomic migration safety changes applied")


if __name__ == "__main__":
    main()
