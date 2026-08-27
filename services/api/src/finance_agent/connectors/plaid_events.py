"""Append-only persistence for Plaid added, modified, and removed events."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from finance_agent.connectors.base import ConnectorError
from finance_agent.connectors.plaid import PlaidAccount, PlaidTransaction
from finance_agent.storage import SQLiteStore, canonical_json


@dataclass(frozen=True, slots=True)
class PlaidEventBatchResult:
    source_item_id: str
    digest: str
    status: str
    event_count: int
    added_count: int
    modified_count: int
    removed_count: int


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _transaction_payload(transaction: PlaidTransaction) -> dict[str, object]:
    return {
        "providerId": transaction.provider_id,
        "accountId": transaction.account_id,
        "occurredOn": transaction.occurred_on,
        "description": transaction.description,
        "amountMinor": transaction.amount_minor,
        "currency": transaction.currency,
        "externalReference": transaction.external_reference,
        "pending": transaction.pending,
    }


def _removed_payload(item: Mapping[str, object]) -> dict[str, object]:
    provider_id = item.get("transaction_id") or item.get("id")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise ConnectorError(
            "Plaid removed transaction is missing transaction_id",
            code="provider_invalid_response",
            provider="plaid",
        )
    account_id = item.get("account_id")
    if account_id is not None and (
        not isinstance(account_id, str) or not account_id.strip()
    ):
        raise ConnectorError(
            "Plaid removed transaction account_id must be text when supplied",
            code="provider_invalid_response",
            provider="plaid",
        )
    return {
        "providerId": provider_id.strip(),
        "providerAccountId": account_id.strip() if isinstance(account_id, str) else None,
    }


def _latest_event(connection: Any, workspace_id: str, provider_id: str) -> Any:
    return connection.execute(
        """
        SELECT * FROM provider_transaction_events
        WHERE workspace_id = ? AND provider = 'plaid'
          AND (
            provider_transaction_id = ?
            OR provider_transaction_id LIKE ?
          )
        ORDER BY recorded_at DESC, event_id DESC
        LIMIT 1
        """,
        (workspace_id, provider_id, f"%:{provider_id}"),
    ).fetchone()


def record_plaid_event_batch(
    store: SQLiteStore,
    *,
    workspace_id: str,
    accounts: tuple[PlaidAccount, ...],
    added: tuple[PlaidTransaction, ...],
    modified: tuple[PlaidTransaction, ...],
    removed: tuple[Mapping[str, object], ...],
    synced_at: str,
    mapping_version: str,
) -> PlaidEventBatchResult:
    removed_payloads = tuple(_removed_payload(item) for item in removed)
    batch_payload = {
        "provider": "plaid",
        "mappingVersion": mapping_version,
        "accounts": [
            {
                "providerId": account.provider_id,
                "accountId": account.account_id,
                "currency": account.currency,
            }
            for account in accounts
        ],
        "added": [_transaction_payload(item) for item in added],
        "modified": [_transaction_payload(item) for item in modified],
        "removed": list(removed_payloads),
    }
    encoded_batch = canonical_json(batch_payload)
    digest = hashlib.sha256(encoded_batch.encode()).hexdigest()
    source_item_id = _stable_id("src", "plaid_sync", digest)
    account_by_provider_id = {account.provider_id: account for account in accounts}

    with store.transaction() as connection:
        existing = connection.execute(
            """
            SELECT source_item_id FROM source_items
            WHERE workspace_id = ? AND digest = ? AND mapping_version = ?
            """,
            (workspace_id, digest, mapping_version),
        ).fetchone()
        if existing is not None:
            return PlaidEventBatchResult(
                source_item_id=str(existing["source_item_id"]),
                digest=digest,
                status="deduplicated",
                event_count=0,
                added_count=0,
                modified_count=0,
                removed_count=0,
            )

        change_count = len(added) + len(modified) + len(removed_payloads)
        connection.execute(
            """
            INSERT INTO source_items(
                source_item_id, workspace_id, source_type, label, digest,
                mapping_version, received_at, status, row_count
            ) VALUES (?, ?, 'plaid_fixture', ?, ?, ?, ?, 'processed', ?)
            """,
            (
                source_item_id,
                workspace_id,
                "Plaid read-only transaction events",
                digest,
                mapping_version,
                synced_at,
                change_count,
            ),
        )

        def append_transaction_event(
            transaction: PlaidTransaction,
            *,
            event_type: str,
        ) -> None:
            prior = _latest_event(connection, workspace_id, transaction.provider_id)
            supersedes = str(prior["event_id"]) if prior is not None else None
            provider_transaction_id = transaction.external_reference
            event_id = _stable_id(
                "prevt",
                "plaid",
                event_type,
                provider_transaction_id,
                digest,
            )
            payload = {
                **_transaction_payload(transaction),
                "ledgerDisposition": "quarantined_currency_mismatch",
            }
            connection.execute(
                """
                INSERT INTO provider_transaction_events(
                    event_id, workspace_id, provider, provider_account_id,
                    provider_transaction_id, source_item_id, event_type, occurred_on,
                    description, amount_minor, currency, payload_json,
                    supersedes_event_id, recorded_at
                ) VALUES (?, ?, 'plaid', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    workspace_id,
                    transaction.account_id,
                    provider_transaction_id,
                    source_item_id,
                    event_type,
                    transaction.occurred_on,
                    transaction.description,
                    transaction.amount_minor,
                    transaction.currency,
                    canonical_json(payload),
                    supersedes,
                    synced_at,
                ),
            )

        for transaction in added:
            append_transaction_event(transaction, event_type="quarantined")
        for transaction in modified:
            append_transaction_event(transaction, event_type="modified")

        for payload in removed_payloads:
            provider_id = str(payload["providerId"])
            prior = _latest_event(connection, workspace_id, provider_id)
            raw_account_id = payload.get("providerAccountId")
            account = (
                account_by_provider_id.get(str(raw_account_id))
                if raw_account_id is not None
                else None
            )
            provider_account_id = (
                str(prior["provider_account_id"])
                if prior is not None
                else account.account_id if account is not None else "acct_plaid_unknown"
            )
            provider_transaction_id = (
                str(prior["provider_transaction_id"])
                if prior is not None
                else provider_id
            )
            description = (
                str(prior["description"])
                if prior is not None
                else "Removed Plaid transaction"
            )
            amount_minor = prior["amount_minor"] if prior is not None else None
            currency = (
                str(prior["currency"])
                if prior is not None
                else account.currency if account is not None else "USD"
            )
            supersedes = str(prior["event_id"]) if prior is not None else None
            event_id = _stable_id(
                "prevt",
                "plaid",
                "removed",
                provider_transaction_id,
                digest,
            )
            connection.execute(
                """
                INSERT INTO provider_transaction_events(
                    event_id, workspace_id, provider, provider_account_id,
                    provider_transaction_id, source_item_id, event_type, occurred_on,
                    description, amount_minor, currency, payload_json,
                    supersedes_event_id, recorded_at
                ) VALUES (?, ?, 'plaid', ?, ?, ?, 'removed', NULL, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    workspace_id,
                    provider_account_id,
                    provider_transaction_id,
                    source_item_id,
                    description,
                    amount_minor,
                    currency,
                    canonical_json(
                        {
                            **payload,
                            "ledgerDisposition": "provider_tombstone",
                        }
                    ),
                    supersedes,
                    synced_at,
                ),
            )

    return PlaidEventBatchResult(
        source_item_id=source_item_id,
        digest=digest,
        status="recorded",
        event_count=len(added) + len(modified) + len(removed_payloads),
        added_count=len(added),
        modified_count=len(modified),
        removed_count=len(removed_payloads),
    )
