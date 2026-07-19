import { CheckIcon, CloseIcon, SparkIcon } from "./icons";

export type ToolTraceStep = {
  toolCallId: string;
  toolName: string;
  status: "running" | "completed" | "failed";
  durationMs?: number;
};

type ToolTraceProps = {
  open: boolean;
  steps: ToolTraceStep[];
  onClose: () => void;
};

function friendlyToolName(name: string) {
  return name
    .replace(/[_./]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

export function ToolTrace({ open, steps, onClose }: ToolTraceProps) {
  if (!open) return null;

  return (
    <>
      <button className="drawer-backdrop" aria-label="Close tool trace" onClick={onClose} />
      <aside className="tool-trace-panel" role="dialog" aria-modal="true" aria-labelledby="tool-trace-title">
        <header className="drawer-header">
          <div>
            <p className="eyebrow">Local run</p>
            <h2 id="tool-trace-title">Tool trace</h2>
            <p>Steps Folio took for this answer. Local only — no egress.</p>
          </div>
          <button className="icon-button" aria-label="Close tool trace" onClick={onClose}><CloseIcon /></button>
        </header>

        <div className="drawer-content">
          <section className="drawer-section">
            <div className="tool-trace-list">
              {steps.length === 0 ? (
                <p className="tool-trace-empty">No tool steps yet for this run.</p>
              ) : steps.map((step, index) => (
                <article className={`tool-trace-step status-${step.status}`} key={step.toolCallId}>
                  <span className="tool-trace-index">{step.status === "completed" ? <CheckIcon size={12} /> : <SparkIcon size={12} />}</span>
                  <div>
                    <strong>{friendlyToolName(step.toolName)}</strong>
                    <span>
                      {step.status === "running" ? "Running locally" : step.status === "failed" ? "Failed" : "Completed"}
                      {typeof step.durationMs === "number" ? ` · ${step.durationMs}ms` : ""}
                      {" · no egress"}
                    </span>
                  </div>
                  <b>{String(index + 1).padStart(2, "0")}</b>
                </article>
              ))}
            </div>
          </section>
        </div>

        <footer className="drawer-footer">
          <span className="runtime-dot runtime-live" />
          <div><strong>Local tools</strong><span>Deterministic finance services on this computer</span></div>
        </footer>
      </aside>
    </>
  );
}
