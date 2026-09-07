from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def insert_method_before(path: str, class_name: str, before_name: str, method: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    before = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


MIGRATION = '''    Migration(
        version={version},
        name="workspace_egress_consent",
        sql="""
        CREATE TABLE egress_policy_revisions (
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            allowed_modes_json TEXT NOT NULL,
            allowed_field_classes_json TEXT NOT NULL,
            maximum_characters INTEGER NOT NULL CHECK (maximum_characters BETWEEN 1 AND 32000),
            maximum_items INTEGER NOT NULL CHECK (maximum_items BETWEEN 1 AND 512),
            expires_at TEXT NOT NULL,
            revoked INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
            confirmation_hash TEXT NOT NULL CHECK (length(confirmation_hash) = 64),
            created_at TEXT NOT NULL,
            PRIMARY KEY (workspace_id, revision)
        );

        CREATE INDEX egress_policy_latest
            ON egress_policy_revisions(workspace_id, revision DESC, expires_at);
        """,
    ),
'''

CONSENT = '''"""Append-only owner consent for typed Hybrid and Cloud projection egress."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from finance_agent.models.base import ModelMode, ModelPurpose
from finance_agent.models.projection import FIELD_CLASSES, ProjectionPolicy
from finance_agent.storage import SQLiteStore, canonical_json

CONFIRMATION_PHRASE = "ALLOW FOLIO CLOUD PROJECTIONS"
MAX_CONSENT_DAYS = 90


class EgressConsentRequired(PermissionError):
    pass


@dataclass(frozen=True, slots=True)
class EgressConsent:
    workspace_id: str
    revision: int
    allowed_modes: tuple[str, ...]
    allowed_field_classes: tuple[str, ...]
    maximum_characters: int
    maximum_items: int
    expires_at: str
    revoked: bool
    created_at: str

    def as_dict(self) -> dict[str, object]:
        now = datetime.now(UTC)
        expires = datetime.fromisoformat(self.expires_at)
        active = not self.revoked and expires > now
        return {
            "workspaceId": self.workspace_id,
            "revision": self.revision,
            "allowedModes": list(self.allowed_modes),
            "allowedFieldClasses": list(self.allowed_field_classes),
            "maximumCharacters": self.maximum_characters,
            "maximumItems": self.maximum_items,
            "expiresAt": self.expires_at,
            "revoked": self.revoked,
            "active": active,
            "createdAt": self.created_at,
        }


class EgressConsentService:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @staticmethod
    def _row(row: Any) -> EgressConsent:
        return EgressConsent(
            workspace_id=str(row["workspace_id"]),
            revision=int(row["revision"]),
            allowed_modes=tuple(json.loads(str(row["allowed_modes_json"]))),
            allowed_field_classes=tuple(
                json.loads(str(row["allowed_field_classes_json"]))
            ),
            maximum_characters=int(row["maximum_characters"]),
            maximum_items=int(row["maximum_items"]),
            expires_at=str(row["expires_at"]),
            revoked=bool(row["revoked"]),
            created_at=str(row["created_at"]),
        )

    def latest(self, workspace_id: str) -> EgressConsent | None:
        row = self.store.fetch_one(
            """
            SELECT * FROM egress_policy_revisions
            WHERE workspace_id = ? ORDER BY revision DESC LIMIT 1
            """,
            (workspace_id,),
        )
        return None if row is None else self._row(row)

    def grant(
        self,
        *,
        workspace_id: str,
        confirmation: str,
        allowed_modes: tuple[str, ...],
        allowed_field_classes: tuple[str, ...],
        expires_in_days: int,
        maximum_characters: int,
        maximum_items: int,
    ) -> EgressConsent:
        if confirmation != CONFIRMATION_PHRASE:
            raise ValueError("cloud egress confirmation phrase does not match")
        modes = tuple(sorted(set(allowed_modes)))
        if not modes or any(mode not in {"hybrid", "cloud"} for mode in modes):
            raise ValueError("allowedModes must contain hybrid and/or cloud")
        classes = tuple(sorted(set(allowed_field_classes)))
        if not classes or any(value not in FIELD_CLASSES for value in classes):
            raise ValueError("allowedFieldClasses contains an unsupported class")
        if not 1 <= expires_in_days <= MAX_CONSENT_DAYS:
            raise ValueError("expiresInDays must be between 1 and 90")
        if not 1 <= maximum_characters <= 32000:
            raise ValueError("maximumCharacters must be between 1 and 32000")
        if not 1 <= maximum_items <= 512:
            raise ValueError("maximumItems must be between 1 and 512")
        now = datetime.now(UTC)
        expires = now + timedelta(days=expires_in_days)
        confirmation_hash = hashlib.sha256(
            f"{workspace_id}\\0{confirmation}\\0{now.isoformat()}".encode()
        ).hexdigest()
        with self.store.transaction() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(MAX(revision), 0) AS revision
                FROM egress_policy_revisions WHERE workspace_id = ?
                """,
                (workspace_id,),
            ).fetchone()
            revision = int(row["revision"]) + 1
            connection.execute(
                """
                INSERT INTO egress_policy_revisions(
                    workspace_id, revision, allowed_modes_json,
                    allowed_field_classes_json, maximum_characters,
                    maximum_items, expires_at, revoked, confirmation_hash,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    workspace_id,
                    revision,
                    canonical_json(list(modes)),
                    canonical_json(list(classes)),
                    maximum_characters,
                    maximum_items,
                    expires.isoformat(),
                    confirmation_hash,
                    now.isoformat(),
                ),
            )
        value = self.latest(workspace_id)
        assert value is not None
        return value

    def revoke(self, workspace_id: str) -> EgressConsent:
        current = self.latest(workspace_id)
        if current is None:
            raise KeyError(workspace_id)
        now = datetime.now(UTC)
        with self.store.transaction() as connection:
            connection.execute(
                """
                INSERT INTO egress_policy_revisions(
                    workspace_id, revision, allowed_modes_json,
                    allowed_field_classes_json, maximum_characters,
                    maximum_items, expires_at, revoked, confirmation_hash,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    workspace_id,
                    current.revision + 1,
                    canonical_json(list(current.allowed_modes)),
                    canonical_json(list(current.allowed_field_classes)),
                    current.maximum_characters,
                    current.maximum_items,
                    current.expires_at,
                    hashlib.sha256(
                        f"{workspace_id}\\0revoked\\0{now.isoformat()}".encode()
                    ).hexdigest(),
                    now.isoformat(),
                ),
            )
        revoked = self.latest(workspace_id)
        assert revoked is not None
        return revoked

    def require(self, *, workspace_id: str, mode: ModelMode) -> EgressConsent | None:
        if mode is ModelMode.LOCAL:
            return None
        current = self.latest(workspace_id)
        if current is None:
            raise EgressConsentRequired(
                "Hybrid or Cloud mode requires explicit workspace egress consent"
            )
        if current.revoked:
            raise EgressConsentRequired("workspace cloud egress consent is revoked")
        if datetime.fromisoformat(current.expires_at) <= datetime.now(UTC):
            raise EgressConsentRequired("workspace cloud egress consent has expired")
        if mode.value not in current.allowed_modes:
            raise EgressConsentRequired(
                f"workspace cloud egress consent does not allow {mode.value} mode"
            )
        return current

    def preview(
        self,
        *,
        workspace_id: str,
        mode: ModelMode,
        purpose: ModelPurpose,
        source: dict[str, object],
    ) -> dict[str, object]:
        current = self.require(workspace_id=workspace_id, mode=mode)
        assert current is not None
        envelope = ProjectionPolicy().compile(source, mode=mode, purpose=purpose)
        disallowed = sorted(
            set(envelope.field_classes) - set(current.allowed_field_classes)
        )
        within_bounds = (
            envelope.character_count <= current.maximum_characters
            and envelope.item_count <= current.maximum_items
            and not disallowed
        )
        return {
            "workspaceId": workspace_id,
            "mode": mode.value,
            "purpose": purpose.value,
            "consentRevision": current.revision,
            "fieldClasses": list(envelope.field_classes),
            "fieldPaths": list(envelope.field_paths),
            "itemCount": envelope.item_count,
            "characterCount": envelope.character_count,
            "disallowedFieldClasses": disallowed,
            "withinConsentBounds": within_bounds,
            "rawValuesIncluded": False,
        }
'''

SERVICE_METHODS = '''    async def egress_consent_status(
        self, *, workspace_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        current = EgressConsentService(self.store).latest(workspace_id)
        return {
            "workspaceId": workspace_id,
            "consentRequiredFor": ["hybrid", "cloud"],
            "confirmationPhrase": CONFIRMATION_PHRASE,
            "consent": current.as_dict() if current else None,
        }

    async def grant_egress_consent(
        self,
        *,
        workspace_id: str,
        confirmation: str,
        allowed_modes: tuple[str, ...],
        allowed_field_classes: tuple[str, ...],
        expires_in_days: int,
        maximum_characters: int,
        maximum_items: int,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = EgressConsentService(self.store).grant(
                workspace_id=workspace_id,
                confirmation=confirmation,
                allowed_modes=allowed_modes,
                allowed_field_classes=allowed_field_classes,
                expires_in_days=expires_in_days,
                maximum_characters=maximum_characters,
                maximum_items=maximum_items,
            )
        return value.as_dict()

    async def revoke_egress_consent(
        self, *, workspace_id: str
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            value = EgressConsentService(self.store).revoke(workspace_id)
        return value.as_dict()

    async def egress_projection_preview(
        self,
        *,
        workspace_id: str,
        mode: str,
        purpose: str,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        context = await self.finance_core.load_context(workspace_id, THREAD_ID)
        return EgressConsentService(self.store).preview(
            workspace_id=workspace_id,
            mode=ModelMode(mode),
            purpose=ModelPurpose(purpose),
            source=dict(context.projection),
        )
'''

ROUTE_MODELS = '''

class EgressConsentRequest(RequestModel):
    confirmation: str = Field(min_length=1, max_length=80)
    allowed_modes: list[str] = Field(
        alias="allowedModes", min_length=1, max_length=2
    )
    allowed_field_classes: list[str] = Field(
        alias="allowedFieldClasses", min_length=1, max_length=5
    )
    expires_in_days: int = Field(alias="expiresInDays", ge=1, le=90)
    maximum_characters: int = Field(
        default=32000, alias="maximumCharacters", ge=1, le=32000
    )
    maximum_items: int = Field(default=512, alias="maximumItems", ge=1, le=512)


class EgressPreviewRequest(RequestModel):
    mode: str = Field(pattern=r"^(hybrid|cloud)$")
    purpose: str = Field(pattern=r"^(compile_plan|explain|ask_question)$")
'''

ROUTES = '''    @router.get("/v1/workspaces/{workspace_id}/privacy/egress-consent")
    async def egress_consent_status(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        return dict(await services.egress_consent_status(workspace_id=workspace_id))

    @router.post("/v1/workspaces/{workspace_id}/privacy/egress-consent")
    async def grant_egress_consent(
        workspace_id: PathIdentifier,
        body: EgressConsentRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.grant_egress_consent(
                    workspace_id=workspace_id,
                    confirmation=body.confirmation,
                    allowed_modes=tuple(body.allowed_modes),
                    allowed_field_classes=tuple(body.allowed_field_classes),
                    expires_in_days=body.expires_in_days,
                    maximum_characters=body.maximum_characters,
                    maximum_items=body.maximum_items,
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.post("/v1/workspaces/{workspace_id}/privacy/egress-consent/revoke")
    async def revoke_egress_consent(
        workspace_id: PathIdentifier,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.revoke_egress_consent(workspace_id=workspace_id)
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="no egress consent exists") from exc

    @router.post("/v1/workspaces/{workspace_id}/privacy/egress-preview")
    async def egress_projection_preview(
        workspace_id: PathIdentifier,
        body: EgressPreviewRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.egress_projection_preview(
                    workspace_id=workspace_id,
                    mode=body.mode,
                    purpose=body.purpose,
                )
            )
        except EgressConsentRequired as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

'''

TESTS = '''from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from finance_agent.finance import FinanceEngine
from finance_agent.models.base import ModelMode, ModelPurpose
from finance_agent.models.egress_consent import (
    CONFIRMATION_PHRASE,
    EgressConsentRequired,
    EgressConsentService,
)
from finance_agent.storage import SQLiteStore

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def service(tmp_path: Path) -> EgressConsentService:
    store = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(store).reset_demo(CSV)
    return EgressConsentService(store)


def test_local_mode_never_requires_or_creates_egress_consent(tmp_path: Path) -> None:
    value = service(tmp_path)
    assert value.require(
        workspace_id="ws_koru_studio", mode=ModelMode.LOCAL
    ) is None
    assert value.latest("ws_koru_studio") is None


def test_cloud_and_hybrid_fail_closed_without_active_consent(tmp_path: Path) -> None:
    value = service(tmp_path)
    with pytest.raises(EgressConsentRequired, match="requires explicit"):
        value.require(workspace_id="ws_koru_studio", mode=ModelMode.CLOUD)
    with pytest.raises(EgressConsentRequired):
        value.require(workspace_id="ws_koru_studio", mode=ModelMode.HYBRID)


def test_grant_requires_exact_phrase_and_bounded_typed_scope(tmp_path: Path) -> None:
    value = service(tmp_path)
    with pytest.raises(ValueError, match="phrase"):
        value.grant(
            workspace_id="ws_koru_studio",
            confirmation="yes",
            allowed_modes=("cloud",),
            allowed_field_classes=("aggregate_amounts",),
            expires_in_days=30,
            maximum_characters=1000,
            maximum_items=20,
        )
    consent = value.grant(
        workspace_id="ws_koru_studio",
        confirmation=CONFIRMATION_PHRASE,
        allowed_modes=("hybrid", "cloud"),
        allowed_field_classes=(
            "aggregate_amounts", "finding_labels", "forecast_assumptions",
            "owner_claims", "evidence_labels",
        ),
        expires_in_days=30,
        maximum_characters=32000,
        maximum_items=512,
    )
    assert consent.revision == 1
    assert value.require(
        workspace_id="ws_koru_studio", mode=ModelMode.CLOUD
    ) == consent


def test_preview_exposes_classes_and_counts_without_values(tmp_path: Path) -> None:
    value = service(tmp_path)
    value.grant(
        workspace_id="ws_koru_studio",
        confirmation=CONFIRMATION_PHRASE,
        allowed_modes=("cloud",),
        allowed_field_classes=("aggregate_amounts", "finding_labels"),
        expires_in_days=30,
        maximum_characters=32000,
        maximum_items=512,
    )
    preview = value.preview(
        workspace_id="ws_koru_studio",
        mode=ModelMode.CLOUD,
        purpose=ModelPurpose.EXPLAIN,
        source={
            "aggregate_amounts": {"currentBalanceMinor": 12345, "currency": "NZD"},
            "finding_labels": ["Needs review"],
        },
    )
    assert preview["rawValuesIncluded"] is False
    assert preview["fieldClasses"] == ["aggregate_amounts", "finding_labels"]
    assert "12345" not in str(preview)
    assert "Needs review" not in str(preview)


def test_revocation_is_append_only_and_blocks_future_egress(tmp_path: Path) -> None:
    value = service(tmp_path)
    value.grant(
        workspace_id="ws_koru_studio",
        confirmation=CONFIRMATION_PHRASE,
        allowed_modes=("cloud",),
        allowed_field_classes=("aggregate_amounts",),
        expires_in_days=30,
        maximum_characters=1000,
        maximum_items=20,
    )
    revoked = value.revoke("ws_koru_studio")
    assert revoked.revision == 2
    assert revoked.revoked is True
    with pytest.raises(EgressConsentRequired, match="revoked"):
        value.require(workspace_id="ws_koru_studio", mode=ModelMode.CLOUD)
    rows = value.store.fetch_all(
        "SELECT revision, revoked FROM egress_policy_revisions WHERE workspace_id = ? ORDER BY revision",
        ("ws_koru_studio",),
    )
    assert [(int(row["revision"]), int(row["revoked"])) for row in rows] == [(1, 0), (2, 1)]
'''

INTEGRATION_TEST = '''from __future__ import annotations

from pathlib import Path

import pytest

from finance_agent.api.services import LocalRouteServices
from finance_agent.models.egress_consent import CONFIRMATION_PHRASE, EgressConsentRequired


@pytest.mark.asyncio
async def test_submit_turn_blocks_cloud_before_controller_or_provider_call(tmp_path: Path) -> None:
    services = LocalRouteServices(tmp_path / "folio.sqlite3", auto_seed=True)
    before = len(services.store.fetch_all("SELECT * FROM conversation_turns"))
    with pytest.raises(EgressConsentRequired):
        await services.submit_turn(
            workspace_id="ws_koru_studio",
            thread_id="thr_koru_studio_main",
            turn_id="turn_cloud_without_consent",
            content="What needs my attention?",
            mode="cloud",
        )
    after = len(services.store.fetch_all("SELECT * FROM conversation_turns"))
    assert after == before
    await services.grant_egress_consent(
        workspace_id="ws_koru_studio",
        confirmation=CONFIRMATION_PHRASE,
        allowed_modes=("cloud",),
        allowed_field_classes=(
            "aggregate_amounts", "finding_labels", "forecast_assumptions",
            "owner_claims", "evidence_labels",
        ),
        expires_in_days=30,
        maximum_characters=32000,
        maximum_items=512,
    )
    # The configured provider may still be unavailable. Consent only unlocks the
    # bounded route; it never fabricates provider readiness or a cloud result.
    result = await services.submit_turn(
        workspace_id="ws_koru_studio",
        thread_id="thr_koru_studio_main",
        turn_id="turn_cloud_with_consent",
        content="What needs my attention?",
        mode="cloud",
    )
    assert result["runId"]
    await services.aclose()
'''


def add_migration_and_module() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    content = read(path)
    versions = [int(value) for value in re.findall(r"version=(\d+)", content)]
    version = max(versions) + 1
    closing = content.rfind("\n)")
    if closing < 0:
        raise RuntimeError("MIGRATIONS tuple close not found")
    prefix = content[:closing].rstrip()
    if not prefix.endswith(","):
        prefix += ","
    write(path, prefix + "\n" + MIGRATION.format(version=version) + content[closing:])
    write("services/api/src/finance_agent/models/egress_consent.py", CONSENT)


def update_services() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.models.evaluation import (\n"
    imports = (
        "from finance_agent.models.egress_consent import (\n"
        "    CONFIRMATION_PHRASE,\n"
        "    EgressConsentRequired,\n"
        "    EgressConsentService,\n"
        ")\n"
        "from finance_agent.models.base import ModelPurpose\n"
    )
    if "from finance_agent.models.egress_consent import" not in content:
        if marker not in content:
            raise RuntimeError("model evaluation import marker missing")
        content = content.replace(marker, imports + marker, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "save_invoice_draft", SERVICE_METHODS)
    content = read(path)
    submit_marker = '''        if workspace_id != WORKSPACE_ID or thread_id != THREAD_ID:
            raise KeyError("unknown workspace or thread")
        async with self._lock:
            self.current_mode = ModelMode(mode)
'''
    submit_replacement = '''        if workspace_id != WORKSPACE_ID or thread_id != THREAD_ID:
            raise KeyError("unknown workspace or thread")
        requested_mode = ModelMode(mode)
        EgressConsentService(self.store).require(
            workspace_id=workspace_id, mode=requested_mode
        )
        async with self._lock:
            self.current_mode = requested_mode
'''
    if submit_marker not in content:
        raise RuntimeError("submit_turn consent insertion marker missing")
    content = content.replace(submit_marker, submit_replacement, 1)
    write(path, content)


def update_protocol_and_routes() -> None:
    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def save_invoice_draft(\n"
    addition = '''    async def egress_consent_status(\n        self, *, workspace_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def grant_egress_consent(\n        self, *, workspace_id: str, confirmation: str,\n        allowed_modes: tuple[str, ...], allowed_field_classes: tuple[str, ...],\n        expires_in_days: int, maximum_characters: int, maximum_items: int\n    ) -> Mapping[str, object]: ...\n\n    async def revoke_egress_consent(\n        self, *, workspace_id: str\n    ) -> Mapping[str, object]: ...\n\n    async def egress_projection_preview(\n        self, *, workspace_id: str, mode: str, purpose: str\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("invoice protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    import_marker = "from finance_agent.finance.service import WORKSPACE_ID\n"
    import_line = "from finance_agent.models.egress_consent import EgressConsentRequired\n"
    if import_line not in content:
        if import_marker not in content:
            raise RuntimeError("workspace ID import marker missing")
        content = content.replace(import_marker, import_marker + import_line, 1)
    model_marker = "\n\nclass InvoiceLineRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("InvoiceLineRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODELS + model_marker, 1)
    route_marker = '    @router.get("/v1/workspaces/{workspace_id}/invoices")\n'
    if route_marker not in content:
        raise RuntimeError("invoice route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/models/test_egress_consent.py", TESTS)
    write("services/api/tests/integration/test_egress_consent_route.py", INTEGRATION_TEST)
    write("docs/EGRESS_CONSENT.md", '''# Hybrid and Cloud egress consent\n\nA configured cloud credential does not authorise Folio to send workspace information. Hybrid and Cloud turns require an active append-only consent revision for the current workspace. The owner must type `ALLOW FOLIO CLOUD PROJECTIONS`, select the allowed modes and field classes, set item/character bounds and choose an expiry of no more than 90 days.\n\nA preview compiles the same typed projection policy used by the model harness but returns only field classes, paths and counts. It never returns the underlying values. A request outside the allowed field classes or bounds is not within consent. Revocation appends a new revision and immediately blocks future Hybrid or Cloud turns; prior egress and consent receipts remain auditable.\n\nConsent does not make a provider ready, guarantee a result or permit raw source files, credentials or complete ledger history to leave the computer. Local mode never requires or creates an egress consent record.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 25: explicit typed cloud-egress consent\n\n- A configured credential no longer authorises Hybrid or Cloud use by itself.\n- Consent is workspace-specific, append-only, bounded by mode, field class, items, characters and expiry.\n- Projection preview reveals only classes, paths and counts, not values.\n- Revocation immediately blocks future egress while preserving historical receipts.\n- Local mode never requires or creates cloud consent.\n- Consent does not imply provider readiness, successful inference or permission for raw sources.\n'''
    if "## Stack 25: explicit typed cloud-egress consent" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_and_module()
    update_services()
    update_protocol_and_routes()
    add_tests_and_docs()
    print("egress consent changes applied")


if __name__ == "__main__":
    main()
