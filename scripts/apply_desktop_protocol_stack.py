from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(os.environ.get("FOLIO_ROOT", Path.cwd())).resolve()


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


write(
    "apps/desktop/src/protocol.ts",
    '''export type ProblemDetails = {
  type: string;
  title: string;
  status: number;
  detail: string;
  code: string;
  retryable: boolean;
  instance?: string;
  errors?: Array<{ location: string[]; message: string; kind: string }>;
};

export class ApiProblem extends Error {
  readonly problem: ProblemDetails;

  constructor(problem: ProblemDetails) {
    super(problem.detail);
    this.name = "ApiProblem";
    this.problem = problem;
  }

  get status(): number {
    return this.problem.status;
  }

  get code(): string {
    return this.problem.code;
  }

  get retryable(): boolean {
    return this.problem.retryable;
  }
}

const RETRYABLE_STATUSES = new Set([408, 425, 429, 502, 503, 504]);
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export async function apiProblemFromResponse(response: Response): Promise<ApiProblem> {
  const fallback: ProblemDetails = {
    type: `https://folio.local/problems/http-${response.status}`,
    title: response.statusText || "Request failed",
    status: response.status,
    detail: response.statusText || `Request failed with status ${response.status}.`,
    code: `http_${response.status}`,
    retryable: RETRYABLE_STATUSES.has(response.status),
  };
  try {
    const value = await response.clone().json() as Partial<ProblemDetails>;
    return new ApiProblem({
      type: typeof value.type === "string" ? value.type : fallback.type,
      title: typeof value.title === "string" ? value.title : fallback.title,
      status: typeof value.status === "number" ? value.status : fallback.status,
      detail: typeof value.detail === "string" ? value.detail : fallback.detail,
      code: typeof value.code === "string" ? value.code : fallback.code,
      retryable: typeof value.retryable === "boolean" ? value.retryable : fallback.retryable,
      ...(typeof value.instance === "string" ? { instance: value.instance } : {}),
      ...(Array.isArray(value.errors) ? { errors: value.errors } : {}),
    });
  } catch {
    return new ApiProblem(fallback);
  }
}

export function shouldRetryRequest(
  method: string,
  status: number | null,
  networkFailure = false,
): boolean {
  if (!SAFE_METHODS.has(method.toUpperCase())) return false;
  if (networkFailure) return true;
  return status !== null && RETRYABLE_STATUSES.has(status);
}

export function retryDelayMs(
  attempt: number,
  retryAfter: string | null,
  now = Date.now(),
  random = Math.random(),
): number {
  if (retryAfter) {
    const seconds = Number(retryAfter);
    if (Number.isFinite(seconds) && seconds >= 0) {
      return Math.min(seconds * 1000, 10_000);
    }
    const date = Date.parse(retryAfter);
    if (Number.isFinite(date)) {
      return Math.min(Math.max(0, date - now), 10_000);
    }
  }
  const base = Math.min(250 * (2 ** attempt), 2_000);
  return Math.round(base * (0.8 + Math.max(0, Math.min(1, random)) * 0.4));
}

function dataFromFrame(frame: string): string | null {
  const data = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n");
  return data || null;
}

export class SseDecoder {
  private buffer = "";

  push(chunk: string): string[] {
    this.buffer += chunk;
    const values: string[] = [];
    while (true) {
      const match = /\r?\n\r?\n/.exec(this.buffer);
      if (!match || match.index === undefined) break;
      const frame = this.buffer.slice(0, match.index);
      this.buffer = this.buffer.slice(match.index + match[0].length);
      const data = dataFromFrame(frame);
      if (data !== null) values.push(data);
    }
    return values;
  }

  flush(): string[] {
    const frame = this.buffer;
    this.buffer = "";
    const data = dataFromFrame(frame);
    return data === null ? [] : [data];
  }
}
''',
)

types_path = "apps/desktop/src/types.ts"
content = read(types_path)
content = content.replace(
    "    activeQuestion: null | { questionId: string; prompt: string };",
    "    activeQuestion: null | { questionId: string; prompt: string; askedAt?: string };",
)
content = content.replace(
    '    const snapshot = record(payload.snapshot, `${event.type} snapshot`);\n    validateSurfaceSpec(snapshot.currentSurface);',
    "    validateWorkspaceSnapshot(payload.snapshot);",
)
if "export function validateWorkspaceSnapshot" not in content:
    content += '''

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
'''
write(types_path, content)

transport_path = "apps/desktop/src/transport.ts"
content = read(transport_path)
old_import = 'import { validateRunEvent, validateSurfaceSpec, type RunEvent, type WorkspaceSnapshot } from "./types";'
new_import = 'import { validateRunEvent, validateWorkspaceSnapshot, type RunEvent, type WorkspaceSnapshot } from "./types";\nimport { ApiProblem, SseDecoder, apiProblemFromResponse, retryDelayMs, shouldRetryRequest } from "./protocol";'
if old_import in content:
    content = content.replace(old_import, new_import, 1)
elif 'from "./protocol"' not in content:
    raise RuntimeError("Transport import anchor is missing")

if "function folioSessionHeaders" not in content:
    marker = ').replace(/\\/$/, "");\n'
    position = content.find(marker)
    if position < 0:
        raise RuntimeError("Transport API URL anchor is missing")
    position += len(marker)
    content = content[:position] + '''

function folioSessionHeaders(): Record<string, string> {
  const token = window.financeDesktop?.sessionToken;
  return token ? { "X-Folio-Session": token } : {};
}

const wait = (milliseconds: number) => new Promise<void>((resolve) => {
  window.setTimeout(resolve, milliseconds);
});
''' + content[position:]

request_pattern = re.compile(
    r'async function requestJson<T>\(.*?\n}\n\nexport async function probeBackend',
    re.S,
)
request_replacement = '''async function requestJson<T>(path: string, init?: RequestInit, timeoutMs = 2400): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const maximumAttempts = shouldRetryRequest(method, 503) ? 3 : 1;
  let lastFailure: unknown = null;

  for (let attempt = 0; attempt < maximumAttempts; attempt += 1) {
    try {
      const response = await withTimeout(async (signal) => {
        const headers = new Headers(init?.headers);
        headers.set("Accept", "application/json");
        if (init?.body && !(init.body instanceof FormData)) {
          headers.set("Content-Type", "application/json");
        }
        Object.entries(folioSessionHeaders()).forEach(([key, value]) => {
          headers.set(key, value);
        });
        return fetch(`${API_URL}${path}`, { ...init, signal, headers });
      }, timeoutMs);
      if (response.ok) return (await response.json()) as T;

      const problem = await apiProblemFromResponse(response);
      if (
        attempt + 1 >= maximumAttempts
        || !shouldRetryRequest(method, response.status)
      ) {
        throw problem;
      }
      await wait(retryDelayMs(attempt, response.headers.get("Retry-After")));
    } catch (error) {
      if (error instanceof ApiProblem) throw error;
      lastFailure = error;
      if (
        attempt + 1 >= maximumAttempts
        || !shouldRetryRequest(method, null, true)
      ) {
        break;
      }
      await wait(retryDelayMs(attempt, null));
    }
  }

  throw new ApiProblem({
    type: "https://folio.local/problems/local-service-unavailable",
    title: "Local service unavailable",
    status: 0,
    detail: lastFailure instanceof Error
      ? lastFailure.message
      : "The local Folio service could not be reached.",
    code: "local_service_unavailable",
    retryable: true,
  });
}

export async function probeBackend'''
content, count = request_pattern.subn(request_replacement, content, count=1)
if count != 1:
    raise RuntimeError("requestJson replacement failed")

old_offline = '''      mode: navigator.onLine ? "fixture" : "offline",
      label: navigator.onLine ? "Demo data" : "Offline demo",
      detail: navigator.onLine
        ? "The local finance service is not running, so Folio has opened the sample business."
        : "The app is offline. Existing local data remains available.",'''
new_offline = '''      mode: navigator.onLine ? "degraded" : "offline",
      label: navigator.onLine ? "Local service unavailable" : "Folio is offline",
      detail: navigator.onLine
        ? "The local finance service is not running. The last committed view remains visible; demo data is never substituted automatically."
        : "This computer is offline. The last committed local view remains visible.",'''
if old_offline in content:
    content = content.replace(old_offline, new_offline, 1)
elif "demo data is never substituted automatically" not in content:
    raise RuntimeError("Backend fallback-state anchor is missing")

old_snapshot = '''  return {
    ...snapshot,
    currentSurface: validateSurfaceSpec(snapshot.currentSurface),
  };
}'''
new_snapshot = '''  return validateWorkspaceSnapshot(snapshot);
}'''
if old_snapshot in content:
    content = content.replace(old_snapshot, new_snapshot, 1)
elif "return validateWorkspaceSnapshot(snapshot);" not in content:
    raise RuntimeError("Snapshot validation anchor is missing")

run_pattern = re.compile(
    r'export async function readRunEvents\(runId: string\): Promise<RunEvent\[]> \{.*?\n}\n\nexport async function importCsv',
    re.S,
)
run_replacement = '''export type RunEventReadOptions = {
  afterSequence?: number;
  signal?: AbortSignal;
  onEvent?: (event: RunEvent) => void;
};

export async function readRunEvents(
  runId: string,
  options: RunEventReadOptions = {},
): Promise<RunEvent[]> {
  const query = options.afterSequence && options.afterSequence > 0
    ? `?afterSequence=${encodeURIComponent(options.afterSequence)}`
    : "";
  const response = await fetch(
    `${API_URL}/v1/jobs/${encodeURIComponent(runId)}/events${query}`,
    {
      signal: options.signal,
      headers: {
        Accept: "text/event-stream",
        ...folioSessionHeaders(),
      },
    },
  );
  if (!response.ok) throw await apiProblemFromResponse(response);

  const events: RunEvent[] = [];
  const decoder = new SseDecoder();
  const consume = (values: string[]) => {
    values.forEach((data) => {
      const event = validateRunEvent(JSON.parse(data) as unknown);
      events.push(event);
      options.onEvent?.(event);
    });
  };

  if (!response.body) {
    consume(decoder.push(await response.text()));
    consume(decoder.flush());
    return events;
  }

  const reader = response.body.getReader();
  const text = new TextDecoder();
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    consume(decoder.push(text.decode(value, { stream: true })));
  }
  consume(decoder.push(text.decode()));
  consume(decoder.flush());
  return events;
}

export async function importCsv'''
content, count = run_pattern.subn(run_replacement, content, count=1)
if count != 1:
    raise RuntimeError("readRunEvents replacement failed")

content = content.replace(
    '    headers: { Accept: "application/json" },\n    body: form,',
    '    headers: { Accept: "application/json", ...folioSessionHeaders() },\n    body: form,',
)
content = content.replace(
    '  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);\n  return (await response.json()) as Record<string, unknown>;\n}\n\nexport async function openArtifact',
    '  if (!response.ok) throw await apiProblemFromResponse(response);\n  return (await response.json()) as Record<string, unknown>;\n}\n\nexport async function openArtifact',
)
write(transport_path, content)

write(
    "apps/desktop/tests/protocol.test.ts",
    '''import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  ApiProblem,
  SseDecoder,
  apiProblemFromResponse,
  retryDelayMs,
  shouldRetryRequest,
} from "../src/protocol.js";
import { validateWorkspaceSnapshot } from "../src/types.js";

function fixture(): unknown {
  const path = fileURLToPath(
    new URL("../../../../fixtures/ui/workspace-snapshot.json", import.meta.url),
  );
  return JSON.parse(readFileSync(path, "utf8"));
}

test("validates the complete committed workspace snapshot", () => {
  const snapshot = validateWorkspaceSnapshot(fixture());
  assert.equal(snapshot.snapshotVersion, "api.snapshot@1");

  const invalid = structuredClone(snapshot) as unknown as {
    totals: { currentBalanceMinor: unknown };
  };
  invalid.totals.currentBalanceMinor = "504576";
  assert.throws(() => validateWorkspaceSnapshot(invalid), /safe integer/);
});

test("decodes SSE frames split across network chunks", () => {
  const decoder = new SseDecoder();
  assert.deepEqual(
    decoder.push("event: message\\ndata: {\\\"sequence\\\":1"),
    [],
  );
  assert.deepEqual(
    decoder.push("}\\n\\ndata: line one\\ndata: line two\\n\\n"),
    ['{"sequence":1}', "line one\\nline two"],
  );
  assert.deepEqual(decoder.flush(), []);
});

test("retries only safe transient requests", () => {
  assert.equal(shouldRetryRequest("GET", 503), true);
  assert.equal(shouldRetryRequest("GET", null, true), true);
  assert.equal(shouldRetryRequest("POST", 503), false);
  assert.equal(shouldRetryRequest("GET", 422), false);
  assert.equal(retryDelayMs(0, "2", 0, 0), 2000);
});

test("parses RFC problem details and preserves retryability", async () => {
  const response = new Response(JSON.stringify({
    type: "https://folio.local/problems/upstream-timeout",
    title: "Gateway Timeout",
    status: 504,
    detail: "The bank did not respond in time.",
    code: "upstream_timeout",
    retryable: true,
  }), {
    status: 504,
    headers: { "Content-Type": "application/problem+json" },
  });
  const problem = await apiProblemFromResponse(response);
  assert.ok(problem instanceof ApiProblem);
  assert.equal(problem.code, "upstream_timeout");
  assert.equal(problem.retryable, true);
});
''',
)

write(
    "apps/desktop/tsconfig.tests.json",
    '''{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2022", "DOM", "DOM.Iterable"],
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "types": ["node"],
    "rootDir": ".",
    "outDir": "dist-tests",
    "noEmit": false,
    "allowImportingTsExtensions": false
  },
  "include": ["src/protocol.ts", "src/types.ts", "tests/protocol.test.ts"]
}
''',
)

package_path = ROOT / "apps/desktop/package.json"
package = json.loads(package_path.read_text(encoding="utf-8"))
scripts = package.setdefault("scripts", {})
old_test = scripts.get("test", "node --test tests/*.test.mjs")
if old_test != "pnpm test:electron && pnpm test:protocol":
    scripts["test:electron"] = old_test
scripts["build:test"] = "tsc -p tsconfig.tests.json"
scripts["test:protocol"] = (
    "pnpm build:test && node --test dist-tests/tests/protocol.test.js"
)
scripts["test"] = "pnpm test:electron && pnpm test:protocol"
package_path.write_text(json.dumps(package, indent=2) + "\n", encoding="utf-8")

gitignore = read(".gitignore")
if "apps/desktop/dist-tests/" not in gitignore:
    gitignore += "\napps/desktop/dist-tests/\n"
write(".gitignore", gitignore)

audit_path = "docs/AUDIT_PROGRAMME.md"
audit = read(audit_path)
marker = "## Stack 3: client protocol and failure truth"
if marker not in audit:
    audit += '''

## Stack 3: client protocol and failure truth

This stack implements the reviewable protocol subset before cancellable background execution:

- RFC 9457 problem details for HTTP, validation and missing-resource failures;
- typed client errors with safe GET retry policy and bounded backoff;
- complete workspace snapshot validation rather than surface-only validation;
- incremental SSE parsing across arbitrary network chunk boundaries;
- session-authenticated event and CSV requests;
- honest degraded/offline states with no automatic fixture substitution;
- pure TypeScript protocol tests included in the desktop verification gate.

Persistent event replay and committed cancellation receipts remain in the next stack because they change the run lifecycle and storage authority together.
'''
write(audit_path, audit)

print("Applied desktop client-protocol stack")
