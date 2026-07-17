from __future__ import annotations

import csv
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import FormatChecker
from jsonschema.validators import validator_for
from referencing import Registry, Resource


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "contracts" / "schemas"


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def fail(message: str) -> None:
    raise AssertionError(message)


def validate_manifest() -> int:
    schemas: dict[str, dict[str, Any]] = {}
    resources: list[tuple[str, Resource[Any]]] = []

    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schema = load_json(path)
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str):
            fail(f"{path.relative_to(ROOT)} is missing a string $id")
        validator_for(schema).check_schema(schema)
        if schema_id in schemas:
            fail(f"duplicate schema $id: {schema_id}")
        schemas[schema_id] = schema
        resources.append((schema_id, Resource.from_contents(schema)))

    registry = Registry().with_resources(resources)
    manifest = load_json(ROOT / "contracts" / "manifest.json")
    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases:
        fail("contracts/manifest.json has no validation cases")

    for case in cases:
        document_path = ROOT / case["document"]
        schema_id = case["schemaId"]
        if schema_id not in schemas:
            fail(f"manifest references unknown schema: {schema_id}")
        document = load_json(document_path)
        schema = schemas[schema_id]
        validator_class = validator_for(schema)
        validator = validator_class(
            schema,
            registry=registry,
            format_checker=FormatChecker(),
        )
        errors = sorted(validator.iter_errors(document), key=lambda error: list(error.path))
        if errors:
            lines = [f"{case['document']} failed {schema_id}:"]
            for error in errors[:20]:
                location = "/".join(str(part) for part in error.absolute_path) or "<root>"
                lines.append(f"  {location}: {error.message}")
            fail("\n".join(lines))

    return len(cases)


def assert_integer_minor_units(value: Any, path: str = "<root>") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}/{key}"
            if (key.endswith("Minor") or key == "amount_minor") and (
                not isinstance(child, int) or isinstance(child, bool)
            ):
                fail(f"{child_path} must be an integer minor-unit value")
            assert_integer_minor_units(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            assert_integer_minor_units(child, f"{path}/{index}")


def validate_demo_math() -> None:
    csv_path = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    expected = load_json(ROOT / "fixtures" / "demo" / "expected-outcomes.json")
    ids = load_json(ROOT / "fixtures" / "demo" / "canonical-ids.json")
    snapshot = load_json(ROOT / "fixtures" / "ui" / "workspace-snapshot.json")
    events = load_json(ROOT / "fixtures" / "ui" / "daily-close-events.json")

    if len(rows) != expected["source"]["csvRowCount"]:
        fail("CSV row count does not match expected-outcomes.json")
    if [row["source_row_id"] for row in rows] != ids["sourceRowIds"]:
        fail("CSV source row IDs do not match canonical-ids.json")
    if any(row["currency"] != "NZD" for row in rows):
        fail("all demo CSV rows must use NZD")

    try:
        amounts = {row["source_row_id"]: int(row["amount_minor"]) for row in rows}
    except ValueError as error:
        fail(f"CSV amount_minor must contain integers only: {error}")

    included_rows = set(expected["source"]["includedBalanceTransactionIds"])
    transaction_to_row = {
        transaction_id: row_id
        for transaction_id, row_id in zip(ids["transactionIds"], ids["sourceRowIds"], strict=True)
    }
    cleared_total = sum(amounts[transaction_to_row[transaction_id]] for transaction_id in included_rows)
    totals = expected["totalsBeforeCorrection"]
    if cleared_total != totals["currentBalanceMinor"]:
        fail(f"cleared CSV total {cleared_total} does not equal expected current balance")

    classified_total = (
        totals["businessIncomeMinor"]
        - totals["businessExpenseMinor"]
        - totals["personalExpenseMinor"]
        - totals["unresolvedExpenseMinor"]
    )
    if classified_total != totals["currentBalanceMinor"]:
        fail("classified income/expense totals do not reconcile to current balance")

    running_balance: int | None = None
    balances: list[int] = []
    for event in expected["forecastEvents"]:
        if running_balance is None:
            running_balance = event["amountMinor"]
        else:
            running_balance += event["amountMinor"]
        if running_balance != event["balanceMinor"]:
            fail(f"forecast does not reconcile at {event['date']}")
        balances.append(running_balance)
    if min(balances) != totals["projectedLowPointMinor"]:
        fail("forecast low point does not match expected totals")
    shortfall = max(0, totals["protectedReserveMinor"] - min(balances))
    if shortfall != totals["reserveShortfallMinor"]:
        fail("reserve shortfall does not match forecast low point")

    correction = expected["correction"]
    if correction["businessExpenseMinorAfter"] != (
        totals["businessExpenseMinor"] + totals["unresolvedExpenseMinor"]
    ):
        fail("post-correction business expense does not include the resolved Mitre 10 amount")

    if snapshot["workspace"]["workspaceId"] != ids["workspaceId"]:
        fail("UI snapshot workspace ID is not canonical")
    if snapshot["totals"] != {"asOf": snapshot["totals"]["asOf"], "currency": "NZD", **totals}:
        fail("UI snapshot totals do not match expected demo totals")

    sequences = [event["sequence"] for event in events]
    if sequences != list(range(1, len(events) + 1)):
        fail("UI run-event fixture contains a sequence gap or duplicate")
    if any(event["runId"] != ids["runId"] for event in events):
        fail("UI run-event fixture uses a non-canonical run ID")

    for path in sorted((ROOT / "contracts" / "examples").glob("*.json")):
        assert_integer_minor_units(load_json(path), str(path.relative_to(ROOT)))
    for path in sorted((ROOT / "fixtures" / "ui").glob("*.json")):
        assert_integer_minor_units(load_json(path), str(path.relative_to(ROOT)))
    assert_integer_minor_units(expected, "fixtures/demo/expected-outcomes.json")

    csv_digest = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    telegram_path = ROOT / "fixtures" / "demo" / "telegram-update.json"
    telegram_digest = hashlib.sha256(telegram_path.read_bytes()).hexdigest()
    source_digests = {source["sourceItemId"]: source["digest"] for source in snapshot["sources"]}
    if source_digests[ids["sourceItemIds"]["bankCsv"]] != csv_digest:
        fail("UI snapshot CSV digest does not match the canonical source bytes")
    if source_digests[ids["sourceItemIds"]["telegram"]] != telegram_digest:
        fail("UI snapshot Telegram digest does not match the canonical source bytes")


def main() -> int:
    try:
        count = validate_manifest()
        validate_demo_math()
    except (AssertionError, KeyError, TypeError, ValueError) as error:
        print(f"contracts:check FAILED\n{error}", file=sys.stderr)
        return 1

    print(f"contracts:check PASS ({count} schema cases; demo arithmetic, IDs, digests and event order verified)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
