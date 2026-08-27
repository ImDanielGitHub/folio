from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


MODULE = '''import { createHash, createPublicKey, verify } from "node:crypto";

const MAX_UPDATE_BYTES = 500 * 1024 * 1024;
const SHA256 = /^[a-f0-9]{64}$/;
const SEMVER = /^\\d+\\.\\d+\\.\\d+(?:-[0-9A-Za-z.-]+)?$/;

export type SignedUpdateManifest = {
  manifestVersion: "folio.update@1";
  version: string;
  platform: "darwin" | "win32" | "linux";
  arch: "arm64" | "x64";
  packageUrl: string;
  packageSha256: string;
  packageSizeBytes: number;
  releaseNotesUrl: string | null;
  publishedAt: string;
  signature: string;
};

export type VerifiedUpdate = Omit<SignedUpdateManifest, "signature"> & {
  manifestUrl: string;
  signatureVerified: true;
  updateAvailable: boolean;
};

function object(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function exact(value: Record<string, unknown>, keys: readonly string[], label: string): void {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    throw new Error(`${label} contains unexpected fields`);
  }
}

function text(value: unknown, label: string): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(`${label} must be non-empty text`);
  return value;
}

function parseVersion(value: string): [number, number, number, string | null] {
  if (!SEMVER.test(value)) throw new Error("version must use semantic versioning");
  const [core, prerelease] = value.split("-", 2);
  const [major, minor, patch] = core!.split(".").map(Number);
  return [major!, minor!, patch!, prerelease ?? null];
}

export function compareVersions(left: string, right: string): number {
  const a = parseVersion(left);
  const b = parseVersion(right);
  for (let index = 0; index < 3; index += 1) {
    if (a[index]! < b[index]!) return -1;
    if (a[index]! > b[index]!) return 1;
  }
  if (a[3] === b[3]) return 0;
  if (a[3] === null) return 1;
  if (b[3] === null) return -1;
  return a[3]!.localeCompare(b[3]!);
}

export function canonicalManifestPayload(manifest: Omit<SignedUpdateManifest, "signature">): string {
  return JSON.stringify({
    arch: manifest.arch,
    manifestVersion: manifest.manifestVersion,
    packageSha256: manifest.packageSha256,
    packageSizeBytes: manifest.packageSizeBytes,
    packageUrl: manifest.packageUrl,
    platform: manifest.platform,
    publishedAt: manifest.publishedAt,
    releaseNotesUrl: manifest.releaseNotesUrl,
    version: manifest.version,
  });
}

export function parseSignedManifest(value: unknown): SignedUpdateManifest {
  const candidate = object(value, "update manifest");
  exact(candidate, [
    "manifestVersion", "version", "platform", "arch", "packageUrl",
    "packageSha256", "packageSizeBytes", "releaseNotesUrl", "publishedAt", "signature",
  ], "update manifest");
  if (candidate.manifestVersion !== "folio.update@1") throw new Error("unsupported update manifest version");
  const version = text(candidate.version, "version");
  parseVersion(version);
  if (!new Set(["darwin", "win32", "linux"]).has(String(candidate.platform))) {
    throw new Error("unsupported update platform");
  }
  if (!new Set(["arm64", "x64"]).has(String(candidate.arch))) throw new Error("unsupported update architecture");
  const packageUrl = new URL(text(candidate.packageUrl, "packageUrl"));
  if (packageUrl.protocol !== "https:") throw new Error("update package URL must use HTTPS");
  const releaseNotesUrl = candidate.releaseNotesUrl === null
    ? null
    : text(candidate.releaseNotesUrl, "releaseNotesUrl");
  if (releaseNotesUrl !== null && new URL(releaseNotesUrl).protocol !== "https:") {
    throw new Error("release notes URL must use HTTPS");
  }
  const packageSha256 = text(candidate.packageSha256, "packageSha256").toLowerCase();
  if (!SHA256.test(packageSha256)) throw new Error("packageSha256 must be a lowercase SHA-256 digest");
  const packageSizeBytes = candidate.packageSizeBytes;
  if (!Number.isSafeInteger(packageSizeBytes) || Number(packageSizeBytes) < 1 || Number(packageSizeBytes) > MAX_UPDATE_BYTES) {
    throw new Error("packageSizeBytes is outside the allowed range");
  }
  const publishedAt = text(candidate.publishedAt, "publishedAt");
  if (!Number.isFinite(Date.parse(publishedAt))) throw new Error("publishedAt must be an ISO date-time");
  const signature = text(candidate.signature, "signature");
  const decoded = Buffer.from(signature, "base64");
  if (decoded.length !== 64) throw new Error("signature must be a base64 Ed25519 signature");
  return {
    manifestVersion: "folio.update@1",
    version,
    platform: candidate.platform as SignedUpdateManifest["platform"],
    arch: candidate.arch as SignedUpdateManifest["arch"],
    packageUrl: packageUrl.toString(),
    packageSha256,
    packageSizeBytes: Number(packageSizeBytes),
    releaseNotesUrl,
    publishedAt,
    signature,
  };
}

export function verifyManifestSignature(
  manifest: SignedUpdateManifest,
  publicKeyPem: string,
): Omit<SignedUpdateManifest, "signature"> {
  const { signature, ...payload } = manifest;
  const key = createPublicKey(publicKeyPem);
  if (key.asymmetricKeyType !== "ed25519") throw new Error("update key must be Ed25519");
  const accepted = verify(
    null,
    Buffer.from(canonicalManifestPayload(payload)),
    key,
    Buffer.from(signature, "base64"),
  );
  if (!accepted) throw new Error("update manifest signature is invalid");
  return payload;
}

function sameOrigin(manifestUrl: URL, value: string, label: string): URL {
  const candidate = new URL(value);
  if (candidate.protocol !== "https:" || candidate.origin !== manifestUrl.origin) {
    throw new Error(`${label} must use the update manifest HTTPS origin`);
  }
  return candidate;
}

export async function loadVerifiedUpdate(
  options: {
    manifestUrl: string;
    publicKeyPem: string;
    currentVersion: string;
    platform: NodeJS.Platform;
    arch: string;
    fetchImpl?: typeof fetch;
  },
): Promise<VerifiedUpdate> {
  const manifestUrl = new URL(options.manifestUrl);
  if (manifestUrl.protocol !== "https:") throw new Error("update manifest URL must use HTTPS");
  const response = await (options.fetchImpl ?? fetch)(manifestUrl, {
    headers: { Accept: "application/json" },
    redirect: "error",
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`update manifest request failed with ${response.status}`);
  const contentLength = Number(response.headers.get("content-length") ?? 0);
  if (contentLength > 1024 * 1024) throw new Error("update manifest exceeds 1 MB");
  const raw = await response.text();
  if (Buffer.byteLength(raw) > 1024 * 1024) throw new Error("update manifest exceeds 1 MB");
  const manifest = parseSignedManifest(JSON.parse(raw));
  const payload = verifyManifestSignature(manifest, options.publicKeyPem);
  if (payload.platform !== options.platform || payload.arch !== options.arch) {
    throw new Error("update manifest does not match this platform and architecture");
  }
  const packageUrl = sameOrigin(manifestUrl, payload.packageUrl, "packageUrl");
  const releaseNotesUrl = payload.releaseNotesUrl
    ? sameOrigin(manifestUrl, payload.releaseNotesUrl, "releaseNotesUrl").toString()
    : null;
  return {
    ...payload,
    packageUrl: packageUrl.toString(),
    releaseNotesUrl,
    manifestUrl: manifestUrl.toString(),
    signatureVerified: true,
    updateAvailable: compareVersions(payload.version, options.currentVersion) > 0,
  };
}

export async function downloadVerifiedUpdate(
  update: VerifiedUpdate,
  fetchImpl: typeof fetch = fetch,
): Promise<Buffer> {
  const response = await fetchImpl(update.packageUrl, {
    headers: { Accept: "application/octet-stream" },
    redirect: "error",
    cache: "no-store",
  });
  if (!response.ok) throw new Error(`update package request failed with ${response.status}`);
  const contentLength = Number(response.headers.get("content-length") ?? 0);
  if (contentLength && contentLength !== update.packageSizeBytes) {
    throw new Error("update package Content-Length does not match the signed manifest");
  }
  if (contentLength > MAX_UPDATE_BYTES) throw new Error("update package exceeds the local byte limit");
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length !== update.packageSizeBytes) throw new Error("update package size does not match the signed manifest");
  const digest = createHash("sha256").update(bytes).digest("hex");
  if (digest !== update.packageSha256) throw new Error("update package SHA-256 does not match the signed manifest");
  return bytes;
}
'''

SIGN_SCRIPT = '''import { createHash, createPrivateKey, sign } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const [inputPath, privateKeyPath, outputPath] = process.argv.slice(2);
if (!inputPath || !privateKeyPath || !outputPath) {
  console.error("usage: node scripts/sign_update_manifest.mjs <unsigned.json> <ed25519-private-key.pem> <signed.json>");
  process.exit(2);
}
const value = JSON.parse(await readFile(resolve(inputPath), "utf8"));
if ("signature" in value) delete value.signature;
const packageBytes = await readFile(resolve(value.packagePath));
value.packageSha256 = createHash("sha256").update(packageBytes).digest("hex");
value.packageSizeBytes = packageBytes.length;
delete value.packagePath;
const canonical = JSON.stringify({
  arch: value.arch,
  manifestVersion: value.manifestVersion,
  packageSha256: value.packageSha256,
  packageSizeBytes: value.packageSizeBytes,
  packageUrl: value.packageUrl,
  platform: value.platform,
  publishedAt: value.publishedAt,
  releaseNotesUrl: value.releaseNotesUrl,
  version: value.version,
});
const key = createPrivateKey(await readFile(resolve(privateKeyPath), "utf8"));
if (key.asymmetricKeyType !== "ed25519") throw new Error("private key must be Ed25519");
value.signature = sign(null, Buffer.from(canonical), key).toString("base64");
await writeFile(resolve(outputPath), JSON.stringify(value, null, 2) + "\n", { mode: 0o600 });
console.log(`signed ${outputPath} (${value.packageSizeBytes} bytes, ${value.packageSha256})`);
'''

TEST = '''import assert from "node:assert/strict";
import { generateKeyPairSync, sign } from "node:crypto";
import test from "node:test";

import {
  canonicalManifestPayload,
  compareVersions,
  downloadVerifiedUpdate,
  loadVerifiedUpdate,
} from "../../dist-electron/main/updateManifest.js";

function fixture() {
  const keys = generateKeyPairSync("ed25519");
  const bytes = Buffer.from("verified Folio update bytes");
  const payload = {
    manifestVersion: "folio.update@1",
    version: "1.2.3",
    platform: "linux",
    arch: "x64",
    packageUrl: "https://updates.example.test/folio-1.2.3.AppImage",
    packageSha256: (await import("node:crypto")).createHash("sha256").update(bytes).digest("hex"),
    packageSizeBytes: bytes.length,
    releaseNotesUrl: "https://updates.example.test/releases/1.2.3",
    publishedAt: "2026-08-26T00:00:00Z",
  };
  const signature = sign(null, Buffer.from(canonicalManifestPayload(payload)), keys.privateKey).toString("base64");
  return { keys, bytes, manifest: { ...payload, signature } };
}

test("semantic version comparison does not use lexical ordering", () => {
  assert.equal(compareVersions("1.10.0", "1.9.9"), 1);
  assert.equal(compareVersions("1.0.0-beta", "1.0.0"), -1);
  assert.equal(compareVersions("2.0.0", "2.0.0"), 0);
});

test("signed same-origin manifest is accepted for the current platform", async () => {
  const { keys, manifest } = await fixture();
  const update = await loadVerifiedUpdate({
    manifestUrl: "https://updates.example.test/latest.json",
    publicKeyPem: keys.publicKey.export({ type: "spki", format: "pem" }).toString(),
    currentVersion: "1.0.0",
    platform: "linux",
    arch: "x64",
    fetchImpl: async () => new Response(JSON.stringify(manifest), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }),
  });
  assert.equal(update.signatureVerified, true);
  assert.equal(update.updateAvailable, true);
  assert.equal(update.version, "1.2.3");
});

test("tampered signature, cross-origin package and wrong platform fail closed", async () => {
  const { keys, manifest } = await fixture();
  const publicKeyPem = keys.publicKey.export({ type: "spki", format: "pem" }).toString();
  for (const changed of [
    { ...manifest, version: "9.9.9" },
    { ...manifest, packageUrl: "https://evil.example.test/update.bin" },
    { ...manifest, platform: "darwin" },
  ]) {
    await assert.rejects(() => loadVerifiedUpdate({
      manifestUrl: "https://updates.example.test/latest.json",
      publicKeyPem,
      currentVersion: "1.0.0",
      platform: "linux",
      arch: "x64",
      fetchImpl: async () => new Response(JSON.stringify(changed), { status: 200 }),
    }));
  }
});

test("download verifies signed size and SHA-256 before returning bytes", async () => {
  const { keys, bytes, manifest } = await fixture();
  const update = await loadVerifiedUpdate({
    manifestUrl: "https://updates.example.test/latest.json",
    publicKeyPem: keys.publicKey.export({ type: "spki", format: "pem" }).toString(),
    currentVersion: "1.0.0",
    platform: "linux",
    arch: "x64",
    fetchImpl: async () => new Response(JSON.stringify(manifest), { status: 200 }),
  });
  const downloaded = await downloadVerifiedUpdate(
    update,
    async () => new Response(bytes, {
      status: 200,
      headers: { "Content-Length": String(bytes.length) },
    }),
  );
  assert.deepEqual(downloaded, bytes);
  await assert.rejects(() => downloadVerifiedUpdate(
    update,
    async () => new Response(Buffer.from("tampered"), { status: 200 }),
  ));
});
'''

# Fix top-level await in helper: keep the test valid ESM by making fixture async.
TEST = TEST.replace("function fixture() {", "async function fixture() {")


def add_module_script_test() -> None:
    write("apps/desktop/src/main/updateManifest.ts", MODULE)
    write("scripts/sign_update_manifest.mjs", SIGN_SCRIPT)
    write("apps/desktop/tests/electron/update-manifest.test.mjs", TEST)


def update_main_preload_types() -> None:
    path = "apps/desktop/src/main/main.ts"
    content = read(path)
    import_marker = 'import { readFile } from "node:fs/promises";\n'
    replacement = 'import { readFile, writeFile } from "node:fs/promises";\n'
    if import_marker in content:
        content = content.replace(import_marker, replacement, 1)
    elif replacement not in content:
        raise RuntimeError("main fs import marker missing")
    marker = 'import { fileURLToPath } from "node:url";\n'
    imports = (
        'import { loadVerifiedUpdate, downloadVerifiedUpdate } from "./updateManifest.js";\n'
    )
    if imports not in content:
        if marker not in content:
            raise RuntimeError("main url import marker missing")
        content = content.replace(marker, marker + imports, 1)
    handler_marker = 'ipcMain.handle("finance:open-artifact", async (_event, artifactId: string) => {\n'
    handlers = '''ipcMain.handle("finance:update-status", async () => {
  const manifestUrl = process.env.FOLIO_UPDATE_MANIFEST_URL?.trim();
  const publicKeyPem = process.env.FOLIO_UPDATE_PUBLIC_KEY_PEM?.trim();
  if (!manifestUrl || !publicKeyPem) {
    return { configured: false, updateAvailable: false };
  }
  const update = await loadVerifiedUpdate({
    manifestUrl,
    publicKeyPem,
    currentVersion: app.getVersion(),
    platform: process.platform,
    arch: process.arch,
  });
  return {
    configured: true,
    updateAvailable: update.updateAvailable,
    version: update.version,
    publishedAt: update.publishedAt,
    releaseNotesUrl: update.releaseNotesUrl,
    packageSizeBytes: update.packageSizeBytes,
    signatureVerified: true,
  };
});

ipcMain.handle("finance:download-update", async () => {
  const manifestUrl = process.env.FOLIO_UPDATE_MANIFEST_URL?.trim();
  const publicKeyPem = process.env.FOLIO_UPDATE_PUBLIC_KEY_PEM?.trim();
  if (!manifestUrl || !publicKeyPem) return { status: "unconfigured" };
  const update = await loadVerifiedUpdate({
    manifestUrl,
    publicKeyPem,
    currentVersion: app.getVersion(),
    platform: process.platform,
    arch: process.arch,
  });
  if (!update.updateAvailable) return { status: "current", version: update.version };
  const bytes = await downloadVerifiedUpdate(update);
  const suggestedName = new URL(update.packageUrl).pathname.split("/").at(-1) || `folio-${update.version}.bin`;
  const result = await dialog.showSaveDialog({
    title: `Save verified Folio ${update.version} update`,
    defaultPath: suggestedName,
    buttonLabel: "Save verified update",
  });
  if (result.canceled || !result.filePath) return { status: "cancelled" };
  await writeFile(result.filePath, bytes, { mode: 0o600 });
  return {
    status: "downloaded",
    version: update.version,
    path: result.filePath,
    packageSha256: update.packageSha256,
    signatureVerified: true,
    installed: false,
  };
});

'''
    if "finance:update-status" not in content:
        if handler_marker not in content:
            raise RuntimeError("open artifact handler marker missing")
        content = content.replace(handler_marker, handlers + handler_marker, 1)
    write(path, content)

    path = "apps/desktop/src/preload/preload.cts"
    content = read(path)
    marker = '  openArtifact: (artifactId: string) =>\n    ipcRenderer.invoke("finance:open-artifact", artifactId),\n'
    addition = marker + '''  checkForUpdate: () => ipcRenderer.invoke("finance:update-status"),
  downloadUpdate: () => ipcRenderer.invoke("finance:download-update"),
'''
    if "checkForUpdate" not in content:
        if marker not in content:
            raise RuntimeError("preload openArtifact marker missing")
        content = content.replace(marker, addition, 1)
    write(path, content)

    path = "apps/desktop/src/vite-env.d.ts"
    content = read(path)
    marker = "    openArtifact: (artifactId: string) => Promise<boolean>;\n"
    addition = marker + '''    checkForUpdate: () => Promise<{
      configured: boolean;
      updateAvailable: boolean;
      version?: string;
      publishedAt?: string;
      releaseNotesUrl?: string | null;
      packageSizeBytes?: number;
      signatureVerified?: boolean;
    }>;
    downloadUpdate: () => Promise<Record<string, unknown>>;
'''
    if "checkForUpdate" not in content:
        if marker not in content:
            raise RuntimeError("financeDesktop type marker missing")
        content = content.replace(marker, addition, 1)
    write(path, content)


def update_packages_docs_env() -> None:
    path = "package.json"
    value = json.loads(read(path))
    scripts = value["scripts"]
    scripts["test:update-channel"] = "pnpm --filter @folio/desktop build:electron && node --test apps/desktop/tests/electron/update-manifest.test.mjs"
    scripts["update:sign-manifest"] = "node scripts/sign_update_manifest.mjs"
    if "test:update-channel" not in scripts["verify"]:
        scripts["verify"] += " && pnpm test:update-channel"
    write(path, json.dumps(value, indent=2) + "\n")

    path = ".env.example"
    content = read(path)
    addition = '''
# Optional signed manual update channel. Production builds must embed or inject
# an immutable Ed25519 public key; never ship the signing private key.
FOLIO_UPDATE_MANIFEST_URL=
FOLIO_UPDATE_PUBLIC_KEY_PEM=
'''
    if "FOLIO_UPDATE_MANIFEST_URL" not in content:
        write(path, content.rstrip() + "\n" + addition)
    write("docs/SIGNED_UPDATES.md", '''# Signed manual update channel\n\nFolio's update checker is disabled unless an HTTPS manifest URL and Ed25519 public key are configured. The manifest has a closed schema and signature over version, platform, architecture, package URL, package SHA-256, size, notes URL and publication time. Package and release-notes URLs must share the manifest's HTTPS origin. Redirects, oversized manifests, platform mismatches, invalid signatures, size mismatches and SHA mismatches fail closed.\n\nA verified update may be saved manually after a native file prompt. Folio returns `installed: false`; it does not silently execute or install the bytes. `pnpm update:sign-manifest -- unsigned.json private.pem signed.json` is a release-only tool. The private signing key must remain outside the repository and CI logs.\n\nThis code does not prove code signing, macOS notarisation, Windows signing, publication, update-host control or an observed install. Those require release credentials and platform acceptance on the produced artefacts.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 34: signed manual desktop update channel\n\n- Update manifests have a closed schema and Ed25519 signature.\n- Manifest, package and release notes must share one HTTPS origin.\n- Platform, architecture, semantic version, size and SHA-256 are verified.\n- Redirects, tampering and oversized data fail closed.\n- Verified bytes are saved only after a native owner prompt and are not auto-installed.\n- Signing, notarisation, publication and observed installation remain external proof.\n'''
    if "## Stack 34: signed manual desktop update channel" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_module_script_test()
    update_main_preload_types()
    update_packages_docs_env()
    print("signed update channel changes applied")


if __name__ == "__main__":
    main()
