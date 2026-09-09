"""Ordered run-event buffer and SSE serialization with explicit gap recovery."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RunEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    event_id: str = Field(alias="eventId")
    thread_id: str = Field(alias="threadId")
    run_id: str = Field(alias="runId")
    sequence: int = Field(ge=1)
    occurred_at: datetime = Field(alias="occurredAt")
    type: str
    payload: Mapping[str, object]

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
    return f"id: {event.sequence}\nevent: {event.type}\ndata: {payload}\n\n"
