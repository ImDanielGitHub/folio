"""Ask one short business-profile question when tools cannot answer from memory.

Bionic-inspired: clarifying questions only when working understanding is thin —
not form theatre, not approval cards. One question at a time.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from finance_agent.agent.dialogue import ActiveQuestion

_GENERAL_BUSINESS_RE = re.compile(
    r"\b("
    r"tell me about (?:the |my |our )?business|"
    r"what do you know|"
    r"who (?:are|is) (?:we|you|koru)|"
    r"what (?:do|does) (?:we|koru|the business)|"
    r"where (?:do|does) (?:we|koru|the business)|"
    r"why (?:do|does) (?:we|koru)|"
    r"how does (?:the )?business|"
    r"working understanding|"
    r"business profile|"
    r"get to know"
    r")\b",
    re.IGNORECASE,
)

_EXPLICIT_MARKERS = frozenset({"explicit", "owner", "owner_statement", "owner_claim"})

# Prefer gaps tools cannot fill from the bank feed alone.
_GAP_PROMPTS: tuple[tuple[str, str, str], ...] = (
    (
        "who",
        "who",
        "Who usually makes the day-to-day spending calls for your business?",
    ),
    (
        "what",
        "what",
        "What kind of work is your business mostly taking on right now?",
    ),
    (
        "where",
        "where",
        "Where does most client work happen — studio, on site, or remote?",
    ),
    (
        "when",
        "when",
        "When do client invoices usually land in a typical month?",
    ),
    (
        "why",
        "why",
        "What are you mainly protecting the cash reserve for?",
    ),
)


@dataclass(frozen=True, slots=True)
class DiscoveryDecision:
    question: ActiveQuestion | None
    reason: str
    thin: bool


def looks_like_business_discovery(content: str) -> bool:
    return bool(_GENERAL_BUSINESS_RE.search(content))


def _entry_axis(entry: Mapping[str, object]) -> str | None:
    for key in ("axis", "questionAxis", "question_axis"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().casefold()
    return None


def _entry_basis(entry: Mapping[str, object]) -> str:
    for key in ("basis", "sourceKind", "recordType", "status"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().casefold()
    source = entry.get("source")
    if isinstance(source, Mapping):
        kind = source.get("kind")
        if isinstance(kind, str):
            return kind.strip().casefold()
    return ""


def _explicit_axes(working_understanding: Mapping[str, object] | None) -> set[str]:
    if not working_understanding:
        return set()
    axes: set[str] = set()
    for key in ("entries", "hits", "retrievedFacts"):
        entries = working_understanding.get(key)
        if not isinstance(entries, list):
            continue
        for raw in entries:
            if not isinstance(raw, Mapping):
                continue
            axis = _entry_axis(raw)
            basis = _entry_basis(raw)
            if axis and any(marker in basis for marker in _EXPLICIT_MARKERS):
                axes.add(axis)
            # Owner statements without an axis still count as profile signal.
            title = str(raw.get("title") or raw.get("predicate") or "").casefold()
            excerpt = str(raw.get("excerpt") or raw.get("objectText") or "").casefold()
            if "owner" in basis or raw.get("recordType") == "owner_statement":
                for axis_name, *_ in _GAP_PROMPTS:
                    if axis_name in title or axis_name in excerpt:
                        axes.add(axis_name)
    return axes


def _total_by_axis(working_understanding: Mapping[str, object] | None) -> Mapping[str, int]:
    if not working_understanding:
        return {}
    # context_for does not always embed summary totals; tolerate either shape.
    for key in ("totalByAxis", "totalsByAxis"):
        value = working_understanding.get(key)
        if isinstance(value, Mapping):
            return {
                str(axis).casefold(): int(count)
                for axis, count in value.items()
                if isinstance(count, int)
            }
    return {}


def profile_is_thin(working_understanding: Mapping[str, object] | None) -> bool:
    """True when owner-authored profile coverage is sparse across 5W axes."""

    if not working_understanding:
        return True
    owner_count = working_understanding.get("ownerStatementCount")
    if isinstance(owner_count, int) and owner_count >= 4:
        return False
    explicit = _explicit_axes(working_understanding)
    if len(explicit) >= 3:
        return False
    # One or two owner claims (e.g. a single MITRE explanation) is still a thin profile.
    if isinstance(owner_count, int) and owner_count <= 2 and len(explicit) < 3:
        return True
    totals = _total_by_axis(working_understanding)
    if not explicit and (not totals or sum(totals.values()) <= 8):
        return True
    return len(explicit) < 2


def next_discovery_prompt(
    working_understanding: Mapping[str, object] | None,
    *,
    asked_prompts: Sequence[str] = (),
) -> tuple[str, str] | None:
    """Return (axis, prompt) for the highest-value unanswered profile gap."""

    asked_blobs = tuple(
        text.strip().casefold() for text in asked_prompts if isinstance(text, str) and text.strip()
    )
    explicit = _explicit_axes(working_understanding)
    for axis, _label, prompt in _GAP_PROMPTS:
        if axis in explicit:
            continue
        needle = prompt.casefold()
        if any(needle in blob for blob in asked_blobs):
            continue
        return axis, prompt
    return None


def decide_business_discovery(
    *,
    content: str,
    working_understanding: Mapping[str, object] | None,
    has_active_question: bool,
    had_committed_write: bool,
    plan_was_read: bool,
    asked_at: datetime,
    question_id: str,
    asked_prompts: Sequence[str] = (),
    just_answered_question: bool = False,
) -> DiscoveryDecision:
    """Choose at most one clarifying business question.

    Prefer asking when:
    - the owner asked a general business question tools cannot fully answer, or
    - a read just finished and the owner profile is still thin.
    Never ask during/after a write, when a question is already open, or on the
    immediate turn that answered the previous question.
    """

    if has_active_question or had_committed_write or just_answered_question:
        return DiscoveryDecision(question=None, reason="blocked", thin=False)

    thin = profile_is_thin(working_understanding)
    general = looks_like_business_discovery(content)
    if not thin and not general:
        return DiscoveryDecision(question=None, reason="profile_sufficient", thin=False)
    if not general and not plan_was_read:
        return DiscoveryDecision(question=None, reason="not_read_turn", thin=thin)

    gap = next_discovery_prompt(working_understanding, asked_prompts=asked_prompts)
    if gap is None:
        return DiscoveryDecision(question=None, reason="no_gaps", thin=thin)

    axis, prompt = gap
    question = ActiveQuestion(
        question_id=question_id,
        prompt=prompt,
        reason=f"Working understanding is thin on the {axis} axis; tools cannot invent this.",
        asked_at=asked_at,
    )
    return DiscoveryDecision(
        question=question,
        reason="thin_profile" if thin else "general_business_query",
        thin=thin,
    )


__all__ = [
    "DiscoveryDecision",
    "decide_business_discovery",
    "looks_like_business_discovery",
    "next_discovery_prompt",
    "profile_is_thin",
]
