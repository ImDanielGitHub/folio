"""Questions stay read-only and model explanations receive relevant local context."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from finance_agent.agent.catalogue import ACTION_CATALOGUE, WRITE_KINDS, IntentClass
from finance_agent.agent.dialogue import InquiryPolicy
from finance_agent.agent.fallback import classify_intent, compile_fallback_plan
from finance_agent.agent.harness import HarnessRequest, ModelHarness
from finance_agent.agent.ports import FinanceContext
from finance_agent.api.services import LocalRouteServices
from finance_agent.finance.service import THREAD_ID, WORKSPACE_ID
from finance_agent.models.base import (
    AdapterStatus,
    CapabilityCard,
    ModelMode,
    ModelPurpose,
    ModelRequest,
    ModelResponse,
)
from finance_agent.models.router import ModelModeRouter


class RecordingModel:
    def __init__(self, provider: str = "lm_studio") -> None:
        self.provider = provider
        self.requests: list[ModelRequest] = []

    async def capability(self) -> CapabilityCard:
        return CapabilityCard(
            self.provider,
            AdapterStatus.READY,
            "synthetic",
            0,
            False,
            True,
            False,
            8192,
            "Synthetic model, not live accuracy proof",
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        text = (
            "{broken"
            if request.purpose is ModelPurpose.COMPILE_PLAN
            else ("Business expenses are NZD 1,394.99. Review the unresolved expense context.")
        )
        return ModelResponse(text, self.provider, "synthetic", 1)

    async def aclose(self) -> None:
        pass


def source() -> dict[str, object]:
    return {
        "aggregate_amounts": {"currency": "NZD", "businessExpenseMinor": 139499},
        "finding_labels": ["Unresolved expense context"],
        "evidence_labels": ["Imported statement"],
    }


@pytest.mark.parametrize(
    "question",
    [
        "Analyse my business expenses.",
        "Why has client income fallen?",
        "How can I stop overspending?",
        "Compare my business income and expenses.",
        "Is the classification correct?",
        "What would undoing that rule affect?",
        "Explain why my cash changed.",
        "Which client is affecting cash flow?",
    ],
)
def test_analysis_questions_cannot_enter_write_catalogues(question: str) -> None:
    intent = classify_intent(question)
    assert not ACTION_CATALOGUE[intent].intersection(WRITE_KINDS)
    assert intent is not IntentClass.STOP_SYNTHESIS
    assert not InquiryPolicy().is_stop(question)


@pytest.mark.parametrize(
    "content,expected",
    [
        ("Stop here.", IntentClass.STOP_SYNTHESIS),
        ("Some long context. Stop here.", IntentClass.STOP_SYNTHESIS),
        ("Undo that change.", IntentClass.UNDO),
        ("Please reclassify MITRE 10 as business below NZD 500.", IntentClass.CORRECTION),
        (
            "That hardware shop one was for the client studio fit out. Only under $500.",
            IntentClass.CORRECTION,
        ),
        ("If the laptop is deferred, show the cash scenario.", IntentClass.SCENARIO),
    ],
)
def test_explicit_commands_keep_their_existing_authority(
    content: str, expected: IntentClass
) -> None:
    assert classify_intent(content) is expected


def test_unrelated_transaction_read_does_not_inherit_unresolved_merchant() -> None:
    context = FinanceContext(
        WORKSPACE_ID, THREAD_ID, "living_brief", source(), unresolved_merchant="MITRE 10"
    )
    decision = compile_fallback_plan(
        content="Show me transactions.",
        context=context,
        thread_id=THREAD_ID,
        run_id="run_read_test",
    )
    assert decision.plan is not None
    query = next(action for action in decision.plan.actions if action.kind == "query_transactions")
    assert query.merchant_contains is None


@pytest.mark.asyncio
async def test_local_explanation_keeps_question_and_retrieved_context() -> None:
    local = RecordingModel()
    harness = ModelHarness(ModelModeRouter(local, RecordingModel("openai")))
    packet = json.dumps(
        {
            "claims": [{"statement": "Supplier timing matters"}],
            "recentTurnsUntrusted": [{"role": "owner", "content": "Compare timing"}],
            "workingUnderstanding": {"entries": [{"text": "Remember the seasonal work"}]},
        }
    )
    outcome = await harness.explain(
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        run_id="run_explain_local",
        mode=ModelMode.LOCAL,
        source=source(),
        fallback_text="Checked figures.",
        owner_question="Why did expenses change?",
        context_packet=packet,
    )
    prompt = json.loads(local.requests[-1].user)
    assert prompt["ownerQuestionUntrusted"] == "Why did expenses change?"
    assert "seasonal work" in json.dumps(prompt["conversationContextUntrusted"])
    assert "Supplier timing matters" in json.dumps(prompt["conversationContextUntrusted"])
    assert outcome.model_receipt is not None
    assert outcome.model_receipt.status == "completed_validated"
    assert not outcome.egress_receipt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,question_allowed", [(ModelMode.CLOUD, True), (ModelMode.HYBRID, False)]
)
async def test_cloud_context_stays_inside_projection_policy(
    mode: ModelMode, question_allowed: bool
) -> None:
    cloud = RecordingModel("openai")
    harness = ModelHarness(ModelModeRouter(RecordingModel(), cloud))
    result = await harness.explain(
        workspace_id=WORKSPACE_ID,
        thread_id=THREAD_ID,
        run_id="run_cloud_boundary",
        mode=mode,
        source=source(),
        fallback_text="Checked figures.",
        owner_question="Question marker about costs",
        context_packet='{"privateLocalMarker":"Never export this history"}',
    )
    prompt = cloud.requests[-1].user
    assert "privateLocalMarker" not in prompt
    assert "Never export" not in prompt
    assert ("Question marker" in prompt) is question_allowed
    assert result.egress_receipt is not None
    assert ("owner_claims" in result.egress_receipt.field_classes) is question_allowed


@pytest.mark.asyncio
async def test_local_planner_preserves_material_tail_of_long_question() -> None:
    local = RecordingModel()
    harness = ModelHarness(ModelModeRouter(local, RecordingModel("openai")))
    content = "Analyse my expenses. " + "Background detail. " * 400 + "Compare rent, not income."
    await harness.compile_plan(
        HarnessRequest(
            WORKSPACE_ID,
            THREAD_ID,
            "run_long_question",
            "turn_long_question",
            content,
            ModelMode.LOCAL,
            "{}",
            FinanceContext(WORKSPACE_ID, THREAD_ID, "living_brief", source()),
        )
    )
    prompt = json.loads(local.requests[0].user)
    assert "Compare rent, not income." in prompt["ownerTurnUntrusted"]["content"]
    assert len(prompt["ownerTurnUntrusted"]["content"]) <= 3000


@pytest.mark.asyncio
async def test_failed_plan_still_gets_a_model_explanation_without_finance_writes(
    tmp_path: Path,
) -> None:
    services = LocalRouteServices(tmp_path / "analysis.sqlite3")
    await services.local_model.aclose()
    await services.cloud_model.aclose()
    local, cloud = RecordingModel(), RecordingModel("openai")
    services.local_model, services.cloud_model = local, cloud  # type: ignore[assignment]
    services.model_router = ModelModeRouter(local, cloud)
    services._compose_controller()
    before = len(services.store.fetch_all("SELECT * FROM finance_events"))
    try:
        result = await services.submit_turn(
            workspace_id=WORKSPACE_ID,
            thread_id=THREAD_ID,
            turn_id="turn_analysis_read",
            content="Analyse business expenses.",
            mode="local",
        )
        assert result["planSource"] == "deterministic_fallback"
        assert any(item.purpose is ModelPurpose.EXPLAIN for item in local.requests)
        turns = services.conversations.recent_turns(THREAD_ID, 3)
        assert turns[-1].content.startswith("Business expenses are NZD 1,394.99.")
        assert len(services.store.fetch_all("SELECT * FROM finance_events")) == before
        assert not cloud.requests
    finally:
        await services.aclose()


@pytest.mark.asyncio
async def test_old_owner_context_reaches_explanation_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "continued.sqlite3"

    async def configured() -> tuple[LocalRouteServices, RecordingModel]:
        services = LocalRouteServices(database)
        await services.local_model.aclose()
        await services.cloud_model.aclose()
        local, cloud = RecordingModel(), RecordingModel("openai")
        services.local_model, services.cloud_model = local, cloud  # type: ignore[assignment]
        services.model_router = ModelModeRouter(local, cloud)
        services._compose_controller()
        return services, local

    first, _ = await configured()
    try:
        await first.submit_turn(
            workspace_id=WORKSPACE_ID,
            thread_id=THREAD_ID,
            turn_id="turn_early_accountant",
            content="Priya Shah is our accountant.",
            mode="local",
        )
        for index in range(5):
            await first.submit_turn(
                workspace_id=WORKSPACE_ID,
                thread_id=THREAD_ID,
                turn_id=f"turn_later_summary_{index}",
                content="Summarise current expenses.",
                mode="local",
            )
    finally:
        await first.aclose()
    second, local = await configured()
    try:
        assert all(
            "Priya Shah" not in turn.content
            for turn in second.conversations.recent_turns(THREAD_ID, 4)
        )
        await second.submit_turn(
            workspace_id=WORKSPACE_ID,
            thread_id=THREAD_ID,
            turn_id="turn_recall_accountant",
            content="What did I tell you about our accountant?",
            mode="local",
        )
        explanation = next(
            item for item in reversed(local.requests) if item.purpose is ModelPurpose.EXPLAIN
        )
        context = json.loads(explanation.user)["conversationContextUntrusted"]
        assert "Priya Shah" in json.dumps(context["workingUnderstanding"])
    finally:
        await second.aclose()
