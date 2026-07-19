#!/usr/bin/env python3
"""Prove demo reset is byte-stable across five offline runs.

Normalises volatile timestamps out of the snapshot before hashing so the
proof measures finance state, not wall-clock labels.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "services" / "api" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from finance_agent.finance.service import FinanceEngine  # noqa: E402
from finance_agent.jobs import DailyCloseService, DailyCloseWorker  # noqa: E402
from finance_agent.storage import SQLiteStore  # noqa: E402

CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"
VOLATILE_KEYS = {
    "receivedAt",
    "createdAt",
    "updatedAt",
    "occurredAt",
    "syncedAt",
    "generatedAt",
    "closedAt",
    "asOf",
}


def strip_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: strip_volatile(item)
            for key, item in value.items()
            if key not in VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


def run_once(index: int) -> str:
    with tempfile.TemporaryDirectory(prefix=f"folio-reset-{index}-") as tmp:
        store = SQLiteStore(Path(tmp) / "demo.sqlite3")
        engine = FinanceEngine(store)
        imported = engine.reset_demo(CSV)
        engine.ingest_akahu_fixture()
        DailyCloseWorker(DailyCloseService(engine)).tick()
        snapshot = engine.get_snapshot()
        payload = {
            "rowCount": imported.row_count,
            "totals": snapshot.get("totals"),
            "findings": snapshot.get("findings"),
            "sources": [
                {
                    "sourceItemId": item.get("sourceItemId"),
                    "sourceType": item.get("sourceType"),
                    "digest": item.get("digest"),
                    "rowCount": item.get("rowCount"),
                    "status": item.get("status"),
                }
                for item in snapshot.get("sources", [])
            ],
        }
        normalised = strip_volatile(payload)
        encoded = json.dumps(normalised, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    hashes = [run_once(index) for index in range(1, 6)]
    unique = sorted(set(hashes))
    report = {
        "status": "PASS" if len(unique) == 1 else "FAIL",
        "runs": 5,
        "hashes": hashes,
        "uniqueCount": len(unique),
        "canonicalHash": unique[0] if len(unique) == 1 else None,
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
