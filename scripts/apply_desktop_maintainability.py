from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


THREAD_MESSAGE = '''import { memo, useMemo } from "react";
import { BriefIcon, CheckIcon, SourceIcon } from "./icons";
import { formatDateTime } from "./format";
import type { ThreadTurn } from "./types";

type ThreadMessageProps = {
  turn: ThreadTurn;
  appearance?: "lead" | "question" | "standard";
  onEvidence: () => void;
  onUndo: (eventId: string) => void;
  onOpenFinanceView: () => void;
};

export const ThreadMessage = memo(function ThreadMessage({
  turn,
  appearance = "standard",
  onEvidence,
  onUndo,
  onOpenFinanceView,
}: ThreadMessageProps) {
  const offersFinanceView = turn.role === "agent"
    && (appearance === "lead" || /cash|reserve|owner pack|transaction/i.test(turn.content));
  const [leadTitle, leadBody] = useMemo(() => {
    if (appearance !== "lead") return [turn.content, ""];
    const [title, ...remainder] = turn.content.split(/(?<=\\.)\\s+/, 2);
    return [title ?? turn.content, remainder.join(" ")];
  }, [appearance, turn.content]);

  return (
    <article className={`thread-message role-${turn.role} status-${turn.status} appearance-${appearance}`}>
      {turn.role === "agent" ? <span className="agent-avatar" aria-hidden="true">F</span> : null}
      <div className="message-content">
        <div className="message-meta">
          <strong>{turn.role === "agent" ? "Folio" : "You"}</strong>
          <span>{formatDateTime(turn.occurredAt)}</span>
        </div>
        {appearance === "lead" ? (
          <>
            <p className="lead-message-title">{leadTitle}</p>
            {leadBody ? <p className="lead-message-body">{leadBody}</p> : null}
          </>
        ) : <p>{turn.content}</p>}
        {turn.evidenceIds.length ? (
          <button type="button" className="message-evidence" onClick={onEvidence}>
            <SourceIcon size={13} />
            Based on {turn.evidenceIds.length} linked source{turn.evidenceIds.length === 1 ? "" : "s"}
          </button>
        ) : null}
        {turn.receipt ? (
          <div className="inline-receipt">
            <CheckIcon size={13} />
            <span>{turn.receipt.label}</span>
            {turn.receipt.undoable && turn.receipt.eventId ? (
              <button type="button" onClick={() => onUndo(turn.receipt!.eventId!)}>Undo change</button>
            ) : null}
          </div>
        ) : null}
        {offersFinanceView ? (
          <button type="button" className="inline-surface-link" onClick={onOpenFinanceView}>
            View the current picture <BriefIcon size={14} />
          </button>
        ) : null}
      </div>
    </article>
  );
}, (previous, next) => (
  previous.turn === next.turn
  && previous.appearance === next.appearance
  && previous.onEvidence === next.onEvidence
  && previous.onUndo === next.onUndo
  && previous.onOpenFinanceView === next.onOpenFinanceView
));
'''

PERSISTENT_STATE = '''const NUMBER_VERSION = "v1";

export function readBoundedNumber(
  key: string,
  fallback: number,
  minimum: number,
  maximum: number,
): number {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return fallback;
    const candidate = raw.startsWith(`${NUMBER_VERSION}:`)
      ? raw.slice(NUMBER_VERSION.length + 1)
      : raw;
    const parsed = Number(candidate);
    if (!Number.isFinite(parsed)) return fallback;
    return Math.min(maximum, Math.max(minimum, parsed));
  } catch {
    return fallback;
  }
}

export function writeBoundedNumber(
  key: string,
  value: number,
  minimum: number,
  maximum: number,
): number {
  const bounded = Math.min(maximum, Math.max(minimum, value));
  try {
    window.localStorage.setItem(key, `${NUMBER_VERSION}:${bounded}`);
  } catch {
    // The in-memory UI state remains usable when storage is unavailable.
  }
  return bounded;
}
'''

UI_CONTRACT = '''import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const css = await readFile(new URL("../apps/desktop/src/styles.css", import.meta.url), "utf8");
const app = await readFile(new URL("../apps/desktop/src/App.tsx", import.meta.url), "utf8");
const message = await readFile(new URL("../apps/desktop/src/ThreadMessage.tsx", import.meta.url), "utf8");

assert.match(css, /@media\s*\(max-width:\s*900px\)/, "mobile breakpoint missing");
assert.match(css, /prefers-reduced-motion:\s*reduce/, "reduced-motion contract missing");
assert.match(css, /prefers-contrast:\s*more/, "high-contrast contract missing");
assert.match(css, /:focus-visible/, "visible focus contract missing");
assert.match(css, /content-visibility:\s*auto/, "long-list content visibility missing");
assert.doesNotMatch(app, /function ThreadMessage\(/, "ThreadMessage remains embedded in App.tsx");
assert.match(message, /memo\(function ThreadMessage/, "message renderer is not memoised");
assert.match(app, /readBoundedNumber\("folio:thread-width"/, "thread width is not schema-bounded");
console.log("ui:contract PASS (responsive, motion, contrast, focus, split and persistence boundaries)");
'''

THREAD_TEST = '''import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ThreadMessage } from "../../src/ThreadMessage";
import type { ThreadTurn } from "../../src/types";

const turn: ThreadTurn = {
  turnId: "turn_message_test",
  role: "agent",
  content: "Cash is below the protected reserve. Open the current picture for detail.",
  occurredAt: "2026-08-26T08:00:00+12:00",
  status: "complete",
  evidenceIds: ["evd_test_one", "evd_test_two"],
  receipt: {
    label: "Change committed",
    eventId: "evt_test_change",
    undoable: true,
  },
};

describe("ThreadMessage", () => {
  it("exposes evidence, finance view and Undo as native buttons", async () => {
    const user = userEvent.setup();
    const onEvidence = vi.fn();
    const onUndo = vi.fn();
    const onOpenFinanceView = vi.fn();
    render(
      <ThreadMessage
        turn={turn}
        appearance="lead"
        onEvidence={onEvidence}
        onUndo={onUndo}
        onOpenFinanceView={onOpenFinanceView}
      />,
    );
    const evidence = screen.getByRole("button", { name: /Based on 2 linked sources/ });
    const undo = screen.getByRole("button", { name: "Undo change" });
    const view = screen.getByRole("button", { name: /View the current picture/ });
    expect(evidence).toHaveAttribute("type", "button");
    expect(undo).toHaveAttribute("type", "button");
    expect(view).toHaveAttribute("type", "button");
    await user.click(evidence);
    await user.click(undo);
    await user.click(view);
    expect(onEvidence).toHaveBeenCalledOnce();
    expect(onUndo).toHaveBeenCalledWith("evt_test_change");
    expect(onOpenFinanceView).toHaveBeenCalledOnce();
  });
});
'''

PERSISTENT_TEST = '''import { beforeEach, describe, expect, it, vi } from "vitest";

import { readBoundedNumber, writeBoundedNumber } from "../../src/persistentState";

describe("bounded persistent UI numbers", () => {
  beforeEach(() => window.localStorage.clear());

  it("falls back for malformed state and clamps old unversioned values", () => {
    window.localStorage.setItem("width", "not-a-number");
    expect(readBoundedNumber("width", 520, 380, 760)).toBe(520);
    window.localStorage.setItem("width", "9999");
    expect(readBoundedNumber("width", 520, 380, 760)).toBe(760);
  });

  it("writes a versioned bounded value", () => {
    expect(writeBoundedNumber("width", 300, 380, 760)).toBe(380);
    expect(window.localStorage.getItem("width")).toBe("v1:380");
    expect(readBoundedNumber("width", 520, 380, 760)).toBe(380);
  });

  it("keeps the UI usable when browser storage throws", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("storage disabled");
    });
    expect(readBoundedNumber("width", 520, 380, 760)).toBe(520);
  });
});
'''


def split_thread_message() -> None:
    write("apps/desktop/src/ThreadMessage.tsx", THREAD_MESSAGE)
    write("apps/desktop/src/persistentState.ts", PERSISTENT_STATE)
    path = "apps/desktop/src/App.tsx"
    content = read(path)
    import_marker = 'import { SurfaceRenderer } from "./SurfaceRenderer";\n'
    imports = (
        'import { ThreadMessage } from "./ThreadMessage";\n'
        'import { readBoundedNumber, writeBoundedNumber } from "./persistentState";\n'
    )
    if 'from "./ThreadMessage"' not in content:
        if import_marker not in content:
            raise RuntimeError("SurfaceRenderer import marker missing")
        content = content.replace(import_marker, import_marker + imports, 1)
    start = content.find("type ThreadMessageProps = {")
    end = content.find("export function App()")
    if start < 0 or end < 0 or start >= end:
        raise RuntimeError("embedded ThreadMessage block not found")
    content = content[:start] + content[end:]
    old_state = '  const [threadWidth, setThreadWidth] = useState(() => Number(localStorage.getItem("folio:thread-width")) || 520);\n'
    new_state = '  const [threadWidth, setThreadWidth] = useState(() => readBoundedNumber("folio:thread-width", 520, 380, 760));\n'
    if old_state not in content:
        raise RuntimeError("thread width state marker changed")
    content = content.replace(old_state, new_state, 1)
    content = content.replace(
        'localStorage.setItem("folio:thread-width", String(nextWidth));',
        'writeBoundedNumber("folio:thread-width", nextWidth, 380, 760);',
    )
    write(path, content)


def add_ui_contracts() -> None:
    write("scripts/ui_contract_check.mjs", UI_CONTRACT)
    write("apps/desktop/tests/accessibility/thread-message.test.tsx", THREAD_TEST)
    write("apps/desktop/tests/accessibility/persistent-state.test.ts", PERSISTENT_TEST)
    path = "apps/desktop/src/styles.css"
    content = read(path)
    addition = '''

.thread-message,
.activity-item,
.source-list-row,
.table-block tbody tr {
  content-visibility: auto;
  contain-intrinsic-size: auto 88px;
}
'''
    if "contain-intrinsic-size: auto 88px" not in content:
        write(path, content.rstrip() + addition + "\n")

    path = "package.json"
    value = json.loads(read(path))
    scripts = value["scripts"]
    scripts["ui:contract"] = "node scripts/ui_contract_check.mjs"
    if "ui:contract" not in scripts["verify"]:
        scripts["verify"] += " && pnpm ui:contract"
    write(path, json.dumps(value, indent=2) + "\n")


def add_docs() -> None:
    write("docs/DESKTOP_ARCHITECTURE.md", '''# Desktop renderer boundaries\n\nThe desktop remains a chat-first React renderer, but large interaction surfaces are split when they own independent state, behaviour and tests. `ThreadMessage` is a memoised component with explicit callbacks. Persistent dimensions are schema-bounded and versioned rather than trusting arbitrary localStorage strings.\n\nLong conversation, activity, source and table rows use `content-visibility` to avoid unnecessary off-screen rendering. This optimisation must not hide content from keyboard or assistive technology; actual focus and search behaviour remain part of runtime review.\n\n`pnpm ui:contract` protects the mobile breakpoint, reduced motion, high contrast, visible focus, component split and persistence boundary. Component tests cover evidence, Undo and finance-view actions. The contract check is not a substitute for inspecting real mobile/desktop overflow and transition states.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 22: desktop maintainability and interaction performance\n\n- The message renderer moves out of the monolithic App file and is memoised behind explicit callbacks.\n- Persistent thread width is finite, clamped, versioned and failure-tolerant.\n- Focused component tests cover evidence, Undo and finance-view controls.\n- Off-screen conversation, activity, source and table rows use content visibility.\n- A static UI contract protects responsive, motion, contrast and focus rules.\n- Runtime mobile/desktop inspection remains a distinct proof level.\n'''
    if "## Stack 22: desktop maintainability and interaction performance" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    split_thread_message()
    add_ui_contracts()
    add_docs()
    print("desktop maintainability changes applied")


if __name__ == "__main__":
    main()
