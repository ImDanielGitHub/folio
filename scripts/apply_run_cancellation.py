from __future__ import annotations

import ast
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
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:120]!r}")
    write(path, content.replace(old, new, 1))


def replace_method(path: str, class_name: str, name: str, replacement: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    candidate = next(
        node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    if candidate.end_lineno is None:
        raise RuntimeError(f"{path}: method {class_name}.{name} has no end line")
    lines = content.splitlines(keepends=True)
    start = candidate.lineno - 1
    while start > 0 and lines[start - 1].lstrip().startswith("@"):
        start -= 1
    write(
        path,
        "".join(lines[:start])
        + replacement.rstrip()
        + "\n\n"
        + "".join(lines[candidate.end_lineno :]),
    )


def insert_method_before(path: str, class_name: str, before_name: str, method: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    before = next(
        node
        for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


MIGRATION = '''    Migration(
        version={version},
        name="turn_run_cancellation",
        sql="""
        CREATE TABLE run_cancellations (
            request_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            run_id TEXT NOT NULL,
            requested_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('requested', 'cancelled', 'not_active')
            ),
            committed_finance_events INTEGER NOT NULL DEFAULT 0
                CHECK (committed_finance_events >= 0),
            UNIQUE (workspace_id, run_id, request_id)
        );

        CREATE INDEX run_cancellations_run
            ON run_cancellations(workspace_id, run_id, requested_at);
        """,
    ),
'''

SUBMIT_TURN = '''    async def submit_turn(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        turn_id: str,
        content: str,
        mode: str,
        requested_run_id: str | None = None,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or thread_id != THREAD_ID:
            raise KeyError("unknown workspace or thread")
        self.current_mode = ModelMode(mode)
        run_id = requested_run_id or _stable_id("run", thread_id, turn_id, content)
        existing = self.store.fetch_one(
            "SELECT content FROM conversation_turns WHERE turn_id = ? AND role = 'owner'",
            (turn_id,),
        )
        if existing is not None:
            if str(existing["content"]) != content:
                raise ValueError("turnId is already bound to different content")
            receipt = self.store.fetch_one(
                """
                SELECT receipt_id FROM work_receipts
                WHERE run_id = ? ORDER BY created_at DESC LIMIT 1
                """,
                (run_id,),
            )
            if receipt is not None:
                self.working_understanding.ensure_current(workspace_id=workspace_id)
                snapshot = self.workspace_snapshot_sync(workspace_id)
                return {
                    "runId": run_id,
                    "status": "completed",
                    "question": (
                        snapshot["thread"]["activeQuestion"]["prompt"]
                        if snapshot["thread"]["activeQuestion"]
                        else None
                    ),
                    "planSource": "idempotent_replay",
                    "receiptId": str(receipt["receipt_id"]),
                    "snapshotId": snapshot["snapshotId"],
                }

        self.event_buffer.register_run(
            run_id, resync_path=f"/v1/workspaces/{WORKSPACE_ID}/snapshot"
        )
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_turn_tasks[run_id] = current_task
        try:
            async with self._lock:
                result = await self.controller.run_turn(
                    TurnRequest(
                        workspace_id=workspace_id,
                        thread_id=thread_id,
                        run_id=run_id,
                        turn_id=turn_id,
                        content=content,
                        mode=self.current_mode,
                    )
                )
                self._persist_turns(turn_id=turn_id, result=result)
                self.working_understanding.record_committed_owner_turn(
                    workspace_id=workspace_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
                self._register_turn_events(result)
                snapshot = self.workspace_snapshot_sync(workspace_id)
                return {
                    "runId": run_id,
                    "status": "completed" if result.question is None else "question",
                    "question": result.question,
                    "planSource": result.plan_source,
                    "receiptId": result.work_receipt.receipt_id,
                    "snapshotId": snapshot["snapshotId"],
                }
        except asyncio.CancelledError:
            if current_task is not None:
                current_task.uncancel()
            committed = self.store.fetch_one(
                """
                SELECT COUNT(*) AS count FROM finance_events
                WHERE workspace_id = ? AND source_turn_id = ?
                """,
                (workspace_id, turn_id),
            )
            committed_events = int(committed["count"]) if committed is not None else 0
            completed_at = _now().isoformat()
            with self.store.transaction() as connection:
                pending = connection.execute(
                    """
                    SELECT request_id FROM run_cancellations
                    WHERE workspace_id = ? AND run_id = ? AND status = 'requested'
                    ORDER BY requested_at DESC LIMIT 1
                    """,
                    (workspace_id, run_id),
                ).fetchone()
                if pending is not None:
                    connection.execute(
                        """
                        UPDATE run_cancellations
                        SET status = 'cancelled', completed_at = ?,
                            committed_finance_events = ?
                        WHERE request_id = ?
                        """,
                        (completed_at, committed_events, pending["request_id"]),
                    )
            self.working_understanding.ensure_current(workspace_id=workspace_id)
            snapshot = self.workspace_snapshot_sync(workspace_id)
            return {
                "runId": run_id,
                "status": "cancelled",
                "committedFinanceEvents": committed_events,
                "snapshotId": snapshot["snapshotId"],
                "receiptId": None,
                "question": None,
                "planSource": "cancelled_by_owner",
            }
        finally:
            if self._active_turn_tasks.get(run_id) is current_task:
                self._active_turn_tasks.pop(run_id, None)
'''

CANCEL_RUN = '''    async def cancel_run(
        self,
        *,
        workspace_id: str,
        run_id: str,
        request_id: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID:
            raise KeyError(workspace_id)
        existing = self.store.fetch_one(
            "SELECT * FROM run_cancellations WHERE request_id = ?",
            (request_id,),
        )
        if existing is not None:
            if str(existing["run_id"]) != run_id:
                raise ValueError("cancellation requestId is bound to another run")
            return {
                "runId": run_id,
                "requestId": request_id,
                "status": str(existing["status"]),
                "committedFinanceEvents": int(existing["committed_finance_events"]),
            }

        task = self._active_turn_tasks.get(run_id)
        is_active = task is not None and not task.done()
        requested_at = _now().isoformat()
        committed = self.store.fetch_one(
            """
            SELECT COUNT(*) AS count FROM finance_events
            WHERE workspace_id = ? AND correlation_id = ?
            """,
            (workspace_id, run_id),
        )
        committed_events = int(committed["count"]) if committed is not None else 0
        status = "requested" if is_active else "not_active"
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO run_cancellations(
                    request_id, workspace_id, run_id, requested_at,
                    completed_at, status, committed_finance_events
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    workspace_id,
                    run_id,
                    requested_at,
                    None if is_active else requested_at,
                    status,
                    committed_events,
                ),
            )
        if is_active and task is not None:
            task.cancel()
        return {
            "runId": run_id,
            "requestId": request_id,
            "status": "cancellation_requested" if is_active else "not_active",
            "committedFinanceEvents": committed_events,
        }
'''

PENDING_TURN = '''export type PendingTurn = {
  runId: string;
  response: Promise<{
    runId: string;
    status: "completed" | "question" | "cancelled";
    committedFinanceEvents?: number;
  }>;
};

export function postTurn(
  threadId: string,
  content: string,
  mode: "local" | "hybrid" | "cloud",
): PendingTurn {
  const stamp = Date.now().toString(36);
  const runId = `run_desktop_${stamp}`;
  const turnId = `turn_desktop_${stamp}`;
  return {
    runId,
    response: requestJson(`/v1/threads/${threadId}/turns`, {
      method: "POST",
      body: JSON.stringify({
        workspaceId: "ws_koru_studio",
        runId,
        turnId,
        content,
        mode,
      }),
    }, 180_000),
  };
}

export async function cancelRun(runId: string): Promise<Record<string, unknown>> {
  return requestJson(`/v1/jobs/${encodeURIComponent(runId)}/cancel`, {
    method: "POST",
    body: JSON.stringify({
      workspaceId: "ws_koru_studio",
      requestId: `cancel_desktop_${Date.now().toString(36)}`,
    }),
  }, 5000);
}
'''

TESTS = '''from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from finance_agent.api.services import LocalRouteServices


@pytest.mark.asyncio
async def test_in_flight_turn_can_be_cancelled_with_a_persistent_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = LocalRouteServices(tmp_path / "folio.sqlite3", auto_seed=True)
    started = asyncio.Event()

    async def slow_turn(_request):
        started.set()
        await asyncio.sleep(60)
        raise AssertionError("cancelled turn resumed")

    monkeypatch.setattr(services.controller, "run_turn", slow_turn)
    pending = asyncio.create_task(
        services.submit_turn(
            workspace_id="ws_koru_studio",
            thread_id="thr_koru_studio_main",
            turn_id="turn_cancel_target",
            content="Wait while I test cancellation.",
            mode="local",
            requested_run_id="run_cancel_target",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    cancellation = await services.cancel_run(
        workspace_id="ws_koru_studio",
        run_id="run_cancel_target",
        request_id="cancel_request_target",
    )
    assert cancellation["status"] == "cancellation_requested"
    result = await asyncio.wait_for(pending, timeout=2)
    assert result["status"] == "cancelled"
    assert result["committedFinanceEvents"] == 0
    row = services.store.fetch_one(
        "SELECT * FROM run_cancellations WHERE request_id = ?",
        ("cancel_request_target",),
    )
    assert row is not None
    assert str(row["status"]) == "cancelled"
    assert int(row["committed_finance_events"]) == 0
    await services.aclose()


@pytest.mark.asyncio
async def test_cancellation_is_idempotent_and_cannot_change_run_binding(tmp_path: Path) -> None:
    services = LocalRouteServices(tmp_path / "folio.sqlite3", auto_seed=True)
    first = await services.cancel_run(
        workspace_id="ws_koru_studio",
        run_id="run_not_active",
        request_id="cancel_request_idempotent",
    )
    second = await services.cancel_run(
        workspace_id="ws_koru_studio",
        run_id="run_not_active",
        request_id="cancel_request_idempotent",
    )
    assert first["status"] == "not_active"
    assert second["status"] == "not_active"
    with pytest.raises(ValueError, match="another run"):
        await services.cancel_run(
            workspace_id="ws_koru_studio",
            run_id="run_different",
            request_id="cancel_request_idempotent",
        )
    await services.aclose()
'''


def add_migration() -> None:
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


def update_service_protocol() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    content = content.replace(
        "        mode: str,\n    ) -> Mapping[str, object]: ...\n",
        "        mode: str,\n        requested_run_id: str | None = None,\n    ) -> Mapping[str, object]: ...\n\n"
        "    async def cancel_run(\n"
        "        self, *, workspace_id: str, run_id: str, request_id: str\n"
        "    ) -> Mapping[str, object]: ...\n",
        1,
    )
    write(path, content)


def update_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    marker = '    mode: str = Field(pattern=r"^(local|hybrid|cloud)$")\n'
    if marker not in content:
        raise RuntimeError("OwnerTurnRequest mode marker missing")
    content = content.replace(
        marker,
        '    run_id: str | None = Field(default=None, alias="runId", pattern=IDENTIFIER_PATTERN)\n'
        + marker,
        1,
    )
    undo_marker = '''class UndoRequest(RequestModel):\n    request_id: str = Field(alias="requestId")\n    event_id: str = Field(alias="eventId")\n    actor: str = Field(pattern=r"^owner$")\n    reason: str = Field(min_length=1, max_length=240)\n'''
    cancel_model = undo_marker + '''\n\nclass CancelRunRequest(RequestModel):\n    workspace_id: str = Field(alias="workspaceId", pattern=IDENTIFIER_PATTERN)\n    request_id: str = Field(alias="requestId", pattern=IDENTIFIER_PATTERN)\n'''
    if undo_marker not in content:
        raise RuntimeError("UndoRequest marker missing")
    content = content.replace(undo_marker, cancel_model, 1)
    content = content.replace(
        "                mode=body.mode,\n",
        "                mode=body.mode,\n                requested_run_id=body.run_id,\n",
        1,
    )
    route_marker = '    @router.get("/v1/workspaces/{workspace_id}/snapshot")\n'
    cancel_route = '''    @router.post("/v1/jobs/{run_id}/cancel", status_code=202)\n    async def cancel_run(\n        run_id: PathIdentifier,\n        body: CancelRunRequest,\n        services: Services,\n    ) -> dict[str, object]:\n        return dict(\n            await services.cancel_run(\n                workspace_id=body.workspace_id,\n                run_id=run_id,\n                request_id=body.request_id,\n            )\n        )\n\n'''
    if route_marker not in content:
        raise RuntimeError("workspace snapshot route marker missing")
    content = content.replace(route_marker, cancel_route + route_marker, 1)
    write(path, content)


def update_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    if "self._active_turn_tasks" not in content:
        content = content.replace(
            "        self._lock = asyncio.Lock()\n",
            "        self._lock = asyncio.Lock()\n"
            "        self._active_turn_tasks: dict[str, asyncio.Task[Any]] = {}\n",
            1,
        )
        write(path, content)
    replace_method(path, "LocalRouteServices", "submit_turn", SUBMIT_TURN)
    insert_method_before(path, "LocalRouteServices", "workspace_snapshot_sync", CANCEL_RUN)


def update_desktop() -> None:
    path = "apps/desktop/src/transport.ts"
    content = read(path)
    pattern = re.compile(
        r"export async function postTurn\(.*?\n}\n\nexport async function undoEvent",
        re.S,
    )
    match = pattern.search(content)
    if match is None:
        raise RuntimeError("transport postTurn block not found")
    content = content[: match.start()] + PENDING_TURN + "\nexport async function undoEvent" + content[match.end() :]
    write(path, content)

    path = "apps/desktop/src/App.tsx"
    content = read(path)
    if "  cancelRun,\n" not in content:
        content = content.replace(
            "  ingestAkahuFixture,\n",
            "  cancelRun,\n  ingestAkahuFixture,\n",
            1,
        )
    if "activeRunIdRef" not in content:
        content = content.replace(
            "  const runToken = useRef(0);\n",
            "  const runToken = useRef(0);\n"
            "  const activeRunIdRef = useRef<string | null>(null);\n",
            1,
        )
    old_call = "const run = await postTurn(workspaceFixture.thread.threadId, liveSurfacePrompts[surfaceType], modelMode);"
    new_call = (
        "const pending = postTurn(workspaceFixture.thread.threadId, liveSurfacePrompts[surfaceType], modelMode);\n"
        "      activeRunIdRef.current = pending.runId;\n"
        "      const run = await pending.response;\n"
        "      activeRunIdRef.current = null;"
    )
    if content.count(old_call) != 1:
        raise RuntimeError("surface postTurn call count changed")
    content = content.replace(old_call, new_call, 1)
    old_call = "const run = await postTurn(workspaceFixture.thread.threadId, content, modelMode);"
    new_call = (
        "const pending = postTurn(workspaceFixture.thread.threadId, content, modelMode);\n"
        "        activeRunIdRef.current = pending.runId;\n"
        "        const run = await pending.response;\n"
        "        activeRunIdRef.current = null;"
    )
    if content.count(old_call) != 1:
        raise RuntimeError("composer postTurn call count changed")
    content = content.replace(old_call, new_call, 1)
    stop_pattern = re.compile(
        r"  const stopCurrentRun = useCallback\(\(\) => \{.*?\n  \}, \[\]\);",
        re.S,
    )
    replacement = '''  const stopCurrentRun = useCallback(() => {\n    const backendRunId = activeRunIdRef.current;\n    activeRunIdRef.current = null;\n    runToken.current += 1;\n    setRunning(false);\n    setActiveStage(-1);\n    if (backend.mode === "live" && backendRunId) {\n      void cancelRun(backendRunId)\n        .then((result) => {\n          const committed = Number(result.committedFinanceEvents ?? 0);\n          showToast(committed > 0\n            ? `Run stopped after ${committed} committed finance event${committed === 1 ? "" : "s"}.`\n            : "Run cancellation was requested. No committed finance event was reported.");\n        })\n        .catch(() => showToast("The cancellation request could not be confirmed. Refresh before relying on this run."));\n    }\n    setTurns((current) => [...current, {\n      turnId: makeId("turn_stopped"),\n      role: "agent",\n      content: backend.mode === "live" && backendRunId\n        ? "I asked the local service to cancel this run. Any finance event already committed remains visible and auditable; refresh before relying on an in-flight result."\n        : "I stopped waiting in this window. Anything already committed remains in place.",\n      occurredAt: nowIso(),\n      status: "stopped",\n      evidenceIds: [],\n    }]);\n  }, [backend.mode, showToast]);'''
    content, count = stop_pattern.subn(replacement, content, count=1)
    if count != 1:
        raise RuntimeError("stopCurrentRun block not found")
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/integration/test_run_cancellation.py", TESTS)
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 4: real in-flight run cancellation\n\n- The desktop allocates a run ID before waiting for the turn response.\n- Stop sends an authenticated cancellation request to the local service.\n- Active turn tasks are cancelled cooperatively while model I/O is in flight.\n- Cancellation requests are idempotent and persist their final status.\n- Receipts report whether any finance events were already committed.\n- Deterministic synchronous finance stages remain non-interruptible and are not falsely described as cancelled.\n'''
    if "## Stack 4: real in-flight run cancellation" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration()
    update_service_protocol()
    update_routes()
    update_services()
    update_desktop()
    add_tests_and_docs()
    print("run cancellation changes applied")


if __name__ == "__main__":
    main()
