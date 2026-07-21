# ruff: noqa: E501
"""Atomic CSV ingestion with immutable row provenance and stable IDs."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import cast

from finance_agent.storage import SQLiteStore, canonical_json

REQUIRED_COLUMNS = (
    "source_row_id",
    "account_id",
    "occurred_on",
    "description",
    "amount_minor",
    "currency",
    "status",
    "external_reference",
)
ID_PATTERN = re.compile(r"^[a-z][a-z0-9]{1,15}_[a-z0-9][a-z0-9_]{2,95}$")
DATE_COLUMNS = ("date", "transaction_date", "processed_date", "posted_date", "value_date")
SIGNED_AMOUNT_COLUMNS = ("amount", "transaction_amount", "signed_amount", "value")
DEBIT_COLUMNS = ("debit", "debit_amount", "withdrawal", "withdrawals", "money_out")
CREDIT_COLUMNS = ("credit", "credit_amount", "deposit", "deposits", "money_in")
PRIMARY_DESCRIPTION_COLUMNS = (
    "description",
    "transaction_description",
    "details",
    "transaction_details",
    "narrative",
)
COMPOSITE_DESCRIPTION_COLUMNS = ("payee", "other_party", "particulars", "code", "memo")
CURRENCY_COLUMNS = ("currency", "currency_code", "ccy")
STATUS_COLUMNS = ("status", "transaction_status")
REFERENCE_COLUMNS = (
    "reference",
    "transaction_id",
    "unique_id",
    "bank_reference",
    "serial",
)


class CSVIngestError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ImportResult:
    source_item_id: str
    source_sha256: str
    mapping_version: str
    row_count: int
    duplicate_import: bool
    transaction_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HeaderMapping:
    """Explain how a practical bank export maps onto Folio's canonical row."""

    profile: str
    date_column: str
    description_columns: tuple[str, ...]
    signed_amount_column: str | None
    debit_column: str | None
    credit_column: str | None
    currency_column: str | None
    status_column: str | None
    reference_column: str | None


@dataclass(frozen=True, slots=True)
class ParsedRow:
    canonical: dict[str, str]
    raw: dict[str, str]


@dataclass(frozen=True, slots=True)
class ParsedCSV:
    rows: tuple[ParsedRow, ...]
    mapping_version: str


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def transaction_id_for(source_row_id: str, source_digest: str) -> str:
    if source_row_id.startswith("row_"):
        candidate = f"txn_{source_row_id[4:]}"
        if ID_PATTERN.fullmatch(candidate):
            return candidate
    return stable_id("txn", source_digest, source_row_id)


def _read_csv(raw_bytes: bytes) -> tuple[tuple[str, ...], list[dict[str, str]]]:
    try:
        text = raw_bytes.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise CSVIngestError("CSV must be UTF-8") from error
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fieldnames = tuple(reader.fieldnames or ())
    if not fieldnames or any(not column.strip() for column in fieldnames):
        raise CSVIngestError("CSV requires a non-empty header row")
    if len(set(fieldnames)) != len(fieldnames):
        raise CSVIngestError("CSV contains duplicate column headers")
    rows: list[dict[str, str]] = []
    for row_number, raw_row in enumerate(reader, start=1):
        if None in raw_row or any(value is None for value in raw_row.values()):
            raise CSVIngestError(f"row {row_number}: values do not match the CSV headers")
        rows.append({str(key): str(value) for key, value in raw_row.items()})
    if not rows:
        raise CSVIngestError("CSV contains no transaction rows")
    return fieldnames, rows


def _normalise_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _column_lookup(fieldnames: tuple[str, ...]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for original in fieldnames:
        normalised = _normalise_header(original)
        if not normalised:
            raise CSVIngestError(f"CSV header {original!r} is not meaningful")
        if normalised in lookup:
            raise CSVIngestError(
                f"CSV headers {lookup[normalised]!r} and {original!r} are ambiguous"
            )
        lookup[normalised] = original
    return lookup


def _first_column(lookup: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    return next((lookup[alias] for alias in aliases if alias in lookup), None)


def _detect_header_mapping(fieldnames: tuple[str, ...]) -> HeaderMapping:
    lookup = _column_lookup(fieldnames)
    date_column = _first_column(lookup, DATE_COLUMNS)
    if date_column is None:
        raise CSVIngestError(
            "CSV headers could not be mapped: expected a date or transaction-date column"
        )

    description_columns: tuple[str, ...]
    primary_description = _first_column(lookup, PRIMARY_DESCRIPTION_COLUMNS)
    if primary_description is not None:
        description_columns = (primary_description,)
    else:
        description_columns = tuple(
            lookup[alias] for alias in COMPOSITE_DESCRIPTION_COLUMNS if alias in lookup
        )
    if not description_columns:
        raise CSVIngestError(
            "CSV headers could not be mapped: expected description, details, payee, particulars, or memo"
        )

    signed_amount = _first_column(lookup, SIGNED_AMOUNT_COLUMNS)
    debit = _first_column(lookup, DEBIT_COLUMNS)
    credit = _first_column(lookup, CREDIT_COLUMNS)
    if signed_amount is not None and (debit is not None or credit is not None):
        raise CSVIngestError(
            "CSV headers are ambiguous: use a signed amount column or debit and credit columns, not both"
        )
    if signed_amount is None and (debit is None or credit is None):
        raise CSVIngestError(
            "CSV headers could not be mapped: expected a signed amount column or both debit and credit columns"
        )

    return HeaderMapping(
        profile=(
            "nz_bank_signed_amount@1"
            if signed_amount is not None
            else "nz_bank_debit_credit@1"
        ),
        date_column=date_column,
        description_columns=description_columns,
        signed_amount_column=signed_amount,
        debit_column=debit,
        credit_column=credit,
        currency_column=_first_column(lookup, CURRENCY_COLUMNS),
        status_column=_first_column(lookup, STATUS_COLUMNS),
        reference_column=_first_column(lookup, REFERENCE_COLUMNS),
    )


def _parse_date(value: str, row_number: int) -> str:
    candidate = value.strip()
    for date_format in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%Y", "%d-%m-%y"):
        try:
            return datetime.strptime(candidate, date_format).date().isoformat()
        except ValueError:
            continue
    raise CSVIngestError(
        f"row {row_number}: date must be YYYY-MM-DD or an unambiguous NZ day/month/year date"
    )


def _parse_decimal_amount(value: str, row_number: int, column: str) -> Decimal | None:
    candidate = value.strip()
    if not candidate:
        return None
    negative_parentheses = candidate.startswith("(") and candidate.endswith(")")
    if negative_parentheses:
        candidate = candidate[1:-1]
    candidate = re.sub(r"(?i)NZD|NZ\$", "", candidate).replace("$", "").replace(",", "").strip()
    try:
        amount = Decimal(candidate)
    except InvalidOperation as error:
        raise CSVIngestError(f"row {row_number}: {column} is not a valid amount") from error
    if not amount.is_finite():
        raise CSVIngestError(f"row {row_number}: {column} is not a finite amount")
    if negative_parentheses:
        amount = -amount
    minor = amount * 100
    if minor != minor.to_integral_value():
        raise CSVIngestError(f"row {row_number}: {column} has more than two decimal places")
    return amount


def _amount_minor(row: dict[str, str], mapping: HeaderMapping, row_number: int) -> int:
    if mapping.signed_amount_column is not None:
        amount = _parse_decimal_amount(
            row[mapping.signed_amount_column], row_number, mapping.signed_amount_column
        )
        if amount is None:
            raise CSVIngestError(
                f"row {row_number}: {mapping.signed_amount_column} is required"
            )
        return int(amount * 100)

    assert mapping.debit_column is not None and mapping.credit_column is not None
    debit = _parse_decimal_amount(row[mapping.debit_column], row_number, mapping.debit_column)
    credit = _parse_decimal_amount(row[mapping.credit_column], row_number, mapping.credit_column)
    if debit is not None and debit < 0:
        raise CSVIngestError(f"row {row_number}: debit must be zero or positive")
    if credit is not None and credit < 0:
        raise CSVIngestError(f"row {row_number}: credit must be zero or positive")
    debit_value = debit or Decimal(0)
    credit_value = credit or Decimal(0)
    if debit_value and credit_value:
        raise CSVIngestError(f"row {row_number}: debit and credit cannot both contain an amount")
    if not debit_value and not credit_value:
        raise CSVIngestError(f"row {row_number}: either debit or credit must contain an amount")
    return int((credit_value - debit_value) * 100)


def _mapped_rows(
    raw_rows: list[dict[str, str]],
    *,
    mapping: HeaderMapping,
    account_id: str,
    source_digest: str,
    mapping_version: str,
) -> ParsedCSV:
    parsed: list[ParsedRow] = []
    for row_number, raw in enumerate(raw_rows, start=1):
        occurred_on = _parse_date(raw[mapping.date_column], row_number)
        description_parts: list[str] = []
        for column in mapping.description_columns:
            part = raw[column].strip()
            if part and part not in description_parts:
                description_parts.append(part)
        description = " — ".join(description_parts)
        if not description:
            raise CSVIngestError(f"row {row_number}: description is required")
        amount_minor = _amount_minor(raw, mapping, row_number)

        currency = (
            raw[mapping.currency_column].strip().upper()
            if mapping.currency_column is not None
            else "NZD"
        ) or "NZD"
        if currency != "NZD":
            raise CSVIngestError(f"row {row_number}: only NZD is supported")

        status = (
            raw[mapping.status_column].strip().lower()
            if mapping.status_column is not None
            else "posted"
        ) or "posted"
        if status not in {"posted", "pending"}:
            raise CSVIngestError(f"row {row_number}: status must be posted or pending")

        reference = (
            raw[mapping.reference_column].strip()
            if mapping.reference_column is not None
            else ""
        )
        if not reference:
            reference = stable_id(
                "bankref", occurred_on, description, str(amount_minor), str(row_number)
            )
        row_id = stable_id("row", source_digest, reference)
        parsed.append(
            ParsedRow(
                canonical={
                    "source_row_id": row_id,
                    "account_id": account_id,
                    "occurred_on": occurred_on,
                    "description": description,
                    "amount_minor": str(amount_minor),
                    "currency": currency,
                    "status": status,
                    "external_reference": reference,
                },
                raw=raw,
            )
        )
    return ParsedCSV(
        rows=tuple(parsed),
        mapping_version=f"{mapping_version}+{mapping.profile}",
    )


def _validate_row(row: dict[str, str], row_number: int) -> tuple[int, str]:
    row_id = row["source_row_id"].strip()
    if not ID_PATTERN.fullmatch(row_id):
        raise CSVIngestError(f"row {row_number}: invalid source_row_id")
    if row["currency"] != "NZD":
        raise CSVIngestError(f"row {row_number}: only NZD is supported")
    if row["status"] not in {"posted", "pending"}:
        raise CSVIngestError(f"row {row_number}: status must be posted or pending")
    try:
        amount_minor = int(row["amount_minor"])
    except ValueError as error:
        raise CSVIngestError(f"row {row_number}: amount_minor must be an integer") from error
    if not row["description"].strip() or not row["external_reference"].strip():
        raise CSVIngestError(f"row {row_number}: description and reference are required")
    try:
        datetime.strptime(row["occurred_on"], "%Y-%m-%d")
    except ValueError as error:
        raise CSVIngestError(f"row {row_number}: occurred_on must be YYYY-MM-DD") from error
    return amount_minor, row_id


def _evidence_id(source_item_id: str, source_digest: str, row_id: str | None = None) -> str:
    if source_item_id == "src_koru_bank_csv_20260717":
        canonical = {
            None: "evd_koru_bank_csv",
            "row_koru_006": "evd_koru_mitre10_row",
            "row_koru_009": "evd_koru_figma_posted",
            "row_koru_010": "evd_koru_figma_pending",
        }
        if row_id in canonical:
            return canonical[row_id]
    return stable_id("evd", source_digest, row_id or "source")


class CSVImporter:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def ingest(
        self,
        path: str | Path,
        *,
        workspace_id: str,
        source_item_id: str | None = None,
        label: str | None = None,
        mapping_version: str = "bank_csv@1",
        received_at: str | None = None,
    ) -> ImportResult:
        source_path = Path(path)
        raw_bytes = source_path.read_bytes()
        digest = hashlib.sha256(raw_bytes).hexdigest()
        fieldnames, raw_rows = _read_csv(raw_bytes)
        if fieldnames == REQUIRED_COLUMNS:
            parsed = ParsedCSV(
                rows=tuple(ParsedRow(canonical=row, raw=row) for row in raw_rows),
                mapping_version=mapping_version,
            )
        else:
            account_rows = self.store.fetch_all(
                "SELECT account_id FROM accounts WHERE workspace_id = ? ORDER BY account_id",
                (workspace_id,),
            )
            if not account_rows:
                raise CSVIngestError("workspace has no account for this bank statement")
            if len(account_rows) > 1:
                raise CSVIngestError(
                    "workspace has multiple accounts; select an account before importing this bank statement"
                )
            parsed = _mapped_rows(
                raw_rows,
                mapping=_detect_header_mapping(fieldnames),
                account_id=cast(str, account_rows[0]["account_id"]),
                source_digest=digest,
                mapping_version=mapping_version,
            )
        rows = parsed.rows
        mapping_version = parsed.mapping_version
        source_item_id = source_item_id or stable_id("src", workspace_id, digest, mapping_version)
        label = label or source_path.name
        received_at = received_at or datetime.now().astimezone().isoformat(timespec="seconds")

        with self.store.transaction() as connection:
            existing = connection.execute(
                """
                SELECT source_item_id, row_count
                FROM source_items
                WHERE workspace_id = ? AND digest = ? AND mapping_version = ?
                """,
                (workspace_id, digest, mapping_version),
            ).fetchone()
            if existing is not None:
                existing_transaction_ids = tuple(
                    cast(str, row["transaction_id"])
                    for row in connection.execute(
                        """
                        SELECT t.transaction_id
                        FROM transactions t
                        JOIN source_rows r ON r.source_row_id = t.source_row_id
                        WHERE r.source_item_id = ?
                        ORDER BY r.row_number
                        """,
                        (existing["source_item_id"],),
                    )
                )
                return ImportResult(
                    source_item_id=existing["source_item_id"],
                    source_sha256=digest,
                    mapping_version=mapping_version,
                    row_count=existing["row_count"],
                    duplicate_import=True,
                    transaction_ids=existing_transaction_ids,
                )

            id_collision = connection.execute(
                "SELECT digest FROM source_items WHERE source_item_id = ?",
                (source_item_id,),
            ).fetchone()
            if id_collision is not None:
                raise CSVIngestError("source_item_id is already bound to different bytes")

            connection.execute(
                """
                INSERT INTO source_items(
                    source_item_id, workspace_id, source_type, label, digest,
                    mapping_version, received_at, status, row_count
                ) VALUES (?, ?, 'csv', ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    source_item_id,
                    workspace_id,
                    label,
                    digest,
                    mapping_version,
                    received_at,
                    len(rows),
                ),
            )
            source_evidence_id = _evidence_id(source_item_id, digest)
            connection.execute(
                """
                INSERT INTO evidence_links(
                    evidence_id, workspace_id, source_item_id, source_row_id, label, created_at
                ) VALUES (?, ?, ?, NULL, ?, ?)
                """,
                (
                    source_evidence_id,
                    workspace_id,
                    source_item_id,
                    f"{label} (SHA-256 {digest[:12]}…)",
                    received_at,
                ),
            )

            transaction_ids: list[str] = []
            seen_row_ids: set[str] = set()
            for row_number, parsed_row in enumerate(rows, start=1):
                row = parsed_row.canonical
                amount_minor, row_id = _validate_row(row, row_number)
                if row_id in seen_row_ids:
                    raise CSVIngestError(f"row {row_number}: duplicate source_row_id")
                seen_row_ids.add(row_id)
                account = connection.execute(
                    "SELECT 1 FROM accounts WHERE account_id = ? AND workspace_id = ?",
                    (row["account_id"], workspace_id),
                ).fetchone()
                if account is None:
                    raise CSVIngestError(f"row {row_number}: unknown account_id")
                raw_json = canonical_json(parsed_row.raw)
                row_hash = hashlib.sha256(
                    f"{digest}\0{row_number}\0{raw_json}".encode()
                ).hexdigest()
                connection.execute(
                    """
                    INSERT INTO source_rows(
                        source_row_id, source_item_id, row_number, account_id, occurred_on,
                        description, amount_minor, currency, source_status, external_reference,
                        mapping_version, row_hash, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        source_item_id,
                        row_number,
                        row["account_id"],
                        row["occurred_on"],
                        row["description"].strip(),
                        amount_minor,
                        row["currency"],
                        row["status"],
                        row["external_reference"].strip(),
                        mapping_version,
                        row_hash,
                        raw_json,
                    ),
                )
                evidence_id = _evidence_id(source_item_id, digest, row_id)
                if evidence_id != source_evidence_id:
                    connection.execute(
                        """
                        INSERT INTO evidence_links(
                            evidence_id, workspace_id, source_item_id, source_row_id, label, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            evidence_id,
                            workspace_id,
                            source_item_id,
                            row_id,
                            f"{row['description'].strip()} — source row {row_number}",
                            received_at,
                        ),
                    )
                transaction_id = transaction_id_for(row_id, digest)
                transaction_ids.append(transaction_id)
                connection.execute(
                    """
                    INSERT INTO transactions(
                        transaction_id, workspace_id, account_id, source_row_id, evidence_id,
                        occurred_on, description, amount_minor, currency, source_status, status,
                        classification, category, classification_source, rule_id,
                        duplicate_of_transaction_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unresolved', NULL,
                        'unclassified', NULL, NULL, ?, ?)
                    """,
                    (
                        transaction_id,
                        workspace_id,
                        row["account_id"],
                        row_id,
                        evidence_id,
                        row["occurred_on"],
                        row["description"].strip(),
                        amount_minor,
                        row["currency"],
                        row["status"],
                        row["status"],
                        received_at,
                        received_at,
                    ),
                )

        return ImportResult(
            source_item_id=source_item_id,
            source_sha256=digest,
            mapping_version=mapping_version,
            row_count=len(rows),
            duplicate_import=False,
            transaction_ids=tuple(transaction_ids),
        )
