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
        name="accounting_system_csv_exports",
        sql="""
        CREATE TABLE accounting_export_profile_revisions (
            profile_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            profile_name TEXT NOT NULL CHECK (length(trim(profile_name)) BETWEEN 1 AND 200),
            export_format TEXT NOT NULL CHECK (export_format IN ('xero', 'myob')),
            bank_control_account_code TEXT NOT NULL,
            category_mapping_json TEXT NOT NULL,
            default_tax_code TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (profile_id, revision)
        );

        CREATE TABLE accounting_export_artifacts (
            artifact_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            profile_id TEXT NOT NULL,
            profile_revision INTEGER NOT NULL,
            export_format TEXT NOT NULL CHECK (export_format IN ('xero', 'myob')),
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            filename TEXT NOT NULL,
            content BLOB NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            manifest_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (profile_id, profile_revision)
                REFERENCES accounting_export_profile_revisions(profile_id, revision),
            CHECK (period_start <= period_end)
        );

        CREATE INDEX accounting_export_profiles_active
            ON accounting_export_profile_revisions(workspace_id, status, created_at DESC);
        CREATE INDEX accounting_export_artifacts_period
            ON accounting_export_artifacts(workspace_id, period_end DESC, created_at DESC);

        CREATE TRIGGER accounting_export_profile_content_no_update
        BEFORE UPDATE OF profile_id, revision, workspace_id, profile_name,
            export_format, bank_control_account_code, category_mapping_json,
            default_tax_code, created_at
        ON accounting_export_profile_revisions
        BEGIN
            SELECT RAISE(ABORT, 'accounting export profile content is append-only');
        END;

        CREATE TRIGGER accounting_export_profile_status_transition_only
        BEFORE UPDATE OF status ON accounting_export_profile_revisions
        WHEN NOT (OLD.status = 'active' AND NEW.status = 'superseded')
        BEGIN
            SELECT RAISE(ABORT, 'invalid accounting export profile status transition');
        END;

        CREATE TRIGGER accounting_export_artifacts_no_update
        BEFORE UPDATE ON accounting_export_artifacts
        BEGIN
            SELECT RAISE(ABORT, 'accounting export artifacts are immutable');
        END;

        CREATE TRIGGER accounting_export_artifacts_no_delete
        BEFORE DELETE ON accounting_export_artifacts
        BEGIN
            SELECT RAISE(ABORT, 'accounting export artifacts are immutable');
        END;
        """,
    ),
'''

MODULE = '''"""Balanced, evidence-linked Xero and MYOB preparatory journal CSV exports."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Mapping

from finance_agent.storage import SQLiteStore, canonical_json

ACCOUNT_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,29}$")


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:24]}"


def _money(amount_minor: int) -> str:
    return f"{amount_minor // 100}.{amount_minor % 100:02d}"


def _mapping_key(classification: str, category: str | None) -> str:
    return f"{classification}:{category or 'uncategorised'}"


@dataclass(frozen=True, slots=True)
class ExportProfile:
    profile_id: str
    revision: int
    workspace_id: str
    profile_name: str
    export_format: str
    bank_control_account_code: str
    category_mapping: dict[str, str]
    default_tax_code: str
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "profileId": self.profile_id,
            "revision": self.revision,
            "workspaceId": self.workspace_id,
            "profileName": self.profile_name,
            "exportFormat": self.export_format,
            "bankControlAccountCode": self.bank_control_account_code,
            "categoryMapping": dict(self.category_mapping),
            "defaultTaxCode": self.default_tax_code,
            "status": "active",
            "createdAt": self.created_at,
        }


class AccountingSystemExportService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _profile(row: Any) -> ExportProfile:
        return ExportProfile(
            profile_id=str(row["profile_id"]),
            revision=int(row["revision"]),
            workspace_id=str(row["workspace_id"]),
            profile_name=str(row["profile_name"]),
            export_format=str(row["export_format"]),
            bank_control_account_code=str(row["bank_control_account_code"]),
            category_mapping={
                str(key): str(value)
                for key, value in json.loads(str(row["category_mapping_json"])).items()
            },
            default_tax_code=str(row["default_tax_code"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _validate_code(value: str, label: str) -> str:
        code = value.strip()
        if not ACCOUNT_CODE.fullmatch(code):
            raise ValueError(f"{label} must be a 1-30 character account/tax code")
        return code

    def save_profile(
        self,
        *,
        workspace_id: str,
        profile_id: str | None,
        profile_name: str,
        export_format: str,
        bank_control_account_code: str,
        category_mapping: Mapping[str, str],
        default_tax_code: str,
    ) -> ExportProfile:
        name = profile_name.strip()
        if not name:
            raise ValueError("profileName must not be blank")
        if export_format not in {"xero", "myob"}:
            raise ValueError("exportFormat must be xero or myob")
        bank_code = self._validate_code(
            bank_control_account_code, "bankControlAccountCode"
        )
        tax_code = self._validate_code(default_tax_code, "defaultTaxCode")
        mapping: dict[str, str] = {}
        for raw_key, raw_code in category_mapping.items():
            key = str(raw_key).strip()
            if not re.fullmatch(
                r"^(business|personal|unresolved):[a-z0-9_]{1,100}$", key
            ):
                raise ValueError(
                    "categoryMapping keys must use classification:category"
                )
            mapping[key] = self._validate_code(str(raw_code), f"categoryMapping[{key}]")
        if not mapping:
            raise ValueError("categoryMapping must contain at least one mapping")
        identifier = profile_id or _stable_id(
            "exportprofile", workspace_id, export_format, name
        )
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS revision FROM accounting_export_profile_revisions WHERE profile_id = ?",
                (identifier,),
            ).fetchone()
            revision = int(row["revision"]) + 1
            if revision > 1:
                connection.execute(
                    "UPDATE accounting_export_profile_revisions SET status = 'superseded' WHERE profile_id = ? AND status = 'active'",
                    (identifier,),
                )
            connection.execute(
                """
                INSERT INTO accounting_export_profile_revisions(
                    profile_id, revision, workspace_id, profile_name,
                    export_format, bank_control_account_code,
                    category_mapping_json, default_tax_code, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    identifier,
                    revision,
                    workspace_id,
                    name[:200],
                    export_format,
                    bank_code,
                    canonical_json(mapping),
                    tax_code,
                    now,
                ),
            )
        return self.get_profile(workspace_id, identifier)

    def get_profile(self, workspace_id: str, profile_id: str) -> ExportProfile:
        row = self.store.fetch_one(
            """
            SELECT * FROM accounting_export_profile_revisions
            WHERE workspace_id = ? AND profile_id = ? AND status = 'active'
            ORDER BY revision DESC LIMIT 1
            """,
            (workspace_id, profile_id),
        )
        if row is None:
            raise KeyError(profile_id)
        return self._profile(row)

    def list_profiles(self, workspace_id: str) -> tuple[ExportProfile, ...]:
        rows = self.store.fetch_all(
            """
            SELECT * FROM accounting_export_profile_revisions
            WHERE workspace_id = ? AND status = 'active'
            ORDER BY profile_name, profile_id
            """,
            (workspace_id,),
        )
        return tuple(self._profile(row) for row in rows)

    def export(
        self,
        *,
        workspace_id: str,
        profile_id: str,
        period_start: str,
        period_end: str,
    ) -> dict[str, object]:
        try:
            start = date.fromisoformat(period_start)
            end = date.fromisoformat(period_end)
        except ValueError as exc:
            raise ValueError("export period dates must use YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("export period start must be on or before end")
        profile = self.get_profile(workspace_id, profile_id)
        transactions = self.store.fetch_all(
            """
            SELECT transaction_id, occurred_on, description, amount_minor,
                   currency, classification, category, evidence_id
            FROM transactions
            WHERE workspace_id = ? AND occurred_on BETWEEN ? AND ?
              AND status = 'posted' AND source_status = 'posted'
              AND classification != 'transfer'
            ORDER BY occurred_on, transaction_id
            """,
            (workspace_id, start.isoformat(), end.isoformat()),
        )
        missing = sorted(
            {
                _mapping_key(str(row["classification"]), row["category"])
                for row in transactions
                if _mapping_key(str(row["classification"]), row["category"])
                not in profile.category_mapping
            }
        )
        if missing:
            raise ValueError(
                "accounting export profile is missing mappings for: "
                + ", ".join(missing)
            )
        output = io.StringIO(newline="")
        if profile.export_format == "xero":
            fieldnames = (
                "Date", "AccountCode", "Description", "Reference",
                "Debit", "Credit", "TaxType",
            )
        else:
            fieldnames = (
                "Date", "JournalNumber", "AccountNumber", "Memo",
                "Debit", "Credit", "TaxCode",
            )
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        manifest_lines: list[dict[str, object]] = []
        total_debit = 0
        total_credit = 0
        line_number = 1
        for row in transactions:
            amount = int(row["amount_minor"])
            magnitude = abs(amount)
            category_key = _mapping_key(
                str(row["classification"]),
                str(row["category"]) if row["category"] else None,
            )
            category_code = profile.category_mapping[category_key]
            journal_number = "FOLIO-" + hashlib.sha256(
                str(row["transaction_id"]).encode()
            ).hexdigest()[:10].upper()
            if amount >= 0:
                postings = (
                    (profile.bank_control_account_code, magnitude, 0, "bank_control"),
                    (category_code, 0, magnitude, "category"),
                )
            else:
                postings = (
                    (category_code, magnitude, 0, "category"),
                    (profile.bank_control_account_code, 0, magnitude, "bank_control"),
                )
            for account_code, debit, credit, role in postings:
                reference = str(row["transaction_id"])
                description = str(row["description"])[:500]
                if profile.export_format == "xero":
                    csv_row = {
                        "Date": str(row["occurred_on"]),
                        "AccountCode": account_code,
                        "Description": description,
                        "Reference": reference,
                        "Debit": _money(debit) if debit else "",
                        "Credit": _money(credit) if credit else "",
                        "TaxType": profile.default_tax_code,
                    }
                else:
                    csv_row = {
                        "Date": str(row["occurred_on"]),
                        "JournalNumber": journal_number,
                        "AccountNumber": account_code,
                        "Memo": description,
                        "Debit": _money(debit) if debit else "",
                        "Credit": _money(credit) if credit else "",
                        "TaxCode": profile.default_tax_code,
                    }
                writer.writerow(csv_row)
                total_debit += debit
                total_credit += credit
                manifest_lines.append(
                    {
                        "lineNumber": line_number,
                        "transactionId": str(row["transaction_id"]),
                        "postingRole": role,
                        "accountCode": account_code,
                        "debitMinor": debit,
                        "creditMinor": credit,
                        "evidenceIds": [str(row["evidence_id"])],
                    }
                )
                line_number += 1
        if total_debit != total_credit:
            raise RuntimeError("accounting export is not balanced")
        content = output.getvalue().encode()
        content_hash = hashlib.sha256(content).hexdigest()
        manifest = {
            "manifestVersion": "folio.accounting-export-manifest@1",
            "workspaceId": workspace_id,
            "profileId": profile.profile_id,
            "profileRevision": profile.revision,
            "exportFormat": profile.export_format,
            "periodStart": start.isoformat(),
            "periodEnd": end.isoformat(),
            "currency": "NZD",
            "transactionCount": len(transactions),
            "lineCount": len(manifest_lines),
            "totalDebitMinor": total_debit,
            "totalCreditMinor": total_credit,
            "balanced": total_debit == total_credit,
            "taxCalculatedByFolio": False,
            "postedExternally": False,
            "lines": manifest_lines,
        }
        artifact_id = _stable_id(
            "accountingexport",
            workspace_id,
            profile.profile_id,
            str(profile.revision),
            start.isoformat(),
            end.isoformat(),
            content_hash,
        )
        filename = (
            f"folio-{profile.export_format}-{start.isoformat()}-to-{end.isoformat()}.csv"
        )
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO accounting_export_artifacts(
                    artifact_id, workspace_id, profile_id, profile_revision,
                    export_format, period_start, period_end, filename, content,
                    content_hash, manifest_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(artifact_id) DO NOTHING
                """,
                (
                    artifact_id,
                    workspace_id,
                    profile.profile_id,
                    profile.revision,
                    profile.export_format,
                    start.isoformat(),
                    end.isoformat(),
                    filename,
                    content,
                    content_hash,
                    canonical_json(manifest),
                    now,
                ),
            )
        return {
            "artifactId": artifact_id,
            "filename": filename,
            "contentHash": content_hash,
            "manifest": manifest,
            "createdAt": now,
            "postedExternally": False,
            "externalCallsMade": False,
        }

    def artifact(
        self, workspace_id: str, artifact_id: str
    ) -> tuple[str, bytes, str, str]:
        row = self.store.fetch_one(
            """
            SELECT filename, content, content_hash, manifest_json
            FROM accounting_export_artifacts
            WHERE workspace_id = ? AND artifact_id = ?
            """,
            (workspace_id, artifact_id),
        )
        if row is None:
            raise KeyError(artifact_id)
        return (
            str(row["filename"]),
            bytes(row["content"]),
            str(row["content_hash"]),
            str(row["manifest_json"]),
        )
'''

SERVICE_METHODS = '''    async def save_accounting_export_profile(
        self,
        *,
        workspace_id: str,
        profile_id: str | None,
        profile_name: str,
        export_format: str,
        bank_control_account_code: str,
        category_mapping: Mapping[str, str],
        default_tax_code: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return AccountingSystemExportService(self.store).save_profile(
            workspace_id=workspace_id,
            profile_id=profile_id,
            profile_name=profile_name,
            export_format=export_format,
            bank_control_account_code=bank_control_account_code,
            category_mapping=category_mapping,
            default_tax_code=default_tax_code,
        ).as_dict()

    async def list_accounting_export_profiles(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return tuple(
            value.as_dict()
            for value in AccountingSystemExportService(self.store).list_profiles(
                workspace_id
            )
        )

    async def create_accounting_system_export(
        self,
        *,
        workspace_id: str,
        profile_id: str,
        period_start: str,
        period_end: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return AccountingSystemExportService(self.store).export(
            workspace_id=workspace_id,
            profile_id=profile_id,
            period_start=period_start,
            period_end=period_end,
        )

    async def accounting_system_export_artifact(
        self, *, workspace_id: str, artifact_id: str
    ) -> ArtifactPayload:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        filename, content, content_hash, _manifest = AccountingSystemExportService(
            self.store
        ).artifact(workspace_id, artifact_id)
        return ArtifactPayload(
            content=content,
            media_type="text/csv; charset=utf-8",
            filename=filename,
            content_hash=content_hash,
        )
'''

ROUTE_MODELS = '''

class AccountingExportProfileRequest(RequestModel):
    profile_id: str | None = Field(
        default=None, alias="profileId", pattern=IDENTIFIER_PATTERN
    )
    profile_name: str = Field(alias="profileName", min_length=1, max_length=200)
    export_format: str = Field(alias="exportFormat", pattern=r"^(xero|myob)$")
    bank_control_account_code: str = Field(
        alias="bankControlAccountCode", min_length=1, max_length=30
    )
    category_mapping: dict[str, str] = Field(alias="categoryMapping", min_length=1)
    default_tax_code: str = Field(alias="defaultTaxCode", min_length=1, max_length=30)


class AccountingExportRequest(RequestModel):
    profile_id: str = Field(alias="profileId", pattern=IDENTIFIER_PATTERN)
    period_start: date = Field(alias="periodStart")
    period_end: date = Field(alias="periodEnd")
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/accounting-exports/profiles")
    async def save_accounting_export_profile(
        workspace_id: PathIdentifier,
        body: AccountingExportProfileRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.save_accounting_export_profile(
                    workspace_id=workspace_id,
                    profile_id=body.profile_id,
                    profile_name=body.profile_name,
                    export_format=body.export_format,
                    bank_control_account_code=body.bank_control_account_code,
                    category_mapping=body.category_mapping,
                    default_tax_code=body.default_tax_code,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/workspaces/{workspace_id}/accounting-exports/profiles")
    async def list_accounting_export_profiles(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        profiles = await services.list_accounting_export_profiles(
            workspace_id=workspace_id
        )
        return {"workspaceId": workspace_id, "profiles": list(profiles)}

    @router.post("/v1/workspaces/{workspace_id}/accounting-exports")
    async def create_accounting_system_export(
        workspace_id: PathIdentifier,
        body: AccountingExportRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.create_accounting_system_export(
                    workspace_id=workspace_id,
                    profile_id=body.profile_id,
                    period_start=body.period_start.isoformat(),
                    period_end=body.period_end.isoformat(),
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="accounting export profile not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get(
        "/v1/workspaces/{workspace_id}/accounting-exports/{artifact_id}"
    )
    async def accounting_system_export_artifact(
        workspace_id: PathIdentifier,
        artifact_id: PathIdentifier,
        services: Services,
    ) -> Response:
        try:
            value = await services.accounting_system_export_artifact(
                workspace_id=workspace_id,
                artifact_id=artifact_id,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="accounting export not found") from exc
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

import csv
import io
from pathlib import Path

import pytest

from finance_agent.finance import FinanceEngine
from finance_agent.finance.accounting_exports import AccountingSystemExportService
from finance_agent.jobs import DailyCloseService
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def mapping() -> dict[str, str]:
    return {
        "business:client_income": "200",
        "business:studio_rent": "400",
        "business:software_subscriptions": "410",
        "personal:owner_draw": "900",
        "personal:personal_meals": "901",
        "unresolved:uncategorised": "999",
    }


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    DailyCloseService(engine).run()
    return store, AccountingSystemExportService(store)


def parsed(service: AccountingSystemExportService, artifact_id: str):
    _filename, content, _hash, manifest = service.artifact(
        "ws_koru_studio", artifact_id
    )
    return list(csv.DictReader(io.StringIO(content.decode()))), manifest


def test_xero_export_is_two_sided_balanced_and_evidence_linked(tmp_path: Path) -> None:
    _store, service = setup(tmp_path)
    profile = service.save_profile(
        workspace_id="ws_koru_studio",
        profile_id=None,
        profile_name="Xero draft journals",
        export_format="xero",
        bank_control_account_code="100",
        category_mapping=mapping(),
        default_tax_code="NONE",
    )
    value = service.export(
        workspace_id="ws_koru_studio",
        profile_id=profile.profile_id,
        period_start="2026-07-01",
        period_end="2026-07-31",
    )
    rows, manifest_json = parsed(service, str(value["artifactId"]))
    manifest = value["manifest"]
    assert len(rows) == manifest["transactionCount"] * 2
    assert manifest["balanced"] is True
    assert manifest["totalDebitMinor"] == manifest["totalCreditMinor"]
    assert manifest["taxCalculatedByFolio"] is False
    assert manifest["postedExternally"] is False
    assert all(line["evidenceIds"] for line in manifest["lines"])
    assert rows[0].keys() == {
        "Date", "AccountCode", "Description", "Reference", "Debit", "Credit", "TaxType"
    }
    assert '"postedExternally":false' in manifest_json


def test_myob_export_uses_profile_codes_and_is_not_posted(tmp_path: Path) -> None:
    _store, service = setup(tmp_path)
    profile = service.save_profile(
        workspace_id="ws_koru_studio",
        profile_id=None,
        profile_name="MYOB draft journals",
        export_format="myob",
        bank_control_account_code="1-1000",
        category_mapping=mapping(),
        default_tax_code="N-T",
    )
    value = service.export(
        workspace_id="ws_koru_studio",
        profile_id=profile.profile_id,
        period_start="2026-07-01",
        period_end="2026-07-31",
    )
    rows, _manifest = parsed(service, str(value["artifactId"]))
    assert rows[0].keys() == {
        "Date", "JournalNumber", "AccountNumber", "Memo", "Debit", "Credit", "TaxCode"
    }
    assert all(row["TaxCode"] == "N-T" for row in rows)
    assert value["postedExternally"] is False
    assert value["externalCallsMade"] is False


def test_missing_mapping_fails_before_artifact_creation(tmp_path: Path) -> None:
    store, service = setup(tmp_path)
    profile = service.save_profile(
        workspace_id="ws_koru_studio",
        profile_id=None,
        profile_name="Incomplete",
        export_format="xero",
        bank_control_account_code="100",
        category_mapping={"business:client_income": "200"},
        default_tax_code="NONE",
    )
    before = len(store.fetch_all("SELECT * FROM accounting_export_artifacts"))
    with pytest.raises(ValueError, match="missing mappings"):
        service.export(
            workspace_id="ws_koru_studio",
            profile_id=profile.profile_id,
            period_start="2026-07-01",
            period_end="2026-07-31",
        )
    assert len(store.fetch_all("SELECT * FROM accounting_export_artifacts")) == before


def test_profile_revision_supersedes_without_editing_history(tmp_path: Path) -> None:
    store, service = setup(tmp_path)
    first = service.save_profile(
        workspace_id="ws_koru_studio",
        profile_id=None,
        profile_name="Versioned Xero",
        export_format="xero",
        bank_control_account_code="100",
        category_mapping=mapping(),
        default_tax_code="NONE",
    )
    second = service.save_profile(
        workspace_id="ws_koru_studio",
        profile_id=first.profile_id,
        profile_name="Versioned Xero",
        export_format="xero",
        bank_control_account_code="101",
        category_mapping=mapping(),
        default_tax_code="NONE",
    )
    assert second.revision == 2
    rows = store.fetch_all(
        "SELECT revision, bank_control_account_code, status FROM accounting_export_profile_revisions WHERE profile_id = ? ORDER BY revision",
        (first.profile_id,),
    )
    assert [(int(row["revision"]), str(row["bank_control_account_code"]), str(row["status"])) for row in rows] == [
        (1, "100", "superseded"), (2, "101", "active")
    ]
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
    write("services/api/src/finance_agent/finance/accounting_exports.py", MODULE)


def update_service_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.duplicates import DuplicateReviewService\n"
    import_line = "from finance_agent.finance.accounting_exports import AccountingSystemExportService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("duplicate service import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "scan_duplicate_candidates", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def scan_duplicate_candidates(\n"
    addition = '''    async def save_accounting_export_profile(\n        self, *, workspace_id: str, profile_id: str | None, profile_name: str,\n        export_format: str, bank_control_account_code: str,\n        category_mapping: Mapping[str, str], default_tax_code: str\n    ) -> Mapping[str, object]: ...\n\n    async def list_accounting_export_profiles(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def create_accounting_system_export(\n        self, *, workspace_id: str, profile_id: str,\n        period_start: str, period_end: str\n    ) -> Mapping[str, object]: ...\n\n    async def accounting_system_export_artifact(\n        self, *, workspace_id: str, artifact_id: str\n    ) -> ArtifactPayload: ...\n\n'''
    if marker not in content:
        raise RuntimeError("duplicate protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass DuplicateConfirmRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("DuplicateConfirmRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.post("/v1/workspaces/{workspace_id}/duplicates/scan")\n'
    if route_marker not in content:
        raise RuntimeError("duplicate route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def update_audit_state_identity() -> None:
    path = "services/api/src/finance_agent/audit_trail.py"
    content = read(path)
    kind_marker = '        "duplicate_review",\n'
    if '"accounting_export"' not in content:
        if kind_marker not in content:
            raise RuntimeError("duplicate audit kind marker missing")
        content = content.replace(kind_marker, kind_marker + '        "accounting_export",\n', 1)
    optional_marker = '        if self._table_exists("duplicate_review_events"):\n'
    block = '''        if self._table_exists("accounting_export_artifacts"):
            for row in self.store.fetch_all(
                "SELECT * FROM accounting_export_artifacts WHERE workspace_id = ? ORDER BY created_at, artifact_id",
                (workspace_id,),
            ):
                manifest = json.loads(str(row["manifest_json"]))
                yield AuditEvent(
                    event_id=str(row["artifact_id"]),
                    workspace_id=workspace_id,
                    kind="accounting_export",
                    action=f"{row['export_format']}_journal_csv_prepared",
                    status="prepared_not_posted",
                    occurred_at=str(row["created_at"]),
                    actor="owner",
                    correlation_id=None,
                    subject_type="accounting_export_artifact",
                    subject_id=str(row["artifact_id"]),
                    evidence_ids=tuple(
                        dict.fromkeys(
                            evidence
                            for line in manifest.get("lines", [])
                            for evidence in line.get("evidenceIds", [])
                        )
                    ),
                    metadata={
                        "profileId": str(row["profile_id"]),
                        "profileRevision": int(row["profile_revision"]),
                        "contentHash": str(row["content_hash"]),
                        "periodStart": str(row["period_start"]),
                        "periodEnd": str(row["period_end"]),
                        "balanced": bool(manifest.get("balanced")),
                        "postedExternally": False,
                    },
                )
'''
    if "prepared_not_posted" not in content:
        if optional_marker not in content:
            raise RuntimeError("duplicate optional audit marker missing")
        content = content.replace(optional_marker, block + optional_marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/storage/state_identity.py"
    content = read(path)
    marker = '''    duplicate_events = _rows(
        store,
        """
        SELECT event_id, candidate_id, event_type, keeper_transaction_id,
               duplicate_transaction_id, before_json, after_json,
               evidence_ids_json, occurred_at, reverses_event_id
        FROM duplicate_review_events
        WHERE workspace_id = ? ORDER BY occurred_at, event_id
        """,
        (workspace_id,),
    )
'''
    addition = marker + '''    accounting_export_profiles = _rows(
        store,
        """
        SELECT profile_id, revision, profile_name, export_format,
               bank_control_account_code, category_mapping_json,
               default_tax_code, status, created_at
        FROM accounting_export_profile_revisions
        WHERE workspace_id = ? ORDER BY profile_id, revision
        """,
        (workspace_id,),
    )
'''
    if "accounting_export_profiles = _rows(" not in content:
        if marker not in content:
            raise RuntimeError("duplicate identity marker missing")
        content = content.replace(marker, addition, 1)
    payload_marker = '        "duplicateEvents": duplicate_events,\n'
    if '"accountingExportProfiles": accounting_export_profiles' not in content:
        if payload_marker not in content:
            raise RuntimeError("duplicate identity payload missing")
        content = content.replace(
            payload_marker,
            payload_marker
            + '        "accountingExportProfiles": accounting_export_profiles,\n',
            1,
        )
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/finance/test_accounting_system_exports.py", TESTS)
    write("docs/ACCOUNTING_SYSTEM_EXPORTS.md", '''# Xero and MYOB preparatory journal exports\n\nAn export profile is an append-only mapping from Folio classification/category keys to external account codes, plus one bank control account and a default tax code. Xero and MYOB profiles are separate. Updating a profile appends a revision and preserves the prior mapping.\n\nFor each posted non-transfer transaction, Folio creates two journal lines: a category posting and an equal bank-control counterposting. Income debits the bank and credits the mapped account; outgoings debit the mapped account and credit the bank. Total debit must exactly equal total credit in integer NZD cents or no artifact is committed. Unmapped categories fail closed.\n\nEach immutable CSV has a SHA-256 hash and a JSON manifest mapping every line to the source transaction and evidence. Folio does not calculate tax in this export, call Xero/MYOB or claim the journal was imported, accepted or posted. Account and tax code correctness remains owner/accountant responsibility.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 44: balanced Xero and MYOB journal CSV exports\n\n- Versioned profiles map Folio category keys to external account codes.\n- Every transaction produces a category line and equal bank-control counterline.\n- Unmapped categories and unbalanced totals fail before artifact creation.\n- Transfers are excluded and tax is not silently calculated.\n- Immutable CSVs include hashes and a line-to-transaction/evidence manifest.\n- Preparation does not claim external import, acceptance or posting.\n'''
    if "## Stack 44: balanced Xero and MYOB journal CSV exports" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_service_protocol_routes()
    update_audit_state_identity()
    tests_docs()
    print("accounting system CSV export changes applied")


if __name__ == "__main__":
    main()
