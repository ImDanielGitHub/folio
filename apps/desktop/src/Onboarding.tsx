import { useState } from "react";
import { ArrowIcon, CheckIcon, PrivacyIcon, SourceIcon, SparkIcon } from "./icons";
import type { BackendHealth } from "./transport";
import type { ModelMode } from "./types";

export type OnboardingSource = "demo" | "akahu" | "csv";

type OnboardingProps = {
  initialMode: ModelMode;
  backend: BackendHealth;
  onModeChange: (mode: ModelMode) => void;
  onComplete: (sourceChoice: OnboardingSource, file: File | null, businessContext: string) => void;
};

type Panel = "source" | "akahu" | "privacy";

const asideSteps = [
  { key: "source", label: "Bring the facts" },
  { key: "privacy", label: "Choose privacy" },
] as const;

export function Onboarding({ initialMode, backend, onModeChange, onComplete }: OnboardingProps) {
  const [panel, setPanel] = useState<Panel>("source");
  const [sourceChoice, setSourceChoice] = useState<OnboardingSource>("demo");
  const [csvFile, setCsvFile] = useState<File | null>(null);
  const [akahuAccount, setAkahuAccount] = useState<"anz_everyday" | "asb_later">("anz_everyday");

  const modeStatus = (mode: ModelMode): string => {
    if (backend.mode !== "live") return "Checked when local service connects";
    if (mode === "local") return backend.lmStudioReady ? "LM Studio ready" : "LM Studio unavailable";
    if (backend.cloudReady) return "OpenAI ready";
    return backend.cloudCredentialState === "absent" ? "OpenAI key absent" : "OpenAI unavailable";
  };

  const sourceComplete = sourceChoice !== "csv" || Boolean(csvFile);
  const asideIndex = panel === "privacy" ? 1 : 0;

  const goNext = () => {
    if (panel === "source") {
      if (sourceChoice === "akahu") {
        setPanel("akahu");
        return;
      }
      setPanel("privacy");
      return;
    }
    if (panel === "akahu") {
      setPanel("privacy");
      return;
    }
    onComplete(
      sourceChoice,
      csvFile,
      sourceChoice === "akahu"
        ? "Connected ANZ Everyday through Akahu for read-only Open Banking. Protect enough cash for tax and a quiet month."
        : "Protect enough cash for tax and a quiet month. Tell me when something materially changes.",
    );
  };

  const goBack = () => {
    if (panel === "privacy") {
      setPanel(sourceChoice === "akahu" ? "akahu" : "source");
      return;
    }
    if (panel === "akahu") setPanel("source");
  };

  return (
    <div className="onboarding-layer" role="dialog" aria-modal="true" aria-labelledby="onboarding-title">
      <div className="onboarding-shell">
        <aside className="onboarding-aside">
          <div className="onboarding-brand"><span>F.</span><strong>Folio</strong></div>
          <div className="onboarding-promise">
            <p className="eyebrow">Local-first continuous finance</p>
            <h2>Start with the business, not a settings form.</h2>
            <p>Your agent prepares the close, documents its work, and keeps one ongoing conversation with you.</p>
          </div>
          <ol className="onboarding-steps">
            {asideSteps.map((item, index) => (
              <li className={index === asideIndex ? "is-current" : index < asideIndex ? "is-complete" : ""} key={item.key}>
                <span>{index < asideIndex ? <CheckIcon size={13} /> : index + 1}</span><b>{item.label}</b>
              </li>
            ))}
          </ol>
          <p className="onboarding-note"><PrivacyIcon size={15} /> Bank passwords stay with Akahu. Numbers stay on this computer.</p>
        </aside>

        <main className="onboarding-main">
          {panel === "source" ? (
            <div className="onboarding-panel">
              <p className="step-label">Get started</p>
              <h1 id="onboarding-title">Give the agent something true to work from.</h1>
              <p className="panel-lede">Open the sealed Koru Studio demo, connect a bank through Akahu, or import a local CSV. Telegram can be added later from Sources.</p>
              <div className="choice-grid choice-grid-three">
                <button className={`choice-card ${sourceChoice === "demo" ? "is-selected" : ""}`} onClick={() => setSourceChoice("demo")}>
                  <span className="choice-icon"><SparkIcon /></span>
                  <span><strong>Open Koru Studio demo</strong><small>10 synthetic bank rows, one receipt gap, one duplicate, and a real cash decision.</small></span>
                  <i>{sourceChoice === "demo" ? <CheckIcon size={14} /> : null}</i>
                </button>
                <button className={`choice-card choice-card-accent ${sourceChoice === "akahu" ? "is-selected" : ""}`} onClick={() => setSourceChoice("akahu")}>
                  <span className="choice-icon"><PrivacyIcon /></span>
                  <span><strong>Connect with Akahu</strong><small>Read-only Open Banking. Your bank password stays with Akahu — Folio only receives transactions.</small></span>
                  <i>{sourceChoice === "akahu" ? <CheckIcon size={14} /> : null}</i>
                </button>
                <button className={`choice-card ${sourceChoice === "csv" ? "is-selected" : ""}`} onClick={() => setSourceChoice("csv")}>
                  <span className="choice-icon"><SourceIcon /></span>
                  <span><strong>Import local CSV</strong><small>Files remain on this computer. Mapping is reviewed before import.</small></span>
                  <i>{sourceChoice === "csv" ? <CheckIcon size={14} /> : null}</i>
                </button>
              </div>
              {sourceChoice === "csv" ? (
                <label className="csv-picker">
                  <span>Bank CSV</span>
                  <input
                    type="file"
                    accept=".csv,text/csv"
                    onChange={(event) => setCsvFile(event.currentTarget.files?.[0] ?? null)}
                  />
                  <small>{csvFile ? `${csvFile.name} is ready to import locally.` : "Choose a CSV to continue. Nothing is uploaded to a cloud service."}</small>
                </label>
              ) : null}
              <div className="onboarding-callout"><strong>What happens next?</strong><p>Folio ingests quietly, then your continuing thread tells you only what changed or needs context. No stage theatre.</p></div>
            </div>
          ) : null}

          {panel === "akahu" ? (
            <div className="onboarding-panel">
              <p className="step-label">Akahu</p>
              <h1 id="onboarding-title">Connect a bank account.</h1>
              <p className="panel-lede">Akahu uses Open Banking so Folio can read transactions. Your bank password stays with Akahu — it never reaches Folio.</p>
              <div className="akahu-accounts" role="radiogroup" aria-label="Accounts to connect">
                <button
                  className={`privacy-choice ${akahuAccount === "anz_everyday" ? "is-selected" : ""}`}
                  role="radio"
                  aria-checked={akahuAccount === "anz_everyday"}
                  onClick={() => setAkahuAccount("anz_everyday")}
                >
                  <span className="radio-mark">{akahuAccount === "anz_everyday" ? <i /> : null}</span>
                  <span><strong>ANZ Everyday</strong><small>Primary operating account · read-only · selected for first sync.</small></span>
                  <b>Selected</b>
                </button>
                <button
                  className={`privacy-choice ${akahuAccount === "asb_later" ? "is-selected" : ""}`}
                  role="radio"
                  aria-checked={akahuAccount === "asb_later"}
                  onClick={() => setAkahuAccount("asb_later")}
                >
                  <span className="radio-mark">{akahuAccount === "asb_later" ? <i /> : null}</span>
                  <span><strong>ASB Business</strong><small>Add later from Sources. Not required for the first look.</small></span>
                  <b>Add later</b>
                </button>
              </div>
              <div className="onboarding-callout"><PrivacyIcon size={17} /><div><strong>Consent stays narrow.</strong><p>Folio asks Akahu for transaction history and balances only. You can disconnect from Sources at any time.</p></div></div>
            </div>
          ) : null}

          {panel === "privacy" ? (
            <div className="onboarding-panel">
              <p className="step-label">Privacy</p>
              <h1 id="onboarding-title">Choose where the conversation thinks.</h1>
              <p className="panel-lede">The same deterministic finance engine runs locally in every mode. This only changes the model used for questions and explanations.</p>
              <div className="privacy-choices">
                {([
                  ["local", "Local", "Use a tool-capable model in LM Studio. No model data leaves this computer."],
                  ["hybrid", "Hybrid", "Compute locally, then use OpenAI only when configured for eligible language tasks."],
                  ["cloud", "Cloud", "Use OpenAI only when configured; raw source files remain excluded by default."],
                ] as const).map(([mode, label, detail]) => (
                  <button className={`privacy-choice ${initialMode === mode ? "is-selected" : ""}`} onClick={() => onModeChange(mode)} key={mode}>
                    <span className="radio-mark">{initialMode === mode ? <i /> : null}</span>
                    <span><strong>{label}</strong><small>{detail}</small></span>
                    <b>{modeStatus(mode)}</b>
                  </button>
                ))}
              </div>
              <div className="onboarding-callout"><PrivacyIcon size={17} /><div><strong>Raw records are never the prompt.</strong><p>Deterministic services prepare a smaller, typed projection before any model sees a finance task.</p></div></div>
            </div>
          ) : null}

          <footer className="onboarding-actions">
            <button className="button button-ghost" onClick={goBack} disabled={panel === "source"}>Back</button>
            <button
              className="button button-primary"
              disabled={panel === "source" && !sourceComplete}
              onClick={goNext}
            >
              {panel === "privacy" ? "Meet Folio" : panel === "akahu" ? "Continue to Akahu" : "Continue"}
              <ArrowIcon size={15} />
            </button>
          </footer>
        </main>
      </div>
    </div>
  );
}
