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
        name="portable_workspace_bundles",
        sql="""
        CREATE TABLE portable_bundle_receipts (
            receipt_id TEXT PRIMARY KEY,
            operation TEXT NOT NULL CHECK (operation IN ('exported', 'imported')),
            database_sha256 TEXT NOT NULL CHECK (length(database_sha256) = 64),
            manifest_sha256 TEXT NOT NULL CHECK (length(manifest_sha256) = 64),
            table_counts_json TEXT NOT NULL,
            excluded_tables_json TEXT NOT NULL,
            archive_size_bytes INTEGER NOT NULL CHECK (archive_size_bytes >= 0),
            created_at TEXT NOT NULL
        );

        CREATE TABLE portable_restore_candidates (
            candidate_id TEXT PRIMARY KEY,
            receipt_id TEXT NOT NULL REFERENCES portable_bundle_receipts(receipt_id),
            filename TEXT NOT NULL,
            database_sha256 TEXT NOT NULL CHECK (length(database_sha256) = 64),
            status TEXT NOT NULL CHECK (status IN ('validated', 'rejected')),
            created_at TEXT NOT NULL
        );

        CREATE INDEX portable_bundle_receipts_time
            ON portable_bundle_receipts(created_at DESC);
        """,
    ),
'''

MODULE = '''"""Portable, scrubbed SQLite bundles with human-readable JSONL and safe restore candidates."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from finance_agent.storage import SQLiteStore, canonical_json

BUNDLE_VERSION = "folio.portable-bundle@1"
MAX_ARCHIVE_BYTES = 10_000_000
MAX_UNCOMPRESSED_BYTES = 100_000_000
MAX_ENTRIES = 1000
SENSITIVE_MARKERS = ("token", "session", "credential", "secret")
IGNORED_PREFIXES = ("sqlite_", "knowledge_fts_")
IGNORED_TABLES = frozenset({"knowledge_fts", "schema_migrations"})


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:24]}"


def _is_sensitive(name: str) -> bool:
    value = name.casefold()
    return any(marker in value for marker in SENSITIVE_MARKERS)


def _is_readable_table(name: str) -> bool:
    return (
        name not in IGNORED_TABLES
        and not any(name.startswith(prefix) for prefix in IGNORED_PREFIXES)
        and not _is_sensitive(name)
    )


def _json_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"$base64": base64.b64encode(value).decode("ascii")}
    return value


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    return info


def _safe_entries(archive: zipfile.ZipFile) -> tuple[zipfile.ZipInfo, ...]:
    entries = tuple(archive.infolist())
    if len(entries) > MAX_ENTRIES:
        raise ValueError("portable bundle contains too many entries")
    total = 0
    for entry in entries:
        path = Path(entry.filename)
        if path.is_absolute() or ".." in path.parts or entry.filename.endswith("/"):
            raise ValueError("portable bundle contains an unsafe entry path")
        total += entry.file_size
        if total > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("portable bundle expands beyond the local byte limit")
    return entries


class PortableWorkspaceBundleService:
    def __init__(self, store: SQLiteStore, *, restore_root: str | Path) -> None:
        self.store = store
        self.restore_root = Path(restore_root)

    @staticmethod
    def _user_tables(connection: sqlite3.Connection) -> tuple[str, ...]:
        return tuple(
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        )

    @staticmethod
    def _schema_hash(connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            """
            SELECT type, name, tbl_name, sql FROM sqlite_master
            WHERE name NOT LIKE 'sqlite_%'
            ORDER BY type, name
            """
        ).fetchall()
        return hashlib.sha256(
            canonical_json([list(row) for row in rows]).encode()
        ).hexdigest()

    @staticmethod
    def _quick_check(path: Path) -> None:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
            if row is None or str(row[0]).lower() != "ok":
                raise ValueError("portable SQLite snapshot failed quick_check")
        finally:
            connection.close()

    def _snapshot(self, target: Path) -> None:
        source = sqlite3.connect(self.store.database_path)
        destination = sqlite3.connect(target)
        try:
            source.execute("PRAGMA wal_checkpoint(FULL)")
            source.backup(destination)
            destination.execute("PRAGMA foreign_keys = OFF")
            for table in self._user_tables(destination):
                if _is_sensitive(table):
                    destination.execute(f'DELETE FROM "{table}"')
            destination.commit()
            destination.execute("VACUUM")
        finally:
            destination.close()
            source.close()
        os.chmod(target, 0o600)
        self._quick_check(target)

    @staticmethod
    def _table_payloads(
        connection: sqlite3.Connection,
    ) -> tuple[dict[str, int], dict[str, str], dict[str, bytes], list[str]]:
        connection.row_factory = sqlite3.Row
        counts: dict[str, int] = {}
        digests: dict[str, str] = {}
        payloads: dict[str, bytes] = {}
        excluded: list[str] = []
        tables = PortableWorkspaceBundleService._user_tables(connection)
        for table in tables:
            if not _is_readable_table(table):
                excluded.append(table)
                continue
            rows = connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            lines = [
                canonical_json({key: _json_value(row[key]) for key in row.keys()})
                for row in rows
            ]
            content = ("\n".join(lines) + ("\n" if lines else "")).encode()
            counts[table] = len(rows)
            digests[table] = hashlib.sha256(content).hexdigest()
            payloads[table] = content
        return counts, digests, payloads, sorted(excluded)

    def export_bundle(self) -> tuple[str, bytes, str, str]:
        now = datetime.now(UTC).isoformat()
        with tempfile.TemporaryDirectory(prefix="folio-portable-") as directory:
            snapshot = Path(directory) / "database.sqlite3"
            self._snapshot(snapshot)
            database_bytes = snapshot.read_bytes()
            database_sha = hashlib.sha256(database_bytes).hexdigest()
            connection = sqlite3.connect(snapshot)
            try:
                counts, table_digests, payloads, excluded = self._table_payloads(connection)
                schema_hash = self._schema_hash(connection)
            finally:
                connection.close()
            manifest = {
                "bundleVersion": BUNDLE_VERSION,
                "createdAt": now,
                "databaseFilename": "database.sqlite3",
                "databaseSha256": database_sha,
                "schemaSha256": schema_hash,
                "tableCounts": counts,
                "tableJsonlSha256": table_digests,
                "excludedTables": excluded,
                "ephemeralSecurityStateIncluded": False,
                "encrypted": False,
                "automaticActivation": False,
            }
            manifest_bytes = (canonical_json(manifest) + "\n").encode()
            manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr(_zip_info("manifest.json"), manifest_bytes)
                archive.writestr(_zip_info("database.sqlite3"), database_bytes)
                for table in sorted(payloads):
                    archive.writestr(_zip_info(f"tables/{table}.jsonl"), payloads[table])
                archive.writestr(
                    _zip_info("README.txt"),
                    (
                        "Folio portable local-data bundle. The SQLite snapshot is the restore source. "
                        "JSONL files are human-readable inspection copies. Session/token tables were scrubbed. "
                        "This plaintext archive should be stored securely and is never auto-activated.\n"
                    ).encode(),
                )
            archive_bytes = output.getvalue()
            if len(archive_bytes) > MAX_ARCHIVE_BYTES:
                raise ValueError(
                    "portable bundle exceeds 10 MB; use Folio's encrypted backup path for this database"
                )
        receipt_id = _stable_id("portablebundle", "exported", database_sha, manifest_sha)
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO portable_bundle_receipts(
                    receipt_id, operation, database_sha256, manifest_sha256,
                    table_counts_json, excluded_tables_json,
                    archive_size_bytes, created_at
                ) VALUES (?, 'exported', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_id) DO NOTHING
                """,
                (
                    receipt_id,
                    database_sha,
                    manifest_sha,
                    canonical_json(counts),
                    canonical_json(excluded),
                    len(archive_bytes),
                    now,
                ),
            )
        return (
            f"folio-portable-{now[:10]}.zip",
            archive_bytes,
            hashlib.sha256(archive_bytes).hexdigest(),
            receipt_id,
        )

    def import_candidate(self, archive_bytes: bytes) -> dict[str, object]:
        if not archive_bytes or len(archive_bytes) > MAX_ARCHIVE_BYTES:
            raise ValueError("portable bundle must be between 1 byte and 10 MB")
        with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
            entries = _safe_entries(archive)
            names = {entry.filename for entry in entries}
            if not {"manifest.json", "database.sqlite3"}.issubset(names):
                raise ValueError("portable bundle is missing manifest.json or database.sqlite3")
            manifest_bytes = archive.read("manifest.json")
            if len(manifest_bytes) > 1_000_000:
                raise ValueError("portable manifest exceeds 1 MB")
            manifest = json.loads(manifest_bytes)
            expected_keys = {
                "bundleVersion", "createdAt", "databaseFilename", "databaseSha256",
                "schemaSha256", "tableCounts", "tableJsonlSha256", "excludedTables",
                "ephemeralSecurityStateIncluded", "encrypted", "automaticActivation",
            }
            if not isinstance(manifest, dict) or set(manifest) != expected_keys:
                raise ValueError("portable manifest does not match the closed schema")
            if manifest["bundleVersion"] != BUNDLE_VERSION:
                raise ValueError("unsupported portable bundle version")
            if manifest["ephemeralSecurityStateIncluded"] is not False:
                raise ValueError("portable bundle claims to include ephemeral security state")
            if manifest["automaticActivation"] is not False:
                raise ValueError("portable bundle may not request automatic activation")
            database_bytes = archive.read("database.sqlite3")
            database_sha = hashlib.sha256(database_bytes).hexdigest()
            if database_sha != manifest["databaseSha256"]:
                raise ValueError("portable database SHA-256 does not match the manifest")
            for table, digest in manifest["tableJsonlSha256"].items():
                name = f"tables/{table}.jsonl"
                if name not in names or hashlib.sha256(archive.read(name)).hexdigest() != digest:
                    raise ValueError(f"portable JSONL digest mismatch for {table}")
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        self.restore_root.mkdir(parents=True, exist_ok=True)
        candidate_id = _stable_id("restorecandidate", database_sha, manifest_sha)
        filename = f"{candidate_id}.sqlite3"
        target = self.restore_root / filename
        if not target.exists():
            with tempfile.NamedTemporaryFile(
                dir=self.restore_root, prefix=".incoming-", delete=False
            ) as temporary:
                temporary.write(database_bytes)
                temporary.flush()
                os.fsync(temporary.fileno())
                incoming = Path(temporary.name)
            try:
                os.chmod(incoming, 0o600)
                self._quick_check(incoming)
                connection = sqlite3.connect(f"file:{incoming}?mode=ro", uri=True)
                try:
                    for table in self._user_tables(connection):
                        if _is_sensitive(table):
                            count = int(
                                connection.execute(
                                    f'SELECT COUNT(*) FROM "{table}"'
                                ).fetchone()[0]
                            )
                            if count:
                                raise ValueError(
                                    f"portable snapshot contains non-empty sensitive table {table}"
                                )
                finally:
                    connection.close()
                os.replace(incoming, target)
            finally:
                incoming.unlink(missing_ok=True)
        receipt_id = _stable_id("portablebundle", "imported", database_sha, manifest_sha)
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO portable_bundle_receipts(
                    receipt_id, operation, database_sha256, manifest_sha256,
                    table_counts_json, excluded_tables_json,
                    archive_size_bytes, created_at
                ) VALUES (?, 'imported', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_id) DO NOTHING
                """,
                (
                    receipt_id,
                    database_sha,
                    manifest_sha,
                    canonical_json(manifest["tableCounts"]),
                    canonical_json(manifest["excludedTables"]),
                    len(archive_bytes),
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO portable_restore_candidates(
                    candidate_id, receipt_id, filename, database_sha256,
                    status, created_at
                ) VALUES (?, ?, ?, ?, 'validated', ?)
                ON CONFLICT(candidate_id) DO NOTHING
                """,
                (candidate_id, receipt_id, filename, database_sha, now),
            )
        return {
            "candidateId": candidate_id,
            "receiptId": receipt_id,
            "filename": filename,
            "databaseSha256": database_sha,
            "manifestSha256": manifest_sha,
            "tableCounts": manifest["tableCounts"],
            "excludedTables": manifest["excludedTables"],
            "status": "validated",
            "liveDatabaseChanged": False,
            "automaticActivation": False,
        }
'''

SERVICE_METHODS = '''    async def portable_data_export(self) -> ArtifactPayload:
        service = PortableWorkspaceBundleService(
            self.store,
            restore_root=ROOT / "var" / "restore-candidates",
        )
        filename, content, content_hash, _receipt_id = service.export_bundle()
        return ArtifactPayload(
            content=content,
            media_type="application/zip",
            filename=filename,
            content_hash=content_hash,
        )

    async def portable_data_import(
        self, *, content: bytes
    ) -> Mapping[str, object]:
        async with self._lock:
            return PortableWorkspaceBundleService(
                self.store,
                restore_root=ROOT / "var" / "restore-candidates",
            ).import_candidate(content)
'''

ROUTES = '''    @router.get("/v1/system/portable-export")
    async def portable_data_export(services: Services) -> Response:
        value = await services.portable_data_export()
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

    @router.post("/v1/system/portable-import")
    async def portable_data_import(
        services: Services,
        file: Annotated[UploadFile, File()],
    ) -> dict[str, object]:
        try:
            content = await read_upload_with_limit(file, max_bytes=10_000_000)
            return dict(await services.portable_data_import(content=content))
        except UploadTooLarge as exc:
            raise HTTPException(status_code=413, detail="portable bundle exceeds 10 MB") from exc
        except (ValueError, zipfile.BadZipFile) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from finance_agent.finance import FinanceEngine
from finance_agent.storage import SQLiteStore
from finance_agent.storage.portable_bundle import PortableWorkspaceBundleService

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    with store.transaction() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS local_access_sessions(
                session_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO local_access_sessions(session_id, token_hash) VALUES ('sess_test', 'secret_hash')"
        )
    service = PortableWorkspaceBundleService(
        store, restore_root=tmp_path / "restore-candidates"
    )
    return store, service


def test_export_contains_snapshot_manifest_and_jsonl_but_scrubs_sessions(tmp_path: Path) -> None:
    _store, service = setup(tmp_path)
    filename, content, archive_hash, receipt_id = service.export_bundle()
    assert filename.endswith(".zip")
    assert hashlib.sha256(content).hexdigest() == archive_hash
    assert receipt_id.startswith("portablebundle_")
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "database.sqlite3", "README.txt"}.issubset(names)
        assert "tables/transactions.jsonl" in names
        assert "tables/local_access_sessions.jsonl" not in names
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["ephemeralSecurityStateIncluded"] is False
        assert manifest["automaticActivation"] is False
        snapshot = tmp_path / "exported.sqlite3"
        snapshot.write_bytes(archive.read("database.sqlite3"))
    connection = sqlite3.connect(snapshot)
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM local_access_sessions"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_import_creates_validated_candidate_without_changing_live_database(tmp_path: Path) -> None:
    store, service = setup(tmp_path)
    before = store.fetch_one("SELECT COUNT(*) AS count FROM transactions")["count"]
    _filename, content, _hash, _receipt = service.export_bundle()
    value = service.import_candidate(content)
    assert value["status"] == "validated"
    assert value["liveDatabaseChanged"] is False
    assert value["automaticActivation"] is False
    candidate = tmp_path / "restore-candidates" / value["filename"]
    assert candidate.exists()
    assert candidate.stat().st_mode & 0o777 == 0o600
    assert store.fetch_one("SELECT COUNT(*) AS count FROM transactions")["count"] == before
    connection = sqlite3.connect(candidate)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_tampered_database_and_path_traversal_fail_closed(tmp_path: Path) -> None:
    _store, service = setup(tmp_path)
    _filename, content, _hash, _receipt = service.export_bundle()
    with zipfile.ZipFile(io.BytesIO(content)) as source:
        manifest = source.read("manifest.json")
        database = bytearray(source.read("database.sqlite3"))
    database[-1] ^= 1
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("manifest.json", manifest)
        archive.writestr("database.sqlite3", bytes(database))
    with pytest.raises(ValueError, match="SHA-256"):
        service.import_candidate(output.getvalue())
    traversal = io.BytesIO()
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../manifest.json", b"{}")
        archive.writestr("database.sqlite3", b"not sqlite")
    with pytest.raises(ValueError, match="unsafe entry"):
        service.import_candidate(traversal.getvalue())


def test_jsonl_digest_tampering_fails_before_candidate_creation(tmp_path: Path) -> None:
    _store, service = setup(tmp_path)
    _filename, content, _hash, _receipt = service.export_bundle()
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(content)) as source, zipfile.ZipFile(output, "w") as target:
        for entry in source.infolist():
            payload = source.read(entry.filename)
            if entry.filename == "tables/transactions.jsonl":
                payload += b"{}\n"
            target.writestr(entry.filename, payload)
    with pytest.raises(ValueError, match="JSONL digest mismatch"):
        service.import_candidate(output.getvalue())
'''


def add_migration_module() -> None:
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
    write("services/api/src/finance_agent/storage/portable_bundle.py", MODULE)


def update_service_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.storage import SQLiteConversationStore, SQLiteStore, canonical_json\n"
    import_line = "from finance_agent.storage.portable_bundle import PortableWorkspaceBundleService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("storage import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "save_accounting_export_profile", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def save_accounting_export_profile(\n"
    addition = '''    async def portable_data_export(self) -> ArtifactPayload: ...\n\n    async def portable_data_import(\n        self, *, content: bytes\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("accounting export protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    if "import zipfile\n" not in content:
        content = content.replace("from __future__ import annotations\n", "from __future__ import annotations\n\nimport zipfile\n", 1)
    marker = '    @router.post("/v1/workspaces/{workspace_id}/accounting-exports/profiles")\n'
    if marker not in content:
        raise RuntimeError("accounting export route marker missing")
    content = content.replace(marker, ROUTES + marker, 1)
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/storage/test_portable_workspace_bundle.py", TESTS)
    write("docs/PORTABLE_DATA.md", '''# Portable local-data bundles\n\nA Folio portable bundle contains a consistent SQLite snapshot, a closed hashed manifest and human-readable JSONL copies of non-sensitive user tables. Before packaging, Folio copies the database, clears any table whose name indicates token, session, credential or secret state, runs SQLite `quick_check`, hashes the schema, database and every JSONL file, and caps the compressed archive at 10 MB. The archive is plaintext and must be stored securely.\n\nImport verifies entry paths, entry count, expanded bytes, manifest keys/version, snapshot hash, every JSONL hash, SQLite integrity and that sensitive tables are empty. A valid bundle is written with mode `0600` into `var/restore-candidates` and receipted. It does not replace, merge into or activate the live database.\n\nPortable export complements, rather than replaces, encrypted backup. Large databases must use the encrypted backup path until a resumable streaming portable format is implemented. Activation of a restore candidate remains a separate destructive operation requiring explicit owner review.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 45: portable scrubbed local-data bundles\n\n- Consistent SQLite snapshots include hashed manifests and human-readable JSONL.\n- Session, token, credential and secret tables are scrubbed and omitted from JSONL.\n- Paths, entry count, expanded size, hashes and SQLite integrity validate on import.\n- Valid imports become mode-0600 restore candidates only.\n- The live database is never replaced or merged automatically.\n- Plaintext portable bundles remain distinct from encrypted backups and are capped at 10 MB.\n'''
    if "## Stack 45: portable scrubbed local-data bundles" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_service_protocol_routes()
    tests_docs()
    print("portable workspace bundle changes applied")


if __name__ == "__main__":
    main()
