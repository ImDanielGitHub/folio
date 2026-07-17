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
        selected = next(
            (
                item
                for item in inventory
                if self.config.model and self._model_id(item) == self.config.model
            ),
            inventory[0],
        )
        state = self._state(selected)
        if state in {"loading", "downloading", "initializing"}:
            status = AdapterStatus.LOADING
        elif state in {"failed", "error"}:
            status = AdapterStatus.FAILED
        else:
            status = AdapterStatus.READY
        capabilities = selected.get("capabilities", [])
        advertised = (
            {str(value) for value in capabilities}
            if isinstance(capabilities, list)
            else set()
        )
        context = selected.get("max_context_length") or selected.get("context_length")
        return CapabilityCard(
            provider=self.provider,
            status=status,
            model=self._model_id(selected),
            tier=0,
            tier_measured=False,
            structured_output=status is AdapterStatus.READY,
            tool_use="tool_use" in advertised,
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
            data = response.json()
            text = data["choices"][0]["message"]["content"]
            if not isinstance(text, str):
                raise ValueError("LM Studio returned non-text model content")
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelUnavailable("LM Studio inference failed without a valid response") from exc
        return ModelResponse(
            text=text,
            provider=self.provider,
            model=self.config.model or card.model,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
