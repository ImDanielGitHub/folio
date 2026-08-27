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
        name="csv_import_previews",
        sql="""
        CREATE TABLE csv_import_previews (
            preview_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            filename TEXT NOT NULL,
            source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
            selected_account_id TEXT REFERENCES accounts(account_id),
            base_mapping_version TEXT NOT NULL,
            resolved_mapping_version TEXT NOT NULL,
            summary_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            committed_at TEXT,
            committed_source_item_id TEXT REFERENCES source_items(source_item_id)
        );

        CREATE INDEX csv_import_preview_expiry
            ON csv_import_previews(workspace_id, expires_at, committed_at);
        """,
    ),
'''

MODULE = '''"""Non-mutating CSV inspection followed by exact-byte guarded commitment."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import PurePath
from typing import Any

from finance_agent.finance.ingest import (
    REQUIRED_COLUMNS,
    CSVImporter,
    CSVIngestError,
    ParsedCSV,
    ParsedRow,
    _detect_header_mapping,
    _mapped_rows,
    _read_csv,
    _validate_row,
    stable_id,
)
from finance_agent.finance.service import FinanceEngine
from finance_agent.storage import SQLiteStore, canonical_json

PREVIEW_TTL_MINUTES = 30
BASE_MAPPING_VERSION = "bank_csv_preview@1"
MAX_SAMPLE_ROWS = 10


def _filename(value: str) -> str:
    name = PurePath(value.replace("\\", "/")).name.strip()
    if not name.lower().endswith(".csv"):
        raise CSVIngestError("source file must use a .csv name")
    return (name or "source.csv")[:255]


def _account(store: SQLiteStore, workspace_id: str, account_id: str | None) -> str:
    if account_id:
        row = store.fetch_one(
            "SELECT account_id FROM accounts WHERE workspace_id = ? AND account_id = ?",
            (workspace_id, account_id),
        )
        if row is None:
            raise CSVIngestError("selected account does not belong to this workspace")
        return str(row["account_id"])
    rows = store.fetch_all(
        "SELECT account_id FROM accounts WHERE workspace_id = ? ORDER BY account_id",
        (workspace_id,),
    )
    if not rows:
        raise CSVIngestError("workspace has no account for this bank statement")
    if len(rows) != 1:
        raise CSVIngestError(
            "workspace has multiple accounts; select accountId before previewing this statement"
        )
    return str(rows[0]["account_id"])


def _hard_lock_status(store: SQLiteStore, workspace_id: str, occurred_on: str) -> str | None:
    exists = store.fetch_one(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'accounting_period_revisions'"
    )
    if exists is None:
        return None
    row = store.fetch_one(
        """
        SELECT p.status FROM accounting_period_revisions p
        WHERE p.workspace_id = ? AND p.period_start <= ? AND p.period_end >= ?
          AND p.revision = (
            SELECT MAX(p2.revision) FROM accounting_period_revisions p2
            WHERE p2.period_id = p.period_id
          )
        ORDER BY CASE p.status WHEN 'hard_locked' THEN 3 WHEN 'soft_locked' THEN 2 ELSE 1 END DESC
        LIMIT 1
        """,
        (workspace_id, occurred_on, occurred_on),
    )
    return str(row["status"]) if row else None


class CSVImportPreviewService:
    def __init__(self, store: SQLiteStore, engine: FinanceEngine) -> None:
        self.store = store
        self.engine = engine

    def _parse(
        self,
        *,
        workspace_id: str,
        content: bytes,
        account_id: str | None,
    ) -> tuple[ParsedCSV, str | None, tuple[str, ...], str]:
        digest = hashlib.sha256(content).hexdigest()
        fieldnames, raw_rows = _read_csv(content)
        selected: str | None = None
        if fieldnames == REQUIRED_COLUMNS:
            parsed = ParsedCSV(
                rows=tuple(ParsedRow(canonical=row, raw=row) for row in raw_rows),
                mapping_version=BASE_MAPPING_VERSION,
            )
            account_ids = tuple(
                dict.fromkeys(row.canonical["account_id"].strip() for row in parsed.rows)
            )
            for value in account_ids:
                _account(self.store, workspace_id, value)
            if account_id and account_id not in account_ids:
                raise CSVIngestError(
                    "accountId does not match any canonical account_id in the statement"
                )
        else:
            selected = _account(self.store, workspace_id, account_id)
            mapping = _detect_header_mapping(fieldnames)
            parsed = _mapped_rows(
                raw_rows,
                mapping=mapping,
                account_id=selected,
                source_digest=digest,
                mapping_version=BASE_MAPPING_VERSION,
            )
            account_ids = (selected,)
        return parsed, selected, fieldnames, digest

    def preview(
        self,
        *,
        workspace_id: str,
        filename: str,
        content: bytes,
        account_id: str | None = None,
    ) -> dict[str, object]:
        name = _filename(filename)
        if not content:
            raise CSVIngestError("CSV file is empty")
        parsed, selected, fieldnames, digest = self._parse(
            workspace_id=workspace_id,
            content=content,
            account_id=account_id,
        )
        amounts: list[int] = []
        dates: list[str] = []
        pending = 0
        generated_references = 0
        external_references: set[str] = set()
        sample: list[dict[str, object]] = []
        hard_locked = 0
        soft_locked = 0
        for index, parsed_row in enumerate(parsed.rows, start=1):
            amount, row_id = _validate_row(parsed_row.canonical, index)
            reference = parsed_row.canonical["external_reference"].strip()
            if reference in external_references:
                raise CSVIngestError(
                    f"row {index}: external_reference is duplicated within this statement"
                )
            external_references.add(reference)
            if reference.startswith("bankref_"):
                generated_references += 1
            occurred_on = parsed_row.canonical["occurred_on"]
            period_status = _hard_lock_status(self.store, workspace_id, occurred_on)
            hard_locked += period_status == "hard_locked"
            soft_locked += period_status == "soft_locked"
            amounts.append(amount)
            dates.append(occurred_on)
            pending += parsed_row.canonical["status"] == "pending"
            if len(sample) < MAX_SAMPLE_ROWS:
                sample.append(
                    {
                        "rowNumber": index,
                        "sourceRowId": row_id,
                        "accountId": parsed_row.canonical["account_id"],
                        "occurredOn": occurred_on,
                        "description": parsed_row.canonical["description"],
                        "amountMinor": amount,
                        "currency": parsed_row.canonical["currency"],
                        "status": parsed_row.canonical["status"],
                        "externalReference": reference,
                        "periodStatus": period_status,
                    }
                )
        duplicate = self.store.fetch_one(
            """
            SELECT source_item_id FROM source_items
            WHERE workspace_id = ? AND digest = ? AND mapping_version = ?
            """,
            (workspace_id, digest, parsed.mapping_version),
        )
        warnings: list[str] = []
        if pending:
            warnings.append(
                f"{pending} pending row{'s' if pending != 1 else ''} will not be treated as cleared cash."
            )
        if generated_references:
            warnings.append(
                f"{generated_references} row{'s' if generated_references != 1 else ''} have generated references because the export did not provide stable bank references."
            )
        if hard_locked:
            warnings.append(
                f"{hard_locked} row{'s' if hard_locked != 1 else ''} fall inside a hard-locked accounting period and commitment will fail until the period is explicitly reopened."
            )
        if soft_locked:
            warnings.append(
                f"{soft_locked} row{'s' if soft_locked != 1 else ''} fall inside a soft-locked review period."
            )
        if amounts and all(value >= 0 for value in amounts):
            warnings.append("Every row is an inflow; confirm the bank export sign convention.")
        if amounts and all(value <= 0 for value in amounts):
            warnings.append("Every row is an outflow; confirm the bank export sign convention.")
        now = datetime.now(UTC)
        preview_id = stable_id(
            "csvpreview",
            workspace_id,
            digest,
            parsed.mapping_version,
            selected or "canonical_accounts",
            now.isoformat(),
        )
        summary = {
            "previewVersion": "folio.csv-import-preview@1",
            "previewId": preview_id,
            "workspaceId": workspace_id,
            "filename": name,
            "sourceSha256": digest,
            "selectedAccountId": selected,
            "headers": list(fieldnames),
            "resolvedMappingVersion": parsed.mapping_version,
            "rowCount": len(parsed.rows),
            "dateRange": {
                "start": min(dates),
                "end": max(dates),
            },
            "totals": {
                "currency": "NZD",
                "inflowMinor": sum(max(value, 0) for value in amounts),
                "outflowMinor": sum(abs(min(value, 0)) for value in amounts),
                "netMinor": sum(amounts),
            },
            "pendingRowCount": pending,
            "hardLockedRowCount": hard_locked,
            "softLockedRowCount": soft_locked,
            "generatedReferenceCount": generated_references,
            "duplicateImport": duplicate is not None,
            "existingSourceItemId": str(duplicate["source_item_id"]) if duplicate else None,
            "warnings": warnings,
            "sampleRows": sample,
            "sampleTruncated": len(parsed.rows) > len(sample),
            "committed": False,
            "createdAt": now.isoformat(),
            "expiresAt": (now + timedelta(minutes=PREVIEW_TTL_MINUTES)).isoformat(),
        }
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO csv_import_previews(
                    preview_id, workspace_id, filename, source_sha256,
                    selected_account_id, base_mapping_version,
                    resolved_mapping_version, summary_json, created_at, expires_at,
                    committed_at, committed_source_item_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    preview_id,
                    workspace_id,
                    name,
                    digest,
                    selected,
                    BASE_MAPPING_VERSION,
                    parsed.mapping_version,
                    canonical_json(summary),
                    now.isoformat(),
                    summary["expiresAt"],
                ),
            )
        return summary

    def commit(
        self,
        *,
        workspace_id: str,
        preview_id: str,
        filename: str,
        content: bytes,
    ) -> dict[str, object]:
        row = self.store.fetch_one(
            """
            SELECT * FROM csv_import_previews
            WHERE workspace_id = ? AND preview_id = ?
            """,
            (workspace_id, preview_id),
        )
        if row is None:
            raise KeyError(preview_id)
        digest = hashlib.sha256(content).hexdigest()
        if digest != str(row["source_sha256"]):
            raise CSVIngestError(
                "CSV bytes changed after preview; create a new preview before committing"
            )
        if datetime.fromisoformat(str(row["expires_at"])) <= datetime.now(UTC):
            raise CSVIngestError("CSV preview has expired; create a new preview")
        if row["committed_at"]:
            return {
                "previewId": preview_id,
                "sourceItemId": str(row["committed_source_item_id"]),
                "status": "already_committed",
                "idempotentReplay": True,
            }
        with tempfile.NamedTemporaryFile(suffix=".csv") as value:
            value.write(content)
            value.flush()
            imported = self.engine.importer.ingest(
                value.name,
                workspace_id=workspace_id,
                label=f"Imported bank CSV · {_filename(filename)}",
                mapping_version=str(row["base_mapping_version"]),
                account_id_override=(
                    str(row["selected_account_id"])
                    if row["selected_account_id"] else None
                ),
            )
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """
                UPDATE csv_import_previews
                SET committed_at = ?, committed_source_item_id = ?
                WHERE preview_id = ? AND committed_at IS NULL
                """,
                (now, imported.source_item_id, preview_id),
            )
        return {
            "previewId": preview_id,
            "sourceItemId": imported.source_item_id,
            "sourceSha256": imported.source_sha256,
            "mappingVersion": imported.mapping_version,
            "rowCount": imported.row_count,
            "status": "deduplicated" if imported.duplicate_import else "committed",
            "idempotentReplay": imported.duplicate_import,
            "committedAt": now,
        }
'''

SERVICE_METHODS = '''    async def preview_csv_import(
        self,
        *,
        workspace_id: str,
        filename: str,
        content: bytes,
        account_id: str | None,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return CSVImportPreviewService(self.store, self.engine).preview(
                workspace_id=workspace_id,
                filename=filename,
                content=content,
                account_id=account_id,
            )

    async def commit_csv_import_preview(
        self,
        *,
        workspace_id: str,
        preview_id: str,
        filename: str,
        content: bytes,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            result = CSVImportPreviewService(self.store, self.engine).commit(
                workspace_id=workspace_id,
                preview_id=preview_id,
                filename=filename,
                content=content,
            )
            self.working_understanding.ensure_current(workspace_id=workspace_id)
        return result
'''

ROUTES = '''    @router.post("/v1/ingest/csv/preview")
    async def preview_csv_import(
        services: Services,
        workspace_id: Annotated[str, Form(alias="workspaceId")],
        file: Annotated[UploadFile, File()],
        account_id: Annotated[str | None, Form(alias="accountId")] = None,
    ) -> dict[str, object]:
        try:
            content = await read_upload_with_limit(file)
            return dict(
                await services.preview_csv_import(
                    workspace_id=workspace_id,
                    filename=file.filename or "source.csv",
                    content=content,
                    account_id=account_id,
                )
            )
        except UploadTooLarge as exc:
            raise HTTPException(status_code=413, detail="CSV exceeds the 10 MB limit") from exc
        except CSVIngestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/ingest/csv/commit")
    async def commit_csv_import_preview(
        services: Services,
        workspace_id: Annotated[str, Form(alias="workspaceId")],
        preview_id: Annotated[str, Form(alias="previewId")],
        file: Annotated[UploadFile, File()],
    ) -> dict[str, object]:
        try:
            content = await read_upload_with_limit(file)
            return dict(
                await services.commit_csv_import_preview(
                    workspace_id=workspace_id,
                    preview_id=preview_id,
                    filename=file.filename or "source.csv",
                    content=content,
                )
            )
        except UploadTooLarge as exc:
            raise HTTPException(status_code=413, detail="CSV exceeds the 10 MB limit") from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="CSV preview not found") from exc
        except CSVIngestError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from finance_agent.finance import FinanceEngine
from finance_agent.finance.import_preview import CSVImportPreviewService
from finance_agent.finance.ingest import CSVIngestError
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
SEED = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def practical() -> bytes:
    return (
        "Date,Description,Amount,Reference,Currency,Status\n"
        "2026-08-20,New client payment,1200.00,new-income-1,NZD,posted\n"
        "2026-08-21,Office supplies,-85.50,new-expense-1,NZD,posted\n"
        "2026-08-22,Pending software,-20.00,new-pending-1,NZD,pending\n"
    ).encode()


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(SEED)
    DailyCloseService(engine).run()
    return store, engine, CSVImportPreviewService(store, engine)


def counts(store: SQLiteStore) -> tuple[int, int, int]:
    return (
        len(store.fetch_all("SELECT * FROM source_items")),
        len(store.fetch_all("SELECT * FROM source_rows")),
        len(store.fetch_all("SELECT * FROM transactions")),
    )


def test_preview_calculates_mapping_and_totals_without_committing_rows(tmp_path: Path) -> None:
    store, _engine, service = setup(tmp_path)
    before = counts(store)
    preview = service.preview(
        workspace_id="ws_koru_studio",
        filename="statement.csv",
        content=practical(),
    )
    assert preview["committed"] is False
    assert preview["rowCount"] == 3
    assert preview["totals"] == {
        "currency": "NZD",
        "inflowMinor": 120000,
        "outflowMinor": 10550,
        "netMinor": 109450,
    }
    assert preview["pendingRowCount"] == 1
    assert preview["resolvedMappingVersion"].endswith("+nz_bank_signed_amount@1")
    assert preview["sampleRows"][0]["description"] == "New client payment"
    assert counts(store) == before
    assert len(store.fetch_all("SELECT * FROM csv_import_previews")) == 1


def test_commit_requires_same_bytes_and_is_idempotent(tmp_path: Path) -> None:
    store, _engine, service = setup(tmp_path)
    preview = service.preview(
        workspace_id="ws_koru_studio",
        filename="statement.csv",
        content=practical(),
    )
    with pytest.raises(CSVIngestError, match="changed after preview"):
        service.commit(
            workspace_id="ws_koru_studio",
            preview_id=str(preview["previewId"]),
            filename="statement.csv",
            content=practical() + b"\n",
        )
    before = counts(store)
    committed = service.commit(
        workspace_id="ws_koru_studio",
        preview_id=str(preview["previewId"]),
        filename="statement.csv",
        content=practical(),
    )
    assert committed["status"] == "committed"
    after = counts(store)
    assert after[0] == before[0] + 1
    assert after[1] == before[1] + 3
    assert after[2] == before[2] + 3
    replay = service.commit(
        workspace_id="ws_koru_studio",
        preview_id=str(preview["previewId"]),
        filename="statement.csv",
        content=practical(),
    )
    assert replay["idempotentReplay"] is True
    assert counts(store) == after


def test_expired_preview_fails_without_partial_commit(tmp_path: Path) -> None:
    store, _engine, service = setup(tmp_path)
    preview = service.preview(
        workspace_id="ws_koru_studio",
        filename="statement.csv",
        content=practical(),
    )
    with store.transaction() as connection:
        connection.execute(
            "UPDATE csv_import_previews SET expires_at = ? WHERE preview_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), preview["previewId"]),
        )
    before = counts(store)
    with pytest.raises(CSVIngestError, match="expired"):
        service.commit(
            workspace_id="ws_koru_studio",
            preview_id=str(preview["previewId"]),
            filename="statement.csv",
            content=practical(),
        )
    assert counts(store) == before


def test_preview_reports_hard_locked_rows_before_commit(tmp_path: Path) -> None:
    store, _engine, service = setup(tmp_path)
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO accounting_period_revisions(
                period_id, revision, workspace_id, period_start, period_end,
                status, actor, reason, created_at
            ) VALUES (
                'period_august_test', 1, 'ws_koru_studio', '2026-08-01',
                '2026-08-31', 'hard_locked', 'owner', 'Test lock',
                '2026-08-26T00:00:00+00:00'
            )
            """
        )
    preview = service.preview(
        workspace_id="ws_koru_studio",
        filename="statement.csv",
        content=practical(),
    )
    assert preview["hardLockedRowCount"] == 3
    assert any("hard-locked" in warning for warning in preview["warnings"])
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
    write("services/api/src/finance_agent/finance/import_preview.py", MODULE)


def update_importer() -> None:
    path = "services/api/src/finance_agent/finance/ingest.py"
    content = read(path)
    signature_marker = '''        mapping_version: str = "bank_csv@1",
        received_at: str | None = None,
    ) -> ImportResult:
'''
    signature_replacement = '''        mapping_version: str = "bank_csv@1",
        received_at: str | None = None,
        account_id_override: str | None = None,
    ) -> ImportResult:
'''
    if "account_id_override: str | None = None" not in content:
        if signature_marker not in content:
            raise RuntimeError("CSVImporter.ingest signature marker missing")
        content = content.replace(signature_marker, signature_replacement, 1)
    block = '''            account_rows = self.store.fetch_all(
                "SELECT account_id FROM accounts WHERE workspace_id = ? ORDER BY account_id",
                (workspace_id,),
            )
            if not account_rows:
                raise CSVIngestError("workspace has no account for this bank statement")
            if len(account_rows) > 1:
                raise CSVIngestError(
                    "workspace has multiple accounts; select an account before importing this bank statement"
                )
            parsed = _mapped_rows(
                raw_rows,
                mapping=_detect_header_mapping(fieldnames),
                account_id=cast(str, account_rows[0]["account_id"]),
                source_digest=digest,
                mapping_version=mapping_version,
            )
'''
    replacement = '''            if account_id_override is not None:
                selected_account = self.store.fetch_one(
                    "SELECT account_id FROM accounts WHERE workspace_id = ? AND account_id = ?",
                    (workspace_id, account_id_override),
                )
                if selected_account is None:
                    raise CSVIngestError(
                        "selected account does not belong to this workspace"
                    )
                account_id = cast(str, selected_account["account_id"])
            else:
                account_rows = self.store.fetch_all(
                    "SELECT account_id FROM accounts WHERE workspace_id = ? ORDER BY account_id",
                    (workspace_id,),
                )
                if not account_rows:
                    raise CSVIngestError("workspace has no account for this bank statement")
                if len(account_rows) > 1:
                    raise CSVIngestError(
                        "workspace has multiple accounts; select an account before importing this bank statement"
                    )
                account_id = cast(str, account_rows[0]["account_id"])
            parsed = _mapped_rows(
                raw_rows,
                mapping=_detect_header_mapping(fieldnames),
                account_id=account_id,
                source_digest=digest,
                mapping_version=mapping_version,
            )
'''
    if "selected_account = self.store.fetch_one(" not in content:
        if block not in content:
            raise RuntimeError("CSV practical-account selection block missing")
        content = content.replace(block, replacement, 1)
    write(path, content)


def update_service_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.rule_management import ClassificationRuleManagementService\n"
    import_line = "from finance_agent.finance.import_preview import CSVImportPreviewService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("rule management import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "preview_classification_rule", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def preview_classification_rule(\n"
    addition = '''    async def preview_csv_import(\n        self, *, workspace_id: str, filename: str, content: bytes,\n        account_id: str | None\n    ) -> Mapping[str, object]: ...\n\n    async def commit_csv_import_preview(\n        self, *, workspace_id: str, preview_id: str, filename: str, content: bytes\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("rule management protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    import_marker = "from finance_agent.connectors.base import ConnectorError\n"
    import_line = "from finance_agent.finance.ingest import CSVIngestError\n"
    if import_line not in content:
        if import_marker not in content:
            raise RuntimeError("ConnectorError import marker missing")
        content = content.replace(import_marker, import_marker + import_line, 1)
    marker = '    @router.post("/v1/ingest/csv")\n'
    if marker not in content:
        raise RuntimeError("legacy CSV route marker missing")
    content = content.replace(marker, ROUTES + marker, 1)
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/finance/test_csv_import_preview.py", TESTS)
    write("docs/CSV_IMPORT_PREVIEW.md", '''# Bank CSV preview and guarded commitment\n\nFolio can inspect a local bank CSV before creating any source, row or transaction. Preview resolves the header profile and account, validates every row, calculates exact NZD inflows/outflows/net, date range, pending count, generated-reference count, duplicate status and accounting-period lock impact. Only ten bounded sample rows are returned.\n\nPreview stores the SHA-256 digest, mapping, selected account, summary and a 30-minute expiry. It does not retain the uploaded bytes or commit finance data. Commitment requires the same bytes and preview ID. Changed or expired bytes fail before ingestion. Successful commitment uses the selected account and existing atomic importer, then records the resulting source item on the preview receipt. Repeated commitment is idempotent.\n\nA preview explains what Folio would ingest. It does not assert that the bank export is complete, that its sign convention is correct or that hard-locked accounting periods should be reopened.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 38: non-mutating CSV import preview\n\n- Header mapping, account selection and every row validate before commitment.\n- Exact totals, date range, pending rows, duplicate state and lock impact are visible.\n- Preview creates no source rows or transactions and stores no upload bytes.\n- Commit requires the same SHA-256 bytes and an unexpired preview receipt.\n- Account selection is explicit when a workspace has multiple accounts.\n- Preview does not prove statement completeness or justify reopening a locked period.\n'''
    if "## Stack 38: non-mutating CSV import preview" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_importer()
    update_service_protocol_routes()
    tests_docs()
    print("CSV import preview changes applied")


if __name__ == "__main__":
    main()
