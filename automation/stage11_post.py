from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    path = ROOT / "services/api/src/finance_agent/models/evaluation.py"
    value = path.read_text(encoding="utf-8")
    old = '            "cases": [case.__dict__ for case in cases],\n'
    new = '''            "cases": [\n                {\n                    "caseId": case.case_id,\n                    "requiredTier": case.required_tier,\n                    "rawStatus": case.raw_status,\n                    "effectiveStatus": case.effective_status,\n                    "repairAttempts": case.repair_attempts,\n                    "latencyMs": case.latency_ms,\n                    "outputHash": case.output_hash,\n                    "failureCode": case.failure_code,\n                }\n                for case in cases\n            ],\n'''
    count = value.count(old)
    if count != 1:
        raise RuntimeError(f"evaluation receipt projection: expected one match, found {count}")
    path.write_text(value.replace(old, new, 1), encoding="utf-8")


if __name__ == "__main__":
    main()
