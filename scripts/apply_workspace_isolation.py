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


def insert_method_before(path: str, class_name: str, before_name: str, method: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    before = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


WORKSPACE_MODULE = '''"""Process-scoped active workspace identity and isolated local directory."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

IDENTIFIER = re.compile(r"^[a-z][a-z0-9]{1,15}_[a-z0-9][a-z0-9_]{2,95}$")
ROOT = Path(__file__).resolve().parents[4]
DEFAULT_WORKSPACE_ID = "ws_koru_studio"


def _validated_identifier(value: str, label: str) -> str:
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} is not a valid Folio identifier")
    return value


def active_workspace_id() -> str:
    return _validated_identifier(
        os.getenv("FOLIO_ACTIVE_WORKSPACE_ID", DEFAULT_WORKSPACE_ID),
        "FOLIO_ACTIVE_WORKSPACE_ID",
    )


def active_thread_id() -> str:
    default = "thr_koru_studio_main" if active_workspace_id() == DEFAULT_WORKSPACE_ID else f"thr_{active_workspace_id()[3:]}_main"
    return _validated_identifier(
        os.getenv("FOLIO_ACTIVE_THREAD_ID", default),
        "FOLIO_ACTIVE_THREAD_ID",
    )


def active_workspace_name() -> str:
    value = os.getenv("FOLIO_ACTIVE_WORKSPACE_NAME", "Folio Demo Business").strip()
    if not value:
        raise ValueError("FOLIO_ACTIVE_WORKSPACE_NAME must not be blank")
    return value[:200]


def workspace_directory_path() -> Path:
    configured = os.getenv("FOLIO_WORKSPACE_DIRECTORY")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else (ROOT / "var" / "workspaces.sqlite3").resolve()
    )


def active_database_path() -> Path:
    configured = os.getenv("FINANCE_DATABASE_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    if active_workspace_id() == DEFAULT_WORKSPACE_ID:
        return (ROOT / "var" / "finance-agent.sqlite3").resolve()
    return WorkspaceDirectory(workspace_directory_path()).database_path(active_workspace_id())


@dataclass(frozen=True, slots=True)
class WorkspaceDirectoryEntry:
    workspace_id: str
    thread_id: str
    name: str
    database_path: str
    created_at: str
    updated_at: str
    status: str

    def as_dict(self) -> dict[str, str]:
        return {
            "workspaceId": self.workspace_id,
            "threadId": self.thread_id,
            "name": self.name,
            "databasePath": self.database_path,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "status": self.status,
        }


class WorkspaceDirectory:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or workspace_directory_path()).expanduser().resolve()
        self.root = self.path.parent / "workspace-data"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS local_workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    database_path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('active', 'archived'))
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def database_path(self, workspace_id: str) -> Path:
        value = _validated_identifier(workspace_id, "workspaceId")
        candidate = (self.root / f"{value}.sqlite3").resolve()
        if candidate.parent != self.root.resolve():
            raise ValueError("workspace database path escaped its directory")
        return candidate

    def register(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        name: str,
    ) -> WorkspaceDirectoryEntry:
        workspace = _validated_identifier(workspace_id, "workspaceId")
        thread = _validated_identifier(thread_id, "threadId")
        display_name = name.strip()
        if not display_name:
            raise ValueError("workspace name must not be blank")
        now = datetime.now(UTC).isoformat()
        database_path = self.database_path(workspace)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM local_workspaces WHERE workspace_id = ?",
                (workspace,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["thread_id"]) != thread
                    or str(existing["database_path"]) != str(database_path)
                ):
                    raise ValueError("workspace ID is already bound to another identity")
                connection.execute(
                    """
                    UPDATE local_workspaces SET name = ?, updated_at = ?, status = 'active'
                    WHERE workspace_id = ?
                    """,
                    (display_name[:200], now, workspace),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO local_workspaces(
                        workspace_id, thread_id, name, database_path,
                        created_at, updated_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        workspace,
                        thread,
                        display_name[:200],
                        str(database_path),
                        now,
                        now,
                    ),
                )
        return self.get(workspace)

    def get(self, workspace_id: str) -> WorkspaceDirectoryEntry:
        workspace = _validated_identifier(workspace_id, "workspaceId")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM local_workspaces WHERE workspace_id = ?",
                (workspace,),
            ).fetchone()
        if row is None:
            raise KeyError(workspace)
        return WorkspaceDirectoryEntry(
            workspace_id=str(row["workspace_id"]),
            thread_id=str(row["thread_id"]),
            name=str(row["name"]),
            database_path=str(row["database_path"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            status=str(row["status"]),
        )

    def list(self) -> tuple[WorkspaceDirectoryEntry, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM local_workspaces ORDER BY created_at, workspace_id"
            ).fetchall()
        return tuple(
            WorkspaceDirectoryEntry(
                workspace_id=str(row["workspace_id"]),
                thread_id=str(row["thread_id"]),
                name=str(row["name"]),
                database_path=str(row["database_path"]),
                created_at=str(row["created_at"]),
                updated_at=str(row["updated_at"]),
                status=str(row["status"]),
            )
            for row in rows
        )
'''

CLI = '''from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from finance_agent.workspace import WorkspaceDirectory


def default_thread(workspace_id: str) -> str:
    return f"thr_{workspace_id[3:]}_main"


async def initialise_demo(entry) -> None:
    os.environ["FOLIO_ACTIVE_WORKSPACE_ID"] = entry.workspace_id
    os.environ["FOLIO_ACTIVE_THREAD_ID"] = entry.thread_id
    os.environ["FOLIO_ACTIVE_WORKSPACE_NAME"] = entry.name
    os.environ["FINANCE_DATABASE_PATH"] = entry.database_path
    from finance_agent.api.services import LocalRouteServices

    services = LocalRouteServices(entry.database_path, auto_seed=True)
    try:
        with services.store.transaction() as connection:
            connection.execute(
                "UPDATE workspaces SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE workspace_id = ?",
                (entry.name, entry.workspace_id),
            )
    finally:
        await services.aclose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage isolated Folio workspace databases")
    parser.add_argument(
        "--directory",
        type=Path,
        default=None,
        help="Optional workspace directory SQLite path",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("workspace_id")
    create.add_argument("name")
    create.add_argument("--thread-id")
    create.add_argument("--seed", choices=("demo",), default="demo")
    commands.add_parser("list")
    path = commands.add_parser("path")
    path.add_argument("workspace_id")
    arguments = parser.parse_args()

    directory = WorkspaceDirectory(arguments.directory)
    if arguments.command == "create":
        entry = directory.register(
            workspace_id=arguments.workspace_id,
            thread_id=arguments.thread_id or default_thread(arguments.workspace_id),
            name=arguments.name,
        )
        asyncio.run(initialise_demo(entry))
        print(json.dumps(entry.as_dict(), indent=2))
        return 0
    if arguments.command == "list":
        print(json.dumps([entry.as_dict() for entry in directory.list()], indent=2))
        return 0
    if arguments.command == "path":
        print(directory.get(arguments.workspace_id).database_path)
        return 0
    raise AssertionError(arguments.command)


if __name__ == "__main__":
    raise SystemExit(main())
'''

SERVICE_METHOD = '''    async def workspace_directory_entries(self) -> tuple[Mapping[str, object], ...]:
        return tuple(entry.as_dict() for entry in WorkspaceDirectory().list())
'''

ROUTE = '''    @router.get(
        "/v1/workspace-directory",
        dependencies=[Depends(require_development_routes)],
    )
    async def workspace_directory_entries(services: Services) -> dict[str, object]:
        entries = await services.workspace_directory_entries()
        return {
            "activeWorkspaceId": WORKSPACE_ID,
            "singleActiveWorkspacePerProcess": True,
            "workspaces": list(entries),
        }

'''

TESTS = '''from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from finance_agent.workspace import WorkspaceDirectory


def test_workspace_directory_uses_separate_safe_database_paths(tmp_path: Path) -> None:
    directory = WorkspaceDirectory(tmp_path / "directory.sqlite3")
    first = directory.register(
        workspace_id="ws_alpha_business",
        thread_id="thr_alpha_business_main",
        name="Alpha Business",
    )
    second = directory.register(
        workspace_id="ws_beta_business",
        thread_id="thr_beta_business_main",
        name="Beta Business",
    )
    assert first.database_path != second.database_path
    assert Path(first.database_path).parent == Path(second.database_path).parent
    assert Path(first.database_path).parent.name == "workspace-data"
    assert [entry.workspace_id for entry in directory.list()] == [
        "ws_alpha_business",
        "ws_beta_business",
    ]


def test_invalid_workspace_identifier_cannot_escape_directory(tmp_path: Path) -> None:
    directory = WorkspaceDirectory(tmp_path / "directory.sqlite3")
    with pytest.raises(ValueError):
        directory.database_path("../../secret")
    with pytest.raises(ValueError):
        directory.register(
            workspace_id="ws_valid_business",
            thread_id="../../thread",
            name="Invalid",
        )


def test_process_scoped_identity_is_resolved_before_domain_import(tmp_path: Path) -> None:
    script = '''
import json
from finance_agent.workspace import active_workspace_id, active_thread_id, active_database_path
from finance_agent.finance.service import WORKSPACE_ID, THREAD_ID
print(json.dumps({
  "activeWorkspace": active_workspace_id(),
  "domainWorkspace": WORKSPACE_ID,
  "activeThread": active_thread_id(),
  "domainThread": THREAD_ID,
  "database": str(active_database_path()),
}))
'''
    environment = {
        **os.environ,
        "FOLIO_ACTIVE_WORKSPACE_ID": "ws_isolated_test",
        "FOLIO_ACTIVE_THREAD_ID": "thr_isolated_test_main",
        "FOLIO_WORKSPACE_DIRECTORY": str(tmp_path / "directory.sqlite3"),
    }
    directory = WorkspaceDirectory(environment["FOLIO_WORKSPACE_DIRECTORY"])
    directory.register(
        workspace_id="ws_isolated_test",
        thread_id="thr_isolated_test_main",
        name="Isolated Test",
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    value = json.loads(result.stdout)
    assert value["activeWorkspace"] == "ws_isolated_test"
    assert value["domainWorkspace"] == "ws_isolated_test"
    assert value["activeThread"] == "thr_isolated_test_main"
    assert value["domainThread"] == "thr_isolated_test_main"
    assert value["database"].endswith("ws_isolated_test.sqlite3")
'''


def add_workspace_module_and_cli() -> None:
    write("services/api/src/finance_agent/workspace.py", WORKSPACE_MODULE)
    write("scripts/workspace_control.py", CLI)


def centralise_identity() -> None:
    path = "services/api/src/finance_agent/finance/service.py"
    content = read(path)
    import_marker = "from finance_agent.storage import SQLiteStore, canonical_json\n"
    import_line = "from finance_agent.workspace import active_thread_id, active_workspace_id\n"
    if import_line not in content:
        if import_marker not in content:
            raise RuntimeError("finance service storage import marker missing")
        content = content.replace(import_marker, import_marker + import_line, 1)
    content = re.sub(
        r'WORKSPACE_ID = "ws_koru_studio"\nTHREAD_ID = "thr_koru_studio_main"',
        'WORKSPACE_ID = active_workspace_id()\nTHREAD_ID = active_thread_id()',
        content,
        count=1,
    )
    write(path, content)

    path = "services/api/src/finance_agent/storage/conversations.py"
    content = read(path)
    marker = "from .store import SQLiteStore\n"
    import_line = "from finance_agent.workspace import active_thread_id, active_workspace_id\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("conversation store marker missing")
        content = content.replace(marker, marker + "\n" + import_line, 1)
    content = re.sub(
        r'WORKSPACE_ID = "ws_koru_studio"\nTHREAD_ID = "thr_koru_studio_main"',
        'WORKSPACE_ID = active_workspace_id()\nTHREAD_ID = active_thread_id()',
        content,
        count=1,
    )
    write(path, content)


def update_default_database_and_services() -> None:
    path = "services/api/src/finance_agent/api/app.py"
    content = read(path)
    marker = "from finance_agent.api.services import LocalRouteServices\n"
    import_line = "from finance_agent.workspace import active_database_path\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("app services import marker missing")
        content = content.replace(marker, marker + import_line, 1)
    content = re.sub(
        r'DEFAULT_DATABASE = ROOT / "var" / "finance-agent.sqlite3"',
        'DEFAULT_DATABASE = active_database_path()',
        content,
        count=1,
    )
    write(path, content)

    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.storage import SQLiteConversationStore, SQLiteStore, canonical_json\n"
    import_line = "from finance_agent.workspace import WorkspaceDirectory, active_workspace_name\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("services storage import marker missing")
        content = content.replace(marker, marker + import_line, 1)
    seed_marker = "            self.engine.reset_demo(DEMO_CSV)\n"
    if "active_workspace_name()" not in content:
        if seed_marker not in content:
            raise RuntimeError("ensure_seeded reset marker missing")
        content = content.replace(
            seed_marker,
            seed_marker
            + "            with self.store.transaction() as connection:\n"
            + "                connection.execute(\n"
            + "                    \"UPDATE workspaces SET name = ?, updated_at = ? WHERE workspace_id = ?\",\n"
            + "                    (active_workspace_name(), _now().isoformat(), WORKSPACE_ID),\n"
            + "                )\n",
            1,
        )
    reset_marker = "            imported = self.engine.reset_demo(DEMO_CSV)\n"
    if "(active_workspace_name(), _now().isoformat(), WORKSPACE_ID)" not in content[content.find(reset_marker):content.find(reset_marker)+1000]:
        if reset_marker not in content:
            raise RuntimeError("reset_demo marker missing")
        content = content.replace(
            reset_marker,
            reset_marker
            + "            with self.store.transaction() as connection:\n"
            + "                connection.execute(\n"
            + "                    \"UPDATE workspaces SET name = ?, updated_at = ? WHERE workspace_id = ?\",\n"
            + "                    (active_workspace_name(), _now().isoformat(), WORKSPACE_ID),\n"
            + "                )\n",
            1,
        )
    write(path, content)
    insert_method_before(path, "LocalRouteServices", "list_cash_commitments", SERVICE_METHOD)


def update_protocol_and_route() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def list_cash_commitments(\n"
    addition = '''    async def workspace_directory_entries(\n        self\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("cash commitments protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    import_marker = "from finance_agent.storage.documents import DocumentIngestError\n"
    import_line = "from finance_agent.finance.service import WORKSPACE_ID\n"
    if import_line not in content:
        if import_marker not in content:
            raise RuntimeError("router import marker missing")
        content = content.replace(import_marker, import_marker + import_line, 1)
    route_marker = '    @router.post("/v1/workspaces/{workspace_id}/accounting/mappings")\n'
    if route_marker not in content:
        raise RuntimeError("accounting mapping route marker missing")
    content = content.replace(route_marker, ROUTE + route_marker, 1)
    write(path, content)


def update_run_and_scripts() -> None:
    path = "run"
    content = read(path)
    if "FOLIO_WORKSPACE_ID" not in content:
        content = content.replace(
            "DO_STOP=0\n",
            'DO_STOP=0\nFOLIO_WORKSPACE_ID="${FOLIO_ACTIVE_WORKSPACE_ID:-ws_koru_studio}"\n',
            1,
        )
        old_loop = re.search(r'for arg in "\$@"; do\n.*?\ndone\n', content, flags=re.S)
        if old_loop is None:
            raise RuntimeError("run argument loop missing")
        new_loop = '''while [[ $# -gt 0 ]]; do
  case "$1" in
    --reset) DO_RESET=1; shift ;;
    --with-lms) DO_LMS=1; shift ;;
    --electron) DO_ELECTRON=1; shift ;;
    --no-electron) DO_ELECTRON=0; shift ;;
    --stop) DO_STOP=1; shift ;;
    --workspace)
      [[ $# -ge 2 ]] || die "--workspace requires an ID"
      FOLIO_WORKSPACE_ID="$2"
      shift 2
      ;;
    -h|--help)
      sed -n '2,15p' "$0"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Try: ./run --help" >&2
      exit 2
      ;;
  esac
done
'''
        content = content[:old_loop.start()] + new_loop + content[old_loop.end():]
        env_marker = 'set +a\n'
        workspace_block = '''set +a

export FOLIO_ACTIVE_WORKSPACE_ID="$FOLIO_WORKSPACE_ID"
if [[ "$FOLIO_WORKSPACE_ID" != "ws_koru_studio" ]]; then
  WORKSPACE_JSON="$(uv run --project services/api python scripts/workspace_control.py list)"
  export FINANCE_DATABASE_PATH="$(uv run --project services/api python scripts/workspace_control.py path "$FOLIO_WORKSPACE_ID")"
  export FOLIO_ACTIVE_THREAD_ID="thr_${FOLIO_WORKSPACE_ID#ws_}_main"
fi
'''
        if env_marker not in content:
            raise RuntimeError("run env marker missing")
        content = content.replace(env_marker, workspace_block, 1)
    write(path, content)

    path = "package.json"
    value = json.loads(read(path))
    scripts = value["scripts"]
    scripts["workspace:list"] = "uv run --project services/api python scripts/workspace_control.py list"
    scripts["workspace:create"] = "uv run --project services/api python scripts/workspace_control.py create"
    write(path, json.dumps(value, indent=2) + "\n")


def update_env_docs_tests() -> None:
    path = ".env.example"
    content = read(path)
    addition = '''
# One active isolated workspace per Folio service process.
FOLIO_ACTIVE_WORKSPACE_ID=ws_koru_studio
FOLIO_ACTIVE_THREAD_ID=thr_koru_studio_main
FOLIO_ACTIVE_WORKSPACE_NAME=Folio Demo Business
# Optional registry path; defaults to ./var/workspaces.sqlite3
FOLIO_WORKSPACE_DIRECTORY=
'''
    if "FOLIO_ACTIVE_WORKSPACE_ID" not in content:
        write(path, content.rstrip() + "\n" + addition)
    write("services/api/tests/storage/test_workspace_isolation.py", TESTS)
    write("docs/WORKSPACE_ISOLATION.md", '''# Local workspace isolation\n\nFolio stores each registered business in a separate SQLite database file. Workspace and thread identity are resolved before finance modules are imported, so one service process cannot silently point different workspace IDs at the same database. Paths are generated inside a dedicated `workspace-data` directory and IDs cannot provide filesystem paths.\n\nUse `pnpm workspace:create -- <workspace_id> <name>` to create an isolated demo-seeded workspace, `pnpm workspace:list` to inspect the local registry, and `./run --workspace <workspace_id>` to start Folio against that database. The default Koru demo remains available without registry setup.\n\nThis is intentionally one active workspace per API/Electron process. It is process and file isolation, not a multi-user tenancy or role system. Switching the active business requires restarting the local service. Production identities, accountant roles, cross-workspace search and concurrent multi-business UI remain separate work.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 21: process and file workspace isolation\n\n- Workspace and thread identity are resolved from validated process configuration.\n- Registered businesses receive separate SQLite files inside a path-safe local directory.\n- The launcher can select a registered workspace and supplies its isolated database path.\n- The local directory is inspectable through CLI and development-only API metadata.\n- The default Koru demo remains backwards compatible.\n- The proof boundary is one active workspace per process; multi-user tenancy and roles are not claimed.\n'''
    if "## Stack 21: process and file workspace isolation" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_workspace_module_and_cli()
    centralise_identity()
    update_default_database_and_services()
    update_protocol_and_route()
    update_run_and_scripts()
    update_env_docs_tests()
    print("workspace isolation changes applied")


if __name__ == "__main__":
    main()
