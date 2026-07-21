"""External connector package (Task 2)."""

from finance_agent.connectors.akahu import (
    AkahuAccount,
    AkahuConfig,
    AkahuReadOnlyAdapter,
    AkahuTransaction,
)
from finance_agent.connectors.akahu import (
    normalise_accounts as normalise_akahu_accounts,
)
from finance_agent.connectors.akahu import (
    normalise_transactions as normalise_akahu_transactions,
)
from finance_agent.connectors.akahu_fixture import (
    AkahuFixtureIngestor,
    AkahuFixtureResult,
)
from finance_agent.connectors.plaid import (
    PlaidAccount,
    PlaidConfig,
    PlaidReadOnlyAdapter,
    PlaidTransaction,
)
from finance_agent.connectors.plaid import (
    normalise_accounts as normalise_plaid_accounts,
)
from finance_agent.connectors.plaid import (
    normalise_transactions as normalise_plaid_transactions,
)
from finance_agent.connectors.plaid_fixture import (
    PlaidFixtureIngestor,
    PlaidFixtureResult,
)
from finance_agent.connectors.telegram import (
    TelegramBotAdapter,
    TelegramConfig,
    TelegramFixtureIngestor,
)

# Preserve the historical Akahu-oriented names used by services and tests.
normalise_accounts = normalise_akahu_accounts
normalise_transactions = normalise_akahu_transactions

__all__ = [
    "AkahuConfig",
    "AkahuAccount",
    "AkahuFixtureIngestor",
    "AkahuFixtureResult",
    "AkahuReadOnlyAdapter",
    "AkahuTransaction",
    "PlaidConfig",
    "PlaidAccount",
    "PlaidFixtureIngestor",
    "PlaidFixtureResult",
    "PlaidReadOnlyAdapter",
    "PlaidTransaction",
    "TelegramBotAdapter",
    "TelegramConfig",
    "TelegramFixtureIngestor",
    "normalise_accounts",
    "normalise_transactions",
    "normalise_akahu_accounts",
    "normalise_akahu_transactions",
    "normalise_plaid_accounts",
    "normalise_plaid_transactions",
]
