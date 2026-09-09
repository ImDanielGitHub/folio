"""Conservative JSON recovery for common local-model response quirks."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from finance_agent.agent.plan import FinancePlan


class PlanParseError(ValueError):
    """No closed FinancePlan could be recovered from model output."""


_FENCE_RE = re.compile(r"^\s*```(?:json|JSON)?\s*(.*?)\s*```\s*$", re.DOTALL)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_WRAPPER_KEYS = ("plan", "result", "output", "response", "arguments", "content")


def raw_parse_plan(text: str) -> FinancePlan:
    """Baseline used by evals: strict JSON only, with no repair or unwrapping."""

    value = json.loads(text)
    return FinancePlan.model_validate(value)


def _balanced_json_objects(text: str) -> list[str]:
    objects: list[str] = []
    start: int | None = None
    depth = 0
    quoted = False
    escaped = False
    for index, character in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
            continue
        if character == '"':
            quoted = True
        elif character == "{":
            if depth == 0:
                start = index
            depth += 1
        elif character == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start : index + 1])
                start = None
    return objects


def _json_candidates(text: str) -> list[str]:
    cleaned = _THINK_RE.sub("", text).strip().replace("\ufeff", "")
    fence_match = _FENCE_RE.match(cleaned)
    if fence_match:
        cleaned = fence_match.group(1).strip()
    candidates = [cleaned]
    candidates.extend(_balanced_json_objects(cleaned))
    repaired = cleaned.translate(str.maketrans("“”’", "\"\"'"))
    repaired = _TRAILING_COMMA_RE.sub(r"\1", repaired)
    if repaired not in candidates:
        candidates.append(repaired)
    candidates.extend(
        candidate
        for candidate in _balanced_json_objects(repaired)
        if candidate not in candidates
    )
    return candidates


def _unwrap(value: Any) -> Any:
    current = value
    for _ in range(5):
        if isinstance(current, dict) and "planVersion" in current:
            return current
        if isinstance(current, str):
            parsed = None
            for candidate in _json_candidates(current):
                try:
                    parsed = json.loads(candidate)
                    break
                except json.JSONDecodeError:
                    continue
            if parsed is None:
                return current
            current = parsed
            continue
        if isinstance(current, dict):
            wrapper = next((key for key in _WRAPPER_KEYS if key in current), None)
            if wrapper is not None:
                current = current[wrapper]
                continue
        return current
    return current


class FinancePlanParser:
    """Recover one candidate, then require the canonical closed contract."""

    def parse(self, text: str) -> FinancePlan:
        errors: list[str] = []
        for candidate in _json_candidates(text):
            try:
                value = _unwrap(json.loads(candidate))
                return FinancePlan.model_validate(value)
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                errors.append(str(exc).splitlines()[0][:240])
        detail = errors[-1] if errors else "no JSON object found"
        raise PlanParseError(f"FinancePlan failed closed: {detail}")
