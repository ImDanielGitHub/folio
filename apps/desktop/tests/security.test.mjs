import assert from "node:assert/strict";
import test from "node:test";

import {
  isTrustedRendererUrl,
  isValidArtifactId,
  MAX_CSV_BYTES,
} from "../dist-electron/main/security.js";

test("renderer trust compares exact origins", () => {
  assert.equal(isTrustedRendererUrl("app://folio/index.html"), true);
  assert.equal(isTrustedRendererUrl("app://evil/index.html"), false);
  assert.equal(
    isTrustedRendererUrl(
      "http://127.0.0.1:4173/workspace",
      "http://127.0.0.1:4173",
    ),
    true,
  );
  assert.equal(
    isTrustedRendererUrl(
      "http://127.0.0.1:4173.evil.example/workspace",
      "http://127.0.0.1:4173",
    ),
    false,
  );
});

test("artifact identifiers follow the frozen identifier contract", () => {
  assert.equal(isValidArtifactId("artifact_koru_owner_pack_pdf"), true);
  assert.equal(isValidArtifactId("../../etc/passwd"), false);
  assert.equal(isValidArtifactId("artifact-short"), false);
});

test("native picker limit matches the API CSV limit", () => {
  assert.equal(MAX_CSV_BYTES, 10_000_000);
});
