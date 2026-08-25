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
        raise RuntimeError(f"{path}: expected one exact match, found {count}: {old[:100]!r}")
    write(path, content.replace(old, new, 1))


def regex_once(path: str, pattern: str, replacement: str, *, flags: int = 0) -> None:
    content = read(path)
    updated, count = re.subn(pattern, replacement, content, count=1, flags=flags)
    if count != 1:
        raise RuntimeError(f"{path}: expected one regex match, found {count}: {pattern!r}")
    write(path, updated)


def update_json(path: str, mutate) -> None:
    value = json.loads(read(path))
    mutate(value)
    write(path, json.dumps(value, indent=2) + "\n")


HTTP_SECURITY = '''"""HTTP boundary controls for Folio's loopback API."""

from __future__ import annotations

import hmac
import re
from collections.abc import Mapping
from typing import Final
from urllib.parse import quote

from fastapi import UploadFile
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

IDENTIFIER_PATTERN: Final = r"^[a-z][a-z0-9]{1,15}_[a-z0-9][a-z0-9_]{2,95}$"
IDENTIFIER_MIN_LENGTH: Final = 6
IDENTIFIER_MAX_LENGTH: Final = 113
MAX_CSV_BYTES: Final = 10_000_000
MAX_REQUEST_BODY_BYTES: Final = 12_000_000
UPLOAD_CHUNK_BYTES: Final = 1_048_576

_SECURITY_HEADERS: Final[Mapping[bytes, bytes]] = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"no-referrer",
    b"permissions-policy": b"camera=(), microphone=(), geolocation=()",
    b"cross-origin-resource-policy": b"same-origin",
}


class UploadTooLarge(ValueError):
    """Raised as soon as a streamed upload crosses its byte limit."""


class _RequestBodyTooLarge(RuntimeError):
    pass


async def read_upload_with_limit(
    upload: UploadFile,
    *,
    max_bytes: int = MAX_CSV_BYTES,
    chunk_bytes: int = UPLOAD_CHUNK_BYTES,
) -> bytes:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(chunk_bytes)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadTooLarge(f"upload exceeds the {max_bytes} byte limit")
        chunks.append(chunk)
    return b"".join(chunks)


def content_disposition(filename: str, *, disposition: str = "inline") -> str:
    if disposition not in {"inline", "attachment"}:
        raise ValueError("unsupported content disposition")
    cleaned = filename.replace("\\r", " ").replace("\\n", " ").strip()
    cleaned = re.sub(r"[\\x00-\\x1f\\x7f]", "", cleaned)
    if not cleaned:
        cleaned = "artifact"
    fallback = cleaned.encode("ascii", "ignore").decode("ascii")
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", fallback).strip(" .") or "artifact"
    fallback = fallback[:180]
    encoded = quote(cleaned, safe="")
    return f'{disposition}; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'


async def _send_problem(
    scope: Scope,
    receive: Receive,
    send: Send,
    *,
    status: int,
    title: str,
    detail: str,
) -> None:
    response = JSONResponse(
        status_code=status,
        media_type="application/problem+json",
        content={"title": title, "status": status, "detail": detail},
    )
    await response(scope, receive, send)


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw_length = Headers(scope=scope).get("content-length")
        if raw_length is not None:
            try:
                content_length = int(raw_length)
            except ValueError:
                await _send_problem(
                    scope,
                    receive,
                    send,
                    status=400,
                    title="Invalid Content-Length",
                    detail="Content-Length must be a non-negative integer.",
                )
                return
            if content_length < 0:
                await _send_problem(
                    scope,
                    receive,
                    send,
                    status=400,
                    title="Invalid Content-Length",
                    detail="Content-Length must be a non-negative integer.",
                )
                return
            if content_length > self.max_bytes:
                await _send_problem(
                    scope,
                    receive,
                    send,
                    status=413,
                    title="Request body too large",
                    detail=f"The request exceeds the {self.max_bytes} byte limit.",
                )
                return

        total = 0

        async def limited_receive() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _RequestBodyTooLarge:
            await _send_problem(
                scope,
                receive,
                send,
                status=413,
                title="Request body too large",
                detail=f"The request exceeds the {self.max_bytes} byte limit.",
            )


class SessionAuthMiddleware:
    """Require a per-launch secret when the local launcher configures one."""

    def __init__(self, app: ASGIApp, *, token: str | None) -> None:
        self.app = app
        self.token = token.strip() if token else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or self.token is None:
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        if method == "OPTIONS" or path == "/health" or path.startswith("/v1/artifacts/"):
            await self.app(scope, receive, send)
            return
        supplied = Headers(scope=scope).get("x-folio-session") or ""
        if not hmac.compare_digest(supplied, self.token):
            await _send_problem(
                scope,
                receive,
                send,
                status=401,
                title="Local session authentication required",
                detail="The request did not present the current Folio session token.",
            )
            return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def hardened_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                present = {name.lower() for name, _ in headers}
                headers.extend(
                    (name, value)
                    for name, value in _SECURITY_HEADERS.items()
                    if name not in present
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, hardened_send)
'''

APP_PY = '''"""Loopback-only FastAPI composition root for the Folio local service."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware

from finance_agent.api.http_security import (
    MAX_REQUEST_BODY_BYTES,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    SessionAuthMiddleware,
)
from finance_agent.api.routes import create_router
from finance_agent.api.services import LocalRouteServices

ROOT = Path(__file__).resolve().parents[5]
DEFAULT_DATABASE = ROOT / "var" / "finance-agent.sqlite3"


def create_app(
    *,
    database_path: str | Path | None = None,
    auto_seed: bool = True,
    session_token: str | None = None,
) -> FastAPI:
    services = LocalRouteServices(database_path or DEFAULT_DATABASE, auto_seed=auto_seed)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await services.aclose()

    value = FastAPI(
        title="Folio Local Finance API",
        version="0.2.0",
        docs_url="/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    value.add_middleware(RequestBodyLimitMiddleware, max_bytes=MAX_REQUEST_BODY_BYTES)
    value.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:4173",
            "http://localhost:4173",
            "http://127.0.0.1:4174",
            "http://localhost:4174",
            "app://folio",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Last-Event-ID", "X-Folio-Session"],
    )
    value.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "[::1]", "test", "testserver"],
    )
    value.add_middleware(
        SessionAuthMiddleware,
        token=session_token if session_token is not None else os.getenv("FOLIO_SESSION_TOKEN"),
    )
    value.add_middleware(SecurityHeadersMiddleware)
    value.include_router(create_router())
    value.state.finance_route_services = services
    return value


app = create_app()


__all__ = ["app", "create_app"]
'''

HTTP_TESTS = '''from __future__ import annotations

import io

import httpx
import pytest
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import PlainTextResponse
from starlette.datastructures import Headers

from finance_agent.api.http_security import (
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
    SessionAuthMiddleware,
    UploadTooLarge,
    content_disposition,
    read_upload_with_limit,
)


@pytest.mark.asyncio
async def test_rejects_declared_request_body_before_route_execution() -> None:
    app = FastAPI()
    called = False

    @app.post("/body")
    async def body(_: Request) -> dict[str, bool]:
        nonlocal called
        called = True
        return {"called": True}

    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=4)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/body", content=b"12345")
    assert response.status_code == 413
    assert called is False


@pytest.mark.asyncio
async def test_rejects_chunked_body_when_limit_is_crossed() -> None:
    app = FastAPI()

    @app.post("/body")
    async def body(request: Request) -> dict[str, int]:
        return {"size": len(await request.body())}

    app.add_middleware(RequestBodyLimitMiddleware, max_bytes=4)

    async def chunks():
        yield b"12"
        yield b"345"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/body", content=chunks())
    assert response.status_code == 413


@pytest.mark.asyncio
async def test_session_auth_is_optional_but_enforced_when_configured() -> None:
    app = FastAPI()

    @app.get("/private")
    async def private() -> dict[str, bool]:
        return {"ok": True}

    app.add_middleware(SessionAuthMiddleware, token="secret-token")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        rejected = await client.get("/private")
        accepted = await client.get(
            "/private", headers={"X-Folio-Session": "secret-token"}
        )
    assert rejected.status_code == 401
    assert accepted.json() == {"ok": True}


@pytest.mark.asyncio
async def test_security_headers_are_added_without_overwriting_route_headers() -> None:
    app = FastAPI()

    @app.get("/")
    async def root() -> PlainTextResponse:
        return PlainTextResponse("ok", headers={"X-Content-Type-Options": "custom"})

    app.add_middleware(SecurityHeadersMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/")
    assert response.headers["x-content-type-options"] == "custom"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.asyncio
async def test_streamed_upload_stops_at_configured_limit() -> None:
    upload = UploadFile(filename="bank.csv", file=io.BytesIO(b"12345"))
    with pytest.raises(UploadTooLarge):
        await read_upload_with_limit(upload, max_bytes=4, chunk_bytes=2)


def test_content_disposition_removes_header_control_characters() -> None:
    value = content_disposition('owner\\r\\nX-Evil: yes — pack.pdf')
    headers = Headers({"Content-Disposition": value})
    assert "\\r" not in headers["content-disposition"]
    assert "\\n" not in headers["content-disposition"]
    assert "filename*=UTF-8''" in value
'''

DESKTOP_SECURITY = '''export const MAX_CSV_BYTES = 10_000_000;

export function isTrustedRendererUrl(candidate: string, developmentUrl?: string | null): boolean {
  let value: URL;
  try {
    value = new URL(candidate);
  } catch {
    return false;
  }
  if (value.protocol === "app:" && value.hostname === "folio") return true;
  if (!developmentUrl) return false;
  try {
    const development = new URL(developmentUrl);
    return value.origin === development.origin;
  } catch {
    return false;
  }
}

export function isValidArtifactId(value: unknown): value is string {
  return typeof value === "string" && /^[a-z][a-z0-9]{1,15}_[a-z0-9][a-z0-9_]{2,95}$/.test(value);
}
'''

DESKTOP_MAIN = '''import {
  app,
  BrowserWindow,
  dialog,
  ipcMain,
  net,
  protocol,
  session,
  shell,
} from "electron";
import { readFile, stat } from "node:fs/promises";
import { dirname, join, resolve, sep } from "node:path";
import { pathToFileURL, fileURLToPath } from "node:url";
import { isTrustedRendererUrl, isValidArtifactId, MAX_CSV_BYTES } from "./security.js";

protocol.registerSchemesAsPrivileged([
  {
    scheme: "app",
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: true,
    },
  },
]);

const currentDirectory = dirname(fileURLToPath(import.meta.url));
const apiBase = "http://127.0.0.1:8787";

function rendererUrl(): string | null {
  const argument = process.argv.find((value) => value.startsWith("--renderer-url="));
  return argument?.slice("--renderer-url=".length) ?? null;
}

function assertTrustedSender(url: string): void {
  if (!isTrustedRendererUrl(url, rendererUrl())) {
    throw new Error("Rejected IPC from an untrusted renderer origin")
  }
}

async function createWindow(): Promise<void> {
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 860,
    minHeight: 640,
    backgroundColor: "#0d0f0e",
    title: "Folio",
    autoHideMenuBar: true,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      preload: join(currentDirectory, "..", "preload", "preload.cjs"),
    },
  });

  window.once("ready-to-show", () => window.show());
  window.webContents.setWindowOpenHandler(({ url }) => {
    const parsed = new URL(url);
    if (
      parsed.origin === apiBase
      && /^\\/v1\\/artifacts\\/[a-z][a-z0-9]{1,15}_[a-z0-9][a-z0-9_]{2,95}$/.test(parsed.pathname)
    ) {
      void shell.openExternal(url);
    }
    return { action: "deny" };
  });
  window.webContents.on("will-navigate", (event, url) => {
    if (!isTrustedRendererUrl(url, rendererUrl())) event.preventDefault();
  });
  window.webContents.on("will-attach-webview", (event) => event.preventDefault());

  const developmentUrl = rendererUrl();
  if (developmentUrl) {
    await window.loadURL(developmentUrl);
  } else {
    await window.loadURL("app://folio/index.html");
  }
}

ipcMain.handle("finance:pick-csv", async (event) => {
  assertTrustedSender(event.senderFrame.url);
  const result = await dialog.showOpenDialog({
    title: "Import a bank CSV",
    properties: ["openFile"],
    filters: [{ name: "CSV files", extensions: ["csv"] }],
  });
  if (result.canceled || !result.filePaths[0]) return null;
  const path = result.filePaths[0];
  const metadata = await stat(path);
  if (!metadata.isFile() || metadata.size > MAX_CSV_BYTES) {
    throw new Error("CSV must be a regular file no larger than 10 MB")
  }
  const bytes = await readFile(path);
  return {
    name: path.split(/[\\\\/]/).at(-1) ?? "source.csv",
    base64: bytes.toString("base64"),
  };
});

ipcMain.handle("finance:open-artifact", async (event, artifactId: unknown) => {
  assertTrustedSender(event.senderFrame.url);
  if (!isValidArtifactId(artifactId)) return false;
  await shell.openExternal(`${apiBase}/v1/artifacts/${artifactId}`);
  return true;
});

app.whenReady().then(async () => {
  const rendererRoot = resolve(currentDirectory, "..", "..", "dist");
  protocol.handle("app", (request) => {
    const requestUrl = new URL(request.url);
    const relativePath = requestUrl.pathname === "/" ? "index.html" : requestUrl.pathname.slice(1);
    const resolvedPath = resolve(rendererRoot, relativePath);
    if (resolvedPath !== rendererRoot && !resolvedPath.startsWith(`${rendererRoot}${sep}`)) {
      return new Response("Not found", { status: 404 });
    }
    return net.fetch(pathToFileURL(resolvedPath).toString());
  });
  session.defaultSession.setPermissionCheckHandler(() => false);
  session.defaultSession.setPermissionRequestHandler((_webContents, _permission, callback) => callback(false));
  await createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) void createWindow();
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
'''

DESKTOP_SECURITY_TEST = '''import assert from "node:assert/strict";
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
'''

CI_WORKFLOW = '''name: CI

on:
  pull_request:
  push:
    branches:
      - main

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

jobs:
  verify:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: pnpm/action-setup@v4
        with:
          version: 10.33.0
          run_install: false
      - uses: actions/setup-node@v4
        with:
          node-version: 22
          cache: pnpm
          cache-dependency-path: pnpm-lock.yaml
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - uses: astral-sh/setup-uv@v6
        with:
          version: "0.10.0"
          enable-cache: true
      - run: pnpm install --frozen-lockfile
      - run: uv sync --project services/api --frozen
      - name: Verify generated fixes are committed
        run: |
          pnpm lint:fix
          git diff --exit-code
      - run: pnpm verify
'''

CONTRIBUTING = '''# Contributing to Folio

Folio is a local-first finance operator. Changes must preserve the boundary between model-authored language and deterministic finance truth.

## Setup

```bash
pnpm install --frozen-lockfile
uv sync --project services/api --frozen
```

Run the complete gate before review:

```bash
pnpm verify
```

## Engineering rules

1. Amounts, transaction selection, effects, forecasts, evidence and generated documents remain deterministic.
2. Source records and owner statements are immutable. Corrections append replacement state and supersession links.
3. Model output must pass closed schemas and bounded validation before execution.
4. Local mode must never silently call a cloud provider.
5. External connectors are disabled by default and require fixture-backed tests.
6. Fixtures must remain fictional and contain no real banking, customer or credential data.
7. A finance mutation needs an idempotency rule, an evidence trail and a reversible event or an explicit reason it cannot be reversed.
8. Do not describe source, test, build, runtime, release or provider proof as interchangeable.
'''

SECURITY = '''# Security policy

Folio handles sensitive financial information even when it runs only on the owner's computer. Confidentiality, integrity, provenance and local process isolation are product requirements.

## Reporting

Use GitHub private vulnerability reporting when enabled. Otherwise contact the repository owner privately. Include the affected commit, operating system, smallest reproduction, expected boundary and whether real data or credentials were exposed. Never attach live bank exports, tokens or customer records.

## Current boundary

The supported development version is the latest commit on `main`. The API must bind only to loopback. Electron uses context isolation, a sandboxed preload bridge, no renderer Node integration, exact renderer-origin checks and default-deny permissions. Finance effects come from deterministic services, not model prose.

Provider credentials are opt-in and process-scoped. They must not be committed, included in fixtures or logs, or returned from capability endpoints. The optional per-launch `FOLIO_SESSION_TOKEN` protects state-changing loopback requests made by the normal launcher.
'''

AUDIT_PROGRAMME = '''# Folio audit implementation programme

This branch implements the first mergeable programme slice from the 200-item repository audit. It deliberately does not pretend that one pull request can complete external accreditation, legal review, live-bank acceptance, multi-business identity, signed distribution and full accounting integrations.

## Implemented in this pull request

- cross-platform root commands and a complete CI verification gate
- bounded HTTP and CSV bodies, safe artifact headers, identifier validation and conservative response headers
- optional per-launch loopback session authentication
- exact Electron renderer-origin and IPC sender checks, default-deny permissions and a secure `app://` production origin
- bounded native CSV reads
- material Daily Close identity across every committed source, active rule and reserve policy
- real job timestamps and result counts rather than fixed demo claims
- per-turn model-mode provenance
- cross-workspace guards for claim supersession and dialogue-frame updates
- a provider-event quarantine for non-NZD Plaid records so USD cents cannot become NZD ledger cents
- regression tests for the new trust boundaries

## Explicitly remaining

The remaining audit programme includes encrypted-at-rest workspace storage, key recovery, multi-business identities and roles, signed installers and notarisation, auto-update, real Akahu OAuth/accreditation, Xero/MYOB integrations, GST-period and lock-date workflows, authenticated Telegram/WhatsApp ingestion, durable scheduling and notifications, document extraction and quarantine, full renderer interaction tests, accessibility runtime testing, performance budgets, privacy/legal review and real-provider acceptance evidence.

Those are separate proof boundaries and should be delivered as reviewable follow-up pull requests rather than hidden behind a single untestable mega-merge.
'''


def configure_packages() -> None:
    def root_package(value: dict) -> None:
        scripts = value["scripts"]
        scripts.update(
            {
                "api": "uv run --project services/api uvicorn finance_agent.api.app:app --app-dir services/api/src --host 127.0.0.1 --port 8787",
                "contracts:check": "uv run --project services/api python scripts/contracts_check.py",
                "lint": "uv run --project services/api ruff check --config services/api/pyproject.toml services/api/src services/api/tests",
                "lint:fix": "uv run --project services/api ruff check --fix --config services/api/pyproject.toml services/api/src services/api/tests",
                "typecheck": "uv run --project services/api mypy --config-file services/api/pyproject.toml services/api/src/finance_agent && pnpm --filter @folio/desktop typecheck",
                "test:python": "uv run --project services/api pytest -q services/api/tests",
                "test:desktop": "pnpm --filter @folio/desktop test",
                "test": "pnpm contracts:check && pnpm test:python && pnpm test:desktop && pnpm --filter @folio/desktop typecheck",
                "build": "pnpm --filter @folio/desktop build",
                "verify": "pnpm contracts:check && pnpm lint && pnpm typecheck && pnpm test:python && pnpm test:desktop && pnpm build && pnpm eval:offline",
                "demo:reset": "uv run --project services/api python scripts/demo_control.py reset",
                "demo:daily-close": "uv run --project services/api python scripts/demo_control.py daily-close",
                "demo:golden": "uv run --project services/api python scripts/golden_flow.py",
                "test:golden": "uv run --project services/api pytest -q services/api/tests/integration/test_golden_api.py",
                "eval:offline": "uv run --project services/api python evals/run_offline_harness.py && uv run --project services/api python evals/run_narrative_guard.py",
                "eval:lmstudio:live": "uv run --project services/api python evals/run_live_lmstudio_harness.py",
                "test:local-model": "uv run --project services/api pytest -q services/api/tests/models/test_lm_studio.py services/api/tests/agent/test_harness_controller.py && pnpm eval:offline",
                "test:cloud-model": "uv run --project services/api pytest -q services/api/tests/models/test_narrative_guard.py services/api/tests/agent/test_harness_controller.py",
            }
        )
    update_json("package.json", root_package)

    def desktop_package(value: dict) -> None:
        value["scripts"]["test"] = "pnpm build:electron && node --test tests/*.test.mjs"
    update_json("apps/desktop/package.json", desktop_package)

    replace_once(
        "services/api/pyproject.toml",
        'license = "LicenseRef-Provisional-Apache-2.0"',
        'license = "Apache-2.0"',
    )


def harden_api_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/router.py"
    replace_once(
        path,
        "    Query,\n    UploadFile,\n)",
        "    Path,\n    Query,\n    UploadFile,\n)",
    )
    replace_once(
        path,
        "from finance_agent.agent.events import SequenceGap, format_sse\n",
        "from finance_agent.agent.events import SequenceGap, format_sse\nfrom finance_agent.api.http_security import (\n    IDENTIFIER_MAX_LENGTH,\n    IDENTIFIER_MIN_LENGTH,\n    IDENTIFIER_PATTERN,\n    MAX_CSV_BYTES,\n    UploadTooLarge,\n    content_disposition,\n    read_upload_with_limit,\n)\n",
    )
    replace_once(
        path,
        "Services = Annotated[RouteServices, Depends(get_route_services)]\n",
        "PathIdentifier = Annotated[\n    str,\n    Path(\n        min_length=IDENTIFIER_MIN_LENGTH,\n        max_length=IDENTIFIER_MAX_LENGTH,\n        pattern=IDENTIFIER_PATTERN,\n    ),\n]\n\nServices = Annotated[RouteServices, Depends(get_route_services)]\n",
    )
    replace_once(
        path,
        "        content = await file.read()\n        if not content:\n            raise HTTPException(status_code=422, detail=\"CSV file is empty\")\n        if len(content) > 10_000_000:\n            raise HTTPException(status_code=413, detail=\"CSV exceeds the 10 MB limit\")\n",
        "        try:\n            content = await read_upload_with_limit(file, max_bytes=MAX_CSV_BYTES)\n        except UploadTooLarge as exc:\n            raise HTTPException(status_code=413, detail=\"CSV exceeds the 10 MB limit\") from exc\n        if not content:\n            raise HTTPException(status_code=422, detail=\"CSV file is empty\")\n",
    )
    for name in ("run_id", "thread_id", "workspace_id", "event_id", "artifact_id"):
        content = read(path)
        content, count = re.subn(rf"(?m)^(\s*){name}: str,$", rf"\1{name}: PathIdentifier,", content)
        if count == 0:
            raise RuntimeError(f"{path}: no path parameter replacement for {name}")
        write(path, content)
    replace_once(
        path,
        "        resume = resume or 0\n",
        "        if resume is not None and resume < 0:\n            raise HTTPException(status_code=400, detail=\"event sequence must be non-negative\")\n        resume = resume or 0\n",
    )
    replace_once(
        path,
        "                \"Content-Disposition\": f'inline; filename=\"{value.filename}\"',\n",
        "                \"Content-Disposition\": content_disposition(value.filename),\n",
    )


def add_material_state_migration() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    content = read(path)
    versions = [int(value) for value in re.findall(r"version=(\\d+)", content)]
    next_version = max(versions) + 1
    migration = f''',\n    Migration(\n        version={next_version},\n        name="provider_quarantine_and_turn_provenance",\n        sql="""\n        ALTER TABLE conversation_turns\n            ADD COLUMN model_mode TEXT NOT NULL DEFAULT 'local'\n            CHECK (model_mode IN ('local', 'hybrid', 'cloud'));\n\n        CREATE TABLE provider_transaction_events (\n            event_id TEXT PRIMARY KEY,\n            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),\n            provider TEXT NOT NULL,\n            provider_account_id TEXT NOT NULL,\n            provider_transaction_id TEXT NOT NULL,\n            source_item_id TEXT NOT NULL REFERENCES source_items(source_item_id),\n            event_type TEXT NOT NULL CHECK (event_type IN (\n                'added', 'modified', 'removed', 'quarantined'\n            )),\n            occurred_on TEXT,\n            description TEXT NOT NULL,\n            amount_minor INTEGER,\n            currency TEXT NOT NULL,\n            payload_json TEXT NOT NULL,\n            supersedes_event_id TEXT REFERENCES provider_transaction_events(event_id),\n            recorded_at TEXT NOT NULL,\n            UNIQUE (workspace_id, provider, event_id)\n        );\n\n        CREATE INDEX provider_events_reference\n            ON provider_transaction_events(\n                workspace_id, provider, provider_account_id, provider_transaction_id, recorded_at\n            );\n        """,\n    )'''
    closing = content.rfind("\n)")
    if closing < 0:
        raise RuntimeError("could not locate MIGRATIONS tuple close")
    write(path, content[:closing] + migration + content[closing:])


def preserve_turn_mode_and_workspace_ownership() -> None:
    path = "services/api/src/finance_agent/storage/store.py"
    replace_once(
        path,
        "        status: str = \"complete\",\n        evidence_ids: Sequence[str] = (),\n",
        "        status: str = \"complete\",\n        evidence_ids: Sequence[str] = (),\n        model_mode: str = \"local\",\n",
    )
    replace_once(
        path,
        "                    content, occurred_at,\n                    status, evidence_ids_json\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)\n",
        "                    content, occurred_at,\n                    status, evidence_ids_json, model_mode\n                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)\n",
    )
    replace_once(
        path,
        "                    canonical_json(list(evidence_ids)),\n                ),\n",
        "                    canonical_json(list(evidence_ids)),\n                    model_mode,\n                ),\n",
    )
    replace_once(
        path,
        "                connection.execute(\n                    \"UPDATE claims SET status = 'superseded' WHERE claim_id = ?\",\n                    (supersedes,),\n                )\n",
        "                target = connection.execute(\n                    \"SELECT workspace_id FROM claims WHERE claim_id = ?\",\n                    (supersedes,),\n                ).fetchone()\n                if target is None or str(target[\"workspace_id\"]) != str(claim[\"workspaceId\"]):\n                    raise ValueError(\"superseded claim must belong to the same workspace\")\n                connection.execute(\n                    \"UPDATE claims SET status = 'superseded' WHERE claim_id = ? AND workspace_id = ?\",\n                    (supersedes, claim[\"workspaceId\"]),\n                )\n",
    )
    replace_once(
        path,
        "        with self.transaction() as connection:\n            encoded = canonical_json(frame)\n            existing = connection.execute(\n                \"SELECT frame_json FROM dialogue_frames WHERE frame_id = ?\",\n",
        "        with self.transaction() as connection:\n            encoded = canonical_json(frame)\n            owner = connection.execute(\n                \"SELECT workspace_id, thread_id FROM dialogue_frames WHERE frame_id = ?\",\n                (frame[\"frameId\"],),\n            ).fetchone()\n            if owner is not None and (\n                str(owner[\"workspace_id\"]) != str(frame[\"workspaceId\"])\n                or str(owner[\"thread_id\"]) != str(frame[\"threadId\"])\n            ):\n                raise ValueError(\"frame_id is already owned by another workspace or thread\")\n            existing = connection.execute(\n                \"SELECT frame_json FROM dialogue_frames WHERE frame_id = ?\",\n",
    )

    path = "services/api/src/finance_agent/storage/conversations.py"
    replace_once(
        path,
        "            occurred_at=turn.occurred_at.isoformat(),\n        )\n",
        "            occurred_at=turn.occurred_at.isoformat(),\n            model_mode=turn.mode,\n        )\n",
    )
    replace_once(
        path,
        "            SELECT turn_id, role, content, occurred_at\n",
        "            SELECT turn_id, role, content, occurred_at, model_mode\n",
    )
    regex_once(
        path,
        r"\n        mode_row = self\.store\.fetch_one\(.*?\n        mode = str\(mode_row\[\"model_mode\"\]\) if mode_row else \"local\"\n",
        "\n",
        flags=re.S,
    )
    replace_once(path, "                mode=mode,\n", "                mode=str(row[\"model_mode\"]),\n")


def make_daily_close_material() -> None:
    path = "services/api/src/finance_agent/jobs/daily_close.py"
    replace_once(
        path,
        "from dataclasses import dataclass\nfrom typing import Any\n",
        "from collections.abc import Callable\nfrom dataclasses import dataclass\nfrom datetime import UTC, datetime, timedelta\nfrom typing import Any\n",
    )
    replace_once(
        path,
        "class DailyCloseService:\n    def __init__(self, engine: FinanceEngine, *, worker_id: str = \"worker_local_001\") -> None:\n        self.engine = engine\n        self.worker_id = worker_id\n",
        "class DailyCloseService:\n    def __init__(\n        self,\n        engine: FinanceEngine,\n        *,\n        worker_id: str = \"worker_local_001\",\n        clock: Callable[[], datetime] | None = None,\n    ) -> None:\n        self.engine = engine\n        self.worker_id = worker_id\n        self.clock = clock or (lambda: datetime.now(UTC))\n",
    )
    regex_once(
        path,
        r"    def _input_hash\(self\) -> str:\n.*?        return hashlib\.sha256\(canonical_json\(payload\)\.encode\(\)\)\.hexdigest\(\)\n",
        '''    def _input_hash(self) -> str:\n        sources = self.engine.store.fetch_all(\n            """\n            SELECT source_item_id, source_type, digest, mapping_version, status, row_count\n            FROM source_items WHERE workspace_id = ?\n            ORDER BY source_item_id\n            """,\n            (WORKSPACE_ID,),\n        )\n        rules = self.engine.store.fetch_all(\n            """\n            SELECT rule_id, merchant_contains, maximum_amount_minor, currency,\n                   target_classification, target_category, effective_from, priority, active\n            FROM classification_rules\n            WHERE workspace_id = ? AND active = 1\n            ORDER BY priority DESC, rule_id\n            """,\n            (WORKSPACE_ID,),\n        )\n        workspace = self.engine.store.fetch_one(\n            """\n            SELECT protected_reserve_minor, currency, timezone, data_through\n            FROM workspaces WHERE workspace_id = ?\n            """,\n            (WORKSPACE_ID,),\n        )\n        provider_events = self.engine.store.fetch_all(\n            """\n            SELECT provider, provider_account_id, provider_transaction_id, event_type,\n                   amount_minor, currency, recorded_at\n            FROM provider_transaction_events\n            WHERE workspace_id = ?\n            ORDER BY provider, provider_account_id, provider_transaction_id, recorded_at, event_id\n            """,\n            (WORKSPACE_ID,),\n        )\n        payload = {\n            "policyVersion": POLICY_VERSION,\n            "workspace": dict(workspace) if workspace is not None else None,\n            "sources": [dict(row) for row in sources],\n            "activeRules": [dict(row) for row in rules],\n            "providerEvents": [dict(row) for row in provider_events],\n        }\n        return hashlib.sha256(canonical_json(payload).encode()).hexdigest()\n''',
        flags=re.S,
    )
    replace_once(
        path,
        "        occurred_at = \"2026-07-17T08:00:04+12:00\"\n",
        "        started = self.clock()\n        if started.tzinfo is None:\n            started = started.replace(tzinfo=UTC)\n        occurred_at = started.isoformat()\n        lease_expires_at = (started + timedelta(minutes=5)).isoformat()\n",
    )
    replace_once(
        path,
        "                ) VALUES (?, 'jobdef_koru_daily_close', ?, ?, ?, 'running', ?,\n                    '2026-07-17T08:05:00+12:00', 1, ?, ?)\n",
        "                ) VALUES (?, 'jobdef_koru_daily_close', ?, ?, ?, 'running', ?,\n                    ?, 1, ?, ?)\n",
    )
    replace_once(
        path,
        "                    self.worker_id,\n                    DATA_THROUGH,\n                    correlation_id,\n",
        "                    self.worker_id,\n                    lease_expires_at,\n                    occurred_at,\n                    correlation_id,\n",
    )
    replace_once(
        path,
        "            new_findings=3,\n            new_artifacts=2,\n            new_owner_messages=1,\n",
        "            new_findings=len(derived.findings),\n            new_artifacts=len(derived.artifacts),\n            new_owner_messages=1 if close_turn_id else 0,\n",
    )


def quarantine_foreign_plaid() -> None:
    path = "services/api/src/finance_agent/finance/service.py"
    replace_once(path, "from dataclasses import dataclass\n", "from dataclasses import dataclass, replace\n")
    marker = "        plaid_account_id = account_id or PLAID_ACCOUNT_ID\n        with self.store.transaction() as connection:\n"
    branch = '''        plaid_account_id = account_id or PLAID_ACCOUNT_ID\n        if parsed.currency != "NZD":\n            with self.store.transaction() as connection:\n                existing = connection.execute(\n                    """\n                    SELECT source_item_id, row_count FROM source_items\n                    WHERE workspace_id = ? AND digest = ? AND mapping_version = ?\n                    """,\n                    (WORKSPACE_ID, parsed.digest, version),\n                ).fetchone()\n                if existing is not None:\n                    return replace(parsed, status="deduplicated")\n                connection.execute(\n                    """\n                    INSERT INTO source_items(\n                        source_item_id, workspace_id, source_type, label, digest,\n                        mapping_version, received_at, status, row_count\n                    ) VALUES (?, ?, 'plaid_fixture', ?, ?, ?, ?, 'processed', ?)\n                    """,\n                    (\n                        parsed.source_item_id,\n                        WORKSPACE_ID,\n                        parsed.account_label,\n                        parsed.digest,\n                        version,\n                        parsed.synced_at,\n                        parsed.row_count,\n                    ),\n                )\n                for transaction in parsed.transactions:\n                    provider_account_id = plaid_account_id\n                    provider_transaction_id = transaction.external_reference\n                    event_id = stable_id(\n                        "prevt", "plaid", provider_account_id, provider_transaction_id, parsed.digest\n                    )\n                    connection.execute(\n                        """\n                        INSERT INTO provider_transaction_events(\n                            event_id, workspace_id, provider, provider_account_id,\n                            provider_transaction_id, source_item_id, event_type, occurred_on,\n                            description, amount_minor, currency, payload_json,\n                            supersedes_event_id, recorded_at\n                        ) VALUES (?, ?, 'plaid', ?, ?, ?, 'quarantined', ?, ?, ?, ?, ?, NULL, ?)\n                        """,\n                        (\n                            event_id,\n                            WORKSPACE_ID,\n                            provider_account_id,\n                            provider_transaction_id,\n                            parsed.source_item_id,\n                            transaction.occurred_on,\n                            transaction.description,\n                            transaction.amount_minor,\n                            parsed.currency,\n                            canonical_json(\n                                {\n                                    "providerCurrency": parsed.currency,\n                                    "reason": "workspace_currency_mismatch",\n                                    "externalReference": transaction.external_reference,\n                                }\n                            ),\n                            parsed.synced_at,\n                        ),\n                    )\n            return replace(parsed, status="quarantined_currency_mismatch")\n\n        with self.store.transaction() as connection:\n'''
    replace_once(path, marker, branch)

    path = "services/api/src/finance_agent/api/services.py"
    marker = "        transactions = normalise_plaid_transactions(tuple(transaction_items), accounts)\n        synced_at = _now().isoformat()\n        async with self._lock:\n"
    replacement = '''        transactions = normalise_plaid_transactions(tuple(transaction_items), accounts)\n        synced_at = _now().isoformat()\n        if any(account.currency != "NZD" for account in accounts):\n            primary = accounts[0]\n            payload = {\n                "account": {\n                    "name": primary.label,\n                    "maskedNumber": primary.mask or "",\n                    "currency": primary.currency,\n                },\n                "syncedAt": synced_at,\n                "transactions": [\n                    {\n                        "occurredOn": transaction.occurred_on,\n                        "description": transaction.description,\n                        "amountMinor": transaction.amount_minor,\n                        "externalReference": transaction.external_reference,\n                        "status": "posted",\n                    }\n                    for transaction in transactions\n                ],\n            }\n            async with self._lock:\n                imported = self.engine.ingest_plaid_fixture(\n                    payload,\n                    source_item_id=_stable_id(\n                        "src", "plaid_live", synced_at, primary.provider_id\n                    ),\n                    mapping_version=PLAID_MAPPING_VERSION,\n                    account_id=primary.account_id or PLAID_ACCOUNT_ID,\n                )\n                self.working_understanding.ensure_current(workspace_id=WORKSPACE_ID)\n            return {\n                "sourceItemId": imported.source_item_id,\n                "status": imported.status,\n                "sourceSha256": imported.digest,\n                "accountCount": len(accounts),\n                "transactionCount": len(transactions),\n                "rowCount": 0,\n                "providerEventCount": imported.row_count,\n                "providerCurrency": primary.currency,\n                "ledgerCommitted": False,\n                "quarantineReason": "workspace_currency_mismatch",\n                "settledOnly": True,\n                "liveSyncAttempted": True,\n                "externalCallsMade": True,\n            }\n\n        async with self._lock:\n'''
    replace_once(path, marker, replacement)


def add_regression_tests() -> None:
    write(
        "services/api/tests/finance/test_audit_correctness.py",
        '''from __future__ import annotations\n\nfrom datetime import UTC, datetime\nfrom pathlib import Path\n\nfrom finance_agent.finance import FinanceEngine\nfrom finance_agent.jobs import DailyCloseService\nfrom finance_agent.storage import SQLiteConversationStore, SQLiteStore\nfrom finance_agent.agent.dialogue import TranscriptTurn\n\nROOT = Path(__file__).resolve().parents[4]\nCSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"\n\n\ndef engine(tmp_path: Path) -> FinanceEngine:\n    value = FinanceEngine(SQLiteStore(tmp_path / "folio.sqlite3"))\n    value.reset_demo(CSV)\n    return value\n\n\ndef test_plaid_usd_is_quarantined_outside_the_nzd_ledger(tmp_path: Path) -> None:\n    value = engine(tmp_path)\n    before = len(value.store.fetch_all("SELECT * FROM transactions"))\n    result = value.ingest_plaid_fixture()\n    after = len(value.store.fetch_all("SELECT * FROM transactions"))\n    events = value.store.fetch_all("SELECT * FROM provider_transaction_events")\n    assert result.status == "quarantined_currency_mismatch"\n    assert after == before\n    assert len(events) == result.row_count\n    assert {str(row["currency"]) for row in events} == {"USD"}\n\n\ndef test_daily_close_identity_changes_for_non_csv_sources_and_rules(tmp_path: Path) -> None:\n    value = engine(tmp_path)\n    service = DailyCloseService(value)\n    initial = service.identity().input_hash\n    value.ingest_akahu_fixture()\n    after_provider = service.identity().input_hash\n    assert after_provider != initial\n    value.create_classification_rule(\n        merchant_contains="MITRE 10",\n        maximum_amount_minor=50000,\n        target_classification="business",\n        target_category="materials",\n        effective_from="2026-07-01",\n        source_turn_id="turn_audit_rule",\n        owner_statement="MITRE 10 was business materials.",\n    )\n    assert service.identity().input_hash != after_provider\n\n\ndef test_daily_close_uses_injected_clock_and_computed_counts(tmp_path: Path) -> None:\n    value = engine(tmp_path)\n    fixed = datetime(2026, 8, 26, 9, 30, tzinfo=UTC)\n    result = DailyCloseService(value, clock=lambda: fixed).run()\n    row = value.store.fetch_one("SELECT * FROM job_runs WHERE run_id = ?", (result.run_id,))\n    assert row is not None\n    assert str(row["started_at"]) == fixed.isoformat()\n    assert result.new_findings == len(value.get_snapshot()["findings"])\n    assert result.new_artifacts == len(value.get_snapshot()["artifacts"])\n\n\ndef test_each_conversation_turn_preserves_its_model_mode(tmp_path: Path) -> None:\n    value = engine(tmp_path)\n    value.store.migrate()\n    conversations = SQLiteConversationStore(value.store)\n    # The demo frame is composed by the normal service; direct turn persistence is enough here.\n    value.store.record_turn(\n        turn_id="turn_audit_local",\n        workspace_id="ws_koru_studio",\n        thread_id="thr_koru_studio_main",\n        role="owner",\n        content="Local turn",\n        occurred_at="2026-08-26T09:00:00+00:00",\n        model_mode="local",\n    )\n    value.store.record_turn(\n        turn_id="turn_audit_cloud",\n        workspace_id="ws_koru_studio",\n        thread_id="thr_koru_studio_main",\n        role="agent",\n        content="Cloud turn",\n        occurred_at="2026-08-26T09:01:00+00:00",\n        model_mode="cloud",\n    )\n    rows = value.store.fetch_all(\n        "SELECT turn_id, model_mode FROM conversation_turns "\n        "WHERE turn_id LIKE 'turn_audit_%' ORDER BY occurred_at"\n    )\n    assert [(str(row["turn_id"]), str(row["model_mode"])) for row in rows] == [\n        ("turn_audit_local", "local"),\n        ("turn_audit_cloud", "cloud"),\n    ]\n''',
    )
    write(
        "services/api/tests/storage/test_workspace_ownership.py",
        '''from __future__ import annotations\n\nfrom pathlib import Path\n\nimport pytest\n\nfrom finance_agent.finance import FinanceEngine\nfrom finance_agent.storage import SQLiteStore\n\nROOT = Path(__file__).resolve().parents[4]\nCSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"\n\n\ndef seeded(tmp_path: Path) -> SQLiteStore:\n    store = SQLiteStore(tmp_path / "folio.sqlite3")\n    FinanceEngine(store).reset_demo(CSV)\n    return store\n\n\ndef test_claim_cannot_supersede_a_claim_from_another_workspace(tmp_path: Path) -> None:\n    store = seeded(tmp_path)\n    with store.transaction() as connection:\n        connection.execute(\n            "INSERT INTO workspaces(workspace_id, name, entity_type, currency, timezone, "\n            "protected_reserve_minor, data_through, thread_id, model_mode, created_at, updated_at) "\n            "VALUES ('ws_other_business', 'Other', 'nz_sole_trader', 'NZD', 'Pacific/Auckland', "\n            "0, '2026-08-26', 'thr_other_main', 'local', '2026-08-26', '2026-08-26')"\n        )\n        connection.execute(\n            "INSERT INTO conversation_turns(turn_id, workspace_id, thread_id, role, content, "\n            "occurred_at, status, evidence_ids_json, model_mode) VALUES "\n            "('turn_other_claim', 'ws_other_business', 'thr_other_main', 'owner', 'Other', "\n            "'2026-08-26', 'complete', '[]', 'local')"\n        )\n        connection.execute(\n            "INSERT INTO claims(claim_id, workspace_id, claim_type, statement, source_turn_id, "\n            "scope_json, effective_date, recorded_at, status, supersedes_claim_id) VALUES "\n            "('claim_other', 'ws_other_business', 'business_context', 'Other claim', "\n            "'turn_other_claim', '{}', '2026-08-26', '2026-08-26', 'active', NULL)"\n        )\n    with pytest.raises(ValueError, match="same workspace"):\n        store.record_claim(\n            {\n                "claimId": "claim_koru_bad_supersession",\n                "workspaceId": "ws_koru_studio",\n                "claimType": "business_context",\n                "statement": "Koru claim",\n                "sourceTurnId": "turn_koru_morning_close",\n                "scope": {},\n                "effectiveDate": "2026-08-26",\n                "recordedAt": "2026-08-26",\n                "supersedesClaimId": "claim_other",\n            }\n        )\n''',
    )
    write("apps/desktop/tests/security.test.mjs", DESKTOP_SECURITY_TEST)


def secure_desktop_and_transport() -> None:
    write("apps/desktop/src/main/security.ts", DESKTOP_SECURITY)
    write("apps/desktop/src/main/main.ts", DESKTOP_MAIN)
    write(
        "apps/desktop/src/preload/preload.cts",
        '''import { contextBridge, ipcRenderer } from "electron";\n\ncontextBridge.exposeInMainWorld("financeDesktop", {\n  runtime: "electron",\n  apiBase: "http://127.0.0.1:8787",\n  sessionToken: process.env.FOLIO_SESSION_TOKEN || undefined,\n  pickCsv: () => ipcRenderer.invoke("finance:pick-csv"),\n  openArtifact: (artifactId: string) => ipcRenderer.invoke("finance:open-artifact", artifactId),\n});\n''',
    )
    path = "apps/desktop/src/vite-env.d.ts"
    content = read(path)
    if "sessionToken" not in content:
        content = content.replace("apiBase: string;", "apiBase: string;\n      sessionToken?: string;")
        write(path, content)
    path = "apps/desktop/src/transport.ts"
    replace_once(
        path,
        "const API_URL = (\n",
        "const SESSION_TOKEN = window.financeDesktop?.sessionToken ?? import.meta.env.VITE_FOLIO_SESSION_TOKEN;\nconst sessionHeaders = (): Record<string, string> => SESSION_TOKEN\n  ? { \"X-Folio-Session\": SESSION_TOKEN }\n  : {};\n\nconst API_URL = (\n",
    )
    replace_once(
        path,
        "        Accept: \"application/json\",\n",
        "        Accept: \"application/json\",\n        ...sessionHeaders(),\n",
    )
    replace_once(
        path,
        "    headers: { Accept: \"text/event-stream\" },\n",
        "    headers: { Accept: \"text/event-stream\", ...sessionHeaders() },\n",
    )
    replace_once(
        path,
        "    headers: { Accept: \"application/json\" },\n    body: form,\n",
        "    headers: { Accept: \"application/json\", ...sessionHeaders() },\n    body: form,\n",
    )
    path = "run"
    replace_once(
        path,
        "set +a\n\nif [[ ! -d",
        "set +a\n\nif [[ -z \"${FOLIO_SESSION_TOKEN:-}\" ]]; then\n  FOLIO_SESSION_TOKEN=\"$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')\"\nfi\nexport FOLIO_SESSION_TOKEN\nexport VITE_FOLIO_SESSION_TOKEN=\"$FOLIO_SESSION_TOKEN\"\n\nif [[ ! -d",
    )
    path = ".env.example"
    content = read(path)
    if "FOLIO_SESSION_TOKEN" not in content:
        content = content.replace(
            "FINANCE_DATABASE_PATH=./var/finance-agent.sqlite3\n",
            "FINANCE_DATABASE_PATH=./var/finance-agent.sqlite3\n# Optional outside ./run. The launcher generates an ephemeral value when blank.\nFOLIO_SESSION_TOKEN=\n",
        )
        write(path, content)
    write(
        "apps/desktop/index.html",
        '''<!doctype html>\n<html lang="en-NZ">\n  <head>\n    <meta charset="UTF-8" />\n    <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n    <meta name="theme-color" content="#0d0f0e" />\n    <meta name="color-scheme" content="dark" />\n    <meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self' http://127.0.0.1:8787 http://127.0.0.1:4173 ws://127.0.0.1:4173; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'" />\n    <link rel="icon" href="data:," />\n    <meta name="description" content="Folio, a local-first finance workspace for independent businesses." />\n    <title>Folio</title>\n  </head>\n  <body>\n    <div id="root"></div>\n    <script type="module" src="/src/main.tsx"></script>\n  </body>\n</html>\n''',
    )


def main() -> None:
    configure_packages()
    write("services/api/src/finance_agent/api/http_security.py", HTTP_SECURITY)
    write("services/api/src/finance_agent/api/app.py", APP_PY)
    write("services/api/tests/api/test_http_security.py", HTTP_TESTS)
    harden_api_routes()
    add_material_state_migration()
    preserve_turn_mode_and_workspace_ownership()
    make_daily_close_material()
    quarantine_foreign_plaid()
    secure_desktop_and_transport()
    add_regression_tests()
    write(".github/workflows/ci.yml", CI_WORKFLOW)
    write("CONTRIBUTING.md", CONTRIBUTING)
    write("SECURITY.md", SECURITY)
    write("docs/AUDIT_PROGRAMME.md", AUDIT_PROGRAMME)
    print("Audit programme transformations applied")


if __name__ == "__main__":
    main()
