from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def insert_method_before(path: str, class_name: str, before_name: str, method: str) -> None:
    content = read(path)
    tree = ast.parse(content)
    owner = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    before = next(
        node for node in owner.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == before_name
    )
    lines = content.splitlines(keepends=True)
    start = before.lineno - 1
    write(path, "".join(lines[:start]) + method.rstrip() + "\n\n" + "".join(lines[start:]))


MIGRATION = '''    Migration(
        version={version},
        name="outbox_delivery_attempts",
        sql="""
        CREATE TABLE outbox_delivery_attempts (
            attempt_id TEXT PRIMARY KEY,
            outbox_id TEXT NOT NULL REFERENCES outbox_messages(outbox_id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            channel TEXT NOT NULL CHECK (channel = 'local_notification'),
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL CHECK (
                status IN ('attempted', 'delivered', 'failed', 'abandoned')
            ),
            failure_json TEXT,
            UNIQUE (outbox_id, attempt_id)
        );

        CREATE INDEX outbox_delivery_attempts_pending
            ON outbox_delivery_attempts(workspace_id, status, started_at);
        """,
    ),
'''

OUTBOX = '''"""Atomic claim and truthful completion of local notification outbox work."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from finance_agent.storage import SQLiteStore, canonical_json

CLAIM_TIMEOUT = timedelta(minutes=5)
MAX_TITLE = 120
MAX_BODY = 500


def _now() -> datetime:
    return datetime.now(UTC)


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\\0".join(parts).encode()).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _notification_text(payload: dict[str, Any]) -> tuple[str, str]:
    title = str(payload.get("title") or "Folio needs your attention").strip()
    body = str(
        payload.get("body")
        or payload.get("summary")
        or "Open Folio to review the latest prepared finance update."
    ).strip()
    return title[:MAX_TITLE], body[:MAX_BODY]


@dataclass(frozen=True, slots=True)
class ClaimedNotification:
    attempt_id: str
    outbox_id: str
    workspace_id: str
    title: str
    body: str
    created_at: str
    evidence_ids: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "attemptId": self.attempt_id,
            "outboxId": self.outbox_id,
            "workspaceId": self.workspace_id,
            "title": self.title,
            "body": self.body,
            "createdAt": self.created_at,
            "evidenceIds": list(self.evidence_ids),
        }


class LocalNotificationOutbox:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def claim(
        self,
        *,
        workspace_id: str,
        limit: int = 3,
        now: datetime | None = None,
    ) -> tuple[ClaimedNotification, ...]:
        if not 1 <= limit <= 10:
            raise ValueError("notification claim limit must be between 1 and 10")
        current = (now or _now()).astimezone(UTC)
        stale_before = (current - CLAIM_TIMEOUT).isoformat()
        claims: list[ClaimedNotification] = []
        with self.store.transaction() as connection:
            stale = connection.execute(
                """
                SELECT a.attempt_id, a.outbox_id
                FROM outbox_delivery_attempts a
                JOIN outbox_messages o ON o.outbox_id = a.outbox_id
                WHERE a.workspace_id = ? AND a.status = 'attempted'
                  AND a.started_at < ? AND o.status = 'attempted'
                """,
                (workspace_id, stale_before),
            ).fetchall()
            for row in stale:
                connection.execute(
                    """
                    UPDATE outbox_delivery_attempts
                    SET status = 'abandoned', completed_at = ?,
                        failure_json = ?
                    WHERE attempt_id = ? AND status = 'attempted'
                    """,
                    (
                        current.isoformat(),
                        canonical_json({"code": "claim_timeout", "retryable": True}),
                        row["attempt_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE outbox_messages SET status = 'queued'
                    WHERE outbox_id = ? AND status = 'attempted'
                    """,
                    (row["outbox_id"],),
                )
            rows = connection.execute(
                """
                SELECT * FROM outbox_messages
                WHERE workspace_id = ? AND status IN ('queued', 'failed')
                ORDER BY created_at, outbox_id LIMIT ?
                """,
                (workspace_id, limit),
            ).fetchall()
            for row in rows:
                payload = json.loads(str(row["payload_json"]))
                title, body = _notification_text(payload)
                attempt_id = _stable_id(
                    "notifyattempt",
                    str(row["outbox_id"]),
                    current.isoformat(),
                )
                connection.execute(
                    """
                    INSERT INTO outbox_delivery_attempts(
                        attempt_id, outbox_id, workspace_id, channel,
                        started_at, status
                    ) VALUES (?, ?, ?, 'local_notification', ?, 'attempted')
                    """,
                    (
                        attempt_id,
                        row["outbox_id"],
                        workspace_id,
                        current.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    UPDATE outbox_messages
                    SET status = 'attempted', attempted_at = ?, failure_json = NULL
                    WHERE outbox_id = ?
                    """,
                    (current.isoformat(), row["outbox_id"]),
                )
                claims.append(
                    ClaimedNotification(
                        attempt_id=attempt_id,
                        outbox_id=str(row["outbox_id"]),
                        workspace_id=workspace_id,
                        title=title,
                        body=body,
                        created_at=str(row["created_at"]),
                        evidence_ids=tuple(json.loads(str(row["evidence_ids_json"]))),
                    )
                )
        return tuple(claims)

    def complete(
        self,
        *,
        workspace_id: str,
        attempt_id: str,
        delivered: bool,
        failure_code: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, object]:
        current = (now or _now()).astimezone(UTC).isoformat()
        with self.store.transaction() as connection:
            attempt = connection.execute(
                """
                SELECT * FROM outbox_delivery_attempts
                WHERE attempt_id = ? AND workspace_id = ?
                """,
                (attempt_id, workspace_id),
            ).fetchone()
            if attempt is None:
                raise KeyError(attempt_id)
            if str(attempt["status"]) in {"delivered", "failed"}:
                return {
                    "attemptId": attempt_id,
                    "outboxId": str(attempt["outbox_id"]),
                    "status": str(attempt["status"]),
                }
            if str(attempt["status"]) != "attempted":
                raise ValueError("notification attempt is no longer active")
            failure = None if delivered else {
                "code": failure_code or "native_notification_failed",
                "retryable": True,
            }
            final_status = "delivered" if delivered else "failed"
            connection.execute(
                """
                UPDATE outbox_delivery_attempts
                SET status = ?, completed_at = ?, failure_json = ?
                WHERE attempt_id = ?
                """,
                (
                    final_status,
                    current,
                    canonical_json(failure) if failure else None,
                    attempt_id,
                ),
            )
            connection.execute(
                """
                UPDATE outbox_messages
                SET status = ?, delivered_at = ?, failure_json = ?
                WHERE outbox_id = ?
                """,
                (
                    final_status,
                    current if delivered else None,
                    canonical_json(failure) if failure else None,
                    attempt["outbox_id"],
                ),
            )
        return {
            "attemptId": attempt_id,
            "outboxId": str(attempt["outbox_id"]),
            "status": final_status,
        }
'''

SERVICE_METHODS = '''    async def claim_local_notifications(
        self, *, workspace_id: str, limit: int
    ) -> tuple[Mapping[str, object], ...]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            values = LocalNotificationOutbox(self.store).claim(
                workspace_id=workspace_id, limit=limit
            )
        return tuple(value.as_dict() for value in values)

    async def complete_local_notification(
        self,
        *,
        workspace_id: str,
        attempt_id: str,
        delivered: bool,
        failure_code: str | None,
    ) -> Mapping[str, object]:
        if workspace_id != WORKSPACE_ID or self._workspace_destroyed:
            raise KeyError(workspace_id)
        async with self._lock:
            return LocalNotificationOutbox(self.store).complete(
                workspace_id=workspace_id,
                attempt_id=attempt_id,
                delivered=delivered,
                failure_code=failure_code,
            )
'''

ROUTE_MODEL = '''

class CompleteNotificationRequest(RequestModel):
    attempt_id: str = Field(alias="attemptId", pattern=IDENTIFIER_PATTERN)
    delivered: bool
    failure_code: str | None = Field(
        default=None, alias="failureCode", min_length=1, max_length=100
    )
'''

ROUTES = '''    @router.post("/v1/workspaces/{workspace_id}/notifications/claim")
    async def claim_local_notifications(
        workspace_id: PathIdentifier,
        services: Services,
        limit: Annotated[int, Query(ge=1, le=10)] = 3,
    ) -> dict[str, object]:
        notifications = await services.claim_local_notifications(
            workspace_id=workspace_id, limit=limit
        )
        return {"workspaceId": workspace_id, "notifications": list(notifications)}

    @router.post("/v1/workspaces/{workspace_id}/notifications/complete")
    async def complete_local_notification(
        workspace_id: PathIdentifier,
        body: CompleteNotificationRequest,
        services: Services,
    ) -> dict[str, object]:
        try:
            return dict(
                await services.complete_local_notification(
                    workspace_id=workspace_id,
                    attempt_id=body.attempt_id,
                    delivered=body.delivered,
                    failure_code=body.failure_code,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="notification attempt not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

'''

NOTIFICATIONS_TS = '''import { useEffect } from "react";
import {
  claimLocalNotifications,
  completeLocalNotification,
  type RuntimeMode,
} from "./transport";

const POLL_INTERVAL_MS = 30_000;

export function useLocalNotifications(mode: RuntimeMode): void {
  useEffect(() => {
    if (mode !== "live" || !window.financeDesktop?.notify) return;
    let active = true;
    let running = false;

    const poll = async () => {
      if (!active || running) return;
      running = true;
      try {
        const notifications = await claimLocalNotifications();
        for (const notification of notifications) {
          if (!active) break;
          let delivered = false;
          let failureCode: string | undefined;
          try {
            delivered = await window.financeDesktop.notify({
              title: notification.title,
              body: notification.body,
            });
            if (!delivered) failureCode = "native_notifications_unsupported";
          } catch {
            failureCode = "native_notification_exception";
          }
          await completeLocalNotification(
            notification.attemptId,
            delivered,
            failureCode,
          );
        }
      } catch {
        // The outbox remains queued or becomes retryable; no delivery is claimed.
      } finally {
        running = false;
      }
    };

    void poll();
    const timer = window.setInterval(() => void poll(), POLL_INTERVAL_MS);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [mode]);
}
'''

TRANSPORT_TS = '''

export type LocalNotificationClaim = {
  attemptId: string;
  outboxId: string;
  workspaceId: string;
  title: string;
  body: string;
  createdAt: string;
  evidenceIds: string[];
};

export async function claimLocalNotifications(): Promise<LocalNotificationClaim[]> {
  const value = await requestJson<{ notifications?: LocalNotificationClaim[] }>(
    "/v1/workspaces/ws_koru_studio/notifications/claim?limit=3",
    { method: "POST", body: JSON.stringify({}) },
    5000,
  );
  return Array.isArray(value.notifications) ? value.notifications : [];
}

export async function completeLocalNotification(
  attemptId: string,
  delivered: boolean,
  failureCode?: string,
): Promise<Record<string, unknown>> {
  return requestJson("/v1/workspaces/ws_koru_studio/notifications/complete", {
    method: "POST",
    body: JSON.stringify({
      attemptId,
      delivered,
      ...(failureCode ? { failureCode } : {}),
    }),
  }, 5000);
}
'''

TESTS = '''from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from finance_agent.finance import FinanceEngine
from finance_agent.storage import SQLiteStore, canonical_json
from finance_agent.storage.outbox import LocalNotificationOutbox

ROOT = Path(__file__).resolve().parents[4]
CSV = ROOT / "fixtures" / "demo" / "koru-studio-bank-2026-07.csv"


def store(tmp_path: Path) -> SQLiteStore:
    value = SQLiteStore(tmp_path / "folio.sqlite3")
    FinanceEngine(value).reset_demo(CSV)
    with value.transaction() as connection:
        connection.execute(
            """
            INSERT INTO outbox_messages(
                outbox_id, workspace_id, kind, payload_json, status,
                idempotency_key, correlation_id, evidence_ids_json,
                state_revision, created_at
            ) VALUES (?, ?, 'reserve_risk_brief', ?, 'queued', ?, ?, ?, 1, ?)
            """,
            (
                "outbox_notify_test",
                "ws_koru_studio",
                canonical_json({
                    "title": "Reserve risk",
                    "body": "Open Folio to review the projected shortfall.",
                }),
                "notify-test",
                "corr-notify-test",
                canonical_json(["evd_koru_bank_csv"]),
                "2026-08-26T08:00:00+00:00",
            ),
        )
    return value


def test_claim_is_atomic_and_delivery_is_only_recorded_after_completion(tmp_path: Path) -> None:
    value = store(tmp_path)
    outbox = LocalNotificationOutbox(value)
    claims = outbox.claim(workspace_id="ws_koru_studio")
    assert len(claims) == 1
    assert outbox.claim(workspace_id="ws_koru_studio") == ()
    message = value.fetch_one(
        "SELECT status, delivered_at FROM outbox_messages WHERE outbox_id = ?",
        ("outbox_notify_test",),
    )
    assert str(message["status"]) == "attempted"
    assert message["delivered_at"] is None
    completed = outbox.complete(
        workspace_id="ws_koru_studio",
        attempt_id=claims[0].attempt_id,
        delivered=True,
    )
    assert completed["status"] == "delivered"
    message = value.fetch_one(
        "SELECT status, delivered_at FROM outbox_messages WHERE outbox_id = ?",
        ("outbox_notify_test",),
    )
    assert str(message["status"]) == "delivered"
    assert message["delivered_at"] is not None


def test_failed_native_notification_remains_retryable(tmp_path: Path) -> None:
    value = store(tmp_path)
    outbox = LocalNotificationOutbox(value)
    claim = outbox.claim(workspace_id="ws_koru_studio")[0]
    result = outbox.complete(
        workspace_id="ws_koru_studio",
        attempt_id=claim.attempt_id,
        delivered=False,
        failure_code="native_notifications_unsupported",
    )
    assert result["status"] == "failed"
    retry = outbox.claim(workspace_id="ws_koru_studio")
    assert len(retry) == 1
    assert retry[0].attempt_id != claim.attempt_id


def test_stale_attempt_is_abandoned_before_reclaim(tmp_path: Path) -> None:
    value = store(tmp_path)
    outbox = LocalNotificationOutbox(value)
    now = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
    claim = outbox.claim(workspace_id="ws_koru_studio", now=now)[0]
    reclaimed = outbox.claim(
        workspace_id="ws_koru_studio",
        now=now + timedelta(minutes=6),
    )
    assert len(reclaimed) == 1
    old = value.fetch_one(
        "SELECT status FROM outbox_delivery_attempts WHERE attempt_id = ?",
        (claim.attempt_id,),
    )
    assert str(old["status"]) == "abandoned"
'''

ELECTRON_TEST = '''import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const main = await readFile(new URL("../src/main/main.ts", import.meta.url), "utf8");
const preload = await readFile(new URL("../src/preload/preload.cts", import.meta.url), "utf8");

test("native notification IPC is sender-checked and bounded", () => {
  assert.match(main, /finance:notify/);
  assert.match(main, /senderFrame/);
  assert.match(main, /Notification\.isSupported/);
  assert.match(main, /slice\(0, 120\)/);
  assert.match(main, /slice\(0, 500\)/);
  assert.match(preload, /finance:notify/);
});
'''


def add_migration_and_module() -> None:
    path = "services/api/src/finance_agent/storage/migrations.py"
    content = read(path)
    versions = [int(value) for value in re.findall(r"version=(\d+)", content)]
    version = max(versions) + 1
    closing = content.rfind("\n)")
    if closing < 0:
        raise RuntimeError("MIGRATIONS tuple close not found")
    prefix = content[:closing].rstrip()
    if not prefix.endswith(","):
        prefix += ","
    write(path, prefix + "\n" + MIGRATION.format(version=version) + content[closing:])
    write("services/api/src/finance_agent/storage/outbox.py", OUTBOX)


def update_backend() -> None:
    path = "services/api/src/finance_agent/api/services.py"
    content = read(path)
    marker = "from finance_agent.storage.privacy import PrivacyManager, destroyed_marker_path\n"
    import_line = "from finance_agent.storage.outbox import LocalNotificationOutbox\n"
    if import_line not in content:
        if marker not in content:
            raise RuntimeError("privacy import marker missing")
        content = content.replace(marker, marker + import_line, 1)
        write(path, content)
    insert_method_before(path, "LocalRouteServices", "ingest_document", SERVICE_METHODS)

    path = "services/api/src/finance_agent/api/routes/dependencies.py"
    content = read(path)
    marker = "    async def ingest_document(\n"
    addition = '''    async def claim_local_notifications(\n        self, *, workspace_id: str, limit: int\n    ) -> tuple[Mapping[str, object], ...]: ...\n\n    async def complete_local_notification(\n        self, *, workspace_id: str, attempt_id: str, delivered: bool,\n        failure_code: str | None\n    ) -> Mapping[str, object]: ...\n\n'''
    if marker not in content:
        raise RuntimeError("document protocol marker missing")
    content = content.replace(marker, addition + marker, 1)
    write(path, content)

    path = "services/api/src/finance_agent/api/routes/router.py"
    content = read(path)
    model_marker = "\n\nclass GSTMappingRequest(RequestModel):"
    if model_marker not in content:
        raise RuntimeError("GSTMappingRequest marker missing")
    content = content.replace(model_marker, ROUTE_MODEL + model_marker, 1)
    route_marker = '    @router.post("/v1/workspaces/{workspace_id}/documents", status_code=201)\n'
    if route_marker not in content:
        raise RuntimeError("document route marker missing")
    content = content.replace(route_marker, ROUTES + route_marker, 1)
    write(path, content)


def update_electron() -> None:
    path = "apps/desktop/src/main/main.ts"
    content = read(path)
    content = re.sub(
        r'import \{([^}]+)\} from "electron";',
        lambda match: 'import {' + (
            match.group(1) if "Notification" in match.group(1)
            else match.group(1).rstrip() + ", Notification"
        ) + '} from "electron";',
        content,
        count=1,
    )
    handler = '''
ipcMain.handle("finance:notify", async (event, value: { title?: unknown; body?: unknown }) => {
  const source = event.senderFrame?.url ?? "";
  const trusted = source.startsWith("app://folio/")
    || source.startsWith("http://127.0.0.1:4173/")
    || source.startsWith("http://localhost:4173/");
  if (!trusted) throw new Error("Untrusted notification IPC sender");
  if (!Notification.isSupported()) return false;
  const title = String(value?.title ?? "Folio").trim().slice(0, 120);
  const body = String(value?.body ?? "").trim().slice(0, 500);
  if (!title || !body) return false;
  const notification = new Notification({ title, body, silent: false });
  notification.show();
  return true;
});
'''
    marker = "\napp.whenReady().then(async () => {"
    if "finance:notify" not in content:
        if marker not in content:
            raise RuntimeError("app.whenReady marker missing")
        content = content.replace(marker, handler + marker, 1)
    write(path, content)

    path = "apps/desktop/src/preload/preload.cts"
    content = read(path)
    marker = '  openArtifact: (artifactId: string) =>\n    ipcRenderer.invoke("finance:open-artifact", artifactId),\n'
    addition = marker + '  notify: (value: { title: string; body: string }) =>\n    ipcRenderer.invoke("finance:notify", value),\n'
    if "finance:notify" not in content:
        if marker not in content:
            raise RuntimeError("preload openArtifact marker missing")
        content = content.replace(marker, addition, 1)
    write(path, content)

    path = "apps/desktop/src/vite-env.d.ts"
    content = read(path)
    if "notify:" not in content:
        content = content.replace(
            "      openArtifact: (artifactId: string) => Promise<boolean>;\n",
            "      openArtifact: (artifactId: string) => Promise<boolean>;\n"
            "      notify: (value: { title: string; body: string }) => Promise<boolean>;\n",
            1,
        )
    write(path, content)


def update_renderer() -> None:
    write("apps/desktop/src/notifications.ts", NOTIFICATIONS_TS)
    path = "apps/desktop/src/transport.ts"
    content = read(path)
    marker = "\nexport { API_URL };\n"
    if marker not in content:
        raise RuntimeError("transport export marker missing")
    content = content.replace(marker, TRANSPORT_TS + marker, 1)
    write(path, content)

    path = "apps/desktop/src/App.tsx"
    content = read(path)
    import_marker = 'import { Onboarding } from "./Onboarding";\n'
    import_line = 'import { useLocalNotifications } from "./notifications";\n'
    if import_line not in content:
        if import_marker not in content:
            raise RuntimeError("Onboarding import marker missing")
        content = content.replace(import_marker, import_marker + import_line, 1)
    state_marker = "  const [backend, setBackend] = useState(initialBackend);\n"
    if "useLocalNotifications(backend.mode);" not in content:
        if state_marker not in content:
            raise RuntimeError("backend state marker missing")
        content = content.replace(
            state_marker,
            state_marker + "  useLocalNotifications(backend.mode);\n",
            1,
        )
    write(path, content)


def add_tests_and_docs() -> None:
    write("services/api/tests/storage/test_local_notification_outbox.py", TESTS)
    write("apps/desktop/tests/electron-notification-security.test.mjs", ELECTRON_TEST)
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 15: truthful local notification delivery\n\n- Outbox messages are claimed atomically and each attempt has its own durable row.\n- A message remains `attempted` until Electron confirms a native notification was shown.\n- Failed native delivery remains retryable and is never labelled delivered.\n- Abandoned claims become retryable after a bounded timeout.\n- IPC verifies the renderer origin, bounds title/body length and checks OS support.\n- The desktop polls only while the live local service is connected.\n'''
    if "## Stack 15: truthful local notification delivery" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_migration_and_module()
    update_backend()
    update_electron()
    update_renderer()
    add_tests_and_docs()
    print("local notification outbox changes applied")


if __name__ == "__main__":
    main()
