/** Authenticated, bounded downloads. A digest proves transport integrity, not document safety. */
export const MAX_ARTIFACT_BYTES = 10_000_000;
export type DownloadedArtifact = {
  bytes: Uint8Array<ArrayBuffer>;
  mediaType: "application/pdf" | "text/html";
  extension: "pdf" | "html";
  contentHash: string;
};

export function isValidArtifactId(value: unknown): value is string {
  return typeof value === "string" && /^[a-z][a-z0-9]{1,15}_[a-z0-9][a-z0-9_]{2,95}$/.test(value);
}

function checkedApiBase(base: string): string {
  if (base === "/api") return base;
  const url = new URL(base);
  if (url.protocol !== "http:" || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)
    || url.username || url.password || url.search || url.hash || url.pathname !== "/") {
    throw new Error("Artefacts require the configured loopback service");
  }
  return url.origin;
}

export async function downloadArtifact(
  artifactId: string,
  apiBase: string,
  sessionToken?: string,
  fetcher: typeof fetch = fetch,
): Promise<DownloadedArtifact> {
  if (!isValidArtifactId(artifactId)) throw new Error("Invalid artefact identifier");
  const url = `${checkedApiBase(apiBase)}/v1/artifacts/${artifactId}`;
  const abort = new AbortController();
  const timeout = setTimeout(() => abort.abort(), 20_000);
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  try {
    const response = await fetcher(url, {
      headers: {
        Accept: "application/pdf, text/html",
        ...(sessionToken ? { "X-Folio-Session": sessionToken } : {}),
      },
      signal: abort.signal,
      redirect: "error",
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`Artefact download failed (${response.status})`);
    const mediaType = response.headers.get("Content-Type")?.split(";", 1)[0]?.trim();
    if (mediaType !== "application/pdf" && mediaType !== "text/html") {
      throw new Error("Unsupported artefact media type");
    }
    const contentHash = /^"([a-f0-9]{64})"$/.exec(response.headers.get("ETag") ?? "")?.[1];
    if (!contentHash) throw new Error("Missing artefact integrity receipt");
    const rawLength = response.headers.get("Content-Length");
    const declaredLength = rawLength === null ? null : Number(rawLength);
    if (rawLength !== null && (!/^\d+$/.test(rawLength)
      || !Number.isSafeInteger(declaredLength) || declaredLength! > MAX_ARTIFACT_BYTES)) {
      throw new Error("Artefact exceeds the download limit");
    }
    if (!response.body) throw new Error("Empty artefact response");
    reader = response.body.getReader();
    const chunks: Uint8Array[] = [];
    let size = 0;
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_ARTIFACT_BYTES) throw new Error("Artefact exceeds the download limit");
      chunks.push(value);
    }
    if (!size || (declaredLength !== null && size !== declaredLength)) {
      throw new Error("Artefact length does not match the response");
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", bytes));
    const actualHash = Array.from(digest, (byte) => byte.toString(16).padStart(2, "0")).join("");
    if (actualHash !== contentHash) throw new Error("Artefact integrity check failed");
    if (mediaType === "application/pdf" && new TextDecoder().decode(bytes.slice(0, 5)) !== "%PDF-") {
      throw new Error("Artefact is not a PDF document");
    }
    return { bytes, mediaType, extension: mediaType === "application/pdf" ? "pdf" : "html", contentHash };
  } finally {
    clearTimeout(timeout);
    abort.abort();
    if (reader) {
      await reader.cancel().catch(() => undefined);
      reader.releaseLock();
    }
  }
}
