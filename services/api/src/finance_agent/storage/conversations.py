"""SQLite-backed continuing-thread and DialogueFrame persistence."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, cast

from finance_agent.agent.dialogue import (
    ActiveQuestion,
    ClaimBasis,
    ClaimScope,
    ClaimStatus,
    DialogueFrame,
    TemporalClaim,
    TranscriptTurn,
)

from .store import SQLiteStore

WORKSPACE_ID = "ws_koru_studio"
THREAD_ID = "thr_koru_studio_main"


class SQLiteConversationStore:
    """Idempotent storage adapter for the one continuing business thread."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        workspace_id: str = WORKSPACE_ID,
        thread_id: str = THREAD_ID,
    ) -> None:
        self.store = store
        self.workspace_id = workspace_id
        self.thread_id = thread_id

    @staticmethod
    def _claim(value: dict[str, Any]) -> TemporalClaim:
        scope = cast(dict[str, Any], value.get("scope", {}))
        return TemporalClaim(
            claim_id=str(value["claimId"]),
            claim_type=str(value["claimType"]),
            statement=str(value["statement"]),
            source_turn_id=str(value["sourceTurnId"]),
            scope=ClaimScope(
                merchant_contains=cast(str | None, scope.get("merchantContains")),
                maximum_amount_minor=cast(int | None, scope.get("maximumAmountMinor")),
                currency=cast(str | None, scope.get("currency")),
                workspace_id=cast(str | None, value.get("workspaceId")),
            ),
            basis=ClaimBasis(str(value.get("basis", "explicit"))),
            effective_from=date.fromisoformat(str(value["effectiveDate"])),
            effective_until=(
                date.fromisoformat(str(value["effectiveUntil"]))
                if value.get("effectiveUntil")
                else None
            ),
            confidence=float(value.get("confidence", 1.0)),
            recorded_at=datetime.fromisoformat(str(value["recordedAt"])),
            status=ClaimStatus(str(value.get("status", "active"))),
            supersedes_claim_id=cast(str | None, value.get("supersedesClaimId")),
        )

    @classmethod
    def _frame(cls, value: dict[str, Any]) -> DialogueFrame:
        raw_question = cast(dict[str, Any] | None, value.get("activeQuestion"))
        question = (
            ActiveQuestion(
                question_id=str(raw_question["questionId"]),
                prompt=str(raw_question["prompt"]),
                reason=str(raw_question.get("reason", "Owner context is required.")),
                asked_at=datetime.fromisoformat(str(raw_question["askedAt"])),
            )
            if raw_question
            else None
        )
        raw_claims = cast(list[dict[str, Any]], value.get("claims", []))
        return DialogueFrame(
            frame_id=str(value["frameId"]),
            workspace_id=str(value["workspaceId"]),
            thread_id=str(value["threadId"]),
            updated_at=datetime.fromisoformat(str(value["updatedAt"])),
            current_intent=str(value.get("currentIntent", "Continue the finance thread.")),
            active_question=question,
            claims=tuple(cls._claim(item) for item in raw_claims),
            active_scenario_id=cast(str | None, value.get("activeScenarioId")),
            stopped=bool(value.get("stopped", False)),
        )

    def get_frame(self, thread_id: str) -> DialogueFrame:
        if thread_id != self.thread_id:
            raise KeyError(f"unknown thread: {thread_id}")
        value = self.store.current_dialogue_frame(self.workspace_id, thread_id)
        if value is None:
            raise KeyError(f"unknown thread: {thread_id}")
        return self._frame(value)

    def save_frame(self, frame: DialogueFrame) -> None:
        if frame.workspace_id != self.workspace_id or frame.thread_id != self.thread_id:
            raise ValueError("DialogueFrame does not belong to the canonical workspace/thread")
        self.store.save_dialogue_frame(frame.to_contract())

    def append_turn(self, thread_id: str, turn: TranscriptTurn) -> None:
        if thread_id != self.thread_id:
            raise KeyError(f"unknown thread: {thread_id}")
        self.store.record_turn(
            turn_id=turn.turn_id,
            workspace_id=self.workspace_id,
            thread_id=thread_id,
            role=turn.role,
            content=turn.content,
            occurred_at=turn.occurred_at.isoformat(),
        )

    def recent_turns(self, thread_id: str, limit: int) -> tuple[TranscriptTurn, ...]:
        if thread_id != self.thread_id:
            raise KeyError(f"unknown thread: {thread_id}")
        rows = self.store.fetch_all(
            """
            SELECT turn_id, role, content, occurred_at
            FROM conversation_turns
            WHERE workspace_id = ? AND thread_id = ?
            ORDER BY occurred_at DESC, turn_id DESC
            LIMIT ?
            """,
            (self.workspace_id, thread_id, limit),
        )
        mode_row = self.store.fetch_one(
            "SELECT model_mode FROM workspaces WHERE workspace_id = ?", (self.workspace_id,)
        )
        mode = str(mode_row["model_mode"]) if mode_row else "local"
        return tuple(
            TranscriptTurn(
                turn_id=str(row["turn_id"]),
                role=str(row["role"]),
                content=str(row["content"]),
                occurred_at=datetime.fromisoformat(str(row["occurred_at"])),
                mode=mode,
            )
            for row in reversed(rows)
        )
