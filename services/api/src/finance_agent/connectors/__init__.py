"""External connector package (Task 2)."""

from finance_agent.connectors.akahu import AkahuConfig, AkahuReadOnlyAdapter
from finance_agent.connectors.telegram import (
    TelegramBotAdapter,
    TelegramConfig,
    TelegramFixtureIngestor,
)

__all__ = [
    "AkahuConfig",
    "AkahuReadOnlyAdapter",
    "TelegramBotAdapter",
    "TelegramConfig",
    "TelegramFixtureIngestor",
]
