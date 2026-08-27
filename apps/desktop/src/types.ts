export type ModelMode = "local" | "hybrid" | "cloud";
export type DrawerKind = "sources" | "activity" | "connections";
export type SurfaceType =
  | "living_brief"
  | "transaction_detail"
  | "cash_scenario"
  | "records_table"
  | "owner_pack"
  | "work_receipt";

export type Money = {
  valueMinor: number;
  currency: string;
};

export type Freshness = {
  dataThrough: string;
  status: "current" | "stale" | "partial";
  timezone: string;
};

export type ForecastPoint = {
  date: string;
  balanceMinor: number;
  reserveMinor: number;
  status: "above_reserve" | "below_reserve";
};

export type TransactionRow = {
  transactionId: string;
  occurredOn: string;
  description: string;
  amountMinor: number;
  currency: string;
  classification: "business" | "personal" | "unresolved" | "transfer";
  category: string | null;
  status: "posted" | "pending" | "duplicate" | "ignored";
  evidenceIds: string[];
};

export type NarrativeBlock = {
  blockId: string;
  type: "narrative";
  text: string;
  tone: "neutral" | "positive" | "caution" | "critical";
};

export type MetricBlock = {
  blockId: string;
  type: "metric";
  label: string;
  valueMinor: number;
  currency: string;
  evidenceIds: string[];
};

export type CashSeriesBlock = {
  blockId: string;
  type: "cash_series";
  currency: string;
  points: ForecastPoint[];
  assumptions: string[];
  evidenceIds: string[];
};

export type ScenarioCompareBlock = {
  blockId: string;
  type: "scenario_compare";
  baseline: {
    label: string;
    lowPointMinor: number;
    reserveShortfallMinor: number;
    currency: string;
  };
  alternative: {
    label: string;
    lowPointMinor: number;
    reserveShortfallMinor: number;
    currency: string;
  };
  assumptions: string[];
  evidenceIds: string[];
};

export type TransactionRowsBlock = {
  blockId: string;
  type: "transaction_rows";
  rows: TransactionRow[];
  totalMinor: number;
  currency: string;
};

export type FindingBlock = {
  blockId: string;
  type: "finding";
  findingId: string;
  severity: "info" | "attention" | "critical";
  title: string;
  summary: string;
  amountMinor: number | null;
  currency: string | null;
  status: "open" | "resolved" | "dismissed";
  evidenceIds: string[];
};

export type SourceListBlock = {
  blockId: string;
  type: "source_list";
  sources: Array<{
    sourceItemId: string;
    label: string;
    sourceType: "csv" | "telegram_fixture" | "owner_claim" | "akahu_fixture" | "plaid_fixture";
    receivedAt: string;
    status: "pending" | "processed" | "failed";
  }>;
};

export type ChangeDiffBlock = {
  blockId: string;
  type: "change_diff";
  eventId: string;
  changes: Array<{
    field:
      | "classification"
      | "category"
      | "ruleId"
      | "unresolvedExpenseMinor"
      | "projectedLowPointMinor"
      | "reserveShortfallMinor";
    label: string;
    before: string | number | boolean | null;
    after: string | number | boolean | null;
  }>;
  evidenceIds: string[];
  undoAvailable: boolean;
};

export type ArtifactPreviewBlock = {
  blockId: string;
  type: "artifact_preview";
  artifactId: string;
  kind: "html" | "pdf";
  title: string;
  generatedAt: string;
  contentHash: string;
  downloadAvailable: boolean;
  evidenceIds: string[];
};

export type FinanceBlock =
  | NarrativeBlock
  | MetricBlock
  | CashSeriesBlock
  | ScenarioCompareBlock
  | TransactionRowsBlock
  | FindingBlock
  | SourceListBlock
  | ChangeDiffBlock
  | ArtifactPreviewBlock;

export type FinanceAction =
  | { actionId: string; type: "focus_source"; label: string; sourceItemId: string }
  | { actionId: string; type: "open_drawer"; label: string; drawer: DrawerKind }
  | { actionId: string; type: "run_scenario"; label: string; scenarioId: string }
  | { actionId: string; type: "undo_event"; label: string; eventId: string }
  | {
      actionId: string;
      type: "download_artifact";
      label: string;
      artifactId: string;
      format: "html" | "pdf";
    };

export type FinanceSurfaceSpec = {
  specVersion: "FinanceSurfaceSpec@1";
  surfaceId: string;
  surfaceType: SurfaceType;
  title: string;
  subtitle: string | null;
  freshness: Freshness;
  blocks: FinanceBlock[];
  actions: FinanceAction[];
};

export type ThreadTurn = {
  turnId: string;
  role: "agent" | "owner";
  content: string;
  occurredAt: string;
  status: "complete" | "streaming" | "stopped";
  evidenceIds: string[];
  receipt?: {
    label: string;
    eventId?: string;
    undoable?: boolean;
  };
};

export type ActivityItem = {
  activityId: string;
  kind: "job_run" | "finance_event" | "undo" | "source_ingest" | "artifact" | "outbox";
  summary: string;
  detail?: string;
  status: "queued" | "running" | "completed" | "undone" | "failed";
  occurredAt: string;
  undoable: boolean;
  correlationId?: string;
  eventId?: string;
  evidenceIds: string[];
};

export type SourceItem = {
  sourceItemId: string;
  sourceType: "csv" | "telegram_fixture" | "owner_claim" | "akahu_fixture" | "plaid_fixture";
  label: string;
  receivedAt: string;
  status: "pending" | "processed" | "failed";
  rowCount: number;
  digest?: string;
};

export type WorkspaceSnapshot = {
  snapshotVersion: "api.snapshot@1";
  snapshotId: string;
  workspace: {
    workspaceId: string;
    name: string;
    entityType: string;
    currency: string;
    timezone: string;
    protectedReserveMinor: number;
  };
  thread: {
    threadId: string;
    turns: ThreadTurn[];
    activeQuestion: null | { questionId: string; prompt: string; askedAt?: string };
  };
  currentSurface: FinanceSurfaceSpec;
  findings: Array<{
    findingId: string;
    kind: string;
    severity: "info" | "attention" | "critical";
    title: string;
    summary: string;
    amountMinor: number | null;
    currency: string | null;
    status: "open" | "resolved" | "dismissed";
    evidenceIds: string[];
  }>;
  activity: ActivityItem[];
  sources: SourceItem[];
  totals: {
    asOf: string;
    currency: string;
    currentBalanceMinor: number;
    protectedReserveMinor: number;
    businessIncomeMinor: number;
    businessExpenseMinor: number;
    personalExpenseMinor: number;
    unresolvedExpenseMinor: number;
    projectedLowPointMinor: number;
    reserveShortfallMinor: number;
  };
  artifacts: Array<{
    artifactId: string;
    kind: "owner_pack_html" | "owner_pack_pdf";
    title: string;
    contentHash: string;
    generatedAt: string;
    evidenceIds: string[];
  }>;
  modelMode: ModelMode;
  freshness: Freshness;
};

export type RunEvent = {
  eventId: string;
  threadId: string;
  runId: string;
  sequence: number;
  occurredAt: string;
  type:
    | "run.started"
    | "message.delta"
    | "message.completed"
    | "stage.started"
    | "stage.completed"
    | "tool.started"
    | "tool.completed"
    | "state.snapshot"
    | "state.patch"
    | "surface.replace"
    | "surface.patch"
    | "receipt.committed"
    | "run.failed"
    | "run.completed";
  payload: Record<string, unknown>;
};

const surfaceTypes: ReadonlySet<string> = new Set([
  "living_brief",
  "transaction_detail",
  "cash_scenario",
  "records_table",
  "owner_pack",
  "work_receipt",
]);

const blockTypes: ReadonlySet<string> = new Set([
  "narrative",
  "metric",
  "cash_series",
  "scenario_compare",
  "transaction_rows",
  "finding",
  "source_list",
  "change_diff",
  "artifact_preview",
]);

const actionTypes: ReadonlySet<string> = new Set([
  "focus_source",
  "open_drawer",
  "run_scenario",
  "undo_event",
  "download_artifact",
]);

export function validateSurfaceSpec(value: unknown): FinanceSurfaceSpec {
  const candidate = record(value, "finance surface");
  exact(candidate, ["specVersion", "surfaceId", "surfaceType", "title", "subtitle", "freshness", "blocks", "actions"], "finance surface");
  if (candidate.specVersion !== "FinanceSurfaceSpec@1") throw new Error("Unsupported finance surface version.");
  requiredString(candidate.surfaceId, "surfaceId");
  requiredString(candidate.title, "surface title");
  if (candidate.subtitle !== null && typeof candidate.subtitle !== "string") throw new Error("surface subtitle must be text or null");
  if (typeof candidate.surfaceType !== "string" || !surfaceTypes.has(candidate.surfaceType)) throw new Error("Surface type is outside the six-surface finance catalogue.");
  const freshness = record(candidate.freshness, "freshness");
  exact(freshness, ["dataThrough", "status", "timezone"], "freshness");
  requiredString(freshness.dataThrough, "freshness dataThrough");
  requiredString(freshness.timezone, "freshness timezone");
  if (!Array.isArray(candidate.blocks) || candidate.blocks.length < 1 || candidate.blocks.length > 20) throw new Error("Surface blocks violate the closed contract count.");
  candidate.blocks.forEach(validateBlock);
  if (!Array.isArray(candidate.actions) || candidate.actions.length > 8) throw new Error("Surface actions violate the closed contract count.");
  candidate.actions.forEach(validateAction);
  return candidate as unknown as FinanceSurfaceSpec;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} must be an object.`);
  return value as Record<string, unknown>;
}

function exact(value: Record<string, unknown>, keys: readonly string[], label: string): void {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) throw new Error(`${label} contains fields outside the closed contract.`);
}

function requiredString(value: unknown, label: string): asserts value is string {
  if (typeof value !== "string" || value.length === 0) throw new Error(`${label} must be non-empty text.`);
}

function requiredNumber(value: unknown, label: string): asserts value is number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${label} must be a finite number.`);
}

function strings(value: unknown, label: string): asserts value is string[] {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) throw new Error(`${label} must be a string array.`);
}

function validateEvidence(value: Record<string, unknown>, label: string): void {
  strings(value.evidenceIds, `${label} evidenceIds`);
}

function validateTransaction(value: unknown): void {
  const row = record(value, "transaction row");
  exact(row, ["transactionId", "occurredOn", "description", "amountMinor", "currency", "classification", "category", "status", "evidenceIds"], "transaction row");
  requiredString(row.transactionId, "transactionId");
  requiredString(row.occurredOn, "transaction occurredOn");
  requiredString(row.description, "transaction description");
  requiredNumber(row.amountMinor, "transaction amountMinor");
  requiredString(row.currency, "transaction currency");
  validateEvidence(row, "transaction");
}

function validateBlock(value: unknown): void {
  const block = record(value, "surface block");
  requiredString(block.blockId, "blockId");
  requiredString(block.type, "block type");
  if (!blockTypes.has(block.type)) throw new Error(`Unknown finance block: ${block.type}`);
  switch (block.type) {
    case "narrative":
      exact(block, ["blockId", "type", "text", "tone"], "narrative block");
      requiredString(block.text, "narrative text");
      break;
    case "metric":
      exact(block, ["blockId", "type", "label", "valueMinor", "currency", "evidenceIds"], "metric block");
      requiredNumber(block.valueMinor, "metric valueMinor");
      validateEvidence(block, "metric");
      break;
    case "cash_series":
      exact(block, ["blockId", "type", "currency", "points", "assumptions", "evidenceIds"], "cash series block");
      if (!Array.isArray(block.points) || block.points.length < 2) throw new Error("cash series needs at least two points");
      block.points.forEach((item) => exact(record(item, "cash point"), ["date", "balanceMinor", "reserveMinor", "status"], "cash point"));
      strings(block.assumptions, "cash assumptions");
      validateEvidence(block, "cash series");
      break;
    case "scenario_compare":
      exact(block, ["blockId", "type", "baseline", "alternative", "assumptions", "evidenceIds"], "scenario block");
      exact(record(block.baseline, "baseline scenario"), ["label", "lowPointMinor", "reserveShortfallMinor", "currency"], "baseline scenario");
      exact(record(block.alternative, "alternative scenario"), ["label", "lowPointMinor", "reserveShortfallMinor", "currency"], "alternative scenario");
      strings(block.assumptions, "scenario assumptions");
      validateEvidence(block, "scenario");
      break;
    case "transaction_rows":
      exact(block, ["blockId", "type", "rows", "totalMinor", "currency"], "transaction rows block");
      if (!Array.isArray(block.rows)) throw new Error("transaction rows must be an array");
      block.rows.forEach(validateTransaction);
      requiredNumber(block.totalMinor, "transaction totalMinor");
      break;
    case "finding":
      exact(block, ["blockId", "type", "findingId", "severity", "title", "summary", "amountMinor", "currency", "status", "evidenceIds"], "finding block");
      requiredString(block.findingId, "findingId");
      validateEvidence(block, "finding");
      break;
    case "source_list":
      exact(block, ["blockId", "type", "sources"], "source list block");
      if (!Array.isArray(block.sources)) throw new Error("source list must be an array");
      block.sources.forEach((item) => exact(record(item, "source item"), ["sourceItemId", "label", "sourceType", "receivedAt", "status"], "source item"));
      break;
    case "change_diff":
      exact(block, ["blockId", "type", "eventId", "changes", "evidenceIds", "undoAvailable"], "change diff block");
      requiredString(block.eventId, "change eventId");
      if (!Array.isArray(block.changes)) throw new Error("changes must be an array");
      block.changes.forEach((item) => exact(record(item, "change"), ["field", "label", "before", "after"], "change"));
      validateEvidence(block, "change diff");
      break;
    case "artifact_preview":
      exact(block, ["blockId", "type", "artifactId", "kind", "title", "generatedAt", "contentHash", "downloadAvailable", "evidenceIds"], "artifact block");
      requiredString(block.artifactId, "artifactId");
      validateEvidence(block, "artifact");
      break;
  }
}

function validateAction(value: unknown): void {
  const action = record(value, "surface action");
  requiredString(action.actionId, "actionId");
  requiredString(action.type, "action type");
  if (!actionTypes.has(action.type)) throw new Error(`Unknown finance action: ${action.type}`);
  const keys: Record<string, readonly string[]> = {
    focus_source: ["actionId", "type", "label", "sourceItemId"],
    open_drawer: ["actionId", "type", "label", "drawer"],
    run_scenario: ["actionId", "type", "label", "scenarioId"],
    undo_event: ["actionId", "type", "label", "eventId"],
    download_artifact: ["actionId", "type", "label", "artifactId", "format"],
  };
  const expectedKeys = keys[action.type];
  if (!expectedKeys) throw new Error(`Unknown finance action: ${action.type}`);
  exact(action, expectedKeys, `${action.type} action`);
  requiredString(action.label, "action label");
}

export function validateRunEvent(value: unknown): RunEvent {
  const event = record(value, "run event");
  exact(event, ["eventId", "threadId", "runId", "sequence", "occurredAt", "type", "payload"], "run event");
  requiredString(event.eventId, "eventId");
  requiredString(event.threadId, "threadId");
  requiredString(event.runId, "runId");
  requiredNumber(event.sequence, "event sequence");
  requiredString(event.occurredAt, "event occurredAt");
  requiredString(event.type, "event type");
  const payload = record(event.payload, "event payload");
  const payloadKeys: Record<string, readonly string[]> = {
    "run.started": ["mode", "reason", "resumeFromSequence"],
    "message.delta": ["turnId", "delta"],
    "message.completed": ["turnId", "content", "evidenceIds"],
    "stage.started": ["stage"],
    "stage.completed": ["stage", "status", "durationMs"],
    "tool.started": ["toolCallId", "toolName"],
    "tool.completed": ["toolCallId", "toolName", "status", "durationMs", "evidenceIds"],
    "state.snapshot": ["snapshot"],
    "state.patch": ["baseSnapshotId", "snapshot"],
    "surface.replace": ["surface"],
    "surface.patch": ["surfaceId", "surface"],
    "receipt.committed": ["receiptType", "receiptId", "contentHash", "evidenceIds"],
    "run.failed": ["code", "message", "retryable", "lastSequence"],
    "run.completed": ["status", "durationMs", "snapshotId", "receiptId"],
  };
  const expectedPayloadKeys = payloadKeys[event.type];
  if (!expectedPayloadKeys) throw new Error(`Unknown run event type: ${event.type}`);
  exact(payload, expectedPayloadKeys, `${event.type} payload`);
  if (event.type === "surface.replace" || event.type === "surface.patch") {
    validateSurfaceSpec(payload.surface);
  }
  if (event.type === "state.snapshot" || event.type === "state.patch") {
    validateWorkspaceSnapshot(payload.snapshot);
  }
  return event as unknown as RunEvent;
}


function requiredInteger(value: unknown, label: string): asserts value is number {
  requiredNumber(value, label);
  if (!Number.isSafeInteger(value)) throw new Error(`${label} must be a safe integer.`);
}

function allowedValue(
  value: unknown,
  allowed: ReadonlySet<string>,
  label: string,
): asserts value is string {
  if (typeof value !== "string" || !allowed.has(value)) {
    throw new Error(`${label} is outside the closed contract.`);
  }
}

function exactWithOptional(
  value: Record<string, unknown>,
  required: readonly string[],
  optional: readonly string[],
  label: string,
): void {
  const actual = Object.keys(value).sort();
  const permitted = new Set([...required, ...optional]);
  if (
    required.some((key) => !(key in value))
    || actual.some((key) => !permitted.has(key))
  ) {
    throw new Error(`${label} contains missing or unsupported fields.`);
  }
}

function validateFreshness(value: unknown, label = "freshness"): void {
  const freshness = record(value, label);
  exact(freshness, ["dataThrough", "status", "timezone"], label);
  requiredString(freshness.dataThrough, `${label} dataThrough`);
  requiredString(freshness.timezone, `${label} timezone`);
  allowedValue(
    freshness.status,
    new Set(["current", "stale", "partial"]),
    `${label} status`,
  );
}

export function validateWorkspaceSnapshot(value: unknown): WorkspaceSnapshot {
  const snapshot = record(value, "workspace snapshot");
  exact(snapshot, [
    "snapshotVersion",
    "snapshotId",
    "workspace",
    "thread",
    "currentSurface",
    "findings",
    "activity",
    "sources",
    "totals",
    "artifacts",
    "modelMode",
    "freshness",
  ], "workspace snapshot");
  if (snapshot.snapshotVersion !== "api.snapshot@1") {
    throw new Error("Unsupported workspace snapshot version.");
  }
  requiredString(snapshot.snapshotId, "snapshotId");

  const workspace = record(snapshot.workspace, "workspace");
  exact(workspace, [
    "workspaceId",
    "name",
    "entityType",
    "currency",
    "timezone",
    "protectedReserveMinor",
  ], "workspace");
  requiredString(workspace.workspaceId, "workspaceId");
  requiredString(workspace.name, "workspace name");
  requiredString(workspace.entityType, "workspace entityType");
  requiredString(workspace.currency, "workspace currency");
  requiredString(workspace.timezone, "workspace timezone");
  requiredInteger(workspace.protectedReserveMinor, "workspace protectedReserveMinor");

  const thread = record(snapshot.thread, "thread");
  exact(thread, ["threadId", "turns", "activeQuestion"], "thread");
  requiredString(thread.threadId, "threadId");
  if (!Array.isArray(thread.turns)) throw new Error("thread turns must be an array");
  thread.turns.forEach((item) => {
    const turn = record(item, "thread turn");
    exactWithOptional(
      turn,
      ["turnId", "role", "content", "occurredAt", "status", "evidenceIds"],
      ["receipt"],
      "thread turn",
    );
    requiredString(turn.turnId, "turnId");
    allowedValue(turn.role, new Set(["agent", "owner"]), "turn role");
    requiredString(turn.content, "turn content");
    requiredString(turn.occurredAt, "turn occurredAt");
    allowedValue(
      turn.status,
      new Set(["complete", "streaming", "stopped"]),
      "turn status",
    );
    strings(turn.evidenceIds, "turn evidenceIds");
    if (turn.receipt !== undefined) {
      const receipt = record(turn.receipt, "turn receipt");
      exactWithOptional(
        receipt,
        ["label"],
        ["eventId", "undoable"],
        "turn receipt",
      );
      requiredString(receipt.label, "receipt label");
      if (receipt.eventId !== undefined) {
        requiredString(receipt.eventId, "receipt eventId");
      }
      if (receipt.undoable !== undefined && typeof receipt.undoable !== "boolean") {
        throw new Error("receipt undoable must be boolean");
      }
    }
  });
  if (thread.activeQuestion !== null) {
    const question = record(thread.activeQuestion, "active question");
    exactWithOptional(
      question,
      ["questionId", "prompt"],
      ["askedAt"],
      "active question",
    );
    requiredString(question.questionId, "questionId");
    requiredString(question.prompt, "question prompt");
    if (question.askedAt !== undefined) {
      requiredString(question.askedAt, "question askedAt");
    }
  }

  validateSurfaceSpec(snapshot.currentSurface);

  if (!Array.isArray(snapshot.findings)) throw new Error("findings must be an array");
  snapshot.findings.forEach((item) => {
    const finding = record(item, "finding");
    exact(finding, [
      "findingId",
      "kind",
      "severity",
      "title",
      "summary",
      "amountMinor",
      "currency",
      "status",
      "evidenceIds",
    ], "finding");
    requiredString(finding.findingId, "findingId");
    requiredString(finding.kind, "finding kind");
    allowedValue(
      finding.severity,
      new Set(["info", "attention", "critical"]),
      "finding severity",
    );
    requiredString(finding.title, "finding title");
    requiredString(finding.summary, "finding summary");
    if (finding.amountMinor !== null) {
      requiredInteger(finding.amountMinor, "finding amountMinor");
    }
    if (finding.currency !== null) requiredString(finding.currency, "finding currency");
    allowedValue(
      finding.status,
      new Set(["open", "resolved", "dismissed"]),
      "finding status",
    );
    strings(finding.evidenceIds, "finding evidenceIds");
  });

  if (!Array.isArray(snapshot.activity)) throw new Error("activity must be an array");
  snapshot.activity.forEach((item) => {
    const activity = record(item, "activity item");
    exactWithOptional(
      activity,
      ["activityId", "kind", "summary", "status", "occurredAt", "undoable", "evidenceIds"],
      ["detail", "correlationId", "eventId"],
      "activity item",
    );
    requiredString(activity.activityId, "activityId");
    allowedValue(
      activity.kind,
      new Set(["job_run", "finance_event", "undo", "source_ingest", "artifact", "outbox"]),
      "activity kind",
    );
    requiredString(activity.summary, "activity summary");
    allowedValue(
      activity.status,
      new Set(["queued", "running", "completed", "undone", "failed"]),
      "activity status",
    );
    requiredString(activity.occurredAt, "activity occurredAt");
    if (typeof activity.undoable !== "boolean") {
      throw new Error("activity undoable must be boolean");
    }
    strings(activity.evidenceIds, "activity evidenceIds");
  });

  if (!Array.isArray(snapshot.sources)) throw new Error("sources must be an array");
  snapshot.sources.forEach((item) => {
    const source = record(item, "source item");
    exactWithOptional(
      source,
      ["sourceItemId", "sourceType", "label", "receivedAt", "status", "rowCount"],
      ["digest"],
      "source item",
    );
    requiredString(source.sourceItemId, "sourceItemId");
    allowedValue(
      source.sourceType,
      new Set(["csv", "telegram_fixture", "owner_claim", "akahu_fixture", "plaid_fixture"]),
      "source type",
    );
    requiredString(source.label, "source label");
    requiredString(source.receivedAt, "source receivedAt");
    allowedValue(
      source.status,
      new Set(["pending", "processed", "failed"]),
      "source status",
    );
    requiredInteger(source.rowCount, "source rowCount");
    if (source.digest !== undefined) requiredString(source.digest, "source digest");
  });

  const totals = record(snapshot.totals, "totals");
  exact(totals, [
    "asOf",
    "currency",
    "currentBalanceMinor",
    "protectedReserveMinor",
    "businessIncomeMinor",
    "businessExpenseMinor",
    "personalExpenseMinor",
    "unresolvedExpenseMinor",
    "projectedLowPointMinor",
    "reserveShortfallMinor",
  ], "totals");
  requiredString(totals.asOf, "totals asOf");
  requiredString(totals.currency, "totals currency");
  for (const key of [
    "currentBalanceMinor",
    "protectedReserveMinor",
    "businessIncomeMinor",
    "businessExpenseMinor",
    "personalExpenseMinor",
    "unresolvedExpenseMinor",
    "projectedLowPointMinor",
    "reserveShortfallMinor",
  ] as const) {
    requiredInteger(totals[key], `totals ${key}`);
  }

  if (!Array.isArray(snapshot.artifacts)) throw new Error("artifacts must be an array");
  snapshot.artifacts.forEach((item) => {
    const artifact = record(item, "artifact");
    exact(artifact, [
      "artifactId",
      "kind",
      "title",
      "contentHash",
      "generatedAt",
      "evidenceIds",
    ], "artifact");
    requiredString(artifact.artifactId, "artifactId");
    allowedValue(
      artifact.kind,
      new Set(["owner_pack_html", "owner_pack_pdf"]),
      "artifact kind",
    );
    requiredString(artifact.title, "artifact title");
    requiredString(artifact.contentHash, "artifact contentHash");
    requiredString(artifact.generatedAt, "artifact generatedAt");
    strings(artifact.evidenceIds, "artifact evidenceIds");
  });

  allowedValue(snapshot.modelMode, new Set(["local", "hybrid", "cloud"]), "modelMode");
  validateFreshness(snapshot.freshness);
  return snapshot as unknown as WorkspaceSnapshot;
}
