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
        name="nz_gst_preparation",
        sql="""
        CREATE TABLE accounting_tax_mappings (
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            category TEXT NOT NULL CHECK (length(trim(category)) > 0),
            treatment TEXT NOT NULL CHECK (treatment IN (
                'standard', 'zero_rated', 'exempt', 'out_of_scope', 'unreviewed'
            )),
            rate_basis_points INTEGER NOT NULL DEFAULT 1500
                CHECK (rate_basis_points BETWEEN 0 AND 10000),
            source TEXT NOT NULL CHECK (source IN ('owner', 'accountant', 'import')),
            updated_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, category)
        );

        CREATE TABLE gst_report_revisions (
            report_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            accounting_basis TEXT NOT NULL CHECK (accounting_basis = 'payments'),
            status TEXT NOT NULL CHECK (status IN ('draft', 'review_ready')),
            payload_json TEXT NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            created_at TEXT NOT NULL,
            PRIMARY KEY (report_id, revision)
        );

        CREATE INDEX gst_report_workspace_period
            ON gst_report_revisions(workspace_id, start_date, end_date, revision);
        """,
    ),
'''

ACCOUNTING = '''"""Deterministic New Zealand GST preparation and neutral accountant exports."""

from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal

from finance_agent.storage import SQLiteStore, canonical_json

TaxTreatment = Literal[
    "standard", "zero_rated", "exempt", "out_of_scope", "unreviewed"
]
GST_RATE_BASIS_POINTS = 1500
PREPARATORY_NOTICE = (
    "Draft GST working paper only. Folio has not filed a return, determined legal "
    "deductibility, or replaced accountant review."
)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def gst_inclusive_component(amount_minor: int, *, rate_basis_points: int = 1500) -> int:
    if isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
        raise TypeError("GST calculation requires integer minor units")
    if not 0 <= rate_basis_points <= 10000:
        raise ValueError("GST rate basis points must be between 0 and 10000")
    if rate_basis_points == 0:
        return 0
    rate = Decimal(rate_basis_points) / Decimal(10000)
    magnitude = Decimal(abs(amount_minor))
    component = magnitude * rate / (Decimal(1) + rate)
    return int(component.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True, slots=True)
class GSTReport:
    report_id: str
    workspace_id: str
    start_date: str
    end_date: str
    status: str
    payload: dict[str, Any]
    content_hash: str


class NZGSTService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def set_mapping(
        self,
        *,
        workspace_id: str,
        category: str,
        treatment: TaxTreatment,
        source: str = "owner",
    ) -> dict[str, object]:
        canonical_category = category.strip()
        if not canonical_category:
            raise ValueError("category must not be blank")
        if treatment not in {
            "standard", "zero_rated", "exempt", "out_of_scope", "unreviewed"
        }:
            raise ValueError("unsupported GST treatment")
        if source not in {"owner", "accountant", "import"}:
            raise ValueError("unsupported mapping source")
        updated_at = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO accounting_tax_mappings(
                    workspace_id, category, treatment, rate_basis_points,
                    source, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id, category) DO UPDATE SET
                    treatment = excluded.treatment,
                    rate_basis_points = excluded.rate_basis_points,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_id,
                    canonical_category,
                    treatment,
                    GST_RATE_BASIS_POINTS,
                    source,
                    updated_at,
                ),
            )
        return {
            "workspaceId": workspace_id,
            "category": canonical_category,
            "treatment": treatment,
            "rateBasisPoints": GST_RATE_BASIS_POINTS,
            "source": source,
            "updatedAt": updated_at,
        }

    def _period_rows(
        self, workspace_id: str, start_date: str, end_date: str
    ) -> list[dict[str, Any]]:
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError as exc:
            raise ValueError("GST period dates must use YYYY-MM-DD") from exc
        if start > end:
            raise ValueError("GST period start must be on or before end")
        rows = self.store.fetch_all(
            """
            SELECT t.transaction_id, t.occurred_on, t.description, t.amount_minor,
                   t.currency, t.classification, t.category, t.evidence_id,
                   m.treatment, m.rate_basis_points, m.source AS mapping_source
            FROM transactions t
            LEFT JOIN accounting_tax_mappings m
              ON m.workspace_id = t.workspace_id AND m.category = t.category
            WHERE t.workspace_id = ? AND t.status = 'posted'
              AND t.source_status = 'posted'
              AND t.occurred_on BETWEEN ? AND ?
            ORDER BY t.occurred_on, t.transaction_id
            """,
            (workspace_id, start.isoformat(), end.isoformat()),
        )
        return [dict(row) for row in rows]

    def preview(
        self, *, workspace_id: str, start_date: str, end_date: str
    ) -> GSTReport:
        rows = self._period_rows(workspace_id, start_date, end_date)
        lines: list[dict[str, Any]] = []
        taxable_sales = 0
        taxable_purchases = 0
        output_gst = 0
        input_gst = 0
        unreviewed = 0
        for row in rows:
            category = str(row["category"]) if row["category"] is not None else None
            treatment: TaxTreatment = (
                str(row["treatment"]) if row["treatment"] else "unreviewed"
            )  # type: ignore[assignment]
            classification = str(row["classification"])
            amount_minor = int(row["amount_minor"])
            included = classification == "business" and treatment != "out_of_scope"
            gst_minor = 0
            if treatment == "standard" and included:
                gst_minor = gst_inclusive_component(
                    amount_minor,
                    rate_basis_points=int(row["rate_basis_points"] or GST_RATE_BASIS_POINTS),
                )
                if amount_minor >= 0:
                    taxable_sales += amount_minor
                    output_gst += gst_minor
                else:
                    taxable_purchases += abs(amount_minor)
                    input_gst += gst_minor
            elif treatment == "unreviewed" and classification == "business":
                unreviewed += 1
            lines.append(
                {
                    "transactionId": str(row["transaction_id"]),
                    "occurredOn": str(row["occurred_on"]),
                    "description": str(row["description"]),
                    "amountMinor": amount_minor,
                    "currency": str(row["currency"]),
                    "classification": classification,
                    "category": category,
                    "treatment": treatment,
                    "included": included,
                    "gstMinor": gst_minor,
                    "evidenceIds": [str(row["evidence_id"])],
                    "mappingSource": (
                        str(row["mapping_source"]) if row["mapping_source"] else None
                    ),
                }
            )
        payload = {
            "reportVersion": "nz.gst-working-paper@1",
            "workspaceId": workspace_id,
            "period": {"start": start_date, "end": end_date},
            "accountingBasis": "payments",
            "currency": "NZD",
            "notice": PREPARATORY_NOTICE,
            "totals": {
                "taxableSalesInclusiveMinor": taxable_sales,
                "taxablePurchasesInclusiveMinor": taxable_purchases,
                "outputGstMinor": output_gst,
                "inputGstMinor": input_gst,
                "netGstMinor": output_gst - input_gst,
                "unreviewedTransactionCount": unreviewed,
            },
            "lines": lines,
            "filed": False,
        }
        encoded = canonical_json(payload)
        report_id = _stable_id("gstrpt", workspace_id, start_date, end_date)
        return GSTReport(
            report_id=report_id,
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
            status="review_ready" if unreviewed == 0 else "draft",
            payload=payload,
            content_hash=hashlib.sha256(encoded.encode()).hexdigest(),
        )

    def commit(
        self, *, workspace_id: str, start_date: str, end_date: str
    ) -> GSTReport:
        report = self.preview(
            workspace_id=workspace_id, start_date=start_date, end_date=end_date
        )
        created_at = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS revision
                FROM gst_report_revisions WHERE report_id = ?
                """,
                (report.report_id,),
            ).fetchone()
            revision = int(row["revision"]) + 1
            connection.execute(
                """
                INSERT INTO gst_report_revisions(
                    report_id, revision, workspace_id, start_date, end_date,
                    accounting_basis, status, payload_json, content_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, 'payments', ?, ?, ?, ?)
                """,
                (
                    report.report_id,
                    revision,
                    workspace_id,
                    start_date,
                    end_date,
                    report.status,
                    canonical_json(report.payload),
                    report.content_hash,
                    created_at,
                ),
            )
        return report

    @staticmethod
    def csv_bytes(report: GSTReport) -> bytes:
        output = io.StringIO(newline="")
        fieldnames = (
            "date", "description", "amount_minor", "currency", "classification",
            "category", "gst_treatment", "gst_minor", "evidence_ids", "review_status",
        )
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for line in report.payload["lines"]:
            writer.writerow(
                {
                    "date": line["occurredOn"],
                    "description": line["description"],
                    "amount_minor": line["amountMinor"],
                    "currency": line["currency"],
                    "classification": line["classification"],
                    "category": line["category"] or "",
                    "gst_treatment": line["treatment"],
                    "gst_minor": line["gstMinor"],
                    "evidence_ids": "|".join(line["evidenceIds"]),
                    "review_status": "reviewed" if line["treatment"] != "unreviewed" else "unreviewed",
                }
            )
        return output.getvalue().encode("utf-8")
'''

SERVICE_METHODS = '''    async def set_gst_mapping(
        self,
        *,
        workspace_id: str,
        category: str,
        treatment: str,
        source: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return NZGSTService(self.store).set_mapping(
                workspace_id=workspace_id,
                category=category,
                treatment=treatment,  # type: ignore[arg-type]
                source=source,
            )

    async def gst_preview(
        self, *, workspace_id: str, start_date: str, end_date: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        report = NZGSTService(self.store).preview(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
        )
        return {
            "reportId": report.report_id,
            "status": report.status,
            "contentHash": report.content_hash,
            **report.payload,
        }

    async def commit_gst_report(
        self, *, workspace_id: str, start_date: str, end_date: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            report = NZGSTService(self.store).commit(
                workspace_id=workspace_id,
                start_date=start_date,
                end_date=end_date,
            )
        return {
            "reportId": report.report_id,
            "status": report.status,
            "contentHash": report.content_hash,
            "filed": False,
            "notice": report.payload["notice"],
        }

    async def gst_csv_payload(
        self, *, workspace_id: str, start_date: str, end_date: str
    ) -> ArtifactPayload:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        report = NZGSTService(self.store).preview(
            workspace_id=workspace_id,
            start_date=start_date,
            end_date=end_date,
        )
        content = NZGSTService.csv_bytes(report)
        return ArtifactPayload(
            content=content,
            media_type="text/csv; charset=utf-8",
            filename=f"folio-gst-working-paper-{start_date}-to-{end_date}.csv",
            content_hash=hashlib.sha256(content).hexdigest(),
        )
'''

ROUTE_MODELS = '''

class GSTMappingRequest(RequestModel):
    category: str = Field(min_length=1, max_length=200)
    treatment: str = Field(
        pattern=r"^(standard|zero_rated|exempt|out_of_scope|unreviewed)$"
    )
    source: str = Field(default="owner", pattern=r"^(owner|accountant|import)$")


class GSTPeriodRequest(RequestModel):
    start: date
    end: date
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/accounting/gst-mappings")
    async def set_gst_mapping(
        workspace_id: PathIdentifier,
        body: GSTMappingRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.set_gst_mapping(
                    workspace_id=workspace_id,
                    category=body.category,
                    treatment=body.treatment,
                    source=body.source,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/workspaces/{workspace_id}/accounting/gst-preview")
    async def gst_preview(
        workspace_id: PathIdentifier,
        body: GSTPeriodRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.gst_preview(
                    workspace_id=workspace_id,
                    start_date=body.start.isoformat(),
                    end_date=body.end.isoformat(),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/workspaces/{workspace_id}/accounting/gst-reports", status_code=201)
    async def commit_gst_report(
        workspace_id: PathIdentifier,
        body: GSTPeriodRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.commit_gst_report(
                    workspace_id=workspace_id,
                    start_date=body.start.isoformat(),
                    end_date=body.end.isoformat(),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/v1/workspaces/{workspace_id}/accounting/gst-export.csv")
    async def export_gst_csv(
        workspace_id: PathIdentifier,
        services: Services,
        start: Annotated[date, Query()],
        end: Annotated[date, Query()],
    ) -> Response:
        try:
            value = await services.gst_csv_payload(
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
from finance_agent.finance.accounting import NZGSTService, gst_inclusive_component
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def service(tmp_path: Path) -> NZGSTService:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    from finance_agent.jobs import DailyCloseService

    DailyCloseService(engine).run()
    return NZGSTService(store)


def test_standard_gst_inclusive_component_uses_exact_minor_units() -> None:
    assert gst_inclusive_component(11500) == 1500
    assert gst_inclusive_component(-11500) == 1500
    assert gst_inclusive_component(0) == 0


def test_unreviewed_business_transactions_keep_report_in_draft(tmp_path: Path) -> None:
    gst = service(tmp_path)
    report = gst.preview(
        workspace_id="ws_koru_studio",
        start_date="2026-07-01",
        end_date="2026-07-31",
    )
    assert report.status == "draft"
    assert report.payload["totals"]["unreviewedTransactionCount"] > 0
    assert report.payload["filed"] is False
    assert "not filed" in report.payload["notice"].lower()


def test_explicit_mappings_produce_evidence_linked_working_paper(tmp_path: Path) -> None:
    gst = service(tmp_path)
    for category in (
        "client_income", "studio_rent", "software_subscriptions",
    ):
        gst.set_mapping(
            workspace_id="ws_koru_studio",
            category=category,
            treatment="standard",
            source="accountant",
        )
    gst.set_mapping(
        workspace_id="ws_koru_studio",
        category="owner_draw",
        treatment="out_of_scope",
        source="accountant",
    )
    gst.set_mapping(
        workspace_id="ws_koru_studio",
        category="personal_meals",
        treatment="out_of_scope",
        source="accountant",
    )
    report = gst.preview(
        workspace_id="ws_koru_studio",
        start_date="2026-07-01",
        end_date="2026-07-31",
    )
    totals = report.payload["totals"]
    assert totals["outputGstMinor"] > 0
    assert totals["inputGstMinor"] > 0
    assert all(line["evidenceIds"] for line in report.payload["lines"])
    csv_bytes = gst.csv_bytes(report)
    assert b"gst_treatment" in csv_bytes
    assert b"evidence_ids" in csv_bytes


def test_report_commit_is_append_only_and_never_marks_filed(tmp_path: Path) -> None:
    gst = service(tmp_path)
    first = gst.commit(
        workspace_id="ws_koru_studio",
        start_date="2026-07-01",
        end_date="2026-07-31",
    )
    second = gst.commit(
        workspace_id="ws_koru_studio",
        start_date="2026-07-01",
        end_date="2026-07-31",
    )
    rows = gst.store.fetch_all(
        "SELECT revision, payload_json FROM gst_report_revisions WHERE report_id = ? ORDER BY revision",
        (first.report_id,),
    )
    assert [int(row["revision"]) for row in rows] == [1, 2]
    assert first.content_hash == second.content_hash
    assert all('"filed":false' in str(row["payload_json"]) for row in rows)
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


def add_accounting_module() -> None:
    write("services/api/src/finance_agent/finance/accounting.py", ACCOUNTING)
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance import FinanceEngine, FinanceStateError, FinanceTotals\n"
    import_line = "from finance_agent.finance.accounting import NZGSTService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("finance import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "privacy_inventory", SERVICE_METHODS)


def update_protocol_and_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def privacy_inventory(\n"
    addition = '''    async def set_gst_mapping(\n        self, *, workspace_id: str, category: str, treatment: str, source: str\n    ) -> Mapping[str, object]: ...\n\n    async def gst_preview(\n        self, *, workspace_id: str, start_date: str, end_date: str\n    ) -> Mapping[str, object]: ...\n\n    async def commit_gst_report(\n        self, *, workspace_id: str, start_date: str, end_date: str\n    ) -> Mapping[str, object]: ...\n\n    async def gst_csv_payload(\n        self, *, workspace_id: str, start_date: str, end_date: str\n    ) -> ArtifactPayload: ...\n\n'''
    if marker not in content:
        raise RuntimeError("privacy protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass PrivacySettingsRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("PrivacySettingsRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.get("/v1/workspaces/{workspace_id}/privacy/inventory")\n'
    if route_marker not in content:
        raise RuntimeError("privacy inventory route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/finance/test_nz_gst_accounting.py", TESTS)
    write("docs/NZ_GST_PREPARATION.md", '''# New Zealand GST preparation boundary\n\nFolio's GST surface is a deterministic working paper, not a filed return or tax opinion. The current implementation uses the standard 15% rate and derives the GST component from GST-inclusive amounts using the 3/23 relationship. A transaction contributes to input or output GST only after its category has an explicit treatment. Unreviewed transactions remain visible and keep the report in draft.\n\nThe report uses the payments basis because the current source model is bank-transaction based. Supporting the invoice basis requires invoice and settlement authority that Folio does not yet possess. Zero-rated, exempt and out-of-scope treatments produce no GST component. Personal transactions do not become deductible merely because they appear in the workspace.\n\nCSV exports preserve transaction IDs, evidence IDs, treatment and review status for accountant handoff. The product does not submit a GST return to Inland Revenue.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 11: deterministic NZ GST preparation\n\n- GST treatment is explicit per category and never inferred by a language model.\n- Standard-rate GST-inclusive values use exact minor units and the 3/23 relationship.\n- Zero-rated, exempt, out-of-scope, personal, and unreviewed states remain distinct.\n- Unreviewed business transactions keep the report in draft.\n- Report revisions are append-only and always record `filed: false`.\n- Neutral CSV working papers retain evidence identifiers for accountant review.\n'''
    if "## Stack 11: deterministic NZ GST preparation" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration()
    add_accounting_module()
    update_protocol_and_routes()
    add_tests_and_docs()
    print("NZ GST accounting changes applied")


if __name__ == "__main__":
    main()
