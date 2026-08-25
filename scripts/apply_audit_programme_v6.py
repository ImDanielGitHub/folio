from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "apply_audit_programme_v5.py"
spec = importlib.util.spec_from_file_location("audit_programme_v5", SOURCE)
if spec is None or spec.loader is None:
    raise RuntimeError("could not load audit programme v5")
programme_v5 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(programme_v5)

# Electron can dispatch IPC events without a sender frame in edge cases. Every
# privileged handler must reject that state rather than dereferencing null or
# weakening sender-origin validation.
path = ROOT / "apps/desktop/src/main/main.ts"
content = path.read_text(encoding="utf-8")
old = "  assertTrustedSender(event.senderFrame.url);\n"
replacement = (
    "  const senderUrl = event.senderFrame?.url;\n"
    "  if (!senderUrl) {\n"
    "    throw new Error(\"Rejected privileged IPC without a sender frame\");\n"
    "  }\n"
    "  assertTrustedSender(senderUrl);\n"
)
if content.count(old) != 2:
    raise RuntimeError("expected exactly two privileged sender-frame checks")
path.write_text(content.replace(old, replacement), encoding="utf-8")

print("Audit programme v6 Electron sender validation fixes applied")
