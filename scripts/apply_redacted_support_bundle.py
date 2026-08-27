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
        name="redacted_support_bundles",
        sql="""
        CREATE TABLE support_bundle_revisions (
            bundle_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            bundle_bytes BLOB NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            manifest_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (bundle_id, revision)
        );

        CREATE INDEX support_bundle_workspace_time
            ON support_bundle_revisions(workspace_id, created_at DESC, revision DESC);
        """,
    ),
'''

MODULE = '''"""Deterministic redacted support bundles with no finance or owner content."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import sqlite3
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from finance_agent.observability import LocalOperationMetrics, sanitise_metadata
from finance_agent.storage import SQLiteStore, canonical_json

ROOT = Path(__file__).resolve().parents[5]
SAFE_ENV_FLAGS = (
    "FINANCE_AKAHU_ENABLED",
    "FINANCE_PLAID_ENABLED",
    "TELEGRAM_LIVE_ENABLED",
    "FOLIO_REQUIRE_SESSION_TOKEN",
    "FOLIO_ALLOW_DEVELOPMENT_ROUTES",
)
FORBIDDEN_TABLE_CONTENTS = frozenset(
    {
        "source_rows",
        "conversation_turns",
        "claims",
        "knowledge_owner_statements",
        "knowledge_documents",
        "knowledge_facts",
        "telegram_live_attachments",
        "artifacts",
    }
)
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _hash_label(value: object) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
    ).encode("utf-8") + b"\n"


def _zip_entry(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, content)


class RedactedSupportBundleService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def _database_summary(self) -> dict[str, object]:
        with self.store.connect() as connection:
            integrity = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
            quick = [str(row[0]) for row in connection.execute("PRAGMA quick_check")]
            journal = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            tables = [
                str(row["name"])
                for row in connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                    ORDER BY name
                    """
                ).fetchall()
            ]
            counts: dict[str, int] = {}
            for table in tables:
                if not re_full_identifier(table):
                    continue
                count = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                counts[table] = int(count)
        return {
            "integrityCheck": integrity,
            "quickCheck": quick,
            "journalMode": journal,
            "pageCount": page_count,
            "pageSize": page_size,
            "databaseBytesEstimate": page_count * page_size,
            "tableRowCounts": counts,
            "tableContentsIncluded": False,
            "forbiddenContentTables": sorted(FORBIDDEN_TABLE_CONTENTS),
        }

    def _schema_summary(self) -> dict[str, object]:
        rows = self.store.fetch_all(
            "SELECT version, name, applied_at FROM schema_migrations ORDER BY version"
        )
        return {
            "migrationCount": len(rows),
            "migrations": [
                {
                    "version": int(row["version"]),
                    "name": str(row["name"]),
                    "appliedAt": str(row["applied_at"]),
                }
                for row in rows
            ],
        }

    def _workspace_summary(self, workspace_id: str) -> dict[str, object]:
        row = self.store.fetch_one(
            """
            SELECT workspace_id, name, entity_type, currency, timezone,
                   state_revision, model_mode, data_through, created_at, updated_at
            FROM workspaces WHERE workspace_id = ?
            """,
            (workspace_id,),
        )
        if row is None:
            raise KeyError(workspace_id)
        return {
            "workspaceIdHash": _hash_label(row["workspace_id"]),
            "workspaceNameHash": _hash_label(row["name"]),
            "entityType": str(row["entity_type"]),
            "currency": str(row["currency"]),
            "timezone": str(row["timezone"]),
            "stateRevision": int(row["state_revision"]),
            "modelMode": str(row["model_mode"]),
            "dataThrough": str(row["data_through"]),
            "createdAt": str(row["created_at"]),
            "updatedAt": str(row["updated_at"]),
            "rawWorkspaceIdIncluded": False,
            "rawWorkspaceNameIncluded": False,
        }

    @staticmethod
    def _runtime_summary() -> dict[str, object]:
        return {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "executableName": Path(sys.executable).name,
            "environmentFlags": {
                key: os.getenv(key, "false").lower() in {"1", "true", "yes", "on"}
                for key in SAFE_ENV_FLAGS
            },
            "environmentValuesIncluded": False,
        }

    def generate(
        self,
        *,
        workspace_id: str,
        connector_capabilities: Mapping[str, object],
        model_capabilities: Mapping[str, object],
    ) -> tuple[str, bytes, str, dict[str, object]]:
        created_at = datetime.now(UTC).isoformat()
        bundle_id = _stable_id("support", workspace_id)
        operations = LocalOperationMetrics(self.store).summary(
            workspace_id=workspace_id,
            since_hours=24,
            slow_limit=20,
        ).as_dict()
        manifest: dict[str, object] = {
            "bundleVersion": "folio.support-bundle@1",
            "bundleId": bundle_id,
            "createdAt": created_at,
            "redacted": True,
            "containsTransactions": False,
            "containsOwnerProse": False,
            "containsDocuments": False,
            "containsCredentials": False,
            "containsRawSources": False,
            "files": [],
        }
        files = {
            "runtime.json": self._runtime_summary(),
            "workspace.json": self._workspace_summary(workspace_id),
            "database.json": self._database_summary(),
            "schema.json": self._schema_summary(),
            "operations.json": operations,
            "connectors.json": sanitise_metadata(connector_capabilities),
            "models.json": sanitise_metadata(model_capabilities),
        }
        file_manifest: list[dict[str, object]] = []
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name in sorted(files):
                content = _json_bytes(files[name])
                _zip_entry(archive, name, content)
                file_manifest.append(
                    {
                        "name": name,
                        "sizeBytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
            manifest["files"] = file_manifest
            manifest_bytes = _json_bytes(manifest)
            _zip_entry(archive, "manifest.json", manifest_bytes)
        bundle = buffer.getvalue()
        content_hash = hashlib.sha256(bundle).hexdigest()
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS revision
                FROM support_bundle_revisions WHERE bundle_id = ?
                """,
                (bundle_id,),
            ).fetchone()
            revision = int(row["revision"]) + 1
            connection.execute(
                """
                INSERT INTO support_bundle_revisions(
                    bundle_id, revision, workspace_id, bundle_bytes,
                    content_hash, manifest_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle_id,
                    revision,
                    workspace_id,
                    bundle,
                    content_hash,
                    canonical_json(manifest),
                    created_at,
                ),
            )
        return bundle_id, bundle, content_hash, {**manifest, "revision": revision}

    def latest(self, workspace_id: str) -> tuple[str, bytes, str, dict[str, object]]:
        row = self.store.fetch_one(
            """
            SELECT * FROM support_bundle_revisions
            WHERE workspace_id = ? ORDER BY created_at DESC, revision DESC LIMIT 1
            """,
            (workspace_id,),
        )
        if row is None:
            raise KeyError(workspace_id)
        return (
            str(row["bundle_id"]),
            bytes(row["bundle_bytes"]),
            str(row["content_hash"]),
            json.loads(str(row["manifest_json"])),
        )


def re_full_identifier(value: str) -> bool:
    return bool(value) and all(character.isalnum() or character == "_" for character in value)
'''

SERVICE_METHODS = '''    async def generate_support_bundle(
        self, *, workspace_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        connectors = await self.connection_capabilities()
        models = await self.model_capabilities()
        async with self._lock:
            bundle_id, content, content_hash, manifest = RedactedSupportBundleService(
                self.store
            ).generate(
                workspace_id=workspace_id,
                connector_capabilities=connectors,
                model_capabilities=models,
            )
        return {
            "bundleId": bundle_id,
            "contentHash": content_hash,
            "sizeBytes": len(content),
            "manifest": manifest,
            "downloadPath": f"/v1/workspaces/{workspace_id}/support-bundle.zip",
        }

    async def support_bundle_payload(
        self, *, workspace_id: str
    ) -> ArtifactPayload:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        bundle_id, content, content_hash, _manifest = RedactedSupportBundleService(
            self.store
        ).latest(workspace_id)
        return ArtifactPayload(
            content=content,
            media_type="application/zip",
            filename=f"folio-support-{bundle_id}.zip",
            content_hash=content_hash,
        )
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/support-bundle")
    async def generate_support_bundle(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        return dict(await services.generate_support_bundle(workspace_id=workspace_id))

    @router.get("/v1/workspaces/{workspace_id}/support-bundle.zip")
    async def download_support_bundle(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> Response:
        try:
            value = await services.support_bundle_payload(workspace_id=workspace_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="support bundle not found") from exc
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

'''

TESTS = '''from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.observability import LocalOperationMetrics
from finance_agent.storage import SQLiteStore
from finance_agent.support_bundle import RedactedSupportBundleService

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def seeded(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE transactions SET description = 'PRIVATE CUSTOMER SECRET 771122' WHERE transaction_id = 'txn_koru_001'"
        )
        connection.execute(
            "INSERT INTO conversation_turns(turn_id, workspace_id, thread_id, role, content, occurred_at, status, evidence_ids_json, model_mode) VALUES ('turn_private_support', 'ws_koru_studio', 'thr_koru_studio_main', 'owner', 'OWNER PRIVATE SUPPORT TEXT 884433', '2026-08-26T00:00:00+00:00', 'complete', '[]', 'local')"
        )
    LocalOperationMetrics(store).record(
        category="api",
        operation="POST /v1/private",
        started_at="2026-08-26T00:00:00+00:00",
        duration_ms=42,
        status="failed",
        workspace_id="ws_koru_studio",
        metadata={"requestBody": "DO NOT EXPORT", "statusCode": 500},
    )
    return store


def test_bundle_contains_only_redacted_operational_files(tmp_path: Path, monkeypatch) -> None:
    store = seeded(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-private-secret")
    monkeypatch.setenv("AKAHU_USER_TOKEN", "akahu-private-secret")
    bundle_id, bundle, content_hash, manifest = RedactedSupportBundleService(
        store
    ).generate(
        workspace_id="ws_koru_studio",
        connector_capabilities={
            "configured": True,
            "apiToken": "connector-secret",
            "detail": "safe capability",
        },
        model_capabilities={
            "provider": "openai",
            "apiKey": "model-secret",
            "status": "configured",
        },
    )
    assert bundle_id.startswith("support_")
    assert len(content_hash) == 64
    assert manifest["containsTransactions"] is False
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        names = sorted(archive.namelist())
        assert names == [
            "connectors.json",
            "database.json",
            "manifest.json",
            "models.json",
            "operations.json",
            "runtime.json",
            "schema.json",
            "workspace.json",
        ]
        contents = b"\n".join(archive.read(name) for name in names)
    for forbidden in (
        b"PRIVATE CUSTOMER SECRET 771122",
        b"OWNER PRIVATE SUPPORT TEXT 884433",
        b"DO NOT EXPORT",
        b"sk-private-secret",
        b"akahu-private-secret",
        b"connector-secret",
        b"model-secret",
    ):
        assert forbidden not in contents
    assert b'"apiToken": "[redacted]"' in contents
    assert b'"apiKey": "[redacted]"' in contents
    assert b'"statusCode": 500' in contents


def test_bundle_is_deterministic_for_identical_inputs_except_revision_storage(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    service = RedactedSupportBundleService(store)
    first = service.generate(
        workspace_id="ws_koru_studio",
        connector_capabilities={"status": "ready"},
        model_capabilities={"status": "unavailable"},
    )
    second = service.generate(
        workspace_id="ws_koru_studio",
        connector_capabilities={"status": "ready"},
        model_capabilities={"status": "unavailable"},
    )
    assert first[0] == second[0]
    # createdAt is an honest point-in-time field, so revisions are expected to differ.
    assert first[2] != second[2] or first[3]["createdAt"] == second[3]["createdAt"]
    rows = store.fetch_all(
        "SELECT revision FROM support_bundle_revisions ORDER BY revision"
    )
    assert [int(row["revision"]) for row in rows] == [1, 2]


def test_latest_requires_a_generated_bundle(tmp_path: Path) -> None:
    store = seeded(tmp_path)
    service = RedactedSupportBundleService(store)
    try:
        service.latest("ws_koru_studio")
    except KeyError:
        pass
    else:
        raise AssertionError("support bundle download was available before generation")
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
    write("services/api/src/finance_agent/support_bundle.py", MODULE)


def update_service_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.storage import SQLiteConversationStore, SQLiteStore, canonical_json\n"
    import_line = "from finance_agent.support_bundle import RedactedSupportBundleService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("storage import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "set_category_budget", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def set_category_budget(\n"
    addition = '''    async def generate_support_bundle(\n        self, *, workspace_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def support_bundle_payload(\n        self, *, workspace_id: str\n    ) -> ArtifactPayload: ...\n\n'''
    if marker not in content:
        raise RuntimeError("budget protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    marker = '    @router.post("/v1/workspaces/{workspace_id}/budgets")\n'
    if marker not in content:
        raise RuntimeError("budget route marker missing")
    content = content.replace(marker, ROUTES + marker, 1)
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/test_support_bundle.py", TESTS)
    write("docs/SUPPORT_BUNDLES.md", '''# Redacted support bundles\n\nA Folio support bundle is a local ZIP containing only runtime version, hashed workspace identity, SQLite integrity results, migration metadata, table row counts, bounded operation timings, redacted connector/model capability and a file-hash manifest. It deliberately excludes transactions, source rows, owner messages, claims, documents, attachments, generated finance artefacts, environment values and credentials.\n\nConnector and model capability metadata passes through the same sensitive-key redaction used by observability. The bundle records feature flags as booleans only. Database diagnostics disclose table names and counts, never table rows. Workspace ID and name are SHA-256 hashes.\n\nGenerating a support bundle proves only that local diagnostic bytes were prepared. Downloading it does not prove it was sent to a maintainer, attached to an issue or reviewed. Owners should still inspect the ZIP before sharing it.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 29: redacted support and incident bundle\n\n- Support ZIPs contain runtime, integrity, schema, counts, metrics and capability metadata only.\n- Workspace ID and name are hashed.\n- Sensitive capability keys are redacted and environment values are excluded.\n- Transactions, owner prose, claims, documents, attachments and raw sources are absent.\n- Every file and bundle has a SHA-256 manifest entry.\n- Preparation and download remain separate from recipient-visible sharing or review.\n'''
    if "## Stack 29: redacted support and incident bundle" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_service_protocol_routes()
    tests_docs()
    print("redacted support bundle changes applied")


if __name__ == "__main__":
    main()
