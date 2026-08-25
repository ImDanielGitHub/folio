"""Create or recover the private token shared by Folio's managed local processes."""

from __future__ import annotations

import argparse
import os
import secrets
import stat
from collections.abc import Sequence
from pathlib import Path

MIN_TOKEN_LENGTH = 20


def _validate_token(value: str) -> str:
    token = value.strip()
    if len(token) < MIN_TOKEN_LENGTH or any(character.isspace() for character in token):
        raise ValueError("Folio session token is invalid")
    return token


def _read_private_token(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("Folio session token path is invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, encoding="utf-8") as handle:
            descriptor = -1
            return _validate_token(handle.read())
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def ensure_session_token(path: Path, supplied: str | None) -> tuple[str, bool]:
    """Return an explicit token or atomically create/reuse a private managed token."""

    if supplied is not None and supplied.strip():
        return _validate_token(supplied), False

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass

    try:
        return _read_private_token(path), False
    except FileNotFoundError:
        pass

    token = secrets.token_urlsafe(32)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read_private_token(path), False

    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(f"{token}\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    return token, True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve Folio's managed local session token")
    parser.add_argument("path", type=Path)
    arguments = parser.parse_args(argv)
    token, _created = ensure_session_token(
        arguments.path,
        supplied=os.getenv("FOLIO_SESSION_TOKEN"),
    )
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
