"""Small, shared boundaries for commands and untrusted conversational context."""

from __future__ import annotations

import json
import re

_STOP_COMMAND = re.compile(
    r"(?:^|[.!?]\s+)(?:please\s+)?"
    r"(?:(?:stop|pause)(?:\s+(?:here|now|for now|asking(?: questions)?))?"
    r"|leave it there|that(?:'s| is) enough|done for now|synthesi[sz]e)"
    r"\s*(?:[.!?,]|$)",
    re.IGNORECASE,
)


def is_stop_command(content: str) -> bool:
    """A financial question containing 'stop' is not a stop command."""
    return bool(_STOP_COMMAND.search(content.strip()))


def bounded_text(content: str, limit: int) -> str:
    """Preserve the material tail as well as the opening; never exceed the cap."""
    if limit < 64:
        raise ValueError("text budget must be at least 64 characters")
    if len(content) <= limit:
        return content
    marker = " [middle omitted; full statement retained locally] "
    available = limit - len(marker)
    head = available // 2
    return content[:head] + marker + content[-(available - head) :]


def local_conversation_context(packet: str | None) -> dict[str, object]:
    """Keep a bounded local packet separate from canonical financial references."""
    if not packet or len(packet) > 6000:
        return {}
    try:
        value = json.loads(packet)
    except ValueError:
        return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in ("dialogue", "claims", "recentTurnsUntrusted", "workingUnderstanding")
        if key in value
    }
