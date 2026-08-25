from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "apply_connector_correctness_v3.py"
spec = importlib.util.spec_from_file_location("connector_correctness_v3", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load connector correctness v3 transformation")
programme = importlib.util.module_from_spec(spec)
spec.loader.exec_module(programme)

path = ROOT / "services/api/src/finance_agent/api/services.py"
content = path.read_text(encoding="utf-8")
content = content.replace(
    "from finance_agent.connectors.plaid import (\n    PlaidReadOnlyAdapter,\n)",
    (
        "from finance_agent.connectors.plaid import (\n"
        "    PlaidReadOnlyAdapter,\n"
        "    PlaidRemovedTransaction,\n"
        ")"
    ),
    1,
)
content = content.replace(
    "        removed_items = []\n",
    "        removed_items: list[PlaidRemovedTransaction] = []\n",
    1,
)
path.write_text(content, encoding="utf-8")
