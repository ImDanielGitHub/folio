"""FastAPI APIRouter implementations for the frozen loopback boundary."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Path,
    Query,
    UploadFile,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from finance_agent.agent.events import SequenceGap, format_sse
from finance_agent.api.http_security import (
    IDENTIFIER_MAX_LENGTH,
    IDENTIFIER_MIN_LENGTH,
    IDENTIFIER_PATTERN,
    MAX_CSV_BYTES,
    UploadTooLarge,
    content_disposition,
    read_upload_with_limit,
)
from finance_agent.api.routes.dependencies import RouteServices, get_route_services
from finance_agent.connectors.base import ConnectorError


PathIdentifier = Annotated[
    str,
    Path(
        min_length=IDENTIFIER_MIN_LENGTH,
        max_length=IDENTIFIER_MAX_LENGTH,
        pattern=IDENTIFIER_PATTERN,
    ),
]


class RequestModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class DemoResetRequest(RequestModel):
    workspace_id: str = Field(
        default="ws_koru_studio",
        alias="workspaceId",
        min_length=IDENTIFIER_MIN_LENGTH,
        max_length=IDENTIFIER_MAX_LENGTH,
        pattern=IDENTIFIER_PATTERN,
    )


class TelegramFixtureRequest(RequestModel):
    update: dict[str, Any]
    attachment_reference: dict[str, Any] | None = Field(
        default=None, alias="attachmentReference"
    )


class AkahuFixtureRequest(RequestModel):
    account: dict[str, Any] | None = None
    synced_at: str | None = Field(default=None, alias="syncedAt")
    transactions: list[dict[str, Any]] | None = None


class AkahuSyncRequest(RequestModel):
    start: date | None = None
    end: date | None = None


class PlaidFixtureRequest(RequestModel):
    account: dict[str, Any] | None = None
    synced_at: str | None = Field(default=None, alias="syncedAt")
    transactions: list[dict[str, Any]] | None = None


class PlaidSyncRequest(RequestModel):
    public_token: str | None = Field(default=None, alias="publicToken")


class DailyCloseRequest(RequestModel):
    workspace_id: str = Field(
        alias="workspaceId",
        min_length=IDENTIFIER_MIN_LENGTH,
        max_length=IDENTIFIER_MAX_LENGTH,
        pattern=IDENTIFIER_PATTERN,
    )
    idempotency_key: str | None = Field(
        default=None, alias="idempotencyKey", max_length=160
    )


class OwnerTurnRequest(RequestModel):
    workspace_id: str = Field(
        alias="workspaceId",
        min_length=IDENTIFIER_MIN_LENGTH,
        max_length=IDENTIFIER_MAX_LENGTH,
        pattern=IDENTIFIER_PATTERN,
    )
    turn_id: str = Field(
        alias="turnId",
        min_length=IDENTIFIER_MIN_LENGTH,
        max_length=IDENTIFIER_MAX_LENGTH,
        pattern=IDENTIFIER_PATTERN,
    )
    content: str = Field(min_length=1, max_length=64_000)
    mode: str = Field(pattern=r"^(local|hybrid|cloud)$")


class UndoRequest(RequestModel):
    request_id: str = Field(
        alias="requestId",
        min_length=IDENTIFIER_MIN_LENGTH,
        max_length=IDENTIFIER_MAX_LENGTH,
        pattern=IDENTIFIER_PATTERN,
    )
    event_id: str = Field(
        alias="eventId",
        min_length=IDENTIFIER_MIN_LENGTH,
        max_length=IDENTIFIER_MAX_LENGTH,
        pattern=IDENTIFIER_PATTERN,
    )
    actor: str = Field(pattern=r"^owner$")
    reason: str = Field(min_length=1, max_length=240)


Services = Annotated[RouteServices, Depends(get_route_services)]


def create_router() -> APIRouter:
    router = APIRouter()

    @router.get("/health")
    async def health(services: Services) -> dict[str, object]:
        return dict(await services.health())

    @router.post("/v1/demo/reset")
    async def reset_demo(
        body: DemoResetRequest,
        services: Services,
    ) -> dict[str, object]:
        return dict(await services.reset_demo(body.workspace_id))

    @router.post("/v1/ingest/csv")
    async def ingest_csv(
        services: Services,
        workspace_id: Annotated[
            str,
            Form(
                alias="workspaceId",
                min_length=IDENTIFIER_MIN_LENGTH,
                max_length=IDENTIFIER_MAX_LENGTH,
                pattern=IDENTIFIER_PATTERN,
            ),
        ],
        file: Annotated[UploadFile, File()],
    ) -> dict[str, object]:
        filename = file.filename or "source.csv"
        if not filename.lower().endswith(".csv"):
            raise HTTPException(status_code=422, detail="source file must use a .csv name")
        try:
            content = await read_upload_with_limit(file, max_bytes=MAX_CSV_BYTES)
        except UploadTooLarge as exc:
            raise HTTPException(status_code=413, detail="CSV exceeds the 10 MB limit") from exc
        if not content:
            raise HTTPException(status_code=422, detail="CSV file is empty")
        return dict(
            await services.ingest_csv(
                workspace_id=workspace_id,
                filename=filename,
                content=content,
            )
        )

    @router.post("/v1/ingest/telegram-fixture")
    async def ingest_telegram_fixture(
        body: TelegramFixtureRequest,
        services: Services,
    ) -> dict[str, object]:
        return dict(
            await services.ingest_telegram_fixture(
                update=body.update,
                attachment_reference=body.attachment_reference,
            )
        )

    @router.post("/v1/ingest/akahu-fixture")
    async def ingest_akahu_fixture(
        body: AkahuFixtureRequest,
        services: Services,
    ) -> dict[str, object]:
        payload = body.model_dump(by_alias=True, exclude_none=True)
        return dict(await services.ingest_akahu_fixture(payload=payload or None))

    @router.post("/v1/connectors/akahu/sync")
    async def sync_akahu(
        services: Services,
        body: AkahuSyncRequest | None = None,
    ) -> dict[str, object]:
        body = body or AkahuSyncRequest()
        try:
            return dict(
                await services.sync_akahu(
                    start=body.start.isoformat() if body.start else None,
                    end=body.end.isoformat() if body.end else None,
                )
            )
        except ConnectorError as exc:
            status = 409 if str(exc) == "Akahu is disabled or unconfigured" else 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/ingest/plaid-fixture")
    async def ingest_plaid_fixture(
        body: PlaidFixtureRequest,
        services: Services,
    ) -> dict[str, object]:
        payload = body.model_dump(by_alias=True, exclude_none=True)
        return dict(await services.ingest_plaid_fixture(payload=payload or None))

    @router.post("/v1/connectors/plaid/link-token")
    async def create_plaid_link_token(services: Services) -> dict[str, object]:
        try:
            return dict(await services.create_plaid_link_token())
        except ConnectorError as exc:
            status = 409 if "disabled or unconfigured" in str(exc) else 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc

    @router.post("/v1/connectors/plaid/sync")
    async def sync_plaid(
        services: Services,
        body: PlaidSyncRequest | None = None,
    ) -> dict[str, object]:
        body = body or PlaidSyncRequest()
        try:
            return dict(await services.sync_plaid(public_token=body.public_token))
        except ConnectorError as exc:
            status = 409 if "disabled or unconfigured" in str(exc) else 502
            raise HTTPException(status_code=status, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/jobs/daily-close")
    async def enqueue_daily_close(
        body: DailyCloseRequest,
        services: Services,
    ) -> dict[str, object]:
        return dict(
            await services.enqueue_daily_close(
                workspace_id=body.workspace_id,
                idempotency_key=body.idempotency_key,
            )
        )

    @router.get("/v1/jobs/{run_id}/events")
    async def run_events(
        run_id: PathIdentifier,
        services: Services,
        after_sequence: Annotated[int | None, Query(alias="afterSequence", ge=0)] = None,
        last_event_id: Annotated[
            str | None,
            Header(alias="Last-Event-ID", max_length=32),
        ] = None,
    ) -> Response:
        resume = after_sequence
        if resume is None and last_event_id:
            try:
                resume = int(last_event_id)
            except ValueError as exc:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative numeric sequence",
                ) from exc
            if resume < 0:
                raise HTTPException(
                    status_code=400,
                    detail="Last-Event-ID must be a non-negative numeric sequence",
                )
        resume = resume or 0
        try:
            events = await services.read_events(run_id=run_id, after_sequence=resume)
        except SequenceGap as gap:
            return JSONResponse(
                status_code=409,
                media_type="application/problem+json",
                content={
                    "type": "https://finance-agent.local/problems/sequence-gap",
                    "title": "Run event sequence gap",
                    "status": 409,
                    "detail": str(gap),
                    "runId": gap.run_id,
                    "requestedAfterSequence": gap.requested_after,
                    "availableFromSequence": gap.available_from,
                    "latestSequence": gap.latest_sequence,
                    "resyncPath": gap.resync_path,
                },
            )

        async def stream() -> AsyncIterator[str]:
            if not events:
                yield ": keep-alive\n\n"
                return
            for event in events:
                yield format_sse(event)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @router.post("/v1/threads/{thread_id}/turns", status_code=202)
    async def submit_turn(
        thread_id: PathIdentifier,
        body: OwnerTurnRequest,
        services: Services,
    ) -> dict[str, object]:
        return dict(
            await services.submit_turn(
                workspace_id=body.workspace_id,
                thread_id=thread_id,
                turn_id=body.turn_id,
                content=body.content,
                mode=body.mode,
            )
        )

    @router.get("/v1/workspaces/{workspace_id}/snapshot")
    async def workspace_snapshot(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        return dict(await services.workspace_snapshot(workspace_id))

    @router.post("/v1/events/{event_id}/undo")
    async def undo_event(
        event_id: PathIdentifier,
        body: UndoRequest,
        services: Services,
    ) -> dict[str, object]:
        if body.event_id != event_id:
            raise HTTPException(
                status_code=409,
                detail="path event_id must match the frozen Undo request eventId",
            )
        return dict(
            await services.undo_event(
                event_id=event_id,
                request_id=body.request_id,
                actor=body.actor,
                reason=body.reason,
            )
        )

    @router.get("/v1/artifacts/{artifact_id}")
    async def artifact(artifact_id: PathIdentifier, services: Services) -> Response:
        value = await services.artifact(artifact_id)
        return Response(
            content=value.content,
            media_type=value.media_type,
            headers={
                "Content-Disposition": content_disposition(value.filename),
                "ETag": f'"{value.content_hash}"',
            },
        )

    @router.get("/v1/models/capabilities")
    async def model_capabilities(services: Services) -> dict[str, object]:
        return dict(await services.model_capabilities())

    @router.get("/v1/connections/capabilities")
    async def connection_capabilities(services: Services) -> dict[str, object]:
        return dict(await services.connection_capabilities())

    @router.get("/v1/diagnostics/working-understanding")
    async def working_understanding_diagnostics(
        services: Services,
        workspace_id: Annotated[
            str,
            Query(
                alias="workspaceId",
                min_length=IDENTIFIER_MIN_LENGTH,
                max_length=IDENTIFIER_MAX_LENGTH,
                pattern=IDENTIFIER_PATTERN,
            ),
        ],
        run_id: Annotated[
            str | None,
            Query(
                alias="runId",
                min_length=IDENTIFIER_MIN_LENGTH,
                max_length=IDENTIFIER_MAX_LENGTH,
                pattern=IDENTIFIER_PATTERN,
            ),
        ] = None,
    ) -> dict[str, object]:
        return dict(
            await services.working_understanding_diagnostics(
                workspace_id=workspace_id,
                run_id=run_id,
            )
        )

    return router


router = create_router()
