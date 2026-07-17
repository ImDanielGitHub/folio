"""Explicit Local/Hybrid/Cloud routing with no hidden cloud fallback."""

from __future__ import annotations

from dataclasses import dataclass

from finance_agent.models.base import ModelAdapter, ModelMode, ModelPurpose


@dataclass(frozen=True, slots=True)
class ModelModeRouter:
    local: ModelAdapter
    cloud: ModelAdapter

    def adapter_for(self, mode: ModelMode, purpose: ModelPurpose) -> ModelAdapter:
        if mode is ModelMode.LOCAL:
            return self.local
        if mode is ModelMode.HYBRID:
            if purpose in {ModelPurpose.EXPLAIN, ModelPurpose.ASK_QUESTION}:
                return self.cloud
            return self.local
        return self.cloud

    async def capabilities(self) -> dict[str, object]:
        local = await self.local.capability()
        cloud = await self.cloud.capability()
        return {
            "modes": {
                "local": local.as_dict(),
                "hybrid": {
                    "planning": local.as_dict(),
                    "language": cloud.as_dict(),
                    "hiddenCloudFallback": False,
                },
                "cloud": cloud.as_dict(),
            }
        }
