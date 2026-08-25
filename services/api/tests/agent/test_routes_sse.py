from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from finance_agent.agent.events import RunEvent, RunEventBuffer
from finance_agent.api.routes import ArtifactPayload, create_router

ROOT = Path(__file__).parents[4]


class FixtureRouteServices:
    def __init__(self) -> None:
        raw_events = json.loads(
            (ROOT / "fixtures" / "ui" / "daily-close-events.json").read_text()
        )
        self.buffer = RunEventBuffer(retention=5)
        self.buffer.register_run(
            "run_koru_daily_close_20260717",
            resync_path="/v1/workspaces/ws_koru_studio/snapshot",
        )
        for raw in raw_events:
            self.buffer.append(RunEvent.model_validate(raw))
        self.snapshot = json.loads(
            (ROOT / "fixtures" / "ui" / "workspace-snapshot.json").read_text()
        )

    async def health(self) -> dict[str, object]:
        return {"status": "ready", "loopback": True}

    async def reset_demo(self, workspace_id: str) -> dict[str, object]:
        return {"workspaceId": workspace_id, "snapshotId": self.snapshot["snapshotId"]}

    async def ingest_csv(self, **kwargs: Any) -> dict[str, object]:
        return {"sourceItemId": "src_koru_bank_csv_20260717", "status": "processed"}

    async def ingest_akahu_fixture(self, **kwargs: Any) -> dict[str, object]:
        return {"sourceItemId": "src_koru_akahu_fixture", "status": "ingested"}

    async def sync_akahu(self, **kwargs: Any) -> dict[str, object]:
        return {"sourceItemId": "src_koru_akahu_live", "liveSyncAttempted": True}

    async def ingest_plaid_fixture(self, **kwargs: Any) -> dict[str, object]:
        return {"sourceItemId": "src_koru_plaid_fixture", "status": "ingested"}

    async def create_plaid_link_token(self, **kwargs: Any) -> dict[str, object]:
        return {"linkToken": "link-sandbox-fixture", "environment": "sandbox"}

    async def sync_plaid(self, **kwargs: Any) -> dict[str, object]:
        return {"sourceItemId": "src_koru_plaid_live", "liveSyncAttempted": True}

    async def ingest_telegram_fixture(self, **kwargs: Any) -> dict[str, object]:
        return {"sourceItemId": "src_koru_telegram_910001", "status": "ingested"}

    async def enqueue_daily_close(self, **kwargs: Any) -> dict[str, object]:
        return {"runId": "run_koru_daily_close_20260717", "status": "queued"}

    async def read_events(self, *, run_id: str, after_sequence: int) -> tuple[RunEvent, ...]:
        return self.buffer.read(run_id, after_sequence=after_sequence)

    async def submit_turn(self, **kwargs: Any) -> dict[str, object]:
        return {"runId": "run_koru_owner_turn_fixture"}

    async def workspace_snapshot(self, workspace_id: str) -> dict[str, object]:
        return self.snapshot

    async def undo_event(self, **kwargs: Any) -> dict[str, object]:
        return {"requestId": kwargs["request_id"], "originalEventId": kwargs["event_id"]}

    async def artifact(self, artifact_id: str) -> ArtifactPayload:
        return ArtifactPayload(b"<html>fixture</html>", "text/html", "owner-pack.html", "a" * 64)

    async def model_capabilities(self) -> dict[str, object]:
        return {"modes": {"local": {"status": "unavailable"}}}

    async def connection_capabilities(self) -> dict[str, object]:
        return {
            "providers": {
                "akahu": {"status": "unconfigured"},
                "plaid": {"status": "unconfigured"},
            }
        }

    async def working_understanding_diagnostics(self, **kwargs: Any) -> dict[str, object]:
        return {
            "workspaceId": kwargs["workspace_id"],
            "summary": {"revision": 1},
            "privacy": {"rawTurnsIncluded": False},
        }


@pytest.fixture
def app() -> FastAPI:
    value = FastAPI()
    value.include_router(create_router())
    value.state.finance_route_services = FixtureRouteServices()
    return value


@pytest.mark.asyncio
async def test_all_frozen_routes_are_exposed_and_fixture_calls_run(app: FastAPI) -> None:
    paths = {
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        for method in operations
    }
    expected = {
        ("GET", "/health"),
        ("POST", "/v1/demo/reset"),
        ("POST", "/v1/ingest/csv"),
        ("POST", "/v1/ingest/akahu-fixture"),
        ("POST", "/v1/connectors/akahu/sync"),
        ("POST", "/v1/ingest/plaid-fixture"),
        ("POST", "/v1/connectors/plaid/link-token"),
        ("POST", "/v1/connectors/plaid/sync"),
        ("POST", "/v1/ingest/telegram-fixture"),
        ("POST", "/v1/jobs/daily-close"),
        ("GET", "/v1/jobs/{run_id}/events"),
        ("POST", "/v1/threads/{thread_id}/turns"),
        ("GET", "/v1/workspaces/{workspace_id}/snapshot"),
        ("POST", "/v1/events/{event_id}/undo"),
        ("GET", "/v1/artifacts/{artifact_id}"),
        ("GET", "/v1/models/capabilities"),
        ("GET", "/v1/connections/capabilities"),
        ("GET", "/v1/diagnostics/working-understanding"),
    }
    assert expected <= paths
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).json()["status"] == "ready"
        snapshot = await client.get("/v1/workspaces/ws_koru_studio/snapshot")
        assert snapshot.json()["snapshotId"] == "snap_koru_after_close"
        artifact = await client.get("/v1/artifacts/artifact_koru_owner_pack_html")
        assert artifact.status_code == 200
        diagnostics = await client.get(
            "/v1/diagnostics/working-understanding",
            params={"workspaceId": "ws_koru_studio"},
        )
        assert diagnostics.status_code == 200
        assert diagnostics.json()["privacy"]["rawTurnsIncluded"] is False


@pytest.mark.asyncio
async def test_sse_resume_is_ordered_and_gap_returns_resync_path(app: FastAPI) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        resumed = await client.get(
            "/v1/jobs/run_koru_daily_close_20260717/events?afterSequence=5"
        )
        assert resumed.status_code == 200
        assert "id: 6" in resumed.text and "id: 7" in resumed.text
        assert resumed.text.index("id: 6") < resumed.text.index("id: 7")
        gap = await client.get(
            "/v1/jobs/run_koru_daily_close_20260717/events?afterSequence=0"
        )
        assert gap.status_code == 409
        assert gap.json()["resyncPath"] == "/v1/workspaces/ws_koru_studio/snapshot"
