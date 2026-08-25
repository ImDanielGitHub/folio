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
        name="accountant_export_working_papers",
        sql="""
        CREATE TABLE accounting_category_mappings (
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            category TEXT NOT NULL CHECK (length(trim(category)) > 0),
            account_code TEXT NOT NULL CHECK (length(trim(account_code)) BETWEEN 1 AND 40),
            account_name TEXT NOT NULL CHECK (length(trim(account_name)) BETWEEN 1 AND 200),
            account_type TEXT NOT NULL CHECK (account_type IN (
                'income', 'expense', 'asset', 'liability', 'equity'
            )),
            source TEXT NOT NULL CHECK (source IN ('owner', 'accountant', 'import')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, category)
        );

        CREATE TABLE accounting_export_revisions (
            export_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            profile TEXT NOT NULL CHECK (profile = 'neutral_cash_basis'),
            status TEXT NOT NULL CHECK (status IN ('draft', 'review_ready')),
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            created_at TEXT NOT NULL,
            PRIMARY KEY (export_id, revision)
        );

        CREATE INDEX accounting_export_workspace_period
            ON accounting_export_revisions(workspace_id, start_date, end_date, revision);
        """,
    ),
'''

ACCOUNTANT_EXPORT = '''"""Balanced cash-basis journal working papers with source traceability."""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json

BANK_CONTROL_CODE = "1000"
BANK_CONTROL_NAME = "Bank control"
PREPARATORY_NOTICE = (
    "Neutral cash-basis journal working paper only. Folio has not posted, imported, "
    "reconciled or filed these entries in an accounting system."
)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


@dataclass(frozen=True, slots=True)
class AccountantExport:
    export_id: str
    workspace_id: str
    start_date: str
    end_date: str
    status: str
    payload: dict[str, Any]
    content_hash: str


class AccountantExportService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def set_mapping(
        self,
        *,
        workspace_id: str,
        category: str,
        account_code: str,
        account_name: str,
        account_type: str,
        source: str,
    ) -> dict[str, object]:
        category_value = category.strip()
        code_value = account_code.strip()
        name_value = account_name.strip()
        if not category_value or not code_value or not name_value:
            raise ValueError("category, accountCode and accountName are required")
        if account_type not in {"income", "expense", "asset", "liability", "equity"}:
            raise ValueError("unsupported account type")
        if source not in {"owner", "accountant", "import"}:
            raise ValueError("unsupported mapping source")
        updated_at = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO accounting_category_mappings(
                    workspace_id, category, account_code, account_name,
                    account_type, source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, category) DO UPDATE SET
                    account_code = excluded.account_code,
                    account_name = excluded.account_name,
                    account_type = excluded.account_type,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_id,
                    category_value,
                    code_value[:40],
                    name_value[:200],
                    account_type,
                    source,
                    updated_at,
                ),
            )
        return {
            "workspaceId": workspace_id,
            "category": category_value,
            "accountCode": code_value[:40],
            "accountName": name_value[:200],
            "accountType": account_type,
            "source": source,
            "updatedAt": updated_at,
        }

    def preview(
        self,
        *,
        workspace_id: str,
        start_date: str,
        end_date: str,
    ) -> AccountantExport:
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError as exc:
            raise ValueError("export dates must use YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("export start must be on or before end")
        rows = self.store.fetch_all(
            """
            SELECT t.transaction_id, t.occurred_on, t.description, t.amount_minor,
                   t.currency, t.classification, t.category, t.evidence_id,
                   a.account_code, a.account_name, a.account_type,
                   a.source AS mapping_source,
                   g.treatment AS gst_treatment
            FROM transactions t
            LEFT JOIN accounting_category_mappings a
              ON a.workspace_id = t.workspace_id AND a.category = t.category
            LEFT JOIN accounting_tax_mappings g
              ON g.workspace_id = t.workspace_id AND g.category = t.category
            WHERE t.workspace_id = ?
              AND t.status = 'posted' AND t.source_status = 'posted'
              AND t.occurred_on BETWEEN ? AND ?
            ORDER BY t.occurred_on, t.transaction_id
            """,
            (workspace_id, start.isoformat(), end.isoformat()),
        )
        entries: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        total_debit = 0
        total_credit = 0
        for row in rows:
            transaction_id = str(row["transaction_id"])
            classification = str(row["classification"])
            amount_minor = int(row["amount_minor"])
            if classification == "transfer":
                unresolved.append(
                    {
                        "transactionId": transaction_id,
                        "reason": "internal_transfer_requires_account_pair_review",
                        "evidenceIds": [str(row["evidence_id"])],
                    }
                )
                continue
            if row["account_code"] is None or row["account_name"] is None:
                unresolved.append(
                    {
                        "transactionId": transaction_id,
                        "reason": "missing_chart_of_accounts_mapping",
                        "category": row["category"],
                        "evidenceIds": [str(row["evidence_id"])],
                    }
                )
                continue
            if classification not in {"business", "personal"}:
                unresolved.append(
                    {
                        "transactionId": transaction_id,
                        "reason": "classification_not_ready_for_export",
                        "classification": classification,
                        "evidenceIds": [str(row["evidence_id"])],
                    }
                )
                continue
            magnitude = abs(amount_minor)
            journal_id = _stable_id("journal", workspace_id, transaction_id)
            common = {
                "journalId": journal_id,
                "transactionId": transaction_id,
                "occurredOn": str(row["occurred_on"]),
                "description": str(row["description"]),
                "currency": str(row["currency"]),
                "category": str(row["category"]) if row["category"] else None,
                "classification": classification,
                "gstTreatment": str(row["gst_treatment"]) if row["gst_treatment"] else "unreviewed",
                "mappingSource": str(row["mapping_source"]),
                "evidenceIds": [str(row["evidence_id"])],
            }
            mapped = {
                **common,
                "accountCode": str(row["account_code"]),
                "accountName": str(row["account_name"]),
                "accountType": str(row["account_type"]),
            }
            bank = {
                **common,
                "accountCode": BANK_CONTROL_CODE,
                "accountName": BANK_CONTROL_NAME,
                "accountType": "asset",
            }
            if amount_minor >= 0:
                entries.append({**bank, "line": 1, "debitMinor": magnitude, "creditMinor": 0})
                entries.append({**mapped, "line": 2, "debitMinor": 0, "creditMinor": magnitude})
            else:
                entries.append({**mapped, "line": 1, "debitMinor": magnitude, "creditMinor": 0})
                entries.append({**bank, "line": 2, "debitMinor": 0, "creditMinor": magnitude})
            total_debit += magnitude
            total_credit += magnitude
        balanced = total_debit == total_credit
        payload = {
            "exportVersion": "accounting.neutral-cash-basis@1",
            "workspaceId": workspace_id,
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "profile": "neutral_cash_basis",
            "currency": "NZD",
            "notice": PREPARATORY_NOTICE,
            "entries": entries,
            "unresolved": unresolved,
            "totals": {
                "debitMinor": total_debit,
                "creditMinor": total_credit,
                "differenceMinor": total_debit - total_credit,
            },
            "balanced": balanced,
            "postedExternally": False,
        }
        encoded = canonical_json(payload)
        export_id = _stable_id("acctexport", workspace_id, start.isoformat(), end.isoformat())
        return AccountantExport(
            export_id=export_id,
            workspace_id=workspace_id,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            status="review_ready" if balanced and not unresolved and bool(entries) else "draft",
            payload=payload,
            content_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        )

    def commit(
        self,
        *,
        workspace_id: str,
        start_date: str,
        end_date: str,
    ) -> AccountantExport:
        value = self.preview(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
        )
        created_at = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS revision
                FROM accounting_export_revisions WHERE export_id = ?
                """,
                (value.export_id,),
            ).fetchone()
            revision = int(row["revision"]) + 1
            connection.execute(
                """
                INSERT INTO accounting_export_revisions(
                    export_id, revision, workspace_id, start_date, end_date,
                    profile, status, payload_json, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, 'neutral_cash_basis', ?, ?, ?, ?)
                """,
                (
                    value.export_id,
                    revision,
                    workspace_id,
                    value.start_date,
                    value.end_date,
                    value.status,
                    canonical_json(value.payload),
                    value.content_hash,
                    created_at,
                ),
            )
        return value

    @staticmethod
    def csv_bytes(value: AccountantExport) -> bytes:
        output = io.StringIO(newline="")
        fields = (
            "journal_id", "line", "date", "account_code", "account_name",
            "account_type", "description", "debit_minor", "credit_minor",
            "currency", "classification", "category", "gst_treatment",
            "transaction_id", "evidence_ids", "mapping_source",
        )
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for entry in value.payload["entries"]:
            writer.writerow(
                {
                    "journal_id": entry["journalId"],
                    "line": entry["line"],
                    "date": entry["occurredOn"],
                    "account_code": entry["accountCode"],
                    "account_name": entry["accountName"],
                    "account_type": entry["accountType"],
                    "description": entry["description"],
                    "debit_minor": entry["debitMinor"],
                    "credit_minor": entry["creditMinor"],
                    "currency": entry["currency"],
                    "classification": entry["classification"],
                    "category": entry["category"] or "",
                    "gst_treatment": entry["gstTreatment"],
                    "transaction_id": entry["transactionId"],
                    "evidence_ids": "|".join(entry["evidenceIds"]),
                    "mapping_source": entry["mappingSource"],
                }
            )
        return output.getvalue().encode("utf-8")
'''

SERVICE_METHODS = '''    async def set_accounting_mapping(
        self,
        *,
        workspace_id: str,
        category: str,
        account_code: str,
        account_name: str,
        account_type: str,
        source: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return AccountantExportService(self.store).set_mapping(
                workspace_id=workspace_id,
                category=category,
                account_code=account_code,
                account_name=account_name,
                account_type=account_type,
                source=source,
            )

    async def accountant_export_preview(
        self, *, workspace_id: str, start_date: str, end_date: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        value = AccountantExportService(self.store).preview(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
        )
        return {
            "exportId": value.export_id,
            "status": value.status,
            "contentHash": value.content_hash,
            **value.payload,
        }

    async def commit_accountant_export(
        self, *, workspace_id: str, start_date: str, end_date: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = AccountantExportService(self.store).commit(
                workspace_id=workspace_id,
                start_date=start_date,
                end_date=end_date,
            )
        return {
            "exportId": value.export_id,
            "status": value.status,
            "contentHash": value.content_hash,
            "postedExternally": False,
            "notice": value.payload["notice"],
        }

    async def accountant_export_csv_payload(
        self, *, workspace_id: str, start_date: str, end_date: str
    ) -> ArtifactPayload:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        value = AccountantExportService(self.store).preview(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
        )
        content = AccountantExportService.csv_bytes(value)
        return ArtifactPayload(
            content=content,
            media_type="text/csv; charset=utf-8",
            filename=f"folio-accountant-working-paper-{start_date}-to-{end_date}.csv",
            content_hash=hashlib.sha256(content).hexdigest(),
        )
'''

ROUTE_MODELS = '''

class AccountingMappingRequest(RequestModel):
    category: str = Field(min_length=1, max_length=200)
    account_code: str = Field(alias="accountCode", min_length=1, max_length=40)
    account_name: str = Field(alias="accountName", min_length=1, max_length=200)
    account_type: str = Field(
        alias="accountType", pattern=r"^(income|expense|asset|liability|equity)$"
    )
    source: str = Field(default="owner", pattern=r"^(owner|accountant|import)$")


class AccountingPeriodRequest(RequestModel):
    start: date
    end: date
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/accounting/mappings")
    async def set_accounting_mapping(
        workspace_id: PathIdentifier,
        body: AccountingMappingRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.set_accounting_mapping(
                    workspace_id=workspace_id,
                    category=body.category,
                    account_code=body.account_code,
                    account_name=body.account_name,
                    account_type=body.account_type,
                    source=body.source,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/workspaces/{workspace_id}/accounting/export-preview")
    async def accountant_export_preview(
        workspace_id: PathIdentifier,
        body: AccountingPeriodRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.accountant_export_preview(
                    workspace_id=workspace_id,
                    start_date=body.start.isoformat(),
                    end_date=body.end.isoformat(),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/workspaces/{workspace_id}/accounting/exports", status_code=201)
    async def commit_accountant_export(
        workspace_id: PathIdentifier,
        body: AccountingPeriodRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.commit_accountant_export(
                    workspace_id=workspace_id,
                    start_date=body.start.isoformat(),
                    end_date=body.end.isoformat(),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/workspaces/{workspace_id}/accounting/export.csv")
    async def accountant_export_csv(
        workspace_id: PathIdentifier,
        services: Services,
        start: Annotated[date, Query()],
        end: Annotated[date, Query()],
    ) -> Response:
        try:
            value = await services.accountant_export_csv_payload(
                workspace_id=workspace_id,
                start_date=start.isoformat(),
                end_date=end.isoformat(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
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

from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.finance.accountant_export import AccountantExportService
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def service(tmp_path: Path) -> AccountantExportService:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    DailyCloseService(engine).run()
    return AccountantExportService(store)


def apply_demo_mappings(value: AccountantExportService) -> None:
    mappings = (
        ("client_income", "200", "Sales", "income"),
        ("studio_rent", "400", "Rent", "expense"),
        ("software_subscriptions", "410", "Software subscriptions", "expense"),
        ("owner_draw", "300", "Owner drawings", "equity"),
        ("personal_meals", "301", "Private expenses", "equity"),
    )
    for category, code, name, account_type in mappings:
        value.set_mapping(
            workspace_id="ws_koru_studio",
            category=category,
            account_code=code,
            account_name=name,
            account_type=account_type,
            source="accountant",
        )


def test_missing_mappings_keep_export_in_draft(tmp_path: Path) -> None:
    value = service(tmp_path)
    export = value.preview(
        workspace_id="ws_koru_studio",
        start_date="2026-07-01",
        end_date="2026-07-31",
    )
    assert export.status == "draft"
    assert export.payload["unresolved"]
    assert export.payload["postedExternally"] is False
    assert "not posted" in export.payload["notice"].lower()


def test_explicit_mappings_create_balanced_evidence_linked_journals(tmp_path: Path) -> None:
    value = service(tmp_path)
    apply_demo_mappings(value)
    export = value.preview(
        workspace_id="ws_koru_studio",
        start_date="2026-07-01",
        end_date="2026-07-31",
    )
    assert export.payload["balanced"] is True
    assert export.payload["totals"]["differenceMinor"] == 0
    assert export.payload["entries"]
    journal_ids = {entry["journalId"] for entry in export.payload["entries"]}
    assert all(
        sum(entry["debitMinor"] for entry in export.payload["entries"] if entry["journalId"] == journal_id)
        == sum(entry["creditMinor"] for entry in export.payload["entries"] if entry["journalId"] == journal_id)
        for journal_id in journal_ids
    )
    assert all(entry["evidenceIds"] for entry in export.payload["entries"])


def test_csv_is_neutral_traceable_and_not_vendor_posting_proof(tmp_path: Path) -> None:
    value = service(tmp_path)
    apply_demo_mappings(value)
    export = value.preview(
        workspace_id="ws_koru_studio",
        start_date="2026-07-01",
        end_date="2026-07-31",
    )
    content = value.csv_bytes(export)
    assert b"journal_id" in content
    assert b"account_code" in content
    assert b"transaction_id" in content
    assert b"evidence_ids" in content
    assert b"Xero" not in content
    assert b"MYOB" not in content


def test_committed_working_papers_are_append_only(tmp_path: Path) -> None:
    value = service(tmp_path)
    apply_demo_mappings(value)
    first = value.commit(
        workspace_id="ws_koru_studio",
        start_date="2026-07-01",
        end_date="2026-07-31",
    )
    second = value.commit(
        workspace_id="ws_koru_studio",
        start_date="2026-07-01",
        end_date="2026-07-31",
    )
    rows = value.store.fetch_all(
        "SELECT revision, payload_json FROM accounting_export_revisions WHERE export_id = ? ORDER BY revision",
        (first.export_id,),
    )
    assert [int(row["revision"]) for row in rows] == [1, 2]
    assert first.content_hash == second.content_hash
    assert all('"postedExternally":false' in str(row["payload_json"]) for row in rows)
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


def add_module_and_service() -> None:
    write("services/api/src/finance_agent/finance/accountant_export.py", ACCOUNTANT_EXPORT)
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.accounting import NZGSTService\n"
    import_line = "from finance_agent.finance.accountant_export import AccountantExportService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("GST service import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "list_cash_commitments", SERVICE_METHODS)


def update_protocol_and_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def list_cash_commitments(\n"
    addition = '''    async def set_accounting_mapping(\n        self, *, workspace_id: str, category: str, account_code: str,\n        account_name: str, account_type: str, source: str\n    ) -> Mapping[str, object]: ...\n\n    async def accountant_export_preview(\n        self, *, workspace_id: str, start_date: str, end_date: str\n    ) -> Mapping[str, object]: ...\n\n    async def commit_accountant_export(\n        self, *, workspace_id: str, start_date: str, end_date: str\n    ) -> Mapping[str, object]: ...\n\n    async def accountant_export_csv_payload(\n        self, *, workspace_id: str, start_date: str, end_date: str\n    ) -> ArtifactPayload: ...\n\n'''
    if marker not in content:
        raise RuntimeError("cash commitment protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass CashCommitmentRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("CashCommitmentRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.get("/v1/workspaces/{workspace_id}/cash-commitments")\n'
    if route_marker not in content:
        raise RuntimeError("cash commitments route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/finance/test_accountant_export.py", TESTS)
    write("docs/ACCOUNTANT_EXPORTS.md", '''# Accountant working-paper exports\n\nFolio creates a neutral, cash-basis journal working paper from posted local transactions. Every exported journal is double-entry balanced in integer NZD minor units. The bank control line is paired with an explicitly mapped income, expense, equity, asset or liability account. Missing mappings, unresolved classifications and internal transfers remain in a separate unresolved list rather than being guessed.\n\nMappings record who supplied them. Journal lines retain transaction IDs, evidence IDs, classification, category and GST treatment. Committed export revisions are append-only and always state `postedExternally: false`.\n\nThe CSV is deliberately vendor-neutral. It can support an accountant preparing Xero, MYOB or another ledger import, but Folio does not claim that the file matches a vendor's current import template, that it was imported, or that any entry was posted or reconciled.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 20: balanced accountant working-paper exports\n\n- Categories map explicitly to account code, name, type and mapping source.\n- Posted transactions produce balanced cash-basis journal pairs in integer minor units.\n- Missing mappings, unresolved classifications and transfers remain visible rather than guessed.\n- Journal lines retain transaction IDs, evidence IDs and GST treatment.\n- Export revisions are append-only and state that nothing was posted externally.\n- CSV output is vendor-neutral and is not described as a completed Xero or MYOB import.\n'''
    if "## Stack 20: balanced accountant working-paper exports" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration()
    add_module_and_service()
    update_protocol_and_routes()
    add_tests_and_docs()
    print("accountant export changes applied")


if __name__ == "__main__":
    main()
