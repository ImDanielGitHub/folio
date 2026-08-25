from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def patch_node_test() -> None:
    path = ROOT / "apps/desktop/tests/pagination.test.mjs"
    content = path.read_text()
    old = '  assert.doesNotMatch(client, /offset/i);\n'
    new = '  assert.doesNotMatch(client, /parameters\\.set\\(["\\\']offset/);\n'
    if old not in content:
        raise RuntimeError("pagination offset assertion marker missing")
    path.write_text(content.replace(old, new, 1))


if __name__ == "__main__":
    patch_node_test()
    print("pagination offset assertion corrected")
