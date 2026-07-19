"""Small stdlib client for the loopback-only prototype demo controls."""

from __future__ import annotations

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_BASE = "http://127.0.0.1:8787"


def post(path: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        f"{API_BASE}{path}",
        data=json.dumps(payload).encode(),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed loopback URL
        return json.loads(response.read())


def reset() -> dict[str, object]:
    result = post("/v1/demo/reset", {"workspaceId": "ws_koru_studio"})
    close = post(
        "/v1/jobs/daily-close",
        {
            "workspaceId": "ws_koru_studio",
            "idempotencyKey": "cli-demo-reset-close",
        },
    )
    return {"reset": result, "dailyClose": close}


def daily_close() -> dict[str, object]:
    return post(
        "/v1/jobs/daily-close",
        {
            "workspaceId": "ws_koru_studio",
            "idempotencyKey": "cli-daily-close",
        },
    )


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "reset"
    try:
        result = reset() if command == "reset" else daily_close()
    except (HTTPError, URLError, TimeoutError) as error:
        print(f"Loopback API unavailable: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
