from __future__ import annotations

import re
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


def create_lifecycle() -> None:
    write(
        "services/api/src/finance_agent/api/run_lifecycle.py",
        '''"""In-process lifecycle authority for cancellable owner-turn runs."""\n\nfrom __future__ import annotations\n\nimport asyncio\nfrom dataclasses import dataclass\nfrom enum import StrEnum\n\n\nclass RunPhase(StrEnum):\n    ACCEPTED = "accepted"\n    PLANNING = "planning"\n    EXECUTING = "executing"\n    COMPLETED = "completed"\n    FAILED = "failed"\n    CANCELLED = "cancelled"\n\n\nTERMINAL_PHASES = frozenset({RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CANCELLED})\nCANCELLABLE_PHASES = frozenset({RunPhase.ACCEPTED, RunPhase.PLANNING})\n\n\n@dataclass(slots=True)\nclass RunLifecycle:\n    run_id: str\n    phase: RunPhase\n    task: asyncio.Task[None] | None = None\n    cancellation_request_id: str | None = None\n\n\n@dataclass(frozen=True, slots=True)\nclass CancellationResult:\n    run_id: str\n    status: str\n    phase: RunPhase\n    request_id: str\n\n    def as_contract(self) -> dict[str, object]:\n        return {\n            "runId": self.run_id,\n            "status": self.status,\n            "phase": self.phase.value,\n            "requestId": self.request_id,\n        }\n\n\nclass RunLifecycleRegistry:\n    """Keep run phase and task identity without making it durable finance truth."""\n\n    def __init__(self) -> None:\n        self._runs: dict[str, RunLifecycle] = {}\n\n    def accept(self, run_id: str) -> RunLifecycle:\n        existing = self._runs.get(run_id)\n        if existing is not None:\n            return existing\n        value = RunLifecycle(run_id=run_id, phase=RunPhase.ACCEPTED)\n        self._runs[run_id] = value\n        return value\n\n    def get(self, run_id: str) -> RunLifecycle | None:\n        return self._runs.get(run_id)\n\n    def attach(self, run_id: str, task: asyncio.Task[None]) -> None:\n        value = self._runs.get(run_id)\n        if value is None:\n            raise KeyError(run_id)\n        if value.task is not None and not value.task.done():\n            raise ValueError("run already has an active task")\n        value.task = task\n\n    def transition(self, run_id: str, phase: RunPhase) -> None:\n        value = self._runs.get(run_id)\n        if value is None:\n            raise KeyError(run_id)\n        if value.phase in TERMINAL_PHASES:\n            if value.phase is phase:\n                return\n            raise ValueError("terminal run phase cannot change")\n        allowed: dict[RunPhase, frozenset[RunPhase]] = {\n            RunPhase.ACCEPTED: frozenset({RunPhase.PLANNING, RunPhase.CANCELLED, RunPhase.FAILED}),\n            RunPhase.PLANNING: frozenset({RunPhase.EXECUTING, RunPhase.CANCELLED, RunPhase.FAILED}),\n            RunPhase.EXECUTING: frozenset({RunPhase.COMPLETED, RunPhase.FAILED}),\n        }\n        if phase not in allowed.get(value.phase, frozenset()):\n            raise ValueError(f"invalid run transition: {value.phase.value} -> {phase.value}")\n        value.phase = phase\n\n    def request_cancel(self, run_id: str, request_id: str) -> CancellationResult:\n        value = self._runs.get(run_id)\n        if value is None:\n            raise KeyError(run_id)\n        if value.cancellation_request_id is not None:\n            if value.cancellation_request_id != request_id:\n                raise ValueError("run cancellation is already bound to another request id")\n            return CancellationResult(run_id, "already_requested", value.phase, request_id)\n        if value.phase in TERMINAL_PHASES:\n            return CancellationResult(run_id, "already_terminal", value.phase, request_id)\n        if value.phase not in CANCELLABLE_PHASES:\n            return CancellationResult(run_id, "too_late", value.phase, request_id)\n        value.cancellation_request_id = request_id\n        task = value.task\n        if task is not None and not task.done():\n            task.cancel()\n        return CancellationResult(run_id, "accepted", value.phase, request_id)\n\n    def active_tasks(self) -> tuple[asyncio.Task[None], ...]:\n        return tuple(\n            value.task for value in self._runs.values()\n            if value.task is not None and not value.task.done()\n        )\n''',
    )


def patch_controller() -> None:
    path = "services/api/src/finance_agent/agent/controller.py"
    value = read(path)
    value = replace_once(
        value,
        "from typing import Protocol\n",
        "from collections.abc import Callable\nfrom typing import Protocol\n",
        label="controller callback import",
    )
    value = replace_once(
        value,
        """    mode: ModelMode\n\n\n@dataclass(frozen=True, slots=True)\nclass WorkReceipt:\n""",
        """    mode: ModelMode\n    phase_callback: Callable[[str], None] | None = None\n\n\n@dataclass(frozen=True, slots=True)\nclass WorkReceipt:\n""",
        label="turn phase callback",
    )
    value = replace_once(
        value,
        """        trace.append(ControllerState.COMPILE_PLAN)\n        outcome = await self.harness.compile_plan(\n""",
        """        trace.append(ControllerState.COMPILE_PLAN)\n        if request.phase_callback is not None:\n            request.phase_callback(\"planning\")\n        outcome = await self.harness.compile_plan(\n""",
        label="planning phase notification",
    )
    value = replace_once(
        value,
        """        execution = await self.executor.execute(plan, source_turn_id=request.turn_id)\n""",
        """        if request.phase_callback is not None:\n            request.phase_callback(\"executing\")\n        execution = await self.executor.execute(plan, source_turn_id=request.turn_id)\n""",
        label="executing phase notification",
    )
    write(path, value)


def patch_route_protocol() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    value = read(path)
    value = replace_once(
        value,
        """    async def submit_turn(\n""",
        """    async def enqueue_turn(\n        self,\n        *,\n        workspace_id: str,\n        thread_id: str,\n        turn_id: str,\n        content: str,\n        mode: str,\n    ) -> Mapping[str, object]: ...\n\n    async def cancel_run(\n        self, *, run_id: str, request_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def submit_turn(\n""",
        label="run lifecycle route protocol",
    )
    write(path, value)


def patch_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/router.py"
    value = read(path)
    value = replace_once(
        value,
        """class UndoRequest(RequestModel):\n""",
        """class CancelRunRequest(RequestModel):\n    request_id: str = Field(alias=\"requestId\")\n\n\nclass UndoRequest(RequestModel):\n""",
        label="cancel request model",
    )
    value = replace_once(
        value,
        """            await services.submit_turn(\n""",
        """            await services.enqueue_turn(\n""",
        label="route enqueues turns",
    )
    anchor = '''    @router.get("/v1/workspaces/{workspace_id}/snapshot")\n'''
    addition = '''    @router.post("/v1/runs/{run_id}/cancel")\n    async def cancel_run(\n        run_id: str,\n        body: CancelRunRequest,\n        services: Services,\n    ) -> Response:\n        result = dict(await services.cancel_run(run_id=run_id, request_id=body.request_id))\n        status = str(result.get("status", ""))\n        status_code = 409 if status == "too_late" else 200\n        return JSONResponse(status_code=status_code, content=result)\n\n'''
    if anchor not in value:
        raise RuntimeError("workspace snapshot route anchor is missing")
    value = value.replace(anchor, addition + anchor, 1)
    write(path, value)


def patch_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    value = read(path)
    value = replace_once(
        value,
        "from collections.abc import Mapping, Sequence\n",
        "from collections.abc import Callable, Mapping, Sequence\n",
        label="service callback import",
    )
    value = replace_once(
        value,
        "from finance_agent.api.routes import ArtifactPayload\n",
        "from finance_agent.api.routes import ArtifactPayload\nfrom finance_agent.api.run_lifecycle import RunLifecycleRegistry, RunPhase\n",
        label="lifecycle service import",
    )
    value = replace_once(
        value,
        """        self.event_buffer = RunEventBuffer(retention=500)\n""",
        """        self.event_buffer = RunEventBuffer(retention=500)\n        self.run_lifecycle = RunLifecycleRegistry()\n        self._turn_tasks: set[asyncio.Task[None]] = set()\n""",
        label="lifecycle composition",
    )

    signature_old = '''    async def submit_turn(\n        self,\n        *,\n        workspace_id: str,\n        thread_id: str,\n        turn_id: str,\n        content: str,\n        mode: str,\n    ) -> Mapping[str, object]:\n'''
    signature_new = '''    async def submit_turn(\n        self,\n        *,\n        workspace_id: str,\n        thread_id: str,\n        turn_id: str,\n        content: str,\n        mode: str,\n        phase_callback: Callable[[str], None] | None = None,\n    ) -> Mapping[str, object]:\n'''
    value = replace_once(value, signature_old, signature_new, label="internal submit callback")
    value = replace_once(
        value,
        """                    mode=self.current_mode,\n                )\n""",
        """                    mode=self.current_mode,\n                    phase_callback=phase_callback,\n                )\n""",
        label="pass controller phase callback",
    )

    submit_anchor = signature_new
    enqueue_methods = '''    def _append_initial_run_event(self, run_id: str, thread_id: str, mode: str) -> None:\n        existing = self.event_buffer.read(run_id)\n        if existing:\n            return\n        self.event_buffer.append(\n            RunEvent.model_validate(\n                {\n                    "eventId": _stable_id("streamevt", run_id, "1", "run.started"),\n                    "threadId": thread_id,\n                    "runId": run_id,\n                    "sequence": 1,\n                    "occurredAt": _now(),\n                    "type": "run.started",\n                    "payload": {\n                        "mode": mode,\n                        "reason": "owner_turn",\n                        "resumeFromSequence": None,\n                    },\n                }\n            )\n        )\n\n    def _append_failed_run_event(self, run_id: str, *, code: str, message: str, retryable: bool) -> None:\n        existing = self.event_buffer.read(run_id)\n        sequence = existing[-1].sequence if existing else 0\n        self.event_buffer.append(\n            RunEvent.model_validate(\n                {\n                    "eventId": _stable_id("streamevt", run_id, str(sequence + 1), "run.failed"),\n                    "threadId": THREAD_ID,\n                    "runId": run_id,\n                    "sequence": sequence + 1,\n                    "occurredAt": _now(),\n                    "type": "run.failed",\n                    "payload": {\n                        "code": code,\n                        "message": message,\n                        "retryable": retryable,\n                        "lastSequence": sequence,\n                    },\n                }\n            )\n        )\n\n    async def _execute_enqueued_turn(\n        self,\n        *,\n        workspace_id: str,\n        thread_id: str,\n        turn_id: str,\n        content: str,\n        mode: str,\n        run_id: str,\n    ) -> None:\n        def phase_callback(value: str) -> None:\n            phase = RunPhase.PLANNING if value == "planning" else RunPhase.EXECUTING\n            self.run_lifecycle.transition(run_id, phase)\n\n        try:\n            await self.submit_turn(\n                workspace_id=workspace_id,\n                thread_id=thread_id,\n                turn_id=turn_id,\n                content=content,\n                mode=mode,\n                phase_callback=phase_callback,\n            )\n            self.run_lifecycle.transition(run_id, RunPhase.COMPLETED)\n        except asyncio.CancelledError:\n            self.run_lifecycle.transition(run_id, RunPhase.CANCELLED)\n            self._append_failed_run_event(\n                run_id,\n                code="cancelled_before_execution",\n                message="The owner cancelled this run before deterministic execution began.",\n                retryable=True,\n            )\n        except Exception:\n            lifecycle = self.run_lifecycle.get(run_id)\n            if lifecycle is not None and lifecycle.phase not in {\n                RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CANCELLED\n            }:\n                self.run_lifecycle.transition(run_id, RunPhase.FAILED)\n            self._append_failed_run_event(\n                run_id,\n                code="run_failed",\n                message="The local run failed without committing a completion receipt.",\n                retryable=True,\n            )\n        finally:\n            current = asyncio.current_task()\n            if current is not None:\n                self._turn_tasks.discard(current)\n\n    async def enqueue_turn(\n        self,\n        *,\n        workspace_id: str,\n        thread_id: str,\n        turn_id: str,\n        content: str,\n        mode: str,\n    ) -> Mapping[str, object]:\n        if workspace_id != WORKSPACE_ID or thread_id != THREAD_ID:\n            raise KeyError("unknown workspace or thread")\n        run_id = _stable_id("run", thread_id, turn_id, content)\n        receipt = self.store.fetch_one(\n            "SELECT receipt_id FROM work_receipts WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",\n            (run_id,),\n        )\n        if receipt is not None:\n            return {\n                "runId": run_id,\n                "status": "completed",\n                "receiptId": str(receipt["receipt_id"]),\n            }\n        existing = self.run_lifecycle.get(run_id)\n        if existing is not None and existing.phase not in {\n            RunPhase.COMPLETED, RunPhase.FAILED, RunPhase.CANCELLED\n        }:\n            return {"runId": run_id, "status": existing.phase.value}\n        self.run_lifecycle.accept(run_id)\n        self.event_buffer.register_run(\n            run_id, resync_path=f"/v1/workspaces/{WORKSPACE_ID}/snapshot"\n        )\n        self._append_initial_run_event(run_id, thread_id, mode)\n        task = asyncio.create_task(\n            self._execute_enqueued_turn(\n                workspace_id=workspace_id, thread_id=thread_id, turn_id=turn_id,\n                content=content, mode=mode, run_id=run_id,\n            ),\n            name=f"folio-turn-{run_id}",\n        )\n        self.run_lifecycle.attach(run_id, task)\n        self._turn_tasks.add(task)\n        return {"runId": run_id, "status": "accepted"}\n\n    async def cancel_run(self, *, run_id: str, request_id: str) -> Mapping[str, object]:\n        return self.run_lifecycle.request_cancel(run_id, request_id).as_contract()\n\n'''
    if submit_anchor not in value:
        raise RuntimeError("submit_turn anchor is missing")
    value = value.replace(submit_anchor, enqueue_methods + submit_anchor, 1)

    segment_start = value.index("    def _register_turn_events(")
    segment_end = value.index("    async def health(", segment_start)
    segment = value[segment_start:segment_end]
    segment = replace_once(
        segment,
        """        sequence = 0\n""",
        """        existing_events = self.event_buffer.read(run_id)\n        sequence = existing_events[-1].sequence if existing_events else 0\n""",
        label="continue existing turn event sequence",
    )
    segment = replace_once(
        segment,
        '''        append(\n            "run.started",\n            {\n                "mode": self.current_mode.value,\n                "reason": "owner_turn",\n                "resumeFromSequence": None,\n            },\n        )\n''',
        '''        if not existing_events:\n            append(\n                "run.started",\n                {\n                    "mode": self.current_mode.value,\n                    "reason": "owner_turn",\n                    "resumeFromSequence": None,\n                },\n            )\n''',
        label="avoid duplicate run started event",
    )
    value = value[:segment_start] + segment + value[segment_end:]

    close_pattern = re.compile(r"    async def aclose\(self\) -> None:\n(?P<body>.*?)(?=\n    async def|\n\Z)", re.S)
    match = close_pattern.search(value)
    if match is None:
        raise RuntimeError("LocalRouteServices.aclose is missing")
    body = match.group("body")
    if "self.run_lifecycle.active_tasks" not in body:
        replacement_body = '''        tasks = self.run_lifecycle.active_tasks()\n        for task in tasks:\n            task.cancel()\n        if tasks:\n            await asyncio.gather(*tasks, return_exceptions=True)\n''' + body
        value = value[: match.start("body")] + replacement_body + value[match.end("body") :]
    write(path, value)


def patch_transport() -> None:
    path = "apps/desktop/src/transport.ts"
    value = read(path)
    value = replace_once(
        value,
        """  // Local LM Studio turns routinely take 30–120s; keep the request alive for demo recording.\n  return requestJson(`/v1/threads/${threadId}/turns`, {\n""",
        """  return requestJson(`/v1/threads/${threadId}/turns`, {\n""",
        label="remove synchronous model timeout comment",
    )
    value = replace_once(value, """  }, 180_000);\n}\n\nexport async function undoEvent""", """  }, 12_000);\n}\n\nexport async function cancelRun(runId: string): Promise<Record<string, unknown>> {\n  return requestJson(`/v1/runs/${encodeURIComponent(runId)}/cancel`, {\n    method: \"POST\",\n    body: JSON.stringify({ requestId: `cancel_desktop_${Date.now().toString(36)}` }),\n  });\n}\n\nexport async function undoEvent""", label="cancel transport")
    old = re.compile(r"export async function readRunEvents\(runId: string\): Promise<RunEvent\[]> \{.*?\n\}\n\nexport async function importCsv", re.S)
    replacement = '''export async function readRunEvents(runId: string): Promise<RunEvent[]> {\n  const events: RunEvent[] = [];\n  let afterSequence = 0;\n  for (let attempt = 0; attempt < 720; attempt += 1) {\n    const response = await fetch(\n      `${API_URL}/v1/jobs/${encodeURIComponent(runId)}/events?afterSequence=${afterSequence}`,\n      { headers: { Accept: "text/event-stream", ...sessionHeaders() } },\n    );\n    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);\n    const stream = await response.text();\n    for (const frame of stream.split(/\\r?\\n\\r?\\n/)) {\n      const data = frame\n        .split(/\\r?\\n/)\n        .filter((line) => line.startsWith("data:"))\n        .map((line) => line.slice(5).trimStart())\n        .join("\\n");\n      if (!data) continue;\n      const event = validateRunEvent(JSON.parse(data) as unknown);\n      events.push(event);\n      afterSequence = Math.max(afterSequence, event.sequence);\n      if (event.type === "run.completed" || event.type === "run.failed") return events;\n    }\n    await new Promise((resolve) => window.setTimeout(resolve, 250));\n  }\n  throw new Error("Run event polling timed out before a terminal event");\n}\n\nexport async function importCsv'''
    value, count = old.subn(replacement, value, count=1)
    if count != 1:
        raise RuntimeError("readRunEvents implementation anchor is missing")
    write(path, value)


def patch_app() -> None:
    path = "apps/desktop/src/App.tsx"
    value = read(path)
    value = replace_once(
        value,
        """  ingestAkahuFixture,\n""",
        """  cancelRun,\n  ingestAkahuFixture,\n""",
        label="cancel transport import",
    )
    value = replace_once(
        value,
        """  const runToken = useRef(0);\n""",
        """  const runToken = useRef(0);\n  const activeRunIdRef = useRef<string | null>(null);\n""",
        label="active run ref",
    )
    value = value.replace(
        """      const run = await postTurn(workspaceFixture.thread.threadId, liveSurfacePrompts[surfaceType], modelMode);\n      await readRunEvents(run.runId);\n""",
        """      const run = await postTurn(workspaceFixture.thread.threadId, liveSurfacePrompts[surfaceType], modelMode);\n      activeRunIdRef.current = run.runId;\n      await readRunEvents(run.runId);\n      activeRunIdRef.current = null;\n""",
    )
    value = value.replace(
        """        const run = await postTurn(workspaceFixture.thread.threadId, content, modelMode);\n        await readRunEvents(run.runId);\n""",
        """        const run = await postTurn(workspaceFixture.thread.threadId, content, modelMode);\n        activeRunIdRef.current = run.runId;\n        await readRunEvents(run.runId);\n        activeRunIdRef.current = null;\n""",
    )
    old_stop = re.compile(r"  const stopCurrentRun = useCallback\(\(\) => \{.*?\n  \}, \[\]\);", re.S)
    replacement = '''  const stopCurrentRun = useCallback(() => {\n    const activeRunId = activeRunIdRef.current;\n    activeRunIdRef.current = null;\n    runToken.current += 1;\n    setRunning(false);\n    setActiveStage(-1);\n    if (activeRunId && backend.mode === "live") {\n      void cancelRun(activeRunId).then((result) => {\n        const status = String(result.status ?? "");\n        if (status === "too_late") {\n          showToast("That run had already started deterministic execution. Refresh before relying on the result.");\n        }\n      }).catch(() => {\n        showToast("Folio could not confirm cancellation. Refresh before relying on the result.");\n      });\n    }\n    setTurns((current) => [...current, {\n      turnId: makeId("turn_stopped"),\n      role: "agent",\n      content: activeRunId\n        ? "I requested cancellation before deterministic execution. Refresh if this run had already crossed the commit boundary."\n        : "I stopped waiting in this window. No active cancellable run was registered.",\n      occurredAt: nowIso(),\n      status: "stopped",\n      evidenceIds: [],\n    }]);\n  }, [backend.mode, showToast]);'''
    value, count = old_stop.subn(replacement, value, count=1)
    if count != 1:
        raise RuntimeError("stopCurrentRun implementation is missing")
    value = value.replace(
        """      if (runToken.current === token) {\n        setRunning(false);\n""",
        """      if (runToken.current === token) {\n        activeRunIdRef.current = null;\n        setRunning(false);\n""",
    )
    write(path, value)


def add_tests() -> None:
    write(
        "services/api/tests/api/test_run_lifecycle.py",
        '''from __future__ import annotations\n\nimport asyncio\nfrom types import MethodType\nfrom pathlib import Path\n\nimport pytest\n\nfrom finance_agent.api.run_lifecycle import RunLifecycleRegistry, RunPhase\nfrom finance_agent.api.services import LocalRouteServices\n\n\ndef test_registry_cancels_only_before_execution() -> None:\n    registry = RunLifecycleRegistry()\n    registry.accept("run_planning")\n    registry.transition("run_planning", RunPhase.PLANNING)\n    accepted = registry.request_cancel("run_planning", "cancel_1")\n    assert accepted.status == "accepted"\n\n    registry.accept("run_executing")\n    registry.transition("run_executing", RunPhase.PLANNING)\n    registry.transition("run_executing", RunPhase.EXECUTING)\n    too_late = registry.request_cancel("run_executing", "cancel_2")\n    assert too_late.status == "too_late"\n    assert too_late.phase is RunPhase.EXECUTING\n\n\n@pytest.mark.asyncio\nasync def test_enqueued_turn_returns_before_work_and_emits_terminal_cancellation(tmp_path: Path) -> None:\n    services = LocalRouteServices(tmp_path / "runs.sqlite3", auto_seed=True)\n    planning_started = asyncio.Event()\n    never_finish = asyncio.Event()\n\n    async def slow_submit(\n        self, *, workspace_id: str, thread_id: str, turn_id: str, content: str,\n        mode: str, phase_callback=None,\n    ):\n        assert phase_callback is not None\n        phase_callback("planning")\n        planning_started.set()\n        await never_finish.wait()\n        raise AssertionError("cancelled planning run continued")\n\n    services.submit_turn = MethodType(slow_submit, services)  # type: ignore[method-assign]\n    accepted = await services.enqueue_turn(\n        workspace_id="ws_koru_studio",\n        thread_id="thr_koru_studio_main",\n        turn_id="turn_background_cancel",\n        content="Please inspect this later",\n        mode="local",\n    )\n    assert accepted["status"] == "accepted"\n    await asyncio.wait_for(planning_started.wait(), timeout=1)\n\n    cancelled = await services.cancel_run(\n        run_id=str(accepted["runId"]), request_id="cancel_background_1"\n    )\n    assert cancelled["status"] == "accepted"\n    await asyncio.sleep(0)\n    await asyncio.sleep(0)\n    events = await services.read_events(run_id=str(accepted["runId"]), after_sequence=0)\n    assert [event.type for event in events] == ["run.started", "run.failed"]\n    assert events[-1].payload.code == "cancelled_before_execution"\n    assert services.run_lifecycle.get(str(accepted["runId"])).phase is RunPhase.CANCELLED\n    await services.aclose()\n\n\n@pytest.mark.asyncio\nasync def test_cancellation_is_idempotent_for_the_same_request(tmp_path: Path) -> None:\n    services = LocalRouteServices(tmp_path / "idempotent.sqlite3", auto_seed=True)\n    blocker = asyncio.Event()\n\n    async def slow_submit(self, **kwargs):\n        kwargs["phase_callback"]("planning")\n        await blocker.wait()\n\n    services.submit_turn = MethodType(slow_submit, services)  # type: ignore[method-assign]\n    accepted = await services.enqueue_turn(\n        workspace_id="ws_koru_studio", thread_id="thr_koru_studio_main",\n        turn_id="turn_idempotent_cancel", content="Wait", mode="local",\n    )\n    await asyncio.sleep(0)\n    first = await services.cancel_run(run_id=str(accepted["runId"]), request_id="cancel_same")\n    second = await services.cancel_run(run_id=str(accepted["runId"]), request_id="cancel_same")\n    assert first["status"] == "accepted"\n    assert second["status"] in {"already_requested", "already_terminal"}\n    await services.aclose()\n''',
    )


def main() -> None:
    create_lifecycle()
    patch_controller()
    patch_route_protocol()
    patch_routes()
    patch_services()
    patch_transport()
    patch_app()
    add_tests()


if __name__ == "__main__":
    main()
