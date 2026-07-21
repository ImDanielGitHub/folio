"""External connector package (Task 2)."""

from finance_agent.connectors.akahu import (
    AkahuAccount,
    AkahuConfig,
    AkahuReadOnlyAdapter,
    AkahuTransaction,
    normalise_accounts,
    normalise_transactions,
)
from finance_agent.connectors.akahu_fixture import (
    AkahuFixtureIngestor,
    AkahuFixtureResult,
)
from finance_agent.connectors.telegram import (
    TelegramBotAdapter,
    TelegramConfig,
    TelegramFixtureIngestor,
)

__all__ = [
    "AkahuConfig",
    "AkahuAccount",
    "AkahuFixtureIngestor",
    "AkahuFixtureResult",
    "AkahuReadOnlyAdapter",
    "AkahuTransaction",
    "TelegramBotAdapter",
    "TelegramConfig",
    "TelegramFixtureIngestor",
    "normalise_accounts",
    "normalise_transactions",
]
