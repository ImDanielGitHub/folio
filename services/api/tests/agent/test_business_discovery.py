"""Business discovery asks one clarifying profile question when memory is thin."""

from __future__ import annotations

from datetime import UTC, datetime

from finance_agent.agent.business_discovery import (
    decide_business_discovery,
    looks_like_business_discovery,
    profile_is_thin,
)


def test_general_business_query_detected() -> None:
    assert looks_like_business_discovery("What do you know about the business so far?")
    assert not looks_like_business_discovery("Show me the MITRE 10 transaction")


def test_thin_profile_when_owner_statements_sparse() -> None:
    assert profile_is_thin({"ownerStatementCount": 1, "entries": [], "totalByAxis": {"who": 1}})
    assert not profile_is_thin(
        {
            "ownerStatementCount": 5,
            "entries": [
                {"axis": "who", "basis": "explicit"},
                {"axis": "what", "basis": "explicit"},
                {"axis": "where", "basis": "explicit"},
            ],
        }
    )


def test_discovery_asks_one_question_after_thin_read() -> None:
    decision = decide_business_discovery(
        content="What needs my attention today?",
        working_understanding={"ownerStatementCount": 1, "entries": []},
        has_active_question=False,
        had_committed_write=False,
        plan_was_read=True,
        asked_at=datetime.now(UTC),
        question_id="question_test_discovery",
    )
    assert decision.thin is True
    assert decision.question is not None
    assert "spending" in decision.question.prompt.lower() or "work" in decision.question.prompt.lower()


def test_discovery_skips_writes_and_open_questions() -> None:
    blocked_write = decide_business_discovery(
        content="What do you know about the business?",
        working_understanding={"ownerStatementCount": 0, "entries": []},
        has_active_question=False,
        had_committed_write=True,
        plan_was_read=False,
        asked_at=datetime.now(UTC),
        question_id="question_blocked",
    )
    blocked_open = decide_business_discovery(
        content="What do you know about the business?",
        working_understanding={"ownerStatementCount": 0, "entries": []},
        has_active_question=True,
        had_committed_write=False,
        plan_was_read=False,
        asked_at=datetime.now(UTC),
        question_id="question_blocked_open",
    )
    assert blocked_write.question is None
    assert blocked_open.question is None
