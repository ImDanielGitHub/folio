from __future__ import annotations

import json

import httpx
import pytest
from finance_agent.models.base import (
    AdapterStatus,
    ModelPurpose,
    ModelRequest,
    ModelUnavailable,
)
from finance_agent.models.lm_studio import LMStudioAdapter, LMStudioConfig


def _inventory() -> dict[str, object]:
    return {
        "models": [
            {
                "type": "llm",
                "key": "qwen/qwen3.5-9b",
                "loaded_instances": [
                    {
                        "id": "folio-qwen3.5-9b",
                        "config": {"context_length": 32768, "parallel": 1},
                    }
                ],
                "max_context_length": 262144,
                "capabilities": {
                    "vision": True,
                    "trained_for_tool_use": True,
                    "reasoning": {"allowed_options": ["off", "on"], "default": "on"},
                },
            }
        ]
    }


def _request() -> ModelRequest:
    return ModelRequest(
        system="Return the typed object.",
        user="Compile this owner turn.",
        purpose=ModelPurpose.COMPILE_PLAN,
        schema={
            "type": "object",
            "properties": {"status": {"const": "ok"}},
            "required": ["status"],
            "additionalProperties": False,
        },
    )


@pytest.mark.asyncio
async def test_capability_matches_loaded_identifier_and_dictionary_tool_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/models"
        return httpx.Response(200, json=_inventory())

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(
        LMStudioConfig(model="folio-qwen3.5-9b"),
        client=client,
    )
    try:
        card = await adapter.capability()
    finally:
        await client.aclose()

    assert card.status is AdapterStatus.READY
    assert card.model == "folio-qwen3.5-9b"
    assert card.structured_output is True
    assert card.tool_use is True
    assert card.context_length == 262144
    assert card.tier_measured is False


@pytest.mark.asyncio
async def test_complete_uses_reasoning_content_when_content_is_empty() -> None:
    posted_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json=_inventory())
        assert request.url.path == "/v1/chat/completions"
        posted_payloads.append(dict(json.loads(request.content)))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "reasoning_content": '{"status":"ok"}',
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(
        LMStudioConfig(model="folio-qwen3.5-9b"),
        client=client,
    )
    try:
        response = await adapter.complete(_request())
    finally:
        await client.aclose()

    assert response.text == '{"status":"ok"}'
    assert response.provider == "lm_studio"
    assert response.model == "folio-qwen3.5-9b"
    assert posted_payloads[0]["model"] == "folio-qwen3.5-9b"
    assert posted_payloads[0]["response_format"] == {
        "type": "json_schema",
        "json_schema": {
            "name": "finance_plan",
            "strict": True,
            "schema": dict(_request().schema or {}),
        },
    }


@pytest.mark.asyncio
async def test_complete_prefers_non_empty_content_over_reasoning_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json=_inventory())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"status":"content"}',
                            "reasoning_content": '{"status":"reasoning"}',
                        }
                    }
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(
        LMStudioConfig(model="folio-qwen3.5-9b"),
        client=client,
    )
    try:
        response = await adapter.complete(_request())
    finally:
        await client.aclose()

    assert response.text == '{"status":"content"}'


@pytest.mark.asyncio
async def test_complete_fails_closed_when_both_text_fields_are_empty() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json=_inventory())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": " ", "reasoning_content": ""}}
                ]
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = LMStudioAdapter(
        LMStudioConfig(model="folio-qwen3.5-9b"),
        client=client,
    )
    try:
        with pytest.raises(ModelUnavailable, match="valid response"):
            await adapter.complete(_request())
    finally:
        await client.aclose()
