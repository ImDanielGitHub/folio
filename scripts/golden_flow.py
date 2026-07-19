"""Exercise the full synthetic Koru flow against the running loopback API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

API_BASE = "http://127.0.0.1:8787"
ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ID = "ws_koru_studio"
THREAD_ID = "thr_koru_studio_main"


def request(path: str, payload: dict[str, object] | None = None) -> tuple[bytes, str]:
    headers = {"Accept": "application/json"}
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        method = "POST"
    value = Request(f"{API_BASE}{path}", data=data, headers=headers, method=method)
    with urlopen(value, timeout=30) as response:  # noqa: S310 - fixed loopback URL
        return response.read(), response.headers.get_content_type()


def json_request(path: str, payload: dict[str, object] | None = None) -> dict[str, Any]:
    content, _ = request(path, payload)
    return json.loads(content)


def run_events(run_id: str) -> list[dict[str, Any]]:
    content, media_type = request(f"/v1/jobs/{run_id}/events")
    assert media_type == "text/event-stream"
    events: list[dict[str, Any]] = []
    for frame in content.decode().split("\n\n"):
        data = "\n".join(
            line.removeprefix("data: ")
            for line in frame.splitlines()
            if line.startswith("data: ")
        )
        if data:
            events.append(json.loads(data))
    assert events and events[-1]["type"] == "run.completed"
    return events


def turn(turn_id: str, content: str, mode: str = "local") -> dict[str, Any]:
    accepted = json_request(
        f"/v1/threads/{THREAD_ID}/turns",
        {
            "workspaceId": WORKSPACE_ID,
            "turnId": turn_id,
            "content": content,
            "mode": mode,
        },
    )
    run_events(str(accepted["runId"]))
    return json_request(f"/v1/workspaces/{WORKSPACE_ID}/snapshot")


def main() -> int:
    health = json_request("/health")
    assert health["status"] == "ready" and health["loopback"] is True

    reset = json_request("/v1/demo/reset", {"workspaceId": WORKSPACE_ID})
    assert reset["rowCount"] == 10 and reset["nextAction"] == "run_daily_close"
    akahu = json_request("/v1/ingest/akahu-fixture", {})
    assert akahu["liveSyncAttempted"] is False
    assert akahu["rowCount"] == 6
    assert akahu["status"] in {"ingested", "deduplicated"}
    close = json_request(
        "/v1/jobs/daily-close",
        {"workspaceId": WORKSPACE_ID, "idempotencyKey": "golden-http-close"},
    )
    close_events = run_events(str(close["runId"]))
    snapshot = json_request(f"/v1/workspaces/{WORKSPACE_ID}/snapshot")
    assert snapshot["totals"]["businessExpenseMinor"] == 139499
    assert snapshot["totals"]["unresolvedExpenseMinor"] == 18475
    assert len(snapshot["findings"]) == 3

    attention = turn("turn_golden_attention", "Explain what needs my attention today.")
    assert attention["currentSurface"]["surfaceType"] == "living_brief"

    corrected = turn(
        "turn_golden_mitre",
        (
            "The MITRE 10 purchase was materials for a client fit-out. "
            "Treat similar MITRE 10 purchases under $500 the same way."
        ),
    )
    assert corrected["totals"]["businessExpenseMinor"] == 157974
    assert corrected["totals"]["unresolvedExpenseMinor"] == 0
    assert corrected["currentSurface"]["surfaceType"] == "work_receipt"
    undo_action = next(
        action
        for action in corrected["currentSurface"]["actions"]
        if action["type"] == "undo_event"
    )

    scenario = turn("turn_golden_scenario", "Show the cash scenario if I defer the laptop.")
    assert scenario["currentSurface"]["surfaceType"] == "cash_scenario"
    assert scenario["totals"]["projectedLowPointMinor"] == 190077

    undone = json_request(
        f"/v1/events/{undo_action['eventId']}/undo",
        {
            "requestId": "req_golden_undo_mitre",
            "eventId": undo_action["eventId"],
            "actor": "owner",
            "reason": "Golden flow verifies the visible inverse-event path.",
        },
    )
    undone_snapshot = json_request(f"/v1/workspaces/{WORKSPACE_ID}/snapshot")
    assert undone_snapshot["totals"]["unresolvedExpenseMinor"] == 18475

    owner_pack = turn("turn_golden_owner_pack", "Prepare my owner pack.")
    assert owner_pack["currentSurface"]["surfaceType"] == "owner_pack"
    artifact = owner_pack["artifacts"][0]
    artifact_content, artifact_type = request(f"/v1/artifacts/{artifact['artifactId']}")
    assert artifact_content and artifact_type in {"text/html", "application/pdf"}

    telegram_update = json.loads((ROOT / "fixtures/demo/telegram-update.json").read_text())
    telegram_attachment = json.loads(
        (ROOT / "fixtures/demo/telegram-attachment-reference.json").read_text()
    )
    telegram = json_request(
        "/v1/ingest/telegram-fixture",
        {"update": telegram_update, "attachmentReference": telegram_attachment},
    )
    assert telegram["status"] in {"ingested", "deduplicated"}
    assert telegram["liveSendAttempted"] is False

    switched = turn(
        "turn_golden_cloud_mode",
        "Summarise the latest owner pack without changing any finance records.",
        mode="cloud",
    )
    capabilities = json_request("/v1/models/capabilities")
    assert switched["modelMode"] == "cloud"
    assert capabilities["cloudCredentialState"] == "absent"
    assert capabilities["externalCallsMade"] is False

    print(
        json.dumps(
            {
                "status": "PASS",
                "resetRows": reset["rowCount"],
                "closeEventCount": len(close_events),
                "baselineBusinessExpenseMinor": snapshot["totals"]["businessExpenseMinor"],
                "correctedBusinessExpenseMinor": corrected["totals"]["businessExpenseMinor"],
                "scenarioLowPointMinor": scenario["totals"]["projectedLowPointMinor"],
                "undoEventId": undone["undoEvent"]["eventId"],
                "artifactId": artifact["artifactId"],
                "telegramStatus": telegram["status"],
                "selectedMode": switched["modelMode"],
                "externalCallsMade": capabilities["externalCallsMade"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
