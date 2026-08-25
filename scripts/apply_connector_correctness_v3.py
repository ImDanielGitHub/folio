from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "apply_connector_correctness_v2.py"
spec = importlib.util.spec_from_file_location("connector_correctness_v2", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load connector correctness v2 transformation")
programme = importlib.util.module_from_spec(spec)
spec.loader.exec_module(programme)

path = ROOT / "services/api/src/finance_agent/connectors/plaid.py"
content = path.read_text(encoding="utf-8")
old = '''        return PlaidSyncPage(
            added=added,
            modified=modified,
            removed=normalise_removed_transactions(removed_raw),
            next_cursor=raw_cursor.strip() if isinstance(raw_cursor, str) and raw_cursor.strip() else None,
            has_more=raw_has_more,
        )
'''
new = '''        next_cursor = (
            raw_cursor.strip()
            if isinstance(raw_cursor, str) and raw_cursor.strip()
            else None
        )
        return PlaidSyncPage(
            added=added,
            modified=modified,
            removed=normalise_removed_transactions(removed_raw),
            next_cursor=next_cursor,
            has_more=raw_has_more,
        )
'''
if old not in content:
    raise RuntimeError("Plaid cursor formatting target changed unexpectedly")
path.write_text(content.replace(old, new, 1), encoding="utf-8")
