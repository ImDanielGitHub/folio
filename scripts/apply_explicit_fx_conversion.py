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
        name="explicit_foreign_currency_conversion",
        sql="""
        CREATE TABLE foreign_currency_import_items (
            item_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            source_item_id TEXT NOT NULL REFERENCES source_items(source_item_id),
            ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
            provider TEXT NOT NULL,
            account_label TEXT NOT NULL,
            occurred_on TEXT NOT NULL,
            description TEXT NOT NULL,
            amount_minor INTEGER NOT NULL,
            currency TEXT NOT NULL CHECK (currency != 'NZD'),
            external_reference TEXT NOT NULL,
            source_status TEXT NOT NULL CHECK (source_status IN ('posted', 'pending')),
            raw_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'converted', 'rejected')),
            converted_transaction_id TEXT REFERENCES transactions(transaction_id),
            created_at TEXT NOT NULL,
            decided_at TEXT,
            UNIQUE (source_item_id, ordinal),
            UNIQUE (workspace_id, provider, external_reference)
        );

        CREATE TABLE fx_rate_revisions (
            rate_id TEXT NOT NULL,
            revision INTEGER NOT NULL CHECK (revision >= 1),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            base_currency TEXT NOT NULL CHECK (length(base_currency) = 3),
            quote_currency TEXT NOT NULL CHECK (quote_currency = 'NZD'),
            rate_numerator INTEGER NOT NULL CHECK (rate_numerator > 0),
            rate_denominator INTEGER NOT NULL CHECK (rate_denominator > 0),
            effective_on TEXT NOT NULL,
            source_label TEXT NOT NULL CHECK (length(trim(source_label)) BETWEEN 1 AND 300),
            evidence_id TEXT NOT NULL REFERENCES evidence_links(evidence_id),
            status TEXT NOT NULL CHECK (status IN ('active', 'superseded')),
            created_at TEXT NOT NULL,
            PRIMARY KEY (rate_id, revision)
        );

        CREATE TABLE fx_conversion_events (
            event_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            item_id TEXT NOT NULL REFERENCES foreign_currency_import_items(item_id),
            event_type TEXT NOT NULL CHECK (event_type IN ('converted', 'rejected')),
            rate_id TEXT,
            rate_revision INTEGER,
            target_account_id TEXT REFERENCES accounts(account_id),
            original_amount_minor INTEGER NOT NULL,
            original_currency TEXT NOT NULL,
            converted_amount_minor INTEGER,
            exact_numerator TEXT,
            exact_denominator TEXT,
            rounding_mode TEXT,
            reason TEXT NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 500),
            evidence_ids_json TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY (rate_id, rate_revision)
                REFERENCES fx_rate_revisions(rate_id, revision)
        );

        CREATE INDEX foreign_currency_pending
            ON foreign_currency_import_items(workspace_id, status, occurred_on);
        CREATE INDEX fx_rates_lookup
            ON fx_rate_revisions(
                workspace_id, base_currency, quote_currency, effective_on, status
            );

        CREATE TRIGGER fx_rates_no_update
        BEFORE UPDATE ON fx_rate_revisions
        BEGIN
            SELECT RAISE(ABORT, 'FX rate revisions are append-only');
        END;

        CREATE TRIGGER fx_rates_no_delete
        BEFORE DELETE ON fx_rate_revisions
        BEGIN
            SELECT RAISE(ABORT, 'FX rate revisions are append-only');
        END;

        CREATE TRIGGER fx_events_no_update
        BEFORE UPDATE ON fx_conversion_events
        BEGIN
            SELECT RAISE(ABORT, 'FX conversion events are append-only');
        END;

        CREATE TRIGGER fx_events_no_delete
        BEFORE DELETE ON fx_conversion_events
        BEGIN
            SELECT RAISE(ABORT, 'FX conversion events are append-only');
        END;
        """,
    ),
'''

MODULE = '''"""Foreign provider staging and explicit evidence-backed NZD conversion."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Mapping

from finance_agent.storage import SQLiteStore, canonical_json

MAX_RATE_PART = 10**12
MAPPING_VERSION = "fx_conversion@1"


def _stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}_{hashlib.sha256(chr(0).join(parts).encode()).hexdigest()[:24]}"


def stage_plaid_result(
    store: SQLiteStore,
    result: Any,
    *,
    workspace_id: str,
    mapping_version: str,
) -> Any:
    """Persist original USD rows without creating NZD ledger transactions."""

    if str(result.currency).upper() == "NZD":
        return result
    existing = store.fetch_one(
        """
        SELECT source_item_id FROM source_items
        WHERE workspace_id = ? AND digest = ? AND mapping_version = ?
        """,
        (workspace_id, result.digest, mapping_version),
    )
    if existing is not None:
        return replace(
            result,
            source_item_id=str(existing["source_item_id"]),
            status="deduplicated_foreign_currency",
        )
    now = str(result.synced_at)
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO source_items(
                source_item_id, workspace_id, source_type, label, digest,
                mapping_version, received_at, status, row_count
            ) VALUES (?, ?, 'plaid_fixture', ?, ?, ?, ?, 'pending', ?)
            """,
            (
                result.source_item_id,
                workspace_id,
                f"{result.account_label} · foreign currency staged",
                result.digest,
                mapping_version,
                now,
                result.row_count,
            ),
        )
        source_evidence_id = _stable_id("evd", result.digest, "foreign_source")
        connection.execute(
            """
            INSERT INTO evidence_links(
                evidence_id, workspace_id, source_item_id, source_row_id,
                label, created_at
            ) VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (
                source_evidence_id,
                workspace_id,
                result.source_item_id,
                f"{result.account_label} · original {result.currency} provider source",
                now,
            ),
        )
        for ordinal, transaction in enumerate(result.transactions, start=1):
            raw = {
                "provider": "plaid",
                "occurredOn": transaction.occurred_on,
                "description": transaction.description,
                "amountMinor": transaction.amount_minor,
                "currency": result.currency,
                "externalReference": transaction.external_reference,
                "status": transaction.status,
            }
            item_id = _stable_id(
                "fxitem",
                workspace_id,
                "plaid",
                transaction.external_reference,
            )
            connection.execute(
                """
                INSERT INTO foreign_currency_import_items(
                    item_id, workspace_id, source_item_id, ordinal, provider,
                    account_label, occurred_on, description, amount_minor,
                    currency, external_reference, source_status, raw_json,
                    status, created_at
                ) VALUES (?, ?, ?, ?, 'plaid', ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    item_id,
                    workspace_id,
                    result.source_item_id,
                    ordinal,
                    result.account_label,
                    transaction.occurred_on,
                    transaction.description,
                    transaction.amount_minor,
                    result.currency,
                    transaction.external_reference,
                    transaction.status,
                    canonical_json(raw),
                    now,
                ),
            )
    return replace(result, status="quarantined_foreign_currency")


class ForeignCurrencyService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def list_items(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        rows = self.store.fetch_all(
            """
            SELECT * FROM foreign_currency_import_items
            WHERE workspace_id = ? ORDER BY occurred_on, item_id
            """,
            (workspace_id,),
        )
        return tuple(
            {
                "itemId": str(row["item_id"]),
                "sourceItemId": str(row["source_item_id"]),
                "provider": str(row["provider"]),
                "accountLabel": str(row["account_label"]),
                "occurredOn": str(row["occurred_on"]),
                "description": str(row["description"]),
                "amountMinor": int(row["amount_minor"]),
                "currency": str(row["currency"]),
                "externalReference": str(row["external_reference"]),
                "sourceStatus": str(row["source_status"]),
                "status": str(row["status"]),
                "convertedTransactionId": (
                    str(row["converted_transaction_id"])
                    if row["converted_transaction_id"] else None
                ),
                "createdAt": str(row["created_at"]),
                "decidedAt": str(row["decided_at"]) if row["decided_at"] else None,
            }
            for row in rows
        )

    def add_rate(
        self,
        *,
        workspace_id: str,
        base_currency: str,
        effective_on: str,
        rate_numerator: int,
        rate_denominator: int,
        source_label: str,
        evidence_id: str,
    ) -> dict[str, object]:
        base = base_currency.strip().upper()
        if len(base) != 3 or base == "NZD" or not base.isalpha():
            raise ValueError("baseCurrency must be a non-NZD ISO-style three-letter code")
        date.fromisoformat(effective_on)
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            or value > MAX_RATE_PART
            for value in (rate_numerator, rate_denominator)
        ):
            raise ValueError("FX rate numerator and denominator must be positive bounded integers")
        label = source_label.strip()
        if not label:
            raise ValueError("FX rate sourceLabel must not be blank")
        evidence = self.store.fetch_one(
            "SELECT evidence_id FROM evidence_links WHERE workspace_id = ? AND evidence_id = ?",
            (workspace_id, evidence_id),
        )
        if evidence is None:
            raise KeyError(evidence_id)
        rate_id = _stable_id(
            "fxrate", workspace_id, base, "NZD", effective_on
        )
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            current = connection.execute(
                "SELECT COALESCE(MAX(revision), 0) AS revision FROM fx_rate_revisions WHERE rate_id = ?",
                (rate_id,),
            ).fetchone()
            revision = int(current["revision"]) + 1
            if revision > 1:
                connection.execute(
                    "UPDATE fx_rate_revisions SET status = 'superseded' WHERE rate_id = ? AND status = 'active'",
                    (rate_id,),
                )
            connection.execute(
                """
                INSERT INTO fx_rate_revisions(
                    rate_id, revision, workspace_id, base_currency,
                    quote_currency, rate_numerator, rate_denominator,
                    effective_on, source_label, evidence_id, status, created_at
                ) VALUES (?, ?, ?, ?, 'NZD', ?, ?, ?, ?, ?, 'active', ?)
                """,
                (
                    rate_id,
                    revision,
                    workspace_id,
                    base,
                    rate_numerator,
                    rate_denominator,
                    effective_on,
                    label[:300],
                    evidence_id,
                    now,
                ),
            )
        return {
            "rateId": rate_id,
            "revision": revision,
            "workspaceId": workspace_id,
            "baseCurrency": base,
            "quoteCurrency": "NZD",
            "rateNumerator": rate_numerator,
            "rateDenominator": rate_denominator,
            "effectiveOn": effective_on,
            "sourceLabel": label[:300],
            "evidenceId": evidence_id,
            "status": "active",
            "createdAt": now,
        }

    def convert(
        self,
        *,
        workspace_id: str,
        item_id: str,
        rate_id: str,
        target_account_id: str,
        reason: str,
    ) -> dict[str, object]:
        reason_value = reason.strip()
        if not reason_value:
            raise ValueError("FX conversion reason must not be blank")
        now = datetime.now(UTC).isoformat()
        try:
            with self.store.transaction() as connection:
                item = connection.execute(
                    "SELECT * FROM foreign_currency_import_items WHERE workspace_id = ? AND item_id = ?",
                    (workspace_id, item_id),
                ).fetchone()
                if item is None:
                    raise KeyError(item_id)
                if str(item["status"]) == "converted":
                    event = connection.execute(
                        "SELECT event_id FROM fx_conversion_events WHERE item_id = ? AND event_type = 'converted'",
                        (item_id,),
                    ).fetchone()
                    return {
                        "itemId": item_id,
                        "eventId": str(event["event_id"]),
                        "transactionId": str(item["converted_transaction_id"]),
                        "status": "converted",
                        "idempotentReplay": True,
                    }
                if str(item["status"]) != "pending":
                    raise ValueError("only a pending foreign-currency item can be converted")
                rate = connection.execute(
                    """
                    SELECT * FROM fx_rate_revisions
                    WHERE workspace_id = ? AND rate_id = ? AND status = 'active'
                    ORDER BY revision DESC LIMIT 1
                    """,
                    (workspace_id, rate_id),
                ).fetchone()
                if rate is None:
                    raise KeyError(rate_id)
                if str(rate["base_currency"]) != str(item["currency"]):
                    raise ValueError("FX rate base currency does not match the staged item")
                if str(rate["effective_on"]) > str(item["occurred_on"]):
                    raise ValueError("FX rate cannot be effective after the transaction date")
                account = connection.execute(
                    "SELECT account_id, currency FROM accounts WHERE workspace_id = ? AND account_id = ?",
                    (workspace_id, target_account_id),
                ).fetchone()
                if account is None:
                    raise KeyError(target_account_id)
                if str(account["currency"]) != "NZD":
                    raise ValueError("FX conversion target account must be an NZD ledger account")
                numerator = int(item["amount_minor"]) * int(rate["rate_numerator"])
                denominator = int(rate["rate_denominator"])
                converted = int(
                    (Decimal(numerator) / Decimal(denominator)).quantize(
                        Decimal("1"), rounding=ROUND_HALF_EVEN
                    )
                )
                row_id = _stable_id("row", item_id, rate_id, str(rate["revision"]))
                evidence_id = _stable_id("evd", item_id, rate_id, str(rate["revision"]))
                transaction_id = _stable_id("txn", item_id, rate_id, str(rate["revision"]))
                raw = {
                    "original": {
                        "amountMinor": int(item["amount_minor"]),
                        "currency": str(item["currency"]),
                        "externalReference": str(item["external_reference"]),
                    },
                    "rate": {
                        "rateId": rate_id,
                        "revision": int(rate["revision"]),
                        "numerator": int(rate["rate_numerator"]),
                        "denominator": denominator,
                        "quoteCurrency": "NZD",
                        "effectiveOn": str(rate["effective_on"]),
                    },
                    "conversion": {
                        "amountMinor": converted,
                        "currency": "NZD",
                        "exactNumerator": str(numerator),
                        "exactDenominator": str(denominator),
                        "roundingMode": "ROUND_HALF_EVEN",
                    },
                }
                raw_json = canonical_json(raw)
                connection.execute(
                    """
                    INSERT INTO source_rows(
                        source_row_id, source_item_id, row_number, account_id,
                        occurred_on, description, amount_minor, currency,
                        source_status, external_reference, mapping_version,
                        row_hash, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'NZD', ?, ?, ?, ?, ?)
                    """,
                    (
                        row_id,
                        item["source_item_id"],
                        item["ordinal"],
                        target_account_id,
                        item["occurred_on"],
                        item["description"],
                        converted,
                        item["source_status"],
                        f"fx:{item['external_reference']}:{rate_id}:{rate['revision']}",
                        MAPPING_VERSION,
                        hashlib.sha256(raw_json.encode()).hexdigest(),
                        raw_json,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO evidence_links(
                        evidence_id, workspace_id, source_item_id, source_row_id,
                        label, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence_id,
                        workspace_id,
                        item["source_item_id"],
                        row_id,
                        f"{item['description']} · explicit {item['currency']}/NZD conversion",
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO transactions(
                        transaction_id, workspace_id, account_id, source_row_id,
                        evidence_id, occurred_on, description, amount_minor,
                        currency, source_status, status, classification, category,
                        classification_source, rule_id, duplicate_of_transaction_id,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NZD', ?, 'pending',
                        'unresolved', NULL, 'unclassified', NULL, NULL, ?, ?)
                    """,
                    (
                        transaction_id,
                        workspace_id,
                        target_account_id,
                        row_id,
                        evidence_id,
                        item["occurred_on"],
                        item["description"],
                        converted,
                        item["source_status"],
                        now,
                        now,
                    ),
                )
                event_id = _stable_id("fxevent", item_id, rate_id, str(rate["revision"]))
                evidence_ids = [str(rate["evidence_id"]), evidence_id]
                connection.execute(
                    """
                    INSERT INTO fx_conversion_events(
                        event_id, workspace_id, item_id, event_type, rate_id,
                        rate_revision, target_account_id, original_amount_minor,
                        original_currency, converted_amount_minor,
                        exact_numerator, exact_denominator, rounding_mode,
                        reason, evidence_ids_json, occurred_at
                    ) VALUES (?, ?, ?, 'converted', ?, ?, ?, ?, ?, ?, ?, ?,
                        'ROUND_HALF_EVEN', ?, ?, ?)
                    """,
                    (
                        event_id,
                        workspace_id,
                        item_id,
                        rate_id,
                        rate["revision"],
                        target_account_id,
                        item["amount_minor"],
                        item["currency"],
                        converted,
                        str(numerator),
                        str(denominator),
                        reason_value[:500],
                        canonical_json(evidence_ids),
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE foreign_currency_import_items
                    SET status = 'converted', converted_transaction_id = ?,
                        decided_at = ? WHERE item_id = ?
                    """,
                    (transaction_id, now, item_id),
                )
                pending = connection.execute(
                    "SELECT COUNT(*) AS count FROM foreign_currency_import_items WHERE source_item_id = ? AND status = 'pending'",
                    (item["source_item_id"],),
                ).fetchone()
                if int(pending["count"]) == 0:
                    connection.execute(
                        "UPDATE source_items SET status = 'processed' WHERE source_item_id = ?",
                        (item["source_item_id"],),
                    )
        except sqlite3.IntegrityError as exc:
            if "hard locked" in str(exc):
                raise PermissionError("converted transaction date is inside a hard-locked period") from exc
            raise
        return {
            "itemId": item_id,
            "eventId": event_id,
            "transactionId": transaction_id,
            "status": "converted",
            "originalAmountMinor": int(item["amount_minor"]),
            "originalCurrency": str(item["currency"]),
            "convertedAmountMinor": converted,
            "currency": "NZD",
            "rateId": rate_id,
            "rateRevision": int(rate["revision"]),
            "exactNumerator": str(numerator),
            "exactDenominator": str(denominator),
            "roundingMode": "ROUND_HALF_EVEN",
            "evidenceIds": evidence_ids,
        }

    def reject(
        self,
        *,
        workspace_id: str,
        item_id: str,
        reason: str,
    ) -> dict[str, object]:
        reason_value = reason.strip()
        if not reason_value:
            raise ValueError("FX rejection reason must not be blank")
        now = datetime.now(UTC).isoformat()
        with self.store.transaction() as connection:
            item = connection.execute(
                "SELECT * FROM foreign_currency_import_items WHERE workspace_id = ? AND item_id = ?",
                (workspace_id, item_id),
            ).fetchone()
            if item is None:
                raise KeyError(item_id)
            if str(item["status"]) == "rejected":
                event = connection.execute(
                    "SELECT event_id FROM fx_conversion_events WHERE item_id = ? AND event_type = 'rejected'",
                    (item_id,),
                ).fetchone()
                return {
                    "itemId": item_id,
                    "eventId": str(event["event_id"]),
                    "status": "rejected",
                    "idempotentReplay": True,
                }
            if str(item["status"]) != "pending":
                raise ValueError("only a pending foreign-currency item can be rejected")
            event_id = _stable_id("fxevent", item_id, "rejected")
            connection.execute(
                """
                INSERT INTO fx_conversion_events(
                    event_id, workspace_id, item_id, event_type,
                    original_amount_minor, original_currency, reason,
                    evidence_ids_json, occurred_at
                ) VALUES (?, ?, ?, 'rejected', ?, ?, ?, '[]', ?)
                """,
                (
                    event_id,
                    workspace_id,
                    item_id,
                    item["amount_minor"],
                    item["currency"],
                    reason_value[:500],
                    now,
                ),
            )
            connection.execute(
                "UPDATE foreign_currency_import_items SET status = 'rejected', decided_at = ? WHERE item_id = ?",
                (now, item_id),
            )
        return {"itemId": item_id, "eventId": event_id, "status": "rejected"}
'''

SERVICE_METHODS = '''    async def list_foreign_currency_items(
        self, *, workspace_id: str
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return ForeignCurrencyService(self.store).list_items(workspace_id)

    async def add_fx_rate(
        self,
        *,
        workspace_id: str,
        base_currency: str,
        effective_on: str,
        rate_numerator: int,
        rate_denominator: int,
        source_label: str,
        evidence_id: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        return ForeignCurrencyService(self.store).add_rate(
            workspace_id=workspace_id,
            base_currency=base_currency,
            effective_on=effective_on,
            rate_numerator=rate_numerator,
            rate_denominator=rate_denominator,
            source_label=source_label,
            evidence_id=evidence_id,
        )

    async def convert_foreign_currency_item(
        self,
        *,
        workspace_id: str,
        item_id: str,
        rate_id: str,
        target_account_id: str,
        reason: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = ForeignCurrencyService(self.store).convert(
                workspace_id=workspace_id,
                item_id=item_id,
                rate_id=rate_id,
                target_account_id=target_account_id,
                reason=reason,
            )
            result = self.daily_close.run()
            self._register_daily_close_events(result)
        return {**value, "dailyCloseRunId": result.run_id}

    async def reject_foreign_currency_item(
        self, *, workspace_id: str, item_id: str, reason: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return ForeignCurrencyService(self.store).reject(
                workspace_id=workspace_id,
                item_id=item_id,
                reason=reason,
            )
'''

ROUTE_MODELS = '''

class FxRateRequest(RequestModel):
    base_currency: str = Field(alias="baseCurrency", min_length=3, max_length=3)
    effective_on: date = Field(alias="effectiveOn")
    rate_numerator: int = Field(alias="rateNumerator", gt=0, le=10**12)
    rate_denominator: int = Field(alias="rateDenominator", gt=0, le=10**12)
    source_label: str = Field(alias="sourceLabel", min_length=1, max_length=300)
    evidence_id: str = Field(alias="evidenceId", pattern=IDENTIFIER_PATTERN)


class FxConvertRequest(RequestModel):
    rate_id: str = Field(alias="rateId", pattern=IDENTIFIER_PATTERN)
    target_account_id: str = Field(alias="targetAccountId", pattern=IDENTIFIER_PATTERN)
    reason: str = Field(min_length=1, max_length=500)


class FxRejectRequest(RequestModel):
    reason: str = Field(min_length=1, max_length=500)
'''

ROUTES = '''    @router.get("/v1/workspaces/{workspace_id}/foreign-currency/items")
    async def list_foreign_currency_items(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        items = await services.list_foreign_currency_items(workspace_id=workspace_id)
        return {"workspaceId": workspace_id, "items": list(items)}

    @router.post("/v1/workspaces/{workspace_id}/foreign-currency/rates")
    async def add_fx_rate(
        workspace_id: PathIdentifier,
        body: FxRateRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.add_fx_rate(
                    workspace_id=workspace_id,
                    base_currency=body.base_currency,
                    effective_on=body.effective_on.isoformat(),
                    rate_numerator=body.rate_numerator,
                    rate_denominator=body.rate_denominator,
                    source_label=body.source_label,
                    evidence_id=body.evidence_id,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="FX evidence not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post(
        "/v1/workspaces/{workspace_id}/foreign-currency/items/{item_id}/convert"
    )
    async def convert_foreign_currency_item(
        workspace_id: PathIdentifier,
        item_id: PathIdentifier,
        body: FxConvertRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.convert_foreign_currency_item(
                    workspace_id=workspace_id,
                    item_id=item_id,
                    rate_id=body.rate_id,
                    target_account_id=body.target_account_id,
                    reason=body.reason,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="FX item, rate or target account not found") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post(
        "/v1/workspaces/{workspace_id}/foreign-currency/items/{item_id}/reject"
    )
    async def reject_foreign_currency_item(
        workspace_id: PathIdentifier,
        item_id: PathIdentifier,
        body: FxRejectRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.reject_foreign_currency_item(
                    workspace_id=workspace_id,
                    item_id=item_id,
                    reason=body.reason,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="foreign-currency item not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

from pathlib import Path

from finance_agent.connectors.plaid_fixture import PlaidFixtureIngestor
from finance_agent.finance import FinanceEngine
from finance_agent.finance.foreign_currency import (
    ForeignCurrencyService,
    stage_plaid_result,
)
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def payload():
    return {
        "account": {"name": "US checking", "maskedNumber": "1111", "currency": "USD"},
        "syncedAt": "2026-08-26T00:00:00+00:00",
        "transactions": [
            {
                "occurredOn": "2026-08-20",
                "description": "USD software",
                "amountMinor": -10000,
                "externalReference": "usd-1",
                "status": "posted",
            },
            {
                "occurredOn": "2026-08-21",
                "description": "USD rounding item",
                "amountMinor": -1,
                "externalReference": "usd-2",
                "status": "posted",
            },
        ],
    }


def setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    parsed = PlaidFixtureIngestor().ingest(payload(), source_item_id="src_fx_plaid_test")
    staged = stage_plaid_result(
        store,
        parsed,
        workspace_id="ws_koru_studio",
        mapping_version="plaid_fx_test@1",
    )
    return store, staged, ForeignCurrencyService(store)


def test_foreign_plaid_rows_stage_without_nzd_transactions(tmp_path: Path) -> None:
    store, staged, service = setup(tmp_path)
    assert staged.status == "quarantined_foreign_currency"
    items = service.list_items("ws_koru_studio")
    assert len(items) == 2
    assert all(item["currency"] == "USD" for item in items)
    assert all(item["status"] == "pending" for item in items)
    transaction_count = store.fetch_one(
        "SELECT COUNT(*) AS count FROM transactions t JOIN source_rows r ON r.source_row_id = t.source_row_id WHERE r.source_item_id = 'src_fx_plaid_test'"
    )
    assert int(transaction_count["count"]) == 0


def test_evidence_backed_rational_rate_converts_exactly_and_receipts_rounding(tmp_path: Path) -> None:
    store, _staged, service = setup(tmp_path)
    rate = service.add_rate(
        workspace_id="ws_koru_studio",
        base_currency="USD",
        effective_on="2026-08-01",
        rate_numerator=162,
        rate_denominator=100,
        source_label="Owner-supplied USD/NZD rate for August",
        evidence_id="evd_koru_bank_csv",
    )
    items = service.list_items("ws_koru_studio")
    first = next(item for item in items if item["externalReference"] == "usd-1")
    converted = service.convert(
        workspace_id="ws_koru_studio",
        item_id=str(first["itemId"]),
        rate_id=str(rate["rateId"]),
        target_account_id="acct_koru_business",
        reason="Use the documented August USD/NZD rate.",
    )
    assert converted["originalAmountMinor"] == -10000
    assert converted["convertedAmountMinor"] == -16200
    assert converted["currency"] == "NZD"
    assert converted["roundingMode"] == "ROUND_HALF_EVEN"
    row = store.fetch_one(
        "SELECT amount_minor, currency, raw_json FROM source_rows WHERE source_row_id = ?",
        (f"row_{converted['transactionId'].removeprefix('txn_')}",),
    )
    if row is None:
        row = store.fetch_one(
            "SELECT amount_minor, currency, raw_json FROM source_rows WHERE source_item_id = 'src_fx_plaid_test' AND external_reference LIKE 'fx:usd-1:%'"
        )
    assert int(row["amount_minor"]) == -16200
    assert str(row["currency"]) == "NZD"
    assert '"currency":"USD"' in str(row["raw_json"])


def test_half_even_rounding_and_idempotent_conversion(tmp_path: Path) -> None:
    _store, _staged, service = setup(tmp_path)
    rate = service.add_rate(
        workspace_id="ws_koru_studio",
        base_currency="USD",
        effective_on="2026-08-01",
        rate_numerator=3,
        rate_denominator=2,
        source_label="Test 1.5 rate",
        evidence_id="evd_koru_bank_csv",
    )
    item = next(
        value for value in service.list_items("ws_koru_studio")
        if value["externalReference"] == "usd-2"
    )
    converted = service.convert(
        workspace_id="ws_koru_studio",
        item_id=str(item["itemId"]),
        rate_id=str(rate["rateId"]),
        target_account_id="acct_koru_business",
        reason="Test deterministic rounding.",
    )
    assert converted["exactNumerator"] == "-3"
    assert converted["exactDenominator"] == "2"
    assert converted["convertedAmountMinor"] == -2
    replay = service.convert(
        workspace_id="ws_koru_studio",
        item_id=str(item["itemId"]),
        rate_id=str(rate["rateId"]),
        target_account_id="acct_koru_business",
        reason="Repeated conversion.",
    )
    assert replay["idempotentReplay"] is True


def test_future_or_wrong_currency_rate_fails_closed(tmp_path: Path) -> None:
    _store, _staged, service = setup(tmp_path)
    future = service.add_rate(
        workspace_id="ws_koru_studio",
        base_currency="USD",
        effective_on="2026-09-01",
        rate_numerator=160,
        rate_denominator=100,
        source_label="Future rate",
        evidence_id="evd_koru_bank_csv",
    )
    item = service.list_items("ws_koru_studio")[0]
    try:
        service.convert(
            workspace_id="ws_koru_studio",
            item_id=str(item["itemId"]),
            rate_id=str(future["rateId"]),
            target_account_id="acct_koru_business",
            reason="Invalid future rate.",
        )
    except ValueError as exc:
        assert "effective after" in str(exc)
    else:
        raise AssertionError("future FX rate was accepted")
'''


def add_migration_module() -> None:
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
    write("services/api/src/finance_agent/finance/foreign_currency.py", MODULE)


def intercept_plaid() -> None:
    path = "services/api/src/finance_agent/finance/service.py"
    content = read(path)
    import_marker = "from finance_agent.storage import SQLiteStore, canonical_json\n"
    import_line = "from finance_agent.finance.foreign_currency import stage_plaid_result\n"
    if import_line not in content:
        if import_marker not in content:
            raise RuntimeError("finance service storage import marker missing")
        content = content.replace(import_marker, import_marker + import_line, 1)
    marker = '''        parsed = PlaidFixtureIngestor().ingest(payload, source_item_id=source_item_id)
        version = mapping_version or PlaidFixtureIngestor.MAPPING_VERSION
        plaid_account_id = account_id or PLAID_ACCOUNT_ID
        with self.store.transaction() as connection:
'''
    replacement = '''        parsed = PlaidFixtureIngestor().ingest(payload, source_item_id=source_item_id)
        version = mapping_version or PlaidFixtureIngestor.MAPPING_VERSION
        plaid_account_id = account_id or PLAID_ACCOUNT_ID
        if parsed.currency != "NZD":
            return stage_plaid_result(
                self.store,
                parsed,
                workspace_id=WORKSPACE_ID,
                mapping_version=version,
            )
        with self.store.transaction() as connection:
'''
    if "return stage_plaid_result(" not in content:
        if marker not in content:
            raise RuntimeError("Plaid ingestion marker missing; review current quarantine implementation")
        content = content.replace(marker, replacement, 1)
    write(path, content)


def update_service_protocol_routes() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.finance.analytics import DeterministicFinanceAnalytics\n"
    import_line = "from finance_agent.finance.foreign_currency import ForeignCurrencyService\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("analytics import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "monthly_finance_analytics", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def monthly_finance_analytics(\n"
    addition = '''    async def list_foreign_currency_items(\n        self, *, workspace_id: str\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def add_fx_rate(\n        self, *, workspace_id: str, base_currency: str, effective_on: str,\n        rate_numerator: int, rate_denominator: int, source_label: str,\n        evidence_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def convert_foreign_currency_item(\n        self, *, workspace_id: str, item_id: str, rate_id: str,\n        target_account_id: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n    async def reject_foreign_currency_item(\n        self, *, workspace_id: str, item_id: str, reason: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("analytics protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass StatementReconciliationRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("StatementReconciliationRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.get("/v1/workspaces/{workspace_id}/analytics/monthly")\n'
    if route_marker not in content:
        raise RuntimeError("analytics route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def update_audit_state_identity() -> None:
    path = "services/api/src/finance_agent/audit_trail.py"
    content = read(path)
    kind_marker = '        "finance_analysis",\n'
    if '"foreign_currency"' not in content:
        if kind_marker not in content:
            raise RuntimeError("finance analysis audit kind marker missing")
        content = content.replace(kind_marker, kind_marker + '        "foreign_currency",\n', 1)
    optional_marker = '        if self._table_exists("finance_analysis_receipts"):\n'
    block = '''        if self._table_exists("fx_conversion_events"):
            for row in self.store.fetch_all(
                "SELECT * FROM fx_conversion_events WHERE workspace_id = ? ORDER BY occurred_at, event_id",
                (workspace_id,),
            ):
                yield AuditEvent(
                    event_id=str(row["event_id"]),
                    workspace_id=workspace_id,
                    kind="foreign_currency",
                    action=f"fx_{row['event_type']}",
                    status=str(row["event_type"]),
                    occurred_at=str(row["occurred_at"]),
                    actor="owner",
                    correlation_id=None,
                    subject_type="foreign_currency_item",
                    subject_id=str(row["item_id"]),
                    evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
                    metadata={
                        "rateId": str(row["rate_id"]) if row["rate_id"] else None,
                        "rateRevision": int(row["rate_revision"]) if row["rate_revision"] else None,
                        "originalAmountMinor": int(row["original_amount_minor"]),
                        "originalCurrency": str(row["original_currency"]),
                        "convertedAmountMinor": int(row["converted_amount_minor"]) if row["converted_amount_minor"] is not None else None,
                        "roundingMode": str(row["rounding_mode"]) if row["rounding_mode"] else None,
                        "reasonIncluded": False,
                    },
                )
'''
    if "fx_converted" not in content:
        if optional_marker not in content:
            raise RuntimeError("finance analysis optional audit marker missing")
        content = content.replace(optional_marker, block + optional_marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/storage/state_identity.py"
    content = read(path)
    marker = '''    statement_reconciliations = _rows(
        store,
        """
        SELECT reconciliation_id, revision, account_id, source_item_id,
               period_start, period_end, opening_balance_minor,
               stated_closing_balance_minor, posted_activity_minor,
               calculated_closing_balance_minor, discrepancy_minor,
               status, actor, evidence_ids_json, created_at
        FROM statement_reconciliation_revisions
        WHERE workspace_id = ? ORDER BY reconciliation_id, revision
        """,
        (workspace_id,),
    )
'''
    addition = marker + '''    fx_rates = _rows(
        store,
        """
        SELECT rate_id, revision, base_currency, quote_currency,
               rate_numerator, rate_denominator, effective_on,
               evidence_id, status, created_at
        FROM fx_rate_revisions
        WHERE workspace_id = ? ORDER BY rate_id, revision
        """,
        (workspace_id,),
    )
    fx_events = _rows(
        store,
        """
        SELECT event_id, item_id, event_type, rate_id, rate_revision,
               target_account_id, original_amount_minor, original_currency,
               converted_amount_minor, exact_numerator, exact_denominator,
               rounding_mode, evidence_ids_json, occurred_at
        FROM fx_conversion_events
        WHERE workspace_id = ? ORDER BY occurred_at, event_id
        """,
        (workspace_id,),
    )
'''
    if "fx_rates = _rows(" not in content:
        if marker not in content:
            raise RuntimeError("statement reconciliation identity marker missing")
        content = content.replace(marker, addition, 1)
    payload_marker = '        "statementReconciliations": statement_reconciliations,\n'
    if '"fxRates": fx_rates' not in content:
        if payload_marker not in content:
            raise RuntimeError("statement reconciliation identity payload missing")
        content = content.replace(
            payload_marker,
            payload_marker + '        "fxRates": fx_rates,\n        "fxEvents": fx_events,\n',
            1,
        )
    write(path, content)


def tests_docs() -> None:
    write("services/api/tests/finance/test_explicit_fx_conversion.py", TESTS)
    write("docs/FOREIGN_CURRENCY.md", '''# Explicit foreign-currency conversion\n\nForeign Plaid amounts remain in their original minor units and currency. Folio stores the provider source and staged items but creates no NZD source row or transaction. A rate is an append-only rational number, such as `162/100 NZD per USD`, with an effective date, human source label and existing evidence ID. No model or network lookup supplies a rate.\n\nConversion requires a pending item, matching active base currency, rate effective on or before the transaction, and an explicit NZD target account. NZD minor units are calculated as `originalMinor × numerator ÷ denominator` with `ROUND_HALF_EVEN`; exact numerator, denominator, rounded value, rate revision and evidence are written to the source row and append-only conversion event. Hard-locked dates fail before posting.\n\nThis creates a traceable bookkeeping conversion. It does not claim the rate is legally or tax appropriate, and Folio does not silently fetch, infer or replace rates.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 42: explicit evidence-backed foreign-currency conversion\n\n- Foreign provider rows stage in original minor units without NZD ledger posting.\n- Rates are append-only positive rationals with date, label and evidence.\n- Conversion requires a matching rate and explicit NZD target account.\n- Exact numerator, denominator, rounded result and half-even mode are receipted.\n- Hard-locked dates block posting and repeated conversion is idempotent.\n- Folio does not fetch, infer or claim the appropriateness of an FX rate.\n'''
    if "## Stack 42: explicit evidence-backed foreign-currency conversion" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_module()
    intercept_plaid()
    update_service_protocol_routes()
    update_audit_state_identity()
    tests_docs()
    print("explicit FX conversion changes applied")


if __name__ == "__main__":
    main()
