"""Thin OpenAI Responses API adapter with no SDK or hidden provider fallback."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from finance_agent.models.base import (
    AdapterStatus,
    CapabilityCard,
    ModelRequest,
    ModelResponse,
    ModelUnavailable,
)


@dataclass(frozen=True, slots=True)
class OpenAIConfig:
    api_key: str | None = field(default=None, repr=False)
    model: str = "gpt-5.6"
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 60.0

    @classmethod
    def from_env(cls) -> OpenAIConfig:
        return cls(
            api_key=os.getenv("OPENAI_API_KEY") or None,
            model=os.getenv("OPENAI_MODEL", "gpt-5.6"),
        )


class OpenAIResponsesAdapter:
    provider = "openai"

    def __init__(
        self,
        config: OpenAIConfig | None = None,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.config = config or OpenAIConfig.from_env()
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(self.config.timeout_seconds, connect=10.0),
            headers=headers,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def capability(self) -> CapabilityCard:
        configured = bool(self.config.api_key)
        return CapabilityCard(
            provider=self.provider,
            status=AdapterStatus.READY if configured else AdapterStatus.UNCONFIGURED,
            model=self.config.model,
            tier=3 if configured else 0,
            tier_measured=False,
            structured_output=configured,
            tool_use=False,
            context_length=None,
            detail=(
                "OpenAI is configured; no live request was made by this capability check."
                if configured
                else "OPENAI_API_KEY is not configured."
            ),
        )

    @staticmethod
    def _output_text(payload: dict[str, Any]) -> str:
        convenience = payload.get("output_text")
        if isinstance(convenience, str):
            return convenience
        parts: list[str] = []
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "output_text":
                        text = part.get("text")
                        if isinstance(text, str):
                            parts.append(text)
        if not parts:
            raise ValueError("Responses API returned no output text")
        return "".join(parts)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not self.config.api_key:
            raise ModelUnavailable("OpenAI is unavailable because no API key is configured")
        payload: dict[str, object] = {
            "model": self.config.model,
            "store": False,
            "input": [
                {
                    "role": "developer",
                    "content": [{"type": "input_text", "text": request.system}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": request.user}],
                },
            ],
            "max_output_tokens": request.max_output_tokens,
        }
        if request.schema is not None:
            payload["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": "finance_plan",
                    "strict": True,
                    "schema": dict(request.schema),
                }
            }
        started = time.monotonic()
        try:
            response = await self._client.post(
                f"{self.config.base_url.rstrip('/')}/responses", json=payload
            )
            response.raise_for_status()
            text = self._output_text(response.json())
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            # The exception deliberately excludes response bodies and headers.
            raise ModelUnavailable("OpenAI Responses request failed") from exc
        return ModelResponse(
            text=text,
            provider=self.provider,
            model=self.config.model,
            latency_ms=max(0, int((time.monotonic() - started) * 1000)),
        )
