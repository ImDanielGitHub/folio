from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, value: str) -> None:
    destination = ROOT / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(value, encoding="utf-8")


def replace_once(value: str, old: str, new: str, *, label: str) -> str:
    count = value.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one match, found {count}")
    return value.replace(old, new, 1)


def create_session_auth() -> None:
    write(
        "services/api/src/finance_agent/api/session_auth.py",
        '''"""Optional per-launch authentication for Folio's loopback API."""\n\nfrom __future__ import annotations\n\nimport hmac\nimport os\nfrom collections.abc import Mapping\nfrom typing import Final\n\nfrom starlette.responses import JSONResponse\nfrom starlette.types import ASGIApp, Receive, Scope, Send\n\nSESSION_HEADER: Final = b"x-folio-session"\nMAX_SESSION_TOKEN_BYTES: Final = 512\n\n\nclass LocalSessionAuthMiddleware:\n    """Require the process-injected session token for every versioned API route.\n\n    Development remains possible without a token. When ``FOLIO_SESSION_TOKEN`` is\n    configured, all ``/v1`` requests except CORS preflight must present the exact\n    value in ``X-Folio-Session``. Health remains unauthenticated so launchers can\n    distinguish an unavailable process from an authentication mismatch.\n    """\n\n    def __init__(\n        self,\n        app: ASGIApp,\n        *,\n        token: str | None = None,\n        protected_prefix: str = "/v1",\n    ) -> None:\n        self.app = app\n        configured = token if token is not None else os.getenv("FOLIO_SESSION_TOKEN")\n        self.token = configured.encode("utf-8") if configured else None\n        self.protected_prefix = protected_prefix\n        if self.token is not None and len(self.token) > MAX_SESSION_TOKEN_BYTES:\n            raise ValueError("FOLIO_SESSION_TOKEN exceeds the local session limit")\n\n    @staticmethod\n    def _headers(scope: Scope) -> Mapping[bytes, tuple[bytes, ...]]:\n        collected: dict[bytes, list[bytes]] = {}\n        for name, value in scope.get("headers", []):\n            collected.setdefault(name.lower(), []).append(value)\n        return {name: tuple(values) for name, values in collected.items()}\n\n    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:\n        if (\n            self.token is None\n            or scope["type"] != "http"\n            or str(scope.get("path", "")) == "/health"\n            or not str(scope.get("path", "")).startswith(self.protected_prefix)\n            or str(scope.get("method", "GET")).upper() == "OPTIONS"\n        ):\n            await self.app(scope, receive, send)\n            return\n\n        values = self._headers(scope).get(SESSION_HEADER, ())\n        accepted = (\n            len(values) == 1\n            and 0 < len(values[0]) <= MAX_SESSION_TOKEN_BYTES\n            and hmac.compare_digest(values[0], self.token)\n        )\n        if accepted:\n            await self.app(scope, receive, send)\n            return\n\n        response = JSONResponse(\n            status_code=401,\n            content={\n                "type": "https://folio.local/problems/session-authentication",\n                "title": "Local Folio session authentication required",\n                "status": 401,\n                "detail": "Restart Folio from the same launcher session and try again.",\n            },\n            media_type="application/problem+json",\n            headers={"Cache-Control": "no-store"},\n        )\n        await response(scope, receive, send)\n\n\n__all__ = ["LocalSessionAuthMiddleware", "MAX_SESSION_TOKEN_BYTES", "SESSION_HEADER"]\n''',
    )


def patch_api_app() -> None:
    path = "services/api/src/finance_agent/api/app.py"
    value = read(path)
    value = replace_once(
        value,
        "from finance_agent.api.routes import create_router\n",
        "from finance_agent.api.routes import create_router\nfrom finance_agent.api.session_auth import LocalSessionAuthMiddleware\n",
        label="session auth import",
    )
    value = replace_once(
        value,
        '        allow_headers=["Content-Type", "Last-Event-ID"],\n',
        '        allow_headers=["Content-Type", "Last-Event-ID", "X-Folio-Session"],\n',
        label="session CORS header",
    )
    anchor = "    value.add_middleware(SecurityHeadersMiddleware)\n"
    if anchor not in value:
        raise RuntimeError("Stage 1 SecurityHeadersMiddleware is missing")
    value = value.replace(
        anchor,
        "    value.add_middleware(LocalSessionAuthMiddleware)\n" + anchor,
        1,
    )
    write(path, value)


def add_api_tests() -> None:
    write(
        "services/api/tests/api/test_session_auth.py",
        '''from __future__ import annotations\n\nfrom pathlib import Path\n\nfrom fastapi.testclient import TestClient\n\nfrom finance_agent.api.app import create_app\n\n\ndef client(tmp_path: Path, monkeypatch, token: str | None) -> TestClient:\n    if token is None:\n        monkeypatch.delenv("FOLIO_SESSION_TOKEN", raising=False)\n    else:\n        monkeypatch.setenv("FOLIO_SESSION_TOKEN", token)\n    return TestClient(create_app(database_path=tmp_path / "auth.sqlite3", auto_seed=True))\n\n\ndef test_health_remains_available_for_process_discovery(tmp_path: Path, monkeypatch) -> None:\n    with client(tmp_path, monkeypatch, "session-secret") as value:\n        assert value.get("/health").status_code == 200\n\n\ndef test_versioned_routes_require_the_exact_session_header(tmp_path: Path, monkeypatch) -> None:\n    with client(tmp_path, monkeypatch, "session-secret") as value:\n        missing = value.get("/v1/models/capabilities")\n        wrong = value.get(\n            "/v1/models/capabilities",\n            headers={"X-Folio-Session": "wrong"},\n        )\n        accepted = value.get(\n            "/v1/models/capabilities",\n            headers={"X-Folio-Session": "session-secret"},\n        )\n\n    assert missing.status_code == 401\n    assert missing.headers["cache-control"] == "no-store"\n    assert "session-secret" not in missing.text\n    assert wrong.status_code == 401\n    assert accepted.status_code == 200\n\n\ndef test_duplicate_session_headers_fail_closed(tmp_path: Path, monkeypatch) -> None:\n    with client(tmp_path, monkeypatch, "session-secret") as value:\n        response = value.get(\n            "/v1/models/capabilities",\n            headers=[\n                ("X-Folio-Session", "session-secret"),\n                ("X-Folio-Session", "session-secret"),\n            ],\n        )\n    assert response.status_code == 401\n\n\ndef test_no_token_preserves_explicit_browser_development_mode(tmp_path: Path, monkeypatch) -> None:\n    with client(tmp_path, monkeypatch, None) as value:\n        assert value.get("/v1/models/capabilities").status_code == 200\n''',
    )


def write_electron_security() -> None:
    write(
        "apps/desktop/src/main/security.ts",
        '''const IDENTIFIER = /^[a-z][a-z0-9_]{2,95}$/;\n\nexport function isValidArtifactId(value: unknown): value is string {\n  return typeof value === "string" && IDENTIFIER.test(value);\n}\n\nexport function isTrustedRendererUrl(candidate: string, developmentUrl: string | null): boolean {\n  try {\n    const value = new URL(candidate);\n    if (developmentUrl) {\n      const development = new URL(developmentUrl);\n      return value.origin === development.origin;\n    }\n    return value.protocol === "folio:" && value.hostname === "app";\n  } catch {\n    return false;\n  }\n}\n\nexport function isSafeExternalUrl(candidate: string): boolean {\n  try {\n    const value = new URL(candidate);\n    return value.protocol === "https:";\n  } catch {\n    return false;\n  }\n}\n\nexport function contentTypeFor(pathname: string): string {\n  const extension = pathname.toLowerCase().split(".").at(-1);\n  const values: Record<string, string> = {\n    css: "text/css; charset=utf-8",\n    html: "text/html; charset=utf-8",\n    js: "text/javascript; charset=utf-8",\n    json: "application/json; charset=utf-8",\n    png: "image/png",\n    svg: "image/svg+xml",\n    webp: "image/webp",\n    woff2: "font/woff2",\n  };\n  return values[extension ?? ""] ?? "application/octet-stream";\n}\n''',
    )
    write(
        "apps/desktop/src/main/security.test.ts",
        '''import assert from "node:assert/strict";\nimport test from "node:test";\n\nimport {\n  contentTypeFor,\n  isSafeExternalUrl,\n  isTrustedRendererUrl,\n  isValidArtifactId,\n} from "./security.js";\n\ntest("renderer trust compares origins rather than vulnerable string prefixes", () => {\n  assert.equal(\n    isTrustedRendererUrl("http://127.0.0.1:4173/workspace", "http://127.0.0.1:4173"),\n    true,\n  );\n  assert.equal(\n    isTrustedRendererUrl("http://127.0.0.1:4173.evil.example/", "http://127.0.0.1:4173"),\n    false,\n  );\n  assert.equal(isTrustedRendererUrl("folio://app/index.html", null), true);\n  assert.equal(isTrustedRendererUrl("file:///tmp/index.html", null), false);\n});\n\ntest("external navigation is limited to valid HTTPS URLs", () => {\n  assert.equal(isSafeExternalUrl("https://example.com/help"), true);\n  assert.equal(isSafeExternalUrl("http://example.com/help"), false);\n  assert.equal(isSafeExternalUrl("javascript:alert(1)"), false);\n});\n\ntest("artifact and static content helpers fail closed", () => {\n  assert.equal(isValidArtifactId("artifact_koru_owner_pack_pdf"), true);\n  assert.equal(isValidArtifactId("../../etc/passwd"), false);\n  assert.equal(contentTypeFor("index.html"), "text/html; charset=utf-8");\n  assert.equal(contentTypeFor("payload.bin"), "application/octet-stream");\n});\n''',
    )


def replace_main() -> None:
    write(
        "apps/desktop/src/main/main.ts",
        '''import { app, BrowserWindow, dialog, ipcMain, protocol, session, shell } from "electron";\nimport { randomBytes } from "node:crypto";\nimport { mkdtemp, readFile, rm, stat, writeFile } from "node:fs/promises";\nimport { tmpdir } from "node:os";\nimport { dirname, join, normalize, sep } from "node:path";\nimport { fileURLToPath } from "node:url";\n\nimport { contentTypeFor, isSafeExternalUrl, isTrustedRendererUrl, isValidArtifactId } from "./security.js";\n\nprotocol.registerSchemesAsPrivileged([\n  {\n    scheme: "folio",\n    privileges: {\n      standard: true,\n      secure: true,\n      supportFetchAPI: true,\n      corsEnabled: true,\n    },\n  },\n]);\n\nconst currentDirectory = dirname(fileURLToPath(import.meta.url));\nconst apiBase = "http://127.0.0.1:8787";\nconst maxCsvBytes = 10_000_000;\nconst maxArtifactBytes = 25_000_000;\nconst sessionToken = process.env.FOLIO_SESSION_TOKEN?.trim() || randomBytes(32).toString("base64url");\nconst temporaryArtifactDirectories = new Set<string>();\n\nfunction rendererUrl(): string | null {\n  const argument = process.argv.find((value) => value.startsWith("--renderer-url="));\n  return argument?.slice("--renderer-url=".length) ?? null;\n}\n\nfunction trustedSender(url: string): boolean {\n  return isTrustedRendererUrl(url, rendererUrl());\n}\n\nfunction assertTrustedSender(event: Electron.IpcMainInvokeEvent): void {\n  const senderUrl = event.senderFrame?.url ?? event.sender.getURL();\n  if (!trustedSender(senderUrl)) {\n    throw new Error("Rejected IPC request from an untrusted renderer origin");\n  }\n}\n\nasync function installApplicationProtocol(): Promise<void> {\n  const applicationRoot = normalize(join(currentDirectory, "..", "..", "dist"));\n  await protocol.handle("folio", async (request) => {\n    const url = new URL(request.url);\n    if (url.hostname !== "app") return new Response("Not found", { status: 404 });\n    const requested = decodeURIComponent(url.pathname === "/" ? "/index.html" : url.pathname);\n    const candidate = normalize(join(applicationRoot, requested));\n    if (candidate !== applicationRoot && !candidate.startsWith(`${applicationRoot}${sep}`)) {\n      return new Response("Forbidden", { status: 403 });\n    }\n    try {\n      const bytes = await readFile(candidate);\n      return new Response(bytes, {\n        status: 200,\n        headers: {\n          "Content-Type": contentTypeFor(candidate),\n          "Cache-Control": "no-store",\n          "X-Content-Type-Options": "nosniff",\n        },\n      });\n    } catch {\n      return new Response("Not found", { status: 404 });\n    }\n  });\n}\n\nfunction hardenSession(): void {\n  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => {\n    callback(false);\n  });\n  session.defaultSession.setPermissionCheckHandler(() => false);\n}\n\nasync function createWindow(): Promise<void> {\n  const developmentUrl = rendererUrl();\n  const window = new BrowserWindow({\n    width: 1440,\n    height: 900,\n    minWidth: 860,\n    minHeight: 640,\n    backgroundColor: "#0d0f0e",\n    title: "Folio",\n    autoHideMenuBar: true,\n    show: false,\n    webPreferences: {\n      contextIsolation: true,\n      nodeIntegration: false,\n      sandbox: true,\n      webSecurity: true,\n      allowRunningInsecureContent: false,\n      webviewTag: false,\n      preload: join(currentDirectory, "..", "preload", "preload.cjs"),\n      additionalArguments: [`--folio-session-token=${sessionToken}`],\n    },\n  });\n\n  window.once("ready-to-show", () => {\n    window.show();\n    if (process.platform === "darwin") window.setSimpleFullScreen(true);\n    else window.setFullScreen(true);\n  });\n  window.webContents.setWindowOpenHandler(({ url }) => {\n    if (isSafeExternalUrl(url)) void shell.openExternal(url);\n    return { action: "deny" };\n  });\n  window.webContents.on("will-navigate", (event, url) => {\n    if (!isTrustedRendererUrl(url, developmentUrl)) event.preventDefault();\n  });\n  window.webContents.on("will-attach-webview", (event) => event.preventDefault());\n\n  if (developmentUrl) await window.loadURL(developmentUrl);\n  else await window.loadURL("folio://app/index.html");\n}\n\nipcMain.handle("finance:pick-csv", async (event) => {\n  assertTrustedSender(event);\n  const result = await dialog.showOpenDialog({\n    title: "Import a bank CSV",\n    properties: ["openFile"],\n    filters: [{ name: "CSV files", extensions: ["csv"] }],\n  });\n  if (result.canceled || !result.filePaths[0]) return null;\n  const path = result.filePaths[0];\n  const metadata = await stat(path);\n  if (!metadata.isFile() || metadata.size <= 0 || metadata.size > maxCsvBytes) {\n    throw new Error("CSV must be a non-empty file no larger than 10 MB");\n  }\n  const bytes = await readFile(path);\n  return {\n    name: path.split(/[\\/]/).at(-1) ?? "source.csv",\n    base64: bytes.toString("base64"),\n  };\n});\n\nipcMain.handle("finance:open-artifact", async (event, artifactId: unknown) => {\n  assertTrustedSender(event);\n  if (!isValidArtifactId(artifactId)) return false;\n  const response = await fetch(`${apiBase}/v1/artifacts/${artifactId}`, {\n    headers: {\n      Accept: "application/pdf,text/html",\n      "X-Folio-Session": sessionToken,\n    },\n  });\n  if (!response.ok) throw new Error(`Artifact request failed with ${response.status}`);\n  const declaredLength = Number(response.headers.get("content-length") ?? "0");\n  if (declaredLength > maxArtifactBytes) throw new Error("Artifact exceeds the local size limit");\n  const bytes = new Uint8Array(await response.arrayBuffer());\n  if (bytes.byteLength === 0 || bytes.byteLength > maxArtifactBytes) {\n    throw new Error("Artifact is empty or exceeds the local size limit");\n  }\n  const mediaType = response.headers.get("content-type")?.split(";", 1)[0] ?? "";\n  const extension = mediaType === "application/pdf" ? "pdf" : mediaType === "text/html" ? "html" : null;\n  if (!extension) throw new Error("Artifact returned an unsupported media type");\n  const directory = await mkdtemp(join(tmpdir(), "folio-artifact-"));\n  temporaryArtifactDirectories.add(directory);\n  const artifactPath = join(directory, `folio-owner-pack.${extension}`);\n  await writeFile(artifactPath, bytes, { mode: 0o600 });\n  const error = await shell.openPath(artifactPath);\n  if (error) throw new Error("The operating system could not open the artifact");\n  return true;\n});\n\napp.whenReady().then(async () => {\n  await installApplicationProtocol();\n  hardenSession();\n  app.on("web-contents-created", (_event, contents) => {\n    contents.on("will-attach-webview", (event) => event.preventDefault());\n  });\n  await createWindow();\n  app.on("activate", () => {\n    if (BrowserWindow.getAllWindows().length === 0) void createWindow();\n  });\n});\n\napp.on("before-quit", () => {\n  for (const directory of temporaryArtifactDirectories) {\n    void rm(directory, { recursive: true, force: true });\n  }\n});\n\napp.on("window-all-closed", () => {\n  if (process.platform !== "darwin") app.quit();\n});\n''',
    )


def replace_preload_and_types() -> None:
    write(
        "apps/desktop/src/preload/preload.cts",
        '''import { contextBridge, ipcRenderer } from "electron";\n\nconst sessionArgument = process.argv.find((value) => value.startsWith("--folio-session-token="));\nconst sessionToken = sessionArgument?.slice("--folio-session-token=".length) ?? "";\n\ncontextBridge.exposeInMainWorld("financeDesktop", {\n  runtime: "electron",\n  apiBase: "http://127.0.0.1:8787",\n  sessionToken,\n  pickCsv: () => ipcRenderer.invoke("finance:pick-csv"),\n  openArtifact: (artifactId: string) => ipcRenderer.invoke("finance:open-artifact", artifactId),\n});\n''',
    )
    write(
        "apps/desktop/src/vite-env.d.ts",
        '''/// <reference types="vite/client" />\n\ninterface ImportMetaEnv {\n  readonly VITE_FINANCE_API_URL?: string;\n  readonly VITE_FOLIO_SESSION_TOKEN?: string;\n}\n\ninterface ImportMeta {\n  readonly env: ImportMetaEnv;\n}\n\ntype FinanceDesktopBridge = {\n  runtime: "electron";\n  apiBase: string;\n  sessionToken: string;\n  pickCsv: () => Promise<{ name: string; base64: string } | null>;\n  openArtifact: (artifactId: string) => Promise<boolean>;\n};\n\ninterface Window {\n  financeDesktop?: FinanceDesktopBridge;\n}\n''',
    )


def patch_transport() -> None:
    path = "apps/desktop/src/transport.ts"
    value = read(path)
    value = replace_once(
        value,
        """const API_URL = (\n  import.meta.env.VITE_FINANCE_API_URL ?? (import.meta.env.DEV ? \"/api\" : LOOPBACK_API_URL)\n).replace(/\\/$/, \"\");\n""",
        """const API_URL = (\n  import.meta.env.VITE_FINANCE_API_URL ?? (import.meta.env.DEV ? \"/api\" : LOOPBACK_API_URL)\n).replace(/\\/$/, \"\");\nconst SESSION_TOKEN = window.financeDesktop?.sessionToken\n  ?? import.meta.env.VITE_FOLIO_SESSION_TOKEN\n  ?? \"\";\n\nfunction sessionHeaders(): Record<string, string> {\n  return SESSION_TOKEN ? { \"X-Folio-Session\": SESSION_TOKEN } : {};\n}\n""",
        label="transport session token",
    )
    value = replace_once(
        value,
        """        ...(init?.body ? { \"Content-Type\": \"application/json\" } : {}),\n        ...init?.headers,\n""",
        """        ...(init?.body ? { \"Content-Type\": \"application/json\" } : {}),\n        ...sessionHeaders(),\n        ...init?.headers,\n""",
        label="authenticated JSON requests",
    )
    value = replace_once(
        value,
        """    headers: { Accept: \"text/event-stream\" },\n""",
        """    headers: { Accept: \"text/event-stream\", ...sessionHeaders() },\n""",
        label="authenticated SSE",
    )
    value = replace_once(
        value,
        """    headers: { Accept: \"application/json\" },\n    body: form,\n""",
        """    headers: { Accept: \"application/json\", ...sessionHeaders() },\n    body: form,\n""",
        label="authenticated CSV import",
    )
    value = replace_once(
        value,
        """  window.open(`${API_URL}/v1/artifacts/${artifactId}`, \"_blank\", \"noopener,noreferrer\");\n""",
        """  const response = await fetch(`${API_URL}/v1/artifacts/${artifactId}`, {\n    headers: { Accept: \"application/pdf,text/html\", ...sessionHeaders() },\n  });\n  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);\n  const blobUrl = URL.createObjectURL(await response.blob());\n  window.open(blobUrl, \"_blank\", \"noopener,noreferrer\");\n  window.setTimeout(() => URL.revokeObjectURL(blobUrl), 60_000);\n""",
        label="authenticated browser artifact",
    )
    write(path, value)


def patch_launcher() -> None:
    path = "run"
    value = read(path)
    value = replace_once(
        value,
        """set -a\nsource \"${ROOT}/.env\"\nset +a\n\n""",
        """set -a\nsource \"${ROOT}/.env\"\nset +a\n\nif [[ -z \"${FOLIO_SESSION_TOKEN:-}\" ]]; then\n  if command -v openssl >/dev/null 2>&1; then\n    FOLIO_SESSION_TOKEN=\"$(openssl rand -hex 32)\"\n  else\n    FOLIO_SESSION_TOKEN=\"$(python3 -c 'import secrets; print(secrets.token_hex(32))')\"\n  fi\nfi\nexport FOLIO_SESSION_TOKEN\nexport VITE_FOLIO_SESSION_TOKEN=\"$FOLIO_SESSION_TOKEN\"\n\n""",
        label="launcher session token",
    )
    value = replace_once(
        value,
        """api_healthy() {\n  curl -fsS --max-time 2 \"${API_URL}/health\" >/dev/null 2>&1\n}\n""",
        """api_healthy() {\n  curl -fsS --max-time 2 \"${API_URL}/health\" >/dev/null 2>&1 \\\n    && curl -fsS --max-time 3 \\\n      -H \"X-Folio-Session: ${FOLIO_SESSION_TOKEN:-}\" \\\n      \"${API_URL}/v1/models/capabilities\" >/dev/null 2>&1\n}\n""",
        label="authenticated launcher health",
    )
    value = value.replace(
        'CAPS="$(curl -fsS --max-time 3 "${API_URL}/v1/models/capabilities" 2>/dev/null || true)"',
        'CAPS="$(curl -fsS --max-time 3 -H "X-Folio-Session: ${FOLIO_SESSION_TOKEN}" "${API_URL}/v1/models/capabilities" 2>/dev/null || true)"',
    )
    write(path, value)


def patch_env_and_csp() -> None:
    env_path = ".env.example"
    value = read(env_path)
    if "FOLIO_SESSION_TOKEN=" not in value:
        value = value.replace(
            "FINANCE_DATABASE_PATH=./var/finance-agent.sqlite3\n",
            "FINANCE_DATABASE_PATH=./var/finance-agent.sqlite3\n# Optional explicit token. ./run generates an ephemeral value when blank.\nFOLIO_SESSION_TOKEN=\n",
        )
    write(env_path, value)

    index_path = "apps/desktop/index.html"
    index = read(index_path)
    if "Content-Security-Policy" not in index:
        index = index.replace(
            '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n',
            '    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
            '    <meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\'; style-src \'self\' \'unsafe-inline\'; img-src \'self\' data:; font-src \'self\' data:; connect-src \'self\' http://127.0.0.1:8787 http://localhost:8787 ws://127.0.0.1:4173 ws://localhost:4173; object-src \'none\'; base-uri \'none\'; form-action \'none\'; frame-ancestors \'none\'" />\n',
        )
    write(index_path, index)


def patch_scripts() -> None:
    root_path = "package.json"
    root = json.loads(read(root_path))
    root["scripts"]["test:desktop"] = "pnpm --filter @folio/desktop test"
    verify = root["scripts"].get("verify", "")
    if "test:desktop" not in verify:
        root["scripts"]["verify"] = verify.replace(" && pnpm build", " && pnpm test:desktop && pnpm build")
    write(root_path, json.dumps(root, indent=2) + "\n")

    desktop_path = "apps/desktop/package.json"
    desktop = json.loads(read(desktop_path))
    desktop["scripts"]["test"] = "pnpm build:electron && node --test dist-electron/main/security.test.js"
    write(desktop_path, json.dumps(desktop, indent=2) + "\n")


def main() -> None:
    create_session_auth()
    patch_api_app()
    add_api_tests()
    write_electron_security()
    replace_main()
    replace_preload_and_types()
    patch_transport()
    patch_launcher()
    patch_env_and_csp()
    patch_scripts()


if __name__ == "__main__":
    main()
