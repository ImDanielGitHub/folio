from __future__ import annotations

import json
from datetime import UTC, date, datetime

from finance_agent.agent.dialogue import (
    ClaimBasis,
    ClaimScope,
    ContextAssembler,
    DialogueFrame,
    TemporalClaim,
    TranscriptTurn,
)


def _frame(*, claims: tuple[TemporalClaim, ...] = ()) -> DialogueFrame:
    return DialogueFrame(
        frame_id="frame_context_eval",
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        updated_at=datetime.now(UTC),
        current_intent="Understand the owner's business context.",
        claims=claims,
    )


def _turn(content: str) -> TranscriptTurn:
    return TranscriptTurn(
        turn_id="turn_context_eval",
        role="owner",
        content=content,
        occurred_at=datetime.now(UTC),
        mode="local",
    )


def test_long_owner_answer_preserves_material_tail() -> None:
    material_tail = "MATERIAL_TAIL: Waitangi invoice is still provisional."
    content = ("Earlier operating context. " * 100) + material_tail

    encoded = ContextAssembler().assemble(_frame(), (_turn(content),), {})
    packet = json.loads(encoded)

    assert material_tail in packet["recentTurnsUntrusted"][0]["content"]
    assert "earlier detail compacted" in packet["recentTurnsUntrusted"][0]["content"]
    assert len(encoded) <= 6000


def test_working_understanding_is_bounded_without_mutating_provider_result() -> None:
    entries = [{"factId": f"fact_{index}", "statement": "context " * 120} for index in range(8)]
    working = {"revision": 3, "contentHash": "a" * 64, "entries": entries}

    encoded = ContextAssembler(max_characters=2600).assemble(
        _frame(),
        (_turn("What do we know about the reserve policy?"),),
        {"aggregate_amounts": {"protectedReserveMinor": 200000}},
        working_understanding=working,
    )
    packet = json.loads(encoded)

    assert len(encoded) <= 2600
    assert 0 < len(packet["workingUnderstanding"]["entries"]) < len(entries)
    assert len(entries) == 8


def test_many_long_claims_compact_instead_of_failing_the_turn() -> None:
    claims = tuple(
        TemporalClaim(
            claim_id=f"claim_{index}",
            claim_type="business_context",
            statement=(f"Context {index}. " * 90),
            source_turn_id=f"turn_{index}",
            scope=ClaimScope(workspace_id="ws_koru_studio"),
            basis=ClaimBasis.EXPLICIT,
            effective_from=date(2026, 7, 1),
            effective_until=None,
            confidence=1.0,
            recorded_at=datetime.now(UTC),
        )
        for index in range(5)
    )

    encoded = ContextAssembler().assemble(
        _frame(claims=claims),
        (_turn("Continue from the broader picture."),),
        {"aggregate_amounts": {"currentBalanceMinor": 504576}},
    )
    packet = json.loads(encoded)

    assert len(encoded) <= 6000
    assert 1 <= len(packet["claims"]) < len(claims)
