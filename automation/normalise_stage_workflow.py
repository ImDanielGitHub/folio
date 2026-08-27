from __future__ import annotations

import argparse
import re
from pathlib import Path


def add_concurrency(value: str, group: str) -> str:
    if "\nconcurrency:\n" in value:
        return value
    marker = "\njobs:\n"
    if marker not in value:
        raise RuntimeError("workflow has no jobs section")
    block = (
        f"\nconcurrency:\n"
        f"  group: {group}\n"
        "  cancel-in-progress: true\n"
    )
    return value.replace(marker, block + marker, 1)


def replace_wait(value: str, dependency_branch: str | None) -> str:
    pattern = re.compile(
        r"      - name: Wait for [^\n]+\n"
        r"        run: \|\n"
        r".*?"
        r"(?=      - name: Check out the merged product base)",
        re.DOTALL,
    )
    matches = list(pattern.finditer(value))
    if dependency_branch is None:
        if matches:
            raise RuntimeError("root stage unexpectedly has a dependency wait")
        return value
    replacement = f'''      - name: Verify prerequisite stage is merged
        run: |
          count=$(gh pr list --repo "$REPOSITORY" --state merged \\
            --search "head:{dependency_branch}" \\
            --json number --jq 'length' 2>/dev/null || echo 0)
          if [ "$count" -eq 0 ]; then
            echo "Prerequisite {dependency_branch} is not merged." >&2
            exit 1
          fi

'''
    if len(matches) != 1:
        if "Verify prerequisite stage is merged" in value:
            return value
        raise RuntimeError(f"expected one dependency wait, found {len(matches)}")
    return pattern.sub(replacement, value, count=1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workflow")
    parser.add_argument("group")
    parser.add_argument("--dependency")
    arguments = parser.parse_args()
    path = Path(arguments.workflow)
    value = path.read_text(encoding="utf-8")
    value = add_concurrency(value, arguments.group)
    value = replace_wait(value, arguments.dependency)
    path.write_text(value, encoding="utf-8")


if __name__ == "__main__":
    main()
