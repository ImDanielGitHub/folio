import assert from "node:assert/strict";
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
import {
  canOpenSealedDemo,
  initialOnboardingVisible,
  onboardingVisibleAfterProbe,
} from "../src/runtimePolicy.js";

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
  invalid.totals.currentBalanceMinor = Number.MAX_SAFE_INTEGER + 1;
  assert.throws(() => validateWorkspaceSnapshot(invalid), /safe integer/);
});

test("decodes SSE frames split across network chunks", () => {
  const decoder = new SseDecoder();
  assert.deepEqual(
    decoder.push("event: message\ndata: {\"sequence\":1"),
    [],
  );
  assert.deepEqual(
    decoder.push("}\n\ndata: line one\ndata: line two\n\n"),
    ['{"sequence":1}', "line one\nline two"],
  );
  assert.deepEqual(decoder.flush(), []);
});

test("retries only safe transient requests", () => {
  assert.equal(shouldRetryRequest("GET", 503), true);
  assert.equal(shouldRetryRequest("GET", null, true), true);
  assert.equal(shouldRetryRequest("POST", 503), false);
  assert.equal(shouldRetryRequest("GET", 422), false);
  assert.equal(retryDelayMs(0, "2", 0, 0), 2000);
  assert.equal(retryDelayMs(-4, null, 0, 0), 200);
  assert.equal(retryDelayMs(999, null, 0, 0), 1600);
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
  assert.equal(problem.provider, undefined);
});

test("preserves safe provider metadata and rejects malformed validation arrays", async () => {
  const response = new Response(JSON.stringify({
    type: "https://folio.local/problems/provider-rate-limited",
    title: "Service Unavailable",
    status: 503,
    detail: "Plaid rate limited the request",
    code: "provider_rate_limited",
    retryable: true,
    provider: "plaid",
    errors: [{ location: "body", message: "bad", kind: "invalid" }],
  }), { status: 503 });
  const problem = await apiProblemFromResponse(response);
  assert.equal(problem.provider, "plaid");
  assert.equal(problem.problem.errors, undefined);
});

test("bounds incomplete SSE frames", () => {
  const decoder = new SseDecoder();
  assert.throws(() => decoder.push("x".repeat(1_000_001)), /bounded client buffer/);
  assert.deepEqual(decoder.flush(), []);
});

test("requires an explicit source choice when no authoritative snapshot exists", () => {
  assert.equal(initialOnboardingVisible(false, false), true);
  assert.equal(initialOnboardingVisible(true, false), false);
  assert.equal(onboardingVisibleAfterProbe({
    mode: "live",
    explicitDemo: false,
    forcedOnboarding: false,
    rememberedOnboarding: false,
    hasAuthoritativeSnapshot: true,
  }), true);
  assert.equal(onboardingVisibleAfterProbe({
    mode: "degraded",
    explicitDemo: false,
    forcedOnboarding: false,
    rememberedOnboarding: true,
    hasAuthoritativeSnapshot: false,
  }), true);
  assert.equal(onboardingVisibleAfterProbe({
    mode: "offline",
    explicitDemo: false,
    forcedOnboarding: false,
    rememberedOnboarding: true,
    hasAuthoritativeSnapshot: true,
  }), false);
  assert.equal(canOpenSealedDemo("degraded"), true);
  assert.equal(canOpenSealedDemo("checking"), false);
});
