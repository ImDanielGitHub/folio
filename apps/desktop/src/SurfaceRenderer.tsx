import { useMemo } from "react";
import { ArrowIcon, CheckIcon, DownloadIcon, LinkIcon, UndoIcon, WarningIcon } from "./icons";
import { formatDate, formatDateTime, formatMoney, shortHash, titleCase } from "./format";
import {
  validateSurfaceSpec,
  type CashSeriesBlock,
  type FinanceAction,
  type FinanceBlock,
  type FinanceSurfaceSpec,
} from "./types";

type SurfaceRendererProps = {
  surface: FinanceSurfaceSpec;
  onAction: (action: FinanceAction) => void;
  onFinding: (findingId: string) => void;
};

function EvidenceChips({ ids }: { ids: string[] }) {
  if (!ids.length) return null;
  return (
    <span className="evidence-row" aria-label={`${ids.length} linked evidence item${ids.length === 1 ? "" : "s"}`}>
      <LinkIcon size={13} />
      {ids.slice(0, 2).map((id) => (
        <span className="evidence-chip" key={id} title={id}>
          {id.replace(/^evd_koru_/, "")}
        </span>
      ))}
      {ids.length > 2 ? <span className="evidence-more">+{ids.length - 2}</span> : null}
    </span>
  );
}

function CashChart({ block }: { block: CashSeriesBlock }) {
  const geometry = useMemo(() => {
    const width = 760;
    const height = 252;
    const padding = { left: 18, right: 18, top: 20, bottom: 38 };
    const values = block.points.flatMap((point) => [point.balanceMinor, point.reserveMinor]);
    const minimum = Math.min(...values) - 30000;
    const maximum = Math.max(...values) + 30000;
    const x = (index: number) =>
      padding.left + (index / Math.max(block.points.length - 1, 1)) * (width - padding.left - padding.right);
    const y = (value: number) =>
      padding.top + ((maximum - value) / Math.max(maximum - minimum, 1)) * (height - padding.top - padding.bottom);
    const line = block.points.map((point, index) => `${index === 0 ? "M" : "L"}${x(index)},${y(point.balanceMinor)}`).join(" ");
    const area = `${line} L${x(block.points.length - 1)},${height - padding.bottom} L${x(0)},${height - padding.bottom} Z`;
    return { width, height, padding, x, y, line, area };
  }, [block]);

  return (
    <div className="cash-chart-wrap">
      <div className="chart-legend">
        <span><i className="legend-line cash" />Projected cash</span>
        <span><i className="legend-line reserve" />Protected reserve</span>
      </div>
      <svg
        className="cash-chart"
        viewBox={`0 0 ${geometry.width} ${geometry.height}`}
        role="img"
        aria-label="Thirty-day projected cash balance and protected reserve"
      >
        <defs>
          <linearGradient id="cashArea" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#c4512f" stopOpacity="0.16" />
            <stop offset="100%" stopColor="#c4512f" stopOpacity="0.01" />
          </linearGradient>
        </defs>
        {[0.25, 0.5, 0.75].map((ratio) => {
          const y = geometry.padding.top + ratio * (geometry.height - geometry.padding.top - geometry.padding.bottom);
          return <line key={ratio} x1="18" x2="742" y1={y} y2={y} className="chart-grid" />;
        })}
        <path d={geometry.area} fill="url(#cashArea)" />
        <line
          x1="18"
          x2="742"
          y1={geometry.y(block.points[0]?.reserveMinor ?? 0)}
          y2={geometry.y(block.points[0]?.reserveMinor ?? 0)}
          className="reserve-path"
        />
        <path d={geometry.line} className="cash-path" />
        {block.points.map((point, index) => (
          <g key={point.date}>
            <circle
              cx={geometry.x(index)}
              cy={geometry.y(point.balanceMinor)}
              r={point.status === "below_reserve" ? 5 : 3.5}
              className={point.status === "below_reserve" ? "chart-point is-risk" : "chart-point"}
            />
            {(index === 0 || index === block.points.length - 1 || point.status === "below_reserve") && (
              <text x={geometry.x(index)} y={geometry.height - 12} textAnchor={index === 0 ? "start" : index === block.points.length - 1 ? "end" : "middle"}>
                {formatDate(point.date)}
              </text>
            )}
          </g>
        ))}
      </svg>
      <div className="chart-risk-note">
        <WarningIcon size={15} />
        Cash falls below reserve on 7 August, reaching {formatMoney(190077, block.currency)}.
      </div>
    </div>
  );
}

function renderScalar(value: string | number | boolean | null, field: string): string {
  if (value === null) return "Not set";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number" && field.toLowerCase().includes("minor")) return formatMoney(value, "NZD");
  return String(value);
}

function FinanceBlockView({ block, onFinding }: { block: FinanceBlock; onFinding: (id: string) => void }) {
  switch (block.type) {
    case "narrative":
      return (
        <section className={`narrative-block tone-${block.tone}`}>
          <p>{block.text}</p>
        </section>
      );

    case "metric":
      return (
        <section className="metric-block">
          <p className="eyebrow">{block.label}</p>
          <p className="metric-value">{formatMoney(block.valueMinor, block.currency)}</p>
          <EvidenceChips ids={block.evidenceIds} />
        </section>
      );

    case "cash_series":
      return (
        <section className="chart-block">
          <CashChart block={block} />
          <details className="assumptions">
            <summary>Forecast assumptions</summary>
            <ul>{block.assumptions.map((item) => <li key={item}>{item}</li>)}</ul>
            <EvidenceChips ids={block.evidenceIds} />
          </details>
        </section>
      );

    case "scenario_compare":
      return (
        <section className="scenario-block">
          <div className="section-heading">
            <div><p className="eyebrow">Decision view</p><h3>Compare the timing</h3></div>
            <span className="calculation-badge">Calculated locally</span>
          </div>
          <div className="scenario-rows">
            {[block.baseline, block.alternative].map((scenario, index) => (
              <div className={`scenario-row ${index === 1 ? "recommended" : ""}`} key={scenario.label}>
                <div>
                  <span className="scenario-label">{scenario.label}</span>
                  {index === 1 ? <span className="recommendation">Keeps reserve</span> : null}
                </div>
                <div className="scenario-money">
                  <strong>{formatMoney(scenario.lowPointMinor, scenario.currency)}</strong>
                  <span>{scenario.reserveShortfallMinor ? `${formatMoney(scenario.reserveShortfallMinor, scenario.currency)} below reserve` : "No shortfall"}</span>
                </div>
              </div>
            ))}
          </div>
          <p className="assumption-note">{block.assumptions.join(" ")}</p>
          <EvidenceChips ids={block.evidenceIds} />
        </section>
      );

    case "transaction_rows":
      return (
        <section className="table-block">
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>Date</th><th>Description</th><th>Prepared as</th><th className="money-cell">Amount</th></tr>
              </thead>
              <tbody>
                {block.rows.map((row) => (
                  <tr key={row.transactionId} className={row.status === "duplicate" ? "muted-row" : ""}>
                    <td>{formatDate(row.occurredOn)}</td>
                    <td><strong>{row.description}</strong><span className="row-id">{row.transactionId}</span></td>
                    <td><span className={`classification classification-${row.classification}`}>{titleCase(row.category ?? row.classification)}</span></td>
                    <td className={`money-cell ${row.amountMinor < 0 ? "expense" : "income"}`}>{formatMoney(row.amountMinor, row.currency)}</td>
                  </tr>
                ))}
              </tbody>
              <tfoot><tr><td colSpan={3}>Included total</td><td className="money-cell">{formatMoney(block.totalMinor, block.currency)}</td></tr></tfoot>
            </table>
          </div>
        </section>
      );

    case "finding":
      return (
        <button className={`finding-block severity-${block.severity}`} onClick={() => onFinding(block.findingId)}>
          <span className="finding-mark" aria-hidden="true" />
          <span className="finding-content">
            <span className="finding-topline">
              <strong>{block.title}</strong>
              {block.amountMinor !== null && block.currency ? <b>{formatMoney(block.amountMinor, block.currency)}</b> : null}
            </span>
            <span className="finding-summary">{block.summary}</span>
            <EvidenceChips ids={block.evidenceIds} />
          </span>
          <ArrowIcon className="finding-arrow" size={17} />
        </button>
      );

    case "source_list":
      return (
        <section className="source-list-block">
          {block.sources.map((source) => (
            <div className="source-list-row" key={source.sourceItemId}>
              <span className={`source-status status-${source.status}`}><CheckIcon size={13} /></span>
              <div><strong>{source.label}</strong><span>{titleCase(source.sourceType)} · {formatDateTime(source.receivedAt)}</span></div>
            </div>
          ))}
        </section>
      );

    case "change_diff":
      return (
        <section className="change-block">
          <div className="section-heading"><div><p className="eyebrow">Committed event</p><h3>What changed</h3></div><code>{block.eventId}</code></div>
          <div className="change-rows">
            {block.changes.map((change) => (
              <div className="change-row" key={`${change.field}-${change.label}`}>
                <span>{change.label}</span>
                <del>{renderScalar(change.before, change.field)}</del>
                <ArrowIcon size={14} />
                <ins>{renderScalar(change.after, change.field)}</ins>
              </div>
            ))}
          </div>
          <div className="change-footer">
            <EvidenceChips ids={block.evidenceIds} />
            <span>{block.undoAvailable ? "Inverse event ready" : "No further undo available"}</span>
          </div>
        </section>
      );

    case "artifact_preview":
      return (
        <section className="artifact-block">
          <div className="paper-preview" aria-hidden="true">
            <div className="paper-brand">F.</div>
            <div className="paper-lines"><i /><i /><i /></div>
            <div className="paper-chart"><i /><i /><i /><i /><i /></div>
            <div className="paper-lines short"><i /><i /></div>
          </div>
          <div className="artifact-meta">
            <span className="file-type">{block.kind.toUpperCase()}</span>
            <div><strong>{block.title}</strong><span>Generated {formatDateTime(block.generatedAt)}</span></div>
            <code title={block.contentHash}>{shortHash(block.contentHash)}</code>
          </div>
        </section>
      );
  }
}

function ActionIcon({ action }: { action: FinanceAction }) {
  if (action.type === "undo_event") return <UndoIcon size={15} />;
  if (action.type === "download_artifact") return <DownloadIcon size={15} />;
  return <ArrowIcon size={15} />;
}

export function SurfaceRenderer({ surface, onAction, onFinding }: SurfaceRendererProps) {
  let safeSurface: FinanceSurfaceSpec;
  try {
    safeSurface = validateSurfaceSpec(surface);
  } catch (error) {
    return (
      <div className="surface-error" role="alert">
        <WarningIcon size={22} />
        <h2>This financial view was blocked</h2>
        <p>{error instanceof Error ? error.message : "The surface did not match the trusted catalogue."}</p>
      </div>
    );
  }

  const metrics = safeSurface.blocks.filter((block) => block.type === "metric");
  const nonMetrics = safeSurface.blocks.filter((block) => block.type !== "metric");

  return (
    <div className={`surface surface-${safeSurface.surfaceType}`} key={safeSurface.surfaceId}>
      <header className="surface-header">
        <div>
          <div className="surface-kicker">
            <span className={`freshness-dot freshness-${safeSurface.freshness.status}`} />
            Data through {formatDateTime(safeSurface.freshness.dataThrough)}
          </div>
          <h1>{safeSurface.title}</h1>
          {safeSurface.subtitle ? <p>{safeSurface.subtitle}</p> : null}
        </div>
      </header>

      <div className="surface-body">
        {safeSurface.surfaceType === "living_brief" && metrics.length ? (
          <div className="metric-pair">
            {metrics.map((block) => <FinanceBlockView key={block.blockId} block={block} onFinding={onFinding} />)}
          </div>
        ) : null}
        {(safeSurface.surfaceType === "living_brief" ? nonMetrics : safeSurface.blocks).map((block) => (
          <FinanceBlockView key={block.blockId} block={block} onFinding={onFinding} />
        ))}
      </div>

      {safeSurface.actions.length ? (
        <footer className="surface-actions">
          {safeSurface.actions.map((action, index) => (
            <button
              key={action.actionId}
              className={index === 0 ? "button button-primary" : "button button-secondary"}
              onClick={() => onAction(action)}
            >
              {action.label}<ActionIcon action={action} />
            </button>
          ))}
        </footer>
      ) : null}
    </div>
  );
}
