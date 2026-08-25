from __future__ import annotations

from pathlib import Path

import pytest
from finance_agent.api.services import LocalRouteServices
from finance_agent.models.base import AdapterStatus, CapabilityCard
from finance_agent.models.lm_studio import LMStudioConfig


def _local_ready_card() -> CapabilityCard:
    return CapabilityCard(
        provider="lm_studio",
        status=AdapterStatus.READY,
        model="test-local-model",
        tier=0,
        tier_measured=False,
        structured_output=True,
        tool_use=False,
        context_length=8192,
        detail="Synthetic local capability for configuration tests.",
    )


def test_lm_studio_config_from_env_selects_loopback_base_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://localhost:2345/v1")
    monkeypatch.setenv("LM_STUDIO_MODEL", "local-finance-test")

    config = LMStudioConfig.from_env()

    assert config.base_url == "http://localhost:2345/v1"
    assert config.model == "local-finance-test"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com/v1",
        "http://192.168.1.10:1234/v1",
        "http://localhost.example:1234/v1",
    ],
)
def test_lm_studio_config_from_env_rejects_non_loopback_endpoints(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
) -> None:
    monkeypatch.setenv("LM_STUDIO_BASE_URL", base_url)

    with pytest.raises(ValueError, match="loopback"):
        LMStudioConfig.from_env()


@pytest.mark.asyncio
async def test_local_route_services_selects_model_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://127.0.0.1:2234/v1")
    monkeypatch.setenv("LM_STUDIO_MODEL", "folio-local-test")
    monkeypatch.setenv("OPENAI_API_KEY", "configured-for-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test-model")

    services = LocalRouteServices(tmp_path / "model-config.sqlite3", auto_seed=False)
    try:
        assert services.local_model.config.base_url == "http://127.0.0.1:2234/v1"
        assert services.local_model.config.model == "folio-local-test"
        assert services.cloud_model.config.api_key == "configured-for-test"
        assert services.cloud_model.config.model == "gpt-test-model"
    finally:
        await services.aclose()


@pytest.mark.asyncio
async def test_model_capabilities_reports_unconfigured_cloud_without_external_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    services = LocalRouteServices(tmp_path / "unconfigured-cloud.sqlite3", auto_seed=False)

    async def local_capability() -> CapabilityCard:
        return _local_ready_card()

    monkeypatch.setattr(services.local_model, "capability", local_capability)
    try:
        capabilities = await services.model_capabilities()
    finally:
        await services.aclose()

    assert capabilities["cloudCredentialState"] == "absent"
    assert capabilities["externalCallsMade"] is False
    assert capabilities["modes"]["cloud"]["status"] == "unconfigured"  # type: ignore[index]
    assert capabilities["modes"]["hybrid"]["hiddenCloudFallback"] is False  # type: ignore[index]


@pytest.mark.asyncio
async def test_model_capabilities_reports_configured_mocked_cloud_without_external_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "configured-for-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test-model")
    services = LocalRouteServices(tmp_path / "configured-cloud.sqlite3", auto_seed=False)

    async def local_capability() -> CapabilityCard:
        return _local_ready_card()

    monkeypatch.setattr(services.local_model, "capability", local_capability)
    try:
        capabilities = await services.model_capabilities()
    finally:
        await services.aclose()

    assert capabilities["cloudCredentialState"] == "configured"
    assert capabilities["externalCallsMade"] is False
    assert capabilities["modes"]["cloud"]["status"] == "ready"  # type: ignore[index]
    assert capabilities["modes"]["cloud"]["model"] == "gpt-test-model"  # type: ignore[index]
