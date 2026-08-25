from __future__ import annotations

import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "apply_audit_programme.py"
spec = importlib.util.spec_from_file_location("audit_programme", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load audit programme")
programme = importlib.util.module_from_spec(spec)
spec.loader.exec_module(programme)


def corrected_migration() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    content = programme.read(path)
    versions = [int(value) for value in re.findall(r"version=(\d+)", content)]
    next_version = max(versions) + 1
    migration = f'''    Migration(
        version={next_version},
        name="provider_quarantine_and_turn_provenance",
        sql="""
        ALTER TABLE conversation_turns
            ADD COLUMN model_mode TEXT NOT NULL DEFAULT 'local'
            CHECK (model_mode IN ('local', 'hybrid', 'cloud'));

        CREATE TABLE provider_transaction_events (
            event_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            provider TEXT NOT NULL,
            provider_account_id TEXT NOT NULL,
            provider_transaction_id TEXT NOT NULL,
            source_item_id TEXT NOT NULL REFERENCES source_items(source_item_id),
            event_type TEXT NOT NULL CHECK (event_type IN (
                'added', 'modified', 'removed', 'quarantined'
            )),
            occurred_on TEXT,
            description TEXT NOT NULL,
            amount_minor INTEGER,
            currency TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            supersedes_event_id TEXT REFERENCES provider_transaction_events(event_id),
            recorded_at TEXT NOT NULL,
            UNIQUE (workspace_id, provider, event_id)
        );

        CREATE INDEX provider_events_reference
            ON provider_transaction_events(
                workspace_id, provider, provider_account_id, provider_transaction_id, recorded_at
            );
        """,
    ),
'''
    closing = content.rfind("\n)")
    if closing < 0:
        raise RuntimeError("could not locate MIGRATIONS tuple close")
    prefix = content[:closing]
    if not prefix.rstrip().endswith(","):
        prefix = prefix.rstrip() + ",\n"
    programme.write(path, prefix + migration + content[closing:])


_original_harden_api_routes = programme.harden_api_routes


def corrected_harden_api_routes() -> None:
    """Normalise the one inline route signature before applying the base transform."""

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = programme.read(path)
    inline = "    async def artifact(artifact_id: str, services: Services) -> Response:\n"
    multiline = (
        "    async def artifact(\n"
        "        artifact_id: str,\n"
        "        services: Services,\n"
        "    ) -> Response:\n"
    )
    if inline in content:
        programme.write(path, content.replace(inline, multiline, 1))
    _original_harden_api_routes()


_original_preserve_turn_mode = programme.preserve_turn_mode_and_workspace_ownership


def corrected_preserve_turn_mode_and_workspace_ownership() -> None:
    path = "services/api/src/finance_agent/storage/store.py"
    content = programme.read(path)
    compact = (
        "                    turn_id, workspace_id, thread_id, role, content, occurred_at,\n"
        "                    status, evidence_ids_json\n"
    )
    expanded = (
        "                    turn_id, workspace_id, thread_id, role,\n"
        "                    content, occurred_at,\n"
        "                    status, evidence_ids_json\n"
    )
    if compact in content:
        programme.write(path, content.replace(compact, expanded, 1))
    _original_preserve_turn_mode()


programme.add_material_state_migration = corrected_migration
programme.harden_api_routes = corrected_harden_api_routes
programme.preserve_turn_mode_and_workspace_ownership = corrected_preserve_turn_mode_and_workspace_ownership
programme.main()

# Stage timing must use the real run clock at both boundaries.
path = ROOT / "services/api/src/finance_agent/jobs/daily_close.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    "                        DATA_THROUGH,\n                        occurred_at,\n",
    "                        occurred_at,\n                        occurred_at,\n",
)
path.write_text(content, encoding="utf-8")

# The rule test first materialises the imported rows, matching the real workflow.
path = ROOT / "services/api/tests/finance/test_audit_correctness.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    "    assert after_provider != initial\n    value.create_classification_rule(\n",
    "    assert after_provider != initial\n    DailyCloseService(value).run()\n    value.create_classification_rule(\n",
)
path.write_text(content, encoding="utf-8")
