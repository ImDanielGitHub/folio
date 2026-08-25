from __future__ import annotations

import json

import pytest
from finance_agent.agent.harness import ModelHarness
from finance_agent.models.base import (
    AdapterStatus,
    CapabilityCard,
    ModelMode,
    ModelRequest,
    ModelResponse,
)
from finance_agent.models.narrative_guard import NarrativeGuard, NarrativeRejection
from finance_agent.models.router import ModelModeRouter


def narrative_source() -> dict[str, object]:
    return {
        "aggregate_amounts": {
            "asOf": "2026-07-17T08:00:00+12:00",
            "currency": "NZD",
            "currentBalanceMinor": 125000,
            "businessExpenseMinor": 18475,
            "reserveShortfallMinor": 9923,
        },
        "finding_labels": ["Protected reserve is at risk"],
        "forecast_assumptions": [
            "The laptop is planned for 7 August.",
            "The cash forecast covers 30 days.",
        ],
        "owner_claims": [
            {
                "statement": "The client fit-out limit is NZD 500.00.",
                "basis": "explicit",
            }
        ],
        "evidence_labels": ["30-day forecast", "evd_koru_forecast_30d"],
    }


class ReadyAdapter:
    provider = "lm_studio"

    def __init__(self, response: str) -> None:
        self.response = response
        self.requests: list[ModelRequest] = []

    async def capability(self) -> CapabilityCard:
        return CapabilityCard(
            provider=self.provider,
            status=AdapterStatus.READY,
            model="small-local-fixture",
            tier=1,
            tier_measured=True,
            structured_output=True,
            tool_use=False,
            context_length=4096,
            detail="fixture",
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        return ModelResponse(
            text=self.response,
            provider=self.provider,
            model="small-local-fixture",
            latency_ms=3,
        )


class ReadyCloudAdapter(ReadyAdapter):
    provider = "openai"


class UnconfiguredCloudAdapter:
    provider = "openai"

    async def capability(self) -> CapabilityCard:
        return CapabilityCard(
            provider=self.provider,
            status=AdapterStatus.UNCONFIGURED,
            model="gpt-5.6",
            tier=0,
            tier_measured=False,
            structured_output=False,
            tool_use=False,
            context_length=None,
            detail="fixture has no credential",
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        raise AssertionError("unconfigured cloud adapter must not be called")


def test_compiler_exposes_formatted_closed_references_not_minor_units_or_raw_ids() -> None:
    guard = NarrativeGuard()
    packet = guard.compile_references(narrative_source())
    prompt = json.dumps(packet.as_prompt_value(), sort_keys=True)

    assert "NZD 99.23" in prompt
    assert '"formattedValue": "NZD 1,250.00"' in prompt
    assert "9923" not in prompt
    assert "125000" not in prompt
    assert "evd_koru_forecast_30d" not in prompt
    assert "30-day forecast" in prompt


@pytest.mark.parametrize(
    ("text", "expected_rejection"),
    [
        (
            "The current balance is NZD 99.23.",
            NarrativeRejection.AMOUNT_LABEL_MISMATCH,
        ),
        (
            "The reserve shortfall is NZD 400.00.",
            NarrativeRejection.UNSUPPORTED_NUMBER,
        ),
        (
            "The reserve shortfall is 99.23.",
            NarrativeRejection.NON_CANONICAL_AMOUNT,
        ),
        (
            "The bill was paid.",
            NarrativeRejection.UNSUPPORTED_ACTION_CLAIM,
        ),
        (
            "According to Xero, the protected reserve is at risk.",
            NarrativeRejection.INVENTED_PROVENANCE,
        ),
        (
            "The source is evd_koru_forecast_30d.",
            NarrativeRejection.RAW_INTERNAL_IDENTIFIER,
        ),
        (
            "You should transfer NZD 99.23 now.",
            NarrativeRejection.PROHIBITED_DIRECTIVE,
        ),
        (
            "The client fit-out limit is NZD 500.00.",
            NarrativeRejection.UNSUPPORTED_NUMBER,
        ),
        (
            "The reserve shortfall is four hundred dollars.",
            NarrativeRejection.UNSUPPORTED_NUMBER,
        ),
        (
            "The reserve shortfall is approximately NZD 99.23.",
            NarrativeRejection.UNSUPPORTED_CALCULATION,
        ),
    ],
)
def test_adversarial_prose_fails_closed(
    text: str,
    expected_rejection: NarrativeRejection,
) -> None:
    guard = NarrativeGuard()
    result = guard.validate(text, guard.compile_references(narrative_source()))

    assert not result.accepted
    assert expected_rejection in result.rejections


@pytest.mark.parametrize(
    "text",
    [
        (
            "The reserve shortfall is NZD 99.23, based on the 30-day forecast. "
            "The practical issue is the protected reserve risk."
        ),
        "The laptop is planned for 7 August.",
        "You said the client fit-out limit is NZD 500.00.",
        "The business spending shown here is NZD 184.75.",
    ],
)
def test_grounded_conversational_prose_is_preserved(text: str) -> None:
    guard = NarrativeGuard()
    result = guard.validate(text, guard.compile_references(narrative_source()))

    assert result.accepted, result.rejections


def test_committed_action_fact_is_required_for_completion_claim() -> None:
    source = narrative_source()
    source["committed_actions"] = [
        {
            "kind": "send_remittance",
            "label": "Remittance sent",
            "status": "sent",
            "committed": True,
        }
    ]
    guard = NarrativeGuard()
    result = guard.validate(
        "The remittance was sent.",
        guard.compile_references(source),
    )

    assert result.accepted
    assert result.used_reference_ids


@pytest.mark.asyncio
async def test_harness_rejects_unsupported_model_amount_and_receipts_fallback() -> None:
    raw_model_text = "The reserve shortfall is NZD 400.00, according to Xero."
    local = ReadyAdapter(raw_model_text)
    cloud = ReadyAdapter("unused")
    harness = ModelHarness(ModelModeRouter(local=local, cloud=cloud))

    outcome = await harness.explain(
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        run_id="run_narrative_rejected",
        mode=ModelMode.LOCAL,
        source=narrative_source(),
        fallback_text="The finance work completed and its receipt is ready.",
    )

    assert outcome.text == "The finance work completed and its receipt is ready."
    assert outcome.model_receipt is not None
    assert outcome.model_receipt.status == "rejected_output_fallback"
    assert outcome.model_receipt.output_characters == len(raw_model_text)
    # The rejected model text never becomes finance authority.
    assert outcome.model_receipt.model_supplied_finance_totals is False
    assert outcome.egress_receipt is None
    request_payload = json.loads(local.requests[0].user)
    assert request_payload["referenceVersion"] == "narrative.references@1"
    assert "aggregate_amounts" not in request_payload


@pytest.mark.asyncio
async def test_harness_accepts_validated_model_prose_and_receipts_it() -> None:
    raw_model_text = "The reserve gap is NZD 99.23, based on the 30-day forecast."
    local = ReadyAdapter(raw_model_text)
    harness = ModelHarness(ModelModeRouter(local=local, cloud=ReadyAdapter("unused")))

    outcome = await harness.explain(
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        run_id="run_narrative_accepted",
        mode=ModelMode.LOCAL,
        source=narrative_source(),
        fallback_text="fallback",
    )

    assert outcome.text == raw_model_text
    assert outcome.model_receipt is not None
    assert outcome.model_receipt.status == "completed_validated"
    # The accepted amount is copied from a deterministic reference, not model-supplied truth.
    assert outcome.model_receipt.model_supplied_finance_totals is False


@pytest.mark.asyncio
async def test_cloud_rejection_records_both_attempted_egress_and_fallback_status() -> None:
    raw_model_text = "I paid NZD 400.00 for you."
    cloud = ReadyCloudAdapter(raw_model_text)
    harness = ModelHarness(ModelModeRouter(local=ReadyAdapter("unused"), cloud=cloud))

    outcome = await harness.explain(
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        run_id="run_cloud_narrative_rejected",
        mode=ModelMode.CLOUD,
        source=narrative_source(),
        fallback_text="No payment was made. The finance work receipt is available.",
    )

    assert outcome.text == "No payment was made. The finance work receipt is available."
    assert outcome.model_receipt is not None
    assert outcome.model_receipt.status == "rejected_output_fallback"
    assert outcome.egress_receipt is not None
    assert outcome.egress_receipt.mode == "cloud"
    assert outcome.egress_receipt.raw_source_files_included is False
    assert outcome.egress_receipt.raw_ledger_history_included is False


@pytest.mark.asyncio
async def test_unconfigured_cloud_emits_no_false_model_or_egress_receipt() -> None:
    harness = ModelHarness(
        ModelModeRouter(local=ReadyAdapter("unused"), cloud=UnconfiguredCloudAdapter())
    )

    outcome = await harness.explain(
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        run_id="run_cloud_unconfigured",
        mode=ModelMode.CLOUD,
        source=narrative_source(),
        fallback_text="Cloud wording is unavailable; deterministic finance state is unchanged.",
    )

    assert outcome.text == "Cloud wording is unavailable; deterministic finance state is unchanged."
    assert outcome.model_receipt is None
    assert outcome.egress_receipt is None
