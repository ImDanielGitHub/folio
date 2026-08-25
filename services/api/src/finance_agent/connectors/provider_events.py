"""Immutable provider transaction lifecycle persistence."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from finance_agent.connectors.plaid import (
    PlaidRemovedTransaction,
    PlaidTransaction,
)
from finance_agent.storage import SQLiteStore, canonical_json


@dataclass(frozen=True, slots=True)
class ProviderEventCommit:
    source_item_id: str | None
    status: str
    source_sha256: str | None
    event_count: int
    added_count: int
    modified_count: int
    removed_count: int


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _latest_event(
    connection: Any,
    *,
    workspace_id: str,
    provider_transaction_id: str,
) -> Any:
    return connection.execute(
        """
        SELECT *
        FROM provider_transaction_events
        WHERE workspace_id = ? AND provider = 'plaid'
          AND (
            provider_transaction_id = ?
            OR provider_transaction_id LIKE ?
          )
        ORDER BY recorded_at DESC, event_id DESC
        LIMIT 1
        """,
        (workspace_id, provider_transaction_id, f"%:{provider_transaction_id}"),
    ).fetchone()


def _transaction_changed(previous: Any, transaction: PlaidTransaction) -> bool:
    if previous is None or str(previous["event_type"]) == "removed":
        return True
    return (
        str(previous["provider_account_id"]) != transaction.account_id
        or str(previous["occurred_on"] or "") != transaction.occurred_on
        or str(previous["description"]) != transaction.description
        or int(previous["amount_minor"]) != transaction.amount_minor
        or str(previous["currency"]) != transaction.currency
    )


def record_plaid_provider_events(
    store: SQLiteStore,
    *,
    workspace_id: str,
    account_label: str,
    default_account_id: str,
    default_currency: str,
    added: Sequence[PlaidTransaction],
    modified: Sequence[PlaidTransaction],
    removed: Sequence[PlaidRemovedTransaction],
    recorded_at: str,
    mapping_version: str,
) -> ProviderEventCommit:
    """Append provider corrections and tombstones without mutating prior evidence."""

    planned: list[dict[str, object]] = []
    with store.transaction() as connection:
        for lifecycle_type, transactions in (("added", added), ("modified", modified)):
            for transaction in transactions:
                previous = _latest_event(
                    connection,
                    workspace_id=workspace_id,
                    provider_transaction_id=transaction.provider_id,
                )
                if not _transaction_changed(previous, transaction):
                    continue
                planned.append(
                    {
                        "lifecycleType": lifecycle_type,
                        "eventType": (
                            "quarantined" if lifecycle_type == "added" else "modified"
                        ),
                        "providerTransactionId": transaction.provider_id,
                        "providerAccountId": transaction.account_id,
                        "occurredOn": transaction.occurred_on,
                        "description": transaction.description,
                        "amountMinor": transaction.amount_minor,
                        "currency": transaction.currency,
                        "supersedesEventId": (
                            str(previous["event_id"]) if previous is not None else None
                        ),
                    }
                )

        for tombstone in removed:
            previous = _latest_event(
                connection,
                workspace_id=workspace_id,
                provider_transaction_id=tombstone.provider_id,
            )
            if previous is not None and str(previous["event_type"]) == "removed":
                continue
            planned.append(
                {
                    "lifecycleType": "removed",
                    "eventType": "removed",
                    "providerTransactionId": tombstone.provider_id,
                    "providerAccountId": (
                        str(previous["provider_account_id"])
                        if previous is not None
                        else default_account_id
                    ),
                    "occurredOn": (
                        str(previous["occurred_on"])
                        if previous is not None and previous["occurred_on"] is not None
                        else None
                    ),
                    "description": (
                        str(previous["description"])
                        if previous is not None
                        else "Removed Plaid transaction"
                    ),
                    "amountMinor": (
                        int(previous["amount_minor"])
                        if previous is not None and previous["amount_minor"] is not None
                        else None
                    ),
                    "currency": (
                        str(previous["currency"])
                        if previous is not None
                        else default_currency
                    ),
                    "supersedesEventId": (
                        str(previous["event_id"]) if previous is not None else None
                    ),
                }
            )

        if not planned:
            return ProviderEventCommit(
                source_item_id=None,
                status="no_new_transactions",
                source_sha256=None,
                event_count=0,
                added_count=0,
                modified_count=0,
                removed_count=0,
            )

        source_payload = {
            "provider": "plaid",
            "mappingVersion": mapping_version,
            "events": planned,
        }
        digest = hashlib.sha256(canonical_json(source_payload).encode()).hexdigest()
        source_item_id = _stable_id("src", "plaid_lifecycle", digest)
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
                f"{account_label} provider lifecycle",
                digest,
                mapping_version,
                recorded_at,
                len(planned),
            ),
        )

        for value in planned:
            provider_transaction_id = str(value["providerTransactionId"])
            event_type = str(value["eventType"])
            event_id = _stable_id(
                "prevt",
                "plaid",
                provider_transaction_id,
                event_type,
                digest,
            )
            payload = {
                "lifecycleType": value["lifecycleType"],
                "providerCurrency": value["currency"],
                "reason": "workspace_currency_mismatch",
                "providerTransactionId": provider_transaction_id,
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
                    value["providerAccountId"],
                    provider_transaction_id,
                    source_item_id,
                    event_type,
                    value["occurredOn"],
                    value["description"],
                    value["amountMinor"],
                    value["currency"],
                    canonical_json(payload),
                    value["supersedesEventId"],
                    recorded_at,
                ),
            )

    added_count = sum(value["lifecycleType"] == "added" for value in planned)
    modified_count = sum(value["lifecycleType"] == "modified" for value in planned)
    removed_count = sum(value["lifecycleType"] == "removed" for value in planned)
    return ProviderEventCommit(
        source_item_id=source_item_id,
        status=(
            "quarantined_currency_mismatch"
            if added_count and not modified_count and not removed_count
            else "provider_events_recorded"
        ),
        source_sha256=digest,
        event_count=len(planned),
        added_count=added_count,
        modified_count=modified_count,
        removed_count=removed_count,
    )
