import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Drawer } from "./Drawer";
import { Onboarding } from "./Onboarding";
import { SurfaceRenderer } from "./SurfaceRenderer";
import {
  cashScenarioSurface,
  correctedReceiptSurface,
  dailyCloseStages,
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
  SourceItem,
  SurfaceType,
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
const prefersReducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const scrollBehaviour = (): ScrollBehavior => prefersReducedMotion() ? "auto" : "smooth";

function makeId(prefix: string) {
  return `${prefix}_${Date.now().toString(36)}`;
}

function modelLabel(mode: ModelMode) {
  return mode === "local" ? "Local" : mode === "hybrid" ? "Hybrid" : "Cloud";
}

function runtimeStatusCopy(backend: BackendHealth): string {
  if (backend.mode === "checking") return "Connecting…";
  if (backend.mode === "live") return "Local service connected";
  if (backend.mode === "degraded") return "Local service needs attention";
  if (backend.mode === "offline") return "Offline · last view";
  return "Sealed demo";
}

const liveSurfacePrompts: Record<SurfaceType, string> = {
  living_brief: "Show me the current finance summary.",
  transaction_detail: "Show me the MITRE 10 transaction and its source evidence.",
  cash_scenario: "Show me the current cash forecast and reserve scenario.",
  records_table: "Show me the current transactions.",
  owner_pack: "Prepare and show the current owner pack.",
  work_receipt: "Show me the latest committed work receipt.",
};

function modeSelectionMessage(mode: ModelMode, backend: BackendHealth): string {
  if (backend.mode !== "live") return `${modelLabel(mode)} preference selected for the sealed demo. No external model call is made.`;
  if (mode === "local") {
    return backend.lmStudioReady
      ? "Local mode selected. LM Studio is ready and finance remains local."
      : `Local mode selected, but LM Studio is ${backend.lmStudioStatus}. Folio will use its deterministic local fallback.`;
  }
  if (backend.cloudReady) return `${modelLabel(mode)} mode selected. OpenAI is configured; finance computation remains local.`;
  return `${modelLabel(mode)} mode selected, but OpenAI is unavailable. Folio will use its deterministic local fallback and make no external call.`;
}

type ThreadMessageProps = {
  turn: ThreadTurn;
  appearance?: "lead" | "question" | "standard";
  onEvidence: () => void;
  onUndo: (eventId: string) => void;
  onOpenFinanceView: () => void;
};

function ThreadMessage({ turn, appearance = "standard", onEvidence, onUndo, onOpenFinanceView }: ThreadMessageProps) {
  const offersFinanceView = turn.role === "agent"
    && (appearance === "lead" || /cash|reserve|owner pack|transaction/i.test(turn.content));
  const [leadTitle, ...leadRemainder] = appearance === "lead"
    ? turn.content.split(/(?<=\.)\s+/, 2)
    : [turn.content];
  const leadBody = leadRemainder.join(" ");
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
          <button className="message-evidence" onClick={onEvidence}>
            <SourceIcon size={13} />
            Based on {turn.evidenceIds.length} linked source{turn.evidenceIds.length === 1 ? "" : "s"}
          </button>
        ) : null}
        {turn.receipt ? (
          <div className="inline-receipt">
            <CheckIcon size={13} />
            <span>{turn.receipt.label}</span>
            {turn.receipt.undoable && turn.receipt.eventId ? (
              <button onClick={() => onUndo(turn.receipt!.eventId!)}>Undo change</button>
            ) : null}
          </div>
        ) : null}
        {offersFinanceView ? (
          <button className="inline-surface-link" onClick={onOpenFinanceView}>
            View the current picture <BriefIcon size={14} />
          </button>
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
  const [surface, setSurface] = useState<FinanceSurfaceSpec>(livingBriefSurface);
  const [turns, setTurns] = useState<ThreadTurn[]>(workspaceFixture.thread.turns);
  const [sources, setSources] = useState<SourceItem[]>(initialSources);
  const [activity, setActivity] = useState<ActivityItem[]>(initialActivity);
  const [drawer, setDrawer] = useState<DrawerKind | null>(null);
  const [composer, setComposer] = useState("");
  const [running, setRunning] = useState(false);
  const [, setActiveStage] = useState(-1);
  const [stageProgress, setStageProgress] = useState(0);
  const [mobilePane, setMobilePane] = useState<"thread" | "canvas">("thread");
  const [threadWidth, setThreadWidth] = useState(() => Number(localStorage.getItem("folio:thread-width")) || 520);
  const [canvasOpen, setCanvasOpen] = useState(false);
  const [canvasFocus, setCanvasFocus] = useState(false);
  const [surfaceMenuOpen, setSurfaceMenuOpen] = useState(false);
  const [showJumpToLatest, setShowJumpToLatest] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  const [telegramImported, setTelegramImported] = useState(false);
  const [correctionActive, setCorrectionActive] = useState(false);
  const runToken = useRef(0);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const threadScrollRef = useRef<HTMLDivElement>(null);
  const shouldAutoScrollRef = useRef(true);

  const showToast = useCallback((message: string) => {
    setToast(message);
    window.setTimeout(() => setToast((current) => current === message ? null : current), 2600);
  }, []);

  const applySnapshot = useCallback((snapshot: typeof workspaceFixture) => {
    setSurface(snapshot.currentSurface);
    setTurns(snapshot.thread.turns);
    setSources(snapshot.sources);
    setActivity(snapshot.activity);
    setModelMode(snapshot.modelMode);
    setTelegramImported(snapshot.sources.some((source) => source.sourceType === "telegram_fixture" && source.status === "processed"));
    setCorrectionActive(snapshot.activity.some((item) => item.eventId === "evt_koru_mitre_rule_created" && item.status !== "undone"));
  }, []);

  const markDegraded = useCallback((operation: string) => {
    setBackend((current) => ({
      ...current,
      mode: "degraded",
      label: "Local service needs attention",
      detail: `${operation} did not finish. The last committed view is unchanged.`,
    }));
  }, []);

  useEffect(() => {
    let active = true;
    if (skipOnboarding) {
      setBackend({
        mode: "fixture",
        label: "Demo data",
        detail: "The sealed Koru Studio fixture is active.",
        apiUrl: "http://127.0.0.1:8787",
        lmStudioReady: false,
        lmStudioStatus: "not checked",
        cloudReady: false,
        cloudCredentialState: "absent",
      });
      return () => { active = false; };
    }
    const refreshLiveSnapshot = async () => {
      const nextBackend = await probeBackend();
      if (!active) return;
      if (nextBackend.mode === "live") {
        try {
          const live = await loadSnapshot("ws_koru_studio");
          if (!active) return;
          applySnapshot(live);
          setBackend(nextBackend);
        } catch {
          if (!active) return;
          setBackend({
            ...nextBackend,
            mode: "degraded",
            label: "Local service needs attention",
            detail: "The service responded, but its workspace could not be loaded. The last committed view remains visible.",
          });
        }
        return;
      }
      setBackend(nextBackend);
    };
    void refreshLiveSnapshot();
    const onOffline = () => setBackend((current) => ({ ...current, mode: "offline", label: "Offline demo", detail: "Existing local data remains available." }));
    const onOnline = () => void refreshLiveSnapshot();
    window.addEventListener("offline", onOffline);
    window.addEventListener("online", onOnline);
    return () => {
      active = false;
      window.removeEventListener("offline", onOffline);
      window.removeEventListener("online", onOnline);
    };
  }, [applySnapshot, skipOnboarding]);

  useEffect(() => {
    if (shouldAutoScrollRef.current) {
      messagesEndRef.current?.scrollIntoView({ behavior: scrollBehaviour(), block: "end" });
      setShowJumpToLatest(false);
    } else {
      setShowJumpToLatest(true);
    }
  }, [turns, running]);

  const completeOnboarding = async (sourceChoice: "demo" | "csv", csvFile: File | null): Promise<void> => {
    if (backend.mode === "live") {
      try {
        if (sourceChoice === "csv" && csvFile) {
          await importCsv(csvFile);
          const close = await runDailyClose();
          await readRunEvents(close.runId);
        }
        applySnapshot(await loadSnapshot("ws_koru_studio"));
      } catch {
        throw new Error(sourceChoice === "csv"
          ? "The local service could not commit this import. Use the exact Folio CSV template, then try again; a repeated import is deduplicated."
          : "The local service could not open this workspace. Keep this screen open and try again.");
      }
      try {
        localStorage.setItem("folio:onboarded", "yes");
      } catch {
        // The committed local thread remains authoritative if browser storage is unavailable.
      }
      setShowOnboarding(false);
      showToast(sourceChoice === "csv" && csvFile
        ? `${csvFile.name} was imported and Folio opened the most useful next question.`
        : "Koru Studio is ready. Folio opened the most useful next question.");
      return;
    }
    if (backend.mode !== "fixture") {
      throw new Error("Folio could not verify a ready local workspace. Nothing was saved or substituted; keep this setup open and try again when the local service is available.");
    }
    if (sourceChoice === "csv") {
      throw new Error("Start the local Folio service before importing a CSV, or go back and choose the sealed Koru Studio demo.");
    }
    try {
      localStorage.setItem("folio:onboarded", "yes");
    } catch {
      // Fixture mode remains usable for this session even without browser storage.
    }
    setShowOnboarding(false);
    showToast("Koru Studio is ready in sealed demo mode. No external model call was made.");
  };

  const openSurface = useCallback((next: FinanceSurfaceSpec) => {
    setSurface(next);
    setCanvasOpen(true);
    setCanvasFocus(false);
    setMobilePane("canvas");
  }, []);

  const requestSurface = useCallback(async (surfaceType: SurfaceType, fixtureSurface: FinanceSurfaceSpec) => {
    if (backend.mode === "fixture") {
      openSurface(fixtureSurface);
      return;
    }
    if (backend.mode !== "live") {
      showToast("The local service is not ready. The last committed view is unchanged.");
      return;
    }
    if (running) {
      showToast("Finish or stop the current work before opening another live financial view.");
      return;
    }
    const token = ++runToken.current;
    setRunning(true);
    setActiveStage(-1);
    setStageProgress(0);
    try {
      const run = await postTurn(workspaceFixture.thread.threadId, liveSurfacePrompts[surfaceType], modelMode);
      await readRunEvents(run.runId);
      if (runToken.current !== token) return;
      const snapshot = await loadSnapshot("ws_koru_studio");
      if (runToken.current !== token) return;
      applySnapshot(snapshot);
      setCanvasOpen(true);
      setCanvasFocus(false);
      setMobilePane("canvas");
      if (snapshot.currentSurface.surfaceType !== surfaceType) {
        showToast(`Folio opened the live ${snapshot.currentSurface.title} view available for that request.`);
      }
    } catch {
      if (runToken.current === token) {
        markDegraded("Opening the financial view");
        showToast("That live view could not be opened. The last committed view is unchanged.");
      }
    } finally {
      if (runToken.current === token) {
        setRunning(false);
        setActiveStage(-1);
      }
    }
  }, [applySnapshot, backend.mode, markDegraded, modelMode, openSurface, running, showToast]);

  const stopCurrentRun = useCallback(() => {
    runToken.current += 1;
    setRunning(false);
    setActiveStage(-1);
    setTurns((current) => [...current, {
      turnId: makeId("turn_stopped"),
      role: "agent",
      content: "I stopped waiting in this window. Anything already committed remains in place, and background work may still finish. Refresh before relying on this result.",
      occurredAt: nowIso(),
      status: "stopped",
      evidenceIds: [],
    }]);
  }, []);

  const handleDailyClose = useCallback(async () => {
    if (running) return;
    if (backend.mode !== "live" && backend.mode !== "fixture") {
      showToast("Daily Close needs the local service. No result was applied.");
      return;
    }
    const token = ++runToken.current;
    setRunning(true);
    setActiveStage(0);
    setStageProgress(0);
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
        await readRunEvents(run.runId);
        if (runToken.current !== token) return;
        liveSnapshot = await loadSnapshot("ws_koru_studio");
      } catch {
        if (runToken.current !== token) return;
        markDegraded("Daily Close");
        setActivity((items) => items.map((item) => item.activityId === activityId ? {
          ...item,
          summary: "Daily Close did not finish",
          detail: "No fixture result was substituted. The last committed finance view remains unchanged.",
          status: "failed",
        } : item));
        setTurns((current) => [...current, {
          turnId: makeId("turn_close_failed"),
          role: "agent",
          content: "Daily Close did not finish through the local service. I have not substituted demo results or claimed a change. Your last committed figures are still on screen.",
          occurredAt: nowIso(),
          status: "stopped",
          evidenceIds: [],
        }]);
        setRunning(false);
        setActiveStage(-1);
        setStageProgress(0);
        showToast("Daily Close did not finish. No result was applied.");
        return;
      }
    }

    for (let index = 0; index < dailyCloseStages.length; index += 1) {
      if (runToken.current !== token) return;
      setActiveStage(index);
      setStageProgress(index / dailyCloseStages.length);
      await sleep(index === 0 ? 380 : 520);
    }
    if (runToken.current !== token) return;
    setStageProgress(1);
    await sleep(220);
    if (runToken.current !== token) return;

    if (liveSnapshot) {
      applySnapshot(liveSnapshot);
      setRunning(false);
      setActiveStage(-1);
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
    setActiveStage(-1);
    showToast("Daily Close complete — no duplicate work created.");
  }, [applySnapshot, backend.mode, markDegraded, running, showToast]);

  const applyCorrection = useCallback(async (ownerText: string, token: number) => {
    await sleep(780);
    if (runToken.current !== token) return;
    setCorrectionActive(true);
    setTurns((current) => [...current, {
      turnId: makeId("turn_rule_complete"),
      role: "agent",
      content: "Understood. I recorded that as client fit-out materials, reclassified the NZD 184.75 MITRE 10 transaction, and added a narrow rule for matching MITRE 10 purchases up to NZD 500. I did not change the cash amount, only its bookkeeping meaning.",
      occurredAt: nowIso(),
      status: "complete",
      evidenceIds: ["evd_koru_mitre10_row", "evd_koru_owner_claim_mitre"],
      receipt: { label: "1 transaction changed · narrow rule saved", eventId: "evt_koru_mitre_rule_created", undoable: true },
    }]);
    setActivity((items) => [{
      activityId: makeId("activity_rule"),
      kind: "finance_event",
      summary: "MITRE 10 correction applied",
      detail: `Owner said: “${ownerText.slice(0, 110)}${ownerText.length > 110 ? "…" : ""}” Affected transaction: txn_koru_006.`,
      status: "completed",
      occurredAt: nowIso(),
      undoable: true,
      eventId: "evt_koru_mitre_rule_created",
      evidenceIds: ["evd_koru_mitre10_row", "evd_koru_owner_claim_mitre"],
    }, ...items]);
    openSurface(correctedReceiptSurface);
    setRunning(false);
    setActiveStage(-1);
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
    setActiveStage(-1);
    setStageProgress(0);
    if (backend.mode === "live") {
      try {
        const run = await postTurn(workspaceFixture.thread.threadId, content, modelMode);
        await readRunEvents(run.runId);
        if (runToken.current !== token) return;
        const snapshot = await loadSnapshot("ws_koru_studio");
        if (runToken.current !== token) return;
        applySnapshot(snapshot);
        setRunning(false);
        setActiveStage(-1);
        return;
      } catch {
        if (runToken.current !== token) return;
        markDegraded("Sending that message");
        setTurns((current) => [...current, {
          turnId: makeId("turn_message_failed"),
          role: "agent",
          content: "I could not finish that request through the local service. I have kept your message, but I have not applied a finance change or substituted a demo answer.",
          occurredAt: nowIso(),
          status: "stopped",
          evidenceIds: [],
        }]);
        setRunning(false);
        setActiveStage(-1);
        showToast("That request did not finish. No finance change was applied.");
        return;
      }
    }
    if (backend.mode !== "fixture") {
      setTurns((current) => [...current, {
        turnId: makeId("turn_service_unavailable"),
        role: "agent",
        content: "The local service is not ready, so I have not answered from demo data or applied a finance change. Your message remains in this conversation.",
        occurredAt: nowIso(),
        status: "stopped",
        evidenceIds: [],
      }]);
      setRunning(false);
      setActiveStage(-1);
      return;
    }
    const normalised = content.toLowerCase();
    if (normalised.includes("mitre") || normalised.includes("fit-out") || normalised.includes("materials")) {
      await applyCorrection(content, token);
      return;
    }
    await sleep(620);
    if (runToken.current !== token) return;
    let response = "I have kept that as an owner claim and linked it to this conversation. I do not need another answer yet; I will use it when the next source or Daily Close makes it relevant.";
    if (normalised.includes("laptop") || normalised.includes("reserve") || normalised.includes("cash")) {
      response = "The laptop timing is the material lever. Paying on 7 August takes the projected low to NZD 1,900.77, which is NZD 99.23 below your protected reserve. Deferring it keeps the low at NZD 4,900.77.";
      openSurface(cashScenarioSurface);
    } else if (normalised.includes("pack") || normalised.includes("accountant") || normalised.includes("document")) {
      response = "I prepared the owner pack from the same committed figures. It includes source coverage, unresolved evidence, the cash projection, and the exact assumptions behind it.";
      openSurface(ownerPackSurface);
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
    setActiveStage(-1);
  }, [applyCorrection, applySnapshot, backend.mode, composer, markDegraded, modelMode, openSurface, running, showToast]);

  const handleUndo = useCallback(async (eventId: string) => {
    if (backend.mode === "fixture" && !correctionActive && eventId === "evt_koru_mitre_rule_created") {
      showToast("That correction is already inactive.");
      return;
    }
    if (backend.mode === "live") {
      try {
        await undoEvent(eventId);
        applySnapshot(await loadSnapshot("ws_koru_studio"));
        setCorrectionActive(false);
        setDrawer(null);
        showToast("Inverse event committed locally; history is preserved.");
        return;
      } catch {
        markDegraded("Undo");
        showToast("Undo did not finish. The last committed history is unchanged.");
        return;
      }
    }
    if (backend.mode !== "fixture") {
      showToast("Undo needs the local service. No history was changed.");
      return;
    }
    setCorrectionActive(false);
    setActivity((items) => [{
      activityId: makeId("activity_inverse"),
      kind: "finance_event",
      summary: "MITRE 10 correction undone",
      detail: "Inverse event applied; the original event remains in history.",
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
      receipt: { label: "Inverse event committed" },
    }]);
    setDrawer(null);
    openSurface(undoneReceiptSurface);
    showToast("Inverse event applied; history preserved.");
  }, [applySnapshot, backend.mode, correctionActive, markDegraded, openSurface, showToast]);

  const importTelegram = useCallback(async () => {
    if (telegramImported) return;
    if (backend.mode === "live") {
      try {
        await ingestTelegramFixture();
        applySnapshot(await loadSnapshot("ws_koru_studio"));
        setTelegramImported(true);
        showToast("Telegram receipt prepared through the local service.");
        return;
      } catch {
        markDegraded("Processing the Telegram message");
        showToast("The Telegram message was not processed. No demo result was substituted.");
        return;
      }
    }
    if (backend.mode !== "fixture") {
      showToast("Processing messages needs the local service. Nothing was added.");
      return;
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
  }, [applySnapshot, backend.mode, markDegraded, showToast, telegramImported]);

  const handleReset = useCallback(async () => {
    if (backend.mode === "live") {
      try {
        await resetDemo();
        const run = await runDailyClose();
        await readRunEvents(run.runId);
        applySnapshot(await loadSnapshot("ws_koru_studio"));
        runToken.current += 1;
        setTelegramImported(false);
        setCorrectionActive(false);
        setRunning(false);
        setDrawer(null);
        showToast("Koru Studio reset and rebuilt through the local service.");
        return;
      } catch {
        markDegraded("Resetting Koru Studio");
        showToast("Reset did not finish. The current committed workspace was preserved.");
        return;
      }
    }
    if (backend.mode !== "fixture") {
      showToast("Reset needs the local service. Nothing was changed.");
      return;
    }
    runToken.current += 1;
    setTurns(workspaceFixture.thread.turns);
    setSources(initialSources);
    setActivity(initialActivity);
    setSurface(livingBriefSurface);
    setTelegramImported(false);
    setCorrectionActive(false);
    setRunning(false);
    setDrawer(null);
    showToast("Koru Studio reset to the sealed demo state.");
  }, [applySnapshot, backend.mode, markDegraded, showToast]);

  const openGeneratedArtifact = useCallback(async (artifactId: string) => {
    if (backend.mode === "fixture") {
      showToast("Owner pack preview is running from the sealed demo fixture.");
      return;
    }
    if (backend.mode !== "live") {
      showToast("The owner pack cannot open until the local service is ready.");
      return;
    }
    showToast("Opening the generated owner pack…");
    try {
      await openArtifact(artifactId);
    } catch {
      markDegraded("Opening the generated owner pack");
      showToast("The owner pack could not be opened. No demo document was substituted.");
    }
  }, [backend.mode, markDegraded, showToast]);

  const handleSurfaceAction = useCallback((action: FinanceAction) => {
    if (action.type === "open_drawer") setDrawer(action.drawer);
    if (action.type === "focus_source") setDrawer("sources");
    if (action.type === "run_scenario") void requestSurface("cash_scenario", cashScenarioSurface);
    if (action.type === "undo_event") void handleUndo(action.eventId);
    if (action.type === "download_artifact") {
      void openGeneratedArtifact(action.artifactId);
    }
  }, [handleUndo, openGeneratedArtifact, requestSurface]);

  const changeMode = useCallback((mode: ModelMode) => {
    setModelMode(mode);
    showToast(modeSelectionMessage(mode, backend));
  }, [backend, showToast]);

  const canvasNav = useMemo(() => [
    { label: "Brief", icon: BriefIcon, surfaceType: "living_brief" as const, fixtureSurface: livingBriefSurface },
    { label: "Cash", icon: CashIcon, surfaceType: "cash_scenario" as const, fixtureSurface: cashScenarioSurface },
    { label: "Records", icon: RecordsIcon, surfaceType: "records_table" as const, fixtureSurface: recordsSurface },
    { label: "Owner pack", icon: PackIcon, surfaceType: "owner_pack" as const, fixtureSurface: ownerPackSurface },
  ], []);

  const closeDrawer = useCallback(() => setDrawer(null), []);

  const startResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (window.innerWidth < 1080) return;
    const startX = event.clientX;
    const startWidth = threadWidth;
    let finalWidth = threadWidth;
    const onMove = (moveEvent: PointerEvent) => {
      const next = Math.min(560, Math.max(360, startWidth + moveEvent.clientX - startX));
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

  const resizeWithKeyboard = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const delta = event.key === "ArrowRight" ? 24 : -24;
    setThreadWidth((current) => {
      const next = Math.min(560, Math.max(360, current + delta));
      localStorage.setItem("folio:thread-width", String(next));
      return next;
    });
  };

  const closeCanvas = useCallback(() => {
    setCanvasOpen(false);
    setCanvasFocus(false);
    setSurfaceMenuOpen(false);
    setMobilePane("thread");
    showToast("Financial view closed. Your conversation is unchanged.");
  }, [showToast]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || drawer) return;
      if (surfaceMenuOpen) {
        setSurfaceMenuOpen(false);
        return;
      }
      if (canvasFocus) {
        setCanvasFocus(false);
        return;
      }
      if (canvasOpen) closeCanvas();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [canvasFocus, canvasOpen, closeCanvas, drawer, surfaceMenuOpen]);

  const jumpToLatest = () => {
    shouldAutoScrollRef.current = true;
    messagesEndRef.current?.scrollIntoView({ behavior: scrollBehaviour(), block: "end" });
    setShowJumpToLatest(false);
  };

  return (
    <div
      className={`app-shell ${canvasOpen ? "has-canvas" : "is-conversation-only"} ${canvasFocus ? "is-canvas-focus" : ""}`}
      style={{ "--thread-width": `${threadWidth}px` } as React.CSSProperties}
    >
      <nav className="rail" aria-label="Workspace controls">
        <button className="brand-button is-active" aria-label="Return to conversation" onClick={closeCanvas}>F</button>
        <div className="rail-actions">
          <button className={drawer === "sources" ? "is-active" : ""} aria-label="Open evidence" title="Evidence" onClick={() => setDrawer("sources")}><SourceIcon /></button>
          <button className={drawer === "activity" ? "is-active" : ""} aria-label="Open audit trail" title="Audit trail" onClick={() => setDrawer("activity")}><ActivityIcon /></button>
          <button className={drawer === "connections" ? "is-active" : ""} aria-label="Open privacy and model settings" title="Privacy & models" onClick={() => setDrawer("connections")}><PrivacyIcon /></button>
        </div>
        <div className="rail-bottom">
          <span className={`rail-runtime runtime-${backend.mode}`} title={backend.detail} />
          <button aria-label="More options" title="More options"><MoreIcon /></button>
        </div>
      </nav>

      <div className="workspace-frame">
        {canvasOpen ? (
          <div className="mobile-tabs" role="tablist" aria-label="Workspace pane">
            <button id="conversation-tab" role="tab" aria-controls="conversation-panel" aria-selected={mobilePane === "thread"} tabIndex={mobilePane === "thread" ? 0 : -1} onClick={() => setMobilePane("thread")}>Conversation</button>
            <button id="document-tab" role="tab" aria-controls="document-panel" aria-selected={mobilePane === "canvas"} tabIndex={mobilePane === "canvas" ? 0 : -1} onClick={() => setMobilePane("canvas")}>Document</button>
          </div>
        ) : null}

        <section id="conversation-panel" role={canvasOpen ? "tabpanel" : undefined} aria-labelledby={canvasOpen ? "conversation-tab" : undefined} className={`thread-pane ${mobilePane === "thread" ? "is-mobile-active" : ""}`}>
          <header className="thread-header">
            <div className="workspace-identity">
              <strong>Koru Studio</strong>
              <span>Your business</span>
            </div>
            <div className="thread-header-actions">
              <button className="privacy-chip" title={backend.detail} onClick={() => setDrawer("connections")}>
                <span className={`mode-dot runtime-${backend.mode}`} />
                {runtimeStatusCopy(backend)}
              </button>
              <button className="quiet-close-button" onClick={() => void handleDailyClose()} disabled={running}>
                <SparkIcon size={14} /> Daily Close
              </button>
            </div>
          </header>

          {backend.mode === "offline" || backend.mode === "degraded" ? (
            <div className="degraded-banner" role="status">
              <strong>{backend.mode === "offline" ? "Offline." : "Local service needs attention."}</strong> {backend.mode === "offline" ? "Your last committed local view is still available." : "No demo result will replace a failed live operation."}
            </div>
          ) : null}

          <div
            className="thread-scroll"
            ref={threadScrollRef}
            onScroll={(event) => {
              const node = event.currentTarget;
              const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight < 160;
              shouldAutoScrollRef.current = nearBottom;
              if (nearBottom) setShowJumpToLatest(false);
            }}
          >
            <div className="conversation-column">
              <div className="thread-day"><span>Today</span></div>
              {turns.map((turn, index) => (
                <ThreadMessage
                  turn={turn}
                  appearance={turn.role === "agent" && index === 0 ? "lead" : turn.role === "agent" && index === 1 ? "question" : "standard"}
                  key={turn.turnId}
                  onEvidence={() => setDrawer("sources")}
                  onUndo={(eventId) => void handleUndo(eventId)}
                  onOpenFinanceView={() => {
                    setCanvasOpen(true);
                    setCanvasFocus(false);
                    setMobilePane("canvas");
                  }}
                />
              ))}
              {running ? (
                <article className="progress-card" aria-live="polite">
                  <div className="progress-heading">
                    <span className="agent-avatar"><SparkIcon size={14} /></span>
                    <div>
                      <strong>Folio is working</strong>
                      <span>Checking your committed facts and linked sources</span>
                    </div>
                  </div>
                  <div className="progress-bar" aria-hidden="true"><i style={{ width: `${Math.max(stageProgress * 100, 18)}%` }} /></div>
                </article>
              ) : null}
              <div ref={messagesEndRef} />
            </div>
          </div>

          {showJumpToLatest ? <button className="jump-latest" onClick={jumpToLatest}>Jump to latest</button> : null}

          <div className="composer-area">
            <div className="conversation-column">
              {!running ? (
                <div className="suggestion-row">
                  <button onClick={() => setComposer("The MITRE 10 purchase was materials for a client fit-out. Treat similar purchases under $500 the same way.")}>Explain MITRE 10</button>
                  <button onClick={() => setComposer("Show me what happens if I defer the laptop purchase.")}>Test laptop timing</button>
                </div>
              ) : null}
              <div className={`composer ${running ? "is-running" : ""}`}>
                <textarea
                  value={composer}
                  onChange={(event) => setComposer(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      void submitTurn();
                    }
                  }}
                  placeholder="Ask Folio, add context, or correct anything…"
                  rows={3}
                  disabled={running}
                  aria-label="Message Folio"
                />
                <div className="composer-footer">
                  <span><PrivacyIcon size={13} /> {running ? "Working from committed facts" : modelMode === "local" ? "Private on this device" : "Finance computation stays local"}</span>
                  {running ? (
                    <button className="stop-button" onClick={stopCurrentRun}><StopIcon size={14} /> Stop &amp; synthesise</button>
                  ) : (
                    <button className="send-button" aria-label="Send message" disabled={!composer.trim()} onClick={() => void submitTurn()}><SendIcon size={16} /></button>
                  )}
                </div>
              </div>
              <p className="composer-disclaimer">Finance preparation, not tax filing or financial advice.</p>
            </div>
          </div>
        </section>

        {canvasOpen ? <div className="splitter" role="separator" aria-orientation="vertical" aria-label="Resize conversation" aria-valuemin={360} aria-valuemax={560} aria-valuenow={threadWidth} tabIndex={0} onKeyDown={resizeWithKeyboard} onPointerDown={startResize} /> : null}

        {canvasOpen ? (
          <main id="document-panel" role="tabpanel" aria-labelledby="document-tab" className={`canvas-pane ${mobilePane === "canvas" ? "is-mobile-active" : ""}`}>
            <header className="canvas-toolbar">
              <div className="canvas-title-group">
                <span>Financial view</span>
                <strong>{surface.title}</strong>
              </div>
              <div className="canvas-actions">
                <span className={`sync-status sync-${backend.mode}`} title={backend.detail}><i />{backend.mode === "checking" ? "Connecting…" : backend.mode === "live" ? "Live local data" : backend.mode === "fixture" ? "Sealed demo data" : backend.mode === "degraded" ? "Last committed view" : "Offline view"}</span>
                <div className="surface-menu-wrap">
                  <button className="canvas-tool-button" aria-expanded={surfaceMenuOpen} onClick={() => setSurfaceMenuOpen((open) => !open)}><MoreIcon size={16} /> Views</button>
                  {surfaceMenuOpen ? (
                    <div className="surface-menu" role="menu">
                      {canvasNav.map((item) => {
                        const Icon = item.icon;
                        return (
                          <button
                            role="menuitem"
                            className={surface.surfaceType === item.surfaceType ? "is-active" : ""}
                            onClick={() => {
                              setSurfaceMenuOpen(false);
                              void requestSurface(item.surfaceType, item.fixtureSurface);
                            }}
                            key={item.label}
                          >
                            <Icon size={15} />
                            <span><strong>{item.label}</strong><small>{item.surfaceType === "living_brief" ? "What matters now" : item.surfaceType === "cash_scenario" ? "Timing and reserve" : item.surfaceType === "records_table" ? "Prepared transactions" : "Shareable document"}</small></span>
                          </button>
                        );
                      })}
                    </div>
                  ) : null}
                </div>
                <button className="canvas-tool-button" aria-pressed={canvasFocus} onClick={() => setCanvasFocus((focus) => !focus)}>{canvasFocus ? "Show chat" : "Focus"}</button>
                <button className="icon-close-button" aria-label="Close financial view" onClick={closeCanvas}><CloseIcon size={16} /></button>
              </div>
            </header>

            <div className="canvas-scroll">
              <SurfaceRenderer
                surface={surface}
                onAction={handleSurfaceAction}
                onFinding={(findingId) => {
                  if (findingId === "finding_koru_missing_receipt") {
                    void requestSurface("transaction_detail", transactionDetailSurface);
                  } else if (findingId === "finding_koru_reserve_risk") {
                    void requestSurface("cash_scenario", cashScenarioSurface);
                  } else {
                    void requestSurface("records_table", recordsSurface);
                  }
                }}
              />
            </div>
          </main>
        ) : null}
      </div>

      <Drawer
        kind={drawer}
        sources={sources}
        activity={activity}
        modelMode={modelMode}
        backend={backend}
        telegramImported={telegramImported}
        onClose={closeDrawer}
        onModeChange={changeMode}
        onImportTelegram={() => void importTelegram()}
        onUndo={(eventId) => void handleUndo(eventId)}
        onReset={() => void handleReset()}
      />

      {showOnboarding ? <Onboarding backend={backend} onComplete={completeOnboarding} /> : null}
      {toast ? <div className="toast" role="status"><CheckIcon size={15} />{toast}<button aria-label="Dismiss" onClick={() => setToast(null)}><CloseIcon size={13} /></button></div> : null}
    </div>
  );
}
