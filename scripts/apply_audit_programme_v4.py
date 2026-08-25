from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "apply_audit_programme_v3.py"
spec = importlib.util.spec_from_file_location("audit_programme_v3", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load audit programme v3")
programme_v3 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(programme_v3)

path = ROOT / "services/api/tests/finance/test_audit_correctness.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    "from finance_agent.storage import SQLiteConversationStore, SQLiteStore\n",
    "from finance_agent.storage import SQLiteStore\n",
)
content = content.replace(
    "    conversations = SQLiteConversationStore(value.store)\n",
    "",
)
path.write_text(content, encoding="utf-8")

path = ROOT / "services/api/tests/connectors/test_plaid_live.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    '        assert len(services.store.fetch_all("SELECT event_id FROM provider_transaction_events")) == 2\n',
    '        event_rows = services.store.fetch_all(\n'
    '            "SELECT event_id FROM provider_transaction_events"\n'
    '        )\n'
    '        assert len(event_rows) == 2\n',
)
path.write_text(content, encoding="utf-8")

print("Audit programme v4 lint compatibility fixes applied")
