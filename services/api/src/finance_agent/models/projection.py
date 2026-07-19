"""Typed cloud projection compiler and frozen egress/model receipts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from finance_agent.models.base import ModelMode, ModelPurpose

FIELD_CLASSES = frozenset(
    {
        "aggregate_amounts",
        "finding_labels",
        "forecast_assumptions",
        "owner_claims",
        "evidence_labels",
    }
)
FORBIDDEN_KEYS = frozenset(
    {
        "raw_source_files",
        "rawSourceFiles",
        "raw_ledger_history",
        "rawLedgerHistory",
        "api_key",
        "apiKey",
    }
)
FORBIDDEN_KEY_MARKERS = ("apikey", "token", "password", "secret")
MAX_OWNER_CLAIMS = 20
MAX_OWNER_CLAIM_STATEMENT_CHARACTERS = 1_000
MAX_PROJECTION_ITEMS = 512
MAX_PROJECTION_CHARACTERS = 32_000


class ReceiptModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ModelReceipt(ReceiptModel):
    receipt_version: str = Field(default="model.receipt@1", alias="receiptVersion")
    receipt_id: str = Field(alias="receiptId")
    workspace_id: str = Field(alias="workspaceId")
    thread_id: str = Field(alias="threadId")
    run_id: str = Field(alias="runId")
    mode: str
    provider: str
    model: str
    capability: str
    input_characters: int = Field(alias="inputCharacters", ge=0)
    output_characters: int = Field(alias="outputCharacters", ge=0)
    latency_ms: int = Field(alias="latencyMs", ge=0)
    schema_repair_attempts: int = Field(alias="schemaRepairAttempts", ge=0, le=1)
    status: str
    model_supplied_finance_totals: bool = Field(
        default=False, alias="modelSuppliedFinanceTotals"
    )
    occurred_at: datetime = Field(alias="occurredAt")

    def as_contract(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True)


class EgressReceipt(ReceiptModel):
    receipt_version: str = Field(default="egress.receipt@1", alias="receiptVersion")
    receipt_id: str = Field(alias="receiptId")
    workspace_id: str = Field(alias="workspaceId")
    thread_id: str = Field(alias="threadId")
    run_id: str = Field(alias="runId")
    mode: str
    provider: str = "openai"
    model: str
    purpose: str
    field_classes: list[str] = Field(alias="fieldClasses", min_length=1, max_length=5)
    item_count: int = Field(alias="itemCount", ge=0)
    character_count: int = Field(alias="characterCount", ge=0)
    raw_source_files_included: bool = Field(default=False, alias="rawSourceFilesIncluded")
    raw_ledger_history_included: bool = Field(
        default=False, alias="rawLedgerHistoryIncluded"
    )
    policy_id: str = Field(default="policy_projection_only", alias="policyId")
    occurred_at: datetime = Field(alias="occurredAt")

    def as_contract(self) -> dict[str, object]:
        return self.model_dump(mode="json", by_alias=True)


@dataclass(frozen=True, slots=True)
class ProjectionEnvelope:
    payload: Mapping[str, object]
    field_classes: tuple[str, ...]
    field_paths: tuple[str, ...]
    item_count: int
    character_count: int


class ProjectionPolicy:
    """Compile only policy-allowed typed fields for OpenAI modes."""

    _POLICY: dict[tuple[ModelMode, ModelPurpose], frozenset[str]] = {
        (ModelMode.HYBRID, ModelPurpose.EXPLAIN): frozenset(
            {"aggregate_amounts", "finding_labels", "forecast_assumptions", "evidence_labels"}
        ),
        (ModelMode.HYBRID, ModelPurpose.ASK_QUESTION): frozenset(
            {"finding_labels", "owner_claims", "evidence_labels"}
        ),
        (ModelMode.CLOUD, ModelPurpose.COMPILE_PLAN): FIELD_CLASSES,
        (ModelMode.CLOUD, ModelPurpose.EXPLAIN): FIELD_CLASSES,
        (ModelMode.CLOUD, ModelPurpose.ASK_QUESTION): FIELD_CLASSES,
    }

    def compile(
        self,
        source: Mapping[str, object],
        *,
        mode: ModelMode,
        purpose: ModelPurpose,
    ) -> ProjectionEnvelope:
        if mode is ModelMode.LOCAL:
            encoded = json.dumps(source, ensure_ascii=False, separators=(",", ":"))
            return ProjectionEnvelope(
                payload=dict(source),
                field_classes=(),
                field_paths=tuple(sorted(source)),
                item_count=len(source),
                character_count=len(encoded),
            )
        allowed = self._POLICY.get((mode, purpose), frozenset())
        if not allowed:
            raise ValueError(f"egress is not allowed for {mode.value}/{purpose.value}")
        if self._contains_forbidden_key(source):
            raise ValueError("raw source/ledger/secret fields are never eligible for egress")
        payload = {key: source[key] for key in sorted(allowed) if key in source}
        if not payload:
            raise ValueError("no policy-allowed typed projection fields were supplied")
        payload = self._bound_owner_claims(payload)
        field_paths = tuple(self._field_paths(payload))
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        item_count = self._item_count(payload)
        if item_count > MAX_PROJECTION_ITEMS or len(encoded) > MAX_PROJECTION_CHARACTERS:
            raise ValueError("typed projection exceeds the bounded egress envelope")
        return ProjectionEnvelope(
            payload=payload,
            field_classes=tuple(payload),
            field_paths=field_paths,
            item_count=item_count,
            character_count=len(encoded),
        )

    @staticmethod
    def _normalise_key(key: object) -> str:
        return "".join(character for character in str(key).lower() if character.isalnum())

    @classmethod
    def _contains_forbidden_key(cls, value: object) -> bool:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalised = cls._normalise_key(key)
                if (
                    str(key) in FORBIDDEN_KEYS
                    or any(marker in normalised for marker in FORBIDDEN_KEY_MARKERS)
                    or cls._contains_forbidden_key(child)
                ):
                    return True
            return False
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return any(cls._contains_forbidden_key(child) for child in value)
        return False

    @classmethod
    def _bound_owner_claims(cls, payload: Mapping[str, object]) -> dict[str, object]:
        bounded = dict(payload)
        raw_claims = bounded.get("owner_claims")
        if raw_claims is None:
            return bounded
        if not isinstance(raw_claims, Sequence) or isinstance(
            raw_claims, str | bytes | bytearray
        ):
            raise ValueError("owner_claims must be a sequence")
        claims: list[dict[str, object]] = []
        for index, raw_claim in enumerate(raw_claims):
            if index >= MAX_OWNER_CLAIMS:
                break
            if not isinstance(raw_claim, Mapping):
                raise ValueError("each owner claim must be an object")
            claim = dict(raw_claim)
            if "statement" in claim:
                claim["statement"] = str(claim["statement"])[
                    :MAX_OWNER_CLAIM_STATEMENT_CHARACTERS
                ]
            claims.append(claim)
        bounded["owner_claims"] = claims
        return bounded

    @classmethod
    def _field_paths(cls, value: object, prefix: str = "") -> list[str]:
        paths: list[str] = []
        if isinstance(value, Mapping):
            for key, child in value.items():
                child_prefix = f"{prefix}.{key}" if prefix else str(key)
                paths.extend(cls._field_paths(child, child_prefix))
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            for index, child in enumerate(value):
                paths.extend(cls._field_paths(child, f"{prefix}[{index}]"))
        else:
            paths.append(prefix)
        return paths

    @classmethod
    def _item_count(cls, value: object) -> int:
        if isinstance(value, Mapping):
            return sum(cls._item_count(child) for child in value.values())
        if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
            return sum(cls._item_count(child) for child in value)
        return 1


def receipt_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


def make_egress_receipt(
    envelope: ProjectionEnvelope,
    *,
    workspace_id: str,
    thread_id: str,
    run_id: str,
    mode: ModelMode,
    model: str,
    purpose: ModelPurpose,
) -> EgressReceipt:
    if mode is ModelMode.LOCAL:
        raise ValueError("Local mode never emits an egress receipt")
    now = datetime.now(UTC)
    return EgressReceipt(
        receiptId=receipt_id("egressrcpt", run_id, purpose.value, now.isoformat()),
        workspaceId=workspace_id,
        threadId=thread_id,
        runId=run_id,
        mode=mode.value,
        model=model,
        purpose=purpose.value,
        fieldClasses=list(envelope.field_classes),
        itemCount=envelope.item_count,
        characterCount=envelope.character_count,
        occurredAt=now,
    )
