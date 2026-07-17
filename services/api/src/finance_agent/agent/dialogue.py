"""Provider-independent transcript, DialogueFrame and temporal claim state."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Protocol


class ClaimBasis(StrEnum):
    EXPLICIT = "explicit"
    INFERRED = "inferred"
    HYPOTHETICAL = "hypothetical"


class ClaimStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    RETRACTED = "retracted"


@dataclass(frozen=True, slots=True)
class ClaimScope:
    merchant_contains: str | None = None
    maximum_amount_minor: int | None = None
    currency: str | None = None
    workspace_id: str | None = None


@dataclass(frozen=True, slots=True)
class TemporalClaim:
    claim_id: str
    claim_type: str
    statement: str
    source_turn_id: str
    scope: ClaimScope
    basis: ClaimBasis
    effective_from: date
    effective_until: date | None
    confidence: float
    recorded_at: datetime
    status: ClaimStatus = ClaimStatus.ACTIVE
    supersedes_claim_id: str | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("claim confidence must be between 0 and 1")
        if self.basis is ClaimBasis.EXPLICIT and self.confidence < 0.5:
            raise ValueError("an explicit owner claim cannot have very low confidence")
        if self.effective_until and self.effective_until < self.effective_from:
            raise ValueError("effective_until cannot precede effective_from")

    def to_contract(self) -> dict[str, object]:
        """Project richer internal claims into the frozen DialogueFrame@1 shape."""

        return {
            "claimId": self.claim_id,
            "claimType": self.claim_type,
            "statement": self.statement,
            "sourceTurnId": self.source_turn_id,
            "scope": {
                "merchantContains": self.scope.merchant_contains,
                "maximumAmountMinor": self.scope.maximum_amount_minor,
                "currency": self.scope.currency,
            },
            "effectiveDate": self.effective_from.isoformat(),
            "recordedAt": self.recorded_at.isoformat(),
            "status": self.status.value,
            "supersedesClaimId": self.supersedes_claim_id,
        }


@dataclass(frozen=True, slots=True)
class ActiveQuestion:
    question_id: str
    prompt: str
    reason: str
    asked_at: datetime

    def to_contract(self) -> dict[str, object]:
        return {
            "questionId": self.question_id,
            "prompt": self.prompt,
            "reason": self.reason,
            "askedAt": self.asked_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class DialogueFrame:
    frame_id: str
    workspace_id: str
    thread_id: str
    updated_at: datetime
    current_intent: str
    active_question: ActiveQuestion | None = None
    claims: tuple[TemporalClaim, ...] = ()
    active_scenario_id: str | None = None
    stopped: bool = False

    def to_contract(self) -> dict[str, object]:
        return {
            "frameVersion": "DialogueFrame@1",
            "frameId": self.frame_id,
            "workspaceId": self.workspace_id,
            "threadId": self.thread_id,
            "updatedAt": self.updated_at.isoformat(),
            "currentIntent": self.current_intent,
            "activeQuestion": self.active_question.to_contract()
            if self.active_question
            else None,
            "claims": [claim.to_contract() for claim in self.claims],
        }

    def with_claim(self, claim: TemporalClaim) -> DialogueFrame:
        claims = list(self.claims)
        if claim.supersedes_claim_id:
            claims = [
                replace(existing, status=ClaimStatus.SUPERSEDED)
                if existing.claim_id == claim.supersedes_claim_id
                else existing
                for existing in claims
            ]
        claims.append(claim)
        return replace(self, claims=tuple(claims), updated_at=datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    turn_id: str
    role: str
    content: str
    occurred_at: datetime
    mode: str


class ConversationStore(Protocol):
    def get_frame(self, thread_id: str) -> DialogueFrame: ...

    def save_frame(self, frame: DialogueFrame) -> None: ...

    def append_turn(self, thread_id: str, turn: TranscriptTurn) -> None: ...

    def recent_turns(self, thread_id: str, limit: int) -> tuple[TranscriptTurn, ...]: ...


@dataclass(slots=True)
class InMemoryConversationStore:
    """Fixture-friendly store; persistence is supplied by the coordinator/Task 1."""

    frames: dict[str, DialogueFrame] = field(default_factory=dict)
    turns: dict[str, list[TranscriptTurn]] = field(default_factory=dict)

    def get_frame(self, thread_id: str) -> DialogueFrame:
        try:
            return self.frames[thread_id]
        except KeyError as exc:
            raise KeyError(f"unknown thread: {thread_id}") from exc

    def save_frame(self, frame: DialogueFrame) -> None:
        self.frames[frame.thread_id] = frame

    def append_turn(self, thread_id: str, turn: TranscriptTurn) -> None:
        self.turns.setdefault(thread_id, []).append(turn)

    def recent_turns(self, thread_id: str, limit: int) -> tuple[TranscriptTurn, ...]:
        return tuple(self.turns.get(thread_id, [])[-limit:])


class ContextAssembler:
    """Build a bounded typed packet instead of stuffing the transcript."""

    def __init__(self, *, max_characters: int = 6000, recent_turn_limit: int = 4) -> None:
        self.max_characters = max_characters
        self.recent_turn_limit = recent_turn_limit

    def assemble(
        self,
        frame: DialogueFrame,
        turns: tuple[TranscriptTurn, ...],
        finance_projection: dict[str, object],
    ) -> str:
        active_claims = [
            {
                "type": claim.claim_type,
                "statement": claim.statement,
                "basis": claim.basis.value,
                "sourceTurnId": claim.source_turn_id,
                "scope": {
                    "merchantContains": claim.scope.merchant_contains,
                    "maximumAmountMinor": claim.scope.maximum_amount_minor,
                    "currency": claim.scope.currency,
                },
                "effectiveFrom": claim.effective_from.isoformat(),
                "effectiveUntil": claim.effective_until.isoformat()
                if claim.effective_until
                else None,
                "confidence": claim.confidence,
                "supersedesClaimId": claim.supersedes_claim_id,
            }
            for claim in frame.claims
            if claim.status is ClaimStatus.ACTIVE
        ]
        recent = [
            {
                "turnId": turn.turn_id,
                "role": turn.role,
                "content": turn.content[: max(256, self.max_characters // 4)],
            }
            for turn in turns[-self.recent_turn_limit :]
        ]
        packet = {
            "dialogue": {
                "currentIntent": frame.current_intent,
                "activeQuestion": frame.active_question.prompt if frame.active_question else None,
                "activeScenarioId": frame.active_scenario_id,
                "stopped": frame.stopped,
            },
            "claims": active_claims,
            "recentTurnsUntrusted": recent,
            "financeProjection": finance_projection,
        }
        encoded = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        while len(encoded) > self.max_characters and len(recent) > 1:
            recent.pop(0)
            encoded = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > self.max_characters and recent:
            overflow = len(encoded) - self.max_characters
            content = str(recent[-1]["content"])
            recent[-1]["content"] = content[: max(0, len(content) - overflow - 32)]
            encoded = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
        if len(encoded) > self.max_characters:
            raise ValueError("typed context metadata exceeds the configured context budget")
        return encoded


class InquiryPolicy:
    """One adaptive question at a time, with graceful stop/topic changes."""

    _STOP_RE = re.compile(
        r"\b(stop|pause|leave it there|that(?:'s| is) enough|done for now|synthesi[sz]e)\b",
        re.IGNORECASE,
    )

    def is_stop(self, content: str) -> bool:
        return bool(self._STOP_RE.search(content))

    def acknowledge(self, content: str) -> str:
        if self.is_stop(content):
            return "Understood — I’ve kept the context you shared."
        return "Thanks — I’ve kept that context and will use it in the next step."

    def synthesise(self) -> str:
        return (
            "I’ve kept the confirmed details so far and closed the question cleanly. "
            "You can return to it later without restarting the thread."
        )

    def ask(self, frame: DialogueFrame, question: ActiveQuestion) -> DialogueFrame:
        if frame.active_question is not None:
            raise ValueError("only one active question is allowed")
        return replace(
            frame,
            active_question=question,
            updated_at=question.asked_at,
            stopped=False,
        )

    def stop(self, frame: DialogueFrame) -> DialogueFrame:
        return replace(
            frame,
            active_question=None,
            stopped=True,
            updated_at=datetime.now(UTC),
        )
