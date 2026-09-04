from __future__ import annotations

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


def replace_once(path: str, old: str, new: str) -> None:
    content = read(path)
    count = content.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:120]!r}")
    write(path, content.replace(old, new, 1))


TRANSPORT_ERRORS = '''export type ApiProblem = {
  type?: string;
  title?: string;
  status?: number;
  detail?: string;
  code?: string;
  retryable?: boolean;
  requestId?: string;
};

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryable: boolean;
  readonly requestId?: string;
  readonly problem: ApiProblem;

  constructor(responseStatus: number, problem: ApiProblem) {
    super(problem.detail || problem.title || `Request failed with status ${responseStatus}`);
    this.name = "ApiError";
    this.status = responseStatus;
    this.code = problem.code || "http_error";
    this.retryable = problem.retryable ?? responseStatus >= 500;
    this.requestId = problem.requestId;
    this.problem = problem;
  }
}

export async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  let problem: ApiProblem = {};
  try {
    const value = await response.clone().json();
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const record = value as Record<string, unknown>;
      const nested = record.detail;
      problem = nested && typeof nested === "object" && !Array.isArray(nested)
        ? nested as ApiProblem
        : record as ApiProblem;
    }
  } catch {
    problem = {};
  }
  if (!problem.detail && response.statusText) problem.detail = response.statusText;
  if (!problem.status) problem.status = response.status;
  return new ApiError(response.status, problem);
}

export function describeApiError(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback;
  const request = error.requestId ? ` Request ${error.requestId}.` : "";
  return `${error.message}${request}`;
}
'''

SNAPSHOT_VALIDATOR = '''

const modelModes: ReadonlySet<string> = new Set(["local", "hybrid", "cloud"]);
const turnStatuses: ReadonlySet<string> = new Set(["complete", "streaming", "stopped"]);
const activityStatuses: ReadonlySet<string> = new Set(["queued", "running", "completed", "undone", "failed"]);
const sourceStatuses: ReadonlySet<string> = new Set(["pending", "processed", "failed"]);
const sourceTypes: ReadonlySet<string> = new Set(["csv", "telegram_fixture", "owner_claim", "akahu_fixture", "plaid_fixture"]);
const freshnessStatuses: ReadonlySet<string> = new Set(["current", "stale", "partial"]);

function requiredInteger(value: unknown, label: string): asserts value is number {
  if (typeof value !== "number" || !Number.isSafeInteger(value)) {
    throw new Error(`${label} must be a safe integer.`);
  }
}

function oneOf(value: unknown, allowed: ReadonlySet<string>, label: string): asserts value is string {
  if (typeof value !== "string" || !allowed.has(value)) {
    throw new Error(`${label} is outside the closed contract.`);
  }
}

function validateFreshness(value: unknown): void {
  const freshness = record(value, "freshness");
  exact(freshness, ["dataThrough", "status", "timezone"], "freshness");
  requiredString(freshness.dataThrough, "freshness dataThrough");
  requiredString(freshness.timezone, "freshness timezone");
  oneOf(freshness.status, freshnessStatuses, "freshness status");
}

function assertMinorUnits(value: unknown, path = "snapshot"): void {
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertMinorUnits(item, `${path}[${index}]`));
    return;
  }
  if (!value || typeof value !== "object") return;
  Object.entries(value as Record<string, unknown>).forEach(([key, child]) => {
    if (key.endsWith("Minor")) requiredInteger(child, `${path}.${key}`);
    assertMinorUnits(child, `${path}.${key}`);
  });
}

export function validateWorkspaceSnapshot(value: unknown): WorkspaceSnapshot {
  const snapshot = record(value, "workspace snapshot");
  exact(snapshot, [
    "snapshotVersion", "snapshotId", "workspace", "thread", "currentSurface",
    "findings", "activity", "sources", "totals", "artifacts", "modelMode", "freshness",
  ], "workspace snapshot");
  if (snapshot.snapshotVersion !== "api.snapshot@1") throw new Error("Unsupported workspace snapshot version.");
  requiredString(snapshot.snapshotId, "snapshotId");

  const workspace = record(snapshot.workspace, "workspace");
  exact(workspace, ["workspaceId", "name", "entityType", "currency", "timezone", "protectedReserveMinor"], "workspace");
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
    requiredString(turn.turnId, "turnId");
    oneOf(turn.role, new Set(["agent", "owner"]), "turn role");
    requiredString(turn.content, "turn content");
    requiredString(turn.occurredAt, "turn occurredAt");
    oneOf(turn.status, turnStatuses, "turn status");
    strings(turn.evidenceIds, "turn evidenceIds");
  });
  if (thread.activeQuestion !== null) {
    const question = record(thread.activeQuestion, "active question");
    requiredString(question.questionId, "questionId");
    requiredString(question.prompt, "question prompt");
  }

  validateSurfaceSpec(snapshot.currentSurface);
  if (!Array.isArray(snapshot.findings)) throw new Error("findings must be an array");
  snapshot.findings.forEach((item) => {
    const finding = record(item, "finding");
    requiredString(finding.findingId, "findingId");
    requiredString(finding.kind, "finding kind");
    requiredString(finding.title, "finding title");
    requiredString(finding.summary, "finding summary");
    strings(finding.evidenceIds, "finding evidenceIds");
  });

  if (!Array.isArray(snapshot.activity)) throw new Error("activity must be an array");
  snapshot.activity.forEach((item) => {
    const activity = record(item, "activity item");
    requiredString(activity.activityId, "activityId");
    requiredString(activity.kind, "activity kind");
    requiredString(activity.summary, "activity summary");
    oneOf(activity.status, activityStatuses, "activity status");
    requiredString(activity.occurredAt, "activity occurredAt");
    if (typeof activity.undoable !== "boolean") throw new Error("activity undoable must be boolean");
    strings(activity.evidenceIds, "activity evidenceIds");
  });

  if (!Array.isArray(snapshot.sources)) throw new Error("sources must be an array");
  snapshot.sources.forEach((item) => {
    const source = record(item, "source item");
    requiredString(source.sourceItemId, "sourceItemId");
    oneOf(source.sourceType, sourceTypes, "source type");
    requiredString(source.label, "source label");
    requiredString(source.receivedAt, "source receivedAt");
    oneOf(source.status, sourceStatuses, "source status");
    requiredInteger(source.rowCount, "source rowCount");
  });

  const totals = record(snapshot.totals, "totals");
  exact(totals, [
    "asOf", "currency", "currentBalanceMinor", "protectedReserveMinor",
    "businessIncomeMinor", "businessExpenseMinor", "personalExpenseMinor",
    "unresolvedExpenseMinor", "projectedLowPointMinor", "reserveShortfallMinor",
  ], "totals");
  requiredString(totals.asOf, "totals asOf");
  requiredString(totals.currency, "totals currency");

  if (!Array.isArray(snapshot.artifacts)) throw new Error("artifacts must be an array");
  snapshot.artifacts.forEach((item) => {
    const artifact = record(item, "artifact");
    requiredString(artifact.artifactId, "artifactId");
    requiredString(artifact.kind, "artifact kind");
    requiredString(artifact.title, "artifact title");
    requiredString(artifact.contentHash, "artifact contentHash");
    requiredString(artifact.generatedAt, "artifact generatedAt");
    strings(artifact.evidenceIds, "artifact evidenceIds");
  });
  oneOf(snapshot.modelMode, modelModes, "model mode");
  validateFreshness(snapshot.freshness);
  assertMinorUnits(snapshot);
  return snapshot as unknown as WorkspaceSnapshot;
}
'''

TESTS = '''import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { ApiError, apiErrorFromResponse, describeApiError } from "../../dist-test/transportErrors.js";
import { validateWorkspaceSnapshot } from "../../dist-test/types.js";

const fixture = JSON.parse(
  await readFile(new URL("../../../../fixtures/ui/workspace-snapshot.json", import.meta.url), "utf8"),
);

test("the committed workspace fixture passes the complete runtime validator", () => {
  const value = validateWorkspaceSnapshot(fixture);
  assert.equal(value.snapshotVersion, "api.snapshot@1");
});

test("unsafe minor-unit values are blocked before rendering", () => {
  const value = structuredClone(fixture);
  value.totals.currentBalanceMinor = 1.25;
  assert.throws(() => validateWorkspaceSnapshot(value), /safe integer/);
});

test("unknown nested source states are blocked", () => {
  const value = structuredClone(fixture);
  value.sources[0].status = "maybe";
  assert.throws(() => validateWorkspaceSnapshot(value), /source status/);
});

test("problem details survive transport error conversion", async () => {
  const response = new Response(JSON.stringify({
    type: "https://folio.local/problems/provider_unconfigured",
    title: "Provider Unconfigured",
    status: 409,
    detail: "Plaid is disabled or unconfigured",
    code: "provider_unconfigured",
    retryable: false,
    requestId: "req_problem_12345",
  }), {
    status: 409,
    headers: { "Content-Type": "application/problem+json" },
  });
  const error = await apiErrorFromResponse(response);
  assert.ok(error instanceof ApiError);
  assert.equal(error.code, "provider_unconfigured");
  assert.equal(error.retryable, false);
  assert.equal(error.requestId, "req_problem_12345");
  assert.match(describeApiError(error, "fallback"), /req_problem_12345/);
});
'''

TSCONFIG = '''{
  "extends": "../../tsconfig.base.json",
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "allowImportingTsExtensions": false,
    "noEmit": false,
    "outDir": "dist-test",
    "rootDir": "src",
    "types": ["node"]
  },
  "include": ["src/types.ts", "src/transportErrors.ts"]
}
'''


def add_complete_snapshot_validation() -> None:
    path = "apps/desktop/src/types.ts"
    content = read(path)
    marker = "\nexport function validateRunEvent(value: unknown): RunEvent {"
    if marker not in content:
        raise RuntimeError("validateRunEvent marker missing")
    if "export function validateWorkspaceSnapshot" not in content:
        content = content.replace(marker, SNAPSHOT_VALIDATOR + marker, 1)
    content = content.replace(
        "    const snapshot = record(payload.snapshot, `${event.type} snapshot`);\n    validateSurfaceSpec(snapshot.currentSurface);",
        "    validateWorkspaceSnapshot(payload.snapshot);",
        1,
    )
    write(path, content)


def add_structured_transport_errors() -> None:
    write("apps/desktop/src/transportErrors.ts", TRANSPORT_ERRORS)
    path = "apps/desktop/src/transport.ts"
    content = read(path)
    content = content.replace(
        'import { validateRunEvent, validateSurfaceSpec, type RunEvent, type WorkspaceSnapshot } from "./types";',
        'import { validateRunEvent, validateWorkspaceSnapshot, type RunEvent, type WorkspaceSnapshot } from "./types";\nimport { apiErrorFromResponse, describeApiError } from "./transportErrors";',
        1,
    )
    content = content.replace(
        "    if (!response.ok) {\n      throw new Error(`${response.status} ${response.statusText}`);\n    }",
        "    if (!response.ok) {\n      throw await apiErrorFromResponse(response);\n    }",
        1,
    )
    old = '''export async function loadSnapshot(workspaceId: string): Promise<WorkspaceSnapshot> {\n  const snapshot = await requestJson<WorkspaceSnapshot>(`/v1/workspaces/${workspaceId}/snapshot`, undefined, 2800);\n  return {\n    ...snapshot,\n    currentSurface: validateSurfaceSpec(snapshot.currentSurface),\n  };\n}\n'''
    new = '''export async function loadSnapshot(workspaceId: string): Promise<WorkspaceSnapshot> {\n  const snapshot = await requestJson<unknown>(`/v1/workspaces/${workspaceId}/snapshot`, undefined, 2800);\n  return validateWorkspaceSnapshot(snapshot);\n}\n'''
    if old not in content:
        raise RuntimeError("loadSnapshot block changed")
    content = content.replace(old, new, 1)
    content = content.replace(
        '''  } catch {\n    return {\n      mode: navigator.onLine ? "fixture" : "offline",\n      label: navigator.onLine ? "Demo data" : "Offline demo",\n      detail: navigator.onLine\n        ? "The local finance service is not running, so Folio has opened the sample business."\n        : "The app is offline. Existing local data remains available.",''',
        '''  } catch (error) {\n    return {\n      mode: navigator.onLine ? "degraded" : "offline",\n      label: navigator.onLine ? "Local service unavailable" : "Offline",\n      detail: navigator.onLine\n        ? describeApiError(error, "The local finance service could not be reached. No demo workspace was substituted.")\n        : "The app is offline. The last committed view remains visible; no demo workspace was substituted.",''',
        1,
    )
    content = content.replace(
        '  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);',
        '  if (!response.ok) throw await apiErrorFromResponse(response);',
    )
    write(path, content)


def make_demo_fallback_explicit() -> None:
    path = "apps/desktop/src/App.tsx"
    content = read(path)
    marker = '''    if (backend.mode !== "fixture") {\n      throw new Error("Folio could not verify a ready local workspace. Nothing was saved or substituted; keep this setup open and try again when the local service is available.");\n    }\n'''
    explicit = '''    if (sourceChoice === "demo") {\n      applySnapshot(workspaceFixture);\n      setBackend({\n        mode: "fixture",\n        label: "Explicit sample business",\n        detail: "You chose the sealed fictional workspace. It was not substituted for a failed live service.",\n        apiUrl: "http://127.0.0.1:8787",\n        lmStudioReady: false,\n        lmStudioStatus: "not checked",\n        cloudReady: false,\n        cloudCredentialState: "absent",\n        akahuReady: false,\n        akahuStatus: "unconfigured",\n        akahuDetail: "The explicit sample makes no Akahu request.",\n        plaidReady: false,\n        plaidStatus: "unconfigured",\n        plaidDetail: "The explicit sample makes no Plaid request.",\n      });\n      try {\n        localStorage.setItem("folio:onboarded", "yes");\n      } catch {\n        // The explicit fixture remains usable for this session.\n      }\n      setShowOnboarding(false);\n      showToast("You opened Folio's fictional sample business. No local or external service was substituted.");\n      return;\n    }\n    if (backend.mode !== "fixture") {\n      throw new Error("Folio could not verify a ready local workspace. Nothing was saved or substituted; keep this setup open and try again when the local service is available.");\n    }\n'''
    if marker not in content:
        raise RuntimeError("fixture fallback marker missing")
    content = content.replace(marker, explicit, 1)
    write(path, content)


def configure_renderer_tests() -> None:
    write("apps/desktop/tsconfig.renderer-test.json", TSCONFIG)
    write("apps/desktop/tests/renderer/runtime-validation.test.mjs", TESTS)
    path = "apps/desktop/package.json"
    value = json.loads(read(path))
    value["scripts"]["test"] = (
        "pnpm build:electron && tsc -p tsconfig.renderer-test.json "
        "&& node --test tests/*.test.mjs tests/renderer/*.test.mjs"
    )
    write(path, json.dumps(value, indent=2) + "\n")


def add_docs() -> None:
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 5: renderer contract and failure truth\n\n- The renderer validates the complete workspace snapshot before using it.\n- Every minor-unit value must be a safe integer and nested state enums are closed.\n- API problem details retain code, retryability, and request ID through the transport.\n- A failed local service now produces a degraded state rather than silently opening demo data.\n- The fictional sample remains available only through an explicit owner choice or `?demo=1`.\n- Renderer contract tests run in the permanent desktop verification gate.\n'''
    if "## Stack 5: renderer contract and failure truth" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_complete_snapshot_validation()
    add_structured_transport_errors()
    make_demo_fallback_explicit()
    configure_renderer_tests()
    add_docs()
    print("renderer resilience changes applied")


if __name__ == "__main__":
    main()
