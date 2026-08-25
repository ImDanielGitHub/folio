from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    content = read(path)
    count = content.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one match, found {count}: {old[:120]!r}")
    write(path, content.replace(old, new, 1))


VITEST_CONFIG = '''import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    setupFiles: ["./tests/accessibility/setup.ts"],
    include: ["tests/accessibility/**/*.test.tsx"],
    restoreMocks: true,
    clearMocks: true,
  },
});
'''

SETUP = '''import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

afterEach(() => cleanup());

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  }),
});
'''

A11Y_TESTS = '''import { axe } from "vitest-axe";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Onboarding } from "../../src/Onboarding";
import { SurfaceRenderer } from "../../src/SurfaceRenderer";
import { livingBriefSurface } from "../../src/fixtures";
import type { BackendHealth } from "../../src/transport";

const backend: BackendHealth = {
  mode: "live",
  label: "Local service connected",
  detail: "Deterministic finance truth is local.",
  apiUrl: "http://127.0.0.1:8787",
  lmStudioReady: true,
  lmStudioStatus: "ready",
  cloudReady: false,
  cloudCredentialState: "absent",
  akahuReady: false,
  akahuStatus: "unconfigured",
  akahuDetail: "Not configured",
  plaidReady: false,
  plaidStatus: "unconfigured",
  plaidDetail: "Not configured",
};

describe("accessible finance surfaces", () => {
  it("renders onboarding without automated accessibility violations", async () => {
    const view = render(<Onboarding backend={backend} onComplete={vi.fn()} />);
    const results = await axe(view.container, {
      rules: { region: { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });

  it("moves keyboard focus through the onboarding choices and action", async () => {
    const user = userEvent.setup();
    render(<Onboarding backend={backend} onComplete={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "What should I look at first?" })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole("button", { name: /Open Folio demo/ })).toHaveFocus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("button", { name: /Preview an Akahu import/ })).toHaveFocus();
    await user.keyboard("{End}");
    expect(screen.getByRole("button", { name: /Choose a local CSV/ })).toHaveFocus();
  });

  it("renders the financial canvas with table and finding semantics", async () => {
    const view = render(
      <SurfaceRenderer surface={livingBriefSurface} onAction={vi.fn()} onFinding={vi.fn()} />,
    );
    const results = await axe(view.container, {
      rules: { region: { enabled: false } },
    });
    expect(results.violations).toEqual([]);
    expect(screen.getByRole("heading", { name: livingBriefSurface.title })).toBeVisible();
    expect(screen.getAllByRole("button").every((button) => button.getAttribute("type") === "button")).toBe(true);
  });
});
'''


def update_dependencies_and_test_gate() -> None:
    path = "apps/desktop/package.json"
    value = json.loads(read(path))
    dev = value["devDependencies"]
    dev.update(
        {
            "@testing-library/jest-dom": "^6.8.0",
            "@testing-library/react": "^16.3.0",
            "@testing-library/user-event": "^14.6.1",
            "jsdom": "^26.1.0",
            "vitest": "^3.2.4",
            "vitest-axe": "^0.1.0",
        }
    )
    scripts = value["scripts"]
    scripts["test:a11y"] = "vitest run --config vitest.config.ts"
    current = scripts.get("test", "")
    if "test:a11y" not in current:
        scripts["test"] = f"{current} && pnpm test:a11y" if current else "pnpm test:a11y"
    write(path, json.dumps(value, indent=2) + "\n")
    write("apps/desktop/vitest.config.ts", VITEST_CONFIG)
    write("apps/desktop/tests/accessibility/setup.ts", SETUP)
    write("apps/desktop/tests/accessibility/accessibility.test.tsx", A11Y_TESTS)


def improve_onboarding_keyboard_and_descriptions() -> None:
    path = "apps/desktop/src/Onboarding.tsx"
    content = read(path)
    content = content.replace(
        'aria-modal="true" aria-labelledby="onboarding-title"',
        'aria-modal="true" aria-labelledby="onboarding-title" aria-describedby="onboarding-description"',
        1,
    )
    content = content.replace(
        '<p className="panel-lede">Open Folio with a private example business, or bring in a local bank export.',
        '<p id="onboarding-description" className="panel-lede">Open Folio with a private example business, or bring in a local bank export.',
        1,
    )
    key_marker = '''      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
'''
    key_replacement = '''      const dialog = dialogRef.current;
      if (!dialog) return;
      const choices = Array.from(dialog.querySelectorAll<HTMLButtonElement>(".choice-card:not(:disabled)"));
      const activeChoiceIndex = choices.indexOf(document.activeElement as HTMLButtonElement);
      if (activeChoiceIndex >= 0 && ["ArrowDown", "ArrowRight", "ArrowUp", "ArrowLeft", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        let next = activeChoiceIndex;
        if (event.key === "ArrowDown" || event.key === "ArrowRight") next = (activeChoiceIndex + 1) % choices.length;
        if (event.key === "ArrowUp" || event.key === "ArrowLeft") next = (activeChoiceIndex - 1 + choices.length) % choices.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = choices.length - 1;
        choices[next]?.focus();
        return;
      }
      if (event.key !== "Tab") return;
'''
    if key_marker not in content:
        raise RuntimeError("Onboarding keyboard marker changed")
    content = content.replace(key_marker, key_replacement, 1)
    content = content.replace(
        'accept=".csv,text/csv"\n                      onChange=',
        'accept=".csv,text/csv"\n                      aria-describedby="csv-format-help"\n                      onChange=',
        1,
    )
    content = content.replace(
        '<small>{csvFile',
        '<small id="csv-format-help">{csvFile',
        1,
    )
    write(path, content)


def improve_surface_semantics() -> None:
    path = "apps/desktop/src/SurfaceRenderer.tsx"
    content = read(path)
    content = content.replace(
        '<table>\n              <thead>\n                <tr><th>Date</th><th>Description</th><th>Prepared as</th><th className="money-cell">Amount</th></tr>',
        '<table>\n              <caption className="sr-only">Transactions included in this prepared financial view</caption>\n              <thead>\n                <tr><th scope="col">Date</th><th scope="col">Description</th><th scope="col">Prepared as</th><th scope="col" className="money-cell">Amount</th></tr>',
        1,
    )
    content = content.replace(
        '<button className={`finding-block severity-${block.severity}`} onClick={() => onFinding(block.findingId)}>',
        '<button type="button" className={`finding-block severity-${block.severity}`} onClick={() => onFinding(block.findingId)} aria-describedby={`finding-summary-${block.findingId}`}>',
        1,
    )
    content = content.replace(
        '<span className="finding-summary">{block.summary}</span>',
        '<span id={`finding-summary-${block.findingId}`} className="finding-summary">{block.summary}</span>',
        1,
    )
    content = content.replace(
        '<button className="message-evidence"',
        '<button type="button" className="message-evidence"',
    )
    content = content.replace(
        '<button className="inline-surface-link"',
        '<button type="button" className="inline-surface-link"',
    )
    content = content.replace(
        '<button onClick={() => onUndo(turn.receipt!.eventId!)}>',
        '<button type="button" onClick={() => onUndo(turn.receipt!.eventId!)}>',
    )
    content = content.replace(
        '<button className={`finding-block',
        '<button type="button" className={`finding-block',
    )
    # Ensure every action button in SurfaceRenderer declares its native type.
    content = content.replace('<button\n', '<button\n          type="button"\n') if False else content
    action_marker = '<button key={action.actionId} className="button button-secondary" onClick={() => onAction(action)}>'
    if action_marker in content:
        content = content.replace(
            action_marker,
            '<button type="button" key={action.actionId} className="button button-secondary" onClick={() => onAction(action)}>',
        )
    write(path, content)


def add_status_semantics_and_css() -> None:
    path = "apps/desktop/src/App.tsx"
    content = read(path)
    content = content.replace(
        '<div className="thread-scroll" ref={threadScrollRef}',
        '<div className="thread-scroll" ref={threadScrollRef} aria-busy={running}',
        1,
    )
    content = content.replace(
        '<div className="investigate-card">',
        '<div className="investigate-card" role="status" aria-live="polite" aria-atomic="true">',
        1,
    )
    content = content.replace(
        '<div className="toast">',
        '<div className="toast" role="status" aria-live="polite">',
        1,
    )
    write(path, content)

    path = "apps/desktop/src/styles.css"
    content = read(path)
    addition = '''

.sr-only {
  position: absolute !important;
  width: 1px !important;
  height: 1px !important;
  padding: 0 !important;
  margin: -1px !important;
  overflow: hidden !important;
  clip: rect(0, 0, 0, 0) !important;
  white-space: nowrap !important;
  border: 0 !important;
}

:focus-visible {
  outline: 3px solid currentColor;
  outline-offset: 3px;
}

@media (prefers-contrast: more) {
  :root {
    --border: currentColor;
  }

  button,
  input,
  details,
  .surface,
  .onboarding-panel,
  .drawer-panel {
    border-width: 2px !important;
  }

  .muted,
  .message-meta,
  .surface-kicker,
  small {
    opacity: 1 !important;
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
  }
}
'''
    if ".sr-only" not in content:
        write(path, content.rstrip() + addition + "\n")


def add_docs() -> None:
    write("docs/ACCESSIBILITY.md", '''# Accessibility acceptance\n\nFolio targets WCAG 2.2 AA for the desktop and browser renderer. Automated axe checks are a floor, not the full proof. Keyboard order, focus visibility, screen-reader naming, reduced motion, high contrast, table semantics, chart alternatives, status announcements, text resizing and responsive overflow must be inspected on real rendered states.\n\nThe cash chart exposes a text description of the first reserve breach and projected low. Finance tables use captions and column headers. Onboarding uses a modal dialog name and description, focus trap, visible focus, and arrow-key navigation through the source choices. Background work surfaces `aria-busy` and polite live status.\n\nRelease acceptance still requires manual VoiceOver or NVDA review on a packaged build. A passing axe test must not be described as complete assistive-technology proof.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 13: accessibility and interaction proof\n\n- Axe checks cover onboarding and representative financial surfaces.\n- Keyboard tests cover initial focus, Tab movement, arrow-key choice navigation, Home and End.\n- Tables have captions and scoped headers; findings expose labelled summaries.\n- Background work and toast status use polite live regions and `aria-busy`.\n- Visible focus, high-contrast and reduced-motion rules are explicit.\n- Accessibility tests are part of the permanent desktop gate, while manual screen-reader packaged-build review remains unclaimed.\n'''
    if "## Stack 13: accessibility and interaction proof" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    update_dependencies_and_test_gate()
    improve_onboarding_keyboard_and_descriptions()
    improve_surface_semantics()
    add_status_semantics_and_css()
    add_docs()
    print("accessibility and interaction changes applied")


if __name__ == "__main__":
    main()
