export type ProblemFieldError = {
  location: string[];
  message: string;
  kind: string;
};

export type ProblemDetails = {
  type: string;
  title: string;
  status: number;
  detail: string;
  code: string;
  retryable: boolean;
  instance?: string;
  provider?: string;
  errors?: ProblemFieldError[];
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

  get provider(): string | undefined {
    return this.problem.provider;
  }
}

const RETRYABLE_STATUSES = new Set([408, 425, 429, 502, 503, 504]);
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);
const MAX_RETRY_DELAY_MS = 10_000;
const MAX_SSE_BUFFER_CHARACTERS = 1_000_000;

function nonEmptyText(value: unknown): string | undefined {
  return typeof value === "string" && value.trim() ? value : undefined;
}

function problemErrors(value: unknown): ProblemFieldError[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const errors: ProblemFieldError[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) return undefined;
    const candidate = item as Record<string, unknown>;
    if (
      !Array.isArray(candidate.location)
      || candidate.location.some((part) => typeof part !== "string")
      || typeof candidate.message !== "string"
      || typeof candidate.kind !== "string"
    ) {
      return undefined;
    }
    errors.push({
      location: candidate.location as string[],
      message: candidate.message,
      kind: candidate.kind,
    });
  }
  return errors;
}

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
    const value = await response.clone().json() as Record<string, unknown>;
    const errors = problemErrors(value.errors);
    const provider = nonEmptyText(value.provider);
    const instance = nonEmptyText(value.instance);
    return new ApiProblem({
      type: nonEmptyText(value.type) ?? fallback.type,
      title: nonEmptyText(value.title) ?? fallback.title,
      status: value.status === response.status ? response.status : fallback.status,
      detail: nonEmptyText(value.detail) ?? fallback.detail,
      code: nonEmptyText(value.code) ?? fallback.code,
      retryable: typeof value.retryable === "boolean"
        ? value.retryable
        : fallback.retryable,
      ...(instance ? { instance } : {}),
      ...(provider ? { provider } : {}),
      ...(errors ? { errors } : {}),
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
      return Math.min(seconds * 1000, MAX_RETRY_DELAY_MS);
    }
    const date = Date.parse(retryAfter);
    if (Number.isFinite(date)) {
      return Math.min(Math.max(0, date - now), MAX_RETRY_DELAY_MS);
    }
  }
  const boundedAttempt = Number.isFinite(attempt)
    ? Math.max(0, Math.min(6, Math.trunc(attempt)))
    : 0;
  const base = Math.min(250 * (2 ** boundedAttempt), 2_000);
  const boundedRandom = Math.max(0, Math.min(1, random));
  return Math.round(base * (0.8 + boundedRandom * 0.4));
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
    if (this.buffer.length > MAX_SSE_BUFFER_CHARACTERS) {
      this.buffer = "";
      throw new Error("SSE frame exceeded the bounded client buffer.");
    }
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
