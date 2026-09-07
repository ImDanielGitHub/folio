import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, stat, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { downloadArtifact, MAX_ARTIFACT_BYTES } from "../dist-electron/artifact.js";
import { cacheArtifact } from "../dist-electron/main/artifact-cache.js";

const id = "artifact_koru_owner_pack_pdf";
const pdf = new TextEncoder().encode("%PDF-1.4\nSynthetic content\n%%EOF");
const hash = (data) => createHash("sha256").update(data).digest("hex");
function response(data = pdf, headers = {}, status = 200) {
  return new Response(data, { status, headers: {
    "Content-Type": "application/pdf", ETag: `"${hash(data)}"`, ...headers,
  } });
}
test("downloads with a session header and never places credentials in a URL", async () => {
  const result = await downloadArtifact(id, "http://127.0.0.1:8787", "synthetic-session", async (url, init) => {
    assert.equal(url, `http://127.0.0.1:8787/v1/artifacts/${id}`);
    assert.equal(new Headers(init.headers).get("X-Folio-Session"), "synthetic-session");
    assert.equal(init.redirect, "error");
    assert.equal(init.cache, "no-store");
    return response();
  });
  assert.deepEqual(result.bytes, pdf);
  assert.equal(result.extension, "pdf");
  assert.equal(result.contentHash, hash(pdf));
});
test("relative browser proxy is supported with authenticated HTML download", async () => {
  const bytes = new TextEncoder().encode("<!doctype html><title>Working paper</title>");
  const result = await downloadArtifact(id, "/api", undefined, async (url) => {
    assert.equal(url, `/api/v1/artifacts/${id}`);
    return response(bytes, { "Content-Type": "text/html; charset=utf-8" });
  });
  assert.equal(result.extension, "html");
});
for (const base of ["https://evil.example", "http://127.0.0.1:8787@evil.example", "http://user:pass@localhost:8787", "http://localhost:8787?token=x", "//evil.example", "http://localhost:8787/redirect"]) {
  test(`rejects unsafe API base ${base}`, async () => {
    await assert.rejects(downloadArtifact(id, base, "synthetic", async () => assert.fail("must not fetch")));
  });
}
test("rejects path traversal before network access", async () => {
  await assert.rejects(downloadArtifact("../../passwd", "/api", undefined, async () => assert.fail("must not fetch")));
});
for (const [label, reply] of [
  ["missing hash", () => response(pdf, { ETag: "" })],
  ["wrong hash", () => response(pdf, { ETag: `"${"0".repeat(64)}"` })],
  ["executable MIME", () => response(pdf, { "Content-Type": "application/octet-stream" })],
  ["fake PDF", () => response(new TextEncoder().encode("Not a PDF"))],
  ["length mismatch", () => response(pdf, { "Content-Length": "999" })],
  ["oversized declared body", () => response(pdf, { "Content-Length": String(MAX_ARTIFACT_BYTES + 1) })],
  ["unauthenticated response", () => response(pdf, {}, 401)],
]) {
  test(`rejects ${label}`, async () => {
    await assert.rejects(downloadArtifact(id, "/api", undefined, async () => reply()));
  });
}
test("bounds chunked bodies even without Content-Length", async () => {
  let cancelled = false;
  const body = new ReadableStream({
    start(controller) { controller.enqueue(new Uint8Array(MAX_ARTIFACT_BYTES + 1)); },
    cancel() { cancelled = true; },
  });
  await assert.rejects(downloadArtifact(id, "/api", undefined, async () => new Response(body, {
    headers: { "Content-Type": "application/pdf", ETag: `"${"0".repeat(64)}"` },
  })), /limit/);
  assert.equal(cancelled, true);
});
test("private artefact cache preserves bytes without overwriting earlier downloads", async () => {
  const root = await mkdtemp(join(tmpdir(), "folio-test-"));
  try {
    const artifact = await downloadArtifact(id, "/api", undefined, async () => response());
    const first = await cacheArtifact(root, id, artifact);
    const second = await cacheArtifact(root, id, artifact);
    assert.notEqual(first, second);
    assert.deepEqual(new Uint8Array(await readFile(first)), pdf);
    if (process.platform !== "win32") {
      assert.equal((await stat(first)).mode & 0o777, 0o600);
    }
    await assert.rejects(cacheArtifact(root, "../../evil", artifact));
    await assert.rejects(cacheArtifact(root, id, { ...artifact, extension: "app" }));
  } finally { await rm(root, { recursive: true, force: true }); }
});
