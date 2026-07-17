"""Ordered run-event buffer and SSE serialization with explicit gap recovery."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from jsonschema import FormatChecker  # type: ignore[import-untyped]
from jsonschema.validators import validator_for  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field, model_validator
from referencing import Registry, Resource


class PayloadModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def __getitem__(self, key: str) -> object:
        return getattr(self, key)


class RunStartedPayload(PayloadModel):
    mode: Literal["local", "hybrid", "cloud"]
    reason: Literal["daily_close", "owner_turn", "undo", "demo_reset"]
    resume_from_sequence: int | None = Field(alias="resumeFromSequence", ge=1)


class MessageDeltaPayload(PayloadModel):
    turn_id: str = Field(alias="turnId")
    delta: str = Field(min_length=1, max_length=4000)


class MessageCompletedPayload(PayloadModel):
    turn_id: str = Field(alias="turnId")
    content: str = Field(min_length=1, max_length=8000)
    evidence_ids: list[str] = Field(alias="evidenceIds", max_length=30)


class StageStartedPayload(PayloadModel):
    stage: Literal[
        "ingest",
        "normalise",
        "deduplicate",
        "apply_rules",
        "classify",
        "findings",
        "forecast",
        "owner_pack",
        "receipt",
        "telegram_outbox",
    ]


class StageCompletedPayload(StageStartedPayload):
    status: Literal["completed", "no_op", "failed"]
    duration_ms: int = Field(alias="durationMs", ge=0)


class ToolStartedPayload(PayloadModel):
    tool_call_id: str = Field(alias="toolCallId")
    tool_name: Literal[
        "query_summary",
        "query_transactions",
        "run_cash_scenario",
        "record_business_claim",
        "create_classification_rule",
        "undo_event",
        "prepare_owner_pack",
        "show_surface",
    ] = Field(alias="toolName")


class ToolCompletedPayload(ToolStartedPayload):
    status: Literal["completed", "failed_closed"]
    duration_ms: int = Field(alias="durationMs", ge=0)
    evidence_ids: list[str] = Field(alias="evidenceIds", max_length=50)


class StateSnapshotPayload(PayloadModel):
    snapshot: dict[str, object]


class StatePatchPayload(PayloadModel):
    base_snapshot_id: str = Field(alias="baseSnapshotId")
    snapshot: dict[str, object]


class SurfaceReplacePayload(PayloadModel):
    surface: dict[str, object]


class SurfacePatchPayload(PayloadModel):
    surface_id: str = Field(alias="surfaceId")
    surface: dict[str, object]


class ReceiptCommittedPayload(PayloadModel):
    receipt_type: Literal[
        "daily_close",
        "finance_event",
        "owner_pack",
        "model",
        "egress",
        "telegram_outbox",
    ] = Field(alias="receiptType")
    receipt_id: str = Field(alias="receiptId")
    content_hash: str = Field(alias="contentHash")
    evidence_ids: list[str] = Field(alias="evidenceIds", max_length=50)


class RunFailedPayload(PayloadModel):
    code: Literal[
        "schema_invalid",
        "sequence_gap",
        "finance_service_failed",
        "model_unavailable",
        "cancelled",
    ]
    message: str = Field(min_length=1, max_length=500)
    retryable: bool
    last_sequence: int = Field(alias="lastSequence", ge=1)


class RunCompletedPayload(PayloadModel):
    status: Literal["completed", "no_op"]
    duration_ms: int = Field(alias="durationMs", ge=0)
    snapshot_id: str = Field(alias="snapshotId")
    receipt_id: str = Field(alias="receiptId")


type RunPayload = (
    RunStartedPayload
    | MessageDeltaPayload
    | MessageCompletedPayload
    | StageStartedPayload
    | StageCompletedPayload
    | ToolStartedPayload
    | ToolCompletedPayload
    | StateSnapshotPayload
    | StatePatchPayload
    | SurfaceReplacePayload
    | SurfacePatchPayload
    | ReceiptCommittedPayload
    | RunFailedPayload
    | RunCompletedPayload
)


class RunEventType(StrEnum):
    RUN_STARTED = "run.started"
    MESSAGE_DELTA = "message.delta"
    MESSAGE_COMPLETED = "message.completed"
    STAGE_STARTED = "stage.started"
    STAGE_COMPLETED = "stage.completed"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    STATE_SNAPSHOT = "state.snapshot"
    STATE_PATCH = "state.patch"
    SURFACE_REPLACE = "surface.replace"
    SURFACE_PATCH = "surface.patch"
    RECEIPT_COMMITTED = "receipt.committed"
    RUN_FAILED = "run.failed"
    RUN_COMPLETED = "run.completed"


PAYLOAD_MODELS: dict[RunEventType, type[PayloadModel]] = {
    RunEventType.RUN_STARTED: RunStartedPayload,
    RunEventType.MESSAGE_DELTA: MessageDeltaPayload,
    RunEventType.MESSAGE_COMPLETED: MessageCompletedPayload,
    RunEventType.STAGE_STARTED: StageStartedPayload,
    RunEventType.STAGE_COMPLETED: StageCompletedPayload,
    RunEventType.TOOL_STARTED: ToolStartedPayload,
    RunEventType.TOOL_COMPLETED: ToolCompletedPayload,
    RunEventType.STATE_SNAPSHOT: StateSnapshotPayload,
    RunEventType.STATE_PATCH: StatePatchPayload,
    RunEventType.SURFACE_REPLACE: SurfaceReplacePayload,
    RunEventType.SURFACE_PATCH: SurfacePatchPayload,
    RunEventType.RECEIPT_COMMITTED: ReceiptCommittedPayload,
    RunEventType.RUN_FAILED: RunFailedPayload,
    RunEventType.RUN_COMPLETED: RunCompletedPayload,
}


def _contract_validator() -> Any:
    root = Path(__file__).resolve().parents[5]
    resources: list[tuple[str, Resource[Any]]] = []
    schemas: dict[str, dict[str, Any]] = {}
    for path in (root / "contracts" / "schemas").glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        schema_id = str(schema["$id"])
        schemas[schema_id] = schema
        resources.append((schema_id, Resource.from_contents(schema)))
    schema = schemas["https://finance-agent.local/schemas/run-event.schema.json"]
    return validator_for(schema)(
        schema,
        registry=Registry().with_resources(resources),
        format_checker=FormatChecker(),
    )


RUN_EVENT_CONTRACT_VALIDATOR = _contract_validator()


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    event_id: str = Field(alias="eventId")
    thread_id: str = Field(alias="threadId")
    run_id: str = Field(alias="runId")
    sequence: int = Field(ge=1)
    occurred_at: datetime = Field(alias="occurredAt")
    type: RunEventType
    payload: RunPayload

    @model_validator(mode="before")
    @classmethod
    def correlate_payload(cls, value: object) -> object:
        if not isinstance(value, Mapping):
            return value
        raw_type = value.get("type")
        try:
            event_type = RunEventType(str(raw_type))
        except ValueError:
            return value
        payload_model = PAYLOAD_MODELS[event_type]
        correlated = dict(value)
        correlated["payload"] = payload_model.model_validate(value.get("payload"))
        return correlated

    @model_validator(mode="after")
    def validate_contract(self) -> RunEvent:
        errors = sorted(
            RUN_EVENT_CONTRACT_VALIDATOR.iter_errors(self.as_contract()),
            key=lambda error: list(error.absolute_path),
        )
        if errors:
            location = "/".join(str(part) for part in errors[0].absolute_path) or "<root>"
            raise ValueError(f"run event contract invalid at {location}: {errors[0].message}")
        return self

    def as_contract(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True)


class SequenceGap(ValueError):
    def __init__(
        self,
        *,
        run_id: str,
        requested_after: int,
        available_from: int,
        latest_sequence: int,
        resync_path: str,
    ) -> None:
        super().__init__("run event sequence gap requires a workspace snapshot resync")
        self.run_id = run_id
        self.requested_after = requested_after
        self.available_from = available_from
        self.latest_sequence = latest_sequence
        self.resync_path = resync_path


@dataclass(slots=True)
class RunEventBuffer:
    """In-memory reference implementation used by fixtures and route tests."""

    retention: int = 200
    _events: dict[str, list[RunEvent]] = field(default_factory=dict)
    _resync_paths: dict[str, str] = field(default_factory=dict)

    def clear(self) -> None:
        self._events.clear()
        self._resync_paths.clear()

    def register_run(self, run_id: str, *, resync_path: str) -> None:
        self._resync_paths[run_id] = resync_path

    def append(self, event: RunEvent) -> None:
        events = self._events.setdefault(event.run_id, [])
        if events and event.event_id == events[-1].event_id:
            if event != events[-1]:
                raise ValueError("conflicting duplicate run event")
            return
        expected = events[-1].sequence + 1 if events else 1
        if event.sequence != expected:
            raise ValueError(f"event sequence must be {expected}, got {event.sequence}")
        events.append(event)
        if len(events) > self.retention:
            del events[: len(events) - self.retention]

    def read(self, run_id: str, *, after_sequence: int = 0) -> tuple[RunEvent, ...]:
        events = self._events.get(run_id, [])
        if not events:
            return ()
        first = events[0].sequence
        latest = events[-1].sequence
        if after_sequence > latest or after_sequence + 1 < first:
            raise SequenceGap(
                run_id=run_id,
                requested_after=after_sequence,
                available_from=first,
                latest_sequence=latest,
                resync_path=self._resync_paths.get(run_id, "/v1/workspaces/unknown/snapshot"),
            )
        selected = tuple(event for event in events if event.sequence > after_sequence)
        for expected, event in enumerate(selected, start=after_sequence + 1):
            if event.sequence != expected:
                raise SequenceGap(
                    run_id=run_id,
                    requested_after=after_sequence,
                    available_from=first,
                    latest_sequence=latest,
                    resync_path=self._resync_paths.get(
                        run_id, "/v1/workspaces/unknown/snapshot"
                    ),
                )
        return selected


def format_sse(event: RunEvent) -> str:
    payload = json.dumps(event.as_contract(), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.sequence}\nevent: {event.type.value}\ndata: {payload}\n\n"
