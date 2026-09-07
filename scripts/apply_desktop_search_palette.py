from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


COMPONENT = '''import { useEffect, useId, useRef, useState } from "react";
import { SearchIcon, CloseIcon, SourceIcon } from "./icons";
import { formatMoney } from "./format";
import { searchWorkspace, type WorkspaceSearchResponse } from "./transport";

export type SearchFunction = (
  query: string,
  options?: { resultTypes?: string[]; maxResults?: number; signal?: AbortSignal },
) => Promise<WorkspaceSearchResponse>;

type SearchPaletteProps = {
  search?: SearchFunction;
};

function resultSubtitle(result: WorkspaceSearchResponse["results"][number]): string {
  const parts = [result.subtitle];
  if (result.amountMinor !== null && result.currency) {
    parts.push(formatMoney(result.amountMinor, result.currency));
  }
  return parts.filter(Boolean).join(" · ");
}

export function SearchPalette({ search = searchWorkspace }: SearchPaletteProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [response, setResponse] = useState<WorkspaceSearchResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const resultsId = useId();

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const shortcut = (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k";
      if (shortcut) {
        event.preventDefault();
        setOpen(true);
      } else if (event.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  useEffect(() => {
    if (!open) return;
    const timer = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, [open]);

  useEffect(() => {
    if (!open || query.trim().length < 2) {
      setResponse(null);
      setLoading(false);
      setError(null);
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setLoading(true);
      setError(null);
      void search(query.trim(), { maxResults: 30, signal: controller.signal })
        .then((value) => setResponse(value))
        .catch((reason: unknown) => {
          if (controller.signal.aborted) return;
          setError(reason instanceof Error ? reason.message : "Local search failed.");
          setResponse(null);
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, 180);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [open, query, search]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const dialog = dialogRef.current;
      if (!dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(
        "button:not(:disabled), input:not(:disabled), [href], [tabindex]:not([tabindex='-1'])",
      ));
      const first = focusable[0];
      const last = focusable.at(-1);
      if (!first || !last) return;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open]);

  const useResult = (result: WorkspaceSearchResponse["results"][number]) => {
    window.dispatchEvent(new CustomEvent("folio:search-result", {
      detail: {
        resultType: result.resultType,
        resultId: result.resultId,
        title: result.title,
      },
    }));
    setOpen(false);
  };

  return (
    <>
      <button
        type="button"
        className="global-search-launcher"
        aria-label="Search this Folio workspace"
        aria-keyshortcuts="Control+K Meta+K"
        onClick={() => setOpen(true)}
      >
        <SearchIcon size={16} />
        <span>Search</span>
        <kbd>⌘K</kbd>
      </button>
      {open ? (
        <div className="search-palette-layer" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setOpen(false);
        }}>
          <div
            ref={dialogRef}
            className="search-palette"
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
          >
            <header>
              <div>
                <p className="eyebrow">Local workspace search</p>
                <h2 id={titleId}>Find finance records and evidence</h2>
              </div>
              <button type="button" aria-label="Close search" onClick={() => setOpen(false)}>
                <CloseIcon size={18} />
              </button>
            </header>
            <label className="search-palette-input">
              <SearchIcon size={17} />
              <span className="sr-only">Search query</span>
              <input
                ref={inputRef}
                type="search"
                value={query}
                placeholder="Transaction, invoice, document or audit ID"
                aria-controls={resultsId}
                aria-describedby={`${resultsId}-status`}
                onChange={(event) => setQuery(event.currentTarget.value)}
              />
            </label>
            <div id={`${resultsId}-status`} className="search-palette-status" role="status" aria-live="polite">
              {loading
                ? "Searching this computer…"
                : error
                  ? error
                  : response
                    ? `${response.resultCount} local result${response.resultCount === 1 ? "" : "s"}`
                    : query.trim().length < 2
                      ? "Type at least two characters. Nothing is sent to a model."
                      : "No result loaded."}
            </div>
            <div id={resultsId} className="search-palette-results" role="listbox" aria-label="Workspace search results">
              {response?.results.map((result) => (
                <button
                  type="button"
                  role="option"
                  aria-selected="false"
                  className="search-result"
                  key={`${result.resultType}:${result.resultId}`}
                  onClick={() => useResult(result)}
                >
                  <span className="search-result-type">{result.resultType.replaceAll("_", " ")}</span>
                  <span className="search-result-copy">
                    <strong>{result.title}</strong>
                    <small>{resultSubtitle(result)}</small>
                    {result.evidenceIds.length ? (
                      <span><SourceIcon size={12} /> {result.evidenceIds.length} linked source{result.evidenceIds.length === 1 ? "" : "s"}</span>
                    ) : null}
                  </span>
                  <span className="search-result-action">Use in conversation</span>
                </button>
              ))}
              {response && response.resultCount === 0 ? (
                <p className="search-empty">No local record matched that query.</p>
              ) : null}
            </div>
            <footer>
              <span>Search is deterministic and local.</span>
              <span>Selecting a result prepares a message; it does not apply a finance change.</span>
            </footer>
          </div>
        </div>
      ) : null}
    </>
  );
}
'''

TEST = '''import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { SearchPalette } from "../../src/SearchPalette";
import type { WorkspaceSearchResponse } from "../../src/transport";

const response: WorkspaceSearchResponse = {
  searchVersion: "folio.workspace-search@1",
  workspaceId: "ws_koru_studio",
  queryHash: "a".repeat(64),
  queryStored: false,
  receiptId: "searchrcpt_test_result",
  resultCount: 1,
  resultTypeCounts: { transaction: 1 },
  results: [{
    resultType: "transaction",
    resultId: "txn_koru_006",
    title: "MITRE 10 HAMILTON",
    subtitle: "2026-07-14 · unresolved",
    occurredAt: "2026-07-14",
    amountMinor: -18475,
    currency: "NZD",
    evidenceIds: ["evd_koru_mitre10_row"],
    scoreBasisPoints: 9000,
    metadata: {},
  }],
  modelUsed: false,
  externalCallsMade: false,
};

describe("SearchPalette", () => {
  it("opens with Cmd/Ctrl+K, searches locally and prepares the selected record", async () => {
    vi.useFakeTimers();
    const search = vi.fn(async () => response);
    const selected = vi.fn();
    window.addEventListener("folio:search-result", selected as EventListener, { once: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<SearchPalette search={search} />);
    await user.keyboard("{Control>}k{/Control}");
    const input = screen.getByRole("searchbox", { name: "Search query" });
    expect(input).toHaveFocus();
    await user.type(input, "MITRE 10");
    await act(async () => vi.advanceTimersByTime(200));
    await waitFor(() => expect(search).toHaveBeenCalledWith(
      "MITRE 10",
      expect.objectContaining({ maxResults: 30 }),
    ));
    expect(await screen.findByText("MITRE 10 HAMILTON")).toBeInTheDocument();
    expect(screen.getByText("-NZD 184.75", { exact: false })).toBeInTheDocument();
    await user.click(screen.getByRole("option", { name: /MITRE 10 HAMILTON/ }));
    expect(selected).toHaveBeenCalledOnce();
    const event = selected.mock.calls[0]![0] as CustomEvent;
    expect(event.detail).toEqual({
      resultType: "transaction",
      resultId: "txn_koru_006",
      title: "MITRE 10 HAMILTON",
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    vi.useRealTimers();
  });

  it("does not search until two characters and closes with Escape", async () => {
    vi.useFakeTimers();
    const search = vi.fn(async () => response);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<SearchPalette search={search} />);
    await user.click(screen.getByRole("button", { name: "Search this Folio workspace" }));
    await user.type(screen.getByRole("searchbox"), "x");
    await act(async () => vi.advanceTimersByTime(250));
    expect(search).not.toHaveBeenCalled();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    vi.useRealTimers();
  });
});
'''

CSS = '''

.global-search-launcher {
  position: fixed;
  right: 22px;
  bottom: 22px;
  z-index: 70;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 42px;
  padding: 0 12px;
  border: 1px solid var(--line-strong);
  border-radius: 12px;
  background: color-mix(in srgb, var(--panel) 92%, transparent);
  box-shadow: 0 14px 38px rgb(0 0 0 / 0.22);
  backdrop-filter: blur(16px);
}
.global-search-launcher kbd {
  padding: 2px 5px;
  border: 1px solid var(--line);
  border-radius: 5px;
  color: var(--text-muted);
  font: inherit;
  font-size: 11px;
}
.search-palette-layer {
  position: fixed;
  inset: 0;
  z-index: 120;
  display: grid;
  place-items: start center;
  padding: min(12vh, 96px) 20px 30px;
  overflow: auto;
  background: rgb(4 7 5 / 0.68);
  backdrop-filter: blur(10px);
}
.search-palette {
  width: min(760px, 100%);
  max-height: min(720px, 80vh);
  display: grid;
  grid-template-rows: auto auto auto minmax(0, 1fr) auto;
  overflow: hidden;
  border: 1px solid var(--line-strong);
  border-radius: 18px;
  background: var(--panel);
  box-shadow: 0 28px 90px rgb(0 0 0 / 0.45);
}
.search-palette > header,
.search-palette > footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 18px;
}
.search-palette > header { border-bottom: 1px solid var(--line); }
.search-palette > header h2 { margin: 2px 0 0; font-size: 20px; }
.search-palette > header button {
  width: 36px;
  height: 36px;
  display: grid;
  place-items: center;
}
.search-palette-input {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 16px 18px 8px;
  padding: 0 13px;
  min-height: 48px;
  border: 1px solid var(--line-strong);
  border-radius: 12px;
  background: var(--surface-soft);
}
.search-palette-input input {
  flex: 1;
  min-width: 0;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: 16px;
}
.search-palette-status {
  min-height: 28px;
  padding: 0 20px 8px;
  color: var(--text-muted);
  font-size: 13px;
}
.search-palette-results {
  overflow: auto;
  overscroll-behavior: contain;
  padding: 0 10px 12px;
}
.search-result {
  width: 100%;
  display: grid;
  grid-template-columns: 100px minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  padding: 13px 10px;
  border: 0;
  border-radius: 10px;
  text-align: left;
  background: transparent;
}
.search-result:hover,
.search-result:focus-visible { background: var(--surface-soft); }
.search-result-type {
  color: var(--text-muted);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.07em;
}
.search-result-copy { display: grid; gap: 4px; min-width: 0; }
.search-result-copy strong,
.search-result-copy small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.search-result-copy small,
.search-result-copy > span { color: var(--text-muted); font-size: 12px; }
.search-result-copy > span { display: inline-flex; align-items: center; gap: 5px; }
.search-result-action { color: var(--accent); font-size: 12px; }
.search-empty { padding: 28px 16px; color: var(--text-muted); text-align: center; }
.search-palette > footer {
  border-top: 1px solid var(--line);
  color: var(--text-muted);
  font-size: 11px;
}
@media (max-width: 680px) {
  .global-search-launcher span,
  .global-search-launcher kbd { display: none; }
  .global-search-launcher { right: 14px; bottom: 14px; width: 44px; padding: 0; justify-content: center; }
  .search-palette-layer { padding: 12px; }
  .search-palette { max-height: calc(100vh - 24px); border-radius: 14px; }
  .search-result { grid-template-columns: 76px minmax(0, 1fr); }
  .search-result-action { grid-column: 2; }
  .search-palette > footer { display: grid; }
}
@media (prefers-reduced-motion: reduce) {
  .search-palette-layer,
  .search-palette { scroll-behavior: auto; }
}
'''


def add_component_test_css() -> None:
    write("apps/desktop/src/SearchPalette.tsx", COMPONENT)
    write("apps/desktop/tests/accessibility/search-palette.test.tsx", TEST)
    path = "apps/desktop/src/styles.css"
    content = read(path)
    if ".global-search-launcher" not in content:
        write(path, content.rstrip() + CSS + "\n")


def update_transport_main_app() -> None:
    path = "apps/desktop/src/transport.ts"
    content = read(path)
    type_marker = "export type BackendHealth = {\n"
    types = '''export type WorkspaceSearchResult = {
  resultType: string;
  resultId: string;
  title: string;
  subtitle: string;
  occurredAt: string | null;
  amountMinor: number | null;
  currency: string | null;
  evidenceIds: string[];
  scoreBasisPoints: number;
  metadata: Record<string, unknown>;
};

export type WorkspaceSearchResponse = {
  searchVersion: "folio.workspace-search@1";
  workspaceId: string;
  queryHash: string;
  queryStored: false;
  receiptId: string;
  resultCount: number;
  resultTypeCounts: Record<string, number>;
  results: WorkspaceSearchResult[];
  modelUsed: false;
  externalCallsMade: false;
};

'''
    if "export type WorkspaceSearchResponse" not in content:
        if type_marker not in content:
            raise RuntimeError("transport BackendHealth marker missing")
        content = content.replace(type_marker, types + type_marker, 1)
    function_marker = "export async function loadSnapshot(workspaceId: string): Promise<WorkspaceSnapshot> {\n"
    function = '''export async function searchWorkspace(
  query: string,
  options: { resultTypes?: string[]; maxResults?: number; signal?: AbortSignal } = {},
): Promise<WorkspaceSearchResponse> {
  const parameters = new URLSearchParams({
    q: query,
    maxResults: String(options.maxResults ?? 30),
  });
  for (const resultType of options.resultTypes ?? []) parameters.append("type", resultType);
  return requestJson<WorkspaceSearchResponse>(
    `/v1/workspaces/ws_koru_studio/search?${parameters.toString()}`,
    { signal: options.signal },
    8000,
  );
}

'''
    if "export async function searchWorkspace" not in content:
        if function_marker not in content:
            raise RuntimeError("loadSnapshot marker missing")
        content = content.replace(function_marker, function + function_marker, 1)
    write(path, content)

    path = "apps/desktop/src/main.tsx"
    content = read(path)
    import_marker = 'import { App } from "./App";\n'
    import_line = 'import { SearchPalette } from "./SearchPalette";\n'
    if import_line not in content:
        if import_marker not in content:
            raise RuntimeError("App import marker missing")
        content = content.replace(import_marker, import_marker + import_line, 1)
    old = "      <App />\n"
    new = "      <App />\n      <SearchPalette />\n"
    if new not in content:
        if old not in content:
            raise RuntimeError("App render marker missing")
        content = content.replace(old, new, 1)
    write(path, content)

    path = "apps/desktop/src/App.tsx"
    content = read(path)
    marker = "  useEffect(() => {\n    let active = true;\n"
    effect = '''  useEffect(() => {
    const onSearchResult = (event: Event) => {
      const detail = (event as CustomEvent<{
        resultType?: string;
        resultId?: string;
        title?: string;
      }>).detail;
      if (!detail?.resultType || !detail.resultId || !detail.title) return;
      setComposer(`Show me the ${detail.resultType.replaceAll("_", " ")} ${detail.resultId}: ${detail.title}`);
      setMobilePane("thread");
      shouldAutoScrollRef.current = true;
      messagesEndRef.current?.scrollIntoView({ behavior: scrollBehaviour(), block: "end" });
    };
    window.addEventListener("folio:search-result", onSearchResult);
    return () => window.removeEventListener("folio:search-result", onSearchResult);
  }, []);

'''
    if 'window.addEventListener("folio:search-result"' not in content:
        if marker not in content:
            raise RuntimeError("App first live refresh effect marker missing")
        content = content.replace(marker, effect + marker, 1)
    write(path, content)


def update_contract_docs() -> None:
    path = "scripts/ui_contract_check.mjs"
    content = read(path)
    addition = '''
const search = await readFile(new URL("../apps/desktop/src/SearchPalette.tsx", import.meta.url), "utf8");
assert.match(search, /aria-keyshortcuts="Control\\+K Meta\\+K"/, "search keyboard shortcut missing");
assert.match(search, /role="dialog"/, "search palette dialog contract missing");
assert.match(search, /Selecting a result prepares a message/, "search mutation boundary copy missing");
'''
    if "search keyboard shortcut missing" not in content:
        content = content.rstrip() + "\n" + addition
    write(path, content)
    write("docs/DESKTOP_SEARCH.md", '''# Desktop local search palette\n\n`Cmd+K` or `Ctrl+K` opens a modal local-search palette. It waits for at least two characters, debounces requests, cancels stale requests and queries the loopback search API only. Results identify their type, show bounded subtitles, retain exact amounts and linked-source counts, and state that no model is used.\n\nSelecting a result dispatches its type, stable ID and title to the main workspace, which prepares a natural message in the existing composer. The owner still chooses whether to send it. Selection does not open a source, change classification, settle an invoice or execute any finance action.\n\nThe launcher has keyboard-shortcut metadata, the modal traps Tab focus, Escape closes it, and mobile layout protects the underlying workspace. Component tests cover shortcut, focus, debounce, local result rendering and the prepare-only boundary.\n''')
    path = "docs/AUDIT_PROGRAMME.md"
    content = read(path)
    section = '''\n\n## Stack 35: accessible desktop local-search palette\n\n- Cmd/Ctrl+K opens an accessible, focus-contained local-search dialog.\n- Debounced requests cancel stale work and never call a model.\n- Typed results show exact money and linked-source counts.\n- Selection prepares a conversation message rather than auto-executing work.\n- Escape, click-away, mobile layout and reduced-motion behaviour are covered.\n- Search remains retrieval, not finance authority.\n'''
    if "## Stack 35: accessible desktop local-search palette" not in content:
        write(path, content.rstrip() + section + "\n")


def main() -> None:
    add_component_test_css()
    update_transport_main_app()
    update_contract_docs()
    print("desktop search palette changes applied")


if __name__ == "__main__":
    main()
