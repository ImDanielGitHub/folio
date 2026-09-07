"""Local inference must select runnable models and only release final answers."""

from __future__ import annotations

import json
from dataclasses import replace

import httpx
import pytest
from finance_agent.models.base import AdapterStatus, ModelPurpose, ModelRequest, ModelUnavailable
from finance_agent.models.lm_studio import LMStudioAdapter, LMStudioConfig


def inventory() -> dict[str, object]:
    return {
        "models": [
            {"type": "embedding", "key": "embed", "loaded_instances": [{"id": "embed"}]},
            {"type": "llm", "key": "idle", "loaded_instances": []},
            {
                "type": "llm",
                "key": "qwen",
                "max_context_length": 262144,
                "loaded_instances": [{"id": "local-qwen", "config": {"context_length": 8192}}],
            },
        ]
    }


def request(*, structured: bool = False) -> ModelRequest:
    return ModelRequest(
        system="Use checked evidence.",
        user="Explain the recorded expenses.",
        purpose=ModelPurpose.EXPLAIN,
        schema={
            "type": "object",
            "properties": {"ok": {"const": True}},
            "required": ["ok"],
            "additionalProperties": False,
        }
        if structured
        else None,
    )


@pytest.mark.asyncio
async def test_selects_loaded_language_model_and_actual_context() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=inventory()))
    ) as client:
        card = await LMStudioAdapter(client=client).capability()
    assert card.status is AdapterStatus.READY
    assert card.model == "local-qwen"
    assert card.context_length == 8192
    assert card.tier_measured is False


@pytest.mark.asyncio
async def test_explicit_unloaded_model_is_not_replaced_or_called() -> None:
    seen: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(200, json=inventory())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = LMStudioAdapter(LMStudioConfig(model="idle"), client=client)
        assert (await adapter.capability()).status is AdapterStatus.NO_MODELS
        with pytest.raises(ModelUnavailable):
            await adapter.complete(request())
    assert "/v1/chat/completions" not in seen


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["missing", "embed"])
async def test_explicit_selection_never_becomes_another_model(model: str) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=inventory()))
    ) as client:
        card = await LMStudioAdapter(LMStudioConfig(model=model), client=client).capability()
    assert card.status is AdapterStatus.NO_MODELS


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 405])
async def test_legacy_inventory_compatibility_retains_explicit_model(status: int) -> None:
    seen: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == "/api/v1/models":
            return httpx.Response(status)
        return httpx.Response(200, json={"data": [{"id": "chosen"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        card = await LMStudioAdapter(LMStudioConfig(model="chosen"), client=client).capability()
    assert card.status is AdapterStatus.READY
    assert card.model == "chosen"
    assert card.context_length is None
    assert seen == ["/api/v1/models", "/v1/models"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 500])
async def test_auth_and_server_errors_do_not_downgrade_discovery(status: int) -> None:
    seen: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(status, json={"error": "sensitive upstream body"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        card = await LMStudioAdapter(client=client).capability()
    assert card.status is AdapterStatus.UNAVAILABLE
    assert seen == ["/api/v1/models"]
    assert "sensitive" not in card.detail


@pytest.mark.asyncio
async def test_configured_token_is_used_with_injected_client() -> None:
    seen: list[str | None] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req.headers.get("authorization"))
        if req.method == "GET":
            return httpx.Response(200, json=inventory())
        assert json.loads(req.content)["model"] == "local-qwen"
        return httpx.Response(200, json={"choices": [{"message": {"content": "Checked."}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await LMStudioAdapter(LMStudioConfig(api_token="synthetic-token"), client=client).complete(
            request()
        )
    assert seen and all(value == "Bearer synthetic-token" for value in seen)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message,finish,structured",
    [
        ({"content": "", "reasoning_content": "Private deliberation"}, "stop", False),
        ({"content": "", "reasoning_content": '{"not_ok":true}'}, "stop", True),
        ({"content": "Part of an answer"}, "length", False),
        ({"content": "<think>Unfinished reasoning"}, "stop", False),
    ],
)
async def test_non_final_output_fails_closed(
    message: dict[str, str], finish: str, structured: bool
) -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=inventory())
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": finish}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ModelUnavailable):
            await LMStudioAdapter(client=client).complete(request(structured=structured))


@pytest.mark.asyncio
async def test_strips_inline_reasoning_before_returning_final_content() -> None:
    def handle(req: httpx.Request) -> httpx.Response:
        if req.method == "GET":
            return httpx.Response(200, json=inventory())
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "<think>Do not publish this.</think>Costs need review."
                        }
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await LMStudioAdapter(client=client).complete(request())
    assert result.text == "Costs need review."


def test_environment_token_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LM_STUDIO_API_TOKEN", "synthetic-token")
    monkeypatch.setenv("LM_STUDIO_TIMEOUT_SECONDS", "75")
    config = LMStudioConfig.from_env()
    assert config.api_token == "synthetic-token"
    assert config.timeout_seconds == 75
    assert "synthetic-token" not in repr(config)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), 601])
def test_rejects_invalid_timeout(value: float) -> None:
    with pytest.raises(ValueError):
        replace(LMStudioConfig(), timeout_seconds=value)


@pytest.mark.parametrize(
    "url",
    [
        "http://user:password@127.0.0.1:1234/v1",
        "http://127.0.0.1:1234/v1?token=secret",
        "http://127.0.0.1:1234/v1#fragment",
    ],
)
def test_rejects_credentials_or_components_in_endpoint(url: str) -> None:
    with pytest.raises(ValueError):
        LMStudioConfig(base_url=url)


@pytest.mark.asyncio
@pytest.mark.parametrize("item", [{}, {"id": {"bad": "value"}}, {"type": {"bad": "value"}}])
async def test_malformed_inventory_never_claims_ready(item: dict[str, object]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"models": [item]}))
    ) as client:
        card = await LMStudioAdapter(client=client).capability()
    assert card.status in {AdapterStatus.NO_MODELS, AdapterStatus.UNAVAILABLE}
    assert card.structured_output is False
