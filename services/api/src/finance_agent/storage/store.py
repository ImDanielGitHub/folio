# ruff: noqa: E501
"""SQLite connection, migration, and shared persistence seams."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from .migrations import MIGRATIONS


def canonical_json(value: Any) -> str:
    """Encode a value deterministically for hashes, receipts, and JSON columns."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


class SQLiteStore:
    """One-workspace SQLite store with explicit transactional boundaries."""

    def __init__(self, database_path: str | Path) -> None:
        raw_path = str(database_path)
        if raw_path == ":memory:":
            self.database_path = raw_path
            return
        resolved = Path(raw_path).expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self.database_path = str(resolved)

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection and always release its file descriptor."""

        connection = self._open_connection()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            applied = {
                int(row["version"])
                for row in connection.execute("SELECT version FROM schema_migrations")
            }
            for migration in MIGRATIONS:
                if migration.version in applied:
                    continue
                connection.executescript(migration.sql)
                connection.execute(
                    "INSERT INTO schema_migrations(version, name) VALUES (?, ?)",
                    (migration.version, migration.name),
                )

    def recreate(self) -> None:
        """Recreate the synthetic demo database for the explicit demo-reset path."""

        with self.connect() as connection:
            connection.execute("PRAGMA foreign_keys = OFF")
            triggers = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
            tables = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
            for row in triggers:
                connection.execute(f'DROP TRIGGER IF EXISTS "{row["name"]}"')
            for row in tables:
                connection.execute(f'DROP TABLE IF EXISTS "{row["name"]}"')
            connection.commit()
        self.migrate()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._open_connection()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def fetch_one(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> sqlite3.Row | None:
        with self.connect() as connection:
            return cast(sqlite3.Row | None, connection.execute(sql, parameters).fetchone())

    def fetch_all(
        self,
        sql: str,
        parameters: Sequence[Any] | Mapping[str, Any] = (),
    ) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return list(connection.execute(sql, parameters).fetchall())

    def record_turn(
        self,
        *,
        turn_id: str,
        workspace_id: str,
        thread_id: str,
        role: str,
        content: str,
        occurred_at: str,
        status: str = "complete",
        evidence_ids: Sequence[str] = (),
        model_mode: str = "local",
    ) -> None:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM conversation_turns WHERE turn_id = ?", (turn_id,)
            ).fetchone()
            if existing is not None:
                expected = (
                    workspace_id,
                    thread_id,
                    role,
                    content,
                )
                actual = (
                    existing["workspace_id"],
                    existing["thread_id"],
                    existing["role"],
                    existing["content"],
                )
                if actual != expected:
                    raise ValueError(f"turn_id is already bound to different content: {turn_id}")
                return
            connection.execute(
                """
                INSERT INTO conversation_turns(
                    turn_id, workspace_id, thread_id, role,
                    content, occurred_at,
                    status, evidence_ids_json, model_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    turn_id,
                    workspace_id,
                    thread_id,
                    role,
                    content,
                    occurred_at,
                    status,
                    canonical_json(list(evidence_ids)),
                    model_mode,
                ),
            )

    def record_claim(self, claim: Mapping[str, Any]) -> None:
        """Persist a typed claim without treating transcript prose as current truth."""

        with self.transaction() as connection:
            supersedes = claim.get("supersedesClaimId")
            if supersedes is not None:
                target = connection.execute(
                    "SELECT workspace_id FROM claims WHERE claim_id = ?",
                    (supersedes,),
                ).fetchone()
                if target is None or str(target["workspace_id"]) != str(claim["workspaceId"]):
                    raise ValueError("superseded claim must belong to the same workspace")
                connection.execute(
                    "UPDATE claims SET status = 'superseded' WHERE claim_id = ? AND workspace_id = ?",
                    (supersedes, claim["workspaceId"]),
                )
            connection.execute(
                """
                INSERT INTO claims(
                    claim_id, workspace_id, claim_type, statement, source_turn_id,
                    scope_json, effective_date, recorded_at, status, supersedes_claim_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    claim["claimId"],
                    claim["workspaceId"],
                    claim["claimType"],
                    claim["statement"],
                    claim["sourceTurnId"],
                    canonical_json(claim["scope"]),
                    claim["effectiveDate"],
                    claim["recordedAt"],
                    claim.get("status", "active"),
                    supersedes,
                ),
            )

    def save_dialogue_frame(self, frame: Mapping[str, Any]) -> None:
        with self.transaction() as connection:
            encoded = canonical_json(frame)
            owner = connection.execute(
                "SELECT workspace_id, thread_id FROM dialogue_frames WHERE frame_id = ?",
                (frame["frameId"],),
            ).fetchone()
            if owner is not None and (
                str(owner["workspace_id"]) != str(frame["workspaceId"])
                or str(owner["thread_id"]) != str(frame["threadId"])
            ):
                raise ValueError("frame_id is already owned by another workspace or thread")
            existing = connection.execute(
                "SELECT frame_json FROM dialogue_frames WHERE frame_id = ?",
                (frame["frameId"],),
            ).fetchone()
            if existing is not None and str(existing["frame_json"]) == encoded:
                return
            connection.execute(
                """
                UPDATE dialogue_frames
                SET is_current = 0
                WHERE workspace_id = ? AND thread_id = ? AND is_current = 1
                """,
                (frame["workspaceId"], frame["threadId"]),
            )
            connection.execute(
                """
                INSERT INTO dialogue_frames(
                    frame_id, workspace_id, thread_id, frame_json,
                    updated_at, is_current, revision
                ) VALUES (?, ?, ?, ?, ?, 1, 1)
                ON CONFLICT(frame_id) DO UPDATE SET
                    frame_json = excluded.frame_json,
                    updated_at = excluded.updated_at,
                    is_current = 1,
                    revision = dialogue_frames.revision + 1
                """,
                (
                    frame["frameId"],
                    frame["workspaceId"],
                    frame["threadId"],
                    encoded,
                    frame["updatedAt"],
                ),
            )

    def current_dialogue_frame(self, workspace_id: str, thread_id: str) -> dict[str, Any] | None:
        row = self.fetch_one(
            """
            SELECT frame_json FROM dialogue_frames
            WHERE workspace_id = ? AND thread_id = ? AND is_current = 1
            """,
            (workspace_id, thread_id),
        )
        return None if row is None else json.loads(row["frame_json"])
