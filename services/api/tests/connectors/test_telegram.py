from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest

from finance_agent.connectors.base import ConnectorError
from finance_agent.connectors.telegram import TelegramConfig, TelegramFixtureIngestor

ROOT = Path(__file__).parents[4]


def fixture(name: str) -> dict[str, object]:
    return json.loads((ROOT / "fixtures" / "demo" / name).read_text())


def test_recorded_update_is_allowlisted_bounded_redacted_and_deduplicated() -> None:
    update = fixture("telegram-update.json")
    attachment = fixture("telegram-attachment-reference.json")
    modified = deepcopy(update)
    modified["message"]["caption"] += " Bearer secret-token-value-123456"  # type: ignore[index]
    ingestor = TelegramFixtureIngestor(TelegramConfig(allowed_chat_id=700001))
    first = ingestor.ingest(modified, attachment)
    assert first.status == "ingested"
    assert first.source is not None
    assert first.source.source_item_id == "src_koru_telegram_910001"
    assert "secret-token" not in first.source.text
    assert "[REDACTED]" in first.source.text
    assert ingestor.ingest(modified, attachment).status == "deduplicated"


def test_disallowed_chat_fails_closed() -> None:
    ingestor = TelegramFixtureIngestor(TelegramConfig(allowed_chat_id=42))
    with pytest.raises(ConnectorError, match="allowlisted"):
        ingestor.ingest(
            fixture("telegram-update.json"), fixture("telegram-attachment-reference.json")
        )
