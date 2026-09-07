from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


CSV_TESTS = '''from __future__ import annotations

import csv
import random
from pathlib import Path

import pytest

from finance_agent.finance import FinanceEngine
from finance_agent.finance.ingest import CSVIngestError
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
SEED = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def counts(store: SQLiteStore) -> tuple[int, int, int]:
    return (
        len(store.fetch_all("SELECT * FROM source_items")),
        len(store.fetch_all("SELECT * FROM source_rows")),
        len(store.fetch_all("SELECT * FROM transactions")),
    )


def write_csv(path: Path, headers: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def test_seeded_random_valid_statements_commit_exactly_once(tmp_path: Path) -> None:
    randomiser = random.Random(20260826)
    for case_index in range(40):
        database = tmp_path / f"valid-{case_index}.sqlite3"
        store = SQLiteStore(database)
        engine = FinanceEngine(store)
        engine.reset_demo(SEED)
        path = tmp_path / f"valid-{case_index}.csv"
        rows: list[list[str]] = []
        expected = 0
        for row_index in range(1, randomiser.randint(1, 40) + 1):
            cents = randomiser.randint(-2_000_000, 2_000_000)
            if cents == 0:
                cents = 1
            expected += cents
            rows.append(
                [
                    f"2026-08-{(row_index % 28) + 1:02d}",
                    f"Random merchant {case_index}-{row_index}",
                    f"{cents / 100:.2f}",
                    f"ref-{case_index}-{row_index}",
                    "NZD",
                    "posted",
                ]
            )
        write_csv(
            path,
            ["Date", "Description", "Amount", "Reference", "Currency", "Status"],
            rows,
        )
        before = counts(store)
        result = engine.ingest_csv(path, mapping_version="adversarial_valid@1")
        after = counts(store)
        assert result.row_count == len(rows)
        assert after[0] == before[0] + 1
        assert after[1] == before[1] + len(rows)
        assert after[2] == before[2] + len(rows)
        imported = store.fetch_one(
            """
            SELECT COALESCE(SUM(amount_minor), 0) AS total
            FROM source_rows WHERE source_item_id = ?
            """,
            (result.source_item_id,),
        )
        assert int(imported["total"]) == expected
        duplicate = engine.ingest_csv(path, mapping_version="adversarial_valid@1")
        assert duplicate.duplicate_import is True
        assert counts(store) == after


@pytest.mark.parametrize(
    ("headers", "rows"),
    [
        (["Date", "Description", "Amount"], [["2026-08-01", "A", "1.00"]]),
        (["Date", "Description", "Amount", "Debit", "Credit"], [["2026-08-01", "A", "1.00", "", ""]]),
        (["Date", "Description", "Debit", "Credit"], [["2026-08-01", "A", "1.00", "2.00"]]),
        (["Date", "Description", "Amount", "Currency"], [["2026-08-01", "A", "NaN", "NZD"]]),
        (["Date", "Description", "Amount", "Currency"], [["2026-08-01", "A", "1.001", "NZD"]]),
        (["Date", "Description", "Amount", "Currency"], [["08/01/2026", "A", "1.00", "NZD"]]),
        (["Date", "Description", "Amount", "Currency"], [["2026-08-01", "A", "1.00", "USD"]]),
        (["Date", "Description", "Amount", "Status"], [["2026-08-01", "A", "1.00", "settled"]]),
        (["Date", "Description", "Amount", "Amount"], [["2026-08-01", "A", "1.00", "1.00"]]),
        (["Date", "Description", "Debit", "Credit"], [["2026-08-01", "A", "", ""]]),
    ],
)
def test_malformed_statements_fail_without_partial_rows(
    tmp_path: Path,
    headers: list[str],
    rows: list[list[str]],
) -> None:
    store = SQLiteStore(tmp_path / "invalid.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(SEED)
    before = counts(store)
    path = tmp_path / "invalid.csv"
    write_csv(path, headers, rows)
    with pytest.raises(CSVIngestError):
        engine.ingest_csv(path, mapping_version="adversarial_invalid@1")
    assert counts(store) == before
'''

MONEY_TESTS = '''from __future__ import annotations

import random

import pytest

from finance_agent.finance.classification import calculate_classified_totals
from finance_agent.finance.domain import Money, Transaction
from finance_agent.finance.forecast import project_cash
from finance_agent.finance.domain import ForecastEvent


def transaction(index: int, amount: int, classification: str) -> Transaction:
    return Transaction(
        transaction_id=f"txn_property_{index}",
        occurred_on="2026-08-01",
        description=f"Property transaction {index}",
        amount_minor=amount,
        currency="NZD",
        source_status="posted",
        status="posted",
        classification=classification,
        category="property",
        classification_source="deterministic",
        rule_id=None,
        evidence_id=f"evd_property_{index}",
    )


def test_money_add_subtract_inverse_for_seeded_integer_domain() -> None:
    randomiser = random.Random(998877)
    for _ in range(5000):
        left = randomiser.randint(-(2**53), 2**53)
        right = randomiser.randint(-(2**53), 2**53)
        assert (Money(left) + Money(right) - Money(right)).amount_minor == left
        assert (Money(left) - Money(right) + Money(right)).amount_minor == left


def test_money_rejects_boolean_float_and_foreign_currency() -> None:
    with pytest.raises(TypeError):
        Money(True)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        Money(1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Money(100, "USD")


def test_classified_totals_reconcile_for_seeded_random_transactions() -> None:
    randomiser = random.Random(110022)
    for case in range(100):
        rows: list[Transaction] = []
        for index in range(randomiser.randint(1, 100)):
            amount = randomiser.randint(-1_000_000, 1_000_000)
            classification = (
                "business" if amount >= 0 else randomiser.choice(["business", "personal", "unresolved"])
            )
            rows.append(transaction(case * 1000 + index, amount, classification))
        totals = calculate_classified_totals(
            rows,
            protected_reserve_minor=200_000,
            projected_low_point_minor=-50_000,
        )
        expected_balance = sum(row.amount_minor for row in rows)
        assert totals.current_balance_minor == expected_balance
        classified_balance = (
            totals.business_income_minor
            - totals.business_expense_minor
            - totals.personal_expense_minor
            - totals.unresolved_expense_minor
        )
        assert classified_balance == expected_balance
        assert totals.reserve_shortfall_minor == 250_000


def test_forecast_point_balances_are_exact_prefix_sums() -> None:
    randomiser = random.Random(440066)
    for case in range(100):
        opening = randomiser.randint(-500_000, 2_000_000)
        events = [
            ForecastEvent(
                f"2026-08-{index + 1:02d}",
                f"Event {case}-{index}",
                randomiser.randint(-250_000, 250_000),
            )
            for index in range(randomiser.randint(1, 20))
        ]
        forecast = project_cash(
            current_balance_minor=opening,
            protected_reserve_minor=200_000,
            start_date="2026-08-01",
            events=events,
            assumptions=("Generated property case",),
        )
        running = opening
        assert forecast.points[0].balance_minor == running
        for point, event in zip(forecast.points[1:], sorted(events, key=lambda value: (value.date, value.label)), strict=True):
            running += event.amount_minor
            assert point.balance_minor == running
        assert forecast.low_point_minor == min(point.balance_minor for point in forecast.points)
'''

CONTRACT_TESTS = '''from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema.validators import validator_for
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[4]
SCHEMAS = ROOT / "contracts" / "schemas"


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def registry_and_schemas():
    schemas = {}
    resources = []
    for path in SCHEMAS.glob("*.schema.json"):
        schema = load(path)
        schemas[schema["$id"]] = schema
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources), schemas


def validation_errors(schema, document, registry):
    return list(validator_for(schema)(schema, registry=registry).iter_errors(document))


def mutations(document):
    if isinstance(document, dict):
        if document:
            first = next(iter(document))
            removed = copy.deepcopy(document)
            removed.pop(first)
            yield "removed-field", removed
        extra = copy.deepcopy(document)
        extra["__unexpected_contract_field__"] = True
        yield "unexpected-field", extra
        for key, value in list(document.items())[:3]:
            if isinstance(value, int) and not isinstance(value, bool):
                wrong = copy.deepcopy(document)
                wrong[key] = value + 0.5
                yield f"float-{key}", wrong
            elif isinstance(value, str):
                wrong = copy.deepcopy(document)
                wrong[key] = None
                yield f"null-{key}", wrong


def test_every_manifest_example_accepts_original_and_rejects_a_mutation() -> None:
    registry, schemas = registry_and_schemas()
    manifest = load(ROOT / "contracts" / "manifest.json")
    for case in manifest["cases"]:
        document = load(ROOT / case["document"])
        schema = schemas[case["schemaId"]]
        assert validation_errors(schema, document, registry) == []
        rejected = 0
        for _name, mutation in mutations(document):
            if validation_errors(schema, mutation, registry):
                rejected += 1
        assert rejected >= 1, f"{case['document']} accepted every adversarial mutation"


def test_all_minor_unit_fields_reject_floats_recursively() -> None:
    registry, schemas = registry_and_schemas()
    manifest = load(ROOT / "contracts" / "manifest.json")

    def paths(value, prefix=()):
        if isinstance(value, dict):
            for key, child in value.items():
                if key.endswith("Minor") or key == "amount_minor":
                    yield (*prefix, key)
                yield from paths(child, (*prefix, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                yield from paths(child, (*prefix, index))

    def set_path(value, path, replacement):
        target = value
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = replacement

    checked = 0
    for case in manifest["cases"]:
        original = load(ROOT / case["document"])
        schema = schemas[case["schemaId"]]
        for path in paths(original):
            mutated = copy.deepcopy(original)
            set_path(mutated, path, 1.5)
            assert validation_errors(schema, mutated, registry), (
                f"{case['document']} accepted float at {path}"
            )
            checked += 1
    assert checked > 0
'''

MIGRATION_TESTS = '''from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from finance_agent.storage import SQLiteStore
from finance_agent.storage.migrations import MIGRATIONS


def schema(store: SQLiteStore):
    rows = store.fetch_all(
        """
        SELECT type, name, tbl_name, COALESCE(sql, '') AS sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    )
    return [tuple(row) for row in rows]


def apply_prefix(path: Path, count: int) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        for migration in MIGRATIONS[:count]:
            connection.executescript(migration.sql)
            connection.execute(
                "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
        connection.commit()
    finally:
        connection.close()


@pytest.mark.parametrize("prefix", [0, 1, 2, 4, 7, 10, 15, 20])
def test_partial_supported_schema_converges_to_clean_latest(tmp_path: Path, prefix: int) -> None:
    prefix = min(prefix, max(0, len(MIGRATIONS) - 1))
    clean = SQLiteStore(tmp_path / f"clean-{prefix}.sqlite3")
    clean.migrate()
    partial_path = tmp_path / f"partial-{prefix}.sqlite3"
    apply_prefix(partial_path, prefix)
    partial = SQLiteStore(partial_path)
    partial.migrate()
    partial.migrate()
    assert schema(partial) == schema(clean)
    with partial.connect() as connection:
        assert [row[0] for row in connection.execute("PRAGMA integrity_check")] == ["ok"]
        assert list(connection.execute("PRAGMA foreign_key_check")) == []
    applied = partial.fetch_all("SELECT version FROM schema_migrations ORDER BY version")
    assert [int(row["version"]) for row in applied] == [value.version for value in MIGRATIONS]


def test_duplicate_or_out_of_order_migration_versions_are_rejected_by_contract() -> None:
    versions = [value.version for value in MIGRATIONS]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions))
    assert all(current > previous for previous, current in zip(versions, versions[1:]))
'''


def add_tests() -> None:
    write("services/api/tests/adversarial/test_csv_ingest_properties.py", CSV_TESTS)
    write("services/api/tests/adversarial/test_money_properties.py", MONEY_TESTS)
    write("services/api/tests/adversarial/test_contract_mutations.py", CONTRACT_TESTS)
    write("services/api/tests/adversarial/test_migration_replay.py", MIGRATION_TESTS)


def update_scripts_docs() -> None:
    path = "package.json"
    value = json.loads(read(path))
    scripts = value["scripts"]
    scripts["test:adversarial"] = "uv run --project services/api pytest -q services/api/tests/adversarial"
    if "test:adversarial" not in scripts["verify"]:
        scripts["verify"] += " && pnpm test:adversarial"
    write(path, json.dumps(value, indent=2) + "\n")
    write("docs/ADVERSARIAL_VERIFICATION.md", '''# Adversarial verification\n\nFolio's permanent verification gate includes deterministic seeded generators rather than depending only on the happy-path Koru fixture. Valid practical CSV statements must commit exact integer totals once and deduplicate on replay. Malformed headers, ambiguous amount columns, fractional cents, foreign currencies, invalid dates and statuses must fail without adding a source, row or transaction.\n\nMoney arithmetic is exercised across thousands of signed integer combinations. Classified totals must reconcile exactly, and every forecast point must equal the opening balance plus its event prefix sum. Contract examples are mutated by removing fields, adding fields, changing scalar types and replacing minor-unit integers with floats.\n\nMigration replay starts from multiple supported partial prefixes, applies the current migration runner twice and compares the result to a clean latest schema. Every result must pass SQLite integrity and foreign-key checks. This reduces regression risk but does not prove arbitrary future or hand-edited databases can migrate safely.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 30: adversarial finance and migration invariants\n\n- Seeded valid CSVs must commit exact totals once and deduplicate on replay.\n- Malformed or ambiguous statements must fail without partial rows.\n- Thousands of signed-money operations and forecast prefix sums are checked.\n- Contract examples must reject structural and minor-unit mutations.\n- Multiple partial schema prefixes must converge to the clean latest schema.\n- Every replayed database must pass integrity and foreign-key checks.\n'''
    if "## Stack 30: adversarial finance and migration invariants" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_tests()
    update_scripts_docs()
    print("adversarial invariant changes applied")


if __name__ == "__main__":
    main()
