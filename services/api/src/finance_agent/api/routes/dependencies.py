"""Fixture-friendly composition boundary for all frozen loopback routes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from fastapi import HTTPException, Request

from finance_agent.agent.events import RunEvent


@dataclass(frozen=True, slots=True)
class ArtifactPayload:
    content: bytes
    media_type: str
    filename: str
    content_hash: str


class RouteServices(Protocol):
    async def health(self) -> Mapping[str, object]: ...

    async def reset_demo(self, workspace_id: str) -> Mapping[str, object]: ...

    async def ingest_csv(
        self,
        *,
        workspace_id: str,
        filename: str,
        content: bytes,
    ) -> Mapping[str, object]: ...

    async def ingest_telegram_fixture(
        self,
        *,
        update: Mapping[str, object],
        attachment_reference: Mapping[str, object] | None,
    ) -> Mapping[str, object]: ...

    async def enqueue_daily_close(
        self, *, workspace_id: str, idempotency_key: str | None
    ) -> Mapping[str, object]: ...

    async def read_events(
        self, *, run_id: str, after_sequence: int
    ) -> tuple[RunEvent, ...]: ...

    async def submit_turn(
        self,
        *,
        workspace_id: str,
        thread_id: str,
        turn_id: str,
        content: str,
        mode: str,
    ) -> Mapping[str, object]: ...

    async def workspace_snapshot(self, workspace_id: str) -> Mapping[str, object]: ...

    async def undo_event(
        self,
        *,
        event_id: str,
        request_id: str,
        actor: str,
        reason: str,
    ) -> Mapping[str, object]: ...

    async def artifact(self, artifact_id: str) -> ArtifactPayload: ...

    async def model_capabilities(self) -> Mapping[str, object]: ...


def get_route_services(request: Request) -> RouteServices:
    services = getattr(request.app.state, "finance_route_services", None)
    if services is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "Finance route services are not composed. The coordinator must set "
                "app.state.finance_route_services."
            ),
        )
    return cast(RouteServices, services)
