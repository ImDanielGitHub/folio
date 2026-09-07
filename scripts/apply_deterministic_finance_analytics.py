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
        name="deterministic_finance_analysis_receipts",
        sql="""
        CREATE TABLE finance_analysis_receipts (
            receipt_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            analysis_type TEXT NOT NULL CHECK (analysis_type = 'monthly_trends'),
            parameters_json TEXT NOT NULL,
            result_hash TEXT NOT NULL CHECK (length(result_hash) = 64),
            result_count INTEGER NOT NULL CHECK (result_count >= 0),
            evidence_ids_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX finance_analysis_workspace_time
            ON finance_analysis_receipts(workspace_id, created_at DESC);
        """,
    ),
'''

MODULE = '''"""Exact monthly finance trends and transparent deterministic anomaly thresholds."""

from __future__ import annotations

import calendar
import hashlib
import statistics
from datetime import UTC, date, datetime
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:24]}"


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _add_months(value: date, offset: int) -> date:
    month_index = value.year * 12 + value.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def _month_end(value: date) -> date:
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def _median(values: list[int]) -> int:
    if not values:
        return 0
    return int(statistics.median(values))


def _basis_points(delta: int, baseline: int) -> int | None:
    if baseline == 0:
        return None
    return int(delta * 10000 / abs(baseline))


class DeterministicFinanceAnalytics:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def monthly(
        self,
        *,
        workspace_id: str,
        as_of: str | None = None,
        months: int = 12,
    ) -> dict[str, object]:
        if not 2 <= months <= 36:
            raise ValueError("months must be between 2 and 36")
        target = date.fromisoformat(as_of) if as_of else datetime.now(UTC).date()
        last_month = _month_start(target)
        first_month = _add_months(last_month, -(months - 1))
        end_date = _month_end(last_month)
        rows = self.store.fetch_all(
            """
            SELECT transaction_id, occurred_on, description, amount_minor,
                   currency, classification, category, status, source_status,
                   evidence_id
            FROM transactions
            WHERE workspace_id = ? AND occurred_on BETWEEN ? AND ?
              AND status = 'posted' AND source_status = 'posted'
            ORDER BY occurred_on, transaction_id
            """,
            (workspace_id, first_month.isoformat(), end_date.isoformat()),
        )
        buckets: dict[str, dict[str, Any]] = {}
        for offset in range(months):
            start = _add_months(first_month, offset)
            buckets[start.strftime("%Y-%m")] = {
                "month": start.strftime("%Y-%m"),
                "periodStart": start.isoformat(),
                "periodEnd": _month_end(start).isoformat(),
                "operatingInflowMinor": 0,
                "operatingOutflowMinor": 0,
                "operatingNetMinor": 0,
                "businessExpenseMinor": 0,
                "personalExpenseMinor": 0,
                "unresolvedExpenseMinor": 0,
                "transferInMinor": 0,
                "transferOutMinor": 0,
                "transactionCount": 0,
                "categorySpendingMinor": {},
                "evidenceIds": [],
            }
        for row in rows:
            key = str(row["occurred_on"])[:7]
            bucket = buckets[key]
            amount = int(row["amount_minor"])
            classification = str(row["classification"])
            category = str(row["category"]) if row["category"] else "uncategorised"
            bucket["transactionCount"] += 1
            bucket["evidenceIds"].append(str(row["evidence_id"]))
            if classification == "transfer":
                if amount >= 0:
                    bucket["transferInMinor"] += amount
                else:
                    bucket["transferOutMinor"] += abs(amount)
                continue
            if amount >= 0:
                bucket["operatingInflowMinor"] += amount
            else:
                magnitude = abs(amount)
                bucket["operatingOutflowMinor"] += magnitude
                bucket["categorySpendingMinor"][category] = (
                    bucket["categorySpendingMinor"].get(category, 0) + magnitude
                )
                if classification == "business":
                    bucket["businessExpenseMinor"] += magnitude
                elif classification == "personal":
                    bucket["personalExpenseMinor"] += magnitude
                elif classification == "unresolved":
                    bucket["unresolvedExpenseMinor"] += magnitude
            bucket["operatingNetMinor"] = (
                bucket["operatingInflowMinor"] - bucket["operatingOutflowMinor"]
            )
        ordered = list(buckets.values())
        for bucket in ordered:
            bucket["evidenceIds"] = list(dict.fromkeys(bucket["evidenceIds"]))
            bucket["categorySpendingMinor"] = {
                key: bucket["categorySpendingMinor"][key]
                for key in sorted(bucket["categorySpendingMinor"])
            }
        latest = ordered[-1]
        previous = ordered[-2]
        comparisons: dict[str, dict[str, int | None]] = {}
        for field in (
            "operatingInflowMinor",
            "operatingOutflowMinor",
            "operatingNetMinor",
            "businessExpenseMinor",
            "personalExpenseMinor",
            "unresolvedExpenseMinor",
        ):
            delta = int(latest[field]) - int(previous[field])
            comparisons[field] = {
                "latestMinor": int(latest[field]),
                "previousMinor": int(previous[field]),
                "deltaMinor": delta,
                "deltaBasisPoints": _basis_points(delta, int(previous[field])),
            }
        categories = sorted(
            {
                category
                for bucket in ordered
                for category in bucket["categorySpendingMinor"]
            }
        )
        anomalies: list[dict[str, object]] = []
        for category in categories:
            historical = [
                int(bucket["categorySpendingMinor"].get(category, 0))
                for bucket in ordered[:-1]
            ]
            if len(historical) < 3:
                continue
            baseline = _median(historical)
            mad = _median([abs(value - baseline) for value in historical])
            threshold = max(5000, baseline // 4, mad * 3)
            current = int(latest["categorySpendingMinor"].get(category, 0))
            if current < 10000 or current <= baseline + threshold:
                continue
            anomalies.append(
                {
                    "category": category,
                    "currentMinor": current,
                    "baselineMedianMinor": baseline,
                    "medianAbsoluteDeviationMinor": mad,
                    "thresholdMinor": threshold,
                    "excessMinor": current - baseline,
                    "rule": "current > median + max(NZD 50, 25% of median, 3 × MAD)",
                    "evidenceIds": list(latest["evidenceIds"]),
                }
            )
        evidence_ids = list(
            dict.fromkeys(
                evidence
                for bucket in ordered
                for evidence in bucket["evidenceIds"]
            )
        )
        result = {
            "analysisVersion": "folio.monthly-finance-trends@1",
            "workspaceId": workspace_id,
            "currency": "NZD",
            "asOf": target.isoformat(),
            "monthsRequested": months,
            "periodStart": first_month.isoformat(),
            "periodEnd": end_date.isoformat(),
            "months": ordered,
            "latestComparison": comparisons,
            "categoryAnomalies": anomalies,
            "method": {
                "operatingCashflowExcludesTransfers": True,
                "pendingRowsExcluded": True,
                "anomalyBaseline": "median of prior requested months",
                "anomalySpread": "median absolute deviation",
                "minimumHistoryMonths": 3,
            },
            "evidenceIds": evidence_ids,
            "modelUsed": False,
            "externalCallsMade": False,
        }
        result_hash = hashlib.sha256(canonical_json(result).encode()).hexdigest()
        now = datetime.now(UTC).isoformat()
        receipt_id = _stable_id(
            "analysisrcpt", workspace_id, target.isoformat(), str(months), result_hash
        )
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO finance_analysis_receipts(
                    receipt_id, workspace_id, analysis_type, parameters_json,
                    result_hash, result_count, evidence_ids_json, created_at
                ) VALUES (?, ?, 'monthly_trends', ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_id) DO NOTHING
                """,
                (
                    receipt_id,
                    workspace_id,
                    canonical_json({"asOf": target.isoformat(), "months": months}),
                    result_hash,
                    len(ordered),
                    canonical_json(evidence_ids),
                    now,
                ),
            )
        return {**result, "receiptId": receipt_id, "resultHash": result_hash}
'''

SERVICE_METHOD = '''    async def monthly_finance_analytics(
        self, *, workspace_id: str, as_of: str | None, months: int
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return DeterministicFinanceAnalytics(self.store).monthly(
            workspace_id=workspace_id,
            as_of=as_of,
            months=months,
        )
'''

ROUTE = '''    @router.get("/v1/workspaces/{workspace_id}/analytics/monthly")
    async def monthly_finance_analytics(
        workspace_id: PathIdentifier,
        services: Services,
        as_of: Annotated[date | None, Query(alias="asOf")] = None,
        months: Annotated[int, Query(ge=2, le=36)] = 12,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.monthly_finance_analytics(
                    workspace_id=workspace_id,
                    as_of=as_of.isoformat() if as_of else None,
                    months=months,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

import hashlib
from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.finance.analytics import DeterministicFinanceAnalytics
from finance_agent.storage import SQLiteStore, canonical_json

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def add_source(store: SQLiteStore) -> None:
    digest = hashlib.sha256(b"analytics-source").hexdigest()
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO source_items(
                source_item_id, workspace_id, source_type, label, digest,
                mapping_version, received_at, status, row_count
            ) VALUES (
                'src_analytics_test', 'ws_koru_studio', 'csv', 'Analytics test source',
                ?, 'analytics_test@1', '2026-08-26T00:00:00+00:00', 'processed', 20
            )
            """,
            (digest,),
        )


def add_transaction(
    store: SQLiteStore,
    *,
    index: int,
    occurred_on: str,
    amount_minor: int,
    classification: str,
    category: str | None,
    account_id: str = "acct_koru_business",
) -> None:
    row_id = f"row_analytics_{index:03d}"
    transaction_id = f"txn_analytics_{index:03d}"
    evidence_id = f"evd_analytics_{index:03d}"
    raw = canonical_json({"date": occurred_on, "amountMinor": amount_minor})
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO source_rows(
                source_row_id, source_item_id, row_number, account_id,
                occurred_on, description, amount_minor, currency, source_status,
                external_reference, mapping_version, row_hash, raw_json
            ) VALUES (?, 'src_analytics_test', ?, ?, ?, ?, ?, 'NZD', 'posted', ?,
                'analytics_test@1', ?, ?)
            """,
            (
                row_id,
                index,
                account_id,
                occurred_on,
                f"Analytics transaction {index}",
                amount_minor,
                transaction_id,
                hashlib.sha256(raw.encode()).hexdigest(),
                raw,
            ),
        )
        connection.execute(
            """
            INSERT INTO evidence_links(
                evidence_id, workspace_id, source_item_id, source_row_id,
                label, created_at
            ) VALUES (?, 'ws_koru_studio', 'src_analytics_test', ?, ?,
                '2026-08-26T00:00:00+00:00')
            """,
            (evidence_id, row_id, f"Analytics evidence {index}"),
        )
        connection.execute(
            """
            INSERT INTO transactions(
                transaction_id, workspace_id, account_id, source_row_id, evidence_id,
                occurred_on, description, amount_minor, currency, source_status, status,
                classification, category, classification_source, rule_id,
                duplicate_of_transaction_id, created_at, updated_at
            ) VALUES (?, 'ws_koru_studio', ?, ?, ?, ?, ?, ?, 'NZD', 'posted', 'posted',
                ?, ?, 'deterministic', NULL, NULL,
                '2026-08-26T00:00:00+00:00', '2026-08-26T00:00:00+00:00')
            """,
            (
                transaction_id,
                account_id,
                row_id,
                evidence_id,
                occurred_on,
                f"Analytics transaction {index}",
                amount_minor,
                classification,
                category,
            ),
        )


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    add_source(store)
    index = 1
    for month in range(1, 7):
        add_transaction(
            store,
            index=index,
            occurred_on=f"2026-{month:02d}-05",
            amount_minor=200000,
            classification="business",
            category="client_income",
        )
        index += 1
        software = 10000 if month < 6 else 60000
        add_transaction(
            store,
            index=index,
            occurred_on=f"2026-{month:02d}-10",
            amount_minor=-software,
            classification="business",
            category="software_subscriptions",
        )
        index += 1
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO accounts(account_id, workspace_id, name, currency, created_at)
            VALUES ('acct_analytics_savings', 'ws_koru_studio', 'Analytics savings', 'NZD',
                '2026-08-26T00:00:00+00:00')
            """
        )
    add_transaction(
        store,
        index=index,
        occurred_on="2026-06-15",
        amount_minor=-50000,
        classification="transfer",
        category="internal_transfer",
    )
    index += 1
    add_transaction(
        store,
        index=index,
        occurred_on="2026-06-15",
        amount_minor=50000,
        classification="transfer",
        category="internal_transfer",
        account_id="acct_analytics_savings",
    )
    return store, DeterministicFinanceAnalytics(store)


def test_monthly_cashflow_excludes_transfers_and_reconciles_exactly(tmp_path: Path) -> None:
    _store, service = setup(tmp_path)
    value = service.monthly(
        workspace_id="ws_koru_studio",
        as_of="2026-06-30",
        months=6,
    )
    june = value["months"][-1]
    assert june["operatingInflowMinor"] == 200000
    assert june["operatingOutflowMinor"] == 60000
    assert june["operatingNetMinor"] == 140000
    assert june["transferInMinor"] == 50000
    assert june["transferOutMinor"] == 50000
    assert value["method"]["operatingCashflowExcludesTransfers"] is True
    assert value["modelUsed"] is False
    assert value["externalCallsMade"] is False
    assert value["evidenceIds"]


def test_category_anomaly_exposes_baseline_mad_and_threshold(tmp_path: Path) -> None:
    _store, service = setup(tmp_path)
    value = service.monthly(
        workspace_id="ws_koru_studio",
        as_of="2026-06-30",
        months=6,
    )
    anomaly = next(
        item for item in value["categoryAnomalies"]
        if item["category"] == "software_subscriptions"
    )
    assert anomaly["currentMinor"] == 60000
    assert anomaly["baselineMedianMinor"] == 10000
    assert anomaly["medianAbsoluteDeviationMinor"] == 0
    assert anomaly["thresholdMinor"] == 5000
    assert "median + max" in anomaly["rule"]


def test_latest_comparison_uses_integer_deltas_and_receipt_hash(tmp_path: Path) -> None:
    store, service = setup(tmp_path)
    value = service.monthly(
        workspace_id="ws_koru_studio",
        as_of="2026-06-30",
        months=6,
    )
    comparison = value["latestComparison"]["operatingOutflowMinor"]
    assert comparison["latestMinor"] == 60000
    assert comparison["previousMinor"] == 10000
    assert comparison["deltaMinor"] == 50000
    assert comparison["deltaBasisPoints"] == 50000
    assert len(value["resultHash"]) == 64
    row = store.fetch_one(
        "SELECT result_hash FROM finance_analysis_receipts WHERE receipt_id = ?",
        (value["receiptId"],),
    )
    assert str(row["result_hash"]) == value["resultHash"]
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
    write("services/api/src/finance_agent/finance/analytics.py", MODULE)


def update_service_protocol_route() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.statement_reconciliation import StatementReconciliationService\n"
    import_line = "from finance_agent.finance.analytics import DeterministicFinanceAnalytics\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("statement reconciliation import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "prepare_statement_reconciliation", SERVICE_METHOD)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def prepare_statement_reconciliation(\n"
    addition = '''    async def monthly_finance_analytics(\n        self, *, workspace_id: str, as_of: str | None, months: int\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("statement reconciliation protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    marker = '    @router.post("/v1/workspaces/{workspace_id}/statement-reconciliations")\n'
    if marker not in content:
        raise RuntimeError("statement reconciliation route marker missing")
    content = content.replace(marker, ROUTE + marker, 1)
    write(path, content)


def update_audit_docs_tests() -> None:
    path = "services/api/src/finance_agent/audit_trail.py"
    content = read(path)
    kind_marker = '        "statement_reconciliation",\n'
    if '"finance_analysis"' not in content:
        if kind_marker not in content:
            raise RuntimeError("statement reconciliation audit kind marker missing")
        content = content.replace(
            kind_marker,
            kind_marker + '        "finance_analysis",\n',
            1,
        )
    optional_marker = '        if self._table_exists("statement_reconciliation_revisions"):\n'
    block = '''        if self._table_exists("finance_analysis_receipts"):
            for row in self.store.fetch_all(
                "SELECT * FROM finance_analysis_receipts WHERE workspace_id = ? ORDER BY created_at, receipt_id",
                (workspace_id,),
            ):
                yield AuditEvent(
                    event_id=str(row["receipt_id"]),
                    workspace_id=workspace_id,
                    kind="finance_analysis",
                    action=str(row["analysis_type"]),
                    status="calculated",
                    occurred_at=str(row["created_at"]),
                    actor="system",
                    correlation_id=None,
                    subject_type="finance_analysis",
                    subject_id=str(row["receipt_id"]),
                    evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
                    metadata={
                        "resultHash": str(row["result_hash"]),
                        "resultCount": int(row["result_count"]),
                        "parametersIncluded": False,
                    },
                )
'''
    if "status=\"calculated\"" not in content:
        if optional_marker not in content:
            raise RuntimeError("statement reconciliation optional audit marker missing")
        content = content.replace(optional_marker, block + optional_marker, 1)
    write(path, content)

    write("services/api/tests/finance/test_deterministic_finance_analytics.py", TESTS)
    write("docs/FINANCE_ANALYTICS.md", '''# Deterministic finance analytics\n\nMonthly analytics use posted transactions and exact NZD minor units. Operating inflows, outflows and net exclude internal transfers; transfers remain visible in separate fields. Business, personal and unresolved expenses, category spend, transaction count and evidence are returned for every requested calendar month, including empty months. Pending rows are excluded.\n\nThe latest period is compared with the preceding period using exact deltas and integer basis points. Category anomaly flags require at least three prior months and fire only when current spend exceeds the prior median by more than the maximum of NZD 50, 25% of the median or three median absolute deviations. Every flag returns the baseline, MAD, threshold, excess, rule and evidence.\n\nAnalytics are model-free calculations, not forecasts, advice or claims that higher spending is wrong. The selected window and source coverage determine the result.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 41: deterministic monthly finance analytics\n\n- Posted operating cashflow excludes internal transfers and pending rows.\n- Monthly business, personal, unresolved and category values use exact minor units.\n- Latest/previous comparisons expose exact deltas and integer basis points.\n- Anomalies publish their median, MAD, threshold, rule and evidence.\n- Analysis receipts retain parameters, result hash and evidence.\n- Flags are transparent calculations, not advice or automatic finance changes.\n'''
    if "## Stack 41: deterministic monthly finance analytics" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_service_protocol_route()
    update_audit_docs_tests()
    print("deterministic finance analytics changes applied")


if __name__ == "__main__":
    main()
