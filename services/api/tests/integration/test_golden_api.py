from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from finance_agent.api.app import create_app

ROOT = Path(__file__).resolve().parents[4]
TELEGRAM = json.loads(
    (ROOT / "fixtures" / "demo" / "telegram-update.json").read_text()
)
TELEGRAM_ATTACHMENT = json.loads(
    (ROOT / "fixtures" / "demo" / "telegram-attachment-reference.json").read_text()
)


@pytest.mark.asyncio
async def test_loopback_cors_rejects_null_and_unlisted_origins(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "cors.sqlite3", auto_seed=False)
    services = app.state.finance_route_services
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        allowed = await client.options(
            "/health",
            headers={
                "Origin": "http://127.0.0.1:4173",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"

        for origin in ("null", "https://example.invalid"):
            rejected = await client.options(
                "/health",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                },
            )
            assert rejected.status_code == 400
            assert "access-control-allow-origin" not in rejected.headers
    await services.aclose()


@pytest.mark.asyncio
async def test_real_golden_api_flow_is_local_exact_and_reversible(tmp_path: Path) -> None:
    app = create_app(database_path=tmp_path / "golden.sqlite3", auto_seed=False)
    services = app.state.finance_route_services
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        health = await client.get("/health")
        assert health.status_code == 200
        assert health.json()["externalCalls"] == "disabled_by_default"

        reset = await client.post(
            "/v1/demo/reset", json={"workspaceId": "ws_koru_studio"}
        )
        assert reset.status_code == 200
        assert reset.json()["rowCount"] == 10

        akahu = await client.post("/v1/ingest/akahu-fixture", json={})
        assert akahu.status_code == 200
        assert akahu.json()["rowCount"] == 6
        assert akahu.json()["liveSyncAttempted"] is False
        akahu_again = await client.post("/v1/ingest/akahu-fixture", json={})
        assert akahu_again.status_code == 200
        assert akahu_again.json()["status"] == "deduplicated"
        akahu_close = await client.post(
            "/v1/jobs/daily-close",
            json={
                "workspaceId": "ws_koru_studio",
                "idempotencyKey": "akahu-fixture-close",
            },
        )
        assert akahu_close.status_code == 200
        akahu_snapshot = (
            await client.get("/v1/workspaces/ws_koru_studio/snapshot")
        ).json()
        assert any(
            source["sourceType"] == "akahu_fixture"
            for source in akahu_snapshot["sources"]
        )

        # Restore the stable ten-row accounting baseline after connector proof.
        reset = await client.post(
            "/v1/demo/reset", json={"workspaceId": "ws_koru_studio"}
        )
        assert reset.status_code == 200

        close = await client.post(
            "/v1/jobs/daily-close",
            json={
                "workspaceId": "ws_koru_studio",
                "idempotencyKey": "golden-close-1",
            },
        )
        assert close.status_code == 200
        assert close.json()["status"] == "completed"

        events = await client.get(
            f"/v1/jobs/{close.json()['runId']}/events?afterSequence=0"
        )
        assert events.status_code == 200
        assert "event: run.started" in events.text
        assert "event: run.completed" in events.text

        snapshot = (
            await client.get("/v1/workspaces/ws_koru_studio/snapshot")
        ).json()
        assert snapshot["totals"]["businessExpenseMinor"] == 139499
        assert snapshot["totals"]["unresolvedExpenseMinor"] == 18475

        scenario = await client.post(
            "/v1/threads/thr_koru_studio_main/turns",
            json={
                "workspaceId": "ws_koru_studio",
                "turnId": "turn_koru_owner_scenario",
                "content": "Show the cash scenario for the planned laptop.",
                "mode": "local",
            },
        )
        assert scenario.status_code == 200
        assert scenario.json()["status"] == "completed"
        scenario_snapshot = (
            await client.get("/v1/workspaces/ws_koru_studio/snapshot")
        ).json()
        assert scenario_snapshot["currentSurface"]["surfaceType"] == "cash_scenario"

        correction = await client.post(
            "/v1/threads/thr_koru_studio_main/turns",
            json={
                "workspaceId": "ws_koru_studio",
                "turnId": "turn_koru_owner_mitre10",
                "content": (
                    "Mitre 10 was materials for a client fit-out. Apply this merchant "
                    "rule only below NZD 500."
                ),
                "mode": "local",
            },
        )
        assert correction.status_code == 200
        corrected = (
            await client.get("/v1/workspaces/ws_koru_studio/snapshot")
        ).json()
        assert corrected["totals"]["businessExpenseMinor"] == 157974
        assert corrected["totals"]["unresolvedExpenseMinor"] == 0
        assert corrected["totals"]["projectedLowPointMinor"] == 190077
        assert corrected["currentSurface"]["surfaceType"] == "work_receipt"
        assert any(item["undoable"] for item in corrected["activity"])

        undo = await client.post(
            "/v1/events/evt_koru_rule_mitre10/undo",
            json={
                "requestId": "req_koru_undo_mitre10_api",
                "eventId": "evt_koru_rule_mitre10",
                "actor": "owner",
                "reason": "Golden flow exact-state Undo.",
            },
        )
        assert undo.status_code == 200
        after_undo = (
            await client.get("/v1/workspaces/ws_koru_studio/snapshot")
        ).json()
        assert after_undo["totals"]["unresolvedExpenseMinor"] == 18475

        pack = await client.post(
            "/v1/threads/thr_koru_studio_main/turns",
            json={
                "workspaceId": "ws_koru_studio",
                "turnId": "turn_koru_owner_pack_api",
                "content": "Open the owner pack working papers.",
                "mode": "hybrid",
            },
        )
        assert pack.status_code == 200
        pack_snapshot = (
            await client.get("/v1/workspaces/ws_koru_studio/snapshot")
        ).json()
        assert pack_snapshot["currentSurface"]["surfaceType"] == "owner_pack"
        artifact = await client.get(
            "/v1/artifacts/artifact_koru_owner_pack_html"
        )
        assert artifact.status_code == 200
        assert "Preparatory working material" in artifact.text

        telegram = await client.post(
            "/v1/ingest/telegram-fixture",
            json={
                "update": TELEGRAM,
                "attachmentReference": TELEGRAM_ATTACHMENT,
            },
        )
        assert telegram.status_code == 200
        assert telegram.json()["liveSendAttempted"] is False

        cloud_switch = await client.post(
            "/v1/threads/thr_koru_studio_main/turns",
            json={
                "workspaceId": "ws_koru_studio",
                "turnId": "turn_koru_cloud_switch_api",
                "content": "Summarise the current balance and expenses.",
                "mode": "cloud",
            },
        )
        assert cloud_switch.status_code == 200
        capabilities = (await client.get("/v1/models/capabilities")).json()
        assert capabilities["cloudCredentialState"] == "absent"
        assert capabilities["externalCallsMade"] is False
        assert capabilities["selectedMode"] == "cloud"

        second_close = await client.post(
            "/v1/jobs/daily-close",
            json={"workspaceId": "ws_koru_studio"},
        )
        assert second_close.json()["status"] == "completed"
        unchanged_close = await client.post(
            "/v1/jobs/daily-close",
            json={"workspaceId": "ws_koru_studio"},
        )
        assert unchanged_close.json()["status"] == "no_op"

    await services.aclose()
