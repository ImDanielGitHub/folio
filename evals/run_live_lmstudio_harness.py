"""Run a small live Folio planning eval against loopback LM Studio.

This is not part of the deterministic test suite. It requires an already loaded local
model and never selects or calls a cloud adapter. Results distinguish transport output,
model-authored plan acceptance, semantic action correctness, and effective correctness
after Folio's bounded deterministic fallback.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from finance_agent.agent.catalogue import IntentClass
from finance_agent.agent.harness import HarnessRequest, ModelHarness
from finance_agent.agent.ports import FinanceContext
from finance_agent.models.base import ModelMode
from finance_agent.models.lm_studio import LMStudioAdapter, LMStudioConfig
from finance_agent.models.openai import OpenAIConfig, OpenAIResponsesAdapter
from finance_agent.models.router import ModelModeRouter


@dataclass(frozen=True, slots=True)
class EvalCase:
    name: str
    content: str
    expected_intent: IntentClass
    required_action: str


CASES = (
    EvalCase(
        "current_summary",
        "Show me the current summary.",
        IntentClass.READ_SUMMARY,
        "query_summary",
    ),
    EvalCase(
        "recent_transactions",
        "Show the transactions from the last 30 days.",
        IntentClass.READ_TRANSACTIONS,
        "query_transactions",
    ),
    EvalCase(
        "cash_scenario",
        "What happens to cash if the NZD 3,000 laptop is paid on 7 August?",
        IntentClass.SCENARIO,
        "run_cash_scenario",
    ),
    EvalCase(
        "owner_pack",
        "Prepare the owner pack as both a web document and PDF.",
        IntentClass.OWNER_PACK,
        "prepare_owner_pack",
    ),
)


def _finance_context() -> FinanceContext:
    return FinanceContext(
        workspace_id="ws_live_eval_synthetic",
        thread_id="thr_live_eval_synthetic",
        current_surface_type="living_brief",
        projection={
            "dataThrough": "2026-07-17T08:00:00+12:00",
            "aggregate_amounts": {"availableCashMinor": 918400},
            "finding_labels": ["One synthetic invoice is due within seven days"],
            "forecast_assumptions": ["The laptop is paid on 7 August."],
            "evidence_labels": ["Synthetic 30-day forecast"],
        },
        unresolved_merchant="MITRE 10",
        unresolved_date="2026-07-06",
        latest_undoable_event_id="evt_synthetic_latest",
        scenario_id="scenario_synthetic_laptop",
        scenario_amount_minor=300000,
        scenario_date="2026-08-07",
    )


def _context_packet(context: FinanceContext) -> str:
    return json.dumps(
        {
            "business": {"name": "Koru Studio (synthetic eval fixture)"},
            "deterministicProjection": context.projection,
            "currentSurfaceType": context.current_surface_type,
            "activeScenario": {
                "scenarioId": context.scenario_id,
                "plannedAmountMinor": context.scenario_amount_minor,
                "currency": "NZD",
                "plannedDate": context.scenario_date,
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _integer_metric(item: dict[str, object], key: str) -> int:
    value = item[key]
    if not isinstance(value, int):
        raise TypeError(f"Expected integer metric {key}")
    return value


async def _runtime_metadata(config: LMStudioConfig) -> dict[str, object]:
    origin = urlparse(config.base_url)
    inventory_url = f"{origin.scheme}://{origin.netloc}/api/v1/models"
    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.get(inventory_url)
        response.raise_for_status()
    payload = response.json()
    models = payload.get("models", []) if isinstance(payload, dict) else []
    selected: dict[str, Any] = {}
    if isinstance(models, list):
        for item in models:
            if not isinstance(item, dict):
                continue
            if config.model in LMStudioAdapter._model_aliases(item):
                selected = item
                break
    instances = selected.get("loaded_instances", [])
    loaded = instances[0] if isinstance(instances, list) and instances else {}
    loaded_config = loaded.get("config", {}) if isinstance(loaded, dict) else {}
    quantization = selected.get("quantization", {})
    capabilities = selected.get("capabilities", {})
    return {
        "configuredIdentifier": config.model,
        "modelKey": selected.get("key"),
        "displayName": selected.get("display_name"),
        "architecture": selected.get("architecture"),
        "parameterCount": selected.get("params_string"),
        "format": selected.get("format"),
        "quantization": (
            quantization.get("name") if isinstance(quantization, dict) else quantization
        ),
        "sizeBytes": selected.get("size_bytes"),
        "loadedContextLength": (
            loaded_config.get("context_length")
            if isinstance(loaded_config, dict)
            else None
        ),
        "maxContextLength": selected.get("max_context_length"),
        "parallel": loaded_config.get("parallel") if isinstance(loaded_config, dict) else None,
        "trainedForToolUseAdvertised": (
            capabilities.get("trained_for_tool_use")
            if isinstance(capabilities, dict)
            else None
        ),
    }


async def evaluate() -> dict[str, object]:
    config = LMStudioConfig(
        base_url=os.getenv("FOLIO_LM_STUDIO_URL", "http://127.0.0.1:1234/v1"),
        model=os.getenv("FOLIO_LM_STUDIO_MODEL", "folio-qwen3.5-9b"),
        timeout_seconds=90.0,
    )
    adapter = LMStudioAdapter(config)
    cloud = OpenAIResponsesAdapter(OpenAIConfig(api_key=None))
    harness = ModelHarness(ModelModeRouter(local=adapter, cloud=cloud))
    context = _finance_context()
    capability = await adapter.capability()
    runtime = await _runtime_metadata(config)
    details: list[dict[str, object]] = []
    try:
        for index, case in enumerate(CASES, start=1):
            outcome = await harness.compile_plan(
                HarnessRequest(
                    workspace_id=context.workspace_id,
                    thread_id=context.thread_id,
                    run_id=f"run_live_lmstudio_{index}",
                    turn_id=f"turn_live_lmstudio_{index}",
                    content=case.content,
                    mode=ModelMode.LOCAL,
                    context_packet=_context_packet(context),
                    finance_context=context,
                )
            )
            receipt = outcome.model_receipts[0]
            action_kinds = list(outcome.plan.action_kinds) if outcome.plan else []
            required_action_present = case.required_action in action_kinds
            expected_intent = outcome.intent_class is case.expected_intent
            model_accepted = outcome.source == "model"
            details.append(
                {
                    "name": case.name,
                    "expectedIntent": case.expected_intent.value,
                    "observedIntent": outcome.intent_class.value,
                    "requiredAction": case.required_action,
                    "effectiveActionKinds": action_kinds,
                    "source": outcome.source,
                    "modelTransportProducedText": receipt.output_characters > 0,
                    "modelPlanAccepted": model_accepted,
                    "modelPlanSemanticallyCorrect": (
                        model_accepted and expected_intent and required_action_present
                    ),
                    "effectivePlanSemanticallyCorrect": (
                        expected_intent and required_action_present
                    ),
                    "schemaRepairAttempts": receipt.schema_repair_attempts,
                    "modelStatus": receipt.status,
                    "latencyMs": receipt.latency_ms,
                    "fallbackUsed": outcome.source != "model",
                    "questionAsked": outcome.question is not None,
                    "egressReceiptCount": len(outcome.egress_receipts),
                }
            )
    finally:
        await adapter.aclose()
        await cloud.aclose()

    total = len(details)
    transport_count = sum(bool(item["modelTransportProducedText"]) for item in details)
    accepted_count = sum(bool(item["modelPlanAccepted"]) for item in details)
    semantic_count = sum(bool(item["modelPlanSemanticallyCorrect"]) for item in details)
    effective_count = sum(
        bool(item["effectivePlanSemanticallyCorrect"]) for item in details
    )
    return {
        "evalVersion": "live.lmstudio.folio-harness@1",
        "executedAt": datetime.now(UTC).isoformat(),
        "scope": "Four synthetic planning turns through the real Folio adapter and harness.",
        "limitations": [
            "This is a small local runtime smoke benchmark, not a general model-quality claim.",
            "Advertised tool training is inventory metadata, not proof of tool-use accuracy.",
            "Semantic correctness checks the required typed action, not downstream finance results.",
            "Deterministic fallback can make the effective plan correct when the model plan fails.",
        ],
        "runtime": runtime,
        "capability": capability.as_dict(),
        "caseCount": total,
        "modelTransportTextRate": round(transport_count / total, 4),
        "modelTypedPlanAcceptanceRate": round(accepted_count / total, 4),
        "modelSemanticPlanRate": round(semantic_count / total, 4),
        "effectiveSemanticPlanRateAfterFallback": round(effective_count / total, 4),
        "totalSchemaRepairAttempts": sum(
            _integer_metric(item, "schemaRepairAttempts") for item in details
        ),
        "fallbackCount": sum(bool(item["fallbackUsed"]) for item in details),
        "externalEgressReceiptCount": sum(
            _integer_metric(item, "egressReceiptCount") for item in details
        ),
        "cases": details,
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(evaluate()), indent=2, sort_keys=True))
