from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_method(path: str, class_name: str, name: str, replacement: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    candidate = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    if candidate.end_lineno is None:
        raise RuntimeError(f"{path}: method {class_name}.{name} has no end line")
    lines = content.splitlines(keepends=True)
    start = candidate.lineno - 1
    while start > 0 and lines[start - 1].lstrip().startswith("@"):
        start -= 1
    write(path, "".join(lines[:start]) + replacement.rstrip() + "\n\n" + "".join(lines[candidate.end_lineno:]))


def insert_method_before(path: str, class_name: str, before_name: str, method: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    before = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


MIGRATION = '''    Migration(
        version={version},
        name="deterministic_reconciliation",
        sql="""
        CREATE TABLE transaction_match_receipts (
            match_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            match_type TEXT NOT NULL CHECK (
                match_type IN ('pending_posted', 'internal_transfer')
            ),
            transaction_a_id TEXT NOT NULL REFERENCES transactions(transaction_id),
            transaction_b_id TEXT NOT NULL REFERENCES transactions(transaction_id),
            score_basis_points INTEGER NOT NULL CHECK (
                score_basis_points BETWEEN 0 AND 10000
            ),
            factors_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
            CHECK (transaction_a_id < transaction_b_id),
            UNIQUE (workspace_id, match_type, transaction_a_id, transaction_b_id)
        );

        CREATE INDEX transaction_match_receipts_workspace
            ON transaction_match_receipts(workspace_id, match_type, active, created_at);
        """,
    ),
'''

RECONCILIATION = '''"""Deterministic duplicate, transfer and source-to-ledger reconciliation."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json

PENDING_POSTED_WINDOW_DAYS = 3
TRANSFER_WINDOW_DAYS = 2
MERCHANT_SIMILARITY_THRESHOLD = 8800
_TOKEN_RE = re.compile(r"[A-Z0-9]+")
_NOISE = frozenset({"VISA", "DEBIT", "CREDIT", "CARD", "PURCHASE", "PENDING", "POSTED", "NZ", "LTD", "LIMITED"})
_TRANSFER_MARKERS = frozenset({"TRANSFER", "XFER", "INTERNAL", "ONLINE", "FROM", "TO"})


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def merchant_fingerprint(value: str) -> str:
    tokens = [
        token
        for token in _TOKEN_RE.findall(value.upper())
        if token not in _NOISE and not token.isdigit()
    ]
    return " ".join(tokens)


def similarity_basis_points(left: str, right: str) -> int:
    a = merchant_fingerprint(left)
    b = merchant_fingerprint(right)
    if not a or not b:
        return 0
    if a == b:
        return 10000
    left_tokens = set(a.split())
    right_tokens = set(b.split())
    token_score = int(
        10000 * len(left_tokens & right_tokens) / max(len(left_tokens | right_tokens), 1)
    )
    sequence_score = int(10000 * SequenceMatcher(None, a, b).ratio())
    return max(token_score, sequence_score)


def _days(left: str, right: str) -> int:
    return abs((date.fromisoformat(left) - date.fromisoformat(right)).days)


def _ordered_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    match_type: str
    transaction_a_id: str
    transaction_b_id: str
    score_basis_points: int
    factors: dict[str, Any]


class ReconciliationService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _rows(connection: sqlite3.Connection, workspace_id: str) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in connection.execute(
                """
                SELECT transaction_id, account_id, occurred_on, description,
                       amount_minor, currency, source_status, status,
                       classification, evidence_id
                FROM transactions WHERE workspace_id = ?
                ORDER BY occurred_on, transaction_id
                """,
                (workspace_id,),
            ).fetchall()
        ]

    def pending_posted_candidates(
        self, rows: list[dict[str, Any]]
    ) -> list[MatchCandidate]:
        pending = [row for row in rows if row["source_status"] == "pending" and row["status"] == "pending"]
        posted = [row for row in rows if row["source_status"] == "posted" and row["status"] == "posted"]
        candidates: list[MatchCandidate] = []
        for pending_row in pending:
            for posted_row in posted:
                if pending_row["currency"] != posted_row["currency"]:
                    continue
                if int(pending_row["amount_minor"]) != int(posted_row["amount_minor"]):
                    continue
                day_distance = _days(str(pending_row["occurred_on"]), str(posted_row["occurred_on"]))
                if day_distance > PENDING_POSTED_WINDOW_DAYS:
                    continue
                merchant_score = similarity_basis_points(
                    str(pending_row["description"]), str(posted_row["description"])
                )
                if merchant_score < MERCHANT_SIMILARITY_THRESHOLD:
                    continue
                score = max(0, merchant_score - day_distance * 250)
                a, b = _ordered_pair(
                    str(pending_row["transaction_id"]),
                    str(posted_row["transaction_id"]),
                )
                candidates.append(
                    MatchCandidate(
                        "pending_posted",
                        a,
                        b,
                        score,
                        {
                            "merchantSimilarityBasisPoints": merchant_score,
                            "dateDistanceDays": day_distance,
                            "amountEqual": True,
                            "currencyEqual": True,
                            "pendingTransactionId": str(pending_row["transaction_id"]),
                            "postedTransactionId": str(posted_row["transaction_id"]),
                        },
                    )
                )
        return sorted(
            candidates,
            key=lambda value: (-value.score_basis_points, value.transaction_a_id, value.transaction_b_id),
        )

    def transfer_candidates(self, rows: list[dict[str, Any]]) -> list[MatchCandidate]:
        posted = [row for row in rows if row["status"] == "posted" and row["source_status"] == "posted"]
        candidates: list[MatchCandidate] = []
        for index, left in enumerate(posted):
            for right in posted[index + 1:]:
                if left["account_id"] == right["account_id"]:
                    continue
                if left["currency"] != right["currency"]:
                    continue
                if int(left["amount_minor"]) != -int(right["amount_minor"]):
                    continue
                day_distance = _days(str(left["occurred_on"]), str(right["occurred_on"]))
                if day_distance > TRANSFER_WINDOW_DAYS:
                    continue
                left_tokens = set(merchant_fingerprint(str(left["description"])).split())
                right_tokens = set(merchant_fingerprint(str(right["description"])).split())
                marker = bool((left_tokens | right_tokens) & _TRANSFER_MARKERS)
                shared = left_tokens & right_tokens
                if not marker and not shared:
                    continue
                score = 10000 - day_distance * 500 - (0 if marker else 750)
                a, b = _ordered_pair(str(left["transaction_id"]), str(right["transaction_id"]))
                candidates.append(
                    MatchCandidate(
                        "internal_transfer",
                        a,
                        b,
                        max(score, 0),
                        {
                            "dateDistanceDays": day_distance,
                            "oppositeAmounts": True,
                            "differentAccounts": True,
                            "transferMarker": marker,
                            "sharedDescriptionTokens": sorted(shared),
                        },
                    )
                )
        return sorted(
            candidates,
            key=lambda value: (-value.score_basis_points, value.transaction_a_id, value.transaction_b_id),
        )

    @staticmethod
    def _one_to_one(candidates: list[MatchCandidate]) -> list[MatchCandidate]:
        used: set[str] = set()
        selected: list[MatchCandidate] = []
        for candidate in candidates:
            if candidate.transaction_a_id in used or candidate.transaction_b_id in used:
                continue
            used.add(candidate.transaction_a_id)
            used.add(candidate.transaction_b_id)
            selected.append(candidate)
        return selected

    def apply(
        self,
        connection: sqlite3.Connection,
        *,
        workspace_id: str,
        occurred_at: str,
    ) -> dict[str, object]:
        rows = self._rows(connection, workspace_id)
        pending_matches = self._one_to_one(self.pending_posted_candidates(rows))
        transfer_matches = self._one_to_one(self.transfer_candidates(rows))
        for candidate in [*pending_matches, *transfer_matches]:
            match_id = _stable_id(
                "match",
                workspace_id,
                candidate.match_type,
                candidate.transaction_a_id,
                candidate.transaction_b_id,
            )
            connection.execute(
                """
                INSERT INTO transaction_match_receipts(
                    match_id, workspace_id, match_type, transaction_a_id,
                    transaction_b_id, score_basis_points, factors_json,
                    created_at, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(workspace_id, match_type, transaction_a_id, transaction_b_id)
                DO UPDATE SET
                    score_basis_points = excluded.score_basis_points,
                    factors_json = excluded.factors_json,
                    active = 1
                """,
                (
                    match_id,
                    workspace_id,
                    candidate.match_type,
                    candidate.transaction_a_id,
                    candidate.transaction_b_id,
                    candidate.score_basis_points,
                    canonical_json(candidate.factors),
                    occurred_at,
                ),
            )
            if candidate.match_type == "pending_posted":
                pending_id = str(candidate.factors["pendingTransactionId"])
                posted_id = str(candidate.factors["postedTransactionId"])
                connection.execute(
                    """
                    UPDATE transactions
                    SET status = 'duplicate', duplicate_of_transaction_id = ?,
                        updated_at = ?
                    WHERE transaction_id = ? AND workspace_id = ?
                    """,
                    (posted_id, occurred_at, pending_id, workspace_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE transactions
                    SET classification = 'transfer', category = 'internal_transfer',
                        classification_source = 'deterministic', updated_at = ?
                    WHERE transaction_id IN (?, ?) AND workspace_id = ?
                    """,
                    (
                        occurred_at,
                        candidate.transaction_a_id,
                        candidate.transaction_b_id,
                        workspace_id,
                    ),
                )
        return {
            "pendingDuplicates": {
                str(candidate.factors["pendingTransactionId"]): str(
                    candidate.factors["postedTransactionId"]
                )
                for candidate in pending_matches
            },
            "internalTransferPairs": [
                [candidate.transaction_a_id, candidate.transaction_b_id]
                for candidate in transfer_matches
            ],
            "matchReceiptIds": [
                _stable_id(
                    "match",
                    workspace_id,
                    candidate.match_type,
                    candidate.transaction_a_id,
                    candidate.transaction_b_id,
                )
                for candidate in [*pending_matches, *transfer_matches]
            ],
        }

    def report(self, workspace_id: str) -> dict[str, object]:
        source = self.store.fetch_one(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(amount_minor), 0) AS total
            FROM source_rows sr JOIN source_items si ON si.source_item_id = sr.source_item_id
            WHERE si.workspace_id = ? AND sr.source_status = 'posted'
            """,
            (workspace_id,),
        )
        ledger = self.store.fetch_one(
            """
            SELECT COUNT(*) AS count, COALESCE(SUM(amount_minor), 0) AS total,
                   SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                   SUM(CASE WHEN status = 'duplicate' THEN 1 ELSE 0 END) AS duplicate,
                   SUM(CASE WHEN classification = 'transfer' THEN 1 ELSE 0 END) AS transfers
            FROM transactions WHERE workspace_id = ? AND status != 'ignored'
            """,
            (workspace_id,),
        )
        unmatched = self.store.fetch_one(
            """
            SELECT COUNT(*) AS count
            FROM source_rows sr
            JOIN source_items si ON si.source_item_id = sr.source_item_id
            LEFT JOIN transactions t ON t.source_row_id = sr.source_row_id
            WHERE si.workspace_id = ? AND t.transaction_id IS NULL
            """,
            (workspace_id,),
        )
        matches = self.store.fetch_all(
            """
            SELECT match_id, match_type, transaction_a_id, transaction_b_id,
                   score_basis_points, factors_json, created_at
            FROM transaction_match_receipts
            WHERE workspace_id = ? AND active = 1
            ORDER BY created_at, match_id
            """,
            (workspace_id,),
        )
        source_total = int(source["total"]) if source else 0
        ledger_total = int(ledger["total"]) if ledger else 0
        return {
            "workspaceId": workspace_id,
            "sourcePostedRowCount": int(source["count"]) if source else 0,
            "ledgerRecordCount": int(ledger["count"]) if ledger else 0,
            "unmatchedSourceRowCount": int(unmatched["count"]) if unmatched else 0,
            "pendingTransactionCount": int(ledger["pending"] or 0) if ledger else 0,
            "duplicateTransactionCount": int(ledger["duplicate"] or 0) if ledger else 0,
            "transferTransactionCount": int(ledger["transfers"] or 0) if ledger else 0,
            "sourcePostedTotalMinor": source_total,
            "ledgerTotalMinor": ledger_total,
            "differenceMinor": source_total - ledger_total,
            "matches": [
                {
                    "matchId": str(row["match_id"]),
                    "matchType": str(row["match_type"]),
                    "transactionIds": [
                        str(row["transaction_a_id"]), str(row["transaction_b_id"])
                    ],
                    "scoreBasisPoints": int(row["score_basis_points"]),
                    "factors": json.loads(str(row["factors_json"])),
                    "createdAt": str(row["created_at"]),
                }
                for row in matches
            ],
        }
'''

DEDUPLICATE = '''    def deduplicate(
        self, connection: sqlite3.Connection, *, occurred_at: str
    ) -> dict[str, object]:
        return ReconciliationService(self.store).apply(
            connection,
            workspace_id=WORKSPACE_ID,
            occurred_at=occurred_at,
        )
'''

SERVICE_METHOD = '''    async def reconciliation_report(
        self, *, workspace_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return ReconciliationService(self.store).report(workspace_id)
'''

ROUTE = '''    @router.get("/v1/workspaces/{workspace_id}/reconciliation")
    async def reconciliation_report(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        return dict(await services.reconciliation_report(workspace_id=workspace_id))

'''

TESTS = '''from __future__ import annotations

from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.finance.reconciliation import (
    ReconciliationService,
    merchant_fingerprint,
    similarity_basis_points,
)
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def seeded(tmp_path: Path) -> tuple[SQLiteStore, FinanceEngine]:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    engine = FinanceEngine(store)
    engine.reset_demo(CSV)
    return store, engine


def test_merchant_fingerprint_ignores_card_noise_but_preserves_identity() -> None:
    assert merchant_fingerprint("FIGMA *PENDING VISA CARD") == "FIGMA"
    assert merchant_fingerprint("Figma posted debit purchase") == "FIGMA"
    assert similarity_basis_points("FIGMA *PENDING VISA", "Figma posted") == 10000
    assert similarity_basis_points("FIGMA", "XERO") < 8800


def test_pending_to_posted_match_allows_small_date_shift_and_is_one_to_one(tmp_path: Path) -> None:
    store, engine = seeded(tmp_path)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE transactions SET occurred_on = '2026-07-18' WHERE source_status = 'pending'"
        )
        result = engine.deduplicate(connection, occurred_at="2026-07-19T00:00:00+00:00")
    assert result["pendingDuplicates"] == {
        "txn_koru_010": "txn_koru_009"
    }
    pending = store.fetch_one(
        "SELECT status, duplicate_of_transaction_id FROM transactions WHERE transaction_id = ?",
        ("txn_koru_010",),
    )
    assert str(pending["status"]) == "duplicate"
    assert str(pending["duplicate_of_transaction_id"]) == "txn_koru_009"
    receipts = store.fetch_all(
        "SELECT * FROM transaction_match_receipts WHERE match_type = 'pending_posted'"
    )
    assert len(receipts) == 1


def test_balanced_cross_account_transfer_is_classified_without_affecting_totals(tmp_path: Path) -> None:
    store, engine = seeded(tmp_path)
    with store.transaction() as connection:
        connection.execute(
            "INSERT INTO accounts(account_id, workspace_id, name, currency, created_at) VALUES ('acct_savings', 'ws_koru_studio', 'Savings', 'NZD', '2026-07-17T00:00:00+00:00')"
        )
        source = connection.execute(
            "SELECT * FROM source_items WHERE source_item_id = 'src_koru_bank_csv_20260717'"
        ).fetchone()
        for index, (row_id, account_id, amount, description) in enumerate((
            ("row_transfer_out", "acct_koru_business", -50000, "Online transfer to savings"),
            ("row_transfer_in", "acct_savings", 50000, "Transfer from business"),
        ), start=20):
            connection.execute(
                """
                INSERT INTO source_rows(
                    source_row_id, source_item_id, row_number, account_id,
                    occurred_on, description, amount_minor, currency,
                    source_status, external_reference, mapping_version,
                    row_hash, raw_json
                ) VALUES (?, ?, ?, ?, '2026-07-20', ?, ?, 'NZD', 'posted', ?, ?, ?, '{}')
                """,
                (
                    row_id, source["source_item_id"], index, account_id,
                    description, amount, row_id, source["mapping_version"], "a" * 64,
                ),
            )
            evidence_id = f"evd_{row_id}"
            connection.execute(
                "INSERT INTO evidence_links(evidence_id, workspace_id, source_item_id, source_row_id, label, created_at) VALUES (?, 'ws_koru_studio', ?, ?, ?, '2026-07-20T00:00:00+00:00')",
                (evidence_id, source["source_item_id"], row_id, description),
            )
            connection.execute(
                """
                INSERT INTO transactions(
                    transaction_id, workspace_id, account_id, source_row_id, evidence_id,
                    occurred_on, description, amount_minor, currency, source_status, status,
                    classification, category, classification_source, rule_id,
                    duplicate_of_transaction_id, created_at, updated_at
                ) VALUES (?, 'ws_koru_studio', ?, ?, ?, '2026-07-20', ?, ?, 'NZD', 'posted', 'posted',
                    'unresolved', NULL, 'unclassified', NULL, NULL,
                    '2026-07-20T00:00:00+00:00', '2026-07-20T00:00:00+00:00')
                """,
                (f"txn_{row_id}", account_id, row_id, evidence_id, description, amount),
            )
        result = engine.deduplicate(connection, occurred_at="2026-07-20T01:00:00+00:00")
    assert len(result["internalTransferPairs"]) == 1
    rows = store.fetch_all(
        "SELECT classification, category FROM transactions WHERE transaction_id IN ('txn_row_transfer_out', 'txn_row_transfer_in')"
    )
    assert {(row["classification"], row["category"]) for row in rows} == {
        ("transfer", "internal_transfer")
    }


def test_reconciliation_report_exposes_source_ledger_difference_and_match_factors(tmp_path: Path) -> None:
    store, engine = seeded(tmp_path)
    with store.transaction() as connection:
        engine.deduplicate(connection, occurred_at="2026-07-19T00:00:00+00:00")
    report = ReconciliationService(store).report("ws_koru_studio")
    assert report["unmatchedSourceRowCount"] == 0
    assert report["duplicateTransactionCount"] == 1
    assert report["matches"][0]["scoreBasisPoints"] >= 8800
    assert "merchantSimilarityBasisPoints" in report["matches"][0]["factors"]
'''


def add_migration() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    content = read(path)
    versions = [int(value) for value in re.findall(r"version=(\d+)", content)]
    version = max(versions) + 1
    closing = content.rfind("\n)")
    if closing < 0:
        raise RuntimeError("MIGRATIONS tuple close not found")
    prefix = content[:closing].rstrip()
    if not prefix.endswith(","):
        prefix += ","
    write(path, prefix + "\n" + MIGRATION.format(version=version) + content[closing:])


def add_module_and_engine() -> None:
    write("services/api/src/finance_agent/finance/reconciliation.py", RECONCILIATION)
    path = "services/api/src/finance_agent/finance/service.py"
    content = read(path)
    marker = "from .ingest import CSVImporter, ImportResult, stable_id, transaction_id_for\n"
    import_line = "from .reconciliation import ReconciliationService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("finance service import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    replace_method(path, "FinanceEngine", "deduplicate", DEDUPLICATE)

    path = "services/api/src/finance_agent/finance/classification.py"
    content = read(path)
    marker = '''    description = normalise_merchant(transaction.description)\n\n    if transaction.status in {"duplicate", "ignored"}:\n'''
    replacement = '''    description = normalise_merchant(transaction.description)\n\n    if transaction.classification == "transfer":\n        return ClassificationDecision("transfer", "internal_transfer", "deterministic")\n    if transaction.status in {"duplicate", "ignored"}:\n'''
    if marker not in content:
        raise RuntimeError("classification transfer marker missing")
    content = content.replace(marker, replacement, 1)
    write(path, content)


def update_api() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.provider_events import record_provider_sync\n"
    import_line = "from finance_agent.finance.reconciliation import ReconciliationService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("provider events import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "claim_local_notifications", SERVICE_METHOD)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def claim_local_notifications(\n"
    addition = '''    async def reconciliation_report(\n        self, *, workspace_id: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("notification protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    marker = '    @router.post("/v1/workspaces/{workspace_id}/notifications/claim")\n'
    if marker not in content:
        raise RuntimeError("notification route marker missing")
    content = content.replace(marker, ROUTE + marker, 1)
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/finance/test_reconciliation_matching.py", TESTS)
    write("docs/RECONCILIATION.md", '''# Reconciliation and match evidence\n\nFolio reconciles immutable source rows to local transaction records and records every pending-to-posted or internal-transfer match. Pending matches require equal signed minor units, equal currency, a date distance of no more than three days, and a merchant similarity score of at least 0.88. Selection is greedy and one-to-one so one posted record cannot absorb multiple pending records.\n\nInternal transfers require opposite amounts, equal currency, distinct accounts, a date distance of no more than two days, and either a transfer marker or shared description token. Both sides remain in the source and ledger but are classified as `transfer`, so their net finance effect cancels without pretending either side is income or expense.\n\nThe reconciliation report exposes source and ledger counts, unmatched source rows, pending and duplicate records, transfer records, total differences and the exact factors behind every match. This is deterministic bookkeeping preparation, not bank reconciliation certification.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 16: deterministic reconciliation and match evidence\n\n- Pending-to-posted matching tolerates bounded date shifts and merchant noise while requiring exact amount and currency.\n- Match selection is one-to-one and stores score plus factors.\n- Balanced cross-account transfers are classified separately from income and expense.\n- Source-to-ledger counts, unmatched rows, duplicates, transfers and total differences are exposed.\n- Transfer classifications survive later deterministic classification passes.\n- No fuzzy match is described as certified bank reconciliation.\n'''
    if "## Stack 16: deterministic reconciliation and match evidence" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration()
    add_module_and_engine()
    update_api()
    add_tests_and_docs()
    print("reconciliation matching changes applied")


if __name__ == "__main__":
    main()
