from __future__ import annotations

import json
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
    if 'name="accounting_handoff"' in value:
        return
    addition = r'''
    Migration(
        version=24,
        name="accounting_handoff",
        sql="""
        CREATE TABLE accounting_profiles (
            workspace_id TEXT PRIMARY KEY REFERENCES workspaces(workspace_id),
            gst_registered INTEGER NOT NULL CHECK (gst_registered IN (0, 1)),
            gst_basis TEXT NOT NULL CHECK (gst_basis IN ('payments', 'invoice')),
            gst_number_masked TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE accounting_mappings (
            mapping_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            category TEXT NOT NULL,
            account_code TEXT NOT NULL,
            account_name TEXT NOT NULL,
            default_tax_code TEXT NOT NULL CHECK (
                default_tax_code IN ('GST', 'NO_GST', 'EXEMPT', 'UNRESOLVED')
            ),
            active INTEGER NOT NULL CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE (workspace_id, category)
        );

        CREATE TABLE accounting_tax_decisions (
            decision_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            transaction_id TEXT NOT NULL REFERENCES transactions(transaction_id),
            account_code TEXT NOT NULL,
            tax_code TEXT NOT NULL CHECK (
                tax_code IN ('GST', 'NO_GST', 'EXEMPT', 'UNRESOLVED')
            ),
            reason TEXT NOT NULL,
            request_id TEXT NOT NULL,
            evidence_id TEXT NOT NULL REFERENCES evidence_links(evidence_id),
            created_at TEXT NOT NULL,
            is_current INTEGER NOT NULL CHECK (is_current IN (0, 1)),
            UNIQUE (workspace_id, request_id)
        );

        CREATE UNIQUE INDEX accounting_one_current_decision
            ON accounting_tax_decisions(transaction_id) WHERE is_current = 1;

        CREATE TABLE accounting_period_locks (
            lock_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('locked', 'unlocked')),
            reason TEXT NOT NULL,
            request_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            unlocked_at TEXT,
            unlock_request_id TEXT,
            unlock_reason TEXT,
            CHECK (start_date <= end_date),
            UNIQUE (workspace_id, request_id)
        );

        CREATE TABLE accounting_exports (
            artifact_id TEXT PRIMARY KEY,
            export_group_id TEXT NOT NULL,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (kind IN ('accountant_csv', 'exceptions_json')),
            title TEXT NOT NULL,
            media_type TEXT NOT NULL,
            filename TEXT NOT NULL,
            content BLOB NOT NULL,
            content_hash TEXT NOT NULL CHECK (length(content_hash) = 64),
            evidence_ids_json TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            CHECK (period_start <= period_end)
        );

        CREATE INDEX accounting_exports_workspace_period
            ON accounting_exports(workspace_id, period_start, period_end, generated_at);
        CREATE INDEX accounting_decisions_transaction
            ON accounting_tax_decisions(transaction_id, created_at DESC);
        CREATE INDEX accounting_locks_workspace_period
            ON accounting_period_locks(workspace_id, start_date, end_date, status);
        """,
    ),
'''
    stripped = value.rstrip()
    if not stripped.endswith(")"):
        raise RuntimeError("migrations.py does not end with the migration tuple")
    write(path, stripped[:-1] + addition + ")\n")


def create_accounting_module() -> None:
    write(
        "services/api/src/finance_agent/accounting/__init__.py",
        '''from finance_agent.accounting.service import (\n    AccountingArtifact, AccountingExportResult, AccountingService, gst_component_minor,\n)\n\n__all__ = [\n    "AccountingArtifact", "AccountingExportResult", "AccountingService",\n    "gst_component_minor",\n]\n''',
    )
    write(
        "services/api/src/finance_agent/accounting/service.py",
        '''"""Deterministic New Zealand accounting handoff without pretending to be a ledger."""\n\nfrom __future__ import annotations\n\nimport csv\nimport hashlib\nimport io\nimport json\nimport re\nfrom dataclasses import dataclass\nfrom datetime import UTC, date, datetime\nfrom decimal import Decimal, ROUND_HALF_UP\nfrom typing import Any\n\nfrom finance_agent.storage import SQLiteStore, canonical_json\n\nTAX_CODES = frozenset({"GST", "NO_GST", "EXEMPT", "UNRESOLVED"})\nACCOUNT_CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]{1,19}$")\nDEFAULT_MAPPINGS = (\n    ("client_income", "200", "Sales", "GST"),\n    ("studio_rent", "400", "Rent", "UNRESOLVED"),\n    ("software_subscriptions", "410", "Software subscriptions", "UNRESOLVED"),\n    ("client_fit_out_materials", "420", "Client materials", "UNRESOLVED"),\n    ("owner_draw", "900", "Owner drawings", "NO_GST"),\n    ("personal_meals", "901", "Personal expenditure", "NO_GST"),\n    ("transfer", "980", "Transfers", "NO_GST"),\n    ("unresolved", "999", "Suspense and review", "UNRESOLVED"),\n)\n\n\n@dataclass(frozen=True, slots=True)\nclass AccountingArtifact:\n    content: bytes\n    media_type: str\n    filename: str\n    content_hash: str\n\n\n@dataclass(frozen=True, slots=True)\nclass AccountingExportResult:\n    export_group_id: str\n    workspace_id: str\n    period_start: str\n    period_end: str\n    csv_artifact_id: str\n    exceptions_artifact_id: str\n    transaction_count: int\n    exception_count: int\n    gross_minor: int\n    gst_minor: int\n    net_minor: int\n\n    def as_contract(self) -> dict[str, object]:\n        return {\n            "exportGroupId": self.export_group_id,\n            "workspaceId": self.workspace_id,\n            "period": {"start": self.period_start, "end": self.period_end},\n            "artifacts": [\n                {"artifactId": self.csv_artifact_id, "format": "csv"},\n                {"artifactId": self.exceptions_artifact_id, "format": "json"},\n            ],\n            "transactionCount": self.transaction_count,\n            "exceptionCount": self.exception_count,\n            "grossMinor": self.gross_minor,\n            "gstMinor": self.gst_minor,\n            "netMinor": self.net_minor,\n            "currency": "NZD",\n            "preparatory": True,\n        }\n\n\ndef gst_component_minor(gross_minor: int) -> int:\n    """Return the signed 3/23 GST component of a GST-inclusive NZD amount."""\n\n    sign = -1 if gross_minor < 0 else 1\n    magnitude = (\n        Decimal(abs(gross_minor)) * Decimal(3) / Decimal(23)\n    ).quantize(Decimal("1"), rounding=ROUND_HALF_UP)\n    return sign * int(magnitude)\n\n\ndef _stable_id(prefix: str, *parts: str) -> str:\n    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]\n    return f"{prefix}_{digest}"\n\n\ndef _safe_spreadsheet_text(value: str) -> str:\n    cleaned = value.replace("\\x00", "").replace("\\r", " ").replace("\\n", " ").strip()\n    if cleaned.startswith(("=", "+", "-", "@")):\n        return "'" + cleaned\n    return cleaned\n\n\nclass AccountingService:\n    def __init__(self, store: SQLiteStore) -> None:\n        self.store = store\n\n    def ensure_default(self, workspace_id: str, *, occurred_at: str | None = None) -> None:\n        workspace = self.store.fetch_one(\n            "SELECT workspace_id FROM workspaces WHERE workspace_id = ?", (workspace_id,)\n        )\n        if workspace is None:\n            return\n        instant = occurred_at or datetime.now(UTC).isoformat()\n        with self.store.transaction() as connection:\n            connection.execute(\n                """\n                INSERT INTO accounting_profiles(\n                    workspace_id, gst_registered, gst_basis, gst_number_masked, updated_at\n                ) VALUES (?, 0, 'payments', NULL, ?)\n                ON CONFLICT(workspace_id) DO NOTHING\n                """,\n                (workspace_id, instant),\n            )\n            for category, account_code, account_name, tax_code in DEFAULT_MAPPINGS:\n                connection.execute(\n                    """\n                    INSERT INTO accounting_mappings(\n                        mapping_id, workspace_id, category, account_code, account_name,\n                        default_tax_code, active, created_at, updated_at\n                    ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)\n                    ON CONFLICT(workspace_id, category) DO NOTHING\n                    """,\n                    (\n                        _stable_id("acctmap", workspace_id, category), workspace_id, category,\n                        account_code, account_name, tax_code, instant, instant,\n                    ),\n                )\n\n    def profile(self, workspace_id: str) -> dict[str, object]:\n        row = self.store.fetch_one(\n            "SELECT * FROM accounting_profiles WHERE workspace_id = ?",\n            (workspace_id,),\n        )\n        if row is None:\n            raise KeyError(workspace_id)\n        return {\n            "workspaceId": workspace_id,\n            "gstRegistered": bool(row["gst_registered"]),\n            "gstBasis": str(row["gst_basis"]),\n            "gstNumberMasked": str(row["gst_number_masked"]) if row["gst_number_masked"] else None,\n            "updatedAt": str(row["updated_at"]),\n        }\n\n    def configure_profile(\n        self,\n        workspace_id: str,\n        *,\n        gst_registered: bool,\n        gst_basis: str,\n        gst_number_masked: str | None,\n        occurred_at: str | None = None,\n    ) -> dict[str, object]:\n        if gst_basis not in {"payments", "invoice"}:\n            raise ValueError("gst_basis must be payments or invoice")\n        if gst_number_masked is not None and len(gst_number_masked) > 32:\n            raise ValueError("masked GST number is too long")\n        instant = occurred_at or datetime.now(UTC).isoformat()\n        with self.store.transaction() as connection:\n            updated = connection.execute(\n                """\n                UPDATE accounting_profiles\n                SET gst_registered = ?, gst_basis = ?, gst_number_masked = ?, updated_at = ?\n                WHERE workspace_id = ?\n                """,\n                (int(gst_registered), gst_basis, gst_number_masked, instant, workspace_id),\n            )\n            if updated.rowcount != 1:\n                raise KeyError(workspace_id)\n        return self.profile(workspace_id)\n\n    def _locked(self, workspace_id: str, occurred_on: str) -> bool:\n        row = self.store.fetch_one(\n            """\n            SELECT 1 FROM accounting_period_locks\n            WHERE workspace_id = ? AND status = 'locked'\n              AND start_date <= ? AND end_date >= ?\n            LIMIT 1\n            """,\n            (workspace_id, occurred_on, occurred_on),\n        )\n        return row is not None\n\n    def review_transaction(\n        self,\n        workspace_id: str,\n        transaction_id: str,\n        *,\n        account_code: str,\n        tax_code: str,\n        reason: str,\n        request_id: str,\n        occurred_at: str | None = None,\n    ) -> dict[str, object]:\n        if not ACCOUNT_CODE_PATTERN.fullmatch(account_code):\n            raise ValueError("account_code is outside the bounded export format")\n        if tax_code not in TAX_CODES:\n            raise ValueError("unsupported tax_code")\n        if not reason.strip() or len(reason) > 500:\n            raise ValueError("review reason must be non-empty and at most 500 characters")\n        transaction = self.store.fetch_one(\n            """\n            SELECT workspace_id, occurred_on, evidence_id\n            FROM transactions WHERE transaction_id = ?\n            """,\n            (transaction_id,),\n        )\n        if transaction is None:\n            raise KeyError(transaction_id)\n        if str(transaction["workspace_id"]) != workspace_id:\n            raise ValueError("transaction belongs to another workspace")\n        occurred_on = str(transaction["occurred_on"])\n        if self._locked(workspace_id, occurred_on):\n            raise ValueError("transaction date is inside a locked accounting period")\n        instant = occurred_at or datetime.now(UTC).isoformat()\n        decision_id = _stable_id("taxdecision", workspace_id, request_id)\n        with self.store.transaction() as connection:\n            existing = connection.execute(\n                "SELECT * FROM accounting_tax_decisions WHERE workspace_id = ? AND request_id = ?",\n                (workspace_id, request_id),\n            ).fetchone()\n            if existing is not None:\n                return self._decision_contract(existing)\n            connection.execute(\n                "UPDATE accounting_tax_decisions SET is_current = 0 WHERE transaction_id = ? AND is_current = 1",\n                (transaction_id,),\n            )\n            connection.execute(\n                """\n                INSERT INTO accounting_tax_decisions(\n                    decision_id, workspace_id, transaction_id, account_code, tax_code,\n                    reason, request_id, evidence_id, created_at, is_current\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)\n                """,\n                (\n                    decision_id, workspace_id, transaction_id, account_code, tax_code,\n                    reason.strip(), request_id, str(transaction["evidence_id"]), instant,\n                ),\n            )\n        row = self.store.fetch_one(\n            "SELECT * FROM accounting_tax_decisions WHERE decision_id = ?",\n            (decision_id,),\n        )\n        assert row is not None\n        return self._decision_contract(row)\n\n    @staticmethod\n    def _decision_contract(row) -> dict[str, object]:\n        return {\n            "decisionId": str(row["decision_id"]),\n            "workspaceId": str(row["workspace_id"]),\n            "transactionId": str(row["transaction_id"]),\n            "accountCode": str(row["account_code"]),\n            "taxCode": str(row["tax_code"]),\n            "reason": str(row["reason"]),\n            "requestId": str(row["request_id"]),\n            "evidenceId": str(row["evidence_id"]),\n            "createdAt": str(row["created_at"]),\n        }\n\n    def lock_period(\n        self, workspace_id: str, *, start: str, end: str, reason: str,\n        request_id: str, occurred_at: str | None = None,\n    ) -> dict[str, object]:\n        start_date = date.fromisoformat(start)\n        end_date = date.fromisoformat(end)\n        if start_date > end_date:\n            raise ValueError("period start must be on or before end")\n        if not reason.strip() or len(reason) > 500:\n            raise ValueError("lock reason must be non-empty and at most 500 characters")\n        instant = occurred_at or datetime.now(UTC).isoformat()\n        lock_id = _stable_id("periodlock", workspace_id, request_id)\n        with self.store.transaction() as connection:\n            overlap = connection.execute(\n                """\n                SELECT lock_id FROM accounting_period_locks\n                WHERE workspace_id = ? AND status = 'locked'\n                  AND NOT (end_date < ? OR start_date > ?)\n                LIMIT 1\n                """,\n                (workspace_id, start, end),\n            ).fetchone()\n            if overlap is not None:\n                raise ValueError("accounting period overlaps an existing lock")\n            connection.execute(\n                """\n                INSERT INTO accounting_period_locks(\n                    lock_id, workspace_id, start_date, end_date, status, reason,\n                    request_id, created_at\n                ) VALUES (?, ?, ?, ?, 'locked', ?, ?, ?)\n                """,\n                (lock_id, workspace_id, start, end, reason.strip(), request_id, instant),\n            )\n        return {\n            "lockId": lock_id, "workspaceId": workspace_id, "start": start,\n            "end": end, "status": "locked", "reason": reason.strip(),\n        }\n\n    def unlock_period(\n        self, workspace_id: str, lock_id: str, *, request_id: str, reason: str,\n        occurred_at: str | None = None,\n    ) -> dict[str, object]:\n        if not reason.strip() or len(reason) > 500:\n            raise ValueError("unlock reason must be non-empty and at most 500 characters")\n        instant = occurred_at or datetime.now(UTC).isoformat()\n        with self.store.transaction() as connection:\n            row = connection.execute(\n                "SELECT workspace_id, status, unlock_request_id FROM accounting_period_locks WHERE lock_id = ?",\n                (lock_id,),\n            ).fetchone()\n            if row is None:\n                raise KeyError(lock_id)\n            if str(row["workspace_id"]) != workspace_id:\n                raise ValueError("period lock belongs to another workspace")\n            if str(row["status"]) == "unlocked":\n                if str(row["unlock_request_id"]) != request_id:\n                    raise ValueError("period lock was unlocked by another request")\n                return {"lockId": lock_id, "workspaceId": workspace_id, "status": "unlocked"}\n            connection.execute(\n                """\n                UPDATE accounting_period_locks\n                SET status = 'unlocked', unlocked_at = ?, unlock_request_id = ?, unlock_reason = ?\n                WHERE lock_id = ?\n                """,\n                (instant, request_id, reason.strip(), lock_id),\n            )\n        return {"lockId": lock_id, "workspaceId": workspace_id, "status": "unlocked"}\n\n    def _mapping(self, workspace_id: str, category: str | None, classification: str) -> tuple[str, str]:\n        key = category or ("transfer" if classification == "transfer" else "unresolved")\n        row = self.store.fetch_one(\n            """\n            SELECT account_code, default_tax_code FROM accounting_mappings\n            WHERE workspace_id = ? AND category = ? AND active = 1\n            """,\n            (workspace_id, key),\n        )\n        if row is None:\n            return "999", "UNRESOLVED"\n        return str(row["account_code"]), str(row["default_tax_code"])\n\n    def prepare_export(\n        self, workspace_id: str, *, start: str, end: str, generated_at: str | None = None\n    ) -> AccountingExportResult:\n        start_date = date.fromisoformat(start)\n        end_date = date.fromisoformat(end)\n        if start_date > end_date:\n            raise ValueError("period start must be on or before end")\n        profile = self.profile(workspace_id)\n        rows = self.store.fetch_all(\n            """\n            SELECT t.transaction_id, t.occurred_on, t.description, t.amount_minor,\n                   t.currency, t.classification, t.category, t.evidence_id,\n                   d.account_code AS reviewed_account_code, d.tax_code AS reviewed_tax_code\n            FROM transactions t\n            LEFT JOIN accounting_tax_decisions d\n              ON d.transaction_id = t.transaction_id AND d.is_current = 1\n            WHERE t.workspace_id = ? AND t.status = 'posted'\n              AND t.occurred_on BETWEEN ? AND ?\n            ORDER BY t.occurred_on, t.transaction_id\n            """,\n            (workspace_id, start, end),\n        )\n        output_rows: list[dict[str, Any]] = []\n        exceptions: list[dict[str, Any]] = []\n        evidence: list[str] = []\n        for row in rows:\n            gross = int(row["amount_minor"])\n            classification = str(row["classification"])\n            category = str(row["category"]) if row["category"] else None\n            default_account, default_tax = self._mapping(workspace_id, category, classification)\n            account_code = str(row["reviewed_account_code"] or default_account)\n            tax_code = str(row["reviewed_tax_code"] or default_tax)\n            if not bool(profile["gstRegistered"]):\n                tax_code = "NO_GST"\n            elif gross < 0 and row["reviewed_tax_code"] is None and tax_code == "UNRESOLVED":\n                exceptions.append({\n                    "transactionId": str(row["transaction_id"]),\n                    "reason": "GST treatment requires reviewed tax-invoice evidence.",\n                    "evidenceId": str(row["evidence_id"]),\n                })\n            elif classification == "unresolved" and row["reviewed_tax_code"] is None:\n                tax_code = "UNRESOLVED"\n                exceptions.append({\n                    "transactionId": str(row["transaction_id"]),\n                    "reason": "Bookkeeping classification remains unresolved.",\n                    "evidenceId": str(row["evidence_id"]),\n                })\n            gst = gst_component_minor(gross) if tax_code == "GST" else 0\n            net = gross - gst\n            evidence_id = str(row["evidence_id"])
            evidence.append(evidence_id)\n            output_rows.append({\n                "date": str(row["occurred_on"]),\n                "description": _safe_spreadsheet_text(str(row["description"])),\n                "gross_minor": gross,\n                "gst_minor": gst,\n                "net_minor": net,\n                "currency": str(row["currency"]),\n                "classification": classification,\n                "category": category or "",\n                "account_code": account_code,\n                "tax_code": tax_code,\n                "transaction_id": str(row["transaction_id"]),\n                "evidence_id": evidence_id,\n            })\n\n        csv_buffer = io.StringIO(newline="")\n        fieldnames = [\n            "date", "description", "gross_minor", "gst_minor", "net_minor",\n            "currency", "classification", "category", "account_code", "tax_code",\n            "transaction_id", "evidence_id",\n        ]\n        writer = csv.DictWriter(csv_buffer, fieldnames=fieldnames, lineterminator="\\n")\n        writer.writeheader()\n        writer.writerows(output_rows)\n        csv_content = csv_buffer.getvalue().encode("utf-8")\n        exception_payload = {\n            "workspaceId": workspace_id,\n            "period": {"start": start, "end": end},\n            "preparatory": True,\n            "warning": (\n                "Review with a qualified accountant before filing GST or importing into an authoritative ledger."\n            ),\n            "exceptions": exceptions,\n        }\n        exceptions_content = (canonical_json(exception_payload) + "\\n").encode("utf-8")\n        content_seed = hashlib.sha256(csv_content + b"\\0" + exceptions_content).hexdigest()\n        group_id = f"acctexport_{content_seed[:24]}"\n        csv_artifact_id = f"artifact_accounting_csv_{content_seed[:20]}"\n        exceptions_artifact_id = f"artifact_accounting_exceptions_{content_seed[:20]}"\n        instant = generated_at or datetime.now(UTC).isoformat()\n        artifact_values = (\n            (\n                csv_artifact_id, "accountant_csv",\n                f"Folio accountant export {start} to {end}", "text/csv; charset=utf-8",\n                f"folio-accountant-export-{start}-to-{end}.csv", csv_content,\n            ),\n            (\n                exceptions_artifact_id, "exceptions_json",\n                f"Folio accounting exceptions {start} to {end}", "application/json",\n                f"folio-accounting-exceptions-{start}-to-{end}.json", exceptions_content,\n            ),\n        )\n        unique_evidence = list(dict.fromkeys(evidence))\n        with self.store.transaction() as connection:\n            for artifact_id, kind, title, media_type, filename, content in artifact_values:\n                connection.execute(\n                    """\n                    INSERT INTO accounting_exports(\n                        artifact_id, export_group_id, workspace_id, period_start, period_end,\n                        kind, title, media_type, filename, content, content_hash,\n                        evidence_ids_json, generated_at\n                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)\n                    ON CONFLICT(artifact_id) DO NOTHING\n                    """,\n                    (\n                        artifact_id, group_id, workspace_id, start, end, kind, title,\n                        media_type, filename, content, hashlib.sha256(content).hexdigest(),\n                        canonical_json(unique_evidence), instant,\n                    ),\n                )\n        gross_total = sum(int(row["gross_minor"]) for row in output_rows)\n        gst_total = sum(int(row["gst_minor"]) for row in output_rows)\n        return AccountingExportResult(\n            export_group_id=group_id, workspace_id=workspace_id, period_start=start,\n            period_end=end, csv_artifact_id=csv_artifact_id,\n            exceptions_artifact_id=exceptions_artifact_id,\n            transaction_count=len(output_rows), exception_count=len(exceptions),\n            gross_minor=gross_total, gst_minor=gst_total, net_minor=gross_total - gst_total,\n        )\n\n    def get_artifact(self, artifact_id: str) -> AccountingArtifact:\n        row = self.store.fetch_one(\n            """\n            SELECT content, media_type, filename, content_hash\n            FROM accounting_exports WHERE artifact_id = ?\n            """,\n            (artifact_id,),\n        )\n        if row is None:\n            raise KeyError(artifact_id)\n        return AccountingArtifact(\n            content=bytes(row["content"]), media_type=str(row["media_type"]),\n            filename=str(row["filename"]), content_hash=str(row["content_hash"]),\n        )\n''',
    )


def patch_route_protocol() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    value = read(path)
    anchor = """    async def scheduler_status(self, workspace_id: str) -> Mapping[str, object]: ...\n"""
    methods = """    async def accounting_profile(self, workspace_id: str) -> Mapping[str, object]: ...\n\n    async def configure_accounting_profile(\n        self, *, workspace_id: str, gst_registered: bool, gst_basis: str,\n        gst_number_masked: str | None\n    ) -> Mapping[str, object]: ...\n\n    async def review_accounting_transaction(\n        self, *, workspace_id: str, transaction_id: str, account_code: str,\n        tax_code: str, reason: str, request_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def lock_accounting_period(\n        self, *, workspace_id: str, start: str, end: str, reason: str, request_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def unlock_accounting_period(\n        self, *, workspace_id: str, lock_id: str, reason: str, request_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def prepare_accounting_export(\n        self, *, workspace_id: str, start: str, end: str\n    ) -> Mapping[str, object]: ...\n\n"""
    if anchor not in value:
        raise RuntimeError("scheduler protocol anchor is missing")
    write(path, value.replace(anchor, methods + anchor, 1))


def patch_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/router.py"
    value = read(path)
    request_anchor = """class SchedulerConfigurationRequest(RequestModel):\n"""
    models = """class AccountingProfileRequest(RequestModel):\n    workspace_id: str = Field(alias=\"workspaceId\")\n    gst_registered: bool = Field(alias=\"gstRegistered\")\n    gst_basis: str = Field(alias=\"gstBasis\", pattern=r\"^(payments|invoice)$\")\n    gst_number_masked: str | None = Field(\n        default=None, alias=\"gstNumberMasked\", max_length=32\n    )\n\n\nclass AccountingReviewRequest(RequestModel):\n    workspace_id: str = Field(alias=\"workspaceId\")\n    account_code: str = Field(alias=\"accountCode\", pattern=r\"^[A-Z0-9][A-Z0-9-]{1,19}$\")\n    tax_code: str = Field(alias=\"taxCode\", pattern=r\"^(GST|NO_GST|EXEMPT|UNRESOLVED)$\")\n    reason: str = Field(min_length=1, max_length=500)\n    request_id: str = Field(alias=\"requestId\", min_length=1, max_length=160)\n\n\nclass AccountingPeriodRequest(RequestModel):\n    workspace_id: str = Field(alias=\"workspaceId\")\n    start: date\n    end: date\n    reason: str = Field(min_length=1, max_length=500)\n    request_id: str = Field(alias=\"requestId\", min_length=1, max_length=160)\n\n\nclass AccountingUnlockRequest(RequestModel):\n    workspace_id: str = Field(alias=\"workspaceId\")\n    reason: str = Field(min_length=1, max_length=500)\n    request_id: str = Field(alias=\"requestId\", min_length=1, max_length=160)\n\n\nclass AccountingExportRequest(RequestModel):\n    workspace_id: str = Field(alias=\"workspaceId\")\n    start: date\n    end: date\n\n\n"""
    if request_anchor not in value:
        raise RuntimeError("scheduler request anchor is missing")
    value = value.replace(request_anchor, models + request_anchor, 1)

    route_anchor = '''    @router.get("/v1/scheduler/status")\n'''
    routes = '''    @router.get("/v1/accounting/profile")\n    async def accounting_profile(\n        services: Services,\n        workspace_id: Annotated[str, Query(alias="workspaceId")],\n    ) -> dict[str, object]:\n        return dict(await services.accounting_profile(workspace_id))\n\n    @router.put("/v1/accounting/profile")\n    async def configure_accounting_profile(\n        body: AccountingProfileRequest,\n        services: Services,\n    ) -> dict[str, object]:\n        return dict(\n            await services.configure_accounting_profile(\n                workspace_id=body.workspace_id,\n                gst_registered=body.gst_registered,\n                gst_basis=body.gst_basis,\n                gst_number_masked=body.gst_number_masked,\n            )\n        )\n\n    @router.post("/v1/accounting/transactions/{transaction_id}/review")\n    async def review_accounting_transaction(\n        transaction_id: str,\n        body: AccountingReviewRequest,\n        services: Services,\n    ) -> dict[str, object]:\n        return dict(\n            await services.review_accounting_transaction(\n                workspace_id=body.workspace_id, transaction_id=transaction_id,\n                account_code=body.account_code, tax_code=body.tax_code,\n                reason=body.reason, request_id=body.request_id,\n            )\n        )\n\n    @router.post("/v1/accounting/period-locks", status_code=201)\n    async def lock_accounting_period(\n        body: AccountingPeriodRequest,\n        services: Services,\n    ) -> dict[str, object]:\n        return dict(\n            await services.lock_accounting_period(\n                workspace_id=body.workspace_id, start=body.start.isoformat(),\n                end=body.end.isoformat(), reason=body.reason, request_id=body.request_id,\n            )\n        )\n\n    @router.post("/v1/accounting/period-locks/{lock_id}/unlock")\n    async def unlock_accounting_period(\n        lock_id: str,\n        body: AccountingUnlockRequest,\n        services: Services,\n    ) -> dict[str, object]:\n        return dict(\n            await services.unlock_accounting_period(\n                workspace_id=body.workspace_id, lock_id=lock_id, reason=body.reason,\n                request_id=body.request_id,\n            )\n        )\n\n    @router.post("/v1/accounting/exports", status_code=201)\n    async def prepare_accounting_export(\n        body: AccountingExportRequest,\n        services: Services,\n    ) -> dict[str, object]:\n        return dict(\n            await services.prepare_accounting_export(\n                workspace_id=body.workspace_id, start=body.start.isoformat(),\n                end=body.end.isoformat(),\n            )\n        )\n\n'''
    if route_anchor not in value:
        raise RuntimeError("scheduler route anchor is missing")
    write(path, value.replace(route_anchor, routes + route_anchor, 1))


def patch_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    value = read(path)
    value = replace_once(
        value,
        "from finance_agent.agent.ports import FinanceContext, FinanceServiceResult\n",
        "from finance_agent.accounting import AccountingService\nfrom finance_agent.agent.ports import FinanceContext, FinanceServiceResult\n",
        label="accounting service import",
    )
    value = replace_once(
        value,
        """        self.daily_close = DailyCloseService(self.engine)\n""",
        """        self.daily_close = DailyCloseService(self.engine)\n        self.accounting = AccountingService(self.store)\n""",
        label="accounting service composition",
    )
    value = replace_once(
        value,
        """        self.scheduler.ensure_default(WORKSPACE_ID)\n""",
        """        self.scheduler.ensure_default(WORKSPACE_ID)\n        self.accounting.ensure_default(WORKSPACE_ID)\n""",
        label="accounting defaults",
    )
    anchor = """    async def scheduler_status(self, workspace_id: str) -> Mapping[str, object]:\n"""
    methods = '''    async def accounting_profile(self, workspace_id: str) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        return self.accounting.profile(workspace_id)\n\n    async def configure_accounting_profile(\n        self, *, workspace_id: str, gst_registered: bool, gst_basis: str,\n        gst_number_masked: str | None\n    ) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        async with self._lock:\n            return self.accounting.configure_profile(\n                workspace_id, gst_registered=gst_registered, gst_basis=gst_basis,\n                gst_number_masked=gst_number_masked,\n            )\n\n    async def review_accounting_transaction(\n        self, *, workspace_id: str, transaction_id: str, account_code: str,\n        tax_code: str, reason: str, request_id: str\n    ) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        async with self._lock:\n            return self.accounting.review_transaction(\n                workspace_id, transaction_id, account_code=account_code,\n                tax_code=tax_code, reason=reason, request_id=request_id,\n            )\n\n    async def lock_accounting_period(\n        self, *, workspace_id: str, start: str, end: str, reason: str, request_id: str\n    ) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        async with self._lock:\n            return self.accounting.lock_period(\n                workspace_id, start=start, end=end, reason=reason, request_id=request_id\n            )\n\n    async def unlock_accounting_period(\n        self, *, workspace_id: str, lock_id: str, reason: str, request_id: str\n    ) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        async with self._lock:\n            return self.accounting.unlock_period(\n                workspace_id, lock_id, reason=reason, request_id=request_id\n            )\n\n    async def prepare_accounting_export(\n        self, *, workspace_id: str, start: str, end: str\n    ) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID:\n            raise KeyError(workspace_id)\n        async with self._lock:\n            result = self.accounting.prepare_export(workspace_id, start=start, end=end)\n        return result.as_contract()\n\n'''
    if anchor not in value:
        raise RuntimeError("scheduler method anchor is missing")
    value = value.replace(anchor, methods + anchor, 1)

    old_artifact = '''    async def artifact(self, artifact_id: str) -> ArtifactPayload:\n        media_type, content, content_hash = self.engine.get_artifact(artifact_id)\n        suffix = "pdf" if media_type == "application/pdf" else "html"\n        return ArtifactPayload(\n            content=content,\n            media_type=media_type,\n            filename=f"koru-studio-owner-pack.{suffix}",\n            content_hash=content_hash,\n        )\n'''
    new_artifact = '''    async def artifact(self, artifact_id: str) -> ArtifactPayload:\n        if artifact_id.startswith("artifact_accounting_"):\n            value = self.accounting.get_artifact(artifact_id)\n            return ArtifactPayload(\n                content=value.content, media_type=value.media_type,\n                filename=value.filename, content_hash=value.content_hash,\n            )\n        media_type, content, content_hash = self.engine.get_artifact(artifact_id)\n        suffix = "pdf" if media_type == "application/pdf" else "html"\n        return ArtifactPayload(\n            content=content,\n            media_type=media_type,\n            filename=f"koru-studio-owner-pack.{suffix}",\n            content_hash=content_hash,\n        )\n'''
    value = replace_once(value, old_artifact, new_artifact, label="accounting artifact delivery")
    write(path, value)


def patch_electron_artifacts() -> None:
    path = "apps/desktop/src/main/main.ts"
    value = read(path)
    old = '''  const extension = mediaType === "application/pdf" ? "pdf" : mediaType === "text/html" ? "html" : null;\n'''
    new = '''  const extension = mediaType === "application/pdf"\n    ? "pdf"\n    : mediaType === "text/html"\n      ? "html"\n      : mediaType === "text/csv"\n        ? "csv"\n        : mediaType === "application/json"\n          ? "json"\n          : null;\n'''
    value = replace_once(value, old, new, label="accounting artifact media types")
    value = replace_once(
        value,
        '    headers: {\n      Accept: "application/pdf,text/html",\n',
        '    headers: {\n      Accept: "application/pdf,text/html,text/csv,application/json",\n',
        label="accounting artifact accept header",
    )
    write(path, value)


def add_docs() -> None:
    write(
        "docs/ACCOUNTING_BOUNDARY.md",
        '''# Folio accounting handoff boundary\n\nFolio prepares evidence-linked bookkeeping exports. It is not an authoritative general ledger, GST return or tax adviser.\n\nThe accounting handoff deliberately fails closed:\n\n- all amounts stay in integer NZD minor units;\n- GST-inclusive amounts use the exact 3/23 fraction with half-up cent rounding;\n- unreviewed expense GST remains `UNRESOLVED` because a bank row alone is not proof of a valid tax invoice;\n- personal and transfer rows carry no GST;\n- explicit transaction reviews are append-only and blocked by locked periods;\n- exports include transaction and evidence identifiers plus a separate exceptions file;\n- spreadsheet formula prefixes are escaped before CSV generation;\n- every export is content-hashed and retained as immutable preparatory evidence.\n\nA qualified accountant must review exports before they are imported into an accounting system or used for filing.\n''',
    )


def add_tests() -> None:
    write(
        "services/api/tests/accounting/test_accounting_handoff.py",
        '''from __future__ import annotations\n\nimport csv\nimport io\nimport json\nfrom pathlib import Path\n\nimport pytest\n\nfrom finance_agent.accounting import AccountingService, gst_component_minor\nfrom finance_agent.finance import FinanceEngine\nfrom finance_agent.jobs import DailyCloseService\nfrom finance_agent.storage import SQLiteStore\n\nROOT = Path(__file__).resolve().parents[4]\nCSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"\n\n\ndef seeded(tmp_path: Path):\n    store = SQLiteStore(tmp_path / "accounting.sqlite3")\n    engine = FinanceEngine(store)\n    engine.reset_demo(CSV)\n    DailyCloseService(engine).run()\n    service = AccountingService(store)\n    service.ensure_default("ws_koru_studio", occurred_at="2026-08-27T00:00:00+00:00")\n    return store, engine, service\n\n\ndef test_gst_component_uses_signed_three_twenty_thirds_and_half_up_rounding() -> None:\n    assert gst_component_minor(11500) == 1500\n    assert gst_component_minor(-11500) == -1500\n    assert gst_component_minor(1) == 0\n    assert gst_component_minor(4) == 1\n\n\ndef test_default_export_does_not_invent_expense_gst(tmp_path: Path) -> None:\n    store, _engine, service = seeded(tmp_path)\n    service.configure_profile(\n        "ws_koru_studio", gst_registered=True, gst_basis="payments",\n        gst_number_masked="***-***-123", occurred_at="2026-08-27T00:01:00+00:00",\n    )\n    result = service.prepare_export(\n        "ws_koru_studio", start="2026-07-01", end="2026-07-31",\n        generated_at="2026-08-27T00:02:00+00:00",\n    )\n    assert result.transaction_count > 0\n    assert result.exception_count > 0\n    csv_artifact = service.get_artifact(result.csv_artifact_id)\n    rows = list(csv.DictReader(io.StringIO(csv_artifact.content.decode())))\n    expenses = [row for row in rows if int(row["gross_minor"]) < 0 and row["classification"] == "business"]\n    assert expenses\n    assert {row["tax_code"] for row in expenses} == {"UNRESOLVED"}\n    assert {int(row["gst_minor"]) for row in expenses} == {0}\n    exceptions = json.loads(service.get_artifact(result.exceptions_artifact_id).content)\n    assert exceptions["preparatory"] is True\n    assert len(exceptions["exceptions"]) == result.exception_count\n    assert len(store.fetch_all("SELECT * FROM accounting_exports")) == 2\n\n\ndef test_reviewed_gst_decision_changes_only_the_accounting_export(tmp_path: Path) -> None:\n    _store, engine, service = seeded(tmp_path)\n    service.configure_profile(\n        "ws_koru_studio", gst_registered=True, gst_basis="payments",\n        gst_number_masked=None, occurred_at="2026-08-27T00:01:00+00:00",\n    )\n    before = engine.get_snapshot()["totals"]\n    service.review_transaction(\n        "ws_koru_studio", "txn_koru_006", account_code="420", tax_code="GST",\n        reason="Reviewed tax invoice supplied by the owner.", request_id="review_mitre_gst",\n        occurred_at="2026-08-27T00:02:00+00:00",\n    )\n    result = service.prepare_export(\n        "ws_koru_studio", start="2026-07-01", end="2026-07-31",\n        generated_at="2026-08-27T00:03:00+00:00",\n    )\n    rows = list(csv.DictReader(io.StringIO(service.get_artifact(result.csv_artifact_id).content.decode())))\n    mitre = next(row for row in rows if row["transaction_id"] == "txn_koru_006")\n    assert mitre["tax_code"] == "GST"\n    assert int(mitre["gst_minor"]) == gst_component_minor(int(mitre["gross_minor"]))\n    assert engine.get_snapshot()["totals"] == before\n\n\ndef test_locked_period_blocks_review_until_explicit_unlock(tmp_path: Path) -> None:\n    _store, _engine, service = seeded(tmp_path)\n    lock = service.lock_period(\n        "ws_koru_studio", start="2026-07-01", end="2026-07-31",\n        reason="Accountant reviewed July.", request_id="lock_july",\n        occurred_at="2026-08-27T00:01:00+00:00",\n    )\n    with pytest.raises(ValueError, match="locked accounting period"):\n        service.review_transaction(\n            "ws_koru_studio", "txn_koru_006", account_code="420", tax_code="GST",\n            reason="Late review", request_id="late_review",\n        )\n    service.unlock_period(\n        "ws_koru_studio", str(lock["lockId"]), request_id="unlock_july",\n        reason="Accountant requested correction.",\n        occurred_at="2026-08-27T00:02:00+00:00",\n    )\n    decision = service.review_transaction(\n        "ws_koru_studio", "txn_koru_006", account_code="420", tax_code="GST",\n        reason="Reviewed after unlock", request_id="review_after_unlock",\n    )\n    assert decision["taxCode"] == "GST"\n\n\ndef test_csv_escapes_spreadsheet_formula_prefixes(tmp_path: Path) -> None:\n    store, _engine, service = seeded(tmp_path)\n    with store.transaction() as connection:\n        connection.execute(\n            "UPDATE transactions SET description = '=HYPERLINK(\"https://bad\")' WHERE transaction_id = 'txn_koru_006'"\n        )\n    result = service.prepare_export(\n        "ws_koru_studio", start="2026-07-01", end="2026-07-31"\n    )\n    rows = list(csv.DictReader(io.StringIO(service.get_artifact(result.csv_artifact_id).content.decode())))\n    mitre = next(row for row in rows if row["transaction_id"] == "txn_koru_006")\n    assert mitre["description"].startswith("'=")\n''',
    )


def main() -> None:
    patch_migrations()
    create_accounting_module()
    patch_route_protocol()
    patch_routes()
    patch_services()
    patch_electron_artifacts()
    add_docs()
    add_tests()


if __name__ == "__main__":
    main()
