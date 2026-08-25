from __future__ import annotations

import ast
import json
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


SERVICE_METHOD = '''    async def operations_summary(
        self, *, workspace_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        workspace = self.store.fetch_one(
            "SELECT data_through, currency, timezone FROM workspaces WHERE workspace_id = ?",
            (workspace_id,),
        )
        if workspace is None:
            raise KeyError(workspace_id)
        accounts = [
            {
                "accountId": str(row["account_id"]),
                "name": str(row["name"]),
                "currency": str(row["currency"]),
            }
            for row in self.store.fetch_all(
                "SELECT account_id, name, currency FROM accounts WHERE workspace_id = ? ORDER BY name, account_id",
                (workspace_id,),
            )
        ]
        sources = [
            {
                "sourceItemId": str(row["source_item_id"]),
                "sourceType": str(row["source_type"]),
                "label": str(row["label"]),
                "status": str(row["status"]),
                "rowCount": int(row["row_count"]),
                "receivedAt": str(row["received_at"]),
            }
            for row in self.store.fetch_all(
                "SELECT source_item_id, source_type, label, status, row_count, received_at FROM source_items WHERE workspace_id = ? ORDER BY received_at DESC, source_item_id",
                (workspace_id,),
            )
        ]
        return {
            "summaryVersion": "folio.operations-summary@1",
            "workspaceId": workspace_id,
            "currency": str(workspace["currency"]),
            "timezone": str(workspace["timezone"]),
            "dataThrough": str(workspace["data_through"]),
            "accounts": accounts,
            "sources": sources,
            "periods": [
                value.as_dict()
                for value in AccountingPeriodService(self.store).latest(workspace_id)
            ],
            "rules": list(
                ClassificationRuleManagementService(
                    self.store, self.engine
                ).list_rules(workspace_id)
            ),
            "transferCandidates": list(
                InternalTransferService(self.store).list_candidates(workspace_id)
            ),
            "duplicateCandidates": list(
                DuplicateReviewService(self.store).list(workspace_id)
            ),
            "statementReconciliations": [
                value.as_dict()
                for value in StatementReconciliationService(self.store).list(workspace_id)
            ],
            "foreignCurrencyItems": list(
                ForeignCurrencyService(self.store).list_items(workspace_id)
            ),
            "accountingExportProfiles": [
                value.as_dict()
                for value in AccountingSystemExportService(self.store).list_profiles(
                    workspace_id
                )
            ],
            "analytics": DeterministicFinanceAnalytics(self.store).monthly(
                workspace_id=workspace_id,
                as_of=str(workspace["data_through"])[:10],
                months=6,
            ),
            "externalCallsMade": False,
        }
'''

ROUTE = '''    @router.get("/v1/workspaces/{workspace_id}/operations-summary")
    async def operations_summary(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        return dict(await services.operations_summary(workspace_id=workspace_id))

'''

OPERATIONS_TS = '''import { requestJson } from "./transport";

export const OPERATIONS_WORKSPACE_ID = "ws_koru_studio";

export type OperationsSummary = {
  summaryVersion: "folio.operations-summary@1";
  workspaceId: string;
  currency: string;
  timezone: string;
  dataThrough: string;
  accounts: Array<{ accountId: string; name: string; currency: string }>;
  sources: Array<{
    sourceItemId: string;
    sourceType: string;
    label: string;
    status: string;
    rowCount: number;
    receivedAt: string;
  }>;
  periods: Array<Record<string, unknown>>;
  rules: Array<Record<string, unknown>>;
  transferCandidates: Array<Record<string, unknown>>;
  duplicateCandidates: Array<Record<string, unknown>>;
  statementReconciliations: Array<Record<string, unknown>>;
  foreignCurrencyItems: Array<Record<string, unknown>>;
  accountingExportProfiles: Array<Record<string, unknown>>;
  analytics: Record<string, unknown>;
  externalCallsMade: false;
};

const path = (suffix: string) =>
  `/v1/workspaces/${encodeURIComponent(OPERATIONS_WORKSPACE_ID)}${suffix}`;

export function parseMinorInput(value: FormDataEntryValue | null): number {
  const text = String(value ?? "").trim();
  if (!/^-?\d+(?:\.\d{1,2})?$/.test(text)) {
    throw new Error("Use a dollar amount with no more than two decimal places.");
  }
  const negative = text.startsWith("-");
  const unsigned = negative ? text.slice(1) : text;
  const [whole, fraction = ""] = unsigned.split(".");
  const minor = Number.parseInt(whole, 10) * 100 + Number.parseInt(fraction.padEnd(2, "0"), 10);
  if (!Number.isSafeInteger(minor)) throw new Error("Amount is outside the safe range.");
  return negative ? -minor : minor;
}

export const loadOperationsSummary = () =>
  requestJson<OperationsSummary>(path("/operations-summary"), undefined, 12_000);

export const setAccountingPeriod = (body: Record<string, unknown>) =>
  requestJson<Record<string, unknown>>(path("/accounting-periods"), {
    method: "POST",
    body: JSON.stringify(body),
  }, 12_000);

export const previewClassificationRule = (body: Record<string, unknown>) =>
  requestJson<Record<string, unknown>>(path("/classification-rules/preview"), {
    method: "POST",
    body: JSON.stringify(body),
  }, 12_000);

export const deactivateClassificationRule = (ruleId: string) =>
  requestJson<Record<string, unknown>>(
    path(`/classification-rules/${encodeURIComponent(ruleId)}/deactivate`),
    {
      method: "POST",
      body: JSON.stringify({
        requestId: `undo_operations_${Date.now().toString(36)}`,
        reason: "Owner disabled this rule from Finance operations.",
      }),
    },
    20_000,
  );

export const scanTransfers = () =>
  requestJson<Record<string, unknown>>(path("/transfers/scan"), {
    method: "POST",
    body: JSON.stringify({}),
  }, 20_000);

export const confirmTransfer = (candidateId: string) =>
  requestJson<Record<string, unknown>>(
    path(`/transfers/candidates/${encodeURIComponent(candidateId)}/confirm`),
    {
      method: "POST",
      body: JSON.stringify({ reason: "Owner confirmed this internal transfer pair." }),
    },
    30_000,
  );

export const scanDuplicates = () =>
  requestJson<Record<string, unknown>>(path("/duplicates/scan"), {
    method: "POST",
    body: JSON.stringify({}),
  }, 20_000);

export const confirmDuplicate = (candidateId: string, keeperTransactionId: string) =>
  requestJson<Record<string, unknown>>(
    path(`/duplicates/candidates/${encodeURIComponent(candidateId)}/confirm`),
    {
      method: "POST",
      body: JSON.stringify({
        keeperTransactionId,
        reason: "Owner selected the authoritative transaction from Finance operations.",
      }),
    },
    30_000,
  );

export const rejectDuplicate = (candidateId: string) =>
  requestJson<Record<string, unknown>>(
    path(`/duplicates/candidates/${encodeURIComponent(candidateId)}/reject`),
    {
      method: "POST",
      body: JSON.stringify({ reason: "Owner confirmed these records are separate." }),
    },
    20_000,
  );

export const prepareStatementReconciliation = (body: Record<string, unknown>) =>
  requestJson<Record<string, unknown>>(path("/statement-reconciliations"), {
    method: "POST",
    body: JSON.stringify(body),
  }, 20_000);

export const decideStatementReconciliation = (
  reconciliationId: string,
  action: "confirm" | "acknowledge_discrepancy",
) => requestJson<Record<string, unknown>>(
  path(`/statement-reconciliations/${encodeURIComponent(reconciliationId)}/decide`),
  {
    method: "POST",
    body: JSON.stringify({
      action,
      actor: "owner",
      reason: action === "confirm"
        ? "Owner confirmed the statement balances from Finance operations."
        : "Owner acknowledged the remaining discrepancy for follow-up.",
    }),
  },
  20_000,
);

export const addFxRate = (body: Record<string, unknown>) =>
  requestJson<Record<string, unknown>>(path("/foreign-currency/rates"), {
    method: "POST",
    body: JSON.stringify(body),
  }, 20_000);

export const convertFxItem = (
  itemId: string,
  rateId: string,
  targetAccountId: string,
) => requestJson<Record<string, unknown>>(
  path(`/foreign-currency/items/${encodeURIComponent(itemId)}/convert`),
  {
    method: "POST",
    body: JSON.stringify({
      rateId,
      targetAccountId,
      reason: "Owner applied the documented FX rate from Finance operations.",
    }),
  },
  30_000,
);

export const saveAccountingExportProfile = (body: Record<string, unknown>) =>
  requestJson<Record<string, unknown>>(path("/accounting-exports/profiles"), {
    method: "POST",
    body: JSON.stringify(body),
  }, 20_000);

export const createAccountingExport = (body: Record<string, unknown>) =>
  requestJson<Record<string, unknown>>(path("/accounting-exports"), {
    method: "POST",
    body: JSON.stringify(body),
  }, 30_000);
'''

COMPONENT = '''import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  addFxRate,
  confirmDuplicate,
  confirmTransfer,
  createAccountingExport,
  deactivateClassificationRule,
  decideStatementReconciliation,
  loadOperationsSummary,
  parseMinorInput,
  prepareStatementReconciliation,
  previewClassificationRule,
  rejectDuplicate,
  saveAccountingExportProfile,
  scanDuplicates,
  scanTransfers,
  setAccountingPeriod,
  convertFxItem,
  type OperationsSummary,
} from "./operations";
import "./operations.css";

type Tab = "overview" | "rules" | "reconcile" | "accounting" | "fx";
const tabs: Array<{ id: Tab; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "rules", label: "Rules" },
  { id: "reconcile", label: "Reconcile" },
  { id: "accounting", label: "Accounting" },
  { id: "fx", label: "FX" },
];

const text = (value: unknown) => typeof value === "string" ? value : "";
const number = (value: unknown) => typeof value === "number" ? value : 0;
const list = (value: unknown) => Array.isArray(value) ? value : [];
const money = (minor: unknown, currency = "NZD") =>
  new Intl.NumberFormat("en-NZ", { style: "currency", currency }).format(number(minor) / 100);

function Field({ label, name, children }: { label: string; name?: string; children: React.ReactNode }) {
  return <label className="operations-field"><span>{label}</span>{children}</label>;
}

export function OperationsWorkbench() {
  const [open, setOpen] = useState(false);
  const [tab, setTab] = useState<Tab>("overview");
  const [summary, setSummary] = useState<OperationsSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("Open Finance operations to inspect committed local state.");
  const [rulePreview, setRulePreview] = useState<Record<string, unknown> | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);

  const refresh = useCallback(async () => {
    setBusy(true);
    try {
      setSummary(await loadOperationsSummary());
      setNotice("Finance operations refreshed from the local service.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Finance operations could not load.");
    } finally {
      setBusy(false);
    }
  }, []);

  const run = useCallback(async (label: string, work: () => Promise<unknown>) => {
    setBusy(true);
    setNotice(`${label} is running locally.`);
    try {
      await work();
      await refresh();
      setNotice(`${label} completed and the committed view was refreshed.`);
    } catch (error) {
      setNotice(error instanceof Error ? error.message : `${label} did not complete.`);
      setBusy(false);
    }
  }, [refresh]);

  useEffect(() => {
    const keyboard = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === "o") {
        event.preventDefault();
        setOpen((value) => !value);
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
      const dialog = dialogRef.current;
      if (!dialog) return;
      const values = Array.from(dialog.querySelectorAll<HTMLElement>(
        "button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex='-1'])",
      ));
      const first = values.at(0);
      const last = values.at(-1);
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

  const analyticsMonths = useMemo(() =>
    list(summary?.analytics.months) as Array<Record<string, unknown>>, [summary]);

  const submitPeriod = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void run("Accounting period update", () => setAccountingPeriod({
      periodStart: String(data.get("periodStart")),
      periodEnd: String(data.get("periodEnd")),
      status: String(data.get("status")),
      actor: "owner",
      reason: String(data.get("reason")),
    }));
  };

  const submitRule = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const value = await previewClassificationRule({
        merchantContains: String(data.get("merchantContains")),
        maximumAmountMinor: parseMinorInput(data.get("maximumAmount")),
        currency: "NZD",
        targetClassification: String(data.get("targetClassification")),
        targetCategory: String(data.get("targetCategory")) || null,
        effectiveFrom: String(data.get("effectiveFrom")),
        priority: Number.parseInt(String(data.get("priority")), 10),
      });
      setRulePreview(value);
      setNotice("Rule preview calculated without committing a change.");
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "Rule preview failed.");
    } finally {
      setBusy(false);
    }
  };

  const submitReconciliation = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void run("Statement reconciliation", () => prepareStatementReconciliation({
      accountId: String(data.get("accountId")),
      sourceItemId: String(data.get("sourceItemId")),
      periodStart: String(data.get("periodStart")),
      periodEnd: String(data.get("periodEnd")),
      openingBalanceMinor: parseMinorInput(data.get("openingBalance")),
      statedClosingBalanceMinor: parseMinorInput(data.get("closingBalance")),
      actor: "owner",
      reason: String(data.get("reason")),
    }));
  };

  const submitFxRate = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    void run("FX rate revision", () => addFxRate({
      baseCurrency: String(data.get("baseCurrency")).toUpperCase(),
      effectiveOn: String(data.get("effectiveOn")),
      rateNumerator: Number.parseInt(String(data.get("rateNumerator")), 10),
      rateDenominator: Number.parseInt(String(data.get("rateDenominator")), 10),
      sourceLabel: String(data.get("sourceLabel")),
      evidenceId: String(data.get("evidenceId")),
    }));
  };

  const submitExportProfile = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    try {
      const mapping = JSON.parse(String(data.get("categoryMapping"))) as Record<string, string>;
      void run("Accounting export profile", () => saveAccountingExportProfile({
        profileName: String(data.get("profileName")),
        exportFormat: String(data.get("exportFormat")),
        bankControlAccountCode: String(data.get("bankCode")),
        categoryMapping: mapping,
        defaultTaxCode: String(data.get("taxCode")),
      }));
    } catch {
      setNotice("Category mapping must be valid JSON with classification:category account-code pairs.");
    }
  };

  return (
    <>
      <button
        type="button"
        className="operations-launcher"
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => setOpen(true)}
      >
        Finance operations <span aria-hidden="true">⌘⇧O</span>
      </button>
      {open ? (
        <div className="operations-backdrop" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setOpen(false);
        }}>
          <div
            ref={dialogRef}
            className="operations-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="operations-title"
          >
            <header className="operations-header">
              <div>
                <p className="operations-eyebrow">Local bookkeeping controls</p>
                <h1 id="operations-title">Finance operations</h1>
                <p>Preview first, commit deliberately, keep the evidence and receipt.</p>
              </div>
              <button ref={closeRef} type="button" onClick={() => setOpen(false)} aria-label="Close Finance operations">Close</button>
            </header>
            <nav className="operations-tabs" role="tablist" aria-label="Finance operation areas">
              {tabs.map((item) => (
                <button
                  type="button"
                  role="tab"
                  aria-selected={tab === item.id}
                  aria-controls={`operations-panel-${item.id}`}
                  id={`operations-tab-${item.id}`}
                  key={item.id}
                  onClick={() => setTab(item.id)}
                >{item.label}</button>
              ))}
            </nav>
            <div className="operations-status" role="status" aria-live="polite">
              <span className={busy ? "is-busy" : ""} aria-hidden="true" />{notice}
              <button type="button" disabled={busy} onClick={() => void refresh()}>Refresh</button>
            </div>
            <main className="operations-body">
              {tab === "overview" ? (
                <section id="operations-panel-overview" role="tabpanel" aria-labelledby="operations-tab-overview">
                  <div className="operations-metrics">
                    <article><span>Accounts</span><strong>{summary?.accounts.length ?? 0}</strong></article>
                    <article><span>Sources</span><strong>{summary?.sources.length ?? 0}</strong></article>
                    <article><span>Rules</span><strong>{summary?.rules.length ?? 0}</strong></article>
                    <article><span>Open reviews</span><strong>{(summary?.transferCandidates.length ?? 0) + (summary?.duplicateCandidates.length ?? 0)}</strong></article>
                  </div>
                  <h2>Six-month operating view</h2>
                  <div className="operations-table-wrap"><table><thead><tr><th>Month</th><th>Inflows</th><th>Outflows</th><th>Net</th><th>Unresolved</th></tr></thead><tbody>
                    {analyticsMonths.map((month) => <tr key={text(month.month)}><td>{text(month.month)}</td><td>{money(month.operatingInflowMinor)}</td><td>{money(month.operatingOutflowMinor)}</td><td>{money(month.operatingNetMinor)}</td><td>{money(month.unresolvedExpenseMinor)}</td></tr>)}
                  </tbody></table></div>
                  <h2>Accounts and current sources</h2>
                  <div className="operations-columns"><ul>{summary?.accounts.map((account) => <li key={account.accountId}><strong>{account.name}</strong><span>{account.currency} · {account.accountId}</span></li>)}</ul><ul>{summary?.sources.slice(0, 8).map((source) => <li key={source.sourceItemId}><strong>{source.label}</strong><span>{source.status} · {source.rowCount} rows</span></li>)}</ul></div>
                </section>
              ) : null}

              {tab === "rules" ? (
                <section id="operations-panel-rules" role="tabpanel" aria-labelledby="operations-tab-rules">
                  <h2>Preview a classification rule</h2>
                  <form className="operations-form" onSubmit={(event) => void submitRule(event)}>
                    <Field label="Merchant contains"><input name="merchantContains" required maxLength={200} /></Field>
                    <Field label="Maximum amount (NZD)"><input name="maximumAmount" inputMode="decimal" defaultValue="500.00" required /></Field>
                    <Field label="Prepared as"><select name="targetClassification" defaultValue="business"><option value="business">Business</option><option value="personal">Personal</option><option value="unresolved">Unresolved</option></select></Field>
                    <Field label="Category"><input name="targetCategory" defaultValue="client_materials" /></Field>
                    <Field label="Effective from"><input name="effectiveFrom" type="date" required /></Field>
                    <Field label="Priority"><input name="priority" type="number" defaultValue="100" min="-10000" max="10000" /></Field>
                    <button type="submit" disabled={busy}>Preview only</button>
                  </form>
                  {rulePreview ? <article className="operations-receipt"><strong>Preview, not committed</strong><span>{number(rulePreview.matchCount)} matches · {number(rulePreview.changeCount)} changes · {number(rulePreview.conflictCount)} conflicts</span></article> : null}
                  <h2>Current rules</h2>
                  <div className="operations-cards">{summary?.rules.map((rule) => <article key={text(rule.ruleId)}><strong>{text(rule.merchantContains)}</strong><span>{text(rule.targetClassification)} · {text(rule.targetCategory) || "No category"}</span><span>{number(rule.currentMatchCount)} current matches</span>{rule.active ? <button type="button" disabled={busy} onClick={() => void run("Rule deactivation", () => deactivateClassificationRule(text(rule.ruleId)))}>Deactivate with Undo receipt</button> : <em>Inactive</em>}</article>)}</div>
                </section>
              ) : null}

              {tab === "reconcile" ? (
                <section id="operations-panel-reconcile" role="tabpanel" aria-labelledby="operations-tab-reconcile">
                  <div className="operations-actions"><button type="button" disabled={busy} onClick={() => void run("Transfer scan", scanTransfers)}>Scan internal transfers</button><button type="button" disabled={busy} onClick={() => void run("Duplicate scan", scanDuplicates)}>Scan duplicates</button></div>
                  <h2>Transfer candidates</h2>
                  <div className="operations-cards">{summary?.transferCandidates.map((candidate) => <article key={text(candidate.candidateId)}><strong>{money(candidate.amountMinor)}</strong><span>{text(candidate.debitDescription)} ↔ {text(candidate.creditDescription)}</span><span>Score {number(candidate.scoreBasisPoints) / 100}% · {text(candidate.status)}</span>{candidate.status === "proposed" ? <button type="button" disabled={busy} onClick={() => void run("Transfer confirmation", () => confirmTransfer(text(candidate.candidateId)))}>Confirm pair</button> : null}</article>)}</div>
                  <h2>Duplicate candidates</h2>
                  <div className="operations-cards">{summary?.duplicateCandidates.map((candidate) => { const a = candidate.transactionA as Record<string, unknown>; const b = candidate.transactionB as Record<string, unknown>; return <article key={text(candidate.candidateId)}><strong>{money(a?.amountMinor)}</strong><span>{text(a?.description)} / {text(b?.description)}</span><span>Score {number(candidate.scoreBasisPoints) / 100}% · {text(candidate.status)}</span>{candidate.status === "proposed" ? <div className="operations-inline-actions"><button type="button" disabled={busy} onClick={() => void run("Duplicate confirmation", () => confirmDuplicate(text(candidate.candidateId), text(a?.transactionId)))}>Keep first</button><button type="button" disabled={busy} onClick={() => void run("Duplicate rejection", () => rejectDuplicate(text(candidate.candidateId)))}>Not duplicates</button></div> : null}</article>; })}</div>
                  <h2>Prepare statement reconciliation</h2>
                  <form className="operations-form" onSubmit={submitReconciliation}>
                    <Field label="Account"><select name="accountId" required>{summary?.accounts.map((account) => <option key={account.accountId} value={account.accountId}>{account.name}</option>)}</select></Field>
                    <Field label="Statement source"><select name="sourceItemId" required>{summary?.sources.map((source) => <option key={source.sourceItemId} value={source.sourceItemId}>{source.label}</option>)}</select></Field>
                    <Field label="Period start"><input name="periodStart" type="date" required /></Field><Field label="Period end"><input name="periodEnd" type="date" required /></Field>
                    <Field label="Opening balance"><input name="openingBalance" inputMode="decimal" required /></Field><Field label="Closing balance"><input name="closingBalance" inputMode="decimal" required /></Field>
                    <Field label="Reason"><input name="reason" defaultValue="Compare the selected statement balances." required /></Field><button type="submit" disabled={busy}>Prepare comparison</button>
                  </form>
                  <div className="operations-cards">{summary?.statementReconciliations.map((item) => <article key={text(item.reconciliationId)}><strong>{text(item.periodStart)} to {text(item.periodEnd)}</strong><span>Discrepancy {money(item.discrepancyMinor)} · {text(item.status)}</span>{item.status === "draft" ? <div className="operations-inline-actions"><button type="button" disabled={busy || number(item.discrepancyMinor) !== 0} onClick={() => void run("Statement confirmation", () => decideStatementReconciliation(text(item.reconciliationId), "confirm"))}>Confirm exact</button><button type="button" disabled={busy || number(item.discrepancyMinor) === 0} onClick={() => void run("Discrepancy acknowledgement", () => decideStatementReconciliation(text(item.reconciliationId), "acknowledge_discrepancy"))}>Keep discrepancy open</button></div> : null}</article>)}</div>
                </section>
              ) : null}

              {tab === "accounting" ? (
                <section id="operations-panel-accounting" role="tabpanel" aria-labelledby="operations-tab-accounting">
                  <h2>Accounting period status</h2>
                  <form className="operations-form" onSubmit={submitPeriod}><Field label="Period start"><input name="periodStart" type="date" required /></Field><Field label="Period end"><input name="periodEnd" type="date" required /></Field><Field label="Status"><select name="status"><option value="open">Open</option><option value="soft_locked">Soft lock</option><option value="hard_locked">Hard lock</option></select></Field><Field label="Reason"><input name="reason" required defaultValue="Owner reviewed this accounting period." /></Field><button type="submit" disabled={busy}>Append period revision</button></form>
                  <div className="operations-cards">{summary?.periods.map((period) => <article key={`${text(period.periodId)}:${number(period.revision)}`}><strong>{text(period.periodStart)} to {text(period.periodEnd)}</strong><span>{text(period.status)} · revision {number(period.revision)}</span></article>)}</div>
                  <h2>Accounting export profile</h2>
                  <form className="operations-form operations-form-wide" onSubmit={submitExportProfile}><Field label="Profile name"><input name="profileName" required defaultValue="Xero draft journals" /></Field><Field label="Format"><select name="exportFormat"><option value="xero">Xero</option><option value="myob">MYOB</option></select></Field><Field label="Bank control code"><input name="bankCode" required defaultValue="100" /></Field><Field label="Tax code"><input name="taxCode" required defaultValue="NONE" /></Field><Field label="Category mapping JSON"><textarea name="categoryMapping" rows={7} defaultValue={'{"business:client_income":"200","business:studio_rent":"400","business:software_subscriptions":"410","personal:owner_draw":"900","personal:personal_meals":"901","unresolved:uncategorised":"999"}'} /></Field><button type="submit" disabled={busy}>Save versioned profile</button></form>
                  <div className="operations-cards">{summary?.accountingExportProfiles.map((profile) => <article key={text(profile.profileId)}><strong>{text(profile.profileName)}</strong><span>{text(profile.exportFormat)} · revision {number(profile.revision)}</span><button type="button" disabled={busy} onClick={() => void run("Accounting export preparation", () => createAccountingExport({ profileId: text(profile.profileId), periodStart: summary?.dataThrough.slice(0, 8) + "01", periodEnd: summary?.dataThrough.slice(0, 10) }))}>Prepare current-month CSV</button></article>)}</div>
                </section>
              ) : null}

              {tab === "fx" ? (
                <section id="operations-panel-fx" role="tabpanel" aria-labelledby="operations-tab-fx">
                  <h2>Add an evidence-backed FX rate</h2>
                  <form className="operations-form" onSubmit={submitFxRate}><Field label="Base currency"><input name="baseCurrency" required maxLength={3} defaultValue="USD" /></Field><Field label="Effective on"><input name="effectiveOn" type="date" required /></Field><Field label="Rate numerator"><input name="rateNumerator" type="number" required defaultValue="162" min="1" /></Field><Field label="Rate denominator"><input name="rateDenominator" type="number" required defaultValue="100" min="1" /></Field><Field label="Source label"><input name="sourceLabel" required defaultValue="Owner-provided documented rate" /></Field><Field label="Evidence"><select name="evidenceId" required>{summary?.sources.flatMap((source) => source.sourceItemId ? [<option key={source.sourceItemId} value={source.sourceItemId.replace(/^src_/, "evd_")}>{source.label}</option>] : [])}</select></Field><button type="submit" disabled={busy}>Append rate revision</button></form>
                  <h2>Foreign-currency items</h2>
                  <div className="operations-cards">{summary?.foreignCurrencyItems.map((item) => <article key={text(item.itemId)}><strong>{money(item.amountMinor, text(item.currency) || "USD")}</strong><span>{text(item.description)} · {text(item.status)}</span>{item.status === "pending" ? <form className="operations-inline-actions" onSubmit={(event) => { event.preventDefault(); const data = new FormData(event.currentTarget); void run("FX conversion", () => convertFxItem(text(item.itemId), String(data.get("rateId")), String(data.get("accountId")))); }}><input name="rateId" placeholder="Rate ID" required aria-label="FX rate ID" /><select name="accountId" required aria-label="NZD target account">{summary?.accounts.filter((account) => account.currency === "NZD").map((account) => <option key={account.accountId} value={account.accountId}>{account.name}</option>)}</select><button type="submit" disabled={busy}>Convert with receipt</button></form> : null}</article>)}</div>
                </section>
              ) : null}
            </main>
          </div>
        </div>
      ) : null}
    </>
  );
}
'''

CSS = '''.operations-launcher{position:fixed;right:18px;bottom:18px;z-index:45;border:1px solid var(--line,#384039);background:var(--surface,#151917);color:inherit;border-radius:10px;padding:10px 13px;font:inherit;box-shadow:0 8px 28px rgba(0,0,0,.25)}.operations-launcher span{margin-left:8px;opacity:.55;font-size:12px}.operations-backdrop{position:fixed;inset:0;z-index:90;background:rgba(4,7,5,.72);display:grid;place-items:center;padding:18px}.operations-dialog{width:min(1120px,100%);height:min(820px,100%);background:var(--surface,#111512);color:var(--text,#edf2ee);border:1px solid var(--line,#313a33);border-radius:16px;display:grid;grid-template-rows:auto auto auto 1fr;overflow:hidden;box-shadow:0 30px 90px rgba(0,0,0,.5)}.operations-header{display:flex;justify-content:space-between;gap:24px;padding:22px 24px 16px;border-bottom:1px solid var(--line,#313a33)}.operations-header h1{margin:2px 0 4px;font-size:26px}.operations-header p{margin:0;color:var(--muted,#a9b2ab)}.operations-header button,.operations-tabs button,.operations-status button,.operations-dialog button{font:inherit}.operations-eyebrow{text-transform:uppercase;letter-spacing:.12em;font-size:11px}.operations-tabs{display:flex;gap:3px;padding:8px 12px;border-bottom:1px solid var(--line,#313a33);overflow:auto}.operations-tabs button{background:transparent;color:inherit;border:0;border-radius:8px;padding:8px 12px;white-space:nowrap}.operations-tabs button[aria-selected=true]{background:rgba(255,255,255,.09)}.operations-status{display:flex;align-items:center;gap:9px;padding:9px 16px;background:rgba(255,255,255,.035);font-size:13px;color:var(--muted,#a9b2ab)}.operations-status button{margin-left:auto}.operations-status>span{width:8px;height:8px;border-radius:50%;background:#55a56b}.operations-status>span.is-busy{animation:operations-pulse 1s infinite}.operations-body{overflow:auto;padding:22px 24px}.operations-body h2{font-size:17px;margin:24px 0 12px}.operations-body h2:first-child{margin-top:0}.operations-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px}.operations-metrics article,.operations-cards article,.operations-receipt{border:1px solid var(--line,#313a33);border-radius:12px;padding:14px;background:rgba(255,255,255,.025)}.operations-metrics span,.operations-cards span,.operations-cards em,.operations-columns span{display:block;color:var(--muted,#a9b2ab);font-size:12px;margin-top:4px}.operations-metrics strong{display:block;font-size:24px;margin-top:6px}.operations-table-wrap{overflow:auto;border:1px solid var(--line,#313a33);border-radius:12px}.operations-table-wrap table{width:100%;border-collapse:collapse}.operations-table-wrap th,.operations-table-wrap td{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line,#313a33);white-space:nowrap}.operations-columns{display:grid;grid-template-columns:1fr 1fr;gap:12px}.operations-columns ul{list-style:none;margin:0;padding:0;border:1px solid var(--line,#313a33);border-radius:12px}.operations-columns li{padding:11px 13px;border-bottom:1px solid var(--line,#313a33)}.operations-columns li:last-child{border-bottom:0}.operations-form{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:11px;align-items:end;border:1px solid var(--line,#313a33);border-radius:12px;padding:14px}.operations-form-wide{grid-template-columns:repeat(2,minmax(0,1fr))}.operations-field{display:grid;gap:5px}.operations-field>span{font-size:12px;color:var(--muted,#a9b2ab)}.operations-field input,.operations-field select,.operations-field textarea,.operations-inline-actions input,.operations-inline-actions select{width:100%;box-sizing:border-box;background:rgba(0,0,0,.2);border:1px solid var(--line,#384039);border-radius:8px;color:inherit;padding:9px;font:inherit}.operations-field:has(textarea){grid-column:1/-1}.operations-form>button,.operations-cards button,.operations-actions button,.operations-inline-actions button,.operations-header button,.operations-status button{border:1px solid var(--line,#384039);background:rgba(255,255,255,.08);color:inherit;border-radius:8px;padding:8px 10px}.operations-form>button{min-height:40px}.operations-cards{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.operations-cards article{display:grid;gap:5px}.operations-cards button{margin-top:7px;justify-self:start}.operations-inline-actions,.operations-actions{display:flex;gap:8px;flex-wrap:wrap}.operations-inline-actions input,.operations-inline-actions select{min-width:130px;flex:1}.operations-receipt{display:flex;gap:12px;margin-top:10px}.operations-receipt span{color:var(--muted,#a9b2ab)}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid currentColor;outline-offset:2px}@keyframes operations-pulse{50%{opacity:.25;transform:scale(.8)}}@media(max-width:760px){.operations-backdrop{padding:0}.operations-dialog{height:100%;border-radius:0;border:0}.operations-header{padding:17px}.operations-body{padding:16px}.operations-metrics{grid-template-columns:1fr 1fr}.operations-form,.operations-form-wide,.operations-cards,.operations-columns{grid-template-columns:1fr}.operations-launcher{right:10px;bottom:10px}.operations-tabs{padding-inline:8px}}@media(prefers-reduced-motion:reduce){.operations-status>span.is-busy{animation:none}.operations-dialog{scroll-behavior:auto}}'''

NODE_TEST = '''import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const component = await readFile(new URL("../src/OperationsWorkbench.tsx", import.meta.url), "utf8");
const operations = await readFile(new URL("../src/operations.ts", import.meta.url), "utf8");
const css = await readFile(new URL("../src/operations.css", import.meta.url), "utf8");

test("operations workbench is one modal surface with keyboard and focus containment", () => {
  assert.match(component, /role="dialog"/);
  assert.match(component, /aria-modal="true"/);
  assert.match(component, /role="tablist"/);
  assert.match(component, /event\.key === "Escape"/);
  assert.match(component, /event\.key !== "Tab"/);
  assert.doesNotMatch(component, /dangerouslySetInnerHTML/);
});

test("operations actions call only the closed local workspace routes", () => {
  assert.match(operations, /OPERATIONS_WORKSPACE_ID = "ws_koru_studio"/);
  for (const route of [
    "accounting-periods",
    "classification-rules/preview",
    "transfers/scan",
    "duplicates/scan",
    "statement-reconciliations",
    "foreign-currency/rates",
    "accounting-exports",
  ]) assert.ok(operations.includes(route), route);
  assert.doesNotMatch(operations, /https?:\/\//);
});

test("operations workbench has mobile and reduced-motion rules", () => {
  assert.match(css, /@media\(max-width:760px\)/);
  assert.match(css, /prefers-reduced-motion:reduce/);
  assert.match(css, /:focus-visible/);
});
'''

PYTHON_TEST = '''from __future__ import annotations

import json
from pathlib import Path

import pytest

from finance_agent.api.services import LocalRouteServices


@pytest.mark.asyncio
async def test_operations_summary_is_local_bounded_and_secret_free(tmp_path: Path) -> None:
    services = LocalRouteServices(tmp_path / "folio.sqlite3", auto_seed=True)
    try:
        value = await services.operations_summary(workspace_id="ws_koru_studio")
        assert value["summaryVersion"] == "folio.operations-summary@1"
        assert value["externalCallsMade"] is False
        assert value["accounts"]
        assert value["sources"]
        assert value["analytics"]["modelUsed"] is False
        encoded = json.dumps(value).lower()
        for forbidden in ("api_key", "access_token", "bot_token", "password", "secret"):
            assert forbidden not in encoded
    finally:
        await services.aclose()
'''


def update_backend() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    insert_method_before(path, "LocalRouteServices", "portable_data_export", SERVICE_METHOD)
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def portable_data_export(self) -> ArtifactPayload: ...\n"
    addition = '''    async def operations_summary(\n        self, *, workspace_id: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("portable export protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)
    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    marker = '    @router.get("/v1/system/portable-export")\n'
    if marker not in content:
        raise RuntimeError("portable export route marker missing")
    content = content.replace(marker, ROUTE + marker, 1)
    write(path, content)


def update_frontend() -> None:
    path = "apps/desktop/src/transport.ts"
    content = read(path)
    content = content.replace(
        "async function requestJson<T>(",
        "export async function requestJson<T>(",
        1,
    )
    write(path, content)
    write("apps/desktop/src/operations.ts", OPERATIONS_TS)
    write("apps/desktop/src/OperationsWorkbench.tsx", COMPONENT)
    write("apps/desktop/src/operations.css", CSS)
    path = "apps/desktop/src/main.tsx"
    content = read(path)
    if 'import { OperationsWorkbench } from "./OperationsWorkbench";' not in content:
        content = content.replace(
            'import { App } from "./App";\n',
            'import { App } from "./App";\nimport { OperationsWorkbench } from "./OperationsWorkbench";\n',
            1,
        )
    content = content.replace(
        "<App />",
        "<>\n      <App />\n      <OperationsWorkbench />\n    </>",
        1,
    )
    write(path, content)


def tests_scripts_docs() -> None:
    write("apps/desktop/tests/operations-workbench.test.mjs", NODE_TEST)
    write("services/api/tests/api/test_operations_summary.py", PYTHON_TEST)
    package_path = ROOT / "package.json"
    package = json.loads(package_path.read_text())
    scripts = package.setdefault("scripts", {})
    scripts["test:operations"] = "node --test apps/desktop/tests/operations-workbench.test.mjs"
    verify = scripts.get("verify", "")
    if "pnpm test:operations" not in verify:
        scripts["verify"] = verify + " && pnpm test:operations"
    package_path.write_text(json.dumps(package, indent=2) + "\n")
    write("docs/FINANCE_OPERATIONS_WORKBENCH.md", '''# Finance operations workbench\n\nFinance operations is one deliberate desktop dialog opened from the fixed launcher or `Cmd/Ctrl+Shift+O`. It reads one bounded local summary and separates overview, rule, reconciliation, accounting and foreign-currency controls into keyboard-accessible tabs. Focus is contained while open, Escape and click-away close it, status changes use a polite live region and all forms have visible labels.\n\nActions call the existing loopback routes. Rule work starts with a non-mutating preview. Transfer and duplicate scans do not commit meaning until the owner confirms. Statement discrepancies cannot be labelled reconciled. Period lock changes, FX rates and export profiles append revisions. Accounting CSV creation remains preparation only. After every write, the workbench reloads committed state rather than manufacturing local optimistic finance values.\n\nThe workbench is not a second dashboard or a model control panel. It exposes explicit high-judgement operations and receipts while ordinary finance questions remain in the conversation.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 47: accessible desktop finance operations workbench\n\n- One keyboard-accessible dialog exposes the queued accounting controls.\n- Overview uses exact local analytics and committed source/account counts.\n- Rule work begins with preview; scans remain non-mutating until owner confirmation.\n- Statement, period, FX and accounting-export forms preserve their proof boundaries.\n- Every action refreshes authoritative local state instead of fabricating optimistic totals.\n- The ordinary conversation remains the primary surface; internals stay out of daily use.\n'''
    if "## Stack 47: accessible desktop finance operations workbench" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    update_backend()
    update_frontend()
    tests_scripts_docs()
    print("desktop operations workbench changes applied")


if __name__ == "__main__":
    main()
