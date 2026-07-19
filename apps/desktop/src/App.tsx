import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Drawer } from "./Drawer";
import { Onboarding, type OnboardingSource } from "./Onboarding";
import { SurfaceRenderer } from "./SurfaceRenderer";
import { ToolTrace, type ToolTraceStep } from "./ToolTrace";
import {
  cashScenarioSurface,
  correctedReceiptSurface,
  initialActivity,
  initialSources,
  livingBriefSurface,
  ownerPackSurface,
  recordsSurface,
  transactionDetailSurface,
  undoneReceiptSurface,
  workspaceFixture,
} from "./fixtures";
import { formatDateTime } from "./format";
import {
  ActivityIcon,
  BriefIcon,
  CashIcon,
  CheckIcon,
  CloseIcon,
  MoreIcon,
  PackIcon,
  PrivacyIcon,
  RecordsIcon,
  SendIcon,
  SourceIcon,
  SparkIcon,
  StopIcon,
} from "./icons";
import {
  ingestAkahuFixture,
  ingestTelegramFixture,
  importCsv,
  loadSnapshot,
  openArtifact,
  postTurn,
  probeBackend,
  readRunEvents,
  resetDemo,
  runDailyClose,
  undoEvent,
  type BackendHealth,
} from "./transport";
import type {
  ActivityItem,
  DrawerKind,
  FinanceAction,
  FinanceSurfaceSpec,
  ModelMode,
  RunEvent,
  SourceItem,
  ThreadTurn,
} from "./types";

const initialBackend: BackendHealth = {
  mode: "checking",
  label: "Finding local service",
  detail: "Checking 127.0.0.1:8787…",
  apiUrl: "http://127.0.0.1:8787",
  lmStudioReady: false,
  lmStudioStatus: "checking",
  cloudReady: false,
  cloudCredentialState: "absent",
};

const nowIso = () => new Date().toISOString();
const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms));

function makeId(prefix: string) {
  return `${prefix}_${Date.now().toString(36)}`;
}

function modelLabel(mode: ModelMode) {
  return mode === "local" ? "Local" : mode === "hybrid" ? "Hybrid" : "Cloud";
}

function collectToolSteps(events: RunEvent[]): ToolTraceStep[] {
  const steps = new Map<string, ToolTraceStep>();
  for (const event of events) {
    if (event.type === "tool.started") {
      const toolCallId = String(event.payload.toolCallId ?? "");
      const toolName = String(event.payload.toolName ?? "tool");
      if (!toolCallId) continue;
      steps.set(toolCallId, { toolCallId, toolName, status: "running" });
    }
    if (event.type === "tool.completed") {
      const toolCallId = String(event.payload.toolCallId ?? "");
      const toolName = String(event.payload.toolName ?? steps.get(toolCallId)?.toolName ?? "tool");
      if (!toolCallId) continue;
      const status = event.payload.status === "failed" ? "failed" : "completed";
      steps.set(toolCallId, {
        toolCallId,
        toolName,
        status,
        durationMs: typeof event.payload.durationMs === "number" ? event.payload.durationMs : undefined,
      });
    }
  }
  return [...steps.values()];
}

function ThreadMessage({ turn, onUndoReceipt }: { turn: ThreadTurn; onUndoReceipt?: () => void }) {
  return (
    <article className={`thread-message role-${turn.role} status-${turn.status}`}>
      {turn.role === "agent" ? <span className="agent-avatar">F.</span> : null}
      <div className="message-content">
        <div className="message-meta">
          <strong>{turn.role === "agent" ? "Folio" : "You"}</strong>
          <span>{formatDateTime(turn.occurredAt)}</span>
        </div>
        <p>{turn.content}</p>
        {turn.evidenceIds.length ? (
          <button className="message-evidence"><SourceIcon size={13} /> {turn.evidenceIds.length} source{turn.evidenceIds.length === 1 ? "" : "s"}</button>
        ) : null}
        {turn.receipt ? (
          <div className="inline-receipt">
            <CheckIcon size={13} />
            <span>{turn.receipt.label}</span>
            {turn.receipt.undoable && onUndoReceipt ? (
              <button className="receipt-undo" type="button" onClick={onUndoReceipt}>
                Undo
              </button>
            ) : turn.receipt.undoable ? <b>Undo available</b> : null}
          </div>
        ) : null}
      </div>
    </article>
  );
}

export function App() {
  const query = new URLSearchParams(window.location.search);
  const forceOnboarding = query.get("onboarding") === "1";
  const skipOnboarding = query.get("demo") === "1";
  const [showOnboarding, setShowOnboarding] = useState(() => forceOnboarding || (!skipOnboarding && localStorage.getItem("folio:onboarded") !== "yes"));
  const [backend, setBackend] = useState(initialBackend);
  const [modelMode, setModelMode] = useState<ModelMode>(workspaceFixture.modelMode);
  const [selectedModel, setSelectedModel] = useState("qwen-local");
  const [surface, setSurface] = useState<FinanceSurfaceSpec>(livingBriefSurface);
  const [turns, setTurns] = useState<ThreadTurn[]>(workspaceFixture.thread.turns);
  const [sources, setSources] = useState<SourceItem[]>(initialSources);
  const [activity, setActivity] = useState<ActivityItem[]>(initialActivity);
  const [drawer, setDrawer] = useState<DrawerKind | null>(null);
  const [composer, setComposer] = useState("");
  const [running, setRunning] = useState(false);
  const [workingLabel, setWorkingLabel] = useState("Planning locally");
  const [ingestProgress, setIngestProgress] = useState<string | null>(null);
  const [toolSteps, setToolSteps] = useState<ToolTraceStep[]>([]);
  const [toolTraceOpen, setToolTraceOpen] = useState(false);
  const [mobilePane, setMobilePane] = useState<"thread" | "canvas">("thread");
  const [threadWidth, setThreadWidth] = useState(() => Number(localStorage.getItem("folio:thread-width")) || 400);
  const [toast, setToast] = useState<string | null>(null);
  const [telegramImported, setTelegramImported] = useState(false);
  const [correctionActive, setCorrectionActive] = useState(true);
  const runToken = useRef(0);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const localUnavailable = modelMode === "local" && backend.mode !== "checking" && !backend.lmStudioReady;

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast((current) => current === message ? null : current), 2800);
  }, []);

  const applySnapshot = useCallback((snapshot: typeof workspaceFixture) => {
    setSurface(snapshot.currentSurface);
    setTurns(snapshot.thread.turns);
    setSources(snapshot.sources);
    setActivity(snapshot.activity);
    setModelMode(snapshot.modelMode);
  }, []);

  const absorbRunEvents = useCallback((events: RunEvent[]) => {
    const nextTools = collectToolSteps(events);
    if (nextTools.length) setToolSteps(nextTools);
    const stage = [...events].reverse().find((event) => event.type === "stage.started" || event.type === "stage.completed");
    if (stage && typeof stage.payload.stage === "string") {
      setWorkingLabel(String(stage.payload.stage).replace(/[_./]+/g, " "));
    }
  }, []);

  useEffect(() => {
    let active = true;
    if (skipOnboarding || forceOnboarding) {
      setBackend({
        mode: "fixture",
        label: "Demo data",
        detail: "The sealed Koru Studio fixture is active.",
        apiUrl: "http://127.0.0.1:8787",
        lmStudioReady: false,
        lmStudioStatus: "unavailable",
        cloudReady: false,
        cloudCredentialState: "absent",
      });
      return () => { active = false; };
    }
    void probeBackend().then(async (nextBackend) => {
      if (!active) return;
      setBackend(nextBackend);
      if (nextBackend.mode === "live") {
        try {
          const live = await loadSnapshot("ws_koru_studio");
          if (!active) return;
          applySnapshot(live);
        } catch {
          setBackend((current) => ({ ...current, mode: "fixture", label: "Demo data", detail: "The local service responded, but the demo workspace is not ready. Sealed fixtures are active." }));
        }
      }
    });
    const onOffline = () => setBackend((current) => ({ ...current, mode: "offline", label: "Offline demo", detail: "Existing local data remains available." }));
    const onOnline = () => void probeBackend().then(setBackend);
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);
    return () => {
      active = false;
      window.removeEventListener("offline", onOffline);
      window.removeEventListener("online", onOnline);
    };
  }, [applySnapshot, forceOnboarding, skipOnboarding]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, running, ingestProgress]);

  const runIngestProgress = useCallback(async (token: number, labels: string[]) => {
    for (const label of labels) {
      if (runToken.current !== token) return;
      setIngestProgress(label);
      await sleep(520);
    }
    if (runToken.current !== token) return;
    setIngestProgress(null);
  }, []);

  const completeOnboarding = (sourceChoice: OnboardingSource, csvFile: File | null, businessContext: string) => {
    localStorage.setItem("folio:onboarded", "yes");
    setShowOnboarding(false);
    const token = ++runToken.current;
    setRunning(true);
    setWorkingLabel(sourceChoice === "akahu" ? "Syncing Akahu" : sourceChoice === "csv" ? "Importing CSV" : "Opening demo");

    void (async () => {
      if (sourceChoice === "csv" && csvFile) {
        await runIngestProgress(token, [
          `Reading ${csvFile.name} · local only`,
          "Deduping near Mitre 10 · local only",
          "Preparing first look…",
        ]);
      } else if (sourceChoice === "akahu") {
        await runIngestProgress(token, [
          "Reading ANZ Everyday via Akahu · local only",
          "6 rows · dedupe checks",
          "Preparing first look…",
        ]);
      }

      if (backend.mode === "live") {
        try {
          if (sourceChoice === "csv" && csvFile) {
            await importCsv(csvFile);
            const close = await runDailyClose();
            absorbRunEvents(await readRunEvents(close.runId));
          }
          if (sourceChoice === "akahu") {
            await ingestAkahuFixture({ account: "anz_everyday", syncedAt: nowIso() });
            const close = await runDailyClose();
            absorbRunEvents(await readRunEvents(close.runId));
          }
          const contextRun = await postTurn("thr_koru_studio_main", businessContext, modelMode);
          absorbRunEvents(await readRunEvents(contextRun.runId));
          applySnapshot(await loadSnapshot("ws_koru_studio"));
          if (runToken.current !== token) return;
          setRunning(false);
          showToast(
            sourceChoice === "csv" && csvFile
              ? `${csvFile.name} imported locally.`
              : sourceChoice === "akahu"
                ? "Akahu sync prepared. ANZ Everyday is live."
                : "Your business context was saved to the local Folio thread.",
          );
          return;
        } catch {
          showToast("The setup preference was not committed. Continue in the thread when the local service is ready.");
        }
      }

      if (sourceChoice === "akahu") {
        const akahu = await ingestAkahuFixture({ account: "anz_everyday", syncedAt: nowIso() });
        const rowCount = typeof akahu.rowCount === "number" ? akahu.rowCount : 6;
        setSources((items) => {
          if (items.some((item) => item.sourceType === "akahu")) {
            return items.map((item) =>
              item.sourceType === "akahu"
                ? { ...item, status: "live" as const, rowCount }
                : item,
            );
          }
          return [{
            sourceItemId: "src_koru_akahu_anz_everyday",
            sourceType: "akahu",
            label: "Akahu · ANZ Everyday",
            receivedAt: nowIso(),
            status: "live",
            rowCount,
          }, ...items];
        });
        setActivity((items) => [{
          activityId: makeId("activity_akahu"),
          kind: "source_ingest",
          summary: "Akahu sync · ANZ Everyday",
          detail: "Fixture path: read-only Open Banking rows prepared locally.",
          status: "completed",
          occurredAt: nowIso(),
          undoable: false,
          evidenceIds: ["evd_koru_akahu_anz"],
        }, ...items]);
      }

      if (runToken.current !== token) return;
      setRunning(false);
      setIngestProgress(null);
      showToast(
        sourceChoice === "csv"
          ? "CSV import finished in the sealed demo path."
          : sourceChoice === "akahu"
            ? "Akahu fixture ready. ANZ Everyday is linked."
            : "Koru Studio is ready. Your Daily Close is already prepared.",
      );
    })();
  };

  const openSurface = useCallback((next: FinanceSurfaceSpec) => {
    setSurface(next);
    setMobilePane("canvas");
  }, []);

  const stopCurrentRun = useCallback(() => {
    runToken.current += 1;
    setRunning(false);
    setIngestProgress(null);
    setWorkingLabel("Planning locally");
    setTurns((current) => [...current, {
      turnId: makeId("turn_stopped"),
      role: "agent",
      content: "Stopped. I kept the finance work that had already committed, left unfinished work unapplied, and preserved the evidence links so you can continue in your own words.",
      occurredAt: nowIso(),
      status: "stopped",
      evidenceIds: [],
    }]);
  }, []);

  const handleDailyClose = useCallback(async () => {
    if (running) return;
    const token = ++runToken.current;
    setRunning(true);
    setWorkingLabel("Preparing Daily Close");
    setToolSteps([
      { toolCallId: "tool_ingest", toolName: "check_sources", status: "running" },
    ]);
    const activityId = makeId("activity_daily_close");
    setActivity((items) => [{
      activityId,
      kind: "job_run",
      summary: "Daily Close is running",
      detail: "Checking new sources and rebuilding prepared finance views.",
      status: "running",
      occurredAt: nowIso(),
      undoable: false,
      evidenceIds: ["evd_koru_bank_csv"],
    }, ...items]);

    let liveSnapshot: typeof workspaceFixture | null = null;
    if (backend.mode === "live") {
      try {
        const run = await runDailyClose();
        const events = await readRunEvents(run.runId);
        absorbRunEvents(events);
        liveSnapshot = await loadSnapshot("ws_koru_studio");
      } catch {
        setBackend((current) => ({ ...current, mode: "fixture", label: "Demo data", detail: "The loopback close failed, so sealed Koru Studio fixtures are active." }));
      }
    } else {
      await sleep(480);
      if (runToken.current !== token) return;
      setWorkingLabel("Recomputing cash outlook");
      setToolSteps([
        { toolCallId: "tool_ingest", toolName: "check_sources", status: "completed", durationMs: 120 },
        { toolCallId: "tool_forecast", toolName: "forecast_cash", status: "running" },
      ]);
      await sleep(640);
      if (runToken.current !== token) return;
      setToolSteps([
        { toolCallId: "tool_ingest", toolName: "check_sources", status: "completed", durationMs: 120 },
        { toolCallId: "tool_forecast", toolName: "forecast_cash", status: "completed", durationMs: 210 },
        { toolCallId: "tool_pack", toolName: "prepare_owner_pack", status: "completed", durationMs: 180 },
      ]);
    }
    if (runToken.current !== token) return;

    if (liveSnapshot) {
      applySnapshot(liveSnapshot);
      setRunning(false);
      showToast("Daily Close committed through the local service.");
      return;
    }

    setActivity((items) => items.map((item) => item.activityId === activityId ? {
      ...item,
      summary: "Daily Close completed from 10 source rows",
      detail: "No new material change. Totals and owner pack match the previous committed close.",
      status: "completed",
    } : item));
    setTurns((current) => [...current, {
      turnId: makeId("turn_close_complete"),
      role: "agent",
      content: "Daily Close is complete. Nothing material changed since the previous run: the pending Figma duplicate is still held out, MITRE 10 still needs context, and the planned laptop still creates a NZD 99.23 reserve shortfall.",
      occurredAt: nowIso(),
      status: "complete",
      evidenceIds: ["evd_koru_bank_csv", "evd_koru_forecast_30d"],
      receipt: { label: "Idempotent close receipt committed" },
    }]);
    setSurface(livingBriefSurface);
    setRunning(false);
    showToast("Daily Close complete — outlook unchanged.");
  }, [absorbRunEvents, applySnapshot, backend.mode, running, showToast]);

  const applyCorrection = useCallback(async (ownerText: string, token: number) => {
    setWorkingLabel("Recording correction");
    setToolSteps([
      { toolCallId: "tool_claim", toolName: "record_owner_claim", status: "running" },
    ]);
    await sleep(780);
    if (runToken.current !== token) return;
    setCorrectionActive(true);
    setToolSteps([
      { toolCallId: "tool_claim", toolName: "record_owner_claim", status: "completed", durationMs: 140 },
      { toolCallId: "tool_rule", toolName: "apply_narrow_rule", status: "completed", durationMs: 90 },
    ]);
    setTurns((current) => [...current, {
      turnId: makeId("turn_rule_complete"),
      role: "agent",
      content: "Understood. I recorded that as client fit-out materials, reclassified the NZD 184.75 MITRE 10 transaction, and added a narrow rule for matching MITRE 10 purchases up to NZD 500. I did not change the cash amount, only its bookkeeping meaning.",
      occurredAt: nowIso(),
      status: "complete",
      evidenceIds: ["evd_koru_mitre10_row", "evd_koru_owner_claim_mitre"],
      receipt: { label: "1 transaction changed · narrow rule saved", eventId: "evt_koru_mitre_rule_created", undoable: true },
    }]);
    setActivity((items) => {
      const withoutSeed = items.filter((item) => item.eventId !== "evt_koru_mitre_rule_created");
      return [{
        activityId: makeId("activity_rule"),
        kind: "finance_event",
        summary: "Mitre 10 correction",
        detail: `Owner said: “${ownerText.slice(0, 110)}${ownerText.length > 110 ? "…" : ""}” Unresolved → Client fit-out materials.`,
        status: "completed",
        occurredAt: nowIso(),
        undoable: true,
        eventId: "evt_koru_mitre_rule_created",
        evidenceIds: ["evd_koru_mitre10_row", "evd_koru_owner_claim_mitre"],
      }, ...withoutSeed];
    });
    openSurface(correctedReceiptSurface);
    setRunning(false);
    showToast("Correction committed. Undo is available.");
  }, [openSurface, showToast]);

  const submitTurn = useCallback(async () => {
    const content = composer.trim();
    if (!content || running) return;
    setComposer("");
    setTurns((current) => [...current, {
      turnId: makeId("turn_owner"),
      role: "owner",
      content,
      occurredAt: nowIso(),
      status: "complete",
      evidenceIds: [],
    }]);
    const token = ++runToken.current;
    setRunning(true);
    setWorkingLabel(localUnavailable ? "Deterministic fallback" : "Planning locally");
    setToolSteps([{ toolCallId: "tool_plan", toolName: "plan_reply", status: "running" }]);
    if (backend.mode === "live") {
      try {
        const run = await postTurn(workspaceFixture.thread.threadId, content, modelMode);
        const events = await readRunEvents(run.runId);
        absorbRunEvents(events);
        applySnapshot(await loadSnapshot("ws_koru_studio"));
        setRunning(false);
        return;
      } catch {
        setBackend((current) => ({ ...current, mode: "fixture", label: "Demo data", detail: "The loopback turn failed; the sealed controller completed this interaction." }));
      }
    }
    const normalised = content.toLowerCase();
    if (normalised.includes("mitre") || normalised.includes("fit-out") || normalised.includes("materials")) {
      await applyCorrection(content, token);
      return;
    }
    await sleep(620);
    if (runToken.current !== token) return;
    setToolSteps([{ toolCallId: "tool_plan", toolName: "plan_reply", status: "completed", durationMs: 180 }]);
    let response = "I have kept that as an owner claim and linked it to this conversation. I do not need another answer yet; I will use it when the next source or Daily Close makes it relevant.";
    if (normalised.includes("laptop") || normalised.includes("reserve") || normalised.includes("cash")) {
      response = "The laptop timing is the material lever. Paying on 7 August takes the projected low to NZD 1,900.77, which is NZD 99.23 below your protected reserve. Deferring it keeps the low at NZD 4,900.77.";
      openSurface(cashScenarioSurface);
    } else if (normalised.includes("pack") || normalised.includes("accountant") || normalised.includes("document")) {
      response = "I prepared the owner pack from the same committed figures. It includes source coverage, unresolved evidence, the cash projection, and the exact assumptions behind it.";
      openSurface(ownerPackSurface);
    } else if (normalised.includes("receipt") || normalised.includes("work")) {
      openSurface(correctedReceiptSurface);
    } else if (normalised.includes("transaction") || normalised.includes("mitre")) {
      openSurface(transactionDetailSurface);
    }
    setTurns((current) => [...current, {
      turnId: makeId("turn_agent"),
      role: "agent",
      content: response,
      occurredAt: nowIso(),
      status: "complete",
      evidenceIds: normalised.includes("cash") || normalised.includes("laptop") ? ["evd_koru_forecast_30d"] : [],
    }]);
    setRunning(false);
  }, [absorbRunEvents, applyCorrection, applySnapshot, backend.mode, composer, localUnavailable, modelMode, openSurface, running]);

  const handleUndo = useCallback(async (eventId: string) => {
    if (!correctionActive && eventId === "evt_koru_mitre_rule_created") {
      showToast("That correction is already inactive.");
      return;
    }
    if (backend.mode === "live") {
      try {
        await undoEvent(eventId);
        applySnapshot(await loadSnapshot("ws_koru_studio"));
        setCorrectionActive(false);
        setDrawer(null);
        showToast("Classification restored · totals matched");
        return;
      } catch {
        setBackend((current) => ({ ...current, mode: "fixture", label: "Demo data", detail: "The local Undo did not complete, so only the sealed fixture was changed." }));
      }
    }
    setCorrectionActive(false);
    setActivity((items) => [{
      activityId: makeId("activity_inverse"),
      kind: "finance_event",
      summary: "Mitre 10 correction undone",
      detail: "Previous guess restored. Both events remain in the audit trail.",
      status: "completed",
      occurredAt: nowIso(),
      undoable: false,
      eventId: "evt_koru_mitre_rule_undone",
      evidenceIds: ["evd_koru_mitre10_row"],
    }, ...items.map((item) => item.eventId === eventId ? { ...item, status: "undone" as const, undoable: false } : item)]);
    setTurns((current) => [...current, {
      turnId: makeId("turn_undo"),
      role: "agent",
      content: "Done. The narrow rule is inactive and the MITRE 10 transaction is unresolved again. I kept both events in the audit trail.",
      occurredAt: nowIso(),
      status: "complete",
      evidenceIds: ["evd_koru_mitre10_row"],
      receipt: { label: "Undone · Mitre 10 · previous guess restored" },
    }]);
    setDrawer(null);
    openSurface(undoneReceiptSurface);
    showToast("Classification restored · totals matched");
  }, [applySnapshot, backend.mode, correctionActive, openSurface, showToast]);

  const importTelegram = useCallback(async () => {
    if (telegramImported) return;
    if (backend.mode === "live") {
      try {
        await ingestTelegramFixture();
        applySnapshot(await loadSnapshot("ws_koru_studio"));
      } catch {
        setBackend((current) => ({ ...current, mode: "fixture", label: "Demo data", detail: "The loopback Telegram fixture failed, so the sealed UI fixture remains active." }));
      }
    }
    setTelegramImported(true);
    setSources((items) => items.map((source) => source.sourceType === "telegram_fixture" ? { ...source, status: "processed" } : source));
    setActivity((items) => [{
      activityId: makeId("activity_telegram"),
      kind: "source_ingest",
      summary: "Telegram parking receipt processed",
      detail: "NZD 32.40 · client meeting parking · attachment reference retained locally.",
      status: "completed",
      occurredAt: nowIso(),
      undoable: false,
      evidenceIds: ["evd_koru_telegram_parking"],
    }, ...items]);
    setTurns((current) => [...current, {
      turnId: makeId("turn_telegram"),
      role: "agent",
      content: "I received the Telegram message: “Parking for the client meeting, NZD 32.40. Expense it.” I prepared it as business travel and kept the attachment reference with the source. It will be included in the next close.",
      occurredAt: nowIso(),
      status: "complete",
      evidenceIds: ["evd_koru_telegram_parking"],
      receipt: { label: "Telegram fixture deduplicated and prepared" },
    }]);
    showToast("Telegram receipt prepared for the next close.");
  }, [applySnapshot, backend.mode, showToast, telegramImported]);

  const handleReset = useCallback(async () => {
    if (backend.mode === "live") {
      try {
        await resetDemo();
        const run = await runDailyClose();
        absorbRunEvents(await readRunEvents(run.runId));
        applySnapshot(await loadSnapshot("ws_koru_studio"));
        runToken.current += 1;
        setTelegramImported(false);
        setCorrectionActive(true);
        setRunning(false);
        setDrawer(null);
        setToolSteps([]);
        showToast("Koru Studio reset and rebuilt through the local service.");
        return;
      } catch {
        setBackend((current) => ({ ...current, mode: "fixture", label: "Demo data", detail: "The local reset failed, so the sealed fixture state was restored." }));
      }
    }
    runToken.current += 1;
    setTurns(workspaceFixture.thread.turns);
    setSources(initialSources);
    setActivity(initialActivity);
    setSurface(livingBriefSurface);
    setTelegramImported(false);
    setCorrectionActive(true);
    setRunning(false);
    setDrawer(null);
    setToolSteps([]);
    setIngestProgress(null);
    showToast("Koru Studio reset to the sealed demo state.");
  }, [absorbRunEvents, applySnapshot, backend.mode, showToast]);

  const handleSurfaceAction = useCallback((action: FinanceAction) => {
    if (action.type === "open_drawer") setDrawer(action.drawer);
    if (action.type === "focus_source") {
      setDrawer("sources");
      openSurface(transactionDetailSurface);
    }
    if (action.type === "run_scenario") openSurface(cashScenarioSurface);
    if (action.type === "undo_event") void handleUndo(action.eventId);
    if (action.type === "download_artifact") {
      showToast(backend.mode === "live" ? "Opening the generated owner pack…" : "Owner pack preview is running from the sealed demo fixture.");
      if (backend.mode === "live") void openArtifact(action.artifactId);
      openSurface(ownerPackSurface);
    }
  }, [backend.mode, handleUndo, openSurface, showToast]);

  const changeMode = useCallback((mode: ModelMode) => {
    setModelMode(mode);
    if (mode === "cloud") setSelectedModel("gpt-cloud");
    if (mode === "local" && selectedModel === "gpt-cloud") setSelectedModel("qwen-local");
    showToast(`${modelLabel(mode)} mode selected. The finance engine remains local.`);
  }, [selectedModel, showToast]);

  const canvasNav = useMemo(() => [
    { label: "Brief", icon: BriefIcon, surface: livingBriefSurface },
    { label: "Cash", icon: CashIcon, surface: cashScenarioSurface },
    { label: "Records", icon: RecordsIcon, surface: recordsSurface },
    { label: "Owner pack", icon: PackIcon, surface: ownerPackSurface },
  ], []);

  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (window.innerWidth < 1080) return;
    const startX = event.clientX;
    const startWidth = threadWidth;
    let finalWidth = threadWidth;
    const onMove = (moveEvent: PointerEvent) => {
      const next = Math.min(520, Math.max(340, startWidth + moveEvent.clientX - startX));
      finalWidth = next;
      setThreadWidth(next);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      localStorage.setItem("folio:thread-width", String(finalWidth));
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp, { once: true });
  };

  const openFindingSurface = (findingId: string) => {
    if (findingId === "finding_koru_missing_receipt") {
      openSurface(transactionDetailSurface);
      return;
    }
    if (findingId === "finding_koru_reserve_risk") {
      openSurface(cashScenarioSurface);
      return;
    }
    if (findingId === "finding_koru_duplicate_pending") {
      openSurface(recordsSurface);
      return;
    }
    openSurface(livingBriefSurface);
  };

  const composerHint = ingestProgress
    ? ingestProgress
    : running
      ? `${workingLabel} · ${selectedModel === "gpt-cloud" ? "GPT cloud" : selectedModel === "llama-local" ? "Llama local" : "Qwen local"} · no cloud egress`
      : localUnavailable
        ? "Deterministic fallback · local model unavailable"
        : `${modelLabel(modelMode)} language · local finance`;

  return (
    <div className="app-shell" style={{ "--thread-width": `${threadWidth}px` } as React.CSSProperties}>
      <nav className="rail" aria-label="Workspace controls">
        <button className="brand-button" aria-label="Folio home" onClick={() => openSurface(livingBriefSurface)}>F.</button>
        <div className="rail-actions">
          <button className={drawer === "sources" ? "is-active" : ""} aria-label="Sources" title="Sources" onClick={() => setDrawer("sources")}><SourceIcon /></button>
          <button className={drawer === "activity" ? "is-active" : ""} aria-label="Activity and Undo" title="Activity & Undo" onClick={() => setDrawer("activity")}><ActivityIcon /></button>
          <button className={drawer === "connections" ? "is-active" : ""} aria-label="Connections and Privacy" title="Connections & Privacy" onClick={() => setDrawer("connections")}><PrivacyIcon /></button>
        </div>
        <div className="rail-bottom">
          <span className={`rail-runtime runtime-${backend.mode}`} title={backend.label} />
          <button aria-label="Open tool trace" title="Tool trace" onClick={() => setToolTraceOpen(true)}><SparkIcon /></button>
          <button aria-label="More options" title="More options"><MoreIcon /></button>
        </div>
      </nav>

      <section className={`thread-pane ${mobilePane === "thread" ? "is-mobile-active" : ""}`}>
        <header className="thread-header">
          <div><strong>Koru Studio</strong><span>NZ sole trader · continuing thread</span></div>
          <button className={`mode-chip ${localUnavailable ? "mode-chip-warning" : ""}`} onClick={() => setDrawer("connections")}>
            <span className={`mode-dot mode-${modelMode} ${localUnavailable ? "mode-unavailable" : ""}`} />
            {localUnavailable ? "Local · unavailable · fallback" : modelLabel(modelMode)}
          </button>
        </header>

        {localUnavailable ? (
          <div className="local-fallback-banner" role="status">
            <div>
              <strong>Local model unavailable</strong>
              <p>Using deterministic fallback. Folio will not silently call the cloud.</p>
            </div>
            <div className="local-fallback-actions">
              <button className="button button-compact" type="button" onClick={() => void probeBackend().then(setBackend)}>Retry local</button>
              <button className="button button-ghost" type="button" onClick={() => setDrawer("connections")}>Open models</button>
            </div>
          </div>
        ) : null}

        <div className="mobile-tabs" role="tablist" aria-label="Workspace pane">
          <button role="tab" aria-selected={mobilePane === "thread"} onClick={() => setMobilePane("thread")}>Conversation</button>
          <button role="tab" aria-selected={mobilePane === "canvas"} onClick={() => setMobilePane("canvas")}>Financial view</button>
        </div>

        <div className="thread-scroll">
          <div className="thread-day"><span>Today</span></div>
          {turns.map((turn) => (
            <ThreadMessage
              turn={turn}
              key={turn.turnId}
              onUndoReceipt={turn.receipt?.undoable && turn.receipt.eventId ? () => {
                void handleUndo(turn.receipt!.eventId!);
              } : undefined}
            />
          ))}
          {ingestProgress || running ? (
            <article className="progress-card progress-card-calm" aria-live="polite">
              <div className="progress-heading">
                <span className="agent-avatar"><SparkIcon size={14} /></span>
                <div>
                  <strong>{ingestProgress ?? workingLabel}</strong>
                  <span>{ingestProgress ? "Importing · local only" : "Drafting · not sending to cloud"}</span>
                </div>
              </div>
              <div className="progress-bar progress-bar-pulse"><i style={{ width: ingestProgress ? "62%" : "38%" }} /></div>
              {toolSteps.length ? (
                <button className="text-button tool-trace-link" type="button" onClick={() => setToolTraceOpen(true)}>
                  View tool trace · {toolSteps.length} step{toolSteps.length === 1 ? "" : "s"}
                </button>
              ) : null}
            </article>
          ) : null}
          <div ref={messagesEndRef} />
        </div>

        <div className="composer-area">
          {!running && !ingestProgress ? (
            <div className="suggestion-row">
              <button onClick={() => setComposer("The MITRE 10 purchase was materials for a client fit-out. Treat similar purchases under $500 the same way.")}>Explain MITRE 10</button>
              <button onClick={() => setComposer("Show me what happens if I defer the laptop purchase.")}>Test laptop timing</button>
            </div>
          ) : null}
          <div className={`composer ${running || ingestProgress ? "is-running" : ""}`}>
            <textarea
              value={composer}
              onChange={(event) => setComposer(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  void submitTurn();
                }
              }}
              placeholder={running || ingestProgress ? "Folio is working…" : localUnavailable ? "Ask anyway — deterministic fallback stays on" : "Tell me anything about the business…"}
              rows={3}
              disabled={running || Boolean(ingestProgress)}
              aria-label="Message Folio"
            />
            <div className="composer-footer">
              <span><SparkIcon size={14} /> {composerHint}</span>
              {running || ingestProgress ? (
                <button className="stop-button" onClick={stopCurrentRun}><StopIcon size={14} /> Stop</button>
              ) : (
                <button className="send-button" aria-label="Send message" disabled={!composer.trim()} onClick={() => void submitTurn()}><SendIcon size={16} /></button>
              )}
            </div>
          </div>
          <p className="composer-disclaimer">Finance preparation, not tax filing or financial advice.</p>
        </div>
      </section>

      <div className="splitter" role="separator" aria-orientation="vertical" aria-label="Resize conversation" onPointerDown={startResize} />

      <main className={`canvas-pane ${mobilePane === "canvas" ? "is-mobile-active" : ""}`}>
        <header className="canvas-toolbar">
          <div className="canvas-nav" aria-label="Financial views">
            {canvasNav.map((item) => {
              const Icon = item.icon;
              return <button className={surface.surfaceType === item.surface.surfaceType ? "is-active" : ""} onClick={() => openSurface(item.surface)} key={item.label}><Icon size={15} />{item.label}</button>;
            })}
          </div>
          <div className="canvas-actions">
            <span className={`sync-status sync-${backend.mode}`}><i />{backend.mode === "live" ? "Live local data" : backend.mode === "offline" ? "Offline" : "Sealed demo"}</span>
            <button className="button button-compact" onClick={() => setToolTraceOpen(true)} disabled={!toolSteps.length && !running}>Tool trace</button>
            <button className="button button-compact" onClick={() => void handleDailyClose()} disabled={running || Boolean(ingestProgress)}><SparkIcon size={14} />Run close</button>
          </div>
        </header>

        <div className="mobile-tabs canvas-mobile-tabs" role="tablist" aria-label="Workspace pane">
          <button role="tab" aria-selected={mobilePane === "thread"} onClick={() => setMobilePane("thread")}>Conversation</button>
          <button role="tab" aria-selected={mobilePane === "canvas"} onClick={() => setMobilePane("canvas")}>Financial view</button>
        </div>

        <div className="canvas-scroll">
          <SurfaceRenderer
            surface={surface}
            onAction={handleSurfaceAction}
            onFinding={openFindingSurface}
          />
        </div>

        <Drawer
          kind={drawer}
          sources={sources}
          activity={activity}
          modelMode={modelMode}
          selectedModel={selectedModel}
          backend={backend}
          telegramImported={telegramImported}
          onClose={() => setDrawer(null)}
          onModeChange={changeMode}
          onModelChange={setSelectedModel}
          onImportTelegram={() => void importTelegram()}
          onUndo={(eventId) => void handleUndo(eventId)}
          onReset={() => void handleReset()}
        />

        <ToolTrace open={toolTraceOpen} steps={toolSteps} onClose={() => setToolTraceOpen(false)} />
      </main>

      {showOnboarding ? <Onboarding initialMode={modelMode} backend={backend} onModeChange={changeMode} onComplete={completeOnboarding} /> : null}
      {toast ? <div className="toast" role="status"><CheckIcon size={15} />{toast}<button aria-label="Dismiss" onClick={() => setToast(null)}><CloseIcon size={13} /></button></div> : null}
    </div>
  );
}
