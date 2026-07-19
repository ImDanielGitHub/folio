"""External connector package (Task 2).

Imports are lazy so offline fixture paths do not pull httpx/network stacks
at package import time.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AkahuConfig",
    "AkahuFixtureIngestor",
    "AkahuFixtureResult",
    "AkahuReadOnlyAdapter",
    "TelegramBotAdapter",
    "TelegramConfig",
    "TelegramFixtureIngestor",
]


def __getattr__(name: str) -> Any:
    if name in {"AkahuFixtureIngestor", "AkahuFixtureResult"}:
        from finance_agent.connectors.akahu_fixture import (
            AkahuFixtureIngestor,
            AkahuFixtureResult,
        )

        return {
            "AkahuFixtureIngestor": AkahuFixtureIngestor,
            "AkahuFixtureResult": AkahuFixtureResult,
        }[name]
    if name in {"AkahuConfig", "AkahuReadOnlyAdapter"}:
        from finance_agent.connectors.akahu import AkahuConfig, AkahuReadOnlyAdapter

        return {
            "AkahuConfig": AkahuConfig,
            "AkahuReadOnlyAdapter": AkahuReadOnlyAdapter,
        }[name]
    if name in {"TelegramBotAdapter", "TelegramConfig", "TelegramFixtureIngestor"}:
        from finance_agent.connectors.telegram import (
            TelegramBotAdapter,
            TelegramConfig,
            TelegramFixtureIngestor,
        )

        return {
            "TelegramBotAdapter": TelegramBotAdapter,
            "TelegramConfig": TelegramConfig,
            "TelegramFixtureIngestor": TelegramFixtureIngestor,
        }[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
