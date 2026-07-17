import { validateRunEvent, validateSurfaceSpec, type RunEvent, type WorkspaceSnapshot } from "./types";

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
};

const LOOPBACK_API_URL = window.financeDesktop?.apiBase ?? "http://127.0.0.1:8787";
const API_URL = (
  import.meta.env.VITE_FINANCE_API_URL ?? (import.meta.env.DEV ? "/api" : LOOPBACK_API_URL)
).replace(/\/$/, "");

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
  return withTimeout(async (signal) => {
    const response = await fetch(`${API_URL}${path}`, {
      ...init,
      signal,
      headers: {
        Accept: "application/json",
        ...(init?.body ? { "Content-Type": "application/json" } : {}),
        ...init?.headers,
      },
    });
    if (!response.ok) {
      throw new Error(`${response.status} ${response.statusText}`);
    }
    return (await response.json()) as T;
  }, timeoutMs);
}

export async function probeBackend(): Promise<BackendHealth> {
  try {
    await requestJson<Record<string, unknown>>("/health", undefined, 900);
    const capability = await requestJson<Record<string, unknown>>("/v1/models/capabilities", undefined, 3200);
    const modes = (capability.modes ?? {}) as Record<string, unknown>;
    const local = (modes.local ?? {}) as Record<string, unknown>;
    const cloud = (modes.cloud ?? {}) as Record<string, unknown>;
    const localStatus = typeof local.status === "string" ? local.status : "unavailable";
    const cloudStatus = typeof cloud.status === "string" ? cloud.status : "unavailable";
    return {
      mode: "live",
      label: "Local service connected",
      detail: "Deterministic finance truth is served from 127.0.0.1.",
      apiUrl: LOOPBACK_API_URL,
      lmStudioReady: localStatus === "ready",
      lmStudioStatus: localStatus,
      cloudReady: cloudStatus === "ready",
      cloudCredentialState: capability.cloudCredentialState === "configured" ? "configured" : "absent",
    };
  } catch {
    return {
      mode: navigator.onLine ? "fixture" : "offline",
      label: navigator.onLine ? "Demo data" : "Offline demo",
      detail: navigator.onLine
        ? "The local finance service is not running, so the sealed Koru Studio fixture is active."
        : "The app is offline. Existing local data remains available.",
      apiUrl: LOOPBACK_API_URL,
      lmStudioReady: false,
      lmStudioStatus: "not checked",
      cloudReady: false,
      cloudCredentialState: "absent",
    };
  }
}

export async function loadSnapshot(workspaceId: string): Promise<WorkspaceSnapshot> {
  const snapshot = await requestJson<WorkspaceSnapshot>(`/v1/workspaces/${workspaceId}/snapshot`, undefined, 2800);
  return {
    ...snapshot,
    currentSurface: validateSurfaceSpec(snapshot.currentSurface),
  };
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
  return requestJson(`/v1/threads/${threadId}/turns`, {
    method: "POST",
    body: JSON.stringify({
      workspaceId: "ws_koru_studio",
      turnId: `turn_desktop_${Date.now().toString(36)}`,
      content,
      mode,
    }),
  });
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

export async function readRunEvents(runId: string): Promise<RunEvent[]> {
  const response = await fetch(`${API_URL}/v1/jobs/${encodeURIComponent(runId)}/events`, {
    headers: { Accept: "text/event-stream" },
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  const stream = await response.text();
  const events: RunEvent[] = [];
  for (const frame of stream.split(/\r?\n\r?\n/)) {
    const data = frame
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).trimStart())
      .join("\n");
    if (data) events.push(validateRunEvent(JSON.parse(data) as unknown));
  }
  return events;
}

export async function importCsv(file: File): Promise<Record<string, unknown>> {
  const form = new FormData();
  form.set("workspaceId", "ws_koru_studio");
  form.set("file", file, file.name);
  const response = await fetch(`${API_URL}/v1/ingest/csv`, {
    method: "POST",
    headers: { Accept: "application/json" },
    body: form,
  });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
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
