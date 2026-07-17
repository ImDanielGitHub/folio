"""LM Studio adapter over its loopback OpenAI-compatible API."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from finance_agent.models.base import (
    AdapterStatus,
    CapabilityCard,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
)


@dataclass(frozen=True, slots=True)
class LMStudioConfig:
    base_url: str = "http://127.0.0.1:1234/v1"
    model: str | None = None
    api_token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("LM Studio must use a loopback http endpoint")


class LMStudioAdapter:
    provider = "lm_studio"

    def __init__(
        self,
        config: LMStudioConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or LMStudioConfig()
        headers = {"Accept": "application/json"}
        if self.config.api_token:
            headers["Authorization"] = f"Bearer {self.config.api_token}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=2.0),
            headers=headers,
        )
        parsed = urlparse(self.config.base_url)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _model_inventory(self) -> list[dict[str, Any]]:
        response = await self._client.get(f"{self._origin}/api/v1/models")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            values = payload.get("models", payload.get("data", []))
            if isinstance(values, list):
                return [item for item in values if isinstance(item, dict)]
        return []

    @staticmethod
    def _model_id(item: dict[str, Any]) -> str | None:
        value = item.get("id") or item.get("model") or item.get("key")
        return str(value) if value else None

    @classmethod
    def _model_aliases(cls, item: dict[str, Any]) -> set[str]:
        aliases: set[str] = set()
        model_id = cls._model_id(item)
        if model_id:
            aliases.add(model_id)
        loaded_instances = item.get("loaded_instances", [])
        if isinstance(loaded_instances, list):
            for instance in loaded_instances:
                if not isinstance(instance, dict):
                    continue
                instance_id = instance.get("id")
                if instance_id:
                    aliases.add(str(instance_id))
        return aliases

    @staticmethod
    def _advertised_capabilities(item: dict[str, Any]) -> set[str]:
        capabilities = item.get("capabilities", [])
        if isinstance(capabilities, list):
            return {str(value) for value in capabilities}
        if not isinstance(capabilities, dict):
            return set()

        advertised: set[str] = set()
        for name, value in capabilities.items():
            if value is True or isinstance(value, dict) and (
                value.get("supported") is True or value.get("enabled") is True
            ):
                advertised.add(str(name))
        return advertised

    @staticmethod
    def _response_text(data: object) -> str:
        if not isinstance(data, dict):
            raise ValueError("LM Studio returned a non-object response")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("LM Studio returned no response choice")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ValueError("LM Studio returned no response message")

        # LM Studio's OpenAI-compatible endpoint can expose a reasoning model's final
        # structured object in `reasoning_content` while leaving `content` empty. Prefer
        # ordinary content whenever it is present; the reasoning field is a documented
        # compatibility fallback, not a second model or provider route.
        for field_name in ("content", "reasoning_content"):
            value = message.get(field_name)
            if isinstance(value, str) and value.strip():
                return value
        raise ValueError("LM Studio returned no non-empty text content")

    @staticmethod
    def _state(item: dict[str, Any]) -> str:
        return str(item.get("state") or item.get("status") or "").lower()

    async def capability(self) -> CapabilityCard:
        try:
            inventory = await self._model_inventory()
        except (httpx.HTTPError, ValueError):
            return CapabilityCard(
                provider=self.provider,
                status=AdapterStatus.UNAVAILABLE,
                model=self.config.model,
                tier=0,
                tier_measured=False,
                structured_output=False,
                tool_use=False,
                context_length=None,
                detail="LM Studio server is not reachable on the configured loopback endpoint.",
            )
        if not inventory:
            return CapabilityCard(
                provider=self.provider,
                status=AdapterStatus.NO_MODELS,
                model=self.config.model,
                tier=0,
                tier_measured=False,
                structured_output=False,
                tool_use=False,
                context_length=None,
                detail="LM Studio is running but no model is available.",
            )
        if self.config.model:
            selected = next(
                (
                    item
                    for item in inventory
                    if self.config.model in self._model_aliases(item)
                ),
                None,
            )
            if selected is None:
                return CapabilityCard(
                    provider=self.provider,
                    status=AdapterStatus.NO_MODELS,
                    model=self.config.model,
                    tier=0,
                    tier_measured=False,
                    structured_output=False,
                    tool_use=False,
                    context_length=None,
                    detail="The configured LM Studio model is not available.",
                )
        else:
            selected = inventory[0]
        state = self._state(selected)
        if state in {"loading", "downloading", "initializing"}:
            status = AdapterStatus.LOADING
        elif state in {"failed", "error"}:
            status = AdapterStatus.FAILED
        else:
            status = AdapterStatus.READY
        advertised = self._advertised_capabilities(selected)
        context = selected.get("max_context_length") or selected.get("context_length")
        return CapabilityCard(
            provider=self.provider,
            status=status,
            model=self.config.model or self._model_id(selected),
            tier=0,
            tier_measured=False,
            structured_output=status is AdapterStatus.READY,
            tool_use=bool(
                advertised.intersection(
                    {"tool_use", "tool_calls", "trained_for_tool_use"}
                )
            ),
            context_length=int(context) if isinstance(context, int | float) else None,
            detail=(
                "Model discovered; behavioural tier has not been measured."
                if status is AdapterStatus.READY
                else f"Model state is {state or status.value}."
            ),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        card = await self.capability()
        if card.status is not AdapterStatus.READY or not card.model:
            raise ModelUnavailable(card.detail)
        payload: dict[str, object] = {
            "model": self.config.model or card.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": 0,
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if request.schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "finance_plan",
                    "strict": True,
                    "schema": dict(request.schema),
                },
            }
        started = time.monotonic()
        try:
            response = await self._client.post(
                f"{self.config.base_url.rstrip('/')}/chat/completions", json=payload
            )
            response.raise_for_status()
            text = self._response_text(response.json())
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise ModelUnavailable("LM Studio inference failed without a valid response") from exc
        return ModelResponse(
            text=text,
            provider=self.provider,
            model=self.config.model or card.model,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
