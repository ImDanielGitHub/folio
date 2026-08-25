from __future__ import annotations

import stat
from pathlib import Path

import pytest
from finance_agent.runtime.session_token import ensure_session_token


def test_creates_private_token_file_and_reuses_it(tmp_path: Path) -> None:
    token_path = tmp_path / "run" / "session-token"

    first, first_created = ensure_session_token(token_path, supplied=None)
    second, second_created = ensure_session_token(token_path, supplied=None)

    assert first_created is True
    assert second_created is False
    assert second == first
    assert token_path.read_text(encoding="utf-8").strip() == first
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_explicit_token_does_not_touch_the_persistent_file(tmp_path: Path) -> None:
    token_path = tmp_path / "run" / "session-token"

    token, created = ensure_session_token(token_path, supplied="  configured-secret-value  ")

    assert token == "configured-secret-value"
    assert created is False
    assert not token_path.exists()


def test_rejects_a_corrupt_persistent_token(tmp_path: Path) -> None:
    token_path = tmp_path / "session-token"
    token_path.write_text("short", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid"):
        ensure_session_token(token_path, supplied=None)
