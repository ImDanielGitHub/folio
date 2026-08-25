from __future__ import annotations

import ast
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


MIGRATION = '''    Migration(
        version={version},
        name="local_role_access_sessions",
        sql="""
        CREATE TABLE local_access_sessions (
            session_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            role TEXT NOT NULL CHECK (role IN ('owner', 'accountant')),
            label TEXT NOT NULL CHECK (length(trim(label)) BETWEEN 1 AND 200),
            token_hash TEXT NOT NULL UNIQUE CHECK (length(token_hash) = 64),
            expires_at TEXT NOT NULL,
            revoked_at TEXT,
            created_at TEXT NOT NULL,
            last_used_at TEXT
        );

        CREATE INDEX local_access_active
            ON local_access_sessions(workspace_id, role, expires_at, revoked_at);
        """,
    ),
'''

MODULE = '''"""Optional local application roles over the loopback API boundary."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from finance_agent.storage import SQLiteStore

ROLE_HEADER: Final = b"x-folio-role-session"
ACCOUNTANT_WRITE_PATTERNS = (
    re.compile(r"^/v1/workspaces/[^/]+/accounting/(?:mappings|export-preview|exports)$"),
    re.compile(r"^/v1/workspaces/[^/]+/support-bundle$"),
    re.compile(r"^/v1/workspaces/[^/]+/invoices$"),
    re.compile(r"^/v1/workspaces/[^/]+/receivables/scan$"),
)
OWNER_ONLY_PATTERNS = (
    re.compile(r"^/v1/demo/reset$"),
    re.compile(r"^/v1/ingest/"),
    re.compile(r"^/v1/connectors/"),
    re.compile(r"^/v1/workspaces/[^/]+/connectors/"),
    re.compile(r"^/v1/threads/"),
    re.compile(r"^/v1/events/"),
    re.compile(r"^/v1/workspaces/[^/]+/privacy/"),
    re.compile(r"^/v1/workspaces/[^/]+/budgets"),
    re.compile(r"^/v1/workspaces/[^/]+/reserve-policy$"),
    re.compile(r"^/v1/workspaces/[^/]+/invoices/[^/]+/(?:issue|void)$"),
    re.compile(r"^/v1/workspaces/[^/]+/receivables/candidates/[^/]+/(?:confirm|reject)$"),
    re.compile(r"^/v1/workspaces/[^/]+/(?:restore|destroy|backups)"),
)


def role_sessions_enabled() -> bool:
    return os.getenv("FOLIO_ROLE_SESSIONS_ENABLED", "false").lower() in {
        "1", "true", "yes", "on"
    }


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:24]}"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value.decode("latin-1")
    return None


@dataclass(frozen=True, slots=True)
class AccessSession:
    session_id: str
    workspace_id: str
    role: str
    label: str
    expires_at: str
    revoked_at: str | None
    created_at: str
    last_used_at: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "sessionId": self.session_id,
            "workspaceId": self.workspace_id,
            "role": self.role,
            "label": self.label,
            "expiresAt": self.expires_at,
            "revokedAt": self.revoked_at,
            "createdAt": self.created_at,
            "lastUsedAt": self.last_used_at,
            "tokenIncluded": False,
        }


class LocalAccessSessionService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _row(row) -> AccessSession:
        return AccessSession(
            session_id=str(row["session_id"]),
            workspace_id=str(row["workspace_id"]),
            role=str(row["role"]),
            label=str(row["label"]),
            expires_at=str(row["expires_at"]),
            revoked_at=str(row["revoked_at"]) if row["revoked_at"] else None,
            created_at=str(row["created_at"]),
            last_used_at=str(row["last_used_at"]) if row["last_used_at"] else None,
        )

    def issue(
        self,
        *,
        workspace_id: str,
        role: str,
        label: str,
        expires_in_hours: int,
    ) -> tuple[AccessSession, str]:
        if role not in {"owner", "accountant"}:
            raise ValueError("role must be owner or accountant")
        label_value = label.strip()
        if not label_value:
            raise ValueError("session label must not be blank")
        if not 1 <= expires_in_hours <= 24 * 90:
            raise ValueError("expiresInHours must be between 1 and 2160")
        now = datetime.now(UTC)
        token = secrets.token_urlsafe(48)
        session_id = _stable_id(
            "access", workspace_id, role, label_value, now.isoformat(), token
        )
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO local_access_sessions(
                    session_id, workspace_id, role, label, token_hash,
                    expires_at, revoked_at, created_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL)
                """,
                (
                    session_id,
                    workspace_id,
                    role,
                    label_value[:200],
                    _token_hash(token),
                    (now + timedelta(hours=expires_in_hours)).isoformat(),
                    now.isoformat(),
                ),
            )
        row = self.store.fetch_one(
            "SELECT * FROM local_access_sessions WHERE session_id = ?",
            (session_id,),
        )
        assert row is not None
        return self._row(row), token

    def validate(self, token: str) -> AccessSession | None:
        token_value = token.strip()
        if not token_value:
            return None
        candidate_hash = _token_hash(token_value)
        rows = self.store.fetch_all(
            """
            SELECT * FROM local_access_sessions
            WHERE revoked_at IS NULL AND expires_at > ?
            ORDER BY created_at DESC
            """,
            (datetime.now(UTC).isoformat(),),
        )
        matched = None
        for row in rows:
            if hmac.compare_digest(str(row["token_hash"]), candidate_hash):
                matched = row
                break
        if matched is None:
            return None
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            connection.execute(
                "UPDATE local_access_sessions SET last_used_at = ? WHERE session_id = ?",
                (now, matched["session_id"]),
            )
        refreshed = self.store.fetch_one(
            "SELECT * FROM local_access_sessions WHERE session_id = ?",
            (matched["session_id"],),
        )
        assert refreshed is not None
        return self._row(refreshed)

    def revoke(self, *, workspace_id: str, session_id: str) -> AccessSession:
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE local_access_sessions SET revoked_at = ?
                WHERE workspace_id = ? AND session_id = ? AND revoked_at IS NULL
                """,
                (now, workspace_id, session_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(session_id)
        row = self.store.fetch_one(
            "SELECT * FROM local_access_sessions WHERE session_id = ?",
            (session_id,),
        )
        assert row is not None
        return self._row(row)

    def list(self, workspace_id: str) -> tuple[AccessSession, ...]:
        return tuple(
            self._row(row)
            for row in self.store.fetch_all(
                """
                SELECT * FROM local_access_sessions
                WHERE workspace_id = ? ORDER BY created_at DESC, session_id
                """,
                (workspace_id,),
            )
        )


def accountant_write_allowed(path: str) -> bool:
    return any(pattern.fullmatch(path) for pattern in ACCOUNTANT_WRITE_PATTERNS)


def owner_required(path: str) -> bool:
    return any(pattern.match(path) for pattern in OWNER_ONLY_PATTERNS)


class RoleAccessMiddleware:
    def __init__(self, app: ASGIApp, *, store: SQLiteStore) -> None:
        self.app = app
        self.sessions = LocalAccessSessionService(store)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not role_sessions_enabled():
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if path == "/health":
            await self.app(scope, receive, send)
            return
        token = _header(scope, ROLE_HEADER)
        session = self.sessions.validate(token or "")
        if session is None:
            await JSONResponse(
                status_code=401,
                content={
                    "detail": "A valid X-Folio-Role-Session token is required",
                    "roleSessionsEnabled": True,
                },
                headers={"Cache-Control": "no-store"},
            )(scope, receive, send)
            return
        method = str(scope.get("method") or "GET").upper()
        if session.role == "accountant" and method not in {"GET", "HEAD", "OPTIONS"}:
            if owner_required(path) or not accountant_write_allowed(path):
                await JSONResponse(
                    status_code=403,
                    content={
                        "detail": "This operation requires the owner role",
                        "currentRole": "accountant",
                    },
                    headers={"Cache-Control": "no-store"},
                )(scope, receive, send)
                return
        state = scope.setdefault("state", {})
        state["folio_access_session"] = session.as_dict()
        await self.app(scope, receive, send)
'''

CLI = '''from __future__ import annotations

import argparse
import json

from finance_agent.storage import SQLiteStore
from finance_agent.workspace import active_database_path, active_workspace_id
from finance_agent.access_control import LocalAccessSessionService


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage local Folio owner/accountant API sessions")
    commands = parser.add_subparsers(dest="command", required=True)
    issue = commands.add_parser("issue")
    issue.add_argument("role", choices=("owner", "accountant"))
    issue.add_argument("label")
    issue.add_argument("--hours", type=int, default=24)
    commands.add_parser("list")
    revoke = commands.add_parser("revoke")
    revoke.add_argument("session_id")
    arguments = parser.parse_args()
    service = LocalAccessSessionService(SQLiteStore(active_database_path()))
    workspace_id = active_workspace_id()
    if arguments.command == "issue":
        session, token = service.issue(
            workspace_id=workspace_id,
            role=arguments.role,
            label=arguments.label,
            expires_in_hours=arguments.hours,
        )
        print(json.dumps({**session.as_dict(), "token": token}, indent=2))
        return 0
    if arguments.command == "list":
        print(json.dumps([value.as_dict() for value in service.list(workspace_id)], indent=2))
        return 0
    if arguments.command == "revoke":
        print(json.dumps(service.revoke(workspace_id=workspace_id, session_id=arguments.session_id).as_dict(), indent=2))
        return 0
    raise AssertionError(arguments.command)


if __name__ == "__main__":
    raise SystemExit(main())
'''

CURRENT_ROUTE = '''    @router.get("/v1/access/current")
    async def current_access_session(request: Request) -> dict[str, object]:
        session = getattr(request.state, "folio_access_session", None)
        return {
            "roleSessionsEnabled": role_sessions_enabled(),
            "session": session,
            "sameOsUserFilesystemIsolation": False,
        }

'''

TESTS = '''from __future__ import annotations

from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from finance_agent.access_control import (
    LocalAccessSessionService,
    RoleAccessMiddleware,
)
from finance_agent.finance import FinanceEngine
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    sessions = LocalAccessSessionService(store)

    async def route(request):
        return JSONResponse({"ok": True, "path": request.url.path})

    app = Starlette(
        routes=[
            Route("/health", route, methods=["GET"]),
            Route("/v1/workspaces/ws_koru_studio/snapshot", route, methods=["GET"]),
            Route("/v1/workspaces/ws_koru_studio/accounting/export-preview", route, methods=["POST"]),
            Route("/v1/workspaces/ws_koru_studio/invoices", route, methods=["POST"]),
            Route("/v1/workspaces/ws_koru_studio/invoices/invoice_test/issue", route, methods=["POST"]),
            Route("/v1/demo/reset", route, methods=["POST"]),
            Route("/v1/threads/thr_koru_studio_main/turns", route, methods=["POST"]),
        ]
    )
    app.add_middleware(RoleAccessMiddleware, store=store)
    return store, sessions, TestClient(app)


def header(token: str) -> dict[str, str]:
    return {"X-Folio-Role-Session": token}


def test_accountant_can_read_and_prepare_but_not_mutate_owner_state(tmp_path: Path, monkeypatch) -> None:
    _store, sessions, client = setup(tmp_path)
    monkeypatch.setenv("FOLIO_ROLE_SESSIONS_ENABLED", "true")
    _session, token = sessions.issue(
        workspace_id="ws_koru_studio",
        role="accountant",
        label="Bookkeeper",
        expires_in_hours=24,
    )
    assert client.get(
        "/v1/workspaces/ws_koru_studio/snapshot", headers=header(token)
    ).status_code == 200
    assert client.post(
        "/v1/workspaces/ws_koru_studio/accounting/export-preview", headers=header(token)
    ).status_code == 200
    assert client.post(
        "/v1/workspaces/ws_koru_studio/invoices", headers=header(token)
    ).status_code == 200
    assert client.post(
        "/v1/demo/reset", headers=header(token)
    ).status_code == 403
    assert client.post(
        "/v1/threads/thr_koru_studio_main/turns", headers=header(token)
    ).status_code == 403
    assert client.post(
        "/v1/workspaces/ws_koru_studio/invoices/invoice_test/issue", headers=header(token)
    ).status_code == 403


def test_owner_can_mutate_and_missing_token_fails_closed(tmp_path: Path, monkeypatch) -> None:
    _store, sessions, client = setup(tmp_path)
    monkeypatch.setenv("FOLIO_ROLE_SESSIONS_ENABLED", "true")
    _session, token = sessions.issue(
        workspace_id="ws_koru_studio",
        role="owner",
        label="Owner desktop",
        expires_in_hours=24,
    )
    assert client.post("/v1/demo/reset").status_code == 401
    assert client.post("/v1/demo/reset", headers=header(token)).status_code == 200
    assert client.post(
        "/v1/threads/thr_koru_studio_main/turns", headers=header(token)
    ).status_code == 200


def test_revocation_and_expiry_invalidate_token_without_storing_raw_token(tmp_path: Path) -> None:
    store, sessions, _client = setup(tmp_path)
    session, token = sessions.issue(
        workspace_id="ws_koru_studio",
        role="accountant",
        label="Temporary",
        expires_in_hours=1,
    )
    assert sessions.validate(token) is not None
    row = store.fetch_one(
        "SELECT token_hash FROM local_access_sessions WHERE session_id = ?",
        (session.session_id,),
    )
    assert str(row["token_hash"]) != token
    assert token not in str(row["token_hash"])
    sessions.revoke(workspace_id="ws_koru_studio", session_id=session.session_id)
    assert sessions.validate(token) is None
'''


def add_migration_module_cli() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    content = read(path)
    versions = [int(value) for value in re.findall(r"version=(\d+)", content)]
    version = max(versions) + 1
    closing = content.rfind("\n)")
    if closing < 0:
        raise RuntimeError("MIGRATIONS tuple close not found")
    prefix = content[:closing].rstrip()
    if not prefix.endswith(","):
        prefix += ","
    write(path, prefix + "\n" + MIGRATION.format(version=version) + content[closing:])
    write("services/api/src/finance_agent/access_control.py", MODULE)
    write("scripts/access_control.py", CLI)


def update_app_router_package() -> None:
    path = "services/api/src/finance_agent/api/app.py"
    content = read(path)
    marker = "from finance_agent.api.services import LocalRouteServices\n"
    import_line = "from finance_agent.access_control import RoleAccessMiddleware\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("app services import marker missing")
        content = content.replace(marker, import_line + marker, 1)
    middleware_marker = "    value.add_middleware(SecurityHeadersMiddleware)\n"
    addition = (
        "    value.add_middleware(RoleAccessMiddleware, store=services.store)\n"
        + middleware_marker
    )
    if "value.add_middleware(RoleAccessMiddleware" not in content:
        if middleware_marker not in content:
            raise RuntimeError("security headers middleware marker missing")
        content = content.replace(middleware_marker, addition, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    if "    Request,\n" not in content:
        marker = "    Query,\n"
        if marker not in content:
            raise RuntimeError("FastAPI Query import marker missing")
        content = content.replace(marker, marker + "    Request,\n", 1)
    import_marker = "from finance_agent.api.routes.dependencies import RouteServices, get_route_services\n"
    import_line = "from finance_agent.access_control import role_sessions_enabled\n"
    if import_line not in content:
        if import_marker not in content:
            raise RuntimeError("route dependency import marker missing")
        content = content.replace(import_marker, import_line + import_marker, 1)
    route_marker = '    @router.get("/health")\n'
    if route_marker not in content:
        raise RuntimeError("health route marker missing")
    content = content.replace(route_marker, CURRENT_ROUTE + route_marker, 1)
    write(path, content)

    path = "package.json"
    value = json.loads(read(path))
    scripts = value["scripts"]
    scripts["access:issue"] = "uv run --project services/api python scripts/access_control.py issue"
    scripts["access:list"] = "uv run --project services/api python scripts/access_control.py list"
    scripts["access:revoke"] = "uv run --project services/api python scripts/access_control.py revoke"
    write(path, json.dumps(value, indent=2) + "\n")


def tests_docs_env() -> None:
    write("services/api/tests/api/test_local_role_access.py", TESTS)
    path = ".env.example"
    content = read(path)
    addition = '''
# Optional application-level owner/accountant API roles.
# This does not prevent the same OS user from reading workspace files directly.
FOLIO_ROLE_SESSIONS_ENABLED=false
'''
    if "FOLIO_ROLE_SESSIONS_ENABLED" not in content:
        write(path, content.rstrip() + "\n" + addition)
    write("docs/LOCAL_ROLES.md", '''# Local owner and accountant roles\n\nRole sessions are optional and apply to the loopback HTTP API. `pnpm access:issue -- accountant "Bookkeeper" --hours 24` prints a token once; only its SHA-256 hash is stored. Tokens expire, record last use and can be revoked. When `FOLIO_ROLE_SESSIONS_ENABLED=true`, every API route except `/health` requires `X-Folio-Role-Session`.\n\nAccountants may read workspace views and prepare accounting exports, support bundles, invoice drafts and receivable scans. They cannot reset or ingest sources, call bank/messaging connectors, submit owner turns, undo finance events, change egress consent, mutate budgets/reserves, issue or void invoices, confirm settlements, restore/destroy data or manage backups. Owners can perform those application actions.\n\nThis is application-level control for deliberate sharing or a separate local client. It does not encrypt the database, create a separate OS account, stop the same logged-in OS user reading files, or constitute internet-facing multi-user authentication.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 32: optional local owner/accountant roles\n\n- One-time role tokens are stored only as hashes and support expiry and revocation.\n- Accountants can read and prepare bounded working papers and drafts.\n- Owner-only mutation paths fail with 403 for accountant sessions.\n- Missing or invalid role sessions fail closed when the feature is enabled.\n- Current role is visible without exposing the token.\n- The boundary is application-level, not same-OS-user filesystem isolation or internet tenancy.\n'''
    if "## Stack 32: optional local owner/accountant roles" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module_cli()
    update_app_router_package()
    tests_docs_env()
    print("local role access changes applied")


if __name__ == "__main__":
    main()
