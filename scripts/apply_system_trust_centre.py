from __future__ import annotations

import ast
import json
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
        name="integrity_check_receipts",
        sql="""
        CREATE TABLE integrity_check_receipts (
            receipt_id TEXT PRIMARY KEY,
            overall_status TEXT NOT NULL CHECK (
                overall_status IN ('passed', 'warning', 'failed')
            ),
            checks_json TEXT NOT NULL,
            checks_hash TEXT NOT NULL CHECK (length(checks_hash) = 64),
            passed_count INTEGER NOT NULL CHECK (passed_count >= 0),
            warning_count INTEGER NOT NULL CHECK (warning_count >= 0),
            failed_count INTEGER NOT NULL CHECK (failed_count >= 0),
            created_at TEXT NOT NULL
        );

        CREATE INDEX integrity_receipts_time
            ON integrity_check_receipts(created_at DESC);
        """,
    ),
'''

MODULE = '''"""Redacted local integrity checks for finance data, evidence and recovery state."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json

SECRET_ENV_NAMES = (
    "OPENAI_API_KEY",
    "AKAHU_APP_TOKEN",
    "AKAHU_USER_TOKEN",
    "PLAID_CLIENT_ID",
    "PLAID_SECRET",
    "PLAID_ACCESS_TOKEN",
    "TELEGRAM_BOT_TOKEN",
)


@dataclass(frozen=True, slots=True)
class IntegrityCheck:
    check_id: str
    title: str
    status: str
    detail: str
    inspected_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:24]}"


def _sha(value: bytes | str) -> str:
    data = value if isinstance(value, bytes) else value.encode()
    return hashlib.sha256(data).hexdigest()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class IntegrityDiagnostics:
    def __init__(
        self,
        store: SQLiteStore,
        *,
        restore_root: str | Path,
    ) -> None:
        self.store = store
        self.restore_root = Path(restore_root)

    def _table_exists(self, name: str) -> bool:
        return self.store.fetch_one(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ) is not None

    def _sqlite_checks(self) -> list[IntegrityCheck]:
        checks: list[IntegrityCheck] = []
        with self.store.connect() as connection:
            quick = connection.execute("PRAGMA quick_check").fetchone()
            checks.append(
                IntegrityCheck(
                    "sqlite_quick_check",
                    "SQLite page and index integrity",
                    "passed" if quick and str(quick[0]).lower() == "ok" else "failed",
                    "SQLite quick_check returned ok." if quick and str(quick[0]).lower() == "ok" else "SQLite quick_check did not return ok.",
                    1,
                )
            )
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            checks.append(
                IntegrityCheck(
                    "sqlite_foreign_keys",
                    "Foreign-key integrity",
                    "passed" if not foreign else "failed",
                    "No foreign-key violations were found." if not foreign else f"{len(foreign)} foreign-key violation(s) were found.",
                    len(foreign),
                )
            )
        return checks

    def _permission_checks(self) -> list[IntegrityCheck]:
        if self.store.database_path == ":memory:":
            return [
                IntegrityCheck(
                    "database_permissions", "Database file permissions", "warning",
                    "The database is in memory, so filesystem permissions do not apply.", 0,
                )
            ]
        paths = [Path(self.store.database_path)]
        for suffix in ("-wal", "-shm"):
            candidate = Path(f"{self.store.database_path}{suffix}")
            if candidate.exists():
                paths.append(candidate)
        unsafe = [path.name for path in paths if path.exists() and path.stat().st_mode & 0o077]
        return [
            IntegrityCheck(
                "database_permissions",
                "Database file permissions",
                "passed" if not unsafe else "warning",
                "Database files are owner-only." if not unsafe else "Some database sidecar files are readable outside the owner account: " + ", ".join(unsafe),
                len(paths),
            )
        ]

    def _trigger_checks(self) -> list[IntegrityCheck]:
        required = {
            "source_rows_no_update",
            "source_rows_no_delete",
            "finance_events_no_delete",
        }
        rows = self.store.fetch_all(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        )
        present = {str(row["name"]) for row in rows}
        missing = sorted(required - present)
        return [
            IntegrityCheck(
                "immutability_triggers",
                "Immutable source and event triggers",
                "passed" if not missing else "failed",
                "Required immutability triggers are installed." if not missing else "Missing trigger(s): " + ", ".join(missing),
                len(required),
            )
        ]

    def _source_row_checks(self) -> list[IntegrityCheck]:
        rows = self.store.fetch_all(
            """
            SELECT r.row_number, r.raw_json, r.row_hash, s.digest
            FROM source_rows r JOIN source_items s ON s.source_item_id = r.source_item_id
            ORDER BY r.source_item_id, r.row_number
            """
        )
        mismatches = 0
        for row in rows:
            expected = _sha(
                f"{row['digest']}\0{row['row_number']}\0{row['raw_json']}"
            )
            mismatches += expected != str(row["row_hash"])
        return [
            IntegrityCheck(
                "source_row_hashes",
                "Immutable source-row hashes",
                "passed" if mismatches == 0 else "failed",
                "All source-row hashes match their committed source digest and raw row." if mismatches == 0 else f"{mismatches} source-row hash mismatch(es) were found.",
                len(rows),
            )
        ]

    def _artifact_checks(self) -> list[IntegrityCheck]:
        if not self._table_exists("artifacts"):
            return []
        rows = self.store.fetch_all(
            "SELECT content, content_hash, dto_json, dto_hash FROM artifacts"
        )
        mismatches = sum(
            _sha(bytes(row["content"])) != str(row["content_hash"])
            or _sha(str(row["dto_json"])) != str(row["dto_hash"])
            for row in rows
        )
        return [
            IntegrityCheck(
                "artifact_hashes",
                "Generated artefact hashes",
                "passed" if mismatches == 0 else "failed",
                "All generated artefact and DTO hashes match." if mismatches == 0 else f"{mismatches} generated artefact hash mismatch(es) were found.",
                len(rows),
            )
        ]

    def _snapshot_checks(self) -> list[IntegrityCheck]:
        rows = self.store.fetch_all(
            "SELECT snapshot_json, content_hash FROM workspace_snapshots"
        )
        mismatches = sum(
            _sha(str(row["snapshot_json"])) != str(row["content_hash"])
            for row in rows
        )
        return [
            IntegrityCheck(
                "snapshot_hashes",
                "Workspace snapshot hashes",
                "passed" if mismatches == 0 else "failed",
                "All workspace snapshot hashes match." if mismatches == 0 else f"{mismatches} workspace snapshot hash mismatch(es) were found.",
                len(rows),
            )
        ]

    def _secret_checks(self) -> list[IntegrityCheck]:
        known = [
            value for name in SECRET_ENV_NAMES
            if (value := os.getenv(name)) and len(value) >= 8
        ]
        if not known:
            return [
                IntegrityCheck(
                    "known_secret_values",
                    "Known process secrets in persisted fields",
                    "passed",
                    "No configured secret value was available to compare with persisted text fields.",
                    0,
                )
            ]
        matches = 0
        columns_checked = 0
        with self.store.connect() as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
                )
                if not str(row[0]).startswith("knowledge_fts")
            ]
            for table in tables:
                for column in connection.execute(f"PRAGMA table_info({_quote(table)})"):
                    name = str(column[1])
                    kind = str(column[2]).upper()
                    if not any(marker in kind for marker in ("TEXT", "CHAR", "CLOB")):
                        continue
                    columns_checked += 1
                    for secret in known:
                        row = connection.execute(
                            f"SELECT 1 FROM {_quote(table)} WHERE instr(COALESCE({_quote(name)}, ''), ?) > 0 LIMIT 1",
                            (secret,),
                        ).fetchone()
                        if row is not None:
                            matches += 1
                            break
        return [
            IntegrityCheck(
                "known_secret_values",
                "Known process secrets in persisted fields",
                "passed" if matches == 0 else "failed",
                "No configured secret value appears in persisted text fields." if matches == 0 else f"A configured secret value appeared in {matches} persisted field location(s); values and locations are deliberately redacted.",
                columns_checked,
            )
        ]

    def _restore_candidate_checks(self) -> list[IntegrityCheck]:
        if not self.restore_root.exists():
            return [
                IntegrityCheck(
                    "restore_candidates",
                    "Restore candidate integrity",
                    "passed",
                    "No portable restore candidates are present.",
                    0,
                )
            ]
        failures = 0
        candidates = list(self.restore_root.glob("*.sqlite3"))
        for candidate in candidates:
            try:
                connection = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
                try:
                    row = connection.execute("PRAGMA quick_check").fetchone()
                    if row is None or str(row[0]).lower() != "ok":
                        failures += 1
                finally:
                    connection.close()
                if candidate.stat().st_mode & 0o077:
                    failures += 1
            except sqlite3.Error:
                failures += 1
        return [
            IntegrityCheck(
                "restore_candidates",
                "Restore candidate integrity",
                "passed" if failures == 0 else "failed",
                "All restore candidates pass quick_check and are owner-only." if failures == 0 else f"{failures} restore candidate integrity or permission failure(s) were found.",
                len(candidates),
            )
        ]

    def run(self) -> dict[str, object]:
        checks = [
            *self._sqlite_checks(),
            *self._permission_checks(),
            *self._trigger_checks(),
            *self._source_row_checks(),
            *self._artifact_checks(),
            *self._snapshot_checks(),
            *self._secret_checks(),
            *self._restore_candidate_checks(),
        ]
        failed = sum(check.status == "failed" for check in checks)
        warnings = sum(check.status == "warning" for check in checks)
        passed = sum(check.status == "passed" for check in checks)
        overall = "failed" if failed else "warning" if warnings else "passed"
        now = datetime.now(UTC).isoformat()
        payload = [check.as_dict() for check in checks]
        checks_hash = _sha(canonical_json(payload))
        receipt_id = _stable_id("integrityrcpt", now, checks_hash)
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO integrity_check_receipts(
                    receipt_id, overall_status, checks_json, checks_hash,
                    passed_count, warning_count, failed_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    overall,
                    canonical_json(payload),
                    checks_hash,
                    passed,
                    warnings,
                    failed,
                    now,
                ),
            )
        return {
            "integrityVersion": "folio.integrity-check@1",
            "receiptId": receipt_id,
            "overallStatus": overall,
            "passedCount": passed,
            "warningCount": warnings,
            "failedCount": failed,
            "checksHash": checks_hash,
            "checks": payload,
            "occurredAt": now,
            "externalCallsMade": False,
        }
'''

SERVICE_METHODS = '''    async def system_integrity_check(self) -> Mapping[str, object]:
        async with self._lock:
            return IntegrityDiagnostics(
                self.store,
                restore_root=ROOT / "var" / "restore-candidates",
            ).run()

    async def system_trust_summary(self) -> Mapping[str, object]:
        database = Path(self.store.database_path) if self.store.database_path != ":memory:" else None
        migration_receipt_path = (
            database.parent / ".migration-backups" / "last-migration-receipt.json"
            if database else None
        )
        migration_receipt: Mapping[str, object] | None = None
        if migration_receipt_path and migration_receipt_path.exists():
            try:
                value = json.loads(migration_receipt_path.read_text(encoding="utf-8"))
                if isinstance(value, Mapping):
                    migration_receipt = value
            except (OSError, json.JSONDecodeError):
                migration_receipt = None
        def count(table: str) -> int:
            exists = self.store.fetch_one(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            )
            if exists is None:
                return 0
            row = self.store.fetch_one(f'SELECT COUNT(*) AS count FROM "{table}"')
            return int(row["count"]) if row else 0
        integrity = self.store.fetch_one(
            "SELECT * FROM integrity_check_receipts ORDER BY created_at DESC LIMIT 1"
        )
        model, connections = await asyncio.gather(
            self.model_capabilities(),
            self.connection_capabilities(),
        )
        backup_root = database.parent / ".migration-backups" if database else None
        return {
            "trustSummaryVersion": "folio.trust-summary@1",
            "database": {
                "filename": database.name if database else ":memory:",
                "sizeBytes": database.stat().st_size if database and database.exists() else 0,
                "ownerOnly": (
                    not bool(database.stat().st_mode & 0o077)
                    if database and database.exists() else None
                ),
            },
            "migration": {
                "latestReceipt": migration_receipt,
                "backupCount": (
                    len(list(backup_root.glob("*.sqlite3")))
                    if backup_root and backup_root.exists() else 0
                ),
            },
            "recovery": {
                "portableExportReceiptCount": count("portable_bundle_receipts"),
                "restoreCandidateCount": count("portable_restore_candidates"),
            },
            "privacy": {
                "egressReceiptCount": count("egress_receipts"),
                "modelRunReceiptCount": count("model_runs"),
                "supportBundleReceiptCount": count("support_bundle_receipts"),
            },
            "latestIntegrity": (
                {
                    "receiptId": str(integrity["receipt_id"]),
                    "overallStatus": str(integrity["overall_status"]),
                    "passedCount": int(integrity["passed_count"]),
                    "warningCount": int(integrity["warning_count"]),
                    "failedCount": int(integrity["failed_count"]),
                    "checksHash": str(integrity["checks_hash"]),
                    "createdAt": str(integrity["created_at"]),
                }
                if integrity else None
            ),
            "models": model,
            "connections": connections,
            "externalCallsMade": False,
        }
'''

ROUTES = '''    @router.get("/v1/system/trust-summary")
    async def system_trust_summary(services: Services) -> dict[str, object]:
        return dict(await services.system_trust_summary())

    @router.post("/v1/system/integrity-check")
    async def system_integrity_check(services: Services) -> dict[str, object]:
        return dict(await services.system_integrity_check())

'''

TRUST_TS = '''import { requestJson } from "./transport";

export type IntegrityCheck = {
  check_id: string;
  title: string;
  status: "passed" | "warning" | "failed";
  detail: string;
  inspected_count: number;
};

export type IntegrityResult = {
  integrityVersion: "folio.integrity-check@1";
  receiptId: string;
  overallStatus: "passed" | "warning" | "failed";
  passedCount: number;
  warningCount: number;
  failedCount: number;
  checksHash: string;
  checks: IntegrityCheck[];
  occurredAt: string;
  externalCallsMade: false;
};

export type TrustSummary = {
  trustSummaryVersion: "folio.trust-summary@1";
  database: { filename: string; sizeBytes: number; ownerOnly: boolean | null };
  migration: { latestReceipt: Record<string, unknown> | null; backupCount: number };
  recovery: { portableExportReceiptCount: number; restoreCandidateCount: number };
  privacy: { egressReceiptCount: number; modelRunReceiptCount: number; supportBundleReceiptCount: number };
  latestIntegrity: Record<string, unknown> | null;
  models: Record<string, unknown>;
  connections: Record<string, unknown>;
  externalCallsMade: false;
};

export const loadTrustSummary = () =>
  requestJson<TrustSummary>("/v1/system/trust-summary", undefined, 15_000);

export const runIntegrityCheck = () =>
  requestJson<IntegrityResult>("/v1/system/integrity-check", {
    method: "POST",
    body: JSON.stringify({}),
  }, 60_000);
'''

COMPONENT = '''import { useCallback, useEffect, useRef, useState } from "react";
import { loadTrustSummary, runIntegrityCheck, type IntegrityResult, type TrustSummary } from "./trust";
import "./trust.css";

const value = (input: unknown) => typeof input === "string" ? input : "Unavailable";
const count = (input: unknown) => typeof input === "number" ? input : 0;

export function TrustCentre() {
  const [open, setOpen] = useState(false);
  const [summary, setSummary] = useState<TrustSummary | null>(null);
  const [integrity, setIntegrity] = useState<IntegrityResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("Trust status has not been refreshed in this window.");
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      const next = await loadTrustSummary();
      setSummary(next);
      setMessage("Trust status refreshed from local receipts and configuration.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Trust status could not be loaded.");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === "t") {
        event.preventDefault();
        setOpen((current) => !current);
      }
    };
    window.addEventListener("keydown", keyboard);
    return () => window.removeEventListener("keydown", keyboard);
  }, []);

  useEffect(() => {
    if (!open) return;
    void refresh();
    window.setTimeout(() => closeRef.current?.focus(), 0);
    const trap = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        return;
      }
      if (event.key !== "Tab") return;
      const elements = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
        "button:not(:disabled), [href], [tabindex]:not([tabindex='-1'])",
      ) ?? []);
      const first = elements.at(0);
      const last = elements.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", trap);
    return () => document.removeEventListener("keydown", trap);
  }, [open, refresh]);

  const check = async () => {
    setBusy(true);
    setMessage("Integrity checks are running against the local database and recovery files.");
    try {
      const result = await runIntegrityCheck();
      setIntegrity(result);
      await refresh();
      setMessage(`Integrity check ${result.overallStatus}: ${result.failedCount} failed and ${result.warningCount} warning.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Integrity check did not complete.");
      setBusy(false);
    }
  };

  const local = ((summary?.models.modes as Record<string, unknown> | undefined)?.local ?? {}) as Record<string, unknown>;
  const providers = ((summary?.connections.providers as Record<string, unknown> | undefined) ?? {}) as Record<string, unknown>;

  return <>
    <button className="trust-launcher" type="button" aria-haspopup="dialog" aria-expanded={open} onClick={() => setOpen(true)}>Trust centre <span aria-hidden="true">⌘⇧T</span></button>
    {open ? <div className="trust-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) setOpen(false); }}>
      <div ref={dialogRef} className="trust-dialog" role="dialog" aria-modal="true" aria-labelledby="trust-title">
        <header><div><p>Receipts, recovery and local boundaries</p><h1 id="trust-title">Trust centre</h1></div><button ref={closeRef} type="button" onClick={() => setOpen(false)} aria-label="Close Trust centre">Close</button></header>
        <div className="trust-status" role="status" aria-live="polite"><i className={busy ? "is-busy" : ""} />{message}<button type="button" disabled={busy} onClick={() => void refresh()}>Refresh</button></div>
        <main>
          <section className="trust-hero"><div><span>Latest integrity</span><strong className={`status-${integrity?.overallStatus ?? value(summary?.latestIntegrity?.overallStatus).toLowerCase()}`}>{integrity?.overallStatus ?? value(summary?.latestIntegrity?.overallStatus)}</strong></div><button type="button" disabled={busy} onClick={() => void check()}>Run local integrity check</button></section>
          <div className="trust-grid">
            <article><h2>Local data</h2><dl><div><dt>Database</dt><dd>{summary?.database.filename ?? "Unavailable"}</dd></div><div><dt>Size</dt><dd>{count(summary?.database.sizeBytes).toLocaleString("en-NZ")} bytes</dd></div><div><dt>Owner-only</dt><dd>{summary?.database.ownerOnly === true ? "Yes" : summary?.database.ownerOnly === false ? "Needs attention" : "Not applicable"}</dd></div><div><dt>Migration backups</dt><dd>{count(summary?.migration.backupCount)}</dd></div><div><dt>Restore candidates</dt><dd>{count(summary?.recovery.restoreCandidateCount)}</dd></div></dl></article>
            <article><h2>Models and egress</h2><dl><div><dt>Local model</dt><dd>{value(local.status)}</dd></div><div><dt>Measured tier</dt><dd>{local.tierMeasured ? String(local.tier) : "Not measured"}</dd></div><div><dt>Model receipts</dt><dd>{count(summary?.privacy.modelRunReceiptCount)}</dd></div><div><dt>Egress receipts</dt><dd>{count(summary?.privacy.egressReceiptCount)}</dd></div><div><dt>External calls in summary</dt><dd>{summary?.externalCallsMade ? "Yes" : "No"}</dd></div></dl></article>
            <article><h2>Connections</h2><ul>{Object.entries(providers).map(([name, raw]) => { const provider = raw as Record<string, unknown>; return <li key={name}><strong>{name}</strong><span>{value(provider.status)} · {value(provider.mode)}</span></li>; })}</ul></article>
            <article><h2>Recovery and support</h2><dl><div><dt>Portable exports</dt><dd>{count(summary?.recovery.portableExportReceiptCount)}</dd></div><div><dt>Support bundle receipts</dt><dd>{count(summary?.privacy.supportBundleReceiptCount)}</dd></div><div><dt>Automatic restore</dt><dd>Disabled</dd></div><div><dt>Portable export route</dt><dd><code>/v1/system/portable-export</code></dd></div></dl></article>
          </div>
          <section><h2>Integrity checks</h2>{integrity ? <div className="trust-checks">{integrity.checks.map((item) => <article key={item.check_id} className={`status-${item.status}`}><div><strong>{item.title}</strong><span>{item.inspected_count} inspected</span></div><p>{item.detail}</p><b>{item.status}</b></article>)}</div> : <p className="trust-empty">Run the check to inspect the current database, evidence hashes, restore candidates and known secret values.</p>}</section>
          <footer><p>Trust receipts describe what this local process checked. They do not prove external bank, accountant, tax authority, release-signing or penetration-test acceptance.</p></footer>
        </main>
      </div>
    </div> : null}
  </>;
}
'''

CSS = '''.trust-launcher{position:fixed;right:18px;bottom:66px;z-index:45;border:1px solid var(--line,#384039);background:var(--surface,#151917);color:inherit;border-radius:10px;padding:10px 13px;font:inherit;box-shadow:0 8px 28px rgba(0,0,0,.25)}.trust-launcher span{margin-left:8px;opacity:.55;font-size:12px}.trust-backdrop{position:fixed;inset:0;z-index:95;background:rgba(4,7,5,.76);display:grid;place-items:center;padding:18px}.trust-dialog{width:min(1040px,100%);height:min(800px,100%);background:var(--surface,#111512);color:var(--text,#edf2ee);border:1px solid var(--line,#313a33);border-radius:16px;display:grid;grid-template-rows:auto auto 1fr;overflow:hidden;box-shadow:0 30px 90px rgba(0,0,0,.5)}.trust-dialog>header{display:flex;justify-content:space-between;gap:20px;padding:21px 24px;border-bottom:1px solid var(--line,#313a33)}.trust-dialog>header p{margin:0;color:var(--muted,#a9b2ab);font-size:12px;text-transform:uppercase;letter-spacing:.1em}.trust-dialog>header h1{margin:4px 0 0;font-size:26px}.trust-dialog button{font:inherit;border:1px solid var(--line,#384039);background:rgba(255,255,255,.08);color:inherit;border-radius:8px;padding:8px 10px}.trust-status{display:flex;align-items:center;gap:9px;padding:9px 16px;background:rgba(255,255,255,.035);font-size:13px;color:var(--muted,#a9b2ab)}.trust-status button{margin-left:auto}.trust-status i{width:8px;height:8px;border-radius:50%;background:#55a56b}.trust-status i.is-busy{animation:trust-pulse 1s infinite}.trust-dialog main{overflow:auto;padding:22px 24px}.trust-hero{display:flex;justify-content:space-between;align-items:center;gap:18px;border:1px solid var(--line,#313a33);border-radius:13px;padding:16px}.trust-hero span{display:block;color:var(--muted,#a9b2ab);font-size:12px}.trust-hero strong{display:block;font-size:24px;text-transform:capitalize;margin-top:4px}.trust-grid{display:grid;grid-template-columns:1fr 1fr;gap:11px;margin:12px 0 24px}.trust-grid article,.trust-checks article{border:1px solid var(--line,#313a33);border-radius:12px;padding:14px;background:rgba(255,255,255,.025)}.trust-grid h2,.trust-dialog main>section>h2{font-size:16px;margin:0 0 12px}.trust-grid dl{margin:0}.trust-grid dl>div{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px solid var(--line,#313a33)}.trust-grid dl>div:last-child{border-bottom:0}.trust-grid dt{color:var(--muted,#a9b2ab)}.trust-grid dd{margin:0;text-align:right}.trust-grid ul{list-style:none;padding:0;margin:0}.trust-grid li{display:flex;justify-content:space-between;gap:10px;padding:8px 0;border-bottom:1px solid var(--line,#313a33)}.trust-grid li span{color:var(--muted,#a9b2ab)}.trust-checks{display:grid;gap:8px}.trust-checks article{display:grid;grid-template-columns:1fr auto;gap:4px 12px}.trust-checks article div{display:flex;gap:10px}.trust-checks article span{color:var(--muted,#a9b2ab);font-size:12px}.trust-checks article p{margin:2px 0 0;color:var(--muted,#a9b2ab);grid-column:1}.trust-checks article b{text-transform:capitalize;grid-column:2;grid-row:1/3}.status-passed{color:#7bd18b}.status-warning{color:#e1b55b}.status-failed{color:#ef816f}.trust-empty,footer p{color:var(--muted,#a9b2ab)}.trust-dialog footer{border-top:1px solid var(--line,#313a33);margin-top:22px;padding-top:14px;font-size:12px}.trust-dialog code{word-break:break-all}button:focus-visible{outline:2px solid currentColor;outline-offset:2px}@keyframes trust-pulse{50%{opacity:.25;transform:scale(.8)}}@media(max-width:720px){.trust-backdrop{padding:0}.trust-dialog{height:100%;border-radius:0;border:0}.trust-dialog main{padding:16px}.trust-grid{grid-template-columns:1fr}.trust-launcher{right:10px;bottom:58px}.trust-hero{align-items:flex-start;flex-direction:column}}@media(prefers-reduced-motion:reduce){.trust-status i.is-busy{animation:none}}'''

NODE_TEST = '''import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const component = await readFile(new URL("../src/TrustCentre.tsx", import.meta.url), "utf8");
const client = await readFile(new URL("../src/trust.ts", import.meta.url), "utf8");

test("trust centre contains a modal focus boundary and redacted proof language", () => {
  assert.match(component, /role="dialog"/);
  assert.match(component, /aria-modal="true"/);
  assert.match(component, /event\.key === "Escape"/);
  assert.match(component, /event\.key !== "Tab"/);
  assert.match(component, /do not prove external bank/);
  assert.doesNotMatch(component, /dangerouslySetInnerHTML/);
});

test("trust client uses only local trust and integrity routes", () => {
  assert.match(client, /\/v1\/system\/trust-summary/);
  assert.match(client, /\/v1\/system\/integrity-check/);
  assert.doesNotMatch(client, /https?:\/\//);
});
'''

PYTHON_TEST = '''from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from finance_agent.api.services import LocalRouteServices
from finance_agent.storage.integrity import IntegrityDiagnostics


@pytest.mark.asyncio
async def test_integrity_check_receipts_redacted_local_state(tmp_path: Path) -> None:
    services = LocalRouteServices(tmp_path / "folio.sqlite3", auto_seed=True)
    try:
        value = await services.system_integrity_check()
        assert value["integrityVersion"] == "folio.integrity-check@1"
        assert value["externalCallsMade"] is False
        assert value["failedCount"] == 0
        assert len(value["checksHash"]) == 64
        encoded = json.dumps(value).lower()
        assert "openai_api_key" not in encoded
        assert "akahu_user_token" not in encoded
        row = services.store.fetch_one(
            "SELECT checks_hash FROM integrity_check_receipts WHERE receipt_id = ?",
            (value["receiptId"],),
        )
        assert str(row["checks_hash"]) == value["checksHash"]
    finally:
        await services.aclose()


@pytest.mark.asyncio
async def test_trust_summary_uses_basenames_and_receipt_counts(tmp_path: Path) -> None:
    services = LocalRouteServices(tmp_path / "nested" / "folio.sqlite3", auto_seed=True)
    try:
        await services.system_integrity_check()
        value = await services.system_trust_summary()
        assert value["database"]["filename"] == "folio.sqlite3"
        assert "/" not in value["database"]["filename"]
        assert value["latestIntegrity"]["receiptId"]
        assert value["externalCallsMade"] is False
    finally:
        await services.aclose()


def test_known_secret_value_is_detected_without_returning_value(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from finance_agent.finance import FinanceEngine
    from finance_agent.storage import SQLiteStore

    store = SQLiteStore(tmp_path / "folio.sqlite3")
    root = Path(__file__).resolve().parents[4]
    FinanceEngine(store).reset_demo(root / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv")
    secret = "sk-test-super-secret-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE workspaces SET name = ? WHERE workspace_id = 'ws_koru_studio'",
            (secret,),
        )
    value = IntegrityDiagnostics(
        store, restore_root=tmp_path / "restore-candidates"
    ).run()
    check = next(item for item in value["checks"] if item["check_id"] == "known_secret_values")
    assert check["status"] == "failed"
    assert secret not in json.dumps(value)
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
    write("services/api/src/finance_agent/storage/integrity.py", MODULE)


def update_backend() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.storage.portable_bundle import PortableWorkspaceBundleService\n"
    import_line = "from finance_agent.storage.integrity import IntegrityDiagnostics\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("portable bundle import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "operations_summary", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def operations_summary(\n"
    addition = '''    async def system_integrity_check(self) -> Mapping[str, object]: ...\n\n    async def system_trust_summary(self) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("operations summary protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    marker = '    @router.get("/v1/workspaces/{workspace_id}/operations-summary")\n'
    if marker not in content:
        raise RuntimeError("operations summary route marker missing")
    content = content.replace(marker, ROUTES + marker, 1)
    write(path, content)


def update_frontend_tests_docs() -> None:
    write("apps/desktop/src/trust.ts", TRUST_TS)
    write("apps/desktop/src/TrustCentre.tsx", COMPONENT)
    write("apps/desktop/src/trust.css", CSS)
    path = "apps/desktop/src/main.tsx"
    content = read(path)
    if 'import { TrustCentre } from "./TrustCentre";' not in content:
        content = content.replace(
            'import { OperationsWorkbench } from "./OperationsWorkbench";\n',
            'import { OperationsWorkbench } from "./OperationsWorkbench";\nimport { TrustCentre } from "./TrustCentre";\n',
            1,
        )
    content = content.replace(
        "      <OperationsWorkbench />\n",
        "      <OperationsWorkbench />\n      <TrustCentre />\n",
        1,
    )
    write(path, content)
    write("apps/desktop/tests/trust-centre.test.mjs", NODE_TEST)
    write("services/api/tests/storage/test_integrity_diagnostics.py", PYTHON_TEST)
    package_path = ROOT / "package.json"
    package = json.loads(package_path.read_text())
    scripts = package.setdefault("scripts", {})
    scripts["test:trust-centre"] = "node --test apps/desktop/tests/trust-centre.test.mjs"
    verify = scripts.get("verify", "")
    if "pnpm test:trust-centre" not in verify:
        scripts["verify"] = verify + " && pnpm test:trust-centre"
    package_path.write_text(json.dumps(package, indent=2) + "\n")
    write("docs/TRUST_CENTRE.md", '''# Trust centre and local integrity diagnostics\n\nThe Trust centre is a separate deliberate dialog opened with `Cmd/Ctrl+Shift+T`. It shows only redacted local state: database filename/size/permissions, migration backup and restore-candidate counts, model and connector capability state, model/egress/support receipt counts and the latest integrity receipt. It never returns a raw database path, credential or secret value.\n\nThe integrity check runs SQLite quick and foreign-key checks, required immutability-trigger checks, deterministic source-row hash verification, generated artefact and snapshot hash verification, database/restore-candidate permission checks and a comparison of configured process secret values against persisted text fields. A secret match reports only a count and fails the check; it never returns the value or location. Results are hashed and receipted locally.\n\nA passed self-test is evidence for these local invariants at one point in time. It does not prove external provider behaviour, accountant or tax-authority acceptance, package signing, release installation or an independent penetration test.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 48: trust centre and redacted integrity self-test\n\n- SQLite, foreign keys, immutability triggers and deterministic hashes are checked locally.\n- Database and restore-candidate permissions are inspected.\n- Configured secret values are compared with persisted text without revealing values or locations.\n- Results are hashed, receipted and shown with backup, egress, model and connector state.\n- The desktop dialog contains focus, keyboard, mobile and reduced-motion boundaries.\n- A pass is point-in-time local evidence, not external acceptance or penetration-test proof.\n'''
    if "## Stack 48: trust centre and redacted integrity self-test" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    update_backend()
    update_frontend_tests_docs()
    print("system trust centre changes applied")


if __name__ == "__main__":
    main()
