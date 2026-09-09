"""Provider-independent model adapter contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ModelMode(StrEnum):
    LOCAL = "local"
    HYBRID = "hybrid"
    CLOUD = "cloud"


class ModelPurpose(StrEnum):
    COMPILE_PLAN = "compile_plan"
    CLASSIFY = "classify"
    EXPLAIN = "explain"
    ASK_QUESTION = "ask_question"


class AdapterStatus(StrEnum):
    READY = "ready"
    UNAVAILABLE = "unavailable"
    LOADING = "loading"
    UNCONFIGURED = "unconfigured"
    NO_MODELS = "no_models"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system: str
    user: str
    purpose: ModelPurpose
    schema: Mapping[str, object] | None = None
    max_output_tokens: int = 1200


@dataclass(frozen=True, slots=True)
class ModelResponse:
    text: str
    provider: str
    model: str
    latency_ms: int


@dataclass(frozen=True, slots=True)
class CapabilityCard:
    provider: str
    status: AdapterStatus
    model: str | None
    tier: int
    tier_measured: bool
    structured_output: bool
    tool_use: bool
    context_length: int | None
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "status": self.status.value,
            "model": self.model,
            "tier": self.tier,
            "tierMeasured": self.tier_measured,
            "structuredOutput": self.structured_output,
            "toolUse": self.tool_use,
            "contextLength": self.context_length,
            "detail": self.detail,
        }


class ModelUnavailable(RuntimeError):
    """Selected provider is unavailable; callers must not silently switch providers."""


class ModelAdapter(Protocol):
    provider: str

    async def capability(self) -> CapabilityCard: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...
