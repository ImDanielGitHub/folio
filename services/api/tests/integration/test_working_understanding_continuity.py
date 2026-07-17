from __future__ import annotations

from pathlib import Path

import pytest

from finance_agent.api.services import LocalRouteServices
from finance_agent.finance.service import THREAD_ID, WORKSPACE_ID
from finance_agent.models.base import (
    AdapterStatus,
    CapabilityCard,
    ModelMode,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
)
from finance_agent.models.router import ModelModeRouter


class OfflineModel:
    def __init__(self, provider: str) -> None:
        self.provider = provider

    async def capability(self) -> CapabilityCard:
        return CapabilityCard(
            provider=self.provider,
            status=AdapterStatus.UNAVAILABLE,
            model=None,
            tier=0,
            tier_measured=True,
            structured_output=False,
            tool_use=False,
            context_length=None,
            detail="Continuity eval intentionally runs without a language model.",
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        raise ModelUnavailable("Continuity eval intentionally runs offline.")

    async def aclose(self) -> None:
        return None


def _force_offline(services: LocalRouteServices) -> None:
    services.local_model = OfflineModel("lm_studio")  # type: ignore[assignment]
    services.cloud_model = OfflineModel("openai")  # type: ignore[assignment]
    services.model_router = ModelModeRouter(services.local_model, services.cloud_model)
    services._compose_controller()  # noqa: SLF001


@pytest.mark.asyncio
async def test_working_understanding_survives_long_thread_restart_and_model_switch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "working-understanding.sqlite3"
    first = LocalRouteServices(database, auto_seed=False)
    _force_offline(first)
    try:
        await first.reset_demo(WORKSPACE_ID)
        await first.enqueue_daily_close(
            workspace_id=WORKSPACE_ID,
            idempotency_key="working-understanding-close",
        )
        early_turn_id = "turn_working_understanding_early"
        early_content = (
            "Koru Studio is based in Hamilton. Priya Shah is our accountant. "
            "The MITRE 10 purchase was materials for the Harbour client fit-out. "
            "We keep GST in the business saver account."
        )
        await first.submit_turn(
            workspace_id=WORKSPACE_ID,
            thread_id=THREAD_ID,
            turn_id=early_turn_id,
            content=early_content,
            mode="local",
        )
        for index in range(6):
            await first.submit_turn(
                workspace_id=WORKSPACE_ID,
                thread_id=THREAD_ID,
                turn_id=f"turn_working_understanding_filler_{index}",
                content=f"Summarise the current balance and expenses. Check {index + 1}.",
                mode="local" if index % 2 == 0 else "hybrid",
            )

        assert early_turn_id not in {
            turn.turn_id for turn in first.conversations.recent_turns(THREAD_ID, 4)
        }
        before = await first.working_understanding_diagnostics(workspace_id=WORKSPACE_ID)
        before_facts = before["facts"]
        assert isinstance(before_facts, list)
        assert any(
            fact["predicate"] == "business.accountant"
            and fact["objectText"] == "Priya Shah"
            and fact["status"] == "active"
            and fact["source"]["turnId"] == early_turn_id
            for fact in before_facts
        )
        assert any(
            fact["predicate"] == "business.base_city" and fact["objectText"] == "Hamilton"
            for fact in before_facts
        )
        assert any(
            fact["predicate"] == "expense.business_purpose"
            and "Harbour client fit-out" in fact["objectText"]
            for fact in before_facts
        )
        before_revision = int(before["summary"]["revision"])
        assert first.current_mode is ModelMode.HYBRID
    finally:
        await first.aclose()

    second = LocalRouteServices(database, auto_seed=True)
    _force_offline(second)
    try:
        restarted = await second.working_understanding_diagnostics(workspace_id=WORKSPACE_ID)
        assert int(restarted["summary"]["revision"]) >= before_revision
        assert second.current_mode is ModelMode.HYBRID

        correction_turn_id = "turn_working_understanding_accountant_correction"
        await second.submit_turn(
            workspace_id=WORKSPACE_ID,
            thread_id=THREAD_ID,
            turn_id=correction_turn_id,
            content=(
                "Correction: Alex Chen is now our accountant. "
                "Priya Shah was the previous accountant."
            ),
            mode="cloud",
        )
        final = await second.submit_turn(
            workspace_id=WORKSPACE_ID,
            thread_id=THREAD_ID,
            turn_id="turn_working_understanding_final_query",
            content=(
                "Who is our accountant, and why was the MITRE 10 purchase a business expense?"
            ),
            mode="hybrid",
        )
        diagnostics = await second.working_understanding_diagnostics(
            workspace_id=WORKSPACE_ID,
            run_id=str(final["runId"]),
        )
        facts = diagnostics["facts"]
        assert isinstance(facts, list)
        alex = next(
            fact
            for fact in facts
            if fact["predicate"] == "business.accountant" and fact["objectText"] == "Alex Chen"
        )
        priya = next(
            fact
            for fact in facts
            if fact["predicate"] == "business.accountant" and fact["objectText"] == "Priya Shah"
        )
        purpose = next(
            fact
            for fact in facts
            if fact["predicate"] == "expense.business_purpose"
            and "Harbour client fit-out" in fact["objectText"]
        )
        assert alex["status"] == "active"
        assert alex["source"]["turnId"] == correction_turn_id
        assert alex["supersedesFactId"] == priya["factId"]
        assert priya["status"] == "superseded"
        assert priya["supersededByFactId"] == alex["factId"]
        assert purpose["status"] == "active"
        assert purpose["source"]["turnId"] == early_turn_id

        receipts = diagnostics["retrievalReceipts"]
        assert isinstance(receipts, list) and len(receipts) == 1
        selected_ids = set(receipts[0]["selectedIds"])
        assert alex["factId"] in selected_ids
        assert purpose["factId"] in selected_ids
        assert priya["factId"] not in selected_ids
        assert receipts[0]["packetCharacters"] <= receipts[0]["maxCharacters"]
        assert int(diagnostics["summary"]["revision"]) > before_revision
        assert second.current_mode is ModelMode.HYBRID

        original = second.store.fetch_one(
            """
            SELECT content FROM knowledge_owner_statements
            WHERE workspace_id = ? AND turn_id = ?
            """,
            (WORKSPACE_ID, early_turn_id),
        )
        assert original is not None and str(original["content"]) == early_content
        assert second.store.fetch_one("SELECT COUNT(*) AS count FROM egress_receipts")["count"] == 0
    finally:
        await second.aclose()
