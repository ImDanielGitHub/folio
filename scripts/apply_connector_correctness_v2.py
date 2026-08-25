from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "apply_connector_correctness.py"
spec = importlib.util.spec_from_file_location("connector_correctness", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load connector correctness transformation")
programme = importlib.util.module_from_spec(spec)
spec.loader.exec_module(programme)

_original_replace_once = programme.replace_once


def corrected_replace_once(
    content: str,
    old: str,
    new: str,
    *,
    label: str,
) -> str:
    if label in {"Plaid link route error mapping", "Plaid sync route error mapping"}:
        if old not in content:
            raise RuntimeError(f"{label}: expected at least one match")
        return content.replace(old, new, 1)
    return _original_replace_once(content, old, new, label=label)


programme.replace_once = corrected_replace_once
programme.main()
