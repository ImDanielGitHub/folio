import { useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIcon,
  CheckIcon,
  CloseIcon,
  PrivacyIcon,
  SourceIcon,
  TelegramIcon,
  UndoIcon,
  WarningIcon,
} from "./icons";
import { formatDateTime, titleCase } from "./format";
import type { ActivityItem, DrawerKind, ModelMode, SourceItem } from "./types";
import type { BackendHealth } from "./transport";

type DrawerProps = {
  kind: DrawerKind | null;
  sources: SourceItem[];
  activity: ActivityItem[];
  modelMode: ModelMode;
  backend: BackendHealth;
  telegramImported: boolean;
  onClose: () => void;
  onModeChange: (mode: ModelMode) => void;
  onImportTelegram: () => void;
  onUndo: (eventId: string) => void;
  onReset: () => void;
};

const titles: Record<DrawerKind, { title: string; subtitle: string }> = {
  sources: {
    title: "Evidence",
    subtitle: "See what Folio used, how it interpreted it, and what changed.",
  },
  activity: {
    title: "Audit trail",
    subtitle: "A readable history of work completed in this workspace.",
  },
  connections: {
    title: "Privacy & models",
    subtitle: "Keep work local, or choose when cloud language help is allowed.",
  },
};

const sourceTypeLabels: Record<SourceItem["sourceType"], string> = {
  csv: "Bank export",
  telegram_fixture: "Telegram message",
  owner_claim: "Owner note",
};

const statusCopy: Record<SourceItem["status"], string> = {
  pending: "Waiting to be processed",
  processed: "Processed and available",
  failed: "Could not be processed",
};

function sourceInterpretation(source: SourceItem): string {
  if (source.status === "failed") {
    return "Folio could not use this source. No claim or ledger change should depend on it until it is processed successfully.";
  }
  if (source.status === "pending") {
    return "This source is retained locally but has not contributed to the workspace yet.";
  }
  if (source.sourceType === "csv") {
    return `Folio recognised ${source.rowCount} ${source.rowCount === 1 ? "ledger row" : "ledger rows"}. Original values remain attached to the imported source so classifications can be traced and corrected.`;
  }
  if (source.sourceType === "telegram_fixture") {
    return "Folio treats the message as owner-supplied context, not independent proof. The linked transaction remains the financial source of truth.";
  }
  return "Folio keeps this as an owner-provided claim. It can explain a decision, but it cannot silently replace source evidence or create a ledger fact.";
}

function sourcePreview(source: SourceItem) {
  if (source.sourceType === "telegram_fixture") {
    return (
      <blockquote className="source-transcript-quote">
        “Parking for the client meeting, $32.40. Expense it.”
      </blockquote>
    );
  }
  if (source.sourceType === "csv") {
    return (
      <div className="source-file-preview" aria-label={`Preview of ${source.label}`}>
        <span className="source-file-type">CSV</span>
        <div>
          <strong>{source.label}</strong>
          <p>{source.rowCount} {source.rowCount === 1 ? "row" : "rows"} retained in the local source file.</p>
        </div>
      </div>
    );
  }
  return (
    <div className="source-note-preview">
      <span>Owner-provided context</span>
      <p>The original wording is retained verbatim in the local evidence record.</p>
    </div>
  );
}

export function Drawer({
  kind,
  sources,
  activity,
  modelMode,
  backend,
  telegramImported,
  onClose,
  onModeChange,
  onImportTelegram,
  onUndo,
  onReset,
}: DrawerProps) {
  const panelRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const [selectedSourceId, setSelectedSourceId] = useState<string | null>(sources.at(0)?.sourceItemId ?? null);

  const selectedSource = useMemo(
    () => sources.find((source) => source.sourceItemId === selectedSourceId) ?? sources.at(0) ?? null,
    [selectedSourceId, sources],
  );

  useEffect(() => {
    const firstSource = sources.at(0);
    if (!firstSource) {
      setSelectedSourceId(null);
      return;
    }
    if (!sources.some((source) => source.sourceItemId === selectedSourceId)) {
      setSelectedSourceId(firstSource.sourceItemId);
    }
  }, [selectedSourceId, sources]);

  useEffect(() => {
    if (!kind) return;
    const previous = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    closeButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
      if (event.key !== "Tab" || !panel) return;
      const focusable = Array.from(
        panel.querySelectorAll<HTMLElement>(
          "button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex='-1'])",
        ),
      );
      if (!focusable.length) return;
      const first = focusable.at(0);
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
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      previous?.focus();
    };
  }, [kind, onClose]);

  if (!kind) return null;
  const heading = titles[kind];
  const cloudAvailability = backend.cloudCredentialState === "absent"
    ? "Not configured"
    : backend.cloudReady
      ? "Ready"
      : "Unavailable";
  const egressPolicy = modelMode === "local"
    ? "Language work stays on this computer. Folio does not send model data to a cloud provider in Local mode."
    : modelMode === "hybrid"
      ? "Finance calculations and source files stay local. Only the minimum typed context needed for an eligible language task may be sent when cloud access is configured."
      : "Finance calculations and source files stay local. Eligible language tasks may use the configured cloud model with a bounded typed projection.";

  return (
    <>
      <button className="drawer-backdrop" tabIndex={-1} aria-label="Close drawer" onClick={onClose} />
      <aside
        className={`context-drawer drawer-${kind}`}
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="drawer-title"
        aria-describedby="drawer-subtitle"
      >
        <header className="drawer-header">
          <div>
            <h2 id="drawer-title">{heading.title}</h2>
            <p id="drawer-subtitle">{heading.subtitle}</p>
          </div>
          <button ref={closeButtonRef} className="icon-button" aria-label={`Close ${heading.title.toLowerCase()}`} onClick={onClose}>
            <CloseIcon />
          </button>
        </header>

        <div className="drawer-content">
          {kind === "sources" ? (
            <>
              <section className="evidence-overview" aria-labelledby="evidence-overview-title">
                <span className="evidence-overview-icon"><SourceIcon size={17} /></span>
                <div>
                  <h3 id="evidence-overview-title">
                    {sources.length} linked {sources.length === 1 ? "source" : "sources"}
                  </h3>
                  <p>
                    {sources.filter((source) => source.status === "processed").length} processed · {sources.reduce((total, source) => total + source.rowCount, 0)} items retained locally
                  </p>
                </div>
              </section>

              {sources.length ? (
                <div className="evidence-workspace">
                  <nav className="evidence-source-list" aria-label="Linked evidence sources">
                    {sources.map((source) => (
                      <button
                        className={`evidence-source-option ${selectedSource?.sourceItemId === source.sourceItemId ? "is-selected" : ""}`}
                        key={source.sourceItemId}
                        type="button"
                        aria-pressed={selectedSource?.sourceItemId === source.sourceItemId}
                        onClick={() => setSelectedSourceId(source.sourceItemId)}
                      >
                        <span className={`source-icon source-${source.sourceType}`} aria-hidden="true">
                          {source.sourceType === "telegram_fixture" ? <TelegramIcon size={15} /> : <SourceIcon size={15} />}
                        </span>
                        <span>
                          <strong>{source.label}</strong>
                          <small>{sourceTypeLabels[source.sourceType]} · {formatDateTime(source.receivedAt)}</small>
                        </span>
                        <i className={`source-status-dot status-${source.status}`} role="img" aria-label={statusCopy[source.status]} />
                      </button>
                    ))}
                  </nav>

                  {selectedSource ? (
                    <article className="evidence-inspector" aria-live="polite">
                      <header className="evidence-inspector-header">
                        <div>
                          <span>{sourceTypeLabels[selectedSource.sourceType]}</span>
                          <h3>{selectedSource.label}</h3>
                        </div>
                        <span className={`status-pill status-${selectedSource.status}`}>{statusCopy[selectedSource.status]}</span>
                      </header>

                      <div className="evidence-inspector-grid">
                        <section className="evidence-preview" aria-labelledby="source-preview-title">
                          <h4 id="source-preview-title">Source</h4>
                          {sourcePreview(selectedSource)}
                          <p className="source-received-at">Received {formatDateTime(selectedSource.receivedAt)}</p>
                        </section>

                        <section className="evidence-interpretation" aria-labelledby="source-interpretation-title">
                          <h4 id="source-interpretation-title">How Folio used it</h4>
                          <p>{sourceInterpretation(selectedSource)}</p>
                          <div className="evidence-history">
                            <span><i className="history-dot" />Source received</span>
                            <span><i className={`history-dot status-${selectedSource.status}`} />{statusCopy[selectedSource.status]}</span>
                          </div>
                        </section>
                      </div>

                      <details className="technical-details">
                        <summary>Technical details</summary>
                        <dl>
                          <div><dt>Source type</dt><dd>{selectedSource.sourceType}</dd></div>
                          <div><dt>Source ID</dt><dd><code>{selectedSource.sourceItemId}</code></dd></div>
                          <div><dt>Rows</dt><dd>{selectedSource.rowCount}</dd></div>
                          {selectedSource.digest ? <div><dt>Content digest</dt><dd><code>{selectedSource.digest}</code></dd></div> : null}
                        </dl>
                      </details>
                    </article>
                  ) : null}
                </div>
              ) : (
                <section className="drawer-empty-state">
                  <SourceIcon size={20} />
                  <h3>No linked evidence yet</h3>
                  <p>Sources will appear here when Folio imports or receives them.</p>
                </section>
              )}

              <section className="telegram-card">
                <div className="telegram-card-copy">
                  <span className="telegram-orb"><TelegramIcon size={18} /></span>
                  <div>
                    <h3>Add the demo receipt message</h3>
                    <p>See how owner context is linked without being treated as independent financial proof.</p>
                  </div>
                </div>
                <button className="button button-secondary" onClick={onImportTelegram} disabled={telegramImported}>
                  {telegramImported ? <><CheckIcon size={15} /> Message processed</> : "Process demo message"}
                </button>
              </section>
            </>
          ) : null}

          {kind === "activity" ? (
            <section className="audit-section" aria-label="Workspace audit trail">
              <div className="audit-summary">
                <ActivityIcon size={17} />
                <p>{activity.length} recorded {activity.length === 1 ? "action" : "actions"}. Reversible changes can be undone here.</p>
              </div>
              {activity.length ? (
                <ol className="audit-list">
                  {activity.map((item, index) => (
                    <li className={`audit-item status-${item.status}`} key={item.activityId}>
                      <div className="audit-track" aria-hidden="true">
                        <span>{item.status === "completed" || item.status === "undone" ? <CheckIcon size={12} /> : <ActivityIcon size={12} />}</span>
                        {index < activity.length - 1 ? <i /> : null}
                      </div>
                      <article className="audit-entry">
                        <header>
                          <div>
                            <h3>{item.summary}</h3>
                            <time dateTime={item.occurredAt}>{formatDateTime(item.occurredAt)}</time>
                          </div>
                          <span className={`audit-status status-${item.status}`}>{titleCase(item.status)}</span>
                        </header>
                        {item.detail ? <p>{item.detail}</p> : null}
                        <p className="audit-evidence-count">
                          {item.evidenceIds.length
                            ? `${item.evidenceIds.length} linked evidence ${item.evidenceIds.length === 1 ? "record" : "records"}`
                            : "No source evidence linked"}
                        </p>
                        {item.undoable && item.eventId && item.status !== "undone" ? (
                          <button className="undo-button" onClick={() => onUndo(item.eventId!)}>
                            <UndoIcon size={14} /> Undo this change
                          </button>
                        ) : null}
                        {item.status === "undone" ? <span className="undone-label">The original change has been reversed</span> : null}

                        <details className="technical-details audit-technical-details">
                          <summary>Technical details</summary>
                          <dl>
                            <div><dt>Action type</dt><dd>{item.kind}</dd></div>
                            <div><dt>Activity ID</dt><dd><code>{item.activityId}</code></dd></div>
                            {item.eventId ? <div><dt>Event ID</dt><dd><code>{item.eventId}</code></dd></div> : null}
                            {item.correlationId ? <div><dt>Run ID</dt><dd><code>{item.correlationId}</code></dd></div> : null}
                            {item.evidenceIds.length ? (
                              <div>
                                <dt>Evidence IDs</dt>
                                <dd className="technical-id-list">{item.evidenceIds.map((id) => <code key={id}>{id}</code>)}</dd>
                              </div>
                            ) : null}
                          </dl>
                        </details>
                      </article>
                    </li>
                  ))}
                </ol>
              ) : (
                <div className="drawer-empty-state">
                  <ActivityIcon size={20} />
                  <h3>No recorded work yet</h3>
                  <p>Completed runs and reversible changes will appear here.</p>
                </div>
              )}
            </section>
          ) : null}

          {kind === "connections" ? (
            <>
              <section className="privacy-summary">
                <span><PrivacyIcon size={18} /></span>
                <div>
                  <h3>Finance stays local</h3>
                  <p>Calculations, source files, and ledger history remain on this computer in every model mode.</p>
                </div>
              </section>

              <section className="drawer-section model-settings" aria-labelledby="model-mode-title">
                <div className="drawer-section-title">
                  <div>
                    <h3 id="model-mode-title">Language model</h3>
                    <p>Choose how Folio handles explanation and conversation.</p>
                  </div>
                </div>
                <div className="mode-stack" role="radiogroup" aria-label="Language model mode">
                  {([
                    ["local", "Local", "Private on this computer", "No cloud model data is sent."],
                    ["hybrid", "Hybrid", "Local finance, optional cloud language", "Only bounded typed context is eligible."],
                    ["cloud", "Cloud", "Cloud language when configured", "Sources and ledger history remain excluded."],
                  ] as const).map(([mode, label, subtitle, detail]) => {
                    const availability = mode === "local"
                      ? (backend.lmStudioReady ? "Ready" : titleCase(backend.lmStudioStatus))
                      : cloudAvailability;
                    const available = mode === "local" ? backend.lmStudioReady : backend.cloudReady;
                    return (
                      <button
                        className={`mode-card ${modelMode === mode ? "is-selected" : ""}`}
                        role="radio"
                        aria-checked={modelMode === mode}
                        key={mode}
                        onClick={() => onModeChange(mode)}
                      >
                        <span className="radio-mark" aria-hidden="true">{modelMode === mode ? <i /> : null}</span>
                        <span className="mode-card-copy">
                          <strong>{label}</strong>
                          <b>{subtitle}</b>
                          <small>{detail}</small>
                        </span>
                        <span className={`model-ready ${available ? "is-ready" : ""}`}>{availability}</span>
                      </button>
                    );
                  })}
                </div>
              </section>

              <section className="privacy-policy-card" aria-labelledby="egress-policy-title">
                <div><PrivacyIcon size={17} /><h3 id="egress-policy-title">Current privacy boundary</h3></div>
                <p>{egressPolicy}</p>
                <span>{modelMode === "local"
                  ? (backend.lmStudioReady ? "Your local model is ready." : `Local model: ${titleCase(backend.lmStudioStatus)}.`)
                  : `Cloud model: ${cloudAvailability.toLowerCase()}.`}</span>
              </section>

              <section className="drawer-section connection-settings" aria-labelledby="connections-title">
                <div className="drawer-section-title"><h3 id="connections-title">Connections</h3></div>
                <div className="connection-list">
                  <article>
                    <span><PrivacyIcon size={17} /></span>
                    <div><strong>Finance service</strong><p>Processes local data and deterministic calculations.</p></div>
                    <b className={`connection-state state-${backend.mode}`}>{backend.mode === "live" ? "Connected" : backend.mode === "fixture" ? "Sealed demo" : backend.mode === "checking" ? "Connecting" : backend.mode === "degraded" ? "Needs attention" : "Offline"}</b>
                  </article>
                  <article>
                    <span><PrivacyIcon size={17} /></span>
                    <div><strong>LM Studio</strong><p>Discovered through this computer only.</p></div>
                    <b className={`connection-state ${backend.lmStudioReady ? "state-live" : "state-off"}`}>{backend.lmStudioReady ? "Ready" : titleCase(backend.lmStudioStatus)}</b>
                  </article>
                  <article>
                    <span><PrivacyIcon size={17} /></span>
                    <div><strong>OpenAI</strong><p>Disabled until you configure cloud access.</p></div>
                    <b className={`connection-state ${backend.cloudCredentialState === "configured" ? "state-live" : "state-off"}`}>{cloudAvailability}</b>
                  </article>
                  <article>
                    <span><TelegramIcon size={17} /></span>
                    <div><strong>Telegram</strong><p>Demo message path available.</p></div>
                    <b className="connection-state">Demo</b>
                  </article>
                  <article>
                    <span><SourceIcon size={17} /></span>
                    <div><strong>Akahu</strong><p>Read-only bank connection.</p></div>
                    <b className="connection-state state-off">Off</b>
                  </article>
                </div>
                <details className="technical-details connection-technical-details">
                  <summary>Technical details</summary>
                  <dl>
                    <div><dt>Finance API</dt><dd><code>{backend.apiUrl}</code></dd></div>
                    <div><dt>Runtime mode</dt><dd>{backend.mode}</dd></div>
                    <div><dt>Local model status</dt><dd>{backend.lmStudioStatus}</dd></div>
                    <div><dt>Cloud credential</dt><dd>{backend.cloudCredentialState}</dd></div>
                  </dl>
                </details>
              </section>

              <section className="drawer-section danger-zone">
                <h3>Demo workspace</h3>
                <p>Return Koru Studio to the original sealed source data and expected totals.</p>
                <button className="text-button" onClick={onReset}>Reset seeded workspace</button>
              </section>
            </>
          ) : null}
        </div>

        <footer className="drawer-footer">
          <span className={`runtime-dot runtime-${backend.mode}`} />
          <div>
            <strong>{backend.mode === "live" ? "Local finance service connected" : backend.mode === "fixture" ? "Using the sealed demo workspace" : backend.mode === "checking" ? "Finding the local finance service" : backend.mode === "degraded" ? "Local service needs attention" : "Working offline"}</strong>
            <span>{backend.mode === "live" ? "Workspace data is available." : backend.mode === "fixture" ? "Fixture evidence is clearly labelled." : "The last committed view remains visible; failed work is not replaced with demo output."}</span>
          </div>
          {backend.mode !== "live" ? <WarningIcon size={16} /> : null}
        </footer>
      </aside>
    </>
  );
}
