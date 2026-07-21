"""Deterministic Akahu-shaped fixture ingestion for the Koru demo.

This module deliberately performs no network I/O. Live Akahu reads remain behind
``AkahuReadOnlyAdapter`` and require explicit local configuration.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from finance_agent.connectors.base import ConnectorError

REPO_FIXTURE_CANDIDATES = (
    Path("fixtures/demo/akahu-sync.json"),
    Path(__file__).resolve().parents[5] / "fixtures" / "demo" / "akahu-sync.json",
)


@dataclass(frozen=True, slots=True)
class AkahuFixtureTransaction:
    occurred_on: str
    description: str
    amount_minor: int
    external_reference: str
    status: str


@dataclass(frozen=True, slots=True)
class AkahuFixtureResult:
    source_item_id: str
    status: str
    account_label: str
    synced_at: str
    row_count: int
    digest: str
    transactions: tuple[AkahuFixtureTransaction, ...]
    live_sync_attempted: bool = False


def _load_default_payload() -> Mapping[str, Any]:
    for candidate in REPO_FIXTURE_CANDIDATES:
        if candidate.is_file():
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(loaded, Mapping):
                return loaded
    raise ConnectorError("Akahu demo fixture file is missing")


def _parse_transactions(
    raw: Sequence[Mapping[str, Any]],
) -> tuple[AkahuFixtureTransaction, ...]:
    parsed: list[AkahuFixtureTransaction] = []
    for index, item in enumerate(raw, start=1):
        occurred_on = item.get("occurredOn") or item.get("occurred_on")
        description = item.get("description")
        amount = item.get("amountMinor")
        if amount is None:
            amount = item.get("amount_minor")
        external = item.get("externalReference") or item.get("external_reference")
        status = item.get("status", "posted")
        if not isinstance(occurred_on, str) or not occurred_on:
            raise ConnectorError(f"Akahu fixture row {index}: occurredOn required")
        if not isinstance(description, str) or not description.strip():
            raise ConnectorError(f"Akahu fixture row {index}: description required")
        if not isinstance(amount, int):
            raise ConnectorError(f"Akahu fixture row {index}: amountMinor must be int")
        if not isinstance(external, str) or not external.strip():
            raise ConnectorError(f"Akahu fixture row {index}: externalReference required")
        if status not in {"posted", "pending"}:
            raise ConnectorError(f"Akahu fixture row {index}: status must be posted|pending")
        try:
            datetime.strptime(occurred_on, "%Y-%m-%d")
        except ValueError as error:
            raise ConnectorError(
                f"Akahu fixture row {index}: occurredOn must be YYYY-MM-DD"
            ) from error
        parsed.append(
            AkahuFixtureTransaction(
                occurred_on=occurred_on,
                description=description.strip(),
                amount_minor=amount,
                external_reference=external.strip(),
                status=status,
            )
        )
    if not parsed:
        raise ConnectorError("Akahu fixture must include at least one transaction")
    if len(parsed) > 200:
        raise ConnectorError("Akahu fixture exceeds the 200-row demo bound")
    return tuple(parsed)


class AkahuFixtureIngestor:
    """Parse and validate a sealed Akahu-shaped sync payload."""

    SOURCE_ITEM_ID = "src_koru_akahu_anz_everyday"
    MAPPING_VERSION = "akahu_fixture@1"

    def ingest(
        self,
        payload: Mapping[str, Any] | str | Path | None = None,
        *,
        source_item_id: str | None = None,
    ) -> AkahuFixtureResult:
        if payload is None:
            body = dict(_load_default_payload())
        elif isinstance(payload, (str, Path)):
            path = Path(payload)
            if not path.is_file():
                raise ConnectorError(f"Akahu fixture file missing: {path}")
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, Mapping):
                raise ConnectorError("Akahu fixture file must contain a JSON object")
            body = dict(loaded)
        else:
            body = dict(payload)

        account = body.get("account")
        if not isinstance(account, Mapping):
            raise ConnectorError("Akahu fixture requires an account object")
        name = account.get("name")
        masked = account.get("maskedNumber") or account.get("masked_number") or ""
        currency = account.get("currency", "NZD")
        if not isinstance(name, str) or not name.strip():
            raise ConnectorError("Akahu account name is required")
        if currency != "NZD":
            raise ConnectorError("Akahu fixture only supports NZD")

        synced_at = body.get("syncedAt") or body.get("synced_at")
        if not isinstance(synced_at, str) or not synced_at:
            synced_at = datetime.now(UTC).isoformat()
        raw_transactions = body.get("transactions")
        if not isinstance(raw_transactions, list):
            raise ConnectorError("Akahu fixture requires a transactions array")
        transactions = _parse_transactions(raw_transactions)
        digest_payload = {
            "account": {"name": name, "maskedNumber": masked, "currency": currency},
            "syncedAt": synced_at,
            "transactions": [
                {
                    "occurredOn": transaction.occurred_on,
                    "description": transaction.description,
                    "amountMinor": transaction.amount_minor,
                    "externalReference": transaction.external_reference,
                    "status": transaction.status,
                }
                for transaction in transactions
            ],
        }
        digest = hashlib.sha256(
            json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        label = f"Akahu · {name.strip()}"
        if isinstance(masked, str) and masked:
            label = f"{label} · {masked}"
        return AkahuFixtureResult(
            source_item_id=source_item_id or self.SOURCE_ITEM_ID,
            status="ingested",
            account_label=label,
            synced_at=synced_at,
            row_count=len(transactions),
            digest=digest,
            transactions=transactions,
            live_sync_attempted=False,
        )
