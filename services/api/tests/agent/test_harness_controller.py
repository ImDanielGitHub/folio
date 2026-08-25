from __future__ import annotations

from datetime import UTC, datetime

import pytest
from finance_agent.agent.catalogue import ControllerState
from finance_agent.agent.controller import (
    FinanceAgentController,
    InMemoryReceiptSink,
    TurnRequest,
)
from finance_agent.agent.dialogue import (
    ActiveQuestion,
    DialogueFrame,
    InMemoryConversationStore,
)
from finance_agent.agent.fallback import compile_fallback_plan
from finance_agent.agent.harness import HarnessRequest, ModelHarness
from finance_agent.agent.plan import FinancePlan
from finance_agent.agent.ports import FinanceContext, FinanceServiceResult
from finance_agent.models.base import (
    AdapterStatus,
    CapabilityCard,
    ModelMode,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
)
from finance_agent.models.router import ModelModeRouter


def fixture_context() -> FinanceContext:
    return FinanceContext(
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        current_surface_type="living_brief",
        projection={
            "dataThrough": "2026-07-17T08:00:00+12:00",
            "aggregate_amounts": {"reserveShortfallMinor": 9923},
            "finding_labels": ["Protected reserve is at risk"],
            "forecast_assumptions": ["The laptop is paid on 7 August."],
            "evidence_labels": ["30-day forecast"],
        },
        unresolved_merchant="MITRE 10",
        unresolved_date="2026-07-06",
        latest_undoable_event_id="evt_koru_rule_mitre10",
        scenario_id="scenario_koru_laptop",
        scenario_amount_minor=300000,
        scenario_date="2026-08-07",
    )


class StubAdapter:
    def __init__(self, provider: str, responses: list[str] | None = None) -> None:
        self.provider = provider
        self.responses = responses or []

    async def capability(self) -> CapabilityCard:
        return CapabilityCard(
            provider=self.provider,
            status=AdapterStatus.READY if self.responses else AdapterStatus.UNAVAILABLE,
            model="fixture-model" if self.responses else None,
            tier=3 if self.responses else 0,
            tier_measured=True,
            structured_output=bool(self.responses),
            tool_use=False,
            context_length=8192,
            detail="fixture",
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.responses:
            raise ModelUnavailable("fixture unavailable")
        return ModelResponse(
            text=self.responses.pop(0),
            provider=self.provider,
            model="fixture-model",
            latency_ms=1,
        )


class FakeFinanceCore:
    def __init__(self) -> None:
        self.write_actions: tuple[object, ...] = ()

    async def load_context(self, workspace_id: str, thread_id: str) -> FinanceContext:
        return fixture_context()

    async def query_summary(self, action: object) -> FinanceServiceResult:
        return FinanceServiceResult("action_summary", "query_summary", "completed")

    async def query_transactions(self, action: object) -> FinanceServiceResult:
        return FinanceServiceResult("action_query", "query_transactions", "completed")

    async def run_cash_scenario(self, action: object) -> FinanceServiceResult:
        return FinanceServiceResult(
            "action_scenario", "run_cash_scenario", "completed", ("evd_koru_forecast_30d",)
        )

    async def execute_reversible_writes(
        self, actions: tuple[object, ...], *, source_turn_id: str
    ) -> tuple[FinanceServiceResult, ...]:
        self.write_actions = actions
        return (
            FinanceServiceResult(
                "action_claim_fixture",
                "record_business_claim",
                "completed",
                evidence_ids=("evd_koru_owner_claim_mitre10",),
            ),
            FinanceServiceResult(
                "action_rule_fixture",
                "create_classification_rule",
                "completed",
                data={
                    "event": {
                        "scopeJson": {"transactionIds": ["txn_koru_006"]},
                    }
                },
                evidence_ids=("evd_koru_mitre10_row", "evd_koru_owner_claim_mitre10"),
                event_id="evt_koru_rule_mitre10",
            ),
        )

    async def recompute(self, event_ids: tuple[str, ...]) -> FinanceServiceResult:
        return FinanceServiceResult("action_recompute", "recompute", "completed")

    async def prepare_owner_pack(self, action: object) -> FinanceServiceResult:
        return FinanceServiceResult("action_pack", "prepare_owner_pack", "completed")

    async def select_surface(self, action: object) -> FinanceServiceResult:
        return FinanceServiceResult("action_surface", "show_surface", "completed")


def unavailable_harness() -> ModelHarness:
    unavailable = StubAdapter("lm_studio")
    cloud = StubAdapter("openai")
    return ModelHarness(ModelModeRouter(local=unavailable, cloud=cloud))


def test_held_out_correction_fallback_is_bounded_and_has_no_transaction_ids() -> None:
    decision = compile_fallback_plan(
        content=(
            "That hardware shop one was for the client studio fit out. "
            "Keep the rule only under $500."
        ),
        context=fixture_context(),
        thread_id="thr_koru_studio_main",
        run_id="run_held_out_correction",
    )
    assert decision.plan is not None
    assert decision.plan.action_kinds == (
        "record_business_claim",
        "create_classification_rule",
        "show_surface",
    )
    contract = decision.plan.as_contract()
    assert "transactionIds" not in str(contract)
    assert contract["actions"][1]["maximumAmountMinor"] == 50000  # type: ignore[index]


@pytest.mark.asyncio
async def test_model_gets_one_repair_attempt_then_valid_plan() -> None:
    valid = FinancePlan.model_validate(
        {
            "planVersion": "FinancePlan@1",
            "planId": "plan_fixture_repaired",
            "threadId": "thr_koru_studio_main",
            "runId": "run_fixture_repair",
            "intent": "Read the current summary.",
            "actions": [
                {
                    "actionId": "action_fixture_summary",
                    "kind": "query_summary",
                    "window": "current",
                }
            ],
        }
    )
    local = StubAdapter(
        "lm_studio",
        ["{not json", f"```json\n{valid.model_dump_json(by_alias=True)}\n```"],
    )
    harness = ModelHarness(ModelModeRouter(local=local, cloud=StubAdapter("openai")))
    outcome = await harness.compile_plan(
        HarnessRequest(
            workspace_id="ws_koru_studio",
            thread_id="thr_koru_studio_main",
            run_id="run_fixture_repair",
            turn_id="turn_fixture_summary",
            content="Show me the current summary.",
            mode=ModelMode.LOCAL,
            context_packet="{}",
            finance_context=fixture_context(),
        )
    )
    assert outcome.source == "model"
    assert outcome.plan == valid
    assert outcome.model_receipts[0].schema_repair_attempts == 1
    assert outcome.egress_receipts == ()


@pytest.mark.asyncio
async def test_fixture_controller_executes_correction_and_commits_receipt() -> None:
    frame = DialogueFrame(
        frame_id="frame_koru_current",
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        updated_at=datetime.now(UTC),
        current_intent="Resolve the Mitre 10 item.",
    )
    conversations = InMemoryConversationStore(frames={frame.thread_id: frame})
    core = FakeFinanceCore()
    sink = InMemoryReceiptSink()
    controller = FinanceAgentController(
        finance_core=core,
        conversations=conversations,
        harness=unavailable_harness(),
        receipt_sink=sink,
    )
    result = await controller.run_turn(
        TurnRequest(
            workspace_id="ws_koru_studio",
            thread_id="thr_koru_studio_main",
            run_id="run_koru_owner_turn_fixture",
            turn_id="turn_koru_owner_fixture",
            content="Mitre 10 was for a client fit-out; apply this only below NZD 500.",
            mode=ModelMode.LOCAL,
        )
    )
    assert result.plan_source == "deterministic_fallback"
    assert ControllerState.EXECUTE_REVERSIBLE_WRITE in result.state_trace
    assert result.state_trace[-1] is ControllerState.COMMIT_RECEIPT
    assert result.work_receipt.status == "completed"
    assert len(core.write_actions) == 2
    assert sink.receipts == [result.work_receipt]
    stored = conversations.get_frame("thr_koru_studio_main")
    assert stored.claims[-1].basis.value == "explicit"
    lowered = result.narrative.lower()
    assert "thanks" not in lowered
    assert "kept that context" not in lowered
    assert "mitre 10" in lowered
    assert "rule" in lowered
    assert "evidence" in lowered or "undo" in lowered


@pytest.mark.asyncio
async def test_long_answer_stop_preserves_turn_and_closes_question_cleanly() -> None:
    question = ActiveQuestion(
        question_id="question_koru_context",
        prompt="What was the purchase for?",
        reason="classification scope",
        asked_at=datetime.now(UTC),
    )
    frame = DialogueFrame(
        frame_id="frame_koru_question",
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        updated_at=datetime.now(UTC),
        current_intent="Understand the purchase.",
        active_question=question,
    )
    conversations = InMemoryConversationStore(frames={frame.thread_id: frame})
    controller = FinanceAgentController(
        finance_core=FakeFinanceCore(),
        conversations=conversations,
        harness=unavailable_harness(),
        receipt_sink=InMemoryReceiptSink(),
    )
    long_answer = ("It related to the client fit-out and timing context. " * 150) + "Stop here."
    result = await controller.run_turn(
        TurnRequest(
            workspace_id="ws_koru_studio",
            thread_id=frame.thread_id,
            run_id="run_koru_stop_fixture",
            turn_id="turn_koru_stop_fixture",
            content=long_answer,
            mode=ModelMode.LOCAL,
        )
    )
    assert "incomplete" not in result.narrative.lower()
    assert conversations.get_frame(frame.thread_id).active_question is None
    assert conversations.recent_turns(frame.thread_id, 10)[0].content == long_answer
