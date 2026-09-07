"""LM Studio adapter over its loopback OpenAI-compatible API."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from jsonschema import ValidationError, validate  # type: ignore[import-untyped]

from finance_agent.models.base import (
    AdapterStatus,
    CapabilityCard,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
)

DEFAULT_BASE_URL = "http://127.0.0.1:1234/v1"


@dataclass(frozen=True, slots=True)
class LMStudioConfig:
    base_url: str = DEFAULT_BASE_URL
    model: str | None = None
    api_token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("LM Studio must use a loopback http endpoint")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("LM Studio endpoint must not contain credentials, query or fragment")
        if not math.isfinite(self.timeout_seconds) or not 0 < self.timeout_seconds <= 600:
            raise ValueError("LM Studio timeout must be between zero and 600 seconds")

    @classmethod
    def from_env(cls) -> LMStudioConfig:
        base_url = os.getenv("LM_STUDIO_BASE_URL", DEFAULT_BASE_URL).strip() or DEFAULT_BASE_URL
        model = os.getenv("LM_STUDIO_MODEL", "").strip() or None
        return cls(
            base_url=base_url,
            model=model,
            api_token=os.getenv("LM_STUDIO_API_TOKEN", "").strip() or None,
            timeout_seconds=float(os.getenv("LM_STUDIO_TIMEOUT_SECONDS", "30")),
        )


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
        self._headers = headers
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=2.0),
            headers=headers,
            trust_env=False,
            follow_redirects=False,
        )
        parsed = urlparse(self.config.base_url)
        self._origin = f"{parsed.scheme}://{parsed.netloc}"

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _model_inventory(self) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{self._origin}/api/v1/models", headers=self._headers, timeout=3.0
        )
        # Older servers may only expose the compatible endpoint. Authentication
        # or server failures must never trigger a different endpoint/provider.
        if response.status_code in {404, 405}:
            response = await self._client.get(
                f"{self.config.base_url.rstrip('/')}/models",
                headers=self._headers,
                timeout=3.0,
            )
        response.raise_for_status()
        payload = response.json()
        values = (
            payload
            if isinstance(payload, list)
            else (payload.get("models", payload.get("data")) if isinstance(payload, dict) else None)
        )
        if not isinstance(values, list) or len(values) > 1000:
            raise ValueError("LM Studio returned an invalid model inventory")
        if any(not isinstance(item, dict) for item in values):
            raise ValueError("LM Studio returned an invalid model inventory item")
        return [item for item in values if item.get("type") in (None, "llm")]

    @staticmethod
    def _model_id(item: dict[str, Any]) -> str | None:
        value = item.get("id") or item.get("model") or item.get("key")
        return value.strip() if isinstance(value, str) and value.strip() else None

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
            if (
                value is True
                or isinstance(value, dict)
                and (value.get("supported") is True or value.get("enabled") is True)
            ):
                advertised.add(str(name))
        return advertised

    @staticmethod
    def _response_text(data: object, schema: Mapping[str, object] | None = None) -> str:
        if not isinstance(data, dict):
            raise ValueError("LM Studio returned a non-object response")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ValueError("LM Studio returned no response choice")
        if choices[0].get("finish_reason") not in {None, "stop"}:
            raise ValueError("LM Studio did not finish a complete answer")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ValueError("LM Studio returned no response message")
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            # Some local templates include reasoning tags in content. Remove only
            # complete blocks; never publish an unfinished/private reasoning tail.
            cleaned = re.sub(r"<(think|analysis)>.*?</\1>", "", content, flags=re.I | re.S).strip()
            if re.search(r"</?(?:think|analysis)>", cleaned, re.I) or not cleaned:
                raise ValueError("LM Studio returned incomplete reasoning without a final answer")
            return cleaned
        reasoning = message.get("reasoning_content")
        if schema is not None and isinstance(reasoning, str) and reasoning.strip():
            # Preserve the legacy structured-object compatibility case, but only
            # for pure JSON matching the requested schema. Prose is never final.
            try:
                value = json.loads(reasoning)
                validate(value, dict(schema))
            except (ValueError, ValidationError) as exc:
                raise ValueError("LM Studio returned no schema-valid final object") from exc
            return reasoning.strip()
        raise ValueError("LM Studio returned no non-empty final content")

    @staticmethod
    def _state(item: dict[str, Any]) -> str:
        return str(item.get("state") or item.get("status") or "").lower()

    @classmethod
    def _loaded_instances(cls, item: dict[str, Any]) -> list[dict[str, Any]]:
        values = item.get("loaded_instances", [])
        if not isinstance(values, list):
            return []
        return sorted(
            (
                value
                for value in values
                if isinstance(value, dict) and isinstance(value.get("id"), str) and value["id"]
            ),
            key=lambda value: str(value["id"]),
        )

    @classmethod
    def _runnable(cls, item: dict[str, Any]) -> bool:
        if cls._state(item) in {
            "loading",
            "downloading",
            "initializing",
            "failed",
            "error",
            "unloaded",
            "not-loaded",
            "not_loaded",
        }:
            return False
        return "loaded_instances" not in item or bool(cls._loaded_instances(item))

    async def capability(self) -> CapabilityCard:
        def unavailable(status: AdapterStatus, detail: str) -> CapabilityCard:
            return CapabilityCard(
                provider=self.provider,
                status=status,
                model=self.config.model,
                tier=0,
                tier_measured=False,
                structured_output=False,
                tool_use=False,
                context_length=None,
                detail=detail,
            )

        try:
            inventory = await self._model_inventory()
        except (httpx.HTTPError, ValueError):
            return unavailable(
                AdapterStatus.UNAVAILABLE,
                "LM Studio discovery failed. Check the local server and API token.",
            )
        if self.config.model:
            selected = next(
                (item for item in inventory if self.config.model in self._model_aliases(item)), None
            )
        else:
            candidates = sorted(inventory, key=lambda item: self._model_id(item) or "")
            selected = next(
                (item for item in candidates if self._runnable(item)),
                candidates[0] if candidates else None,
            )
        if selected is None:
            return unavailable(
                AdapterStatus.NO_MODELS,
                "No matching language model is available. Load one in LM Studio.",
            )
        state = self._state(selected)
        if state in {"loading", "downloading", "initializing"}:
            return unavailable(AdapterStatus.LOADING, "The selected local model is still loading.")
        if state in {"failed", "error"}:
            return unavailable(AdapterStatus.FAILED, "The selected local model failed to load.")
        if not self._runnable(selected) or not (
            self._model_id(selected) or self._loaded_instances(selected)
        ):
            return unavailable(
                AdapterStatus.NO_MODELS,
                "The selected language model is not loaded. Load it in LM Studio.",
            )
        instances = self._loaded_instances(selected)
        instance = next(
            (item for item in instances if item["id"] == self.config.model),
            instances[0] if instances else None,
        )
        model = str(instance["id"]) if instance else self._model_id(selected)
        config = instance.get("config", {}) if instance else {}
        context = config.get("context_length") if isinstance(config, dict) else None
        if context is None:
            context = selected.get("context_length")
        actual_context = (
            context
            if isinstance(context, int) and not isinstance(context, bool) and context > 0
            else None
        )
        advertised = self._advertised_capabilities(selected)
        return CapabilityCard(
            provider=self.provider,
            status=AdapterStatus.READY,
            model=model,
            tier=0,
            tier_measured=False,
            structured_output=True,
            tool_use=bool(
                advertised.intersection({"tool_use", "tool_calls", "trained_for_tool_use"})
            ),
            context_length=actual_context,
            detail="Language model discovered; behavioural accuracy has not been measured.",
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        card = await self.capability()
        if card.status is not AdapterStatus.READY or not card.model:
            raise ModelUnavailable(card.detail)
        payload: dict[str, object] = {
            "model": card.model,
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
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                json=payload,
                headers=self._headers,
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
            text = self._response_text(response.json(), request.schema)
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise ModelUnavailable("LM Studio inference failed without a valid response") from exc
        return ModelResponse(
            text=text,
            provider=self.provider,
            model=card.model,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
