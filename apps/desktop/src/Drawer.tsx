import { useEffect, useRef } from "react";
import { ActivityIcon, CheckIcon, CloseIcon, PrivacyIcon, SourceIcon, TelegramIcon, UndoIcon, WarningIcon } from "./icons";
import { formatDateTime, titleCase } from "./format";
import type { ActivityItem, DrawerKind, ModelMode, SourceItem } from "./types";
import type { BackendHealth } from "./transport";

type DrawerProps = {
  kind: DrawerKind | null;
  sources: SourceItem[];
  activity: ActivityItem[];
  modelMode: ModelMode;
  selectedModel: string;
  backend: BackendHealth;
  telegramImported: boolean;
  onClose: () => void;
  onModeChange: (mode: ModelMode) => void;
  onModelChange: (modelId: string) => void;
  onImportTelegram: () => void;
  onUndo: (eventId: string) => void;
  onReset: () => void;
};

const titles: Record<DrawerKind, { title: string; subtitle: string }> = {
  sources: { title: "Sources", subtitle: "Imports, messages, and bank links — numbers stay local" },
  activity: { title: "Activity & Undo", subtitle: "Background runs and reversible changes" },
  connections: { title: "Connections & Privacy", subtitle: "Local, hybrid, or cloud — with honesty about what leaves" },
};

const models = [
  { id: "qwen-local", label: "Qwen 3.5 9B", detail: "Local · LM Studio · default", mode: "local" as const },
  { id: "llama-local", label: "Llama 3.1 8B", detail: "Local · LM Studio", mode: "local" as const },
  { id: "gpt-cloud", label: "GPT-4.1", detail: "Cloud · only when configured", mode: "cloud" as const },
];

function sourceStatusLabel(status: SourceItem["status"]) {
  if (status === "live") return "Live";
  if (status === "linked") return "Linked";
  return titleCase(status);
}

export function Drawer({
  kind,
  sources,
  activity,
  modelMode,
  selectedModel,
  backend,
  telegramImported,
  onClose,
  onModeChange,
  onModelChange,
  onImportTelegram,
  onUndo,
  onReset,
}: DrawerProps) {
  const panelRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!kind) return;
    const previous = document.activeElement as HTMLElement | null;
    const panel = panelRef.current;
    panel?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
      if (event.key !== "Tab" || !panel) return;
      const focusable = Array.from(panel.querySelectorAll<HTMLElement>("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])"));
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

  return (
    <>
      <button className="drawer-backdrop" aria-label="Close drawer" onClick={onClose} />
      <aside className="context-drawer" ref={panelRef} tabIndex={-1} role="dialog" aria-modal="true" aria-labelledby="drawer-title">
        <header className="drawer-header">
          <div>
            <p className="eyebrow">Workspace detail</p>
            <h2 id="drawer-title">{heading.title}</h2>
            <p>{heading.subtitle}</p>
          </div>
          <button className="icon-button" aria-label="Close drawer" onClick={onClose}><CloseIcon /></button>
        </header>

        <div className="drawer-content">
          {kind === "sources" ? (
            <>
              <section className="drawer-section">
                <div className="drawer-section-title"><h3>Connected evidence</h3><span>{sources.length}</span></div>
                <div className="source-cards">
                  {sources.map((source) => (
                    <article className="source-card" key={source.sourceItemId}>
                      <span className={`source-icon source-${source.sourceType}`}>
                        {source.sourceType === "telegram_fixture" ? <TelegramIcon size={16} /> : <SourceIcon size={16} />}
                      </span>
                      <div>
                        <div className="source-card-title">
                          <strong>{source.label}</strong>
                          <span className={`status-pill status-${source.status}`}>{sourceStatusLabel(source.status)}</span>
                        </div>
                        <p>
                          {source.sourceType === "akahu" ? "Akahu" : source.sourceType === "telegram_fixture" ? "Telegram" : "CSV"}
                          {" · "}
                          {source.rowCount} {source.rowCount === 1 ? "item" : "rows"}
                          {" · "}
                          {formatDateTime(source.receivedAt)}
                        </p>
                        {source.digest ? <code>{source.digest.slice(0, 18)}…</code> : null}
                      </div>
                    </article>
                  ))}
                </div>
              </section>

              <section className="drawer-section telegram-card">
                <div className="telegram-orb"><TelegramIcon size={20} /></div>
                <div>
                  <h3>Receipt from Telegram</h3>
                  <p>“Parking for the client meeting, $32.40. Expense it.” The attachment reference stays local.</p>
                </div>
                <button className="button button-secondary full-width" onClick={onImportTelegram} disabled={telegramImported}>
                  {telegramImported ? <><CheckIcon size={15} /> Receipt processed</> : "Process demo message"}
                </button>
              </section>
            </>
          ) : null}

          {kind === "activity" ? (
            <section className="drawer-section">
              <div className="activity-list">
                {activity.map((item, index) => (
                  <article className="activity-item" key={item.activityId}>
                    <div className="activity-track">
                      <span className={`activity-dot status-${item.status}`}>{item.status === "completed" ? <CheckIcon size={12} /> : <ActivityIcon size={12} />}</span>
                      {index < activity.length - 1 ? <i /> : null}
                    </div>
                    <div className="activity-body">
                      <span className="activity-time">{formatDateTime(item.occurredAt)}</span>
                      <h3>{item.summary}</h3>
                      {item.detail ? <p>{item.detail}</p> : null}
                      <div className="activity-meta"><span>{item.evidenceIds.length} evidence link{item.evidenceIds.length === 1 ? "" : "s"}</span><span>{titleCase(item.kind)}</span></div>
                      {item.undoable && item.eventId && item.status !== "undone" ? (
                        <button className="undo-button" onClick={() => onUndo(item.eventId!)}><UndoIcon size={14} /> Undo this change</button>
                      ) : null}
                      {item.status === "undone" ? <span className="undone-label">Restored · previous guess kept in history</span> : null}
                    </div>
                  </article>
                ))}
              </div>
            </section>
          ) : null}

          {kind === "connections" ? (
            <>
              <section className="drawer-section">
                <div className="drawer-section-title"><h3>Where language runs</h3><span className="local-first-label">Finance maths is always local</span></div>
                <div className="mode-stack" role="radiogroup" aria-label="Model mode">
                  {([
                    ["local", "Local", "LM Studio on this computer", "No model data leaves this device. If local is down, Folio uses deterministic fallback — never silent cloud."],
                    ["hybrid", "Hybrid", "Local finance + cloud language", "Only an allowed typed projection can leave, and only when you choose Hybrid."],
                    ["cloud", "Cloud", "OpenAI for orchestration", "Sources still remain excluded by default. Cloud is never used without this mode."],
                  ] as const).map(([mode, label, subtitle, detail]) => (
                    <button className={`mode-card ${modelMode === mode ? "is-selected" : ""}`} role="radio" aria-checked={modelMode === mode} key={mode} onClick={() => onModeChange(mode)}>
                      <span className="radio-mark">{modelMode === mode ? <i /> : null}</span>
                      <span><strong>{label}</strong><b>{subtitle}</b><small>{detail}</small></span>
                      {mode === "local" ? <span className={`model-ready ${backend.lmStudioReady ? "is-ready" : ""}`}>{backend.lmStudioReady ? "Ready" : titleCase(backend.lmStudioStatus)}</span> : null}
                      {mode === "cloud" ? <span className={`model-ready ${backend.cloudReady ? "is-ready" : ""}`}>{backend.cloudCredentialState === "absent" ? "No API key" : backend.cloudReady ? "Ready" : "Unavailable"}</span> : null}
                    </button>
                  ))}
                </div>
              </section>

              <section className="drawer-section">
                <div className="drawer-section-title"><h3>Models</h3></div>
                <div className="mode-stack" role="radiogroup" aria-label="Model list">
                  {models.map((model) => (
                    <button
                      className={`mode-card ${selectedModel === model.id ? "is-selected" : ""}`}
                      role="radio"
                      aria-checked={selectedModel === model.id}
                      key={model.id}
                      onClick={() => {
                        onModelChange(model.id);
                        if (model.mode === "cloud") onModeChange("cloud");
                        else onModeChange("local");
                      }}
                    >
                      <span className="radio-mark">{selectedModel === model.id ? <i /> : null}</span>
                      <span><strong>{model.label}</strong><b>{model.detail}</b></span>
                      {selectedModel === model.id ? <span className="model-ready is-ready">Selected</span> : null}
                    </button>
                  ))}
                </div>
              </section>

              <section className="drawer-section connection-list">
                <article><span><PrivacyIcon size={17} /></span><div><strong>Local finance service</strong><p>{backend.apiUrl}</p></div><b className={`connection-state state-${backend.mode}`}>{backend.mode === "live" ? "Connected" : "Fixture"}</b></article>
                <article><span><PrivacyIcon size={17} /></span><div><strong>LM Studio</strong><p>Discovered only through its loopback model inventory.</p></div><b className={`connection-state ${backend.lmStudioReady ? "state-live" : "state-off"}`}>{backend.lmStudioReady ? "Ready" : titleCase(backend.lmStudioStatus)}</b></article>
                <article><span><PrivacyIcon size={17} /></span><div><strong>OpenAI</strong><p>Cloud language stays disabled until explicitly configured.</p></div><b className={`connection-state ${backend.cloudCredentialState === "configured" ? "state-live" : "state-off"}`}>{backend.cloudCredentialState === "absent" ? "API key absent" : "Configured"}</b></article>
                <article><span><TelegramIcon size={17} /></span><div><strong>Telegram</strong><p>Fixture path active; live bot is optional.</p></div><b className="connection-state">Demo</b></article>
                <article><span><SourceIcon size={17} /></span><div><strong>Akahu</strong><p>Read-only Open Banking connector.</p></div><b className={`connection-state ${sources.some((source) => source.sourceType === "akahu" && (source.status === "live" || source.status === "processed")) ? "state-live" : "state-off"}`}>{sources.some((source) => source.sourceType === "akahu") ? "Live" : "Off"}</b></article>
              </section>

              <section className="privacy-receipt">
                <div><PrivacyIcon size={18} /><strong>Latest data-use receipt</strong></div>
                <p>{modelMode === "local" ? "No egress. Language and deterministic finance work stayed on this computer." : "Only workspace name, finding labels and aggregate amounts are eligible for the selected language task."}</p>
              </section>

              <section className="drawer-section danger-zone">
                <h3>Demo workspace</h3>
                <p>Return Koru Studio to the same sealed source data and expected totals.</p>
                <button className="text-button" onClick={onReset}>Reset seeded workspace</button>
              </section>
            </>
          ) : null}
        </div>

        <footer className="drawer-footer">
          <span className={`runtime-dot runtime-${backend.mode}`} />
          <div><strong>{backend.label}</strong><span>{backend.detail}</span></div>
          {backend.mode !== "live" ? <WarningIcon size={16} /> : null}
        </footer>
      </aside>
    </>
  );
}
