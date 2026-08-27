import { validateRunEvent, validateWorkspaceSnapshot, type RunEvent, type WorkspaceSnapshot } from "./types";
import { ApiProblem, SseDecoder, apiProblemFromResponse, retryDelayMs, shouldRetryRequest } from "./protocol";

export type RuntimeMode = "checking" | "live" | "fixture" | "offline" | "degraded";

export type BackendHealth = {
  mode: RuntimeMode;
  label: string;
  detail: string;
  apiUrl: string;
  lmStudioReady: boolean;
  lmStudioStatus: string;
  cloudReady: boolean;
  cloudCredentialState: "absent" | "configured";
  akahuReady: boolean;
  akahuStatus: "configured" | "unconfigured";
  akahuDetail: string;
  plaidReady: boolean;
  plaidStatus: "configured" | "unconfigured";
  plaidDetail: string;
};

const LOOPBACK_API_URL = window.financeDesktop?.apiBase ?? "http://127.0.0.1:8787";
const SESSION_TOKEN = window.financeDesktop?.sessionToken ?? import.meta.env.VITE_FOLIO_SESSION_TOKEN;
const sessionHeaders = (): Record<string, string> => SESSION_TOKEN
  ? { "X-Folio-Session": SESSION_TOKEN }
  : {};

const API_URL = (
  import.meta.env.VITE_FINANCE_API_URL ?? (import.meta.env.DEV ? "/api" : LOOPBACK_API_URL)
).replace(/\/$/, "");

const wait = (milliseconds: number) => new Promise<void>((resolve) => {
  window.setTimeout(resolve, milliseconds);
});

async function withTimeout<T>(work: (signal: AbortSignal) => Promise<T>, timeoutMs = 1000): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await work(controller.signal);
  } finally {
    window.clearTimeout(timeout);
  }
}

async function requestJson<T>(path: string, init?: RequestInit, timeoutMs = 2400): Promise<T> {
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
        Object.entries(sessionHeaders()).forEach(([key, value]) => {
          headers.set(key, value);
        });
        return fetch(`${API_URL}${path}`, { ...init, signal, headers });
      }, timeoutMs);
      if (response.ok) return (await response.json()) as T;

      const problem = await apiProblemFromResponse(response);
      if (
        attempt + 1 >= maximumAttempts
        || !shouldRetryRequest(
          method, response.status, false, problem.retryable,
        )
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

export async function probeBackend(): Promise<BackendHealth> {
  try {
    await requestJson<Record<string, unknown>>("/health", undefined, 900);
    const [capability, connections] = await Promise.all([
      requestJson<Record<string, unknown>>("/v1/models/capabilities", undefined, 3200),
      requestJson<Record<string, unknown>>("/v1/connections/capabilities", undefined, 3200),
    ]);
    const modes = (capability.modes ?? {}) as Record<string, unknown>;
    const local = (modes.local ?? {}) as Record<string, unknown>;
    const cloud = (modes.cloud ?? {}) as Record<string, unknown>;
    const localStatus = typeof local.status === "string" ? local.status : "unavailable";
    const cloudStatus = typeof cloud.status === "string" ? cloud.status : "unavailable";
    const providers = (connections.providers ?? {}) as Record<string, unknown>;
    const akahu = (providers.akahu ?? {}) as Record<string, unknown>;
    const akahuStatus = akahu.status === "configured" ? "configured" : "unconfigured";
    const plaid = (providers.plaid ?? {}) as Record<string, unknown>;
    const plaidStatus = plaid.status === "configured" ? "configured" : "unconfigured";
    return {
      mode: "live",
      label: "Local service connected",
      detail: "Deterministic finance truth is served from 127.0.0.1.",
      apiUrl: LOOPBACK_API_URL,
      lmStudioReady: localStatus === "ready",
      lmStudioStatus: localStatus,
      cloudReady: cloudStatus === "ready",
      cloudCredentialState: capability.cloudCredentialState === "configured" ? "configured" : "absent",
      akahuReady: akahuStatus === "configured",
      akahuStatus,
      akahuDetail: typeof akahu.detail === "string" ? akahu.detail : "Live Akahu is not configured for this process.",
      plaidReady: plaidStatus === "configured",
      plaidStatus,
      plaidDetail: typeof plaid.detail === "string" ? plaid.detail : "Live Plaid is not configured for this process.",
    };
  } catch {
    return {
      mode: navigator.onLine ? "degraded" : "offline",
      label: navigator.onLine ? "Local service unavailable" : "Folio is offline",
      detail: navigator.onLine
        ? "The local finance service is not running. The last committed view remains visible; demo data is never substituted automatically."
        : "This computer is offline. The last committed local view remains visible.",
      apiUrl: LOOPBACK_API_URL,
      lmStudioReady: false,
      lmStudioStatus: "not checked",
      cloudReady: false,
      cloudCredentialState: "absent",
      akahuReady: false,
      akahuStatus: "unconfigured",
      akahuDetail: "Start the local Folio service to inspect Akahu configuration.",
      plaidReady: false,
      plaidStatus: "unconfigured",
      plaidDetail: "Start the local Folio service to inspect Plaid configuration.",
    };
  }
}

export async function loadSnapshot(workspaceId: string): Promise<WorkspaceSnapshot> {
  const snapshot = await requestJson<WorkspaceSnapshot>(`/v1/workspaces/${workspaceId}/snapshot`, undefined, 2800);
  return validateWorkspaceSnapshot(snapshot);
}

export async function resetDemo(): Promise<{ workspaceId?: string }> {
  return requestJson(
    "/v1/demo/reset",
    {
      method: "POST",
      body: JSON.stringify({ workspaceId: "ws_koru_studio" }),
    },
    6000,
  );
}

export async function runDailyClose(): Promise<{ runId: string }> {
  return requestJson("/v1/jobs/daily-close", {
    method: "POST",
    body: JSON.stringify({
      workspaceId: "ws_koru_studio",
      idempotencyKey: `desktop-close-${Date.now()}`,
    }),
  });
}

export async function postTurn(
  threadId: string,
  content: string,
  mode: "local" | "hybrid" | "cloud",
): Promise<{ runId: string }> {
  // Local LM Studio turns routinely take 30–120s; keep the request alive for demo recording.
  return requestJson(`/v1/threads/${threadId}/turns`, {
    method: "POST",
    body: JSON.stringify({
      workspaceId: "ws_koru_studio",
      turnId: `turn_desktop_${Date.now().toString(36)}`,
      content,
      mode,
    }),
  }, 180_000);
}

export async function undoEvent(eventId: string): Promise<Record<string, unknown>> {
  return requestJson(`/v1/events/${eventId}/undo`, {
    method: "POST",
    body: JSON.stringify({
      requestId: `undo_desktop_${Date.now().toString(36)}`,
      eventId,
      actor: "owner",
      reason: "Owner selected Undo from Activity & Undo.",
    }),
  });
}

export async function ingestTelegramFixture(): Promise<Record<string, unknown>> {
  return requestJson("/v1/ingest/telegram-fixture", {
    method: "POST",
    body: JSON.stringify({
      update: {
        update_id: 910001,
        message: {
          message_id: 501,
          date: 1784214000,
          from: { id: 700001, is_bot: false, first_name: "Koru Owner", language_code: "en" },
          chat: { id: 700001, type: "private", first_name: "Koru Owner" },
          caption: "Parking for the client fit-out, NZD 32.40 including GST.",
          photo: [{
            file_id: "fixture_photo_koru_parking_001",
            file_unique_id: "fixture_unique_koru_parking_001",
            width: 1280,
            height: 960,
            file_size: 48213,
          }],
        },
      },
      attachmentReference: {
        fixtureVersion: "telegram.attachment@1",
        sourceItemId: "src_koru_telegram_910001",
        attachmentId: "att_koru_parking_receipt_001",
        telegramFileId: "fixture_photo_koru_parking_001",
        mediaType: "image/jpeg",
        localFixturePath: null,
        captionAmountMinor: -3240,
        currency: "NZD",
        receiptOcrRequired: false,
        synthetic: true,
      },
    }),
  });
}

export type AkahuFixturePayload = {
  account?: { name: string; maskedNumber?: string; currency?: "NZD" };
  syncedAt?: string;
  transactions?: Array<Record<string, unknown>>;
};

export async function ingestAkahuFixture(
  payload: AkahuFixturePayload = {},
): Promise<Record<string, unknown>> {
  return requestJson("/v1/ingest/akahu-fixture", {
    method: "POST",
    body: JSON.stringify(payload),
  }, 8000);
}

export async function syncAkahuLive(): Promise<Record<string, unknown>> {
  return requestJson("/v1/connectors/akahu/sync", {
    method: "POST",
    body: JSON.stringify({}),
  }, 30000);
}

export type PlaidFixturePayload = {
  account?: { name: string; maskedNumber?: string; currency?: "USD" };
  syncedAt?: string;
  transactions?: Array<Record<string, unknown>>;
};

export async function ingestPlaidFixture(
  payload: PlaidFixturePayload = {},
): Promise<Record<string, unknown>> {
  return requestJson("/v1/ingest/plaid-fixture", {
    method: "POST",
    body: JSON.stringify(payload),
  }, 8000);
}

export async function syncPlaidLive(publicToken?: string): Promise<Record<string, unknown>> {
  return requestJson("/v1/connectors/plaid/sync", {
    method: "POST",
    body: JSON.stringify(publicToken ? { publicToken } : {}),
  }, 30000);
}

export async function createPlaidLinkToken(): Promise<Record<string, unknown>> {
  return requestJson("/v1/connectors/plaid/link-token", {
    method: "POST",
    body: JSON.stringify({}),
  }, 12000);
}

export async function loadConnectionCapabilities(): Promise<Record<string, unknown>> {
  return requestJson("/v1/connections/capabilities", undefined, 3200);
}

export type RunEventReadOptions = {
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
        ...sessionHeaders(),
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

export async function importCsv(file: File): Promise<Record<string, unknown>> {
  const form = new FormData();
  form.set("workspaceId", "ws_koru_studio");
  form.set("file", file, file.name);
  const response = await fetch(`${API_URL}/v1/ingest/csv`, {
    method: "POST",
    headers: { Accept: "application/json", ...sessionHeaders() },
    body: form,
  });
  if (!response.ok) throw await apiProblemFromResponse(response);
  return (await response.json()) as Record<string, unknown>;
}

export async function openArtifact(artifactId: string): Promise<void> {
  if (!/^[a-z][a-z0-9_]{2,95}$/.test(artifactId)) throw new Error("Invalid artifact identifier");
  if (window.financeDesktop) {
    await window.financeDesktop.openArtifact(artifactId);
    return;
  }
  window.open(`${API_URL}/v1/artifacts/${artifactId}`, "_blank", "noopener,noreferrer");
}

export { API_URL };
